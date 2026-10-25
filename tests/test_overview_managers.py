"""The overview names the project Manager once, with the rules of manager_from_overview (#320 review): the exact
Manager role, a project Manager preferred over team Managers (*-manager), nothing when ambiguous."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import registry, roster  # noqa: E402


def thread(n):
    return '0bb00000-0000-7000-8000-%012d' % n


class OverviewManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects = []
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def project(self, pid, participants):
        """participants: [(bot, role, team)] in the order they are added to the roster."""
        folder = self.root / pid
        folder.mkdir()
        data = roster.empty(str(folder), pid.title())
        for team in dict.fromkeys(t for _, _, t in participants):
            roster.add_team(data, team, f'{pid} · {team}', team.lower())
        for bot, role, team in participants:
            self.n += 1
            roster.add_participant(data, bot, role, team, f'team/{team}/{bot}', thread(self.n))
        roster.save(str(folder), data, roster.version(roster.empty(str(folder))))
        self.projects.append({'id': pid, 'name': pid.title(), 'root': str(folder)})

    def overview(self):
        config = self.root / 'reg.json'
        config.write_text(json.dumps({'schema': 1, 'projects': self.projects, 'standalone': []}), encoding='utf-8')
        registry.clear_overview_cache()
        return registry.build_overview(str(config), current_project_root=self.root)

    def test_team_manager_listed_first_does_not_replace_the_project_manager(self):
        self.project('delta', [('marketing-manager', 'Manager', 'Marketing'), ('manager', 'Manager', 'Website')])
        self.assertEqual(self.overview()['managers'], {'delta': 'delta:manager'})

    def test_ambiguous_managers_name_nobody(self):
        self.project('gamma', [('manager', 'Manager', 'Dev'), ('manager2', 'Manager', 'Dev')])
        self.assertEqual(self.overview()['managers'], {})

    def test_substring_ids_and_roles_never_qualify(self):
        self.project('zeta', [('manager-helper', 'Developer', 'Dev'), ('manager', 'Manager', 'Dev')])
        self.project('eta', [('pm', 'Product Manager', 'Dev'), ('manager-notes', 'Writer', 'Dev')])
        self.assertEqual(self.overview()['managers'], {'zeta': 'zeta:manager'})

    def test_a_lone_team_manager_is_the_project_manager_as_in_manager_chat(self):
        self.project('epsilon', [('marketing-manager', 'Manager', 'Marketing'), ('writer', 'Writer', 'Marketing')])
        self.assertEqual(self.overview()['managers'], {'epsilon': 'epsilon:marketing-manager'})

    def test_overview_agrees_with_the_resolver_for_every_project(self):
        self.project('alpha', [('manager', 'Manager', 'Dev'), ('dev', 'Developer', 'Dev')])
        self.project('beta', [('dev', 'Developer', 'Dev')])
        self.project('delta', [('marketing-manager', 'Manager', 'Marketing'), ('manager', 'Manager', 'Website')])
        self.project('gamma', [('manager', 'Manager', 'Dev'), ('manager2', 'Manager', 'Dev')])
        overview = self.overview()
        for project in self.projects:
            resolved = registry.manager_from_overview(overview, project['id'])
            expected = {project['id']: resolved['bot_key']} if resolved['status'] == 'ok' else {}
            self.assertEqual({k: v for k, v in overview['managers'].items() if k == project['id']}, expected, project['id'])

    def test_no_registry_gives_no_managers(self):
        registry.clear_overview_cache()
        self.assertEqual(registry.build_overview(str(self.root / 'missing.json')).get('managers', {}), {})


if __name__ == '__main__':
    unittest.main()

