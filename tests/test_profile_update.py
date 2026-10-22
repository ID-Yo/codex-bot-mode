"""Synthetic signed-release tests; no live project or publisher keys are used."""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timezone
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from scripts import profile_update as updater
from scripts import component_release as release


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
WORKSPACE = 'team/Development/manager'
RELEASE_ID = 'manager-profile'
INVENTORY_ID = 'profile:' + WORKSPACE
PROFILE_SOURCE = {
    'agent/PROFILE.md': b'# {{display_name}} / {{team}}\n',
    'agent/SOUL.md': b'Bot {{bot}} at {{host}}\n',
    'agent/PLAYBOOK.md': b'Installed {{date}}\n',
}


class ProfileUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cbm-154-')
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / 'demo-project'
        self.root.mkdir()
        self.workspace = self.root / WORKSPACE
        (self.workspace / 'agent').mkdir(parents=True)
        self.old = {
            'agent/PROFILE.md': b'# Local baseline profile\n',
            'agent/SOUL.md': b'Local baseline soul\n',
            'agent/BASE.md': b'Protected profile base\n',
        }
        for rel, body in self.old.items():
            target = self.workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        (self.workspace / 'agent/agent.yaml').write_text(
            'bot_id: manager\nrole: Manager\ninstalled_at: 2025-01-02\n'
            'parameters:\n  display_name:\n    value: Maya\n    source: owner\n', encoding='utf-8')
        self.roster = self.root / '.bot-mode/roster.json'
        self.roster.parent.mkdir(parents=True)
        self.roster.write_text(json.dumps({'schema': 1, 'project': {'name': 'demo-project'},
            'teams': [{'name': 'Development'}], 'participants': [{'bot': 'manager',
            'role': 'Manager', 'team': 'Development', 'workspace': WORKSPACE, 'status': 'active'}]}),
            encoding='utf-8')
        protected = {
            'agent/STATE.md': b'OWNER_STATE_SENTINEL',
            'agent/memory/lesson.json': b'PRIVATE_MEMORY_SENTINEL',
            '.bot-mode/local/bindings.json': b'CHAT_BINDING_SENTINEL',
            'team/approvals.yaml': b'OWNER_APPROVAL_SENTINEL',
            '.env': b'CREDENTIAL_SENTINEL',
        }
        for rel, body in protected.items():
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        metadata = self.workspace / 'agent/agent.yaml'
        owned = [f'{WORKSPACE}/{rel}' for rel in ('agent/PROFILE.md', 'agent/SOUL.md')]
        component = {'id': INVENTORY_ID, 'kind': 'profile', 'path': WORKSPACE,
            'source': 'local:catalog', 'source_revision': 'a' * 40, 'version': '1.0.0',
            'component_schema': 1, 'owned_paths': owned,
            'baseline': {rel: {'state': 'known', 'sha256': updater.digest((self.root / rel).read_bytes())}
                         for rel in owned}}
        self.inventory_file = self.root / '.bot-mode/local/installations.json'
        self.inventory_file.parent.mkdir(parents=True, exist_ok=True)
        self.inventory = {'schema': 1, 'components': [component]}
        self._save_inventory()
        self.protected = {self.root / rel: (self.root / rel).read_bytes() for rel in protected}
        self.protected[metadata] = metadata.read_bytes()
        self.protected[self.roster] = self.roster.read_bytes()
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.trust = {'schema': 1, 'keys': {'synthetic-test': base64.b64encode(public).decode()}, 'revoked': []}
        self.bundle_path = self.scratch / 'release.zip'
        self.envelope = self._release()
        self.targets = {'schema': 1, 'targets': {RELEASE_ID: INVENTORY_ID}}

    def tearDown(self):
        self.temp.cleanup()

    def _save_inventory(self):
        self.inventory_file.write_text(json.dumps(self.inventory, indent=2), encoding='utf-8')

    def _release(self, files=None):
        files = PROFILE_SOURCE if files is None else files
        body_specs = {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                      for name, data in files.items()}
        payload = {'schema': 1, 'release_id': 'synthetic-profile-release',
            'source_revision': 'b' * 40, 'published_at': '2026-01-01T00:00:00Z',
            'expires_at': '2027-01-01T00:00:00Z', 'minimum_engine': 1,
            'changelog': 'Synthetic profile update', 'components': [{
                'id': RELEASE_ID, 'kind': 'profile', 'version': '2.0.0', 'platforms': ['any'],
                'requires': {}, 'files': body_specs}]}
        self.envelope = {'payload': payload, 'key_id': 'synthetic-test',
                         'signature': base64.b64encode(self.key.sign(release.canonical(payload))).decode()}
        with zipfile.ZipFile(self.bundle_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(RELEASE_ID + '/' + name, data)
        return deepcopy(self.envelope)

    def plan(self, **kwargs):
        target_map = kwargs.pop('targets', self.targets)
        return updater.plan_update(self.root, self.envelope, self.bundle_path, self.trust,
            'windows-x86_64', target_map, [RELEASE_ID], installed={RELEASE_ID: '1.0.0'},
            now=NOW, **kwargs)

    def apply(self, plan, **kwargs):
        return updater.apply_plan(plan, plan['plan_sha256'], self.envelope, self.bundle_path,
            self.trust, 'windows-x86_64', self.targets,
            installed={RELEASE_ID: '1.0.0'}, now=NOW, **kwargs)

    def _legacy_profile_fixture(self, workspace, *, flat=False, rostered=True, metadata=True):
        folder = self.root if workspace == '.' else self.root / workspace
        profile_root = folder if flat else folder / 'agent'
        profile_root.mkdir(parents=True, exist_ok=True)
        metadata_path = profile_root / 'agent.yaml'
        if metadata:
            metadata_path.write_text(
                'bot_id: manager\nrole: Manager\ninstalled_at: 2025-01-02\n'
                'parameters:\n  display_name:\n    value: Maya\n    source: owner\n'
                '  team:\n    value: Development\n    source: owner\n', encoding='utf-8')
        files = {
            'agent/BASE.md': b'Base for {{bot}}\n',
            'agent/SOUL.md': b'Soul for {{display_name}}\n',
            'agent/PROFILE.md': b'# {{display_name}} / {{team}}\n',
            'agent/PLAYBOOK.md': b'Installed {{date}} at {{host}}\n',
        }
        owned = []
        baseline = {}
        if metadata:
            owned.append(metadata_path.relative_to(self.root).as_posix())
            baseline[owned[0]] = {'state': 'known', 'sha256': updater.digest(metadata_path.read_bytes())}
        for package_path, body in files.items():
            target = profile_root / package_path.removeprefix('agent/')
            target.write_bytes(b'legacy ' + package_path.encode('ascii') + b'\n')
            rel = target.relative_to(self.root).as_posix()
            owned.append(rel)
            baseline[rel] = {'state': 'unknown', 'reason': 'legacy-original-not-verified'}
        (profile_root / 'STATE.md').write_bytes(b'PROTECTED_STATE_SENTINEL')
        (profile_root / 'memory').mkdir(exist_ok=True)
        (profile_root / 'memory/lesson.json').write_bytes(b'PROTECTED_PROFILE_MEMORY')
        bot_memory = self.root / '.bot-mode/agents/project-manager/memory'
        bot_memory.mkdir(parents=True, exist_ok=True)
        (bot_memory / 'lesson.json').write_bytes(b'PROTECTED_MANAGER_MEMORY')
        state = profile_root / 'STATE.md'
        memory = profile_root / 'memory/lesson.json'
        protected = {state: state.read_bytes(), memory: memory.read_bytes(),
                     bot_memory / 'lesson.json': (bot_memory / 'lesson.json').read_bytes()}
        if metadata:
            protected[metadata_path] = metadata_path.read_bytes()
        if rostered:
            roster = json.loads(self.roster.read_text(encoding='utf-8'))
            roster['participants'][0]['workspace'] = workspace
            self.roster.write_text(json.dumps(roster), encoding='utf-8')
        elif self.roster.exists():
            self.roster.unlink()
        component = {'id': 'profile:' + workspace, 'kind': 'profile', 'path': workspace,
            'source': 'legacy:manager', 'source_revision': None, 'version': 2,
            'component_schema': 1, 'owned_paths': owned, 'baseline': baseline}
        self.inventory['components'] = [component]
        self._save_inventory()
        self._release(files)
        target_map = {'schema': 1, 'targets': {RELEASE_ID: component['id']}}
        return component, profile_root, files, protected, target_map

    def _legacy_plan(self, target_map):
        preview = updater.plan_update(self.root, self.envelope, self.bundle_path, self.trust,
            'windows-x86_64', target_map, [RELEASE_ID], now=NOW)
        candidate = preview['legacy_version_candidates'][0]
        baseline_conflicts = {conflict['path']: conflict for conflict in preview['conflicts']
                              if isinstance(conflict.get('path'), str)}
        self.assertEqual(set(baseline_conflicts), set(preview['selected_targets'][0]['files']))
        self.assertTrue(all(conflict['reason'] == 'unknown-baseline'
                            for conflict in baseline_conflicts.values()))
        version_map = {key: candidate[key] for key in ('inventory_sha256', 'component',
            'legacy_version', 'release_id', 'declared_version', 'projection_sha256')}
        resolutions = {conflict['path']: {'action': 'replace',
            'expected_sha256': conflict['current_sha256']}
            for conflict in preview['conflicts'] if isinstance(conflict.get('path'), str)}
        return updater.plan_update(self.root, self.envelope, self.bundle_path, self.trust,
            'windows-x86_64', target_map, [RELEASE_ID], now=NOW,
            legacy_version_resolutions={candidate['inventory_component']: version_map},
            resolutions=resolutions)

    def test_preview_renders_original_answers_and_apply_only_changes_selected_files(self):
        before_inventory = self.inventory_file.read_bytes()
        plan = self.plan()
        self.assertEqual(plan['conflicts'], [])
        profile_op = next(op for op in plan['operations'] if op['path'].endswith('agent/PROFILE.md'))
        self.assertIn('# Maya / Development', profile_op['content'])
        self.assertEqual(self.inventory_file.read_bytes(), before_inventory)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        pending = result['pending_activation']
        self.assertEqual(pending['state'], 'pending_activation')
        self.assertFalse(pending['activated'])
        item = pending['targets'][0]
        self.assertEqual(item['path'], WORKSPACE)
        self.assertEqual(item['version'], '2.0.0')
        self.assertEqual(item['files'][f'{WORKSPACE}/agent/PROFILE.md'],
                         updater.digest((self.workspace / 'agent/PROFILE.md').read_bytes()))
        self.assertEqual(item['installed_sha256'], updater.digest(release_update_target_bytes(item['files'])))
        for path, body in self.protected.items():
            self.assertEqual(path.read_bytes(), body)
        self.assertEqual((self.workspace / 'agent/BASE.md').read_bytes(), self.old['agent/BASE.md'])
        saved = json.loads(self.inventory_file.read_text(encoding='utf-8'))['components'][0]
        self.assertEqual(saved['version'], '2.0.0')
        self.assertEqual(saved['baseline'][f'{WORKSPACE}/agent/PLAYBOOK.md']['state'], 'known')

    def test_unknown_legacy_baseline_is_a_manual_conflict_until_exact_resolution(self):
        component = self.inventory['components'][0]
        component['baseline'][f'{WORKSPACE}/agent/PROFILE.md'] = {
            'state': 'unknown', 'reason': 'legacy-original-not-verified'}
        self._save_inventory()
        plan = self.plan()
        conflict = next(c for c in plan['conflicts'] if c['path'].endswith('agent/PROFILE.md'))
        self.assertEqual(conflict['reason'], 'unknown-baseline')
        self.assertEqual((self.workspace / 'agent/PROFILE.md').read_bytes(), self.old['agent/PROFILE.md'])
        resolution = {conflict['path']: {'action': 'replace', 'expected_sha256': conflict['current_sha256']}}
        reviewed = self.plan(resolutions=resolution)
        self.assertEqual(reviewed['conflicts'], [])
        self.assertEqual(reviewed['resolutions'], resolution)
        result = self.apply(reviewed)
        self.assertEqual(result['status'], 'applied')

    def test_legacy_integer_version_requires_explicit_mapping_and_keeps_unknown_baseline(self):
        component = self.inventory['components'][0]
        component['version'] = 2
        component['baseline'][f'{WORKSPACE}/agent/PROFILE.md'] = {
            'state': 'unknown', 'reason': 'legacy-original-not-verified'}
        self._save_inventory()
        before_inventory = self.inventory_file.read_bytes()
        before_profile = (self.workspace / 'agent/PROFILE.md').read_bytes()
        preview = self.plan()
        offered = preview['legacy_version_candidates'][0]
        self.assertEqual(offered['inventory_component'], INVENTORY_ID)
        self.assertEqual(offered['legacy_version'], 2)
        self.assertEqual(offered['declared_version'], '2.0.0')
        self.assertFalse(offered['historical_version_known'])
        self.assertEqual(self.inventory_file.read_bytes(), before_inventory)
        self.assertEqual((self.workspace / 'agent/PROFILE.md').read_bytes(), before_profile)
        mapping = {INVENTORY_ID: {key: offered[key] for key in (
            'inventory_sha256', 'component', 'legacy_version', 'release_id',
            'declared_version', 'projection_sha256')}}
        with self.assertRaisesRegex(updater.UpdateError, 'Legacy version review'):
            self.apply(preview)
        unresolved = self.plan(legacy_version_resolutions=mapping)
        self.assertEqual(unresolved['conflicts'][0]['reason'], 'unknown-baseline')
        self.assertEqual(unresolved['legacy_version_resolutions'], mapping)
        conflict_preview = self.plan(legacy_version_resolutions=mapping, resolutions={
            f'{WORKSPACE}/agent/PROFILE.md': {'action': 'replace',
                'expected_sha256': updater.digest(self.old['agent/PROFILE.md'])}})
        self.assertEqual(conflict_preview['conflicts'], [])
        self.assertEqual(conflict_preview['legacy_version_resolutions'], mapping)
        self.assertFalse(conflict_preview['legacy_version_candidates'][0]['historical_version_known'])

        result = self.apply(conflict_preview)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['pending_activation']['targets'][0]['version'], '2.0.0')
        saved = json.loads(self.inventory_file.read_text(encoding='utf-8'))['components'][0]
        self.assertEqual(saved['version'], '2.0.0')
        self.assertEqual(saved['baseline'][f'{WORKSPACE}/agent/PROFILE.md']['state'], 'known')

    def test_reference_integer_version_one_needs_the_same_explicit_mapping(self):
        ref_root = 'team/references/editorial'
        rel = f'{ref_root}/methods/guide.md'
        old = b'legacy original handbook\n'
        (self.root / rel).parent.mkdir(parents=True)
        (self.root / rel).write_bytes(old)
        reference = {'id': 'reference:editorial', 'kind': 'shared_reference', 'path': ref_root,
            'source': 'legacy:catalog', 'source_revision': None, 'version': 1,
            'component_schema': 1, 'owned_paths': [rel],
            'baseline': {rel: {'state': 'unknown', 'reason': 'legacy-original-not-verified'}}}
        self.inventory['components'].append(reference)
        self._save_inventory()
        body = b'verified release handbook\n'
        self._release_reference(body)
        targets = {'schema': 1, 'targets': {'editorial-handbook': 'reference:editorial'}}
        call = lambda **kwargs: updater.plan_update(self.root, self.envelope, self.bundle_path,
            self.trust, 'windows-x86_64', targets, ['editorial-handbook'], now=NOW, **kwargs)
        preview = call()
        candidate = preview['legacy_version_candidates'][0]
        self.assertEqual(candidate['inventory_component'], 'reference:editorial')
        self.assertEqual(candidate['legacy_version'], 1)
        self.assertFalse(candidate['historical_version_known'])
        mapping = {'reference:editorial': {key: candidate[key] for key in (
            'inventory_sha256', 'component', 'legacy_version', 'release_id',
            'declared_version', 'projection_sha256')}}
        reviewed = call(legacy_version_resolutions=mapping, resolutions={
            rel: {'action': 'replace', 'expected_sha256': updater.digest(old)}})
        self.assertEqual(reviewed['conflicts'], [])
        result = updater.apply_plan(reviewed, reviewed['plan_sha256'], self.envelope,
            self.bundle_path, self.trust, 'windows-x86_64', targets, now=NOW)
        self.assertEqual(result['status'], 'applied')
        saved = next(c for c in json.loads(self.inventory_file.read_text(encoding='utf-8'))['components']
                     if c['id'] == 'reference:editorial')
        self.assertEqual(saved['version'], candidate['declared_version'])
        self.assertEqual(saved['baseline'][rel]['state'], 'known')
        self.assertEqual((self.root / rel).read_bytes(), body)

    def test_legacy_version_resolution_is_bound_to_projection_and_inventory(self):
        self.inventory['components'][0]['version'] = 2
        self._save_inventory()
        preview = self.plan(legacy_version_resolutions={})
        candidate = preview['legacy_version_candidates'][0]
        mapping = {INVENTORY_ID: {key: candidate[key] for key in (
            'inventory_sha256', 'component', 'legacy_version', 'release_id',
            'declared_version', 'projection_sha256')}}
        reviewed = self.plan(legacy_version_resolutions=mapping)
        self.assertEqual(reviewed['conflicts'], [])

        edited = self.workspace / 'agent/PROFILE.md'
        edited.write_bytes(b'changed after legacy mapping preview\n')
        with self.assertRaisesRegex(updater.UpdateError, 'stale'):
            self.apply(reviewed)
        self.assertEqual(edited.read_bytes(), b'changed after legacy mapping preview\n')

        edited.write_bytes(self.old['agent/PROFILE.md'])
        component = self.inventory['components'][0]
        component['version'] = 3
        self._save_inventory()
        with self.assertRaisesRegex(updater.UpdateError, 'inventory|stale'):
            self.plan(legacy_version_resolutions=mapping)
        self.assertEqual(component['version'], 3)
        self.assertEqual(edited.read_bytes(), self.old['agent/PROFILE.md'])

    def test_untrusted_envelope_component_id_is_verified_before_hint_mapping(self):
        self.inventory['components'][0]['version'] = 2
        self._save_inventory()
        before_inventory = self.inventory_file.read_bytes()
        malformed = deepcopy(self.envelope)
        malformed['payload']['components'][0]['id'] = []
        with self.assertRaisesRegex(updater.release.ReleaseError, 'signature'):
            updater.plan_update(self.root, malformed, self.bundle_path, self.trust,
                'windows-x86_64', self.targets, [RELEASE_ID], now=NOW)
        self.assertEqual(self.inventory_file.read_bytes(), before_inventory)

        malformed['signature'] = base64.b64encode(
            self.key.sign(release.canonical(malformed['payload']))).decode()
        with self.assertRaisesRegex(updater.release.ReleaseError, 'component identity'):
            updater.plan_update(self.root, malformed, self.bundle_path, self.trust,
                'windows-x86_64', self.targets, [RELEASE_ID], now=NOW)
        self.assertEqual(self.inventory_file.read_bytes(), before_inventory)

    def test_semver_inventory_still_rejects_a_signed_downgrade(self):
        self.inventory['components'][0]['version'] = '3.0.0'
        self._save_inventory()
        with self.assertRaisesRegex(updater.release.ReleaseError, 'Downgrade'):
            self.plan()

    def test_cli_accepts_legacy_resolution_file_for_plan_and_apply(self):
        self.inventory['components'][0]['version'] = 2
        self._save_inventory()
        manifest_path = self.scratch / 'manifest.json'
        trust_path = self.scratch / 'trust.json'
        targets_path = self.scratch / 'targets.json'
        resolution_path = self.scratch / 'legacy-version-resolutions.json'
        plan_path = self.scratch / 'reviewed-plan.json'
        manifest_path.write_text(json.dumps(self.envelope), encoding='utf-8')
        trust_path.write_text(json.dumps(self.trust), encoding='utf-8')
        targets_path.write_text(json.dumps(self.targets), encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(updater.main(['plan', '--project', str(self.root), '--manifest',
                str(manifest_path), '--bundle', str(self.bundle_path), '--trust', str(trust_path),
                '--targets', str(targets_path), '--platform', 'windows-x86_64',
                '--component', RELEASE_ID]), 0)
        candidate = json.loads(output.getvalue())['legacy_version_candidates'][0]
        mapping = {INVENTORY_ID: {key: candidate[key] for key in (
            'inventory_sha256', 'component', 'legacy_version', 'release_id',
            'declared_version', 'projection_sha256')}}
        resolution_path.write_text(json.dumps(mapping), encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(updater.main(['plan', '--project', str(self.root), '--manifest',
                str(manifest_path), '--bundle', str(self.bundle_path), '--trust', str(trust_path),
                '--targets', str(targets_path), '--platform', 'windows-x86_64',
                '--component', RELEASE_ID, '--legacy-version-resolutions', str(resolution_path)]), 0)
        reviewed = json.loads(output.getvalue())
        plan_path.write_text(json.dumps(reviewed), encoding='utf-8')
        self.assertIn(mapping, [reviewed['legacy_version_resolutions']])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(updater.main(['apply', '--plan', str(plan_path), '--approve',
                reviewed['plan_sha256'], '--manifest', str(manifest_path), '--bundle',
                str(self.bundle_path), '--trust', str(trust_path), '--targets', str(targets_path),
                '--platform', 'windows-x86_64']), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'applied')

    def test_local_change_after_preview_makes_plan_stale(self):
        plan = self.plan()
        target = self.workspace / 'agent/PROFILE.md'
        target.write_bytes(b'Owner edit after preview\n')
        with self.assertRaisesRegex(updater.UpdateError, 'stale'):
            self.apply(plan)
        self.assertEqual(target.read_bytes(), b'Owner edit after preview\n')

    def test_failpoint_mutation_of_submitted_plan_cannot_change_applied_bytes(self):
        plan = self.plan()
        approved_hash = plan['plan_sha256']
        def mutate(stage):
            if stage == 'journal_created':
                operation = plan['operations'][0]
                operation['content'] = 'SYNTHETIC UNAPPROVED CONTENT\n'
                operation['after_sha256'] = updater.digest(operation['content'].encode())
        result = self.apply(plan, failpoint=mutate)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['plan_sha256'], approved_hash)
        for relative, expected in result['pending_activation']['targets'][0]['files'].items():
            actual = (self.root / relative).read_bytes()
            self.assertEqual(updater.digest(actual), expected)
            self.assertNotIn(b'SYNTHETIC UNAPPROVED CONTENT', actual)

    def test_profile_deletion_cannot_target_inventory_owned_identity_metadata(self):
        component = self.inventory['components'][0]
        identity_rel = f'{WORKSPACE}/agent/agent.yaml'
        identity_file = self.root / identity_rel
        component['owned_paths'].append(identity_rel)
        component['baseline'][identity_rel] = {
            'state': 'known', 'sha256': updater.digest(identity_file.read_bytes())}
        self._save_inventory()
        before_inventory = self.inventory_file.read_bytes()
        with self.assertRaisesRegex(updater.UpdateError, 'Deletion path is outside the profile update allowlist'):
            self.plan(delete_paths=[identity_rel])
        self.assertEqual(identity_file.read_bytes(), self.protected[identity_file])
        self.assertEqual(self.inventory_file.read_bytes(), before_inventory)
        ordinary_plan = self.plan()
        self.assertNotIn(identity_rel, ordinary_plan['delete_candidates'])

    def test_same_release_second_application_is_a_no_op(self):
        self.apply(self.plan())
        second = self.plan()
        self.assertEqual(second['operations'], [])
        result = self.apply(second)
        self.assertEqual(result['status'], 'no_op')
        self.assertIsNone(result['transaction'])
        self.assertEqual(result['pending_activation']['targets'][0]['installed_sha256'],
                         updater.digest(release_update_target_bytes(result['pending_activation']['targets'][0]['files'])))

    def _assert_fault_recovers_to_complete_old_or_new_state(self, stage):
        original = {rel: (self.root / rel).read_bytes() for rel in self.inventory['components'][0]['owned_paths']}
        plan = self.plan()
        def stop(at):
            if at == stage:
                raise updater.SimulatedCrash(stage)
        with self.assertRaises(updater.SimulatedCrash):
            self.apply(plan, failpoint=stop)
        transaction = next(p for p in (self.root / '.bot-mode/local/profile-updates').iterdir()
                          if p.is_dir() and (p / 'journal.json').is_file()
                          and json.loads((p / 'journal.json').read_text())['plan_sha256'] == plan['plan_sha256'])
        recovered = updater.recover(self.root, transaction.name)
        if stage == 'metadata_commit':
            self.assertEqual(recovered['status'], 'applied')
            self.assertEqual(recovered['pending_activation']['state'], 'pending_activation')
            for rel, body in PROFILE_SOURCE.items():
                target = self.workspace / rel
                self.assertEqual(target.read_bytes(), body.replace(b'{{display_name}}', b'Maya')
                    .replace(b'{{team}}', b'Development').replace(b'{{bot}}', b'manager')
                    .replace(b'{{host}}', b'../../..').replace(b'{{date}}', b'2025-01-02'))
        else:
            self.assertEqual(recovered['status'], 'recovered_rollback')
            for rel, body in original.items():
                self.assertEqual((self.root / rel).read_bytes(), body)
        self.assertEqual((self.workspace / 'agent/BASE.md').read_bytes(), self.old['agent/BASE.md'])

    def test_fault_after_journal_creation_recovers(self):
        self._assert_fault_recovers_to_complete_old_or_new_state('journal_created')

    def test_fault_after_first_backup_recovers(self):
        self._assert_fault_recovers_to_complete_old_or_new_state('first_backup')

    def test_fault_after_staging_recovers(self):
        self._assert_fault_recovers_to_complete_old_or_new_state('staged')

    def test_fault_after_backup_phase_recovers(self):
        self._assert_fault_recovers_to_complete_old_or_new_state('backup_complete')

    def test_fault_after_first_target_write_recovers(self):
        self._assert_fault_recovers_to_complete_old_or_new_state('first_write')

    def test_fault_after_inventory_commit_finishes_applied(self):
        self._assert_fault_recovers_to_complete_old_or_new_state('metadata_commit')

    def test_concurrent_lock_and_low_disk_fail_before_any_project_write(self):
        before = {p: p.read_bytes() for p in self.protected}
        lock = self.root / '.bot-mode/local/profile-updates.lock'
        lock.write_text('other writer', encoding='utf-8')
        with self.assertRaisesRegex(updater.UpdateError, 'Another profile update'):
            self.apply(self.plan())
        lock.unlink()
        with mock.patch.object(updater.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(updater.UpdateError, 'Insufficient free space'):
                self.apply(self.plan())
        self.assertEqual(before, {p: p.read_bytes() for p in self.protected})
        self.assertEqual((self.workspace / 'agent/PROFILE.md').read_bytes(), self.old['agent/PROFILE.md'])

    def test_inventory_writer_exclusive_lock_is_never_reclaimed_without_owner_metadata(self):
        inventory_lock = self.inventory_file.with_suffix('.lock')
        inventory_lock.write_bytes(b'')
        with self.assertRaisesRegex(updater.UpdateError, 'Another profile update or inventory write'):
            with updater._locks(self.root, self.inventory_file):
                self.fail('updater entered while the schema-1 inventory writer lock exists')
        self.assertEqual(inventory_lock.read_bytes(), b'')

    def test_abrupt_child_termination_releases_locks_for_recovery(self):
        ready = self.scratch / 'lock-holder-ready'
        project_root = Path(updater.__file__).resolve().parents[1]
        child_code = (
            'import sys,time; from pathlib import Path; '
            'sys.path.insert(0,sys.argv[1]); from scripts import profile_update as u; '
            'root=Path(sys.argv[2]); manager=u._locks(root,root/".bot-mode/local/installations.json"); '
            'manager.__enter__(); Path(sys.argv[3]).write_text("ready",encoding="ascii"); time.sleep(120)')
        env = os.environ.copy()
        child = subprocess.Popen([sys.executable, '-B', '-c', child_code,
            str(project_root), str(self.root), str(ready)], stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, env=env)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists() and child.poll() is not None:
                _, stderr = child.communicate(timeout=10)
                self.fail(f'child exited before acquiring locks: {stderr}')
            self.assertTrue(ready.exists(), 'child did not acquire locks')
            child.kill()
            child.communicate(timeout=10)
            with updater._locks(self.root, self.inventory_file):
                self.assertTrue((self.root / '.bot-mode/local/profile-updates.lock').is_file())
                self.assertTrue(self.inventory_file.with_suffix('.lock').is_file())
            self.assertFalse((self.root / '.bot-mode/local/profile-updates.lock').exists())
            self.assertFalse(self.inventory_file.with_suffix('.lock').exists())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_rollback_refuses_new_user_edits_without_partial_rollback(self):
        result = self.apply(self.plan())
        edited = self.workspace / 'agent/PROFILE.md'
        edited.write_bytes(b'New owner work\n')
        soul_after = (self.workspace / 'agent/SOUL.md').read_bytes()
        with self.assertRaisesRegex(updater.UpdateError, 'changed after apply'):
            updater.rollback(self.root, result['transaction'])
        self.assertEqual(edited.read_bytes(), b'New owner work\n')
        self.assertEqual((self.workspace / 'agent/SOUL.md').read_bytes(), soul_after)

    def test_tampered_backup_is_detected_before_rollback_changes_targets(self):
        result = self.apply(self.plan())
        journal = json.loads((self.root / '.bot-mode/local/profile-updates' / result['transaction'] / 'journal.json').read_text())
        backed_up = next(entry for entry in journal['operations'] if entry['backup'] is not None)
        backup = self.root / '.bot-mode/local/profile-updates' / result['transaction'] / backed_up['backup']
        backup.write_bytes(b'tampered backup')
        after = {(self.root / op['path']).read_bytes() for op in journal['operations']}
        with self.assertRaisesRegex(updater.UpdateError, 'backup integrity'):
            updater.rollback(self.root, result['transaction'])
        self.assertEqual(after, {(self.root / op['path']).read_bytes() for op in journal['operations']})

    def test_linked_target_path_is_rejected(self):
        target = self.workspace / 'agent/PROFILE.md'
        outside = self.scratch / 'outside.md'
        outside.write_bytes(b'OUTSIDE_SENTINEL')
        original = target.read_bytes()
        target.unlink()
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            target.write_bytes(original)
            self.skipTest('File symlinks are unavailable on this host')
        with self.assertRaisesRegex(updater.UpdateError, 'Linked target path'):
            self.plan()
        self.assertEqual(outside.read_bytes(), b'OUTSIDE_SENTINEL')

    def test_verified_profile_with_unresolved_placeholder_fails_in_plan(self):
        self._release({'agent/PROFILE.md': b'{{unknown_value}}\n'})
        with self.assertRaisesRegex(updater.UpdateError, 'Unresolved template placeholder'):
            self.plan()

    def test_reference_target_uses_inventory_root_and_previews_dependents(self):
        ref_root = 'team/references/editorial'
        folder = self.root / ref_root / 'methods'
        folder.mkdir(parents=True)
        old_rel = f'{ref_root}/methods/guide.md'
        (self.root / old_rel).write_bytes(b'old trusted guidance\n')
        body = b'new trusted guidance\n'
        ref = {'id': 'reference:editorial', 'kind': 'shared_reference', 'path': ref_root,
            'source': 'local:catalog', 'source_revision': 'a' * 40, 'version': '1.0.0',
            'component_schema': 1, 'dependents': ['Manager'], 'owned_paths': [old_rel],
            'baseline': {old_rel: {'state': 'known', 'sha256': updater.digest((self.root / old_rel).read_bytes())}}}
        self.inventory['components'].append(ref)
        self._save_inventory()
        dependent_rel = f'{WORKSPACE}/agent/PROFILE.md'
        dependent_body = b'No embedded reference path.\n'
        (self.root / dependent_rel).write_bytes(dependent_body)
        target = {'schema': 1, 'targets': {'editorial-handbook': 'reference:editorial'}}
        self._release_reference(body)
        plan = updater.plan_update(self.root, self.envelope, self.bundle_path, self.trust,
            'windows-x86_64', target, ['editorial-handbook'], installed={'editorial-handbook': '1.0.0'}, now=NOW)
        self.assertEqual(plan['conflicts'], [])
        self.assertEqual(plan['operations'][0]['path'], old_rel)
        self.assertEqual(plan['delete_candidates'], [])
        dependent_preview = plan['selected_targets'][0]['dependents']
        self.assertEqual(dependent_preview['inventory_roles'], ['Manager'])
        self.assertEqual(dependent_preview['coverage'], 'known')
        dependent = dependent_preview['verified_roster_profiles'][0]
        self.assertEqual(dependent['inventory_component'], INVENTORY_ID)
        self.assertEqual(dependent['roster_id'], 'manager')
        self.assertEqual(dependent['workspace'], WORKSPACE)
        result = updater.apply_plan(plan, plan['plan_sha256'], self.envelope, self.bundle_path,
            self.trust, 'windows-x86_64', target, installed={'editorial-handbook': '1.0.0'}, now=NOW)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual((self.root / old_rel).read_bytes(), body)
        self.assertEqual((self.root / dependent_rel).read_bytes(), dependent_body)
        saved_reference = next(c for c in json.loads(self.inventory_file.read_text(encoding='utf-8'))['components']
                               if c['id'] == 'reference:editorial')
        self.assertEqual(saved_reference['dependents'], ['Manager'])

    def test_reference_deletion_must_stay_in_verified_package_namespace(self):
        ref_root = 'team/references/editorial'
        valid_rel = f'{ref_root}/methods/guide.md'
        protected_rel = f'{ref_root}/.bot-mode/owner.yaml'
        (self.root / valid_rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / valid_rel).write_bytes(b'old guide\n')
        protected = self.root / protected_rel
        protected.parent.mkdir(parents=True, exist_ok=True)
        protected.write_bytes(b'REFERENCE_PROTECTED_SENTINEL')
        ref = {'id': 'reference:editorial', 'kind': 'shared_reference', 'path': ref_root,
            'source': 'local:catalog', 'source_revision': 'a' * 40, 'version': '1.0.0',
            'component_schema': 1, 'owned_paths': [valid_rel, protected_rel],
            'baseline': {valid_rel: {'state': 'known', 'sha256': updater.digest((self.root / valid_rel).read_bytes())},
                         protected_rel: {'state': 'known', 'sha256': updater.digest(protected.read_bytes())}}}
        self.inventory['components'].append(ref)
        self._save_inventory()
        self._release_reference(b'new guide\n')
        targets = {'schema': 1, 'targets': {'editorial-handbook': 'reference:editorial'}}
        before_inventory = self.inventory_file.read_bytes()
        with self.assertRaisesRegex(updater.UpdateError, 'Deletion path is outside the verified reference namespace'):
            updater.plan_update(self.root, self.envelope, self.bundle_path, self.trust,
                'windows-x86_64', targets, ['editorial-handbook'], now=NOW,
                delete_paths=[protected_rel])
        self.assertEqual(protected.read_bytes(), b'REFERENCE_PROTECTED_SENTINEL')
        self.assertEqual(self.inventory_file.read_bytes(), before_inventory)

    def test_unresolved_inventory_dependent_role_is_reported_as_unknown(self):
        ref_root = 'team/references/editorial'
        ref = {'id': 'reference:editorial', 'kind': 'shared_reference', 'path': ref_root,
            'source': 'local:catalog', 'source_revision': 'a' * 40, 'version': '1.0.0',
            'component_schema': 1, 'dependents': ['Former Manager'], 'owned_paths': [], 'baseline': {}}
        self.inventory['components'].append(ref)
        self._save_inventory()
        body = b'updated handbook\n'
        self._release_reference(body)
        targets = {'schema': 1, 'targets': {'editorial-handbook': 'reference:editorial'}}
        plan = updater.plan_update(self.root, self.envelope, self.bundle_path, self.trust,
            'windows-x86_64', targets, ['editorial-handbook'], now=NOW)
        dependents = plan['selected_targets'][0]['dependents']
        self.assertEqual(dependents['coverage'], 'unknown')
        self.assertIn({'role': 'Former Manager',
                       'reason': 'no-active-verified-roster-profile'}, dependents['unknown'])

    def test_installed_dependency_versions_are_mapped_by_release_component_id(self):
        ref_root = 'team/references/editorial'
        ref = {'id': 'reference:editorial', 'kind': 'shared_reference', 'path': ref_root,
            'source': 'local:catalog', 'source_revision': 'a' * 40, 'version': '1.0.0',
            'component_schema': 1, 'owned_paths': [], 'baseline': {}}
        self.inventory['components'].append(ref)
        self._save_inventory()
        self._release()
        self.envelope['payload']['components'][0]['requires'] = {'editorial-handbook': '1.0.0'}
        self.envelope['signature'] = base64.b64encode(
            self.key.sign(release.canonical(self.envelope['payload']))).decode()
        targets = {'schema': 1, 'targets': {RELEASE_ID: INVENTORY_ID,
                                            'editorial-handbook': 'reference:editorial'}}
        plan = updater.plan_update(self.root, self.envelope, self.bundle_path, self.trust,
            'windows-x86_64', targets, [RELEASE_ID], now=NOW)
        self.assertEqual(plan['conflicts'], [])

    def test_unknown_installed_dependency_version_does_not_use_caller_value(self):
        ref = {'id': 'reference:editorial', 'kind': 'shared_reference',
            'path': 'team/references/editorial', 'source': 'legacy', 'source_revision': None,
            'version': 'not-a-version', 'component_schema': 1, 'owned_paths': [], 'baseline': {}}
        self.inventory['components'].append(ref)
        self._save_inventory()
        self._release()
        self.envelope['payload']['components'][0]['requires'] = {'editorial-handbook': '1.0.0'}
        self.envelope['signature'] = base64.b64encode(
            self.key.sign(release.canonical(self.envelope['payload']))).decode()
        targets = {'schema': 1, 'targets': {RELEASE_ID: INVENTORY_ID,
                                            'editorial-handbook': 'reference:editorial'}}
        with self.assertRaisesRegex(updater.release.ReleaseError, 'Missing or incompatible'):
            updater.plan_update(self.root, self.envelope, self.bundle_path, self.trust,
                'windows-x86_64', targets, [RELEASE_ID],
                installed={'editorial-handbook': '1.0.0'}, now=NOW)

    def test_inventory_change_after_preview_fails_compare_and_swap(self):
        plan = self.plan()
        self.inventory_file.write_bytes(json.dumps(self.inventory, separators=(',', ':')).encode())
        original = (self.workspace / 'agent/PROFILE.md').read_bytes()
        with self.assertRaisesRegex(updater.UpdateError, 'stale|inventory changed'):
            self.apply(plan)
        self.assertEqual((self.workspace / 'agent/PROFILE.md').read_bytes(), original)

    def test_inventory_cas_change_after_staging_aborts_before_first_target_write(self):
        plan = self.plan()
        target_before = {op['path']: (self.root / op['path']).read_bytes()
                         for op in plan['operations'] if op['before_sha256'] is not None}
        external_inventory = []
        def competing_inventory_write(stage):
            if stage == 'backup_complete':
                changed = json.dumps(self.inventory, separators=(',', ':')).encode()
                self.inventory_file.write_bytes(changed)
                external_inventory.append(changed)
        with self.assertRaisesRegex(updater.InventoryCASMismatch, 'compare-and-swap'):
            self.apply(plan, failpoint=competing_inventory_write)
        self.assertEqual(self.inventory_file.read_bytes(), external_inventory[0])
        for rel, body in target_before.items():
            self.assertEqual((self.root / rel).read_bytes(), body)
        transaction = next((self.root / '.bot-mode/local/profile-updates').iterdir())
        self.assertEqual(updater.recover(self.root, transaction.name)['status'], 'already_rolled_back')

    def test_rostered_root_export_updates_only_canonical_profile_and_preserves_private_files(self):
        component, profile_root, files, protected, targets = self._legacy_profile_fixture('.')
        metadata_path = profile_root / 'agent.yaml'
        metadata_path.write_text(metadata_path.read_text(encoding='utf-8').replace(
            'bot_id: manager', 'bot_id: project-manager'), encoding='utf-8')
        roster = json.loads(self.roster.read_text(encoding='utf-8'))
        roster['participants'][0]['bot'] = 'project-manager'
        self.roster.write_text(json.dumps(roster), encoding='utf-8')
        component['baseline']['agent/agent.yaml']['sha256'] = updater.digest(metadata_path.read_bytes())
        self._save_inventory()
        protected[metadata_path] = metadata_path.read_bytes()
        plan = self._legacy_plan(targets)
        expected_paths = {f'agent/{path.removeprefix("agent/")}' for path in files}
        self.assertEqual({op['path'] for op in plan['operations']}, expected_paths)
        self.assertEqual(plan['conflicts'], [])
        result = updater.apply_plan(plan, plan['plan_sha256'], self.envelope, self.bundle_path,
            self.trust, 'windows-x86_64', targets, now=NOW)
        self.assertEqual(result['status'], 'applied')
        self.assertFalse(result['pending_activation']['activated'])
        self.assertEqual(result['pending_activation']['targets'][0]['path'], 'agent')
        for path in expected_paths:
            self.assertTrue((self.root / path).is_file())
        self.assertEqual((profile_root / 'BASE.md').read_bytes(), b'Base for project-manager\n')
        for path, body in protected.items():
            self.assertEqual(path.read_bytes(), body)
        saved = json.loads(self.inventory_file.read_text(encoding='utf-8'))['components'][0]
        self.assertEqual(saved['version'], '2.0.0')
        self.assertEqual(saved['baseline']['agent/agent.yaml']['sha256'],
                         updater.digest(protected[profile_root / 'agent.yaml']))

    def test_flat_legacy_manager_update_recovers_without_touching_identity_or_memory(self):
        component, profile_root, files, protected, targets = self._legacy_profile_fixture(
            'agents/project-manager', flat=True, rostered=False, metadata=False)
        files = {'agent/PROFILE.md': b'{{bot}} {{role}} {{project}} {{host}}\n'}
        self._release(files)
        plan = self._legacy_plan(targets)
        expected_paths = {f'agents/project-manager/{path.removeprefix("agent/")}' for path in files}
        self.assertEqual({op['path'] for op in plan['operations']}, expected_paths)
        self.assertEqual(plan['operations'][0]['content'], 'project-manager Manager demo-project ../..\n')
        self.assertNotIn('agents/project-manager/agent.yaml', plan['delete_candidates'])

        def crash(stage):
            if stage == 'first_write':
                raise updater.SimulatedCrash(stage)

        with self.assertRaises(updater.SimulatedCrash):
            updater.apply_plan(plan, plan['plan_sha256'], self.envelope, self.bundle_path,
                self.trust, 'windows-x86_64', targets, now=NOW, failpoint=crash)
        transaction = next(path for path in (self.root / '.bot-mode/local/profile-updates').iterdir()
            if path.is_dir() and (path / 'journal.json').is_file()
            and json.loads((path / 'journal.json').read_text())['plan_sha256'] == plan['plan_sha256'])
        recovered = updater.recover(self.root, transaction.name)
        self.assertEqual(recovered['status'], 'recovered_rollback')
        for path, body in protected.items():
            self.assertEqual(path.read_bytes(), body)
        for package_path in files:
            target = profile_root / package_path.removeprefix('agent/')
            self.assertTrue(target.read_bytes().startswith(b'legacy '))
        self.assertEqual(self.inventory_file.read_text(encoding='utf-8'),
                         json.dumps(self.inventory, indent=2))

    def test_root_export_can_use_active_roster_identity_without_agent_yaml(self):
        _, profile_root, _, protected, targets = self._legacy_profile_fixture('.', metadata=False)
        roster = json.loads(self.roster.read_text(encoding='utf-8'))
        roster['participants'][0]['bot_id'] = 'project-manager'
        self.roster.write_text(json.dumps(roster), encoding='utf-8')
        files = {'agent/PROFILE.md': b'{{bot}}/{{role}}/{{project}}/{{team}}\n'}
        self._release(files)
        plan = self._legacy_plan(targets)
        self.assertEqual({op['path'] for op in plan['operations']}, {'agent/PROFILE.md'})
        result = updater.apply_plan(plan, plan['plan_sha256'], self.envelope, self.bundle_path,
            self.trust, 'windows-x86_64', targets, now=NOW)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual((profile_root / 'PROFILE.md').read_bytes(),
                         b'project-manager/Manager/demo-project/Development\n')
        for path, body in protected.items():
            self.assertEqual(path.read_bytes(), body)

    def test_flat_legacy_layout_cannot_delete_owned_identity_or_state_paths(self):
        component, _, _, protected, targets = self._legacy_profile_fixture(
            'agents/project-manager', flat=True, rostered=False, metadata=False)
        state_path = 'agents/project-manager/STATE.md'
        component['owned_paths'].append(state_path)
        component['baseline'][state_path] = {
            'state': 'known', 'sha256': updater.digest(protected[self.root / state_path])}
        self._save_inventory()
        self._release({'agent/PROFILE.md': b'Updated safely\n'})
        before_inventory = self.inventory_file.read_bytes()
        with self.assertRaisesRegex(updater.UpdateError, 'outside the profile update allowlist'):
            updater.plan_update(self.root, self.envelope, self.bundle_path, self.trust,
                'windows-x86_64', targets, [RELEASE_ID], now=NOW, delete_paths=[state_path])
        self.assertEqual(self.inventory_file.read_bytes(), before_inventory)
        self.assertEqual((self.root / state_path).read_bytes(), protected[self.root / state_path])

    def test_legacy_adapters_reject_noncanonical_roots_and_identity_mismatch(self):
        component, _, _, _, _ = self._legacy_profile_fixture('agents/project-manager', flat=True)
        component['path'] = 'agents/other'
        component['id'] = 'profile:agents/other'
        for rel in list(component['owned_paths']):
            if rel.startswith('agents/project-manager/'):
                replacement = rel.replace('agents/project-manager/', 'agents/other/', 1)
                component['baseline'][replacement] = component['baseline'].pop(rel)
                component['owned_paths'][component['owned_paths'].index(rel)] = replacement
        self._save_inventory()
        targets = {'schema': 1, 'targets': {RELEASE_ID: component['id']}}
        with self.assertRaisesRegex(updater.UpdateError, 'unsupported|recognized|managed|missing'):
            self.plan(targets=targets)

        component, _, _, _, targets = self._legacy_profile_fixture('agents/project-manager', flat=True)
        (self.root / 'agents/project-manager/agent.yaml').write_text(
            'bot_id: manager\nrole: Developer\n', encoding='utf-8')
        before_inventory = self.inventory_file.read_bytes()
        before_targets = {rel: (self.root / rel).read_bytes() for rel in component['owned_paths']}
        with self.assertRaisesRegex(updater.UpdateError, 'identity|Manager|roster'):
            self.plan(targets=targets)
        self.assertEqual(self.inventory_file.read_bytes(), before_inventory)
        self.assertEqual({rel: (self.root / rel).read_bytes() for rel in component['owned_paths']},
                         before_targets)

        component, _, _, _, targets = self._legacy_profile_fixture(
            'agents/project-manager', flat=True, rostered=False, metadata=False)
        (self.root / 'agents/project-manager/agent.yaml').write_text(
            'bot_id: different-agent\nrole: Manager\n', encoding='utf-8')
        with self.assertRaisesRegex(updater.UpdateError, 'canonical profile_location bot ID'):
            self.plan(targets=targets)

    def test_single_file_tool_component_path_in_inventory_is_valid(self):
        tool_rel = '.bot-mode/roster.py'
        (self.root / tool_rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / tool_rel).write_text('# synthetic tool\n', encoding='utf-8')
        tool_component = {
            'id': 'tool:' + tool_rel,
            'kind': 'tool',
            'path': tool_rel,
            'source': None,
            'source_revision': None,
            'version': None,
            'component_schema': 1,
            'owned_paths': [tool_rel],
            'baseline': {tool_rel: {'state': 'unknown', 'reason': 'legacy-original-not-verified'}},
        }
        self.inventory['components'].append(tool_component)
        self._save_inventory()
        plan = self.plan()
        self.assertIsNotNone(plan['plan_sha256'])

    def _release_reference(self, body):
        payload = {'schema': 1, 'release_id': 'synthetic-reference-release',
            'source_revision': 'c' * 40, 'published_at': '2026-01-01T00:00:00Z',
            'expires_at': '2027-01-01T00:00:00Z', 'minimum_engine': 1,
            'changelog': 'Synthetic reference update', 'components': [{
                'id': 'editorial-handbook', 'kind': 'reference', 'version': '2.0.0', 'platforms': ['any'],
                'requires': {}, 'files': {'methods/guide.md': {
                    'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)}}}]}
        self.envelope = {'payload': payload, 'key_id': 'synthetic-test',
                         'signature': base64.b64encode(self.key.sign(release.canonical(payload))).decode()}
        with zipfile.ZipFile(self.bundle_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('editorial-handbook/methods/guide.md', body)


def release_update_target_bytes(files):
    return updater.canonical(files)


if __name__ == '__main__':
    unittest.main()
