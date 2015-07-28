# coding=utf8
from datetime import datetime
from hashlib import sha1
from math import log
import operator
import random
import re

from config import CONFIG as cfg
from db import _key, msetbit, sequential_id, first_key_with_bit_set

# This is pretty restrictive, but we can always relax it later.
VALID_EXPERIMENT_ALTERNATIVE_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]*$", re.I)
VALID_KPI_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]*$", re.I)


class Client(object):

    def __init__(self, client_id, redis=None):
        self.redis = redis
        self.client_id = client_id


class Experiment(object):

    def __init__(self, name, alternatives,
        winner=False,
        traffic_fraction=False,
        explore_fraction=0.1,
        algorithm=None,
        redis=None):

        if len(alternatives) < 2:
            raise ValueError('experiments require at least two alternatives')

        self.name = name
        self.redis = redis
        self.alternatives = self.initialize_alternatives(alternatives)
        self.kpi = None

        self._algorithm = algorithm

        # False here is a sentinal value for "not looked up yet"
        self._winner = winner
        self._traffic_fraction = traffic_fraction
        self._explore_fraction = explore_fraction
        self._sequential_ids = dict()

    def __repr__(self):
        return '<Experiment: {0})>'.format(self.name)

    def objectify_by_period(self, period, slim=False):
        if period not in ['day', 'month', 'year']:
            raise ValueError("Unrecognized stat range: {0}".format(stat_range))

        objectified = {
            'name': self.name,
            'period': period,
            'alternatives': [],
            'created_at': self.created_at,
            'traffic_fraction': self.traffic_fraction,
            'explore_fraction': self.explore_fraction,
            'excluded_clients': self.excluded_clients(),
            'total_participants': self.total_participants(),
            'total_conversions': self.total_conversions(),
            'description': self.description,
            'has_winner': self.winner is not None,
            'winner': self.winner.name if self.winner is not None else '',
            'is_archived': self.is_archived(),
            'kpis': list(self.kpis),
            'kpi': self.kpi
        }

        data = []

        if period == "day":
            exclusions = self.exclusions_by_day()
        elif period == "month":
            exclusions = self.exclusions_by_month()
        elif period == "year":
            exclusions = self.exclusions_by_year()

        dates = sorted(list(set(exclusions)))
        for date in dates:
            _data = {
                'exclusions': exclusions.get(date, 0),
                'date': date
            }
            data.append(_data)
        objectified["excluded_client_stats"] = data

        for alternative in self.alternatives:
            objectified_alt = alternative.objectify_by_period(period, slim)
            objectified['alternatives'].append(objectified_alt)

        if slim:
            for key in ['period', 'kpi', 'kpis', 'has_winner']:
                del(objectified[key])

        return objectified

    def initialize_alternatives(self, alternatives):
        for alternative_name in alternatives:
            if not Alternative.is_valid(alternative_name):
                raise ValueError('invalid alternative name')

        return [Alternative(n, self, redis=self.redis) for n in alternatives]

    def save(self):
        pipe = self.redis.pipeline()
        if self.is_new_record():
            pipe.sadd(_key('e'), self.name)

        pipe.hset(self.key(), 'algorithm', self._algorithm)
        pipe.hset(self.key(), 'created_at', datetime.now().strftime("%Y-%m-%d %H:%M"))
        pipe.hset(self.key(), 'traffic_fraction', self._traffic_fraction)
        pipe.hset(self.key(), 'explore_fraction', self._explore_fraction)

        # reverse here and use lpush to keep consistent with using lrange
        for alternative in reversed(self.alternatives):
            pipe.lpush("{0}:alternatives".format(self.key()), alternative.name)

        pipe.execute()

    @property
    def control(self):
        return self.alternatives[0]

    @property
    def created_at(self):
        # Note: the split here is to correctly format legacy dates
        try:
            return self.redis.hget(self.key(), 'created_at').split('.')[0]
        except (AttributeError) as e:
            return None

    def get_alternative_names(self):
        return [alt.name for alt in self.alternatives]

    def is_new_record(self):
        return not self.redis.sismember(_key("e"), self.name)

    def total_participants(self):
        key = _key("p:{0}:_all:all".format(self.name))
        return self.redis.bitcount(key)

    def participants_by_day(self):
        return self._get_stats('participations', 'days')

    def participants_by_month(self):
        return self._get_stats('participations', 'months')

    def participants_by_year(self):
        return self._get_stats('participations', 'years')

    def total_conversions(self):
        key = _key("c:{0}:_all:users:all".format(self.kpi_key()))
        return self.redis.bitcount(key)

    def conversions_by_day(self):
        return self._get_stats('conversions', 'days')

    def conversions_by_month(self):
        return self._get_stats('conversions', 'months')

    def conversions_by_year(self):
        return self._get_stats('conversions', 'years')

    def exclusions_by_day(self):
        return self._get_stats('exclusions', 'days')

    def exclusions_by_month(self):
        return self._get_stats('exclusions', 'months')

    def exclusions_by_year(self):
        return self._get_stats('exclusions', 'years')

    def _get_stats(self, stat_type, stat_range):
        if stat_type == 'participations':
            stat_type = 'p'
            exp_key = self.name
            pattern = "{0}:{1}:_all:{2}"
        elif stat_type == 'conversions':
            stat_type = 'c'
            exp_key = self.kpi_key()
            pattern = "{0}:{1}:_all:users:{2}"
        elif stat_type == 'exclusions':
            stat_type = 'x'
            exp_key = self.name
            pattern = "{0}:{1}:{2}"
        else:
            raise ValueError("Unrecognized stat type: {0}".format(stat_type))

        if stat_range not in ['days', 'months', 'years']:
            raise ValueError("Unrecognized stat range: {0}".format(stat_range))

        pipe = self.redis.pipeline()

        stats = {}
        search_key = _key("{0}:{1}:{2}".format(stat_type, exp_key, stat_range))
        keys = self.redis.smembers(search_key)
        for k in keys:
            range_key = _key(pattern.format(stat_type, self.name, k))
            pipe.bitcount(range_key)

        redis_results = pipe.execute()
        for idx, k in enumerate(keys):
            stats[k] = float(redis_results[idx])

        return stats

    def update_description(self, description=None):
        if description == '' or description is None:
            self.redis.hdel(self.key(), 'description')
        else:
            self.redis.hset(self.key(), 'description', description)

    @property
    def description(self):
        description = self.redis.hget(self.key(), 'description')
        if description:
            return description.decode("utf-8", "replace")
        else:
            return None

    def reset(self):
        name = self.name
        desc = self.description
        alts = self.get_alternative_names()

        self.delete()

        experiment = Experiment(name, alts, redis=self.redis)
        experiment.update_description(desc)
        experiment.save()

    def delete(self):
        pipe = self.redis.pipeline()
        pipe.srem(_key('e'), self.name)
        pipe.delete(self.key())
        pipe.delete(_key(self.name))
        pipe.delete(_key('e:{0}'.format(self.name)))

        # Consider a 'non-keys' implementation of this
        keys = self.redis.keys('*:{0}:*'.format(self.name))
        for key in keys:
            pipe.delete(key)

        # Delete the KPIs as well
        kpi_keys = self.redis.keys('*:{0}/*'.format(self.name))
        for kpi_key in kpi_keys:
            pipe.delete(kpi_key)

        pipe.execute()

    def archive(self):
        self.redis.hset(self.key(), 'archived', 1)

    def unarchive(self):
        self.redis.hdel(self.key(), 'archived')

    def is_archived(self):
        return self.redis.hexists(self.key(), 'archived')

    def convert(self, client, reward, dt=None, kpi=None):
        if self.is_client_excluded(client):
            raise ValueError('this client was not participating')

        alternative = self.existing_alternative(client)
        if not alternative:
            raise ValueError('this client was not participating')

        if kpi is not None:
            if not Experiment.validate_kpi(kpi):
                raise ValueError('invalid kpi name')
            self.add_kpi(kpi)

        if not self.existing_conversion(client):
            alternative.record_conversion(client, reward, dt=dt)

        return alternative

    @property
    def kpis(self):
        return self.redis.smembers("{0}:kpis".format(self.key(include_kpi=False)))

    def set_kpi(self, kpi):
        self.kpi = None

        key = "{0}:kpis".format(self.key())
        if kpi not in self.redis.smembers(key):
            raise ValueError('invalid kpi')

        self.kpi = kpi

    def add_kpi(self, kpi):
        self.redis.sadd("{0}:kpis".format(self.key(include_kpi=False)), kpi)
        self.kpi = kpi

    @property
    def winner(self):
        if self._winner is False:
            self._winner = self.redis.get(self._winner_key)
        if self._winner:
            return Alternative(self._winner, self, redis=self.redis)

    def set_winner(self, alternative_name):
        if alternative_name not in self.get_alternative_names():
            raise ValueError('this alternative is not in this experiment')
        self._winner = alternative_name
        self.redis.set(self._winner_key, alternative_name)

    def reset_winner(self):
        self._winner = None
        self.redis.delete(self._winner_key)

    @property
    def _winner_key(self):
        return "{0}:winner".format(self.key())

    @property
    def traffic_fraction(self):
        if self._traffic_fraction is False:
            try:
                self._traffic_fraction = float(self.redis.hget(self.key(), 'traffic_fraction'))
            except (TypeError, ValueError) as e:
                self._traffic_fraction = 1
        return self._traffic_fraction

    def set_traffic_fraction(self, fraction):
        fraction = float(fraction)
        if not 0 <= fraction <= 1:
            raise ValueError('invalid traffic fraction range')

        self._traffic_fraction = fraction

    @property
    def explore_fraction(self):
        if self._explore_fraction is False:
            try:
                self._explore_fraction = float(self.redis.hget(self.key(), 'explore_fraction'))
            except (TypeError, ValueError) as e:
                self._explore_fraction = 0.1
        return self._explore_fraction

    def set_explore_fraction(self, fraction):
        fraction = float(fraction)
        if not 0 <= fraction <= 1:
            raise ValueError('invalid explore fraction range')

        self._explore_fraction = fraction

    def sequential_id(self, client):
        """Return the sequential id for this test for the passed in client"""
        if client.client_id not in self._sequential_ids:
            id_ = sequential_id("e:{0}:users".format(self.name), client.client_id)
            self._sequential_ids[client.client_id] = id_
        return self._sequential_ids[client.client_id]

    def get_alternative(self, client, dt=None, prefetch=False):
        """Returns and records an alternative according to the following
        precedence:
          1. An existing alternative
          2. A server-chosen alternative
        """
        if self.is_archived():
            return self.control

        chosen_alternative = self.existing_alternative(client)
        if not chosen_alternative:
            chosen_alternative, participate, explore = self.choose_alternative(client)
            if participate and not prefetch:
                chosen_alternative.record_participation(client, explore, dt=dt)

        return chosen_alternative

    def exclude_client(self, client):
        key = _key("e:{0}:excluded".format(self.name))
        self.redis.setbit(key, self.sequential_id(client), 1)
        self._record_exclusion(client)

    def _record_exclusion(self, client, dt=None):
        """Record a user's exclusion in a test."""
        if dt is None:
            date = datetime.now()
        else:
            date = dt

        pipe = self.redis.pipeline()

        pipe.sadd(_key("x:{0}:years".format(self.name)), date.strftime('%Y'))
        pipe.sadd(_key("x:{0}:months".format(self.name)), date.strftime('%Y-%m'))
        pipe.sadd(_key("x:{0}:days".format(self.name)), date.strftime('%Y-%m-%d'))

        pipe.execute()

        keys = [
            _key("x:{0}:all".format(self.name)),
            _key("x:{0}:{1}".format(self.name, date.strftime('%Y'))),
            _key("x:{0}:{1}".format(self.name, date.strftime('%Y-%m'))),
            _key("x:{0}:{1}".format(self.name, date.strftime('%Y-%m-%d'))),
        ]
        msetbit(keys=keys, args=([self.sequential_id(client), 1] * len(keys)))

    def is_client_excluded(self, client):
        key = _key("e:{0}:excluded".format(self.name))
        return self.redis.getbit(key, self.sequential_id(client))

    def excluded_clients(self):
        key = _key("e:{0}:excluded".format(self.name))
        return self.redis.bitcount(key)

    def existing_alternative(self, client):
        if self.is_client_excluded(client):
            return self.control

        alts = self.get_alternative_names()
        keys = [_key("p:{0}:{1}:all".format(self.name, alt)) for alt in alts]
        altkey = first_key_with_bit_set(keys=keys, args=[self.sequential_id(client)])
        if altkey:
            idx = keys.index(altkey)
            return Alternative(alts[idx], self, redis=self.redis)

        return None

    def choose_alternative(self, client):
        raise NotImplementedError("Please Implement this method")

    def existing_conversion(self, client):
        alts = self.get_alternative_names()
        keys = [_key("c:{0}:{1}:users:all".format(self.kpi_key(), alt)) for alt in alts]
        altkey = first_key_with_bit_set(keys=keys, args=[self.sequential_id(client)])
        if altkey:
            idx = keys.index(altkey)
            return Alternative(alts[idx], self, redis=self.redis)

        return None

    def kpi_key(self):
        if self.kpi is not None:
            return "{0}/{1}".format(self.name, self.kpi)
        else:
            return self.name

    def key(self, include_kpi=True):
        if include_kpi:
            return _key("e:{0}".format(self.kpi_key()))
        else:
            return _key("e:{0}".format(self.name))

    @classmethod
    def find(cls, experiment_name, redis=None):

        if not redis.sismember(_key("e"), experiment_name):
            raise ValueError('experiment does not exist')

        algorithm_name = redis.hget(_key("e:{0}".format(experiment_name)), "algorithm")
        if algorithm_name is None:
            algorithm_name = "ab"

        algorithm = ALGORITHMS[algorithm_name]

        return algorithm(experiment_name,
                   Experiment.load_alternatives(experiment_name, redis),
                   redis=redis)

    @classmethod
    def find_or_create(cls, experiment_name, experiment_type, alternatives,
        traffic_fraction=None, explore_fraction=None,
        redis=None):

        if len(alternatives) < 2:
            raise ValueError('experiments require at least two alternatives')

        if traffic_fraction is None:
            traffic_fraction = 1

        if explore_fraction is None:
            explore_fraction = 0.1

        check_fraction = False
        try:
            experiment = Experiment.find(experiment_name, redis=redis)
            check_fraction = True
        except ValueError:
            algorithm = ALGORITHMS[experiment_type]
            experiment = algorithm(experiment_name, alternatives, redis=redis)
            # TODO: I want to revist this later
            experiment.set_traffic_fraction(traffic_fraction)
            experiment.set_explore_fraction(explore_fraction)
            experiment.save()

        if check_fraction and experiment.traffic_fraction != traffic_fraction:
            raise ValueError('do not change traffic fraction once a test has started. please delete in admin')

        # Make sure the alternative options are correct. If they are not,
        # raise an error.
        if sorted(experiment.get_alternative_names()) != sorted(alternatives):
            raise ValueError('experiment alternatives have changed. please delete in the admin')

        return experiment

    @staticmethod
    def all_names(redis=None):
        return redis.smembers(_key('e'))

    @staticmethod
    def all(exclude_archived=True, redis=None):
        experiments = []
        keys = redis.smembers(_key('e'))

        for key in keys:
            experiment = Experiment.find(key, redis=redis)
            if experiment.is_archived() and exclude_archived:
                continue
            experiments.append(experiment)
        return experiments

    @staticmethod
    def archived(redis=None):
        experiments = Experiment.all(exclude_archived=False, redis=redis)
        return [exp for exp in experiments if exp.is_archived()]

    @staticmethod
    def load_alternatives(experiment_name, redis=None):
        key = _key("e:{0}:alternatives".format(experiment_name))
        return redis.lrange(key, 0, -1)

    @staticmethod
    def is_valid(experiment_name):
        return (isinstance(experiment_name, basestring) and
                VALID_EXPERIMENT_ALTERNATIVE_RE.match(experiment_name) is not None)

    @staticmethod
    def validate_kpi(kpi):
        return (isinstance(kpi, basestring) and
                VALID_KPI_RE.match(kpi) is not None)

    @staticmethod
    def validate_algorithm(algorithm):
        return algorithm in ALGORITHMS.keys()


