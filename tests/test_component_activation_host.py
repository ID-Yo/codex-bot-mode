import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import component_activation, profile_update
from scripts.component_activation_host import ActivationHost
from scripts.team_room import profiles
from scripts.team_room.controller import Controller


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write_office_roster(root, workspace):
    path = root / '.bot-mode' / 'roster.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'schema': 1,
        'project': {'name': 'activation-fixture'},
        'teams': [{'name': 'Development'}],
        'participants': [{
            'bot': 'developer', 'role': 'Developer', 'team': 'Development',
            'workspace': workspace, 'status': 'active',
        }],
        'updated_at': None,
    }), encoding='utf-8')


class ActivationHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cbm-158-', dir=Path(tempfile.gettempdir()))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.workspace = 'team/Development/developer'
        _write_office_roster(self.root, self.workspace)
        self.profile = self.root / self.workspace / 'agent'
        self.profile.mkdir(parents=True)
        self.contents = {
            'SOUL.md': b'Office producer fixture soul\n',
            'PROFILE.md': b'Office producer fixture profile\n',
        }
        self.profile_files = {}
        for name, data in self.contents.items():
            path = self.profile / name
            path.write_bytes(data)
            self.profile_files[f'{self.workspace}/agent/{name}'] = _sha(data)
        self.component = 'profile:' + self.workspace
        self.transaction = str(uuid.uuid4())
        self.txroot = self.root / '.bot-mode' / 'local' / 'profile-updates' / self.transaction
        self.txroot.mkdir(parents=True)
        self.target = {
            'component': self.component,
            'release_component': 'developer-profile',
            'path': self.workspace,
            'version': '2.0.0',
            'files': dict(self.profile_files),
            'installed_sha256': _sha(_canonical(self.profile_files)),
        }
        inventory = self.root / '.bot-mode' / 'local' / 'installations.json'
        inventory.parent.mkdir(parents=True, exist_ok=True)
        inventory.write_text(json.dumps({'schema': 1, 'components': [{
            'id': self.component, 'kind': 'profile', 'path': self.workspace,
            'version': '2.0.0',
        }]}), encoding='utf-8')
        self.journal_path = self.txroot / 'journal.json'
        self.write_journal()
        self.bindings = {'developer': 'office-thread-current'}

    def write_journal(self, **changes):
        journal = {
            'schema': 1,
            'transaction_id': self.transaction,
            'phase': 'applied',
            'plan_sha256': 'd' * 64,
            'selected_targets': [{
                'inventory_component': self.component,
                'release_component': 'developer-profile',
                'kind': 'profile',
                'root': self.workspace,
            }],
            'pending_activation': {
                'state': 'pending_activation',
                'activated': False,
                'release_id': 'fixture-release',
                'source_revision': 'a' * 40,
                'targets': [self.target],
            },
        }
        journal.update(changes)
        self.journal_path.write_text(json.dumps(journal), encoding='utf-8')

    def complete(self, **changes):
        args = {
            'bot': 'developer',
            'thread_id': self.bindings['developer'],
            'turn_id': 'office-turn-1',
            'terminal_status': 'completed',
            'profile_files': self.profile_files,
            'current_bindings': self.bindings,
            'idle': True,
            'paused': False,
            'ownership': 'office',
        }
        args.update(changes)
        return ActivationHost(self.root).complete_office_turn(**args)

    def activation_state(self):
        data = json.loads((self.txroot / 'activation.json').read_text(encoding='utf-8'))
        return data['records'][self.component]

    def test_completed_office_turn_persists_hash_only_activation_receipt(self):
        journal_before = self.journal_path.read_bytes()
        profile_before = {name: (self.profile / name).read_bytes() for name in self.contents}

        self.assertEqual(self.complete(), 1)

        state = self.activation_state()
        self.assertEqual(state['state'], 'verified_active')
        self.assertEqual(state['consumers'][0]['thread_id'], self.bindings['developer'])
        self.assertEqual(state['consumers'][0]['revision'], self.target['installed_sha256'])
        self.assertRegex(state['consumers'][0]['receipt'], r'^[a-f0-9]{64}$')
        stored = (self.txroot / 'activation.json').read_text(encoding='utf-8')
        self.assertNotIn('Office producer fixture', stored)
        self.assertNotIn('SOUL.md', stored)
        self.assertEqual(self.journal_path.read_bytes(), journal_before)
        self.assertEqual({name: (self.profile / name).read_bytes() for name in self.contents}, profile_before)

    def test_interrupted_stale_paused_native_and_rebound_turns_stay_pending(self):
        for change in (
            {'terminal_status': 'interrupted'},
            {'terminal_status': 'failed'},
            {'idle': False},
            {'paused': True},
            {'ownership': 'native_desktop'},
            {'thread_id': 'old-thread'},
            {'bot': 'reviewer', 'current_bindings': {'reviewer': 'office-thread-current'}},
        ):
            with self.subTest(change=change):
                self.assertEqual(self.complete(**change), 0)
                self.assertFalse((self.txroot / 'activation.json').exists())

    def test_no_pending_directory_creates_no_local_state(self):
        root = self.root / 'empty-project'
        root.mkdir()
        self.assertEqual(ActivationHost(root).complete_office_turn(
            bot='developer', thread_id='office-thread-current', turn_id='turn-1',
            terminal_status='completed', profile_files=self.profile_files,
            current_bindings=self.bindings, idle=True, paused=False), 0)
        self.assertFalse((root / '.bot-mode').exists())

    def test_transaction_discovery_defers_when_count_exceeds_bound(self):
        folder = self.root / '.bot-mode/local/profile-updates'
        for _ in range(257):
            (folder / str(uuid.uuid4())).mkdir(parents=True)
        self.assertEqual(self.complete(), 0)
        self.assertFalse((self.txroot / 'activation.json').exists())

    def test_matching_receipt_is_written_only_to_its_transaction(self):
        other_id = str(uuid.uuid4())
        other = self.root / '.bot-mode/local/profile-updates' / other_id
        other.mkdir()
        other_target = {**self.target, 'component': 'profile:team/Development/reviewer',
                        'path': 'team/Development/reviewer'}
        (other / 'journal.json').write_text(json.dumps({
            'schema': 1, 'transaction_id': other_id, 'phase': 'applied',
            'plan_sha256': 'f' * 64,
            'selected_targets': [{'inventory_component': other_target['component'],
                                  'kind': 'profile', 'root': other_target['path']}],
            'pending_activation': {'state': 'pending_activation', 'activated': False,
                                   'targets': [other_target]},
        }), encoding='utf-8')
        self.assertEqual(self.complete(), 1)
        self.assertTrue((self.txroot / 'activation.json').is_file())
        self.assertFalse((other / 'activation.json').exists())

    def test_update_lock_defers_receipt_without_mutating_transaction(self):
        for name in ('profile-updates.lock', 'installations.lock'):
            with self.subTest(name=name):
                lock = self.root / '.bot-mode/local' / name
                lock.write_text('owner lock', encoding='utf-8')
                try:
                    before = self.journal_path.read_bytes()
                    self.assertEqual(self.complete(), 0)
                    self.assertEqual(lock.read_text(encoding='utf-8'), 'owner lock')
                    self.assertEqual(self.journal_path.read_bytes(), before)
                    self.assertFalse((self.txroot / 'activation.json').exists())
                finally:
                    lock.unlink()

    @unittest.skipUnless(os.name == 'nt', 'Shared profile updater locks require Windows')
    def test_abrupt_receipt_child_exit_allows_real_updater_and_receipt_reacquisition(self):
        ready = self.root / 'receipt-lock-ready'
        code = ('import sys,time; from pathlib import Path; '
                'sys.path.insert(0,sys.argv[1]); '
                'from scripts.component_activation_host import _update_locks; '
                'manager=_update_locks(Path(sys.argv[2])); manager.__enter__(); '
                'Path(sys.argv[3]).write_text("ready"); time.sleep(120)')
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(ROOT),
            str(self.root), str(ready)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists(), 'Fixture child did not acquire shared receipt locks')
            inventory = self.root / '.bot-mode/local/installations.json'
            locks = [inventory.with_name('profile-updates.lock'), inventory.with_suffix('.lock')]
            self.assertEqual(self.complete(), 0)
            with self.assertRaises(profile_update.UpdateError):
                with profile_update._locks(self.root, inventory):
                    self.fail('Real updater acquired locks held by the live receipt child')
            self.assertIsNone(child.poll())
            child.kill(); child.communicate(timeout=5)
            # No stale-marker deletion: the real updater must acquire both names itself.
            with profile_update._locks(self.root, inventory):
                self.assertTrue(all(path.is_file() for path in locks))
            self.assertTrue(all(not path.exists() for path in locks))
            self.assertEqual(self.complete(), 1)
            self.assertEqual(self.activation_state()['state'], 'verified_active')
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)

    def test_stale_completion_evidence_stays_pending(self):
        self.assertEqual(self.complete(completed_at='2000-01-01T00:00:00Z'), 0)
        self.assertFalse((self.txroot / 'activation.json').exists())

    def test_tampered_installed_file_and_unloaded_playbook_stay_pending(self):
        (self.profile / 'PROFILE.md').write_bytes(b'changed after prompt read\n')
        self.assertEqual(self.complete(), 0)
        self.assertFalse((self.txroot / 'activation.json').exists())

        (self.profile / 'PROFILE.md').write_bytes(self.contents['PROFILE.md'])
        playbook = self.profile / 'PLAYBOOK.md'
        playbook.write_bytes(b'Not part of the authorized Office prompt\n')
        path = f'{self.workspace}/agent/PLAYBOOK.md'
        self.target['files'][path] = _sha(playbook.read_bytes())
        self.target['installed_sha256'] = _sha(_canonical(self.target['files']))
        self.write_journal()
        self.assertEqual(self.complete(), 0)
        self.assertFalse((self.txroot / 'activation.json').exists())

    def test_existing_multi_consumer_record_remains_partial_after_one_receipt(self):
        pending = component_activation.pending(
            self.component, self.target['installed_sha256'],
            [{'bot': 'developer', 'thread_id': 'office-thread-current'},
             {'bot': 'reviewer', 'thread_id': 'native-review-thread'}],
        )
        (self.txroot / 'activation.json').write_text(json.dumps({
            'schema': 1, 'records': {self.component: pending},
        }), encoding='utf-8')

        self.assertEqual(self.complete(current_bindings={
            'developer': 'office-thread-current', 'reviewer': 'office-review-thread',
        }), 1)

        state = self.activation_state()
        self.assertEqual(state['state'], 'partial_activation')
        self.assertEqual([consumer['state'] for consumer in state['consumers']],
                         ['verified_active', 'pending_activation'])
        self.assertEqual(state['consumers'][1]['thread_id'], 'native-review-thread')
        self.assertIsNone(state['consumers'][1]['receipt'])
        observed = datetime(2026, 10, 21, 18, tzinfo=timezone.utc)
        safe = component_activation.eligibility({
            'bot': 'developer', 'thread_id': 'office-thread-current',
            'as_of': '2026-10-21T18:00:00+00:00', 'state': 'idle',
            'paused': False, 'pending_writes': False, 'ownership': 'office',
        }, 'developer', 'office-thread-current', now=observed)
        self.assertTrue(safe['eligible'])
        self.assertFalse(safe.get('dispatch_authorized', False))

    def test_native_binding_for_same_bot_is_never_replaced_or_acknowledged(self):
        pending = component_activation.pending(
            self.component, self.target['installed_sha256'],
            [{'bot': 'developer', 'thread_id': 'native-developer-thread'}],
        )
        (self.txroot / 'activation.json').write_text(json.dumps({
            'schema': 1, 'records': {self.component: pending},
        }), encoding='utf-8')

        self.assertEqual(self.complete(), 0)

        state = self.activation_state()
        self.assertEqual(state['state'], 'pending_activation')
        self.assertEqual(state['consumers'][0]['thread_id'], 'native-developer-thread')
        self.assertIsNone(state['consumers'][0]['receipt'])


