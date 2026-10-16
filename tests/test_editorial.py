import unittest
from pathlib import Path
import tempfile
import shutil
import subprocess
import tarfile
import io
import sys
import re
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import teams, manifest

CATALOG = ROOT / 'catalog'


def resolve_target(project, pb, target):
    # Skip public repository source path citations that are not project-installed
    if target.startswith(('docs/', 'templates/', 'http', '#', 'mailto:')) or target.endswith('}'):
        return None
    if target.startswith('team/'):
        return (project / target).resolve()
    if target.startswith('agent/'):
        return (pb.parent.parent / target).resolve()
    return (pb.parent / target).resolve()

def extract_resolvable_targets(content):
    targets = []
    # Markdown links
    for m in re.finditer(r'\[([^\]]+)\]\(([^\)]+)\)', content):
        cand = m.group(2).strip()
        if not cand.startswith(('docs/', 'templates/', 'http', '#', 'mailto:')) and not cand.endswith('}'):
            if '/' in cand or chr(92) in cand or cand.startswith('.'):
                targets.append(cand)
    # Plain backticked paths (must have directory segment or relative prefix)
    for m in re.finditer(r'`([^`\r\n]+\.(?:md|yaml|yml))`', content):
        cand = m.group(1).strip()
        if not cand.startswith(('docs/', 'templates/', 'http', '#', 'mailto:')) and not cand.endswith('}'):
            if '/' in cand or chr(92) in cand or cand.startswith('.'):
                targets.append(cand)
    return targets


