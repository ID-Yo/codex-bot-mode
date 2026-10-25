import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import registry, roster

class RegistryTests(unittest.TestCase):
    def test_missing_or_not_configured(self):
        res = registry.build_overview(None)
        self.assertEqual(res['status'], 'not_configured')
        self.assertEqual(res['bots'], [])
        self.assertEqual(res['teams'], [])

    def test_invalid_config_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_file = Path(tmp) / 'bad.json'
            bad_file.write_text(json.dumps({'schema': 99, 'projects': []}), encoding='utf-8')
            res = registry.build_overview(str(bad_file))
            self.assertEqual(res['status'], 'invalid_config')
            self.assertIn('Unsupported registry schema', res['error'])

            dup = {
                'schema': 1,
                'projects': [
                    {'id': 'p1', 'root': 'C:/p1'},
                    {'id': 'p1', 'root': 'C:/p2'}
                ]
            }
            bad_file.write_text(json.dumps(dup), encoding='utf-8')
            res = registry.build_overview(str(bad_file))
            self.assertEqual(res['status'], 'invalid_config')
            self.assertIn('Duplicate project id', res['error'])

            dup_st = {
                'schema': 1,
                'projects': [],
                'standalone': [
                    {'id': 'cos', 'thread_id': '0aa00000-0000-7000-8000-000000000001'},
                    {'id': 'cos', 'thread_id': '0aa00000-0000-7000-8000-000000000002'}
                ]
            }
            bad_file.write_text(json.dumps(dup_st), encoding='utf-8')
            res = registry.build_overview(str(bad_file))
            self.assertEqual(res['status'], 'invalid_config')
            self.assertIn('Duplicate standalone bot id', res['error'])

            rel = {
                'schema': 1,
                'projects': [{'id': 'p1', 'root': 'relative/path'}]
            }
            bad_file.write_text(json.dumps(rel), encoding='utf-8')
            res = registry.build_overview(str(bad_file))
            self.assertEqual(res['status'], 'invalid_config')
            self.assertIn('root must be an absolute path', res['error'])

            st_bad = {
                'schema': 1,
                'projects': [],
                'standalone': [{'id': 'cos', 'thread_id': 'invalid-uuid'}]
            }
            bad_file.write_text(json.dumps(st_bad), encoding='utf-8')
            res = registry.build_overview(str(bad_file))
            self.assertEqual(res['status'], 'invalid_config')
            self.assertIn('invalid thread_id', res['error'])

    def test_symlink_containment_prevents_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside_secret = Path(tmp) / 'outside_dir'
            outside_secret.mkdir()
            secret_yaml = outside_secret / 'agent.yaml'
            secret_yaml.write_text('parameters:\n  display_name:\n    value: SECRET_NAME\n', encoding='utf-8')

            proj_dir = Path(tmp) / 'proj'
            proj_dir.mkdir()
            workspace_agent = proj_dir / 'team' / 'Dev' / 'agent'
            workspace_agent.mkdir(parents=True)
            target_yaml = workspace_agent / 'agent.yaml'
            try:
                target_yaml.symlink_to(secret_yaml)
            except (OSError, NotImplementedError):
                pass

            self.assertFalse(registry._is_safe_contained(secret_yaml, proj_dir))
            disp = registry._get_display_name(proj_dir, 'team/Dev', 'DefaultRole')
            self.assertEqual(disp, 'DefaultRole')
            self.assertNotIn('SECRET_NAME', disp)

    def test_cross_project_bots_and_teams(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / 'alpha'
            p1.mkdir()
            r1 = roster.empty(str(p1), 'Alpha')
            roster.add_team(r1, 'Development', 'alpha · Development', 'development')
            roster.add_participant(r1, 'manager', 'Manager', 'Development', 'team/Development/manager', '0aa00000-0000-7000-8000-000000000001')
            roster.add_participant(r1, 'developer', 'Developer', 'Development', 'team/Development/developer', '0aa00000-0000-7000-8000-000000000002')
            roster.save(str(p1), r1, roster.version(roster.empty(str(p1))))

            p2 = Path(tmp) / 'beta'
            p2.mkdir()
            r2 = roster.empty(str(p2), 'Beta')
            roster.add_team(r2, 'Development', 'beta · Development', 'development')
            roster.add_participant(r2, 'manager', 'Manager', 'Development', 'team/Development/manager', '0aa00000-0000-7000-8000-000000000003')
            roster.add_participant(r2, 'developer', 'Developer', 'Development', 'team/Development/developer', None)
            roster.save(str(p2), r2, roster.version(roster.empty(str(p2))))

            p3 = Path(tmp) / 'gamma'

            config = {
                'schema': 1,
                'projects': [
                    {'id': 'alpha', 'name': 'Alpha Project', 'root': str(p1)},
                    {'id': 'beta', 'name': 'Beta Project', 'root': str(p2)},
                    {'id': 'gamma', 'name': 'Gamma Project', 'root': str(p3)}
                ],
                'standalone': [
                    {'id': 'chief-of-staff', 'name': 'Chief of Staff/Fos', 'root': str(tmp), 'thread_id': '0aa00000-0000-7000-8000-000000000099'}
                ]
            }
            cfile = Path(tmp) / 'reg.json'
            cfile.write_text(json.dumps(config), encoding='utf-8')

            overview = registry.build_overview(str(cfile), current_project_id='alpha')
            self.assertEqual(overview['status'], 'ok')
            bots = overview['bots']
            self.assertEqual(len(bots), 5)
            
            keys = [b['bot_key'] for b in bots]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertIn('alpha:developer', keys)
            self.assertIn('beta:developer', keys)
            self.assertIn('standalone:chief-of-staff', keys)

            self.assertEqual(registry.resolve_bot_thread(str(cfile), 'alpha:developer'), '0aa00000-0000-7000-8000-000000000002')
            self.assertIsNone(registry.resolve_bot_thread(str(cfile), 'beta:developer'))
            self.assertIsNone(registry.resolve_bot_thread(str(cfile), 'nonexistent:bot'))

            teams = overview['teams']
            self.assertEqual(len(teams), 3)
            gamma_team = next(t for t in teams if t['project_id'] == 'gamma')
            self.assertEqual(gamma_team['status'], 'source_unavailable')

            projects = overview['projects']
            self.assertEqual(len(projects), 3)
            alpha_proj = next(p for p in projects if p['id'] == 'alpha')
            self.assertEqual(alpha_proj['status'], 'ok')
            self.assertTrue(alpha_proj['applicable'])
            self.assertTrue(alpha_proj['is_current'])
            self.assertIsNone(alpha_proj['reason'])

            beta_proj = next(p for p in projects if p['id'] == 'beta')
            self.assertEqual(beta_proj['status'], 'ok')
            self.assertTrue(beta_proj['applicable'])
            self.assertFalse(beta_proj['is_current'])

            gamma_proj = next(p for p in projects if p['id'] == 'gamma')
            self.assertEqual(gamma_proj['status'], 'source_unavailable')
            self.assertFalse(gamma_proj['applicable'])
            self.assertIn('missing', gamma_proj['reason'].lower())

    def test_overview_projects_escaping_roster_and_no_teams(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p_escape = root / 'esc'
            p_escape.mkdir()
            outside = root / 'outside_roster.json'
            outside.write_text('{}', encoding='utf-8')
            roster_sym = p_escape / '.bot-mode/roster.json'
            roster_sym.parent.mkdir(parents=True)
            try:
                roster_sym.symlink_to(outside)
            except (OSError, NotImplementedError):
                pass

            p_noteams = root / 'noteams'
            p_noteams.mkdir()
            r_empty = roster.empty(str(p_noteams), 'NoTeams')
            roster.save(str(p_noteams), r_empty, roster.version(roster.load(str(p_noteams))))

            config = {
                'schema': 1,
                'projects': [
                    {'id': 'esc', 'name': 'Escape', 'root': str(p_escape)},
                    {'id': 'noteams', 'name': 'NoTeams', 'root': str(p_noteams)}
                ]
            }
            cfile = root / 'reg.json'
            cfile.write_text(json.dumps(config), encoding='utf-8')

            overview = registry.build_overview(str(cfile), current_project_root=str(p_noteams))
            projects = {p['id']: p for p in overview['projects']}
            self.assertTrue(projects['noteams']['is_current'])
            self.assertEqual(projects['noteams']['status'], 'no_teams')
            self.assertTrue(projects['noteams']['applicable'])

            if roster_sym.is_symlink():
                self.assertEqual(projects['esc']['status'], 'roster_error')
                self.assertFalse(projects['esc']['applicable'])
                self.assertIn('escape', projects['esc']['reason'].lower())

    def test_fallback_current_project_rejects_escaping_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p_curr = root / 'curr_esc'
            p_curr.mkdir()
            outside = root / 'outside_roster.json'
            outside.write_text('{}', encoding='utf-8')
            sym = p_curr / '.bot-mode/roster.json'
            sym.parent.mkdir(parents=True)
            try:
                sym.symlink_to(outside)
            except (OSError, NotImplementedError):
                pass

            if sym.is_symlink():
                overview = registry.build_overview(None, current_project_root=str(p_curr))
                self.assertEqual(overview['status'], 'not_configured')
                self.assertEqual(len(overview['projects']), 1)
                curr = overview['projects'][0]
                self.assertEqual(curr['status'], 'roster_error')
                self.assertFalse(curr['applicable'])
                self.assertIn('escape', curr['reason'].lower())

    def test_resolve_project_bots(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / 'alpha'
            p1.mkdir()
            r1 = roster.empty(str(p1), 'Alpha')
            roster.add_team(r1, 'Development', 'alpha · Development', 'development')
            roster.add_participant(r1, 'manager', 'Manager', 'Development', 'team/Development/manager', '0aa00000-0000-7000-8000-000000000001')
            roster.add_participant(r1, 'developer', 'Developer', 'Development', 'team/Development/developer', '0aa00000-0000-7000-8000-000000000002')
            roster.save(str(p1), r1, roster.version(roster.empty(str(p1))))

            config = {
                'schema': 1,
                'projects': [{'id': 'alpha', 'name': 'Alpha Project', 'root': str(p1)}],
                'standalone': []
            }
            cfile = Path(tmp) / 'reg.json'
            cfile.write_text(json.dumps(config), encoding='utf-8')

            res = registry.resolve_project_bots(str(cfile), 'alpha')
            self.assertEqual(res['status'], 'ok')
            self.assertEqual(len(res['bots']), 2)
            b_ids = {b['bot_id'] for b in res['bots']}
            self.assertEqual(b_ids, {'manager', 'developer'})

            # Non-configured project
            res_none = registry.resolve_project_bots(str(cfile), 'nonexistent')
            self.assertEqual(res_none['status'], 'not_configured')

            # Invalid ID
            res_bad = registry.resolve_project_bots(str(cfile), '')
            self.assertEqual(res_bad['status'], 'invalid_id')


if __name__ == '__main__':
    unittest.main()