class ProfileSnapshotTests(unittest.TestCase):
    def test_snapshot_hashes_only_base_soul_and_profile_bytes_actually_instructions(self):
        with tempfile.TemporaryDirectory(prefix='cbm-158-profile-', dir=Path(tempfile.gettempdir())) as folder:
            root = Path(folder)
            profile = root / 'team/Development/developer/agent'
            profile.mkdir(parents=True)
            data = {
                'BASE.md': b'Base prompt bytes\r\n',
                'SOUL.md': b'Soul prompt bytes\n',
                'PROFILE.md': b'Profile prompt bytes\n',
                'PLAYBOOK.md': b'Not loaded\n',
            }
            for name, body in data.items():
                (profile / name).write_bytes(body)
            helper = root / 'memory.py'
            helper.write_text(
                "def load_profile(project, bot): return {'memory/README.md': 'memory contract'}\n"
                "def recall(project, bot): return []\n", encoding='utf-8')
            with patch('scripts.team_room.project.profile_location', return_value=(
                    profile, helper, profile.parent.parent, 'developer', 'memory')):
                text, snapshot = profiles.instructions_with_snapshot('developer', 'private', root)

            self.assertIn('Base prompt bytes', text)
            self.assertIn('Soul prompt bytes', text)
            self.assertIn('Profile prompt bytes', text)
            self.assertNotIn('Not loaded', text)
            self.assertEqual(set(snapshot), {
                'team/Development/developer/agent/BASE.md',
                'team/Development/developer/agent/SOUL.md',
                'team/Development/developer/agent/PROFILE.md',
            })
            for name in ('BASE.md', 'SOUL.md', 'PROFILE.md'):
                path = f'team/Development/developer/agent/{name}'
                self.assertEqual(snapshot[path], _sha(data[name]))


