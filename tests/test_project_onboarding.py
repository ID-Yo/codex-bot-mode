import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.team_room import project_onboarding as onboarding


class ProjectOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Path(self.temp.name) / 'host'
        self.host.mkdir()
        self.project = Path(self.temp.name) / 'new'
        (self.project / '.bot-mode/local').mkdir(parents=True)
        self.roster = self.project / '.bot-mode/roster.json'
        self.roster.write_text(json.dumps({'schema': 1, 'project': {'name': 'Existing'},
            'teams': [{'name': 'Development'}], 'participants': [{
                'bot': 'manager', 'role': 'Manager', 'team': 'Development', 'status': 'active',
                'thread_id': '00000000-0000-0000-0000-000000000001'}]}), encoding='utf-8')
        self.payload = dict(name='Existing', folder=str(self.project), kind='existing',
                            repository='example/project', show_dashboard=True)

    def test_preparation_is_readonly_and_confirmation_registers_idempotently(self):
        plan = onboarding.prepare(self.host, self.payload)
        self.assertEqual(plan['state'], 'ready_to_connect')
        self.assertFalse((self.host / '.bot-mode').exists())
        result = onboarding.confirm(self.host, self.payload, plan['version'], confirmed=True)
        self.assertEqual(result['state'], 'connected')
        file = self.host / '.bot-mode/local/office-project-roots.json'
        projects = json.loads(file.read_text())['projects']
        self.assertTrue(any(p['root'] == str(self.project.resolve()) for p in projects))
        self.assertTrue(any(p['id'] == 'current' and p['root'] == str(self.host.resolve()) for p in projects))
        again = onboarding.prepare(self.host, self.payload)
        repeated = onboarding.confirm(self.host, self.payload, again['version'], confirmed=True)
        self.assertTrue(repeated['already_connected'])
        self.assertEqual(len(json.loads(file.read_text())['projects']), 2)

    def test_confirmation_and_stale_plan_are_required(self):
        plan = onboarding.prepare(self.host, self.payload)
        with self.assertRaises(ValueError):
            onboarding.confirm(self.host, self.payload, plan['version'], confirmed=False)
        self.roster.write_text(self.roster.read_text().replace('Existing', 'Changed'))
        with self.assertRaises(ValueError):
            onboarding.confirm(self.host, self.payload, plan['version'], confirmed=True)

    def test_new_project_in_missing_parent_returns_codex_handoff_and_never_registers(self):
        payload = {**self.payload, 'kind': 'new', 'folder': str(self.project / 'no-parent' / 'not-created')}
        plan = onboarding.prepare(self.host, payload)
        self.assertEqual(plan['state'], 'needs_codex_setup')
        self.assertFalse(Path(payload['folder']).exists())
        with self.assertRaises(ValueError):
            onboarding.confirm(self.host, payload, plan['version'], confirmed=True)

    def test_invalid_repo_relative_folder_and_unappointed_project_are_rejected(self):
        for extra in ({'repository': 'https://evil.example/repo'}, {'folder': '../relative'},
                      {'show_dashboard': 'yes'}, {'name': '  '}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                onboarding.prepare(self.host, {**self.payload, **extra})
        self.roster.unlink()
        plan = onboarding.prepare(self.host, self.payload)
        self.assertEqual(plan['state'], 'needs_codex_setup')

    def test_external_roster_junction_is_refused(self):
        from unittest.mock import patch
        with patch.object(onboarding, '_contained_file', side_effect=ValueError('Outside project')):
            with self.assertRaises(ValueError):
                onboarding.prepare(self.host, self.payload)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_local_registry_junction_is_refused_without_outside_writes(self):
        from unittest.mock import patch

        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        registry = outside / 'office-project-roots.json'
        original = {'schema': 1, 'projects': [], 'standalone': []}
        registry.write_text(json.dumps(original), encoding='utf-8')
        local = self.host / '.bot-mode/local'
        local.parent.mkdir(parents=True)
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(local), str(outside)],
                       check=True, capture_output=True, text=True)
        self.addCleanup(subprocess.run, ['cmd', '/c', 'rmdir', str(local)],
                        check=True, capture_output=True, text=True)
        with patch.dict(os.environ, {'CODEX_OFFICE_REGISTRY': ''}):
            with self.assertRaisesRegex(ValueError, 'escapes the host project'):
                onboarding.prepare(self.host, self.payload)
            self.assertEqual(json.loads(registry.read_text(encoding='utf-8')), original)
            self.assertFalse((outside / 'project-onboarding.lock').exists())

    def test_explicit_external_registry_remains_supported(self):
        from unittest.mock import patch

        external = Path(self.temp.name) / 'configured' / 'registry.json'
        external.parent.mkdir()
        external.write_text(json.dumps({'schema': 1, 'projects': [], 'standalone': []}), encoding='utf-8')
        with patch.dict(os.environ, {'CODEX_OFFICE_REGISTRY': str(external)}):
            plan = onboarding.prepare(self.host, self.payload)
            self.assertEqual(Path(plan['registry_path']), external.resolve())
            result = onboarding.confirm(self.host, self.payload, plan['version'], confirmed=True)
        self.assertEqual(result['state'], 'connected')
        self.assertTrue(any(p['root'] == str(self.project.resolve())
                            for p in json.loads(external.read_text(encoding='utf-8'))['projects']))

    def test_registry_destination_drift_under_lock_is_refused(self):
        from unittest.mock import patch

        external = Path(self.temp.name) / 'configured' / 'registry.json'
        external.parent.mkdir()
        external.write_text(json.dumps({'schema': 1, 'projects': [{
            'id': 'current', 'name': self.host.name, 'root': str(self.host.resolve())}],
            'standalone': []}), encoding='utf-8')
        plan = onboarding.prepare(self.host, self.payload)
        original_lock = onboarding.ProcessLock

        def change_destination(path):
            os.environ['CODEX_OFFICE_REGISTRY'] = str(external)
            return original_lock(path)

        with patch.dict(os.environ, {'CODEX_OFFICE_REGISTRY': ''}), \
                patch.object(onboarding, 'ProcessLock', side_effect=change_destination):
            with self.assertRaisesRegex(ValueError, 'Registry destination changed'):
                onboarding.confirm(self.host, self.payload, plan['version'], confirmed=True)
        self.assertEqual(json.loads(external.read_text(encoding='utf-8'))['projects'], [{
            'id': 'current', 'name': self.host.name, 'root': str(self.host.resolve())}])

    def test_registry_content_drift_requires_fresh_plan(self):
        local = self.host / '.bot-mode/local/office-project-roots.json'
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({'schema': 1, 'projects': [{
            'id': 'current', 'name': self.host.name, 'root': str(self.host.resolve())}],
            'standalone': []}), encoding='utf-8')
        plan = onboarding.prepare(self.host, self.payload)
        local.write_text(json.dumps({'schema': 1, 'projects': [{
            'id': 'current', 'name': self.host.name, 'root': str(self.host.resolve())}],
            'standalone': [{'id': 'other'}]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'registry changed'):
            onboarding.confirm(self.host, self.payload, plan['version'], confirmed=True)
