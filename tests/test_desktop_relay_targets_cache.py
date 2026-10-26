import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from team_room import desktop_relay, registry, roster  # noqa: E402
import scripts.office_desktop as office_desktop  # noqa: E402

THREAD = '11111111-2222-3333-4444-{:012d}'


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


class RegistryFixture(unittest.TestCase):
    def setUp(self):
        desktop_relay.invalidate_targets()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.host = base / 'host'
        self.host.mkdir()
        projects = []
        for number in range(3):
            root = base / f'p{number}'
            self.write_roster(root, number)
            projects.append({'id': f'p{number}', 'root': str(root)})
        self.registry = self.host / '.bot-mode' / 'local' / 'office-project-roots.json'
        write(self.registry, {'schema': 1, 'projects': projects, 'standalone': []})
        self.roots = [Path(p['root']) for p in projects]
        patches = [mock.patch.object(desktop_relay.project, 'PROJECT', self.host),
                   mock.patch.dict(os.environ, {'CODEX_OFFICE_REGISTRY': ''})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.overviews = 0
        real = registry.build_overview

        def counted(*args, **kwargs):
            self.overviews += 1
            return real(*args, **kwargs)

        patch = mock.patch.object(registry, 'build_overview', side_effect=counted)
        patch.start()
        self.addCleanup(patch.stop)

    def write_roster(self, root, number, thread=None):
        root.mkdir(parents=True, exist_ok=True)
        data = roster.empty(str(root), f'P{number}')
        roster.add_team(data, 'Development', f'p{number} Development', 'development')
        roster.add_participant(data, 'manager', 'Manager', 'Development', 'team/Development/manager',
                               thread or THREAD.format(number))
        roster.save(str(root), data, roster.version(roster.empty(str(root))))


class TargetsCacheTests(RegistryFixture):
    def test_scan_is_built_once_and_not_repeated_without_changes(self):
        first = desktop_relay.targets()
        self.assertEqual(self.overviews, 1)
        for _ in range(20):
            self.assertEqual(desktop_relay.targets(), first)
        self.assertEqual(self.overviews, 1)

    def test_changed_roster_or_registry_rescans(self):
        desktop_relay.targets()
        before = self.overviews
        bindings = roster.local_path(self.roots[0])
        data = json.loads(bindings.read_text(encoding='utf-8'))
        data['manager']['thread_id'] = THREAD.format(99)
        bindings.write_text(json.dumps(data), encoding='utf-8')
        os.utime(bindings, ns=(1, 1))
        desktop_relay.targets()
        self.assertEqual(self.overviews, before + 1)
        desktop_relay.targets()
        self.assertEqual(self.overviews, before + 1)
        data = json.loads(self.registry.read_text(encoding='utf-8'))
        data['projects'].pop()
        write(self.registry, data)
        desktop_relay.targets()
        self.assertEqual(self.overviews, before + 2)

    def test_invalidate_and_ttl_force_a_rescan(self):
        desktop_relay.targets()
        desktop_relay.invalidate_targets()
        desktop_relay.targets()
        self.assertEqual(self.overviews, 2)
        desktop_relay._REGISTRY_TARGETS['at'] -= desktop_relay.REGISTRY_TARGETS_TTL + 1
        desktop_relay.targets()
        self.assertEqual(self.overviews, 3)

    def test_manager_threads_match_per_project_resolution(self):
        expected = {registry.resolve_project_manager(self.registry, f'p{n}', self.host)['thread_id'] for n in range(3)}
        self.assertEqual(len(expected), 3)
        self.assertLessEqual(expected, desktop_relay.targets())


class NativeCheckCacheTests(unittest.TestCase):
    def setUp(self):
        office_desktop._NATIVE_CACHE.clear()

    def make(self):
        calls = []

        class Bridge:
            def call(self, name, arguments):
                calls.append(arguments['threadId'])
                return {'thread': {'id': arguments['threadId'], 'status': {'type': 'idle'}}, 'turns': []}

        class Personal:
            native_bindings = {'bot': THREAD.format(1)}
            native_bridge = Bridge()
            controllers = {}

            def _native_root(self, bot, thread_id):
                return Path(tempfile.gettempdir()) / 'cbm288-missing-native-root'

        class Controller:
            closing = False
            active = None
            store = None
            work = None
            mutex = None

        return Controller(), Personal(), calls

    def test_status_polls_reuse_native_check_but_shutdown_reads_live(self):
        controller, personal, calls = self.make()
        for _ in range(5):
            state = office_desktop.inspect_shutdown(controller, personal, None, None, native_cache_ttl=10)
            self.assertEqual(state['safety'], 'safe')
        self.assertEqual(len(calls), 1)
        office_desktop.inspect_shutdown(controller, personal, None, None)
        office_desktop.inspect_shutdown(controller, personal, None, None)
        self.assertEqual(len(calls), 3)

class ColdStatusTests(unittest.TestCase):
    """A cold, slow Desktop must not block status; only stop reads live (#329)."""
    def setUp(self):
        office_desktop._NATIVE_CACHE.clear()
        office_desktop._STATUS.update(value=None, at=0.0, worker=None)

    def make(self, delay):
        import threading, time
        calls, gate = [], threading.Event()

        class Bridge:
            def call(self, name, arguments):
                calls.append(arguments['threadId'])
                gate.wait(delay)
                return {'thread': {'id': arguments['threadId'], 'status': {'type': 'idle'}}, 'turns': []}

        class Personal:
            native_bindings = {'a': THREAD.format(1), 'b': THREAD.format(2)}
            native_bridge = Bridge()
            controllers = {}

            def _native_root(self, bot, thread_id):
                return Path(tempfile.gettempdir()) / 'cbm329-missing-native-root'

        class Controller:
            closing = False
            active = None
            store = None
            work = None
            mutex = None

        return Controller(), Personal(), calls, gate

    def test_cold_status_returns_unknown_quickly_then_safe_once_checked(self):
        import time
        controller, personal, calls, gate = self.make(0.4)
        started = time.monotonic()
        first = office_desktop.inspect_status(controller, personal, wait=0.05)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(first['safety'], 'unknown')
        for _ in range(5):  # polls during the slow check join the single worker
            self.assertEqual(office_desktop.inspect_status(controller, personal, wait=0.01)['safety'], 'unknown')
        self.assertEqual(len(calls), 1)
        office_desktop._STATUS['worker'].join(5)
        self.assertEqual(office_desktop.inspect_status(controller, personal, wait=0.01)['safety'], 'safe')
        self.assertEqual(len(calls), 2)

    def test_stale_safe_is_never_reported_and_stop_check_stays_live(self):
        controller, personal, calls, gate = self.make(0)
        office_desktop.inspect_status(controller, personal, wait=2)
        office_desktop._STATUS['at'] -= 100
        office_desktop._NATIVE_CACHE.clear()
        gate.clear()
        stale = office_desktop.inspect_status(controller, personal, wait=0)
        self.assertNotEqual(stale['safety'], 'safe')
        before = len(calls)
        self.assertEqual(office_desktop.inspect_shutdown(controller, personal, None, None)['safety'], 'safe')
        self.assertGreater(len(calls), before)

class SlowAgentEvidenceTests(unittest.TestCase):
    """The agents endpoint answers promptly while per-project Desktop reads are slow (#329)."""
    def test_cold_build_returns_roster_cards_pending_then_fresh(self):
        import threading, time
        from unittest import mock
        from scripts.team_room import agent_visibility
        release = threading.Event()
        root = Path(tempfile.mkdtemp(prefix='cbm329-slow-'))
        agent_visibility._BUILDS.clear()
        real = lambda r, b=None, now=None: {'status': 'ok', 'agents': [{'bot_key': 'x'}], 'source_warnings': []}

        def slow_build(project_root, bridge=None, now=None):
            if bridge is not None:
                release.wait(5)  # one slow Desktop read per project
            return real(project_root, bridge)

        with mock.patch.object(agent_visibility, 'build', slow_build):
            started = time.monotonic()
            cold = agent_visibility.build_nonblocking(root, object(), wait=0.05)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual((cold['enrichment'], cold['agents']), ('pending', [{'bot_key': 'x'}]))
            self.assertTrue(any('Unknown' in w for w in cold['source_warnings']))
            release.set()
            agent_visibility._BUILDS[str(root)]['worker'].join(5)
            done = agent_visibility.build_nonblocking(root, object(), wait=0.5)
            self.assertEqual(done['enrichment'], 'fresh')

    def test_retained_records_after_failed_refresh_do_not_claim_working_and_hide_exception_text(self):
        from unittest import mock
        from scripts.team_room import agent_visibility
        root = Path(tempfile.mkdtemp(prefix='cbm329-stale-'))
        agent_visibility._BUILDS.clear()
        bridge = object()
        card = {'bot_key': 'x', 'execution': {'state': 'working', 'state_since': None, 'certainty': 'confirmed', 'freshness': 'fresh'},
                'runtime': {'freshness': 'fresh'}, 'uncertain': False}
        calls = []

        def build(project_root, b=None, now=None):
            calls.append(b)
            if b is not None and len(calls) > 1:
                raise RuntimeError('token=synthetic-do-not-log')
            return {'status': 'ok', 'agents': [json.loads(json.dumps(card))], 'source_warnings': []}

        with mock.patch.object(agent_visibility, 'build', build):
            agent_visibility.build_nonblocking(root, bridge, wait=0.5)
            agent_visibility._BUILDS[str(root)]['worker'].join(5)
            agent_visibility._BUILDS[str(root)]['at'] -= 100  # the finished build is now old
            agent_visibility.build_nonblocking(root, bridge, wait=0.5)
            agent_visibility._BUILDS[str(root)]['worker'].join(5)
            out = agent_visibility.build_nonblocking(root, bridge, wait=0.5)
        self.assertEqual(out['enrichment'], 'stale')
        shown = out['agents'][0]
        self.assertEqual(shown['execution']['state'], 'unknown')
        self.assertEqual(shown['execution']['freshness'], 'stale')
        self.assertEqual(shown['runtime']['freshness'], 'stale')
        self.assertTrue(shown['uncertain'])
        self.assertNotIn('synthetic-do-not-log', json.dumps(out))

    def test_a_replacement_bridge_does_not_reuse_the_old_connection_result(self):
        from unittest import mock
        from scripts.team_room import agent_visibility
        root = Path(tempfile.mkdtemp(prefix='cbm329-bridge-'))
        agent_visibility._BUILDS.clear()
        build = lambda project_root, b=None, now=None: {'status': 'ok', 'agents': [{'bot_key': 'old' if b == 'one' else 'new'}], 'source_warnings': []}
        with mock.patch.object(agent_visibility, 'build', build):
            agent_visibility.build_nonblocking(root, 'one', wait=0.5)
            agent_visibility._BUILDS[str(root)]['worker'].join(5)
            fresh_new = agent_visibility.build_nonblocking(root, 'two', wait=0.5)
        self.assertEqual(fresh_new['agents'], [{'bot_key': 'new'}])

    def test_failed_build_is_reported_not_hidden(self):
        from unittest import mock
        from scripts.team_room import agent_visibility
        root = Path(tempfile.mkdtemp(prefix='cbm329-fail-'))
        agent_visibility._BUILDS.clear()

        def broken(project_root, bridge=None, now=None):
            if bridge is not None:
                raise RuntimeError('bridge exploded')
            return {'status': 'ok', 'agents': [], 'source_warnings': []}

        with mock.patch.object(agent_visibility, 'build', broken):
            agent_visibility.build_nonblocking(root, object(), wait=0.5)
            agent_visibility._BUILDS[str(root)]['worker'].join(5)
            again = agent_visibility.build_nonblocking(root, object(), wait=0.5)
        self.assertEqual(again['enrichment'], 'failed')
        self.assertTrue(any('RuntimeError' in w for w in again['source_warnings']))
        self.assertFalse(any('bridge exploded' in w for w in again['source_warnings']))


class StatusContextTests(unittest.TestCase):
    def setUp(self):
        office_desktop._NATIVE_CACHE.clear()
        office_desktop._STATUS.update(value=None, at=0.0, worker=None, context=None)

    def test_safe_result_of_one_controller_is_never_returned_for_another(self):
        import threading
        class Idle:
            closing = False; active = None; store = None; work = None; mutex = None
        class Busy(Idle):
            active = {'turn': 1}
        first, second = Idle(), Busy()
        self.assertEqual(office_desktop.inspect_status(first, wait=2)['safety'], 'safe')
        other = office_desktop.inspect_status(second, wait=2)
        self.assertEqual(other['safety'], 'busy')

    def test_status_failure_reason_hides_exception_text(self):
        from unittest import mock
        class Idle:
            closing = False; active = None; store = None; work = None; mutex = None
        with mock.patch.object(office_desktop, 'inspect_shutdown', side_effect=RuntimeError('token=synthetic-do-not-log')):
            out = office_desktop.inspect_status(Idle(), wait=2)
        self.assertEqual(out['safety'], 'unknown')
        self.assertNotIn('synthetic-do-not-log', json.dumps(out))


if __name__ == '__main__':
    unittest.main()
