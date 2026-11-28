"""Read-only relay access to every chat bound in a registry project's roster (#298, cbm298-relay-read).

read_thread may reach any registry-bound chat, but only as metadata (includeOutputs forced to false on the server).
Sending, waiting and every other tool keep the old target list; unknown chats stay refused; the allow list follows
the registry and the rosters."""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from team_room import desktop_relay, registry, roster  # noqa: E402
from scripts.team_room.work import MANAGER  # noqa: E402

MANAGER_THREAD = '11111111-2222-3333-4444-{:012d}'
WORKER_THREAD = '22222222-2222-3333-4444-{:012d}'


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


class FakeBridge:
    origin = MANAGER

    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return {'thread': {'id': arguments.get('threadId'), 'status': {'type': 'idle'}}, 'turns': []}


class ReadOnlyTargetsTests(unittest.TestCase):
    def setUp(self):
        desktop_relay.invalidate_targets()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.host = base / 'host'
        self.host.mkdir()
        self.roots = []
        projects = []
        for number in range(2):
            root = base / f'p{number}'
            self.write_roster(root, number)
            self.roots.append(root)
            projects.append({'id': f'p{number}', 'root': str(root)})
        self.registry = self.host / '.bot-mode' / 'local' / 'office-project-roots.json'
        write(self.registry, {'schema': 1, 'projects': projects, 'standalone': []})
        for patch in (mock.patch.object(desktop_relay.project, 'PROJECT', self.host),
                      mock.patch.dict(os.environ, {'CODEX_OFFICE_REGISTRY': ''})):
            patch.start()
            self.addCleanup(patch.stop)
        self.bridge = FakeBridge()
        self.relay_dir = base / 'relay'
        self.server = desktop_relay.create_relay(self.bridge, self.relay_dir, port=0)
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def write_roster(self, root, number, worker=None):
        root.mkdir(parents=True, exist_ok=True)
        data = roster.empty(str(root), f'P{number}')
        roster.add_team(data, 'Development', f'p{number} Development', 'development')
        roster.add_participant(data, 'manager', 'Manager', 'Development', 'team/Development/manager', MANAGER_THREAD.format(number))
        roster.add_participant(data, 'developer', 'Developer', 'Development', 'team/Development/developer',
                               worker or WORKER_THREAD.format(number))
        roster.save(str(root), data, roster.version(roster.empty(str(root))))

    def post(self, name, arguments, token=True):
        config = json.loads((self.relay_dir / 'relay.json').read_text(encoding='utf-8'))
        request = Request(config['url'] + '/call', data=json.dumps({'name': name, 'arguments': arguments}).encode('utf-8'),
                          headers={'Content-Type': 'application/json', 'X-Office-Relay': config['token'] if token else 'x'})
        with urlopen(request) as response:
            return json.load(response)

    def refused(self, name, arguments):
        with self.assertRaises(HTTPError) as error:
            self.post(name, arguments)
        self.assertEqual(error.exception.code, 400)

    def test_every_roster_bound_chat_is_readable_and_sendable_but_not_an_appointed_target(self):
        for number in range(2):
            worker, manager = WORKER_THREAD.format(number), MANAGER_THREAD.format(number)
            self.assertIn(worker, desktop_relay.read_only_targets())
            self.assertIn(manager, desktop_relay.read_only_targets())
            self.assertIn(worker, desktop_relay.sendable_targets())
            self.assertIn(manager, desktop_relay.sendable_targets())
            self.assertNotIn(worker, desktop_relay.targets(), 'waiting stays on the appointed list')
            self.assertIn(manager, desktop_relay.targets())

    def test_allowed_read_reaches_desktop_with_metadata_only_arguments(self):
        worker = WORKER_THREAD.format(0)
        result = self.post('read_thread', {'threadId': worker, 'hostId': 'local', 'turnLimit': 50, 'includeOutputs': True,
                                           'maxOutputCharsPerItem': 20000, 'cursor': 'older', 'extra': 'x'})
        self.assertEqual(result['result']['thread']['id'], worker)
        self.assertEqual(self.bridge.calls, [('read_thread', {'threadId': worker, 'hostId': 'local', 'turnLimit': 3,
                                                              'includeOutputs': False, 'maxOutputCharsPerItem': 1})])

    def test_turn_limit_within_bounds_is_kept_and_missing_arguments_get_the_forced_values(self):
        worker = WORKER_THREAD.format(1)
        self.post('read_thread', {'threadId': worker, 'turnLimit': 7})
        self.post('read_thread', {'threadId': worker})
        self.assertEqual([c[1]['turnLimit'] for c in self.bridge.calls], [7, 3])
        self.assertTrue(all(c[1]['includeOutputs'] is False and c[1]['maxOutputCharsPerItem'] == 1 for c in self.bridge.calls))

    def test_read_of_an_existing_target_keeps_its_arguments(self):
        manager = MANAGER_THREAD.format(0)  # a registry Manager was already a full target before this change
        self.post('read_thread', {'threadId': manager, 'turnLimit': 10, 'includeOutputs': True, 'maxOutputCharsPerItem': 20000})
        self.assertEqual(self.bridge.calls[0][1], {'threadId': manager, 'turnLimit': 10, 'includeOutputs': True,
                                                   'maxOutputCharsPerItem': 20000})

    def test_send_reaches_a_bound_chat_but_waiting_on_it_is_refused(self):
        worker = WORKER_THREAD.format(0)
        self.post('send_message_to_thread', {'threadId': worker, 'prompt': 'hello'})
        self.assertEqual(self.bridge.calls, [('send_message_to_thread', {'threadId': worker, 'prompt': 'hello'})])
        self.refused('wait_threads', {'targets': [{'threadId': worker, 'hostId': 'local'}], 'timeoutMs': 0})
        self.assertEqual(len(self.bridge.calls), 1)
        desktop_relay.validate('send_message_to_thread', {'threadId': worker, 'prompt': 'hello'})
        with self.assertRaises(ValueError):
            desktop_relay.validate('wait_threads', {'targets': [{'threadId': worker, 'hostId': 'local'}]})

    def test_a_standalone_registry_entry_and_an_unbound_chat_cannot_be_sent_to(self):
        standalone = '33333333-2222-3333-4444-000000000001'
        listed = json.loads(self.registry.read_text(encoding='utf-8'))
        listed['standalone'] = [{'id': 'cos', 'name': 'COS', 'thread_id': standalone}]
        write(self.registry, listed)
        os.utime(self.registry, ns=(2, 2))
        self.assertIn(standalone, desktop_relay.read_only_targets())  # readable as metadata, like before
        self.assertNotIn(standalone, desktop_relay.sendable_targets())
        self.refused('send_message_to_thread', {'threadId': standalone, 'prompt': 'hello'})
        self.refused('send_message_to_thread', {'threadId': 'aaaaaaaa-0000-0000-0000-000000000000', 'prompt': 'x'})
        self.assertEqual(self.bridge.calls, [])

    def test_unknown_chat_and_unsupported_tools_stay_refused(self):
        self.refused('read_thread', {'threadId': 'aaaaaaaa-0000-0000-0000-000000000000'})
        self.refused('read_thread', {'threadId': WORKER_THREAD.format(0), 'hostId': 'remote'})
        self.refused('read_thread', {})
        self.refused('create_thread', {})
        self.assertEqual(self.bridge.calls, [])
        with self.assertRaises(HTTPError) as error:
            self.post('read_thread', {'threadId': WORKER_THREAD.format(0)}, token=False)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.bridge.calls, [])

    def test_allow_list_follows_roster_and_registry_changes(self):
        old, new = WORKER_THREAD.format(0), WORKER_THREAD.format(77)
        self.post('read_thread', {'threadId': old})
        bindings = roster.local_path(self.roots[0])
        data = json.loads(bindings.read_text(encoding='utf-8'))
        data['developer']['thread_id'] = new
        bindings.write_text(json.dumps(data), encoding='utf-8')
        os.utime(bindings, ns=(1, 1))
        self.post('read_thread', {'threadId': new})
        self.refused('read_thread', {'threadId': old})
        listed = json.loads(self.registry.read_text(encoding='utf-8'))
        listed['projects'].pop(0)
        write(self.registry, listed)
        self.refused('read_thread', {'threadId': new})
        self.post('read_thread', {'threadId': WORKER_THREAD.format(1)})

    def test_one_registry_scan_serves_targets_and_read_only_targets(self):
        desktop_relay.invalidate_targets()
        real = registry.build_overview
        with mock.patch.object(registry, 'build_overview', side_effect=real) as scan:
            desktop_relay.targets()
            desktop_relay.read_only_targets()
            desktop_relay.validate('read_thread', {'threadId': WORKER_THREAD.format(0)})
        self.assertEqual(scan.call_count, 1)

    def test_scan_failure_leaves_only_the_old_targets(self):
        with mock.patch.object(desktop_relay, '_registry_scan', side_effect=RuntimeError('boom')):
            self.assertEqual(desktop_relay.read_only_targets(), frozenset())
            self.assertEqual(desktop_relay.sendable_targets(), frozenset())
            with self.assertRaises(ValueError):
                desktop_relay.validate('read_thread', {'threadId': WORKER_THREAD.format(0)})


if __name__ == '__main__':
    unittest.main()
