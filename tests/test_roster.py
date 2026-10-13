import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from team_room import roster  # noqa: E402
import roster as cli  # noqa: E402

A = '0aa10bcb-47a0-7d62-9caa-6f996b2b966c'
B = '0aa10c7c-f0a8-7da3-a35e-99e9e27a00c6'


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def seeded(self):
        data = roster.load(self.project)
        roster.add_team(data, 'Development', 'demo · Development', 'development')
        roster.add_participant(data, 'developer', 'Developer', 'Development', 'team/Development/developer', A)
        return roster.save(self.project, data, roster.version(roster.load(self.project)))

    def test_links_and_replacement_history(self):
        self.seeded()
        data = roster.load(self.project)
        roster.set_thread(data, 'developer', B, 'chat too long')
        roster.save(self.project, data, roster.version(roster.load(self.project)))
        data = roster.load(self.project)
        self.assertEqual(roster.links(data)[0]['link'], f'codex://threads/{B}')
        self.assertEqual(data['participants'][0]['history'][0]['thread_id'], A)
        self.assertEqual(roster.thread_of(self.project, 'developer'), B)

    def test_stale_write_is_refused(self):
        self.seeded()
        first = roster.load(self.project)
        old = roster.version(first)
        second = roster.load(self.project)
        roster.set_thread(second, 'developer', B, 'replaced')
        roster.save(self.project, second, old)
        roster.retire(first, 'developer', 'gone')
        with self.assertRaises(roster.RosterError):
            roster.save(self.project, first, old)

    def test_validation(self):
        data = roster.load(self.project)
        roster.add_team(data, 'Development')
        for bad in (dict(bot='Bad Bot', role='X'), dict(bot='x', role='X', team='Sales'),
                    dict(bot='x', role='X', workspace='../outside'), dict(bot='x', role='X', thread_id='nope')):
            trial = json.loads(json.dumps(data))
            roster.add_participant(trial, **{'team': None, **bad})
            with self.assertRaises(roster.RosterError):
                roster.validate(trial)
        trial = json.loads(json.dumps(data))
        roster.add_participant(trial, 'a', 'A', thread_id=A)
        roster.add_participant(trial, 'b', 'B', thread_id=A)
        with self.assertRaises(roster.RosterError):
            roster.validate(trial)
        with self.assertRaises(roster.RosterError):
            roster.add_team(trial, 'x' * 10 + 'sk-' + 'a' * 20)
            roster.validate(trial)

    def test_retire_keeps_history_and_hides_link(self):
        self.seeded()
        data = roster.load(self.project)
        roster.retire(data, 'developer', 'role removed')
        roster.save(self.project, data, roster.version(roster.load(self.project)))
        data = roster.load(self.project)
        self.assertEqual(roster.links(data), [])
        self.assertEqual(data['participants'][0]['history'][0]['status'], 'retired')

    def test_shared_roster_has_no_chats_and_bindings_stay_local(self):
        self.seeded()
        shared = json.loads(roster.path(self.project).read_text(encoding='utf-8'))
        self.assertNotIn('thread_id', shared['participants'][0])
        local = json.loads(roster.local_path(self.project).read_text(encoding='utf-8'))
        self.assertEqual(local['developer']['thread_id'], A)
        ignore = (roster.path(self.project).parent / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('/local/', ignore)
        # A second computer has the shared roster but no chats yet.
        roster.local_path(self.project).unlink()
        data = roster.load(self.project)
        self.assertIsNone(data['participants'][0]['thread_id'])
        roster.set_thread(data, 'developer', B, 'second computer')
        roster.save(self.project, data, roster.version(roster.load(self.project)))
        self.assertEqual(json.loads(roster.path(self.project).read_text(encoding='utf-8')), {**shared, 'updated_at': json.loads(roster.path(self.project).read_text(encoding='utf-8'))['updated_at']})
        self.assertEqual(roster.thread_of(self.project, 'developer'), B)

    def test_legacy_roster_with_thread_ids_still_loads(self):
        file = roster.path(self.project)
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps({'schema': 1, 'project': {'name': 'demo'}, 'teams': [], 'participants': [
            {'bot': 'manager', 'role': 'Manager', 'team': None, 'workspace': None, 'thread_id': A,
             'host_id': 'local', 'status': 'active', 'history': []}]}), encoding='utf-8')
        self.assertEqual(roster.thread_of(self.project, 'manager'), A)

    def test_cli(self):
        self.assertEqual(cli.main(['--project', self.project, 'add-team', 'Development']), 0)
        self.assertEqual(cli.main(['--project', self.project, 'add', 'manager', '--role', 'Manager',
                                   '--team', 'Development', '--thread', A]), 0)
        self.assertEqual(cli.main(['--project', self.project, 'add', 'manager', '--role', 'Manager']), 2)
        self.assertEqual(roster.thread_of(self.project, 'manager'), A)


if __name__ == '__main__':
    unittest.main()