class _CompletedTransport:
    def __init__(self):
        self.threads = {}
        self.start_count = 0

    def initialize(self):
        pass

    def start_thread(self, cwd, model, instructions):
        self.threads['office-developer-thread'] = []
        return {'thread': {'id': 'office-developer-thread'}, 'model': model}

    def read_thread(self, thread_id):
        return {'thread': {'turns': self.threads[thread_id]}}

    def resume_thread(self, thread_id, model, instructions=None):
        return self.read_thread(thread_id)

    def start_turn(self, thread_id, text, effort=None, before_send=None, images=()):
        self.start_count += 1
        if before_send:
            before_send(1)
        turn = {'id': f'office-turn-{self.start_count}', 'status': 'inProgress', 'items': []}
        self.threads[thread_id].append(turn)
        return {'turn': turn}

    def next_event(self, thread_id, turn_id, timeout=30):
        return {'method': 'turn/completed', 'params': {
            'threadId': thread_id, 'turnId': turn_id, 'status': 'completed',
        }}

    def close(self):
        pass


class ControllerProducerTests(unittest.TestCase):
    def test_only_completed_owned_turn_writes_receipt_and_never_dispatches(self):
        with tempfile.TemporaryDirectory(prefix='cbm-158-controller-', dir=Path(tempfile.gettempdir())) as folder:
            root = Path(folder) / 'project'
            root.mkdir()
            workspace = 'team/Development/developer'
            _write_office_roster(root, workspace)
            profile = root / workspace / 'agent'
            profile.mkdir(parents=True)
            files = {}
            for name, body in {'SOUL.md': b'Office soul\n', 'PROFILE.md': b'Office profile\n'}.items():
                (profile / name).write_bytes(body)
                files[f'{workspace}/agent/{name}'] = _sha(body)
            component = 'profile:' + workspace
            inventory = root / '.bot-mode/local/installations.json'
            inventory.parent.mkdir(parents=True)
            inventory.write_text(json.dumps({'schema': 1, 'components': [{
                'id': component, 'kind': 'profile', 'path': workspace, 'version': '2.0.0',
            }]}), encoding='utf-8')
            transaction = str(uuid.uuid4())
            txroot = root / '.bot-mode/local/profile-updates' / transaction
            txroot.mkdir(parents=True)
            target = {'component': component, 'release_component': 'developer-profile',
                      'path': workspace, 'version': '2.0.0', 'files': files,
                      'installed_sha256': _sha(_canonical(files))}
            (txroot / 'journal.json').write_text(json.dumps({
                'schema': 1, 'transaction_id': transaction, 'phase': 'applied',
                'plan_sha256': 'e' * 64,
                'selected_targets': [{'inventory_component': component,
                                      'release_component': 'developer-profile',
                                      'kind': 'profile', 'root': workspace}],
                'pending_activation': {'state': 'pending_activation', 'activated': False,
                                       'targets': [target]},
            }), encoding='utf-8')
            transport = _CompletedTransport()
            with patch.object(profiles, 'instructions', return_value='profile instructions'), \
                    patch.object(profiles, 'instructions_with_snapshot',
                                 return_value=('profile instructions', files)):
                controller = Controller(root / 'runtime', transport=transport, start_worker=False,
                                        bots=('developer',), session_kind='private', profile_project=root)
                try:
                    controller.send('Use the updated profile on this turn.', 'developer', 'turn-1')
                    active = controller._dispatch()
                    controller.wake.clear()
                    controller._consume(active)
                    state = json.loads((txroot / 'activation.json').read_text(encoding='utf-8'))
                    self.assertEqual(state['records'][component]['state'], 'verified_active')
                    self.assertEqual(controller.store.deliveries()[0]['state'], 'completed')
                    self.assertFalse(controller.store.room()['hold'])
                    self.assertEqual(transport.start_count, 1)
                    self.assertFalse(controller.wake.is_set())

                    controller.send('A later turn is unaffected by receipt persistence failure.',
                                    'developer', 'turn-2')
                    second = controller._dispatch()
                    controller.wake.clear()
                    with patch('scripts.component_activation_host.ActivationHost.complete_office_turn',
                               side_effect=OSError('fixture persistence failure')):
                        controller._consume(second)

                    controller.send('A turn with a queued follow-up.', 'developer', 'turn-3')
                    third = controller._dispatch()
                    controller.send('Queue one more owner turn.', 'developer', 'turn-4')
                    controller.wake.clear()
                    pending_writes = []
                    persist = ActivationHost.complete_office_turn

                    def observe_pending(host, **kwargs):
                        pending_writes.append(kwargs['pending_writes'])
                        return persist(host, **kwargs)

                    with patch('scripts.component_activation_host.ActivationHost.complete_office_turn',
                               new=observe_pending):
                        controller._consume(third)
                    self.assertEqual(pending_writes, [True])
                    self.assertFalse(controller.wake.is_set())

                    fourth = controller._dispatch()
                    controller.wake.clear()
                    controller._consume(fourth)
                    self.assertEqual([item['state'] for item in controller.store.deliveries()],
                                     ['completed'] * 4)
                    self.assertFalse(controller.store.room()['hold'])
                    self.assertEqual(transport.start_count, 4)
                    self.assertFalse(controller.wake.is_set())
                finally:
                    controller.close()


if __name__ == '__main__':
    unittest.main()
