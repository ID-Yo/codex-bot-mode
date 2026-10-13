import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import desktop_relay, profiles, project, roster, store, teams  # noqa: E402

T = ['0aa00000-0000-7000-8000-00000000000' + str(i) for i in range(6)]


class OfficeProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'shop'
        self.root.mkdir()
        teams.install(teams.plan_team(self.root, 'marketing', team_answers={'tracking_mode': 'local'}))
        teams.install(teams.plan_team(self.root, 'sales', team_answers={'tracking_mode': 'local'}))
        teams.install(teams.plan_agent(self.root, None, 'director'))
        data = roster.load(self.root)
        for bot, tid in zip(('manager', 'strategist', 'sales-manager', 'project-director'), T):
            roster.set_thread(data, bot, tid, 'appointed')
        roster.save(self.root, data, roster.version(roster.load(self.root)))
        self.saved = list(store.BOTS)
        project.configure(self.root)

    def tearDown(self):
        project.configure(ROOT)
        store.BOTS[:] = self.saved
        self.tmp.cleanup()

    def test_participants_rooms_and_profiles_come_from_the_roster(self):
        self.assertEqual(store.BOTS[0], 'manager')
        self.assertIn('pipeline-analyst', store.BOTS)
        rooms = {r['name']: r['bots'] for r in project.teams()}
        self.assertEqual(sorted(rooms), ['Leadership', 'Marketing', 'Sales'])
        self.assertEqual(rooms['Leadership'], ['project-director', 'manager', 'sales-manager'])
        self.assertEqual(rooms['Sales'][0], 'sales-manager')
        text = profiles.instructions('outreach-writer', 'room', self.root)
        self.assertIn('Base agent instructions', text)
        self.assertIn('- Bot ID: \x60outreach-writer\x60', text)
        self.assertEqual(project.label('strategist'), 'Strategist')
        self.assertIsNone(project.repository())

    def test_relay_follows_replaced_chats(self):
        desktop_relay.validate('read_thread', {'threadId': T[1]})
        data = roster.load(self.root)
        roster.set_thread(data, 'strategist', T[5], 'chat too long')
        roster.save(self.root, data, roster.version(roster.load(self.root)))
        desktop_relay.validate('read_thread', {'threadId': T[5]})
        with self.assertRaises(ValueError):
            desktop_relay.validate('read_thread', {'threadId': T[1]})


if __name__ == '__main__':
    unittest.main()
