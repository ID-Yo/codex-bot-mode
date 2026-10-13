import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import launch, project, requests as rq, roster, store, teams  # noqa: E402

M = '0aa00000-0000-7000-8000-000000000021'


class Bridge:
    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append(arguments)
        return {}


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'p'
        self.root.mkdir()
        teams.install(teams.plan_team(self.root, 'development', team_answers={'tracking_mode': 'local'}))
        data = roster.load(self.root)
        roster.set_thread(data, 'manager', M, 'appointed')
        roster.save(self.root, data, roster.version(roster.load(self.root)))
        self.saved = list(store.BOTS)
        project.configure(self.root)

    def tearDown(self):
        project.configure(ROOT)
        store.BOTS[:] = self.saved
        self.tmp.cleanup()

    def test_catalog_plan_and_confirmed_launch_goes_to_the_lead_manager(self):
        names = [t['name'] for t in launch.catalog()]
        self.assertEqual(sorted(names), ['development', 'marketing', 'sales'])
        plan = launch.plan('marketing', 'Growth', {'strategist': {'display_name': 'Mila'}})
        self.assertEqual(plan['team'], 'Growth')
        self.assertIsNotNone(plan['director_proposal'])
        self.assertEqual(plan['agents'][1]['values']['display_name'], {'value': 'Mila', 'source': 'owner'})
        self.assertFalse((self.root / 'team/Growth').exists())
        bridge = Bridge()
        result = launch.launch(bridge, 'marketing', 'Growth', {'strategist': {'display_name': 'Mila'}}, 'abc12345def', {'tracking_mode': 'local'})
        self.assertEqual((result['state'], result['manager']), ('sent', 'manager'))
        self.assertEqual(bridge.calls[0]['threadId'], M)
        self.assertIn('install-team marketing', bridge.calls[0]['prompt'])
        self.assertIn('"display_name": "Mila"', bridge.calls[0]['prompt'])
        self.assertTrue(launch.launch(bridge, 'marketing', 'Growth', {}, 'abc12345def', {'tracking_mode': 'local'})['repeated'])
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(rq.load(self.root)['requests'][0]['to'], 'manager')
        with self.assertRaises(ValueError):
            launch.launch(None, 'marketing', 'Other', {}, 'zzz12345zzz', {'tracking_mode': 'local'})

    def test_launch_without_tracking_does_not_send_or_record_request(self):
        bridge = Bridge()
        with self.assertRaises(teams.TeamError):
            launch.launch(bridge, 'marketing', 'Other', {}, 'missing12345')
        self.assertEqual(bridge.calls, [])
        self.assertEqual(rq.load(self.root)['requests'], [])
        with self.assertRaises(ValueError):
            launch.plan('marketing', team_answers=['local'])


if __name__ == '__main__':
    unittest.main()