class EditorialTests(unittest.TestCase):
    def test_editorial_reference_library_completeness(self):
        ref_dir = CATALOG / 'references' / 'editorial'
        self.assertTrue(ref_dir.exists(), "Reference directory missing")
        index_file = ref_dir / 'index.yaml'
        self.assertTrue(index_file.exists(), "index.yaml missing")
        data = manifest.load(index_file)
        self.assertEqual(data.get('name'), 'editorial-references')
        self.assertIn('modules', data)
        self.assertIn('templates', data)

        required_methods = [
            'methods/extraction.md',
            'methods/framing.md',
            'methods/offers.md',
            'methods/articles.md',
            'methods/video-scripts.md',
            'methods/video-timestamps.md',
            'methods/channels.md',
        ]
        for m in required_methods:
            self.assertTrue((ref_dir / m).exists(), f"Missing method file: {m}")

        # Assert every index method id has an executable method definition in its target file
        for mod_name, mod_info in data['modules'].items():
            mod_file = ref_dir / mod_info['path']
            mod_text = mod_file.read_text(encoding='utf-8')
            for method_id in mod_info.get('methods', []):
                # Search for method heading containing method_id or its title version
                slug_pattern = re.escape(method_id).replace(r'\-', r'[ -.]*')
                self.assertTrue(bool(re.search(slug_pattern, mod_text, re.I)),
                                f"Method id {method_id} declared in index.yaml has no definition in {mod_info['path']}")

        required_templates = [
            'templates/brief.md',
            'templates/draft-review.md',
            'templates/calendar.md',
            'templates/performance-note.md',
        ]
        for t in required_templates:
            self.assertTrue((ref_dir / t).exists(), f"Missing template file: {t}")

    def test_marketing_team_and_role_reference_declarations(self):
        mkt_file = CATALOG / 'teams' / 'marketing' / 'team.yaml'
        self.assertTrue(mkt_file.exists())
        mkt_data = manifest.load(mkt_file)
        self.assertEqual(mkt_data.get('version'), 2)
        self.assertEqual(mkt_data.get('references'), ['editorial'])

        for role in ('strategist', 'content-writer', 'content-reviewer'):
            role_file = CATALOG / 'agents' / role / 'agent.yaml'
            self.assertTrue(role_file.exists())
            role_data = manifest.load(role_file)
            self.assertEqual(role_data.get('version'), 2)
            self.assertEqual(role_data.get('references'), ['editorial'])
            params = role_data.get('parameters', {})
            self.assertIn('display_name', params)
            self.assertIn('model', params)
            self.assertIn('reasoning', params)
            self.assertEqual(params['model']['default'], 'inherit')
            self.assertEqual(params['reasoning']['default'], 'inherit')

    def test_reference_delivery_and_link_resolution_on_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            # 1. Install marketing team with parameter overrides
            answers = {
                'strategist': {'model': 'gpt-6.1-sol', 'reasoning': 'high'},
                'content-writer': {'model': 'gpt-6-luna', 'reasoning': 'medium'},
            }
            plan = teams.plan_team(project, 'marketing', answers=answers, team_answers={'tracking_mode': 'local'})
            teams.install(plan)

            ref_dir = project / 'team' / 'references' / 'editorial'
            self.assertTrue(ref_dir.exists())
            self.assertTrue((ref_dir / 'README.md').exists())
            self.assertTrue((ref_dir / 'methods/video-timestamps.md').exists())

            # Verify parameter overrides applied in manifest
            strat_yaml = manifest.load(project / 'team/Marketing/strategist/agent/agent.yaml')
            self.assertEqual(strat_yaml['parameters']['model']['value'], 'gpt-6.1-sol')
            self.assertEqual(strat_yaml['parameters']['reasoning']['value'], 'high')

            # 2. Check relative links and backticked paths in installed playbooks
            total_checked = 0
            for agent in plan['agents']:
                pb = project / agent['workspace'] / 'agent' / 'PLAYBOOK.md'
                self.assertTrue(pb.exists())
                content = pb.read_text(encoding='utf-8')
                targets = extract_resolvable_targets(content)
                self.assertGreater(len(targets), 0, f"No targets extracted from {agent['bot']}")
                for target in targets:
                    resolved = resolve_target(project, pb, target)
                    self.assertTrue(resolved.exists(), f"Broken relative target in {agent['bot']}: {target} -> {resolved}")
                    total_checked += 1
            self.assertGreaterEqual(total_checked, 10, "Expected non-zero checked relative paths")

            # 3. Preservation on re-installation
            custom_marker = "# Custom Method\nPRESERVE_ME\n"
            custom_file = ref_dir / 'methods' / 'custom.md'
            custom_file.write_text(custom_marker, encoding='utf-8')

            plan_dev = teams.plan_team(project, 'development', team_answers={'tracking_mode': 'local'})
            teams.install(plan_dev)
            self.assertEqual(custom_file.read_text(encoding='utf-8'), custom_marker)

    def test_all_three_standalone_editorial_agents_install_references(self):
        for role in ('strategist', 'content-writer', 'content-reviewer'):
            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                plan_dev = teams.plan_team(project, 'development', team_answers={'tracking_mode': 'local'})
                teams.install(plan_dev)

                plan_agent = teams.plan_agent(project, 'Development', role, answers={'model': 'gpt-6.1-sol'})
                teams.install(plan_agent)

                ref_file = project / 'team' / 'references' / 'editorial' / 'methods' / 'framing.md'
                self.assertTrue(ref_file.exists(), f"Reference not installed for standalone {role}")

                pb = project / 'team' / 'Development' / role / 'agent' / 'PLAYBOOK.md'
                content = pb.read_text(encoding='utf-8')
                targets = extract_resolvable_targets(content)
                self.assertGreater(len(targets), 0, f"No targets in standalone {role}")
                for target in targets:
                    resolved = resolve_target(project, pb, target)
                    self.assertTrue(resolved.exists(), f"Broken target in standalone {role}: {target} -> {resolved}")

    def test_reference_delivery_safety_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            bad_cat = Path(tmp) / 'bad_cat'
            shutil.copytree(CATALOG, bad_cat)

            # Missing index.yaml raises TeamError
            (bad_cat / 'references/editorial/index.yaml').unlink()
            with self.assertRaises(teams.TeamError) as caught:
                teams._reference_files(project, bad_cat, ['editorial'])
            self.assertIn("missing index.yaml", str(caught.exception))

            # Malformed index.yaml raises TeamError
            shutil.rmtree(bad_cat)
            shutil.copytree(CATALOG, bad_cat)
            (bad_cat / 'references/editorial/index.yaml').write_text("invalid_yaml: [unterminated", encoding='utf-8')
            with self.assertRaises(teams.TeamError) as caught:
                teams._reference_files(project, bad_cat, ['editorial'])
            self.assertTrue("Malformed index.yaml" in str(caught.exception) or "index lacks required" in str(caught.exception))

            # Index declaring absolute method path rejected
            shutil.rmtree(bad_cat)
            shutil.copytree(CATALOG, bad_cat)
            bad_idx = bad_cat / 'references/editorial/index.yaml'
            d = manifest.load(bad_idx)
            d['modules']['articles']['path'] = '/etc/passwd'
            manifest.dump(bad_idx, d)
            with self.assertRaises(teams.TeamError) as caught:
                teams._reference_files(project, bad_cat, ['editorial'])
            self.assertIn("declares unsafe method path", str(caught.exception))

            # Incompatible existing brief.md (non-empty but lacks schema) raises TeamError
            shutil.rmtree(bad_cat)
            shutil.copytree(CATALOG, bad_cat)
            (project / 'team/references/editorial/templates').mkdir(parents=True, exist_ok=True)
            (project / 'team/references/editorial/templates/brief.md').write_text("INCOMPATIBLE_NONEMPTY_NO_BRIEF_FIELDS", encoding='utf-8')
            with self.assertRaises(teams.TeamError) as caught:
                teams._reference_files(project, bad_cat, ['editorial'])
            self.assertIn("lacks required brief schema", str(caught.exception))
            (project / 'team/references/editorial/templates/brief.md').unlink()

            # Target junction added after planning raises TeamError on install() before writes
            plan_dev = teams.plan_team(project, 'marketing', team_answers={'tracking_mode': 'local'})
            outside_dir = Path(tmp) / 'outside_dir'
            outside_dir.mkdir()
            # Simulate a target junction on team/references
            try:
                (project / 'team/references').symlink_to(outside_dir, target_is_directory=True)
                with self.assertRaises(teams.TeamError) as caught:
                    teams.install(plan_dev)
                self.assertIn("Target junction or symlink not permitted", str(caught.exception))
                (project / 'team/references').unlink()
            except (OSError, NotImplementedError):
                pass  # host without symlink rights skips symlink creation
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            # 1. Non-list declaration
            with self.assertRaises(teams.TeamError):
                teams._reference_files(project, CATALOG, "editorial")
            # 2. Absolute path reference
            with self.assertRaises(teams.TeamError):
                teams._reference_files(project, CATALOG, ['/absolute/path'])
            # 3. Windows drive path reference
            with self.assertRaises(teams.TeamError):
                teams._reference_files(project, CATALOG, ['C:\\windows\\path'])
            # 4. Traversal path reference
            with self.assertRaises(teams.TeamError):
                teams._reference_files(project, CATALOG, ['../outside'])
            # 5. Missing reference directory
            with self.assertRaises(teams.TeamError):
                teams._reference_files(project, CATALOG, ['nonexistent_ref_dir'])

            # 6. Missing indexed method file rejected preflight
            bad_cat = Path(tmp) / 'bad_cat'
            shutil.copytree(CATALOG, bad_cat)
            bad_idx = bad_cat / 'references/editorial/index.yaml'
            idx_data = manifest.load(bad_idx)
            idx_data['modules']['broken'] = {'path': 'methods/missing_method.md'}
            manifest.dump(bad_idx, idx_data)
            with self.assertRaises(teams.TeamError) as caught:
                teams._reference_files(project, bad_cat, ['editorial'])
            self.assertIn("declares missing indexed method file", str(caught.exception))

            # 7. Unsupported file type in reference folder rejected preflight
            extra_bin = bad_cat / 'references/editorial/methods/binary.bin'
            extra_bin.write_bytes(b'\x00\x01\x02')
            manifest.dump(bad_idx, manifest.load(CATALOG / 'references/editorial/index.yaml')) # restore
            with self.assertRaises(teams.TeamError) as caught:
                teams._reference_files(project, bad_cat, ['editorial'])
            self.assertIn("Unsupported file type", str(caught.exception))

    def test_downloaded_tarball_catalog_delivers_references_without_local_repo(self):
        # Package catalog into a synthetic tarball as returned by gh api repos/.../tarball/...
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode='w:gz') as tar:
            for p in CATALOG.rglob('*'):
                if p.is_file():
                    arcname = 'repo-ref-sha/catalog/' + p.relative_to(CATALOG).as_posix()
                    tar.add(p, arcname=arcname)
        tarball_bytes = archive_buffer.getvalue()

        def fake_gh_api(cmd, capture_output=True, check=False):
            class FakeResult:
                returncode = 0
                stdout = tarball_bytes
                stderr = b''
            return FakeResult()

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            with patch.object(teams.subprocess, 'run', side_effect=fake_gh_api):
                plan = teams.plan_team(project, 'marketing', source='github:mock-owner/mock-repo@v1.0.0',
                                       team_answers={'tracking_mode': 'local'})
                teams.install(plan)

            ref_dir = project / 'team' / 'references' / 'editorial'
            self.assertTrue(ref_dir.exists())
            self.assertTrue((ref_dir / 'methods/framing.md').exists())

            # Verify playbook targets resolve cleanly
            writer_pb = project / 'team/Marketing/content-writer/agent/PLAYBOOK.md'
            content = writer_pb.read_text(encoding='utf-8')
            targets = extract_resolvable_targets(content)
            self.assertGreater(len(targets), 0)
            for target in targets:
                resolved = resolve_target(project, writer_pb, target)
                self.assertTrue(resolved.exists(), f"Downloaded catalog target failed to resolve: {target}")

    def test_video_timestamps_grounding_rules(self):
        ts_file = CATALOG / 'references' / 'editorial' / 'methods' / 'video-timestamps.md'
        content = ts_file.read_text(encoding='utf-8')
        self.assertIn("Zero Invented Timings", content)
        self.assertIn("Untimed Fallback", content)
        self.assertIn("No Automatic 00:00 Assumption", content)
        self.assertIn("Chronological & Span Validation", content)


    def test_install_preflight_validates_preserved_target_index_after_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            # Copy valid reference directory into project
            ref_src = CATALOG / 'references' / 'editorial'
            ref_dst = project / 'team' / 'references' / 'editorial'
            shutil.copytree(ref_src, ref_dst)

            # Plan marketing team in project
            plan_mkt = teams.plan_team(project, 'marketing', team_answers={'tracking_mode': 'local'})
            # index.yaml is NOT in project_files because it already exists on disk
            self.assertNotIn('team/references/editorial/index.yaml', plan_mkt['project_files'])

            # Mutate preserved target index.yaml to declare a missing method path
            target_idx = ref_dst / 'index.yaml'
            idx_d = manifest.load(target_idx)
            idx_d['modules']['framing']['path'] = 'methods/MISSING_AFTER_PLAN.md'
            manifest.dump(target_idx, idx_d)

            # install(plan_mkt) must detect the missing path on target_dir and fail before writes
            with self.assertRaises(teams.TeamError) as caught:
                teams.install(plan_mkt)
            self.assertIn("declares missing indexed method file", str(caught.exception))

            # Verify no agent workspaces or roster entries were written
            self.assertFalse((project / 'team/Marketing').exists())
            self.assertFalse((project / '.bot-mode/roster.json').exists())

    def test_install_empty_project_files_succeeds_without_unbound_local_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            # Pre-populate all project files and references so plan has empty project_files
            ref_src = CATALOG / 'references' / 'editorial'
            ref_dst = project / 'team' / 'references' / 'editorial'
            shutil.copytree(ref_src, ref_dst)
            (project / 'team').mkdir(parents=True, exist_ok=True)
            for f in ('PROJECT.md', 'OWNER.md', 'DECISIONS.md'):
                (project / 'team' / f).write_text(f"# {f}", encoding='utf-8')

            plan = teams.plan_team(project, 'marketing', team_answers={'tracking_mode': 'local'})
            self.assertEqual(plan['project_files'], {})
            installed = teams.install(plan)
            self.assertEqual(len(installed), 4)
            self.assertTrue((project / 'team/Marketing').exists())

    def test_install_preserved_index_deleted_raises_team_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ref_src = CATALOG / 'references' / 'editorial'
            ref_dst = project / 'team' / 'references' / 'editorial'
            shutil.copytree(ref_src, ref_dst)

            plan = teams.plan_team(project, 'marketing', team_answers={'tracking_mode': 'local'})
            # Delete index.yaml from the preserved bundle
            (ref_dst / 'index.yaml').unlink()

            with self.assertRaises(teams.TeamError) as caught:
                teams.install(plan)
            self.assertIn("missing index.yaml", str(caught.exception))
            self.assertFalse((project / 'team/Marketing').exists())

if __name__ == '__main__':
    unittest.main()

