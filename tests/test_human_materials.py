from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import human_materials as hm


class HumanMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.materials = self.root / 'human-materials'
        self.materials.mkdir()
        self.addCleanup(self.temp.cleanup)

    def material(self, name):
        path = self.materials / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Original synthetic bytes; not parsed.')
        return path

    def index(self, names):
        rows = ['| Material | Description / purpose | Format | Tags | Review status | Reviewed |',
                '|---|---|---|---|---|---|']
        for name in names:
            rows.append(f'| [Original]({quote(name, safe="/")}) | Filename only; content not reviewed. | binary | intake | unread | — |')
        (self.materials / 'README.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')

    def test_empty_and_housekeeping(self):
        self.index([])
        self.material('.gitignore')
        self.material('nested/.DS_Store')
        self.assertEqual(hm.check(self.root), [])

    def test_documented_example_is_not_a_material_row(self):
        self.index([])
        with (self.materials / 'README.md').open('a', encoding='utf-8') as out:
            out.write('\n```text\n| [Example](not-real.pdf) | Example only | PDF | demo | unread | — |\n```\n')
        self.assertEqual(hm.check(self.root), [])

    def test_scan_permission_error_is_reported(self):
        self.index([])
        def inaccessible(*args, **kwargs):
            kwargs['onerror'](PermissionError(13, 'Access denied', 'restricted'))
            return iter(())
        with patch.object(hm.os, 'walk', side_effect=inaccessible):
            self.assertTrue(any('Cannot inventory' in e for e in hm.check(self.root)))

    def test_nested_spaces_unicode_and_nested_readme_are_materials(self):
        names = ['slides/Plan (final) #1.pptx', 'photos/café.png', 'notes/README.md', '.hidden-source']
        paths = [self.material(n) for n in names]
        before = [p.read_bytes() for p in paths]
        self.index(names)
        self.assertEqual(hm.check(self.root), [])
        self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_add_move_remove_and_reconcile(self):
        path = self.material('old/report.pdf')
        self.index(['old/report.pdf'])
        self.assertEqual(hm.check(self.root), [])
        target = self.materials / 'new/report.pdf'
        target.parent.mkdir()
        path.rename(target)
        errors = hm.check(self.root)
        self.assertTrue(any('Missing row' in e for e in errors))
        self.assertTrue(any('Stale row' in e for e in errors))
        self.index(['new/report.pdf'])
        self.assertEqual(hm.check(self.root), [])
        target.unlink()
        self.assertTrue(any('Stale row' in e for e in hm.check(self.root)))
        self.index([])
        self.assertEqual(hm.check(self.root), [])

    def test_duplicate_and_outside_paths_rejected(self):
        self.material('one.pdf')
        self.index(['one.pdf', 'one.pdf', '../outside.pdf'])
        errors = hm.check(self.root)
        self.assertTrue(any('Duplicate' in e for e in errors))
        self.assertTrue(any('Unsafe' in e for e in errors))

    def test_review_status_and_date(self):
        self.material('one.pdf')
        self.index(['one.pdf'])
        index = self.materials / 'README.md'
        text = index.read_text(encoding='utf-8')
        index.write_text(text.replace('| unread |', '| reviewed |'), encoding='utf-8')
        self.assertTrue(any('review date' in e for e in hm.check(self.root)))
        index.write_text(text.replace('| unread |', '| unparseable |'), encoding='utf-8')
        self.assertEqual(hm.check(self.root), [])

    def test_cli_reports_missing_row_nonzero(self):
        self.index([])
        self.material('unread.bin')
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/human_materials.py'),
                                 '--root', str(self.root)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing row', result.stdout)

    def test_symlink_is_not_followed(self):
        self.index([])
        outside = self.root / 'outside.txt'
        outside.write_text('private', encoding='utf-8')
        try:
            (self.materials / 'link.txt').symlink_to(outside)
        except OSError:
            self.skipTest('Host does not permit creating symlinks')
        self.assertTrue(any('symlink' in e.lower() for e in hm.check(self.root)))


    def test_export_public_excludes_private_human_materials_canary(self):
        ep_path = ROOT / 'scripts/export_public.py'
        if not ep_path.exists():
            return
        import importlib.util
        spec = importlib.util.spec_from_file_location('export_public', ep_path)
        ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ep)

        with tempfile.TemporaryDirectory() as canary_tmp:
            canary_root = Path(canary_tmp)
            proj = canary_root / 'proj'
            proj.mkdir()
            hm_live = proj / 'human-materials'
            hm_live.mkdir()
            (hm_live / '.gitignore').write_text('*\n!README.md\n!.gitignore\n', encoding='utf-8')

            # 1. Populated README with private filenames, descriptions, and links
            populated_table = (
                '# Human Materials\n\n'
                '## Material index\n\n'
                '| Material | Description / purpose | Format | Tags | Review status | Reviewed |\n'
                '|---|---|---|---|---|---|\n'
                '| [Secret Contract](contracts/private_deal.pdf) | Highly confidential client contract | PDF | private | reviewed | 2026-10-14 |\n'
                '| [Financial Dump](quarterly_profit.xlsx) | Private ledger dump | XLSX | financials | unread | — |\n'
            )
            (hm_live / 'README.md').write_text(populated_table, encoding='utf-8')

            # 2. Root and nested private files and folders
            (hm_live / 'quarterly_profit.xlsx').write_bytes(b'PRIVATE_FINANCIAL_BYTES_ROOT')
            nested = hm_live / 'contracts'
            nested.mkdir()
            (nested / 'private_deal.pdf').write_bytes(b'CONFIDENTIAL_DEAL_BYTES_NESTED')

            # 3. Symlink / junction if host environment permits
            outside = canary_root / 'outside_dir'
            outside.mkdir()
            (outside / 'external_leak.txt').write_bytes(b'EXTERNAL_LEAK')
            try:
                (hm_live / 'symlink_dir').symlink_to(outside)
            except (OSError, NotImplementedError):
                pass

            # Clean public scaffold in proj / 'public/human-materials'
            pub_hm = proj / 'public/human-materials'
            pub_hm.mkdir(parents=True)
            (pub_hm / '.gitignore').write_text((ROOT / 'public/human-materials/.gitignore').read_text(encoding='utf-8'), encoding='utf-8')
            canonical_path = (ROOT / 'human-materials/README.md') if (ROOT / 'human-materials/README.md').exists() else (ROOT / 'public/human-materials/README.md')
            canonical_scaffold_readme = canonical_path.read_text(encoding='utf-8')
            (pub_hm / 'README.md').write_text(canonical_scaffold_readme, encoding='utf-8')

            # Run export_public against this synthetic project
            export_out = canary_root / 'export_out'
            with patch.object(ep, 'ROOT', proj), patch.object(ep, 'PUBLIC_FILES', proj / 'public'), patch.object(ep, 'INCLUDE', []):
                ep.export(export_out)

            # Assertions:
            self.assertTrue((export_out / 'human-materials').is_dir())
            self.assertTrue((export_out / 'human-materials/.gitignore').is_file())
            self.assertTrue((export_out / 'human-materials/README.md').is_file())

            exported_readme = (export_out / 'human-materials/README.md').read_text(encoding='utf-8')
            self.assertNotIn('private_deal.pdf', exported_readme)
            self.assertNotIn('quarterly_profit.xlsx', exported_readme)
            self.assertNotIn('Confidential', exported_readme)
            self.assertNotIn('Secret Contract', exported_readme)

            self.assertFalse((export_out / 'human-materials/quarterly_profit.xlsx').exists())
            self.assertFalse((export_out / 'human-materials/contracts').exists())
            self.assertFalse((export_out / 'human-materials/symlink_dir').exists())
            self.assertFalse((export_out / 'human-materials/external_leak.txt').exists())

            self.assertTrue((hm_live / 'quarterly_profit.xlsx').exists())
            self.assertEqual((hm_live / 'quarterly_profit.xlsx').read_bytes(), b'PRIVATE_FINANCIAL_BYTES_ROOT')
            self.assertTrue((nested / 'private_deal.pdf').exists())
            self.assertEqual((nested / 'private_deal.pdf').read_bytes(), b'CONFIDENTIAL_DEAL_BYTES_NESTED')
            self.assertEqual((hm_live / 'README.md').read_text(encoding='utf-8'), populated_table)

    def test_export_public_preflight_rejects_missing_or_corrupted_public_scaffold(self):
        ep_path = ROOT / 'scripts/export_public.py'
        if not ep_path.exists():
            return
        import importlib.util
        spec = importlib.util.spec_from_file_location('export_public', ep_path)
        ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ep)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / 'proj'
            proj.mkdir()
            pub = proj / 'public'
            pub.mkdir()

            # 1. Missing public scaffold entirely -> fails closed
            export_out = root / 'out1'
            with patch.object(ep, 'ROOT', proj), patch.object(ep, 'PUBLIC_FILES', pub), patch.object(ep, 'INCLUDE', []):
                with self.assertRaises(SystemExit) as caught:
                    ep.export(export_out)
                self.assertIn('Missing required public human-materials scaffold', str(caught.exception))
            self.assertFalse(export_out.exists())

            # 2. Extra file in public scaffold -> fails closed
            pub_hm = pub / 'human-materials'
            pub_hm.mkdir()
            (pub_hm / '.gitignore').write_text('*\n', encoding='utf-8')
            (pub_hm / 'README.md').write_text('# HM\n', encoding='utf-8')
            (pub_hm / 'extra.bin').write_bytes(b'CANARY')
            export_out2 = root / 'out2'
            with patch.object(ep, 'ROOT', proj), patch.object(ep, 'PUBLIC_FILES', pub), patch.object(ep, 'INCLUDE', []):
                with self.assertRaises(SystemExit) as caught:
                    ep.export(export_out2)
                self.assertIn('Invalid, extra, or linked entry', str(caught.exception))
            self.assertFalse(export_out2.exists())

            # 3. Populated or custom prose README in public scaffold -> fails closed
            (pub_hm / 'extra.bin').unlink()
            prose_readme = '# Human Materials\nPRIVATE_INDEX_CANARY_PROSE private-filename.pdf\n'
            (pub_hm / 'README.md').write_text(prose_readme, encoding='utf-8')
            export_out3 = root / 'out3'
            with patch.object(ep, 'ROOT', proj), patch.object(ep, 'PUBLIC_FILES', pub), patch.object(ep, 'INCLUDE', []):
                with self.assertRaises(SystemExit) as caught:
                    ep.export(export_out3)
                self.assertIn('canonical empty template', str(caught.exception))
            self.assertFalse(export_out3.exists())

            # 4. Public files symlink to outside target -> fails closed
            (pub_hm / 'README.md').write_text((pub_hm / 'README.md').read_text(), encoding='utf-8')
            outside_target = root / 'outside_secret.txt'
            outside_target.write_bytes(b'OUTSIDE_SECRET')
            export_out4 = root / 'out4'
            try:
                (pub / 'leak.txt').symlink_to(outside_target)
                with patch.object(ep, 'ROOT', proj), patch.object(ep, 'PUBLIC_FILES', pub), patch.object(ep, 'INCLUDE', []):
                    with self.assertRaises(SystemExit) as caught:
                        ep.export(export_out4)
                    self.assertIn('Unsupported symlink, junction, or containment escape in public files', str(caught.exception))
                self.assertFalse(export_out4.exists())
            except (OSError, NotImplementedError):
                pass

    def test_public_audit_detects_binary_nested_symlink_and_populated_leaks(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import public_audit as pa

        with tempfile.TemporaryDirectory() as tmp:
            export_tree = Path(tmp) / 'export'
            export_tree.mkdir()
            hm = export_tree / 'human-materials'
            hm.mkdir()
            canonical_gi = ((ROOT / 'human-materials/.gitignore') if (ROOT / 'human-materials/.gitignore').exists() else (ROOT / 'public/human-materials/.gitignore')).read_text(encoding='utf-8')
            (hm / '.gitignore').write_text(canonical_gi, encoding='utf-8')
            canonical_path = (ROOT / 'human-materials/README.md') if (ROOT / 'human-materials/README.md').exists() else (ROOT / 'public/human-materials/README.md')
            canonical_empty_readme = canonical_path.read_text(encoding='utf-8')
            (hm / 'README.md').write_text(canonical_empty_readme, encoding='utf-8')

            # Clean scaffold produces 0 findings
            self.assertEqual(pa.scan(export_tree), [])

            # Extra non-UTF8 binary file in human-materials -> must be caught BEFORE text decode
            (hm / 'extra.bin').write_bytes(b'\xff\xfe\x00\x00BAD_BINARY_BYTES')
            findings = pa.scan(export_tree)
            self.assertTrue(any('Extra file' in f[3] for f in findings))
            (hm / 'extra.bin').unlink()

            # Extra nested binary file -> must be caught
            nested = hm / 'nested'
            nested.mkdir()
            (nested / 'leak.pdf').write_bytes(b'%PDF-1.4 binary data')
            findings = pa.scan(export_tree)
            self.assertTrue(any('Extra file' in f[3] or 'Extra directory' in f[3] for f in findings))
            shutil.rmtree(nested)

            # Populated README table row with link -> must be caught
            pop_readme = canonical_empty_readme + '\n| [Secret](secret.pdf) | confidential | PDF | tag | unread | — |\n'
            (hm / 'README.md').write_text(pop_readme, encoding='utf-8')
            findings = pa.scan(export_tree)
            self.assertTrue(any('does not match canonical empty template' in f[3] for f in findings))

            # Populated README table row with plain unlinked text -> must be caught
            plain_pop = canonical_empty_readme + '\n| Secret Document | plain description | PDF | tag | unread | — |\n'
            (hm / 'README.md').write_text(plain_pop, encoding='utf-8')
            findings = pa.scan(export_tree)
            self.assertTrue(any('does not match canonical empty template' in f[3] for f in findings))

            # Custom prose README without table rows -> must be caught as modified material scaffold
            (hm / 'README.md').write_text('# Human Materials\nPRIVATE_INDEX_CANARY_PROSE private.pdf\n', encoding='utf-8')
            findings = pa.scan(export_tree)
            self.assertTrue(any('does not match canonical empty template' in f[3] for f in findings))

            # Symlink or junction on human-materials root directory -> must be caught immediately
            shutil.rmtree(hm)
            outside_dir = Path(tmp) / 'outside_hm'
            outside_dir.mkdir()
            try:
                hm.symlink_to(outside_dir)
                findings = pa.scan(export_tree)
                self.assertTrue(any('human-materials root' in f[3] for f in findings))
                hm.unlink()
            except (OSError, NotImplementedError):
                pass
            hm.mkdir()
            (hm / '.gitignore').write_text(canonical_gi, encoding='utf-8')

            # Global patterns in human-materials/README.md prose must NOT be skipped (backslash and slash paths)
            bs = chr(92)
            win_backslash_path = 'C:' + bs + 'Users' + bs + 'User' + bs + 'secret'
            (hm / 'README.md').write_text(canonical_empty_readme + '\nNotes: ' + win_backslash_path + '\n', encoding='utf-8')
            findings = pa.scan(export_tree)
            self.assertTrue(any(f[2] == 'local user path' for f in findings))

            slash_path = 'D:/' + 'Coding' + '/secret'
            (hm / 'README.md').write_text(canonical_empty_readme + '\nNotes: ' + slash_path + '\n', encoding='utf-8')
            findings = pa.scan(export_tree)
            self.assertTrue(any(f[2] == 'local user path' for f in findings))

            # Owner account in prose -> must be caught
            owner_lit = 'ID' + '-Yo'
            (hm / 'README.md').write_text(canonical_empty_readme + '\nAuthor: ' + owner_lit + '\n', encoding='utf-8')
            findings = pa.scan(export_tree)
            self.assertTrue(any(f[2] == 'owner account' for f in findings))

    def test_documentation_links_resolve_in_source_and_export(self):
        import re
        def extract_links_outside_fences(md_text):
            fence = None
            outside_lines = []
            for line in md_text.splitlines():
                marker = re.match(r'^\s*(`{3,}|~{3,})', line)
                if marker:
                    token = marker[1]
                    if fence is None:
                        fence = token
                    elif token[0] == fence[0] and len(token) >= len(fence):
                        fence = None
                    continue
                if not fence:
                    outside_lines.append(line)
            clean = '\n'.join(outside_lines)
            links = []
            for part in clean.split(']('):
                if ')' in part:
                    target = part.split(')')[0].strip()
                    if target.startswith('http') or target.startswith('#') or target.startswith('mailto:') or target.startswith('codex:'):
                        continue
                    clean_target = target.split('#')[0]
                    if clean_target:
                        links.append(clean_target)
            return links

        def check_links_in_dir(doc_dir):
            for md_file in doc_dir.rglob('*.md'):
                for target in extract_links_outside_fences(md_file.read_text(encoding='utf-8')):
                    resolved = (md_file.parent / target).resolve()
                    self.assertTrue(resolved.exists(), f'Broken link in {md_file.relative_to(doc_dir)}: {target} (resolved to {resolved})')

        # 1. Verify newly adopted documentation routes in source repository
        source_files = [
            ROOT / 'docs/00-index.md',
            ROOT / 'docs/STRUCTURE.md',
            ROOT / 'human-materials/README.md',
            ROOT / 'templates/README.md'
        ] + list((ROOT / 'docs/marketing').glob('*.md')) + list((ROOT / 'docs/status').glob('*.md'))
        for sf in source_files:
            for target in extract_links_outside_fences(sf.read_text(encoding='utf-8')):
                resolved = (sf.parent / target).resolve()
                self.assertTrue(resolved.exists(), f'Broken link in {sf.name}: {target} (resolved to {resolved})')

        # 2. Verify all markdown files in public export output
        ep_path = ROOT / 'scripts/export_public.py'
        if ep_path.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location('export_public', ep_path)
            ep = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ep)
            with tempfile.TemporaryDirectory() as export_tmp:
                export_out = Path(export_tmp) / 'export'
                ep.export(export_out)
                check_links_in_dir(export_out / 'docs')
                check_links_in_dir(export_out / 'templates')
                check_links_in_dir(export_out / 'human-materials')
        else:
            if (ROOT / 'docs').is_dir():
                check_links_in_dir(ROOT / 'docs')
            if (ROOT / 'templates').is_dir():
                check_links_in_dir(ROOT / 'templates')
            if (ROOT / 'human-materials').is_dir():
                check_links_in_dir(ROOT / 'human-materials')

if __name__ == '__main__':
    unittest.main()
