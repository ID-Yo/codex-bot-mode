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


if __name__ == '__main__':
    unittest.main()
