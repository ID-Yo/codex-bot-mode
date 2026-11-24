import time
"""A standalone Office client retains Desktop ownership through a local relay."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from scripts.team_room.desktop_relay import create_relay, RelayBridge
from scripts.team_room.work import MANAGER, WORKERS, Uncertain


class FakeBridge:
    origin = MANAGER
    def __init__(self): self.calls = []
    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return {'threads': [], 'origin': self.origin}


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bridge = FakeBridge()
        self.server = create_relay(self.bridge, self.root, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(timeout=5.0)
        self.assertFalse(self.worker.is_alive(), 'Relay worker thread failed to stop within 5.0s')
        self.temp.cleanup()
    def test_external_client_does_not_need_executor_environment(self):
        with patch.dict('os.environ', {}, clear=True):
            client = RelayBridge(self.root)
            self.assertEqual(client.call('list_threads', {'limit': 1})['origin'], MANAGER)
            client.close()
            self.assertEqual(client.call('list_threads', {})['threads'], [])
        saved = (self.root / 'relay.json').read_text()
        self.assertNotIn('CODEX_APP_TOOLS_PIPE_PATH', saved)
    def test_unauthenticated_request_cannot_read_or_send(self):
        config = json.loads((self.root / 'relay.json').read_text())
        request = Request(config['url'] + '/call', data=b'{"name":"list_threads","arguments":{}}', headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as error: urlopen(request)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.bridge.calls, [])
    def test_unappointed_send_target_and_tool_refused(self):
        client = RelayBridge(self.root)
        for name, args in [('create_thread', {}), ('send_message_to_thread', {'threadId':'other','prompt':'test'})]:
            with self.assertRaises(ValueError): client.call(name, args)
        self.assertEqual(self.bridge.calls, [])
    def test_send_reaches_a_bound_roster_chat_of_a_registry_project_but_not_an_unbound_one(self):
        bound = '0bb00000-0000-7000-8000-000000000001'
        client = RelayBridge(self.root)
        with patch('scripts.team_room.desktop_relay.sendable_targets', return_value=frozenset({bound})):
            client.call('send_message_to_thread', {'threadId': bound, 'prompt': 'report'})
            self.assertEqual(self.bridge.calls[-1][1]['threadId'], bound)
            with self.assertRaises(ValueError):
                client.call('send_message_to_thread', {'threadId': '0bb00000-0000-7000-8000-000000000002', 'prompt': 'x'})
            with self.assertRaises(ValueError):  # waiting still needs an appointed chat
                client.call('wait_threads', {'targets': [{'threadId': bound, 'hostId': 'local'}]})
        self.assertEqual(len(self.bridge.calls), 1)

    def test_approved_personal_target_keeps_original_thread(self):
        client = RelayBridge(self.root)
        client.call('send_message_to_thread', {'threadId':WORKERS['developer'],'prompt':'test'})
        self.assertEqual(self.bridge.calls[-1][1]['threadId'], WORKERS['developer'])
    def test_failed_send_is_never_retried(self):
        def uncertain(name, arguments):
            self.bridge.calls.append((name, arguments))
            raise Uncertain('Disconnected')
        self.bridge.call = uncertain
        with self.assertRaises(Uncertain): RelayBridge(self.root).call('send_message_to_thread', {'threadId':MANAGER,'prompt':'test'})
        self.assertEqual(len(self.bridge.calls), 1)
    def test_client_ignores_external_http_proxy(self):
        with patch.dict('os.environ', {'HTTP_PROXY':'http://example.invalid:9','NO_PROXY':''}):
            self.assertEqual(RelayBridge(self.root).call('list_threads', {})['threads'], [])
    def test_registration_cannot_redirect_capability_off_machine(self):
        path = self.root / 'relay.json'
        saved = json.loads(path.read_text())
        for url in ('https://example.com', 'http://localhost:24243/?redirect=1', 'http://user@localhost:24243'):
            path.write_text(json.dumps({**saved, 'url':url}))
            with self.assertRaises(ValueError): RelayBridge(self.root)


    def test_activity_endpoint_batches_all_bots_and_detects_active(self):
        # Create 9 bots to test that >8 bots are not truncated
        bots = [
            {'bot_id': f'bot_{i}', 'name': f'Bot {i}', 'role': 'Worker', 'thread_id': f'0aa00000-0000-7000-8000-00000000000{i}'}
            for i in range(1, 10)
        ]
        with patch('scripts.team_room.registry.registry_path', return_value=Path('dummy/reg.json')),              patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots}):
            
            # Setup FakeBridge to record calls and return 9th bot active, others idle
            def fake_wait(name, args):
                self.bridge.calls.append((name, args))
                polls = []
                for t in args.get('targets', []):
                    tid = t['threadId']
                    # Make 9th bot active, all others idle
                    st = 'active' if tid.endswith('9') else 'idle'
                    polls.append({'thread': {'id': tid, 'status': {'type': st}}, 'hostId': 'local'})
                return {'polls': polls}

            self.bridge.call = fake_wait
            client = RelayBridge(self.root)
            act = client.activity('alpha')

            # Verify both batches were queried (batch 1: 8 targets, batch 2: 1 target)
            wait_calls = [c for c in self.bridge.calls if c[0] == 'wait_threads']
            self.assertEqual(len(wait_calls), 2)
            self.assertEqual(len(wait_calls[0][1]['targets']), 8)
            self.assertEqual(len(wait_calls[1][1]['targets']), 1)

            # Verify 9th active bot makes aggregate status 'active'
            self.assertEqual(act['status'], 'active')
            self.assertIn('Bot 9', act['active_bots'])
            self.assertFalse(act['partial_failure'])

    def test_activity_cache_invalidation_on_roster_change(self):
        bots_v1 = [{'bot_id': 'b1', 'name': 'Bot 1', 'role': 'Worker', 'thread_id': '0aa00000-0000-7000-8000-000000000001'}]
        # Adding an unbound bot without thread_id MUST invalidate cache and switch status to unknown with partial_failure
        bots_v2 = [
            {'bot_id': 'b1', 'name': 'Bot 1', 'role': 'Worker', 'thread_id': '0aa00000-0000-7000-8000-000000000001'},
            {'bot_id': 'b2', 'name': 'Bot 2', 'role': 'Worker', 'thread_id': None}
        ]

        with patch('scripts.team_room.registry.registry_path', return_value=Path('dummy/reg.json')):
            with patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots_v1}):
                self.bridge.call = lambda name, args: {'polls': [{'thread': {'id': '0aa00000-0000-7000-8000-000000000001', 'status': {'type': 'idle'}}, 'hostId': 'local'}]}
                client = RelayBridge(self.root)
                act1 = client.activity('beta')
                self.assertEqual(act1['status'], 'inactive')
                self.assertFalse(act1['partial_failure'])

            # Changing roster to bots_v2 immediately invalidates v1 cache and reports partial_failure=True & status=unknown
            with patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots_v2}):
                act2 = client.activity('beta')
                self.assertEqual(act2['status'], 'unknown')
                self.assertTrue(act2['partial_failure'])


    def test_activity_singleflight_follower_timeout_does_not_compete_or_corrupt(self):
        bots = [{'bot_id': 'b1', 'name': 'Bot 1', 'role': 'Worker', 'thread_id': '0aa00000-0000-7000-8000-000000000001'}]
        from scripts.team_room import desktop_relay
        bridge_call_count = [0]
        unblock_owner = threading.Event()
        owner_entered = threading.Event()

        def slow_bridge_call(name, args):
            bridge_call_count[0] += 1
            owner_entered.set()
            unblock_owner.wait(timeout=5.0)
            return {'polls': [{'thread': {'id': '0aa00000-0000-7000-8000-000000000001', 'status': {'type': 'active'}}, 'hostId': 'local'}]}

        self.bridge.call = slow_bridge_call

        with patch('scripts.team_room.registry.registry_path', return_value=Path('dummy/reg.json')),              patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots}):
            
            client = RelayBridge(self.root)
            owner_res = []
            follower_res = []

            # Start owner in background thread
            owner_thread = threading.Thread(target=lambda: owner_res.append(client.activity('proj_singleflight')))
            owner_thread.start()

            # Wait until owner enters bridge call
            self.assertTrue(owner_entered.wait(timeout=10.0))

            # Follower runs with patched short timeout (0.1s)
            with patch.object(desktop_relay, 'FOLLOWER_WAIT_TIMEOUT', 0.1):
                follower_act = client.activity('proj_singleflight')
                follower_res.append(follower_act)

            # Follower must have returned timed out unknown result
            self.assertEqual(follower_res[0]['status'], 'unknown')
            self.assertIn('timed out', follower_res[0].get('error', ''))
            
            # Crucial: follower did NOT start a second competing bridge call!
            self.assertEqual(bridge_call_count[0], 1)

            # Now unblock owner and let it complete
            unblock_owner.set()
            owner_thread.join(timeout=5.0)
            self.assertFalse(owner_thread.is_alive())

            # Owner returned active
            self.assertEqual(owner_res[0]['status'], 'active')

            # Subsequent call immediately gets owner's cached active result with 0 additional bridge calls
            subsequent_act = client.activity('proj_singleflight')
            self.assertEqual(subsequent_act['status'], 'active')
            self.assertEqual(bridge_call_count[0], 1)


    def test_activity_wait_failure_falls_back_to_list_threads_active(self):
        bots = [
            {'bot_id': 'manager', 'name': 'Manager', 'role': 'Manager', 'thread_id': '0aa00000-0000-7000-8000-000000000001'},
            {'bot_id': 'dev', 'name': 'Developer', 'role': 'Developer', 'thread_id': '0aa00000-0000-7000-8000-000000000002'}
        ]

        def call_router(name, args):
            if name == 'wait_threads':
                raise RuntimeError('MCP error -32000: Codex app tool request failed')
            if name == 'list_threads':
                self.assertEqual(args.get('limit'), 50)
                return {
                    'pinnedThreads': [
                        {'id': '0aa00000-0000-7000-8000-000000000001', 'status': 'idle', 'hostId': 'local', 'title': 'SECRET_TITLE', 'summary': 'SECRET_SUMMARY'}
                    ],
                    'threads': [
                        {'id': '0aa00000-0000-7000-8000-000000000002', 'status': 'active', 'hostId': 'local', 'title': 'SECRET_DEV', 'summary': 'SECRET_DEV_SUM'}
                    ]
                }
            raise ValueError('Unexpected tool: ' + name)

        self.bridge.call = call_router

        with patch('scripts.team_room.registry.registry_path', return_value=Path('dummy/reg.json')),              patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots}):
            client = RelayBridge(self.root)
            act = client.activity('fallback_active')
            self.assertEqual(act['status'], 'active')
            self.assertEqual(act['source'], 'desktop_relay_list_threads')
            self.assertIn('Developer', act['active_bots'])
            self.assertFalse(act['partial_failure'])
            # Verify privacy: no titles or summaries in returned metadata
            self.assertNotIn('SECRET_TITLE', json.dumps(act))
            self.assertNotIn('SECRET_SUMMARY', json.dumps(act))

    def test_activity_wait_failure_falls_back_to_list_threads_all_idle(self):
        bots = [
            {'bot_id': 'manager', 'name': 'Manager', 'role': 'Manager', 'thread_id': '0aa00000-0000-7000-8000-000000000001'},
            {'bot_id': 'dev', 'name': 'Developer', 'role': 'Developer', 'thread_id': '0aa00000-0000-7000-8000-000000000002'}
        ]

        def call_router(name, args):
            if name == 'wait_threads':
                raise RuntimeError('MCP error -32000: Codex app tool request failed')
            if name == 'list_threads':
                return {
                    'pinnedThreads': [],
                    'threads': [
                        {'id': '0aa00000-0000-7000-8000-000000000001', 'status': 'idle', 'hostId': 'local'},
                        {'id': '0aa00000-0000-7000-8000-000000000002', 'status': 'idle', 'hostId': 'local'}
                    ]
                }
            raise ValueError('Unexpected tool: ' + name)

        self.bridge.call = call_router

        with patch('scripts.team_room.registry.registry_path', return_value=Path('dummy/reg.json')),              patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots}):
            client = RelayBridge(self.root)
            act = client.activity('fallback_all_idle')
            self.assertEqual(act['status'], 'inactive')
            self.assertEqual(act['source'], 'desktop_relay_list_threads')
            self.assertEqual(act['active_bots'], [])
            self.assertFalse(act['partial_failure'])

    def test_activity_not_loaded_status_maps_to_unknown_partial(self):
        bots = [
            {'bot_id': 'manager', 'name': 'Manager', 'role': 'Manager', 'thread_id': '0aa00000-0000-7000-8000-000000000001'},
            {'bot_id': 'dev', 'name': 'Developer', 'role': 'Developer', 'thread_id': '0aa00000-0000-7000-8000-000000000002'}
        ]

        def call_router(name, args):
            if name == 'wait_threads':
                raise RuntimeError('MCP error -32000: Codex app tool request failed')
            if name == 'list_threads':
                return {
                    'pinnedThreads': [],
                    'threads': [
                        {'id': '0aa00000-0000-7000-8000-000000000001', 'status': 'idle', 'hostId': 'local'},
                        {'id': '0aa00000-0000-7000-8000-000000000002', 'status': 'notLoaded', 'hostId': 'local'}
                    ]
                }
            raise ValueError('Unexpected tool: ' + name)

        self.bridge.call = call_router

        with patch('scripts.team_room.registry.registry_path', return_value=Path('dummy/reg.json')),              patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots}):
            client = RelayBridge(self.root)
            act = client.activity('fallback_not_loaded')
            # notLoaded is not treated as idle -> failclosed maps to unknown with partial_failure=True
            self.assertEqual(act['status'], 'unknown')
            self.assertEqual(act['source'], 'desktop_relay_list_threads')
            self.assertTrue(act['partial_failure'])

    def test_activity_wait_failure_missing_from_list_threads_marks_unknown_partial(self):
        bots = [
            {'bot_id': 'manager', 'name': 'Manager', 'role': 'Manager', 'thread_id': '0aa00000-0000-7000-8000-000000000001'},
            {'bot_id': 'dev', 'name': 'Developer', 'role': 'Developer', 'thread_id': '0aa00000-0000-7000-8000-000000000002'}
        ]

        # Only manager is in the list, dev is omitted (e.g. beyond 50)
        def call_router(name, args):
            if name == 'wait_threads':
                raise RuntimeError('MCP error -32000: Codex app tool request failed')
            if name == 'list_threads':
                return {
                    'pinnedThreads': [],
                    'threads': [
                        {'id': '0aa00000-0000-7000-8000-000000000001', 'status': 'idle', 'hostId': 'local'}
                    ]
                }
            raise ValueError('Unexpected tool: ' + name)

        self.bridge.call = call_router

        with patch('scripts.team_room.registry.registry_path', return_value=Path('dummy/reg.json')),              patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots}):
            client = RelayBridge(self.root)
            act = client.activity('fallback_omitted')
            # Since dev is missing and manager is idle, must NOT infer all idle beyond coverage!
            self.assertEqual(act['status'], 'unknown')
            self.assertEqual(act['source'], 'desktop_relay_list_threads')
            self.assertTrue(act['partial_failure'])


    def test_activity_wait_batch_idle_preserved_when_omitted_from_fallback(self):
        # 9 bots: bots 1..8 in batch 1, bot 9 in batch 2
        bots = [
            {'bot_id': f'b{i}', 'name': f'Bot {i}', 'role': 'Worker', 'thread_id': f'0aa00000-0000-7000-8000-00000000000{i}'}
            for i in range(1, 10)
        ]

        def call_router(name, args):
            if name == 'wait_threads':
                targets = args.get('targets', [])
                # First batch (8 bots) succeeds with idle
                if len(targets) == 8:
                    return {'polls': [{'thread': {'id': t['threadId'], 'status': {'type': 'idle'}}, 'hostId': 'local'} for t in targets]}
                # Second batch (9th bot) fails
                raise RuntimeError('MCP error -32000: second batch failed')
            if name == 'list_threads':
                # Fallback list contains ONLY bot 9 as idle (bots 1..8 omitted, e.g. beyond 50)
                return {
                    'pinnedThreads': [],
                    'threads': [
                        {'id': '0aa00000-0000-7000-8000-000000000009', 'status': 'idle', 'hostId': 'local'}
                    ]
                }
            raise ValueError('Unexpected tool: ' + name)

        self.bridge.call = call_router

        with patch('scripts.team_room.registry.registry_path', return_value=Path('dummy/reg.json')),              patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots}):
            client = RelayBridge(self.root)
            act = client.activity('batch_idle_preserved')
            # All 9 bots are covered: bots 1..8 idle from batch 1, bot 9 idle from list_threads => all covered idle / inactive!
            self.assertEqual(act['status'], 'inactive')
            self.assertEqual(act['source'], 'desktop_relay_list_threads')
            self.assertFalse(act['partial_failure'])

    def test_activity_conflicting_duplicate_statuses_maps_to_unknown(self):
        bots = [{'bot_id': 'b1', 'name': 'Bot 1', 'role': 'Worker', 'thread_id': '0aa00000-0000-7000-8000-000000000001'}]

        def call_router(name, args):
            if name == 'wait_threads':
                raise RuntimeError('MCP error -32000: wait failed')
            if name == 'list_threads':
                # Duplicate entries with conflicting non-active statuses (e.g. idle and notLoaded)
                return {
                    'pinnedThreads': [{'id': '0aa00000-0000-7000-8000-000000000001', 'status': 'idle', 'hostId': 'local'}],
                    'threads': [{'id': '0aa00000-0000-7000-8000-000000000001', 'status': 'notLoaded', 'hostId': 'local'}]
                }
            raise ValueError('Unexpected tool: ' + name)

        self.bridge.call = call_router

        with patch('scripts.team_room.registry.registry_path', return_value=Path('dummy/reg.json')),              patch('scripts.team_room.registry.resolve_project_bots', return_value={'status': 'ok', 'bots': bots}):
            client = RelayBridge(self.root)
            act = client.activity('conflicting_duplicates')
            # Conflicting statuses should NOT produce false inactive -> unknown with partial_failure
            self.assertEqual(act['status'], 'unknown')
            self.assertTrue(act['partial_failure'])


if __name__ == '__main__': unittest.main()
