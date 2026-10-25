import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import registry, roster  # noqa: E402

THREAD = '11111111-2222-3333-4444-{:012d}'


class OverviewCacheTests(unittest.TestCase):
    def setUp(self):
        registry.clear_overview_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.project = base / 'alpha'
        self.project.mkdir()
        data = roster.empty(str(self.project), 'Alpha')
        roster.add_team(data, 'Development', 'alpha Development', 'development')
        roster.add_participant(data, 'manager', 'Manager', 'Development', 'team/Development/manager', THREAD.format(1))
        roster.save(str(self.project), data, roster.version(roster.empty(str(self.project))))
        self.config = base / 'reg.json'
        self.config.write_text(json.dumps({'schema': 1, 'standalone': [],
                                           'projects': [{'id': 'alpha', 'name': 'Alpha', 'root': str(self.project)}]}),
                               encoding='utf-8')
        real = registry._build_overview
        self.builds = 0

        def counted(*args, **kwargs):
            self.builds += 1
            return real(*args, **kwargs)

        patch = mock.patch.object(registry, '_build_overview', side_effect=counted)
        patch.start()
        self.addCleanup(patch.stop)

    def test_unchanged_files_are_not_scanned_again_and_callers_get_copies(self):
        first = registry.build_overview(self.config, current_project_root=self.project)
        first['projects'].clear()
        second = registry.build_overview(self.config, current_project_root=self.project)
        for _ in range(10):
            registry.build_overview(self.config, current_project_root=self.project)
        self.assertEqual(self.builds, 1)
        self.assertEqual(second['projects'][0]['id'], 'alpha')

    def test_changed_bindings_roster_or_registry_rebuild(self):
        registry.build_overview(self.config, current_project_root=self.project)
        bindings = roster.local_path(self.project)
        data = json.loads(bindings.read_text(encoding='utf-8'))
        data['manager']['thread_id'] = THREAD.format(2)
        bindings.write_text(json.dumps(data), encoding='utf-8')
        os.utime(bindings, ns=(1, 1))
        overview = registry.build_overview(self.config, current_project_root=self.project)
        self.assertEqual(self.builds, 2)
        self.assertEqual(overview['bots'][0]['thread_id'], THREAD.format(2))
        raw = json.loads(self.config.read_text(encoding='utf-8'))
        raw['projects'][0]['name'] = 'Renamed'
        self.config.write_text(json.dumps(raw), encoding='utf-8')
        os.utime(self.config, ns=(5, 5))
        self.assertEqual(registry.build_overview(self.config, current_project_root=self.project)['projects'][0]['name'], 'Renamed')
        self.assertEqual(self.builds, 3)

    def check_invalid_projects(self, value, status="invalid_config"):
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        raw["projects"] = value
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        overview = registry.build_overview(self.config, current_project_root=self.project)
        self.assertEqual(overview["status"], status)
        self.assertEqual(overview["bots"], [])

    def test_projects_null_behaves_as_before_an_empty_registry(self):
        self.check_invalid_projects(None, status="ok")

    def test_projects_number_is_invalid_config_not_a_crash(self):
        self.check_invalid_projects(5)

    def test_projects_with_non_object_items_is_invalid_config_not_a_crash(self):
        self.check_invalid_projects([5, "x"])

    def test_relay_target_scan_survives_invalid_projects(self):
        from team_room import desktop_relay
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        raw["projects"] = 5
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        desktop_relay.invalidate_targets()
        with mock.patch.object(desktop_relay.registry if hasattr(desktop_relay, "registry") else registry, "registry_path", return_value=self.config), \
                mock.patch.object(desktop_relay.project, "PROJECT", self.project):
            desktop_relay._registry_targets()

    def test_ttl_forces_a_rebuild(self):
        registry.build_overview(self.config, current_project_root=self.project)
        with mock.patch.object(registry.time, 'monotonic', return_value=registry.time.monotonic() + registry.OVERVIEW_TTL + 1):
            registry.build_overview(self.config, current_project_root=self.project)
        self.assertEqual(self.builds, 2)


if __name__ == '__main__':
    unittest.main()
