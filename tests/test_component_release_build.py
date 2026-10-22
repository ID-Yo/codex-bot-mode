import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_component_release as builder
import component_release as release
import public_audit


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'public'
        audit = patch.object(builder, 'scan', return_value=[])
        self.audit = audit.start()
        self.addCleanup(audit.stop)
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Publisher')
        self.git('config', 'user.email', 'publisher@example.invalid')
        self.git('remote', 'add', 'origin', 'https://github.com/example/codex-bot-mode.git')
        file = self.repo / 'catalog/references/editorial/methods/test.md'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'# verified committed guidance\n')
        self.git('add', '--', '.')
        self.git('commit', '-qm', 'synthetic public snapshot')
        now = datetime.now(timezone.utc)
        self.spec = {'schema': 1, 'release_id': 'synthetic-release', 'source_revision': self.git('rev-parse', 'HEAD').strip(),
                     'published_at': (now - timedelta(minutes=1)).isoformat(), 'expires_at': (now + timedelta(days=1)).isoformat(),
                     'minimum_engine': 1, 'changelog': 'Synthetic publisher test', 'components': [
                         {'id': 'editorial', 'kind': 'reference', 'version': '1.0.0', 'platforms': ['any'], 'requires': {},
                          'files': {'methods/test.md': 'catalog/references/editorial/methods/test.md'}}]}
        key = Ed25519PrivateKey.generate()
        self.key = self.root / 'synthetic-key.pem'
        self.key.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.trust = {'schema': 1, 'keys': {'synthetic': base64.b64encode(public).decode()},
                      'source_repository': 'https://github.com/example/codex-bot-mode'}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True, stderr=subprocess.DEVNULL)

    def build(self, out='release'):
        return builder.build(self.repo, self.spec, self.key, 'synthetic', self.trust, self.root / out, 'windows-x86_64')

    def test_committed_source_determinism_and_no_publication(self):
        (self.repo / 'catalog/references/editorial/methods/test.md').write_text('private dirty edit')
        self.assertFalse(self.build()['published'])
        self.build('second')
        for name in ('manifest.json', 'bundle.zip'):
            self.assertEqual((self.root / 'release' / name).read_bytes(), (self.root / 'second' / name).read_bytes())
        with zipfile.ZipFile(self.root / 'release/bundle.zip') as archive:
            self.assertEqual(archive.read('editorial/methods/test.md'), b'# verified committed guidance\n')
        manifest = release.read_json(self.root / 'release/manifest.json')
        self.assertEqual(release.verify_bundle(manifest, self.root / 'release/bundle.zip', self.trust, 'windows-x86_64')['release_id'], 'synthetic-release')
        with self.assertRaises(release.ReleaseError):
            self.build()

    def test_private_repo_or_uncommitted_path_rejected(self):
        self.git('remote', 'set-url', 'origin', 'https://github.com/example/codex-bot-mode-dev.git')
        with self.assertRaises(release.ReleaseError):
            self.build()
        self.git('remote', 'set-url', 'origin', 'https://github.com/example/codex-bot-mode.git')
        self.spec['components'][0]['files']['methods/test.md'] = 'untracked.md'
        with self.assertRaises(release.ReleaseError):
            self.build()
        self.assertFalse((self.root / 'release').exists())

    def test_audit_findings_prevent_signing_output(self):
        self.audit.return_value = ['synthetic privacy finding']
        with self.assertRaises(release.ReleaseError):
            self.build()
        self.assertFalse((self.root / 'release').exists())

    def test_export_ignore_cannot_hide_selected_bytes_from_real_scan(self):
        file = self.repo / 'catalog/references/editorial/methods/test.md'
        file.write_text('sk-' + 'X' * 30)
        (self.repo / '.gitattributes').write_text('catalog/references/editorial/methods/test.md export-ignore\n')
        self.git('add', '--', '.')
        self.git('commit', '-qm', 'synthetic hidden canary')
        self.spec['source_revision'] = self.git('rev-parse', 'HEAD').strip()
        examined = []

        def real_scan(root):
            findings = public_audit.scan(root)
            examined.extend(findings)
            self.assertEqual((root / 'catalog/references/editorial/methods/test.md').read_text(), 'sk-' + 'X' * 30)
            return findings

        self.audit.side_effect = real_scan
        with self.assertRaises(release.ReleaseError):
            self.build()
        self.assertTrue(any('credential' in str(f) for f in examined))
        self.assertFalse((self.root / 'release').exists())

    def test_replace_ref_does_not_change_signed_commit_contents(self):
        original = self.spec['source_revision']
        (self.repo / 'catalog/references/editorial/methods/test.md').write_text('replacement content')
        self.git('add', '--', '.')
        self.git('commit', '-qm', 'replacement')
        replacement = self.git('rev-parse', 'HEAD').strip()
        self.git('replace', original, replacement)
        self.build()
        with zipfile.ZipFile(self.root / 'release/bundle.zip') as archive:
            self.assertEqual(archive.read('editorial/methods/test.md'), b'# verified committed guidance\n')

    def test_output_manifest_size_and_malformed_descriptor(self):
        with patch.object(builder, 'MAX_MANIFEST', 10):
            with self.assertRaises(release.ReleaseError):
                self.build()
        with self.assertRaises(release.ReleaseError):
            builder.build(self.repo, [], self.key, 'synthetic', self.trust, self.root / 'bad', 'windows-x86_64')
        self.assertFalse((self.root / 'release').exists())


if __name__ == '__main__':
    unittest.main()