class ABExperiment(Experiment):

    def __init__(self, name, alternatives,
        winner=False,
        traffic_fraction=False,
        explore_fraction=False,
        redis=None):
        super(ABExperiment, self).__init__(name, alternatives, winner, traffic_fraction, explore_fraction, "ab", redis)

    def choose_alternative(self, client):
        rnd = round(random.uniform(1, 0.01), 2)
        if rnd >= self.traffic_fraction:
            self.exclude_client(client)
            return self.control, False, False

        return self._uniform_choice(client), True, True

    # Ported from https://github.com/facebook/planout/blob/master/python/planout/ops/random.py
    def _uniform_choice(self, client):
        idx = self._get_hash(client) % len(self.alternatives)
        return self.alternatives[idx]

    def _get_hash(self, client):
        salty = "{0}.{1}".format(self.name, client.client_id)

        # We're going to take the first 7 bytes of the client UUID
        # because of the largest integer values that can be represented safely
        # with Sixpack client libraries
        # More Info: https://github.com/seatgeek/sixpack/issues/132#issuecomment-54318218
        hashed = sha1(salty).hexdigest()[:7]
        return int(hashed, 16)


class MABEGreedyExperiment(Experiment):
    ''' Very simple implementation of the epsilon-greedy algorithm. Every
        conversion has a reward of 1, otherwise the reward is 0. This reduces
        the update equation to the conversion rate of the alternatives.

        To choose an alternative, we either pick a random alternative with
        chance ɛ and the best converting alternative with chance 1 - ɛ.

        The value for ɛ is currently hardcoded to 0.1
    '''
    def __init__(self, name, alternatives,
        winner=False,
        traffic_fraction=False,
        explore_fraction=False,
        redis=None):
        super(MABEGreedyExperiment, self).__init__(name, alternatives, winner, traffic_fraction, explore_fraction, "mab:egreedy", redis)

    def choose_alternative(self, client):
        rnd = round(random.uniform(1, 0.01), 2)
        if rnd > self.traffic_fraction:
            self.exclude_client(client)
            return self.control, False, False

        if random.random() < self.explore_fraction:
            # explore - pick a random alternative
            idx = random.randrange(len(self.alternatives))
            return self.alternatives[idx], True, True
        else:
            # exploit - pick the alternative with the highest conversion rate
            # pick random between multiple alternatives with max conversion rate
            # TODO: recomputing this every time might be too inefficient
            values = [a.average_reward() for a in self.alternatives]
            vmax = max(values)
            idxs = [i for i,v in enumerate(values) if v == vmax]
            if len(idxs) == 1:
                idx = idxs[0]
            else:
                idx = idxs[random.randrange(len(idxs))]
            return self.alternatives[idx], True, False


