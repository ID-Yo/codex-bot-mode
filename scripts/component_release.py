"""Verify signed CBM component releases without changing an installation.

CLI: component_release.py manifest.json bundle.zip --trust publisher.json
     --platform windows-x86_64 [--installed versions.json]
Requires cryptography only for Ed25519 verification. No network or install hooks.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener
import zipfile

MAX_MANIFEST = 1024 * 1024
MAX_FILES = 4096
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
ENGINE_VERSION = 1
PROFILE_PATHS = {'agent/BASE.md', 'agent/SOUL.md', 'agent/PROFILE.md', 'agent/PLAYBOOK.md'}
ID = re.compile(r'[a-z0-9][a-z0-9-]{0,79}\Z')
HASH = re.compile(r'[a-f0-9]{64}\Z')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


class ReleaseError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError('Duplicate JSON key')
        result[key] = value
    return result


def read_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise ReleaseError('Manifest or policy exceeds size limit')
    try:
        return json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ReleaseError('Nonfinite JSON')))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseError('Invalid JSON') from error


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ReleaseError('Unexpected or missing manifest fields')


def _version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ReleaseError('Version must be a numeric major.minor.patch')
    return tuple(map(int, value.split('.')))


def safe_path(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ReleaseError('Invalid package path')
    path = PurePosixPath(value)
    if path.is_absolute() or '\\' in value or ':' in value or str(path) != value:
        raise ReleaseError('Unsafe package path')
    for part in path.parts:
        stem = part.split('.')[0].upper()
        if part in ('.', '..') or part.endswith((' ', '.')) or any(ord(c) < 32 or c in '<>"|?*' for c in part):
            raise ReleaseError('Unsafe package path')
        if stem in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} or re.fullmatch(r'(COM|LPT)[1-9¹²³]', stem):
            raise ReleaseError('Reserved Windows package path')
    return value


def _allowed(cid, kind, path, policy):
    if kind == 'profile':
        return path in PROFILE_PATHS
    if kind == 'reference':
        return path in {'README.md', 'index.yaml'} or (
            path.startswith(('methods/', 'templates/')) and path.endswith('.md')
            and not any(p.startswith('.') or p.casefold() in {'memory', 'state.md', 'owner.md', 'decisions.md', 'approvals.md'}
                        for p in PurePosixPath(path).parts))
    # Executable components need a separately approved local file allowlist.
    return path in policy.get('component_paths', {}).get(cid, [])


def verify_manifest(envelope, trust, platform, installed=None, now=None):
    """Return an authenticated compatible payload; trust is local, never from bundle."""
    _fields(envelope, {'payload', 'key_id', 'signature'})
    if not isinstance(trust, dict) or type(trust.get('schema')) is not int or trust.get('schema') != 1:
        raise ReleaseError('Unsupported local publisher policy')
    keys = trust.get('keys')
    revoked = trust.get('revoked', [])
    owned = trust.get('component_paths', {})
    hosts = trust.get('download_hosts', [])
    if (not isinstance(keys, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items())
            or not isinstance(revoked, list) or not all(isinstance(k, str) for k in revoked)
            or not isinstance(owned, dict) or not all(isinstance(k, str) and isinstance(v, list)
                and all(isinstance(p, str) for p in v) for k, v in owned.items())
            or not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts)):
        raise ReleaseError('Invalid local publisher policy fields')
    key_id = envelope['key_id']
    if not isinstance(key_id, str) or key_id in trust.get('revoked', []):
        raise ReleaseError('Publisher key is revoked or invalid')
    key = trust.get('keys', {}).get(key_id)
    if not key:
        raise ReleaseError('Publisher key is not trusted')
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True)).verify(
            base64.b64decode(envelope['signature'], validate=True), canonical(envelope['payload']))
    except ImportError as error:
        raise ReleaseError('Install the pinned update verifier dependency before checking releases') from error
    except Exception as error:
        raise ReleaseError('Publisher signature verification failed') from error
    payload = envelope['payload']
    _fields(payload, {'schema', 'release_id', 'source_revision', 'published_at', 'expires_at',
                      'minimum_engine', 'changelog', 'components'})
    if type(payload['schema']) is not int or payload['schema'] != 1 or type(payload['minimum_engine']) is not int or not 1 <= payload['minimum_engine'] <= ENGINE_VERSION:
        raise ReleaseError('Incompatible release schema or update engine')
    if not isinstance(payload['release_id'], str) or not ID.fullmatch(payload['release_id']):
        raise ReleaseError('Invalid immutable release identifier')
    if not isinstance(payload['source_revision'], str) or not re.fullmatch(r'[a-f0-9]{40}', payload['source_revision']):
        raise ReleaseError('Release needs an immutable public source revision')
    try:
        start = datetime.fromisoformat(payload['published_at'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00'))
        current = now or datetime.now(timezone.utc)
        if start.tzinfo is None or end.tzinfo is None or not start <= current < end:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise ReleaseError('Release freshness is expired, future or unknown') from error
    if not isinstance(payload['changelog'], str) or not payload['changelog'].strip():
        raise ReleaseError('Release changelog is required')
    components = payload['components']
    if not isinstance(components, list) or not 1 <= len(components) <= 128:
        raise ReleaseError('Invalid component list')
    installed = {} if installed is None else installed
    if not isinstance(installed, dict) or not all(isinstance(k, str) for k in installed):
        raise ReleaseError('Invalid installed version map')
    for value in installed.values():
        _version(value)
    proposed, entries, total = {}, set(), 0
    for component in components:
        _fields(component, {'id', 'kind', 'version', 'platforms', 'requires', 'files'})
        cid, kind = component['id'], component['kind']
        if not isinstance(cid, str) or not ID.fullmatch(cid) or cid in proposed:
            raise ReleaseError('Invalid or duplicate component identity')
        if not isinstance(kind, str) or kind not in {'profile', 'reference', 'tool', 'application'}:
            raise ReleaseError('Unsupported component kind')
        version = _version(component['version'])
        if cid in installed and version < _version(installed[cid]):
            raise ReleaseError('Downgrade needs a separate reviewed rollback plan')
        platforms = component['platforms']
        if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms) or not ({'any', platform} & set(platforms)):
            raise ReleaseError('Unsupported component platform')
        if not isinstance(component['requires'], dict):
            raise ReleaseError('Invalid compatibility requirements')
        files = component['files']
        if not isinstance(files, dict) or not files:
            raise ReleaseError('Component files are required')
        for path, spec in files.items():
            safe_path(path)
            if not _allowed(cid, kind, path, trust):
                raise ReleaseError('Path is not owned by this component kind')
            _fields(spec, {'sha256', 'size'})
            if not isinstance(spec['sha256'], str) or not HASH.fullmatch(spec['sha256']):
                raise ReleaseError('Invalid file checksum')
            if type(spec['size']) is not int or not 0 <= spec['size'] <= MAX_FILE:
                raise ReleaseError('Invalid file size')
            entry = (cid + '/' + path).casefold()
            if entry in entries:
                raise ReleaseError('Case-colliding package path')
            entries.add(entry)
            total += spec['size']
        proposed[cid] = component['version']
    if len(entries) > MAX_FILES or total > MAX_TOTAL:
        raise ReleaseError('Package exceeds file or byte limits')
    available = {**installed, **proposed}
    for component in components:
        for cid, version in component['requires'].items():
            _version(version)
            if available.get(cid) != version:
                raise ReleaseError('Missing or incompatible component dependency')
    return payload


def verify_bundle(envelope, bundle, trust, platform, installed=None, now=None):
    """Verify all ZIP entries without extraction, hooks or any installation writes."""
    payload = verify_manifest(envelope, trust, platform, installed, now)
    expected = {c['id'] + '/' + path: spec for c in payload['components'] for path, spec in c['files'].items()}
    try:
        with zipfile.ZipFile(bundle) as archive:
            infos = archive.infolist()
            if len(infos) != len(expected) or len({i.filename.casefold() for i in infos}) != len(infos):
                raise ReleaseError('Duplicate, extra or missing archive files')
            for info in infos:
                safe_path(info.filename)
                mode = info.external_attr >> 16
                if info.filename not in expected or info.is_dir() or stat.S_ISLNK(mode) or info.flag_bits & 1:
                    raise ReleaseError('Unsupported archive entry')
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ReleaseError('Archive special files are forbidden')
                spec = expected[info.filename]
                if info.file_size != spec['size'] or info.file_size > MAX_FILE:
                    raise ReleaseError('Archive size mismatch')
                digest = hashlib.sha256()
                seen = 0
                with archive.open(info) as stream:
                    while chunk := stream.read(65536):
                        seen += len(chunk)
                        if seen > spec['size']:
                            raise ReleaseError('Expanded archive exceeds declared size')
                        digest.update(chunk)
                if seen != spec['size'] or digest.hexdigest() != spec['sha256']:
                    raise ReleaseError('Archive checksum mismatch')
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ReleaseError('Invalid or unsupported archive') from error
    return payload


def validate_download_url(url, trust):
    """Reject non-HTTPS or non-pinned release hosts before opening a connection."""
    hosts = trust.get('download_hosts') if isinstance(trust, dict) else None
    if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
        raise ReleaseError('Invalid download host policy')
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme == 'https' and parsed.hostname in hosts
                 and not parsed.username and not parsed.password
                 and parsed.port in (None, 443))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ReleaseError('Release download host is not approved')


def _release_downloader(trust):
    if not isinstance(trust, dict) or not isinstance(trust.get('download_hosts'), list):
        raise ReleaseError('Invalid download host policy')

    def approved(url):
        validate_download_url(url, trust)

    class ApprovedRedirects(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            approved(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = build_opener(ApprovedRedirects())

    def download(url, destination, limit):
        approved(url)
        with opener.open(url, timeout=15) as response:
            approved(response.geturl())
            total = 0
            while chunk := response.read(65536):
                total += len(chunk)
                if total > limit:
                    raise ReleaseError('Release download exceeds size limit')
                destination.write(chunk)

    return download


def fetch_manifest_verified(manifest_url, trust, platform, installed=None):
    """Fetch and verify one pinned manifest without downloading its bundle."""
    download = _release_downloader(trust)
    with tempfile.TemporaryDirectory(prefix='cbm-release-') as tmp:
        manifest = Path(tmp) / 'manifest.json'
        with manifest.open('wb') as stream:
            download(manifest_url, stream, MAX_MANIFEST)
        raw = manifest.read_bytes()
        envelope = read_json(manifest)
        payload = verify_manifest(envelope, trust, platform, installed)
        return envelope, payload, hashlib.sha256(raw).hexdigest()


def fetch_verified(manifest_url, bundle_url, trust, platform, installed=None,
                   preserve_to=None):
    """Fetch and verify a release pair; optionally copy verified assets to a fresh
    host-owned staging directory. Existing callers retain scratch-only behavior.
    """
    download = _release_downloader(trust)

    with tempfile.TemporaryDirectory(prefix='cbm-release-') as tmp:
        manifest = Path(tmp) / 'manifest.json'
        bundle = Path(tmp) / 'bundle.zip'
        with manifest.open('wb') as stream:
            download(manifest_url, stream, MAX_MANIFEST)
        envelope = read_json(manifest)
        verify_manifest(envelope, trust, platform, installed)
        with bundle.open('wb') as stream:
            download(bundle_url, stream, MAX_TOTAL + MAX_MANIFEST)
        payload = verify_bundle(envelope, bundle, trust, platform, installed)

        if preserve_to is not None:
            target = Path(preserve_to)
            junction = getattr(target, 'is_junction', lambda: False)
            if target.is_symlink() or junction() or not target.is_dir():
                raise ReleaseError('Verified download destination must be an owned directory')
            resolved_target = target.resolve()
            resolved_scratch = Path(tmp).resolve()
            if resolved_target == resolved_scratch or resolved_scratch in resolved_target.parents:
                raise ReleaseError('Verified download destination cannot be temporary scratch')
            if any(target.iterdir()):
                raise ReleaseError('Verified download destination must be empty')
            shutil.copyfile(manifest, target / 'manifest.json')
            shutil.copyfile(bundle, target / 'bundle.zip')
        return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('bundle')
    parser.add_argument('--trust', required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--installed')
    args = parser.parse_args(argv)
    try:
        payload = verify_bundle(read_json(args.manifest), args.bundle, read_json(args.trust),
                                args.platform, read_json(args.installed) if args.installed else {})
        print(json.dumps({'state': 'verified', 'release_id': payload['release_id'],
                          'components': [c['id'] for c in payload['components']], 'applied': False}))
        return 0
    except (ReleaseError, OSError, ValueError) as error:
        print(json.dumps({'state': 'unavailable', 'error': str(error), 'applied': False}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
