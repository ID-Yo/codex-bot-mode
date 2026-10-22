import base64
from copy import deepcopy
from datetime import datetime, timezone
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request, HTTPSHandler, ProxyHandler, build_opener
from urllib.response import addinfourl
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import component_release as release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.trust = {'schema': 1, 'keys': {'test': base64.b64encode(public).decode()}, 'revoked': []}
        self.body = b'# Real sourced guidance\n'
        self.path = 'methods/linkedin.md'
        self.payload = {'schema': 1, 'release_id': 'test-release', 'source_revision': 'a' * 40,
                        'published_at': '2026-01-01T00:00:00Z', 'expires_at': '2027-01-01T00:00:00Z',
                        'minimum_engine': 1, 'changelog': 'Add guidance', 'components': [
                            {'id': 'editorial', 'kind': 'reference', 'version': '1.0.0',
                             'platforms': ['any'], 'requires': {}, 'files': {
                                 self.path: {'sha256': hashlib.sha256(self.body).hexdigest(), 'size': len(self.body)}}}]}
        self.now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    def envelope(self, payload=None):
        data = deepcopy(self.payload if payload is None else payload)
        return {'payload': data, 'key_id': 'test',
                'signature': base64.b64encode(self.key.sign(release.canonical(data))).decode()}

    def archive(self, body=None, name=None, mode=None, extra=False):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            info = zipfile.ZipInfo(name or 'editorial/' + self.path)
            if mode:
                info.external_attr = mode << 16
            z.writestr(info, self.body if body is None else body)
            if extra:
                z.writestr('extra.txt', b'bad')
        stream.seek(0)
        return stream

    def verify(self, envelope=None, archive=None, trust=None, installed=None):
        return release.verify_bundle(envelope or self.envelope(), archive or self.archive(),
                                     self.trust if trust is None else trust, 'windows-x86_64', installed, self.now)

    def test_signed_bundle_does_not_write_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            protected = Path(tmp) / 'STATE.md'
            protected.write_bytes(b'owner state')
            before = list(Path(tmp).iterdir())
            self.assertEqual(self.verify()['release_id'], 'test-release')
            self.assertEqual(list(Path(tmp).iterdir()), before)
            self.assertEqual(protected.read_bytes(), b'owner state')

    def test_tampering_untrusted_revoked_wrong_signature_rejected(self):
        bad = self.envelope()
        bad['payload']['changelog'] = 'tampered'
        cases = [(bad, self.trust), (self.envelope(), {'schema': 1, 'keys': {}}),
                 (self.envelope(), {**self.trust, 'revoked': ['test']})]
        other = Ed25519PrivateKey.generate()
        wrong = self.envelope()
        wrong['signature'] = base64.b64encode(other.sign(release.canonical(wrong['payload']))).decode()
        cases.append((wrong, self.trust))
        for envelope, trust in cases:
            with self.subTest(envelope=envelope['key_id'], trust=trust), self.assertRaises(release.ReleaseError):
                self.verify(envelope=envelope, trust=trust)

    def test_incompatible_and_stale_signed_metadata(self):
        for change in [{'schema': 2}, {'minimum_engine': 2}, {'expires_at': '2026-01-01T00:00:00Z'},
                       {'published_at': '2026-12-01T00:00:00Z'}, {'source_revision': 'main'}]:
            p = {**deepcopy(self.payload), **change}
            with self.subTest(change=change), self.assertRaises(release.ReleaseError):
                self.verify(self.envelope(p))
        for change in [{'platforms': ['linux-x86_64']}, {'requires': {'office': '9.0.0'}}, {'version': 'latest'}]:
            p = deepcopy(self.payload)
            p['components'][0].update(change)
            with self.subTest(change=change), self.assertRaises(release.ReleaseError):
                self.verify(self.envelope(p))
        with self.assertRaises(release.ReleaseError):
            self.verify(installed={'editorial': '2.0.0'})

    def test_archive_checksum_size_extras_links(self):
        for archive in [self.archive(body=b'wrong'), self.archive(extra=True),
                        self.archive(mode=stat.S_IFLNK | 0o777), self.archive(name='../bad')]:
            with self.subTest(), self.assertRaises(release.ReleaseError):
                self.verify(archive=archive)
        p = deepcopy(self.payload)
        p['components'][0]['files'][self.path]['sha256'] = 'b' * 64
        with self.assertRaises(release.ReleaseError):
            self.verify(self.envelope(p))

    def test_forbidden_and_windows_ambiguous_paths(self):
        for path in ['../x.md', '/x.md', 'a\\x.md', 'C:/x.md', 'a/../x.md', 'a//b.md',
                     'CON.md', 'methods/COM¹.md', 'templates/LPT².md', 'x./a.md', 'a/STATE.md', 'agent/agent.yaml', 'memory/note.md', 'OWNER.md', 'private/settings.yaml']:
            p = deepcopy(self.payload)
            p['components'][0]['files'] = {path: {'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()}}
            with self.subTest(path=path), self.assertRaises(release.ReleaseError):
                self.verify(self.envelope(p), self.archive(body=b'', name='editorial/' + path))

    def test_profile_and_tool_ownership(self):
        p = deepcopy(self.payload)
        p['components'][0]['kind'] = 'profile'
        with self.assertRaises(release.ReleaseError):
            self.verify(self.envelope(p))
        p['components'][0]['kind'] = 'tool'
        with self.assertRaises(release.ReleaseError):
            self.verify(self.envelope(p))
        policy = {**self.trust, 'component_paths': {'editorial': [self.path]}}
        self.assertEqual(self.verify(self.envelope(p), trust=policy)['components'][0]['kind'], 'tool')

    def test_case_collision_and_limits(self):
        p = deepcopy(self.payload)
        spec = p['components'][0]['files'][self.path]
        p['components'][0]['files'][self.path.upper()] = deepcopy(spec)
        with self.assertRaises(release.ReleaseError):
            self.verify(self.envelope(p))
        for size in [-1, True, release.MAX_FILE + 1]:
            p = deepcopy(self.payload)
            p['components'][0]['files'][self.path]['size'] = size
            with self.subTest(size=size), self.assertRaises(release.ReleaseError):
                self.verify(self.envelope(p))

    def test_duplicate_json_keys_and_size_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'manifest.json'
            for data in [b'{"schema":1,"schema":2}', b' ' * (release.MAX_MANIFEST + 1), b'{"n":NaN}']:
                file.write_bytes(data)
                with self.assertRaises(release.ReleaseError):
                    release.read_json(file)

    def test_network_offline_or_untrusted_never_means_up_to_date(self):
        with patch.object(release, 'build_opener') as built:
            with self.assertRaises(release.ReleaseError):
                release.fetch_verified('https://evil.test/manifest', 'https://evil.test/bundle', self.trust, 'windows-x86_64')
            built.return_value.open.assert_not_called()
        trust = {**self.trust, 'download_hosts': ['releases.test']}
        with patch.object(release, 'build_opener') as built:
            built.return_value.open.side_effect = OSError('offline')
            with self.assertRaises(OSError):
                release.fetch_verified('https://releases.test/manifest', 'https://releases.test/bundle', trust, 'windows-x86_64')

    def test_redirect_policy_rejects_before_following_each_hop(self):
        trust = {**self.trust, 'download_hosts': ['releases.test']}
        with patch.object(release, 'build_opener') as built:
            built.return_value.open.side_effect = OSError('offline')
            with self.assertRaises(OSError):
                release.fetch_verified('https://releases.test/manifest', 'https://releases.test/bundle', trust, 'windows-x86_64')
            redirect = built.call_args.args[0]
            request = Request('https://releases.test/start')
            for destination in ['https://evil.test/hop', 'http://releases.test/a', 'https://user:pass@releases.test/a', 'https://releases.test:444/a']:
                with self.subTest(destination=destination), self.assertRaises(release.ReleaseError):
                    redirect.redirect_request(request, None, 302, 'Moved', {}, destination)
            next_request = redirect.redirect_request(request, None, 302, 'Moved', {}, 'https://releases.test/next')
            self.assertEqual(next_request.full_url, 'https://releases.test/next')

    def test_structural_types_fail_closed(self):
        for field, value in [('keys', []), ('revoked', {}), ('component_paths', []), ('download_hosts', {})]:
            with self.subTest(field=field), self.assertRaises(release.ReleaseError):
                self.verify(trust={**self.trust, field: value})
        for field, value in [('kind', []), ('requires', []), ('platforms', {}), ('files', [])]:
            payload = deepcopy(self.payload)
            payload['components'][0][field] = value
            with self.subTest(field=field), self.assertRaises(release.ReleaseError):
                self.verify(self.envelope(payload))
        payload = {**deepcopy(self.payload), 'schema': True}
        with self.assertRaises(release.ReleaseError):
            self.verify(self.envelope(payload))

    def test_real_redirect_machinery_never_contacts_disallowed_hop(self):
        visited = []

        class Transport(HTTPSHandler):
            def https_open(self, request):
                url = request.full_url
                visited.append(url)
                headers = Message()
                headers['Location'] = 'https://evil.test/hop'
                response = addinfourl(io.BytesIO(b''), headers, url, 302)
                response.msg = 'fixture'
                return response

        def synthetic_opener(redirect):
            return build_opener(ProxyHandler({}), Transport(), redirect)

        trust = {**self.trust, 'download_hosts': ['approved.test']}
        with patch.object(release, 'build_opener', side_effect=synthetic_opener):
            with self.assertRaises(release.ReleaseError):
                release.fetch_verified('https://approved.test/start', 'https://approved.test/bundle', trust, 'windows-x86_64')
        self.assertEqual(visited, ['https://approved.test/start'])


if __name__ == '__main__':
    unittest.main()
