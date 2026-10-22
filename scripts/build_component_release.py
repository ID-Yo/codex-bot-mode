"""Build signed component assets from a frozen public Git revision.

Signing keys are supplied by a publisher; this command never generates keys.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

from component_release import ReleaseError, MAX_FILE, MAX_TOTAL, MAX_FILES, MAX_MANIFEST, canonical, read_json, safe_path, verify_bundle
from public_audit import scan


def git(root, *args):
    return subprocess.check_output(['git', '--no-replace-objects', '-C', str(root), *args], stderr=subprocess.DEVNULL)


def build(source, descriptor, key_file, key_id, trust, destination, platform, password=None):
    """Descriptor files map component destination paths to public repository paths."""
    root = Path(source).resolve(strict=True)
    if not isinstance(descriptor, dict) or not isinstance(trust, dict):
        raise ReleaseError('Descriptor and trust policy must be objects')
    components = descriptor.get('components')
    if not isinstance(components, list) or not 1 <= len(components) <= 128:
        raise ReleaseError('Invalid publisher component descriptor')
    for component in components:
        if not isinstance(component, dict) or not isinstance(component.get('id'), str) or not isinstance(component.get('files'), dict):
            raise ReleaseError('Invalid publisher component descriptor')
    destination = Path(destination)
    if destination.exists():
        raise ReleaseError('Release destination already exists; never overwrite an artifact')
    remote = git(root, 'remote', 'get-url', 'origin').decode().strip().removesuffix('.git')
    expected = trust.get('source_repository')
    if not isinstance(expected, str) or not expected.startswith('https://github.com/') or remote != expected.removesuffix('.git'):
        raise ReleaseError('Release source must be the curated public repository')
    revision = descriptor.get('source_revision')
    if not isinstance(revision, str) or len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ReleaseError('Release source must be an immutable Git SHA')
    if git(root, 'rev-parse', revision + '^{commit}').decode().strip() != revision:
        raise ReleaseError('Release revision is not an exact commit')
    source_blobs = {}
    with tempfile.TemporaryDirectory(prefix='cbm-source-audit-') as tmp:
        audit_root = Path(tmp) / 'public'
        audit_root.mkdir()
        # Read exact immutable tree objects. Export attributes/replace refs cannot
        # hide or substitute the content that will subsequently be signed.
        records = git(root, 'ls-tree', '-r', '-z', revision).split(b'\0')
        if len(records) > MAX_FILES + 1:
            raise ReleaseError('Source audit file count limit exceeded')
        count = 0
        seen = set()
        for record in records:
            if not record:
                continue
            metadata, name = record.split(b'\t', 1)
            mode, kind, oid = metadata.split(b' ')
            name = name.decode('utf-8')
            safe_path(name)
            if mode not in (b'100644', b'100755') or kind != b'blob' or name.casefold() in seen:
                raise ReleaseError('Unsupported or case-colliding source file')
            seen.add(name.casefold())
            size = int(git(root, 'cat-file', '-s', oid.decode()))
            count += size
            if size > MAX_FILE or count > MAX_TOTAL:
                raise ReleaseError('Source audit size limit exceeded')
            content = git(root, 'cat-file', 'blob', oid.decode())
            target = audit_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            source_blobs[name] = content
        if scan(audit_root):
            raise ReleaseError('Curated source privacy audit failed')
    # Read committed blobs, not a potentially dirty workspace. Reject Git links.
    payload = json.loads(json.dumps(descriptor))
    blobs = {}
    for component in payload.get('components', []):
        entries = {}
        for target, repository_path in component.get('files', {}).items():
            safe_path(target)
            safe_path(repository_path)
            if repository_path not in source_blobs:
                raise ReleaseError('Release source path is not a regular committed file')
            content = source_blobs[repository_path]
            if len(content) > MAX_FILE or sum(len(b) for b in blobs.values()) + len(content) > MAX_TOTAL:
                raise ReleaseError('Release file size limit exceeded')
            entries[target] = {'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}
            blobs[component['id'] + '/' + target] = content
        component['files'] = entries
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key_path = Path(key_file).resolve(strict=True)
    if key_path.is_relative_to(root):
        raise ReleaseError('Publisher signing key must be outside the source checkout')
    with key_path.open('rb') as stream:
        key_data = stream.read(16385)
    if len(key_data) > 16384:
        raise ReleaseError('Invalid signing key file size')
    key = load_pem_private_key(key_data, password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ReleaseError('Publisher key must be Ed25519')
    envelope = {'payload': payload, 'key_id': key_id,
                'signature': base64.b64encode(key.sign(canonical(payload))).decode()}
    manifest_bytes = canonical(envelope) + b'\n'
    if len(manifest_bytes) > MAX_MANIFEST:
        raise ReleaseError('Generated manifest exceeds verifier size limit')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cbm-publish-', dir=destination.parent) as tmp:
        stage = Path(tmp) / 'release'
        stage.mkdir()
        bundle = stage / 'bundle.zip'
        with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_STORED) as archive:
            for path, blob in sorted(blobs.items()):
                item = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                item.external_attr = 0o100644 << 16
                archive.writestr(item, blob)
        verify_bundle(envelope, bundle, trust, platform)
        (stage / 'manifest.json').write_bytes(manifest_bytes)
        # The destination must remain absent; only task-owned staging is renamed.
        if destination.exists():
            raise ReleaseError('Release destination changed during build')
        stage.rename(destination)
    return {'release_id': payload['release_id'], 'state': 'built-and-verified',
            'published': False, 'files': ['manifest.json', 'bundle.zip']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'descriptor', 'key', 'key_id', 'trust', 'out', 'platform'):
        parser.add_argument('--' + name.replace('_', '-'), required=True)
    parser.add_argument('--password-env', help='Name of publisher-managed key password environment variable')
    args = parser.parse_args(argv)
    try:
        password = os.environ[args.password_env].encode() if args.password_env else None
        result = build(args.source, read_json(args.descriptor), args.key, args.key_id,
                       read_json(args.trust), args.out, args.platform, password)
        print(json.dumps(result))
        return 0
    except (ReleaseError, ValueError, TypeError, KeyError, OSError, ImportError, subprocess.CalledProcessError):
        # No key material, password, private paths or command diagnostics in output.
        print(json.dumps({'state': 'build-failed', 'published': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