# Mapping algorithm names to implementing classes
ALGORITHMS = {
    "ab"          : ABExperiment,
    "mab:egreedy" : MABEGreedyExperiment
}

class Alternative(object):

    def __init__(self, name, experiment, redis=None):
        self.name = name
        self.experiment = experiment
        self.redis = redis

    def __repr__(self):
        return "<Alternative {0} (Experiment {1})>".format(repr(self.name), repr(self.experiment.name))

    def objectify_by_period(self, period, slim=False):

        if slim:
            return self.name

        PERIOD_TO_METHOD_MAP = {
            'day': {
                'participants': self.participants_by_day,
                'conversions': self.conversions_by_day,
                'participants_explore': self.participants_explore_by_day,
                'conversions_explore': self.conversions_explore_by_day,
                'reward': self.reward_by_day,
                'reward_explore': self.reward_explore_by_day,
            },
            'month': {
                'participants': self.participants_by_month,
                'conversions': self.conversions_by_month,
                'participants_explore': self.participants_explore_by_month,
                'conversions_explore': self.conversions_explore_by_month,
                'reward': self.reward_by_month,
                'reward_explore': self.reward_explore_by_month
            },
            'year': {
                'participants': self.participants_by_year,
                'conversions': self.conversions_by_year,
                'participants_explore': self.participants_explore_by_year,
                'conversions_explore': self.conversions_explore_by_year,
                'reward': self.reward_by_year,
                'reward_explore': self.reward_explore_by_year
            },
        }

        data = []
        conversion_fn = PERIOD_TO_METHOD_MAP[period]['conversions']
        participants_fn = PERIOD_TO_METHOD_MAP[period]['participants']
        conversion_explore_fn = PERIOD_TO_METHOD_MAP[period]['conversions_explore']
        participants_explore_fn = PERIOD_TO_METHOD_MAP[period]['participants_explore']
        reward_fn = PERIOD_TO_METHOD_MAP[period]['reward']
        reward_explore_fn = PERIOD_TO_METHOD_MAP[period]['reward_explore']

        conversions = conversion_fn()
        participants = participants_fn()
        conversions_explore = conversion_explore_fn()
        participants_explore = participants_explore_fn()
        reward = reward_fn()
        reward_explore = reward_explore_fn()

        dates = sorted(list(set(conversions.keys() + participants.keys() + conversions_explore.keys() + participants_explore.keys() + reward.keys() + reward_explore.keys())))
        for date in dates:
            _data = {
                'conversions': conversions.get(date, 0),
                'participants': participants.get(date, 0),
                'conversions_explore': conversions_explore.get(date, 0),
                'participants_explore': participants_explore.get(date, 0),
                'reward': reward.get(date, 0),
                'reward_explore': reward_explore.get(date, 0),
                'date': date
            }
            data.append(_data)

        objectified = {
            'name': self.name,
            'data': data,
            'conversion_rate': float('%.2f' % (self.conversion_rate() * 100)),
            'is_control': self.is_control(),
            'is_winner': self.is_winner(),
            'test_statistic': self.g_stat(),
            'participant_count': self.participant_count(),
            'completed_count': self.completed_count(),
            'confidence_level': self.confidence_level(),
            'confidence_interval': self.confidence_interval()
        }

        return objectified

    def is_control(self):
        return self.experiment.control.name == self.name

    def is_winner(self):
        winner = self.experiment.winner
        return winner and winner.name == self.name

    def participant_count(self):
        key = _key("p:{0}:{1}:all".format(self.experiment.name, self.name))
        return self.redis.bitcount(key)

    def participants_by_day(self):
        return self._get_stats('participations', 'days')

    def participants_by_month(self):
        return self._get_stats('participations', 'months')

    def participants_by_year(self):
        return self._get_stats('participations', 'years')

    def participants_explore_by_day(self):
        return self._get_stats('participations', 'days', explore=True)

    def participants_explore_by_month(self):
        return self._get_stats('participations', 'months', explore=True)

    def participants_explore_by_year(self):
        return self._get_stats('participations', 'years', explore=True)

    def completed_count(self):
        key = _key("c:{0}:{1}:users:all".format(self.experiment.kpi_key(), self.name))
        return self.redis.bitcount(key)

    def conversions_by_day(self):
        return self._get_stats('conversions', 'days')

    def conversions_by_month(self):
        return self._get_stats('conversions', 'months')

    def conversions_by_year(self):
        return self._get_stats('conversions', 'years')

    def conversions_explore_by_day(self):
        return self._get_stats('conversions', 'days', explore=True)

    def conversions_explore_by_month(self):
        return self._get_stats('conversions', 'months', explore=True)

    def conversions_explore_by_year(self):
        return self._get_stats('conversions', 'years', explore=True)

    def _get_stats(self, stat_type, stat_range, explore=False):
        if stat_type == 'participations':
            stat_type = 'p'
            exp_key = self.experiment.name
        elif stat_type == 'conversions':
            stat_type = 'c'
            exp_key = self.experiment.kpi_key()
        else:
            raise ValueError("Unrecognized stat type: {0}".format(stat_type))

        if stat_range not in ['days', 'months', 'years']:
            raise ValueError("Unrecognized stat range: {0}".format(stat_range))

        stats = {}

        pipe = self.redis.pipeline()

        search_key = _key("{0}:{1}:{2}".format(stat_type, exp_key, stat_range))
        keys = self.redis.smembers(search_key)

        for k in keys:
            name = self.name if stat_type == 'p' else "{0}:users".format(self.name)
            if not explore:
                range_key = _key("{0}:{1}:{2}:{3}".format(stat_type, exp_key, name, k))
            else:
                range_key = _key("{0}:{1}:{2}:explore:{3}".format(stat_type, exp_key, name, k))
            pipe.bitcount(range_key)

        redis_results = pipe.execute()
        for idx, k in enumerate(keys):
            stats[k] = float(redis_results[idx])

        return stats

    def reward_by_day(self):
        return self._get_reward_stats('total', 'days')

    def reward_by_month(self):
        return self._get_reward_stats('total', 'months')

    def reward_by_year(self):
        return self._get_reward_stats('total', 'years')

    def reward_explore_by_day(self):
        return self._get_reward_stats('explore', 'days')

    def reward_explore_by_month(self):
        return self._get_reward_stats('explore', 'months')

    def reward_explore_by_year(self):
        return self._get_reward_stats('explore', 'years')

    def _get_reward_stats(self, stat_type, stat_range):
        if stat_type not in ['total', 'explore', 'exploit']:
            raise ValueError("Unrecognized stat type: {0}".format(stat_type))

        if stat_range not in ['days', 'months', 'years']:
            raise ValueError("Unrecognized stat range: {0}".format(stat_range))

        exp_key = self.experiment.kpi_key()

        stats = {}

        pipe = self.redis.pipeline()

        search_key = _key("c:{0}:{1}".format(exp_key, stat_range))
        keys = self.redis.smembers(search_key)

        for k in keys:
            range_key = _key("c:{0}:{1}:{2}:rewards:{3}".format(exp_key, self.name, k, stat_type))
            pipe.get(range_key)

        redis_results = pipe.execute()
        for idx, k in enumerate(keys):
            if not redis_results[idx] is None:
                stats[k] = float(redis_results[idx])
            else:
                stats[k] = 0

        return stats

    def record_participation(self, client, exploration=True, dt=None):
        """Record a user's participation in a test along with a given variation"""
        if dt is None:
            date = datetime.now()
        else:
            date = dt

        experiment_key = self.experiment.name

        pipe = self.redis.pipeline()

        pipe.sadd(_key("p:{0}:years".format(experiment_key)), date.strftime('%Y'))
        pipe.sadd(_key("p:{0}:months".format(experiment_key)), date.strftime('%Y-%m'))
        pipe.sadd(_key("p:{0}:days".format(experiment_key)), date.strftime('%Y-%m-%d'))

        pipe.setbit(_key("p:{0}:explore".format(experiment_key)), self.experiment.sequential_id(client), exploration and 1 or 0)

        pipe.execute()

        keys = [
            _key("p:{0}:_all:all".format(experiment_key)),
            _key("p:{0}:_all:{1}".format(experiment_key, date.strftime('%Y'))),
            _key("p:{0}:_all:{1}".format(experiment_key, date.strftime('%Y-%m'))),
            _key("p:{0}:_all:{1}".format(experiment_key, date.strftime('%Y-%m-%d'))),
            _key("p:{0}:{1}:all".format(experiment_key, self.name)),
            _key("p:{0}:{1}:{2}".format(experiment_key, self.name, date.strftime('%Y'))),
            _key("p:{0}:{1}:{2}".format(experiment_key, self.name, date.strftime('%Y-%m'))),
            _key("p:{0}:{1}:{2}".format(experiment_key, self.name, date.strftime('%Y-%m-%d'))),
        ]
        if exploration:
            keys.extend([
                _key("p:{0}:{1}:explore:all".format(experiment_key, self.name)),
                _key("p:{0}:{1}:explore:{2}".format(experiment_key, self.name, date.strftime('%Y'))),
                _key("p:{0}:{1}:explore:{2}".format(experiment_key, self.name, date.strftime('%Y-%m'))),
                _key("p:{0}:{1}:explore:{2}".format(experiment_key, self.name, date.strftime('%Y-%m-%d'))),
            ])

        msetbit(keys=keys, args=([self.experiment.sequential_id(client), 1] * len(keys)))

    def record_conversion(self, client, reward, dt=None):
        """Record a user's conversion in a test along with a given variation"""
        if dt is None:
            date = datetime.now()
        else:
            date = dt

        experiment_key = self.experiment.kpi_key()

        exploration = self.redis.getbit(_key("p:{0}:explore".format(self.experiment.name)), self.experiment.sequential_id(client))

        pipe = self.redis.pipeline()

        pipe.sadd(_key("c:{0}:years".format(experiment_key)), date.strftime('%Y'))
        pipe.sadd(_key("c:{0}:months".format(experiment_key)), date.strftime('%Y-%m'))
        pipe.sadd(_key("c:{0}:days".format(experiment_key)), date.strftime('%Y-%m-%d'))

        pipe.incrbyfloat(_key("c:{0}:{1}:rewards:total".format(experiment_key, self.name)), reward)
        pipe.incrbyfloat(_key("c:{0}:{1}:{2}:rewards:total".format(experiment_key, self.name, date.strftime('%Y'))), reward)
        pipe.incrbyfloat(_key("c:{0}:{1}:{2}:rewards:total".format(experiment_key, self.name, date.strftime('%Y-%m'))), reward)
        pipe.incrbyfloat(_key("c:{0}:{1}:{2}:rewards:total".format(experiment_key, self.name, date.strftime('%Y-%m-%d'))), reward)
        if exploration:
            pipe.incrbyfloat(_key("c:{0}:{1}:rewards:explore".format(experiment_key, self.name)), reward)
            pipe.incrbyfloat(_key("c:{0}:{1}:{2}:rewards:explore".format(experiment_key, self.name, date.strftime('%Y'))), reward)
            pipe.incrbyfloat(_key("c:{0}:{1}:{2}:rewards:explore".format(experiment_key, self.name, date.strftime('%Y-%m'))), reward)
            pipe.incrbyfloat(_key("c:{0}:{1}:{2}:rewards:explore".format(experiment_key, self.name, date.strftime('%Y-%m-%d'))), reward)
        else:
            pipe.incrbyfloat(_key("c:{0}:{1}:rewards:exploit".format(experiment_key, self.name)), reward)
            pipe.incrbyfloat(_key("c:{0}:{1}:{2}:rewards:exploit".format(experiment_key, self.name, date.strftime('%Y'))), reward)
            pipe.incrbyfloat(_key("c:{0}:{1}:{2}:rewards:exploit".format(experiment_key, self.name, date.strftime('%Y-%m'))), reward)
            pipe.incrbyfloat(_key("c:{0}:{1}:{2}:rewards:exploit".format(experiment_key, self.name, date.strftime('%Y-%m-%d'))), reward)

        pipe.execute()

        keys = [
            _key("c:{0}:_all:users:all".format(experiment_key)),
            _key("c:{0}:_all:users:{1}".format(experiment_key, date.strftime('%Y'))),
            _key("c:{0}:_all:users:{1}".format(experiment_key, date.strftime('%Y-%m'))),
            _key("c:{0}:_all:users:{1}".format(experiment_key, date.strftime('%Y-%m-%d'))),
            _key("c:{0}:{1}:users:all".format(experiment_key, self.name)),
            _key("c:{0}:{1}:users:{2}".format(experiment_key, self.name, date.strftime('%Y'))),
            _key("c:{0}:{1}:users:{2}".format(experiment_key, self.name, date.strftime('%Y-%m'))),
            _key("c:{0}:{1}:users:{2}".format(experiment_key, self.name, date.strftime('%Y-%m-%d'))),
        ]
        if exploration:
            keys.extend([
                _key("c:{0}:{1}:users:explore:{2}".format(experiment_key, self.name, date.strftime('%Y'))),
                _key("c:{0}:{1}:users:explore:{2}".format(experiment_key, self.name, date.strftime('%Y-%m'))),
                _key("c:{0}:{1}:users:explore:{2}".format(experiment_key, self.name, date.strftime('%Y-%m-%d'))),
            ])

        msetbit(keys=keys, args=([self.experiment.sequential_id(client), 1] * len(keys)))

    def conversion_rate(self):
        try:
            return self.completed_count() / float(self.participant_count())
        except ZeroDivisionError:
            return 0

    def total_reward(self):
        experiment_key = self.experiment.kpi_key()

        result = self.redis.get(_key("c:{0}:{1}:rewards:total".format(experiment_key, self.name)))
        if result is None:
            return 0
        return float(result)

    def average_reward(self):
        try:
            return self.total_reward() / float(self.participant_count())
        except ZeroDivisionError:
            return 0

    def g_stat(self):
        # http://en.wikipedia.org/wiki/G-test

        if self.is_control():
            return 'N/A'

        control = self.experiment.control

        alt_conversions = self.completed_count()
        control_conversions = control.completed_count()
        alt_failures = self.participant_count() - alt_conversions
        control_failures = control.participant_count() - control_conversions

        total_conversions = alt_conversions + control_conversions

        if total_conversions < 20:
            # small sample size of conversions, see where it goes for a bit
            return 'N/A'

        total_participants = self.participant_count() + control.participant_count()

        expected_control_conversions = control.participant_count() * total_conversions / float(total_participants)
        expected_alt_conversions = self.participant_count() * total_conversions / float(total_participants)
        expected_control_failures = control.participant_count() - expected_control_conversions
        expected_alt_failures = self.participant_count() - expected_alt_conversions

        try:
            g_stat = 2 * (      alt_conversions * log(alt_conversions / expected_alt_conversions) \
                        +   alt_failures * log(alt_failures / expected_alt_failures) \
                        +   control_conversions * log(control_conversions / expected_control_conversions) \
                        +   control_failures * log(control_failures / expected_control_failures))

        except ZeroDivisionError:
            return 0

        return round(g_stat, 2)

    def z_score(self):
        if self.is_control():
            return 'N/A'

        control = self.experiment.control
        ctr_e = self.conversion_rate()
        ctr_c = control.conversion_rate()

        e = self.participant_count()
        c = control.participant_count()

        try:
            std_dev = pow(((ctr_e / pow(ctr_c, 3)) * ((e * ctr_e) + (c * ctr_c) - (ctr_c * ctr_e) * (c + e)) / (c * e)), 0.5)
            z_score = ((ctr_e / ctr_c) - 1) / std_dev
            return z_score
        except ZeroDivisionError:
            return 0

    def g_confidence_level(self):
        # g stat is approximated by chi-square, we will use
        # critical values from chi-square distribution with one degree of freedom

        g_stat = self.g_stat()
        if g_stat == 'N/A':
            return g_stat

        ret = ''
        if g_stat == 0.0:
            ret = 'No Change'
        elif g_stat < 3.841:
            ret = 'No Confidence'
        elif g_stat < 6.635:
            ret = '95% Confidence'
        elif g_stat < 10.83:
            ret = '99% Confidence'
        else:
            ret = '99.9% Confidence'

        return ret

    def z_confidence_level(self):
        z_score = self.z_score()
        if z_score == 'N/A':
            return z_score

        z_score = abs(round(z_score, 3))

        ret = ''
        if z_score == 0.0:
            ret = 'No Change'
        elif z_score < 1.96:
            ret = 'No Confidence'
        elif z_score < 2.57:
            ret = '95% Confidence'
        elif z_score < 3.27:
            ret = '99% Confidence'
        else:
            ret = '99.9% Confidence'

        return ret

    def confidence_level(self, conf_type="g"):
        if conf_type == "z":
            return self.z_confidence_level()
        else:
            return self.g_confidence_level()

    def confidence_interval(self):
        try:
            # 80% confidence
            p = self.conversion_rate()
            return pow(p * (1 - p) / self.participant_count(), 0.5) * 1.28 * 100
        except ZeroDivisionError:
            return 0

    def key(self):
        return _key("{0}:{1}".format(self.experiment.name, self.name))

    @staticmethod
    def is_valid(alternative_name):
        return (isinstance(alternative_name, basestring) and
                VALID_EXPERIMENT_ALTERNATIVE_RE.match(alternative_name) is not None)
