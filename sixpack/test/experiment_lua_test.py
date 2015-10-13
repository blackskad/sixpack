import unittest
import json

import dateutil.parser

from sixpack.server import create_app
from sixpack.models import Client, ABExperiment, Experiment


class TestExperimentLua(unittest.TestCase):

    def setUp(self):
        self.app = create_app()

    def test_convert(self):
        exp = ABExperiment('test-convert', ['1', '2'], redis=self.app.redis)
        client = Client("eric", redis=self.app.redis)
        exp.get_alternative(client)
        exp.convert(client, 1)
        self.assertEqual(exp.total_conversions(), 1)

    def test_cant_convert_twice(self):
        exp = ABExperiment('test-cant-convert-twice', ['1', '2'], redis=self.app.redis)
        client = Client("eric", redis=self.app.redis)
        alt,policy = exp.get_alternative(client)
        exp.convert(client, 1)
        self.assertEqual(exp.total_conversions(), 1)

        exp.convert(client, 1, dt=dateutil.parser.parse("2012-01-01"))
        self.assertEqual(exp.total_conversions(), 1)

        data = exp.objectify_by_period("day")
        altdata = [a for a in data["alternatives"] if a["name"] == alt.name][0]["data"]
        total_participants = sum([d["participants"] for d in altdata])
        self.assertEqual(total_participants, 1)
        total_conversions = sum([d["conversions"] for d in altdata])
        self.assertEqual(data["has_winner"], False)
        self.assertEqual(total_conversions, 1)

        # Only retrieve the slim set.
        data = exp.objectify_by_period("day", slim=True)
        self.assertFalse(data.has_key("has_winner"))
        self.assertFalse(data.has_key("kpi"))
        self.assertFalse(data.has_key("kpis"))
        self.assertFalse(data.has_key("period"))

    def test_find_existing_conversion(self):
        exp = ABExperiment('test-find-existing-conversion', ['1', '2'], redis=self.app.redis)
        client = Client("eric", redis=self.app.redis)
        alt,policy = exp.get_alternative(client)
        exp.convert(client, 1)
        alt2 = exp.existing_conversion(client)
        self.assertIsNotNone(alt2)
        self.assertTrue(alt.name == alt2.name)
        client2 = Client("zack", redis=self.app.redis)
        alt3 = exp.existing_conversion(client2)
        self.assertIsNone(alt3)
