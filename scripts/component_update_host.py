"""Host-only release discovery and verified caching; this module never applies files."""

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from itertools import islice
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from urllib.error import URLError

try:
    from . import component_release as release
except ImportError:  # Direct script execution places scripts/ on sys.path.
    import component_release as release


STATE_SCHEMA = 1
ENTRY_SCHEMA = 1
STATE_NAME = 'state.json'
VERIFIED_NAME = 'verified'
ENTRY_NAME = 'entry.json'
MANIFEST_NAME = 'manifest.json'
BUNDLE_NAME = 'bundle.zip'
MAX_ENTRY_BYTES = release.MAX_TOTAL + release.MAX_MANIFEST
MAX_CACHE_ENTRIES = 32
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_PLATFORM = 'windows-x86_64'


@dataclass(frozen=True)
class VerifiedEntry:
    release_id: str
    payload: dict
    envelope: dict
    bundle_path: Path
    downloaded: bool = True
    applied: bool = False


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _reason(error):
    # Network/filesystem diagnostics can contain signed URLs or private paths.
    if isinstance(error, URLError):
        return 'Release network unavailable; check the configured source and connection'
    if isinstance(error, OSError):
        return 'Release filesystem unavailable; check the managed cache'
    if isinstance(error, KeyboardInterrupt):
        return 'Release operation interrupted'
    return (str(error).strip() or type(error).__name__)[:500]


def _is_reparse(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, 'st_file_attributes', 0) & reparse_flag
    )


def _reject_reparse_components(path):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if _is_reparse(current):
            raise release.ReleaseError('Cache paths cannot contain symlinks or junctions')


def _ensure_directory(path):
    path = Path(os.path.abspath(path))
    _reject_reparse_components(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            details = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            details = current.lstat()
        if _is_reparse(current) or not stat.S_ISDIR(details.st_mode):
            raise release.ReleaseError('Cache path must contain ordinary directories')
    _reject_reparse_components(path)


def _regular_file(path, limit):
    if _is_reparse(path):
        raise release.ReleaseError('Cache files cannot be symlinks or junctions')
    try:
        details = path.lstat()
    except OSError as error:
        raise release.ReleaseError('Cache entry file is unavailable') from error
    if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
        raise release.ReleaseError('Cache entry file is invalid or exceeds its limit')
    return details


def _record_fields(record, names, message):
    if not isinstance(record, dict) or set(record) != set(names):
        raise release.ReleaseError(message)


def _digest(path, limit):
    _regular_file(path, limit)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


class HostCache:
    """Use fixed host-local config/cache paths; URLs and trust never come from UI calls."""

    def __init__(self, config, cache, platform):
        if not isinstance(platform, str) or not platform:
            raise release.ReleaseError('A target platform is required')
        self.config_path = Path(os.path.abspath(os.fspath(config)))
        self.cache_path = Path(os.path.abspath(os.fspath(cache)))
        self.platform = platform
        self.verified_path = self.cache_path / VERIFIED_NAME

    def _ensure_cache(self):
        _ensure_directory(self.cache_path)
        _ensure_directory(self.verified_path)
        self._guard_cache()

    def _guard_cache(self):
        _reject_reparse_components(self.cache_path)
        _reject_reparse_components(self.verified_path)
        if not self.cache_path.is_dir() or not self.verified_path.is_dir():
            raise release.ReleaseError('Managed cache directories are unavailable')

    def _config(self, trust):
        _reject_reparse_components(self.config_path)
        _regular_file(self.config_path, release.MAX_MANIFEST)
        config = release.read_json(self.config_path)
        _record_fields(config, {'schema', 'manifest_url', 'bundle_url'},
                       'Unexpected or missing release config fields')
        if type(config['schema']) is not int or config['schema'] != 1:
            raise release.ReleaseError('Unsupported release config schema')
        if not isinstance(config['manifest_url'], str) or not isinstance(config['bundle_url'], str):
            raise release.ReleaseError('Release config URLs must be strings')
        release.validate_download_url(config['manifest_url'], trust)
        release.validate_download_url(config['bundle_url'], trust)
        return config

    def _read_state(self):
        self._guard_cache()
        path = self.cache_path / STATE_NAME
        if not path.exists() and not _is_reparse(path):
            return {'schema': STATE_SCHEMA, 'last_check': None, 'last_failure': None}
        _regular_file(path, release.MAX_MANIFEST)
        state = release.read_json(path)
        _record_fields(state, {'schema', 'last_check', 'last_failure'},
                       'Invalid update cache status')
        if type(state['schema']) is not int or state['schema'] != STATE_SCHEMA:
            raise release.ReleaseError('Unsupported update cache status schema')
        if state['last_check'] is not None and not isinstance(state['last_check'], dict):
            raise release.ReleaseError('Invalid last check metadata')
        if state['last_failure'] is not None and not isinstance(state['last_failure'], dict):
            raise release.ReleaseError('Invalid last failure metadata')
        return state

    def _write_state(self, state):
        self._guard_cache()
        data = release.canonical(state)
        if len(data) > release.MAX_MANIFEST:
            raise release.ReleaseError('Update cache status exceeds size limit')
        state_path = self.cache_path / STATE_NAME
        if _is_reparse(state_path):
            raise release.ReleaseError('Cache status cannot be a symlink or junction')
        if state_path.exists():
            _regular_file(state_path, release.MAX_MANIFEST)
        descriptor, name = tempfile.mkstemp(prefix='.state-', suffix='.tmp', dir=self.cache_path)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._guard_cache()
            os.replace(temporary, state_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _record_failure(self, operation, when, error):
        state = self._read_state()
        message = _reason(error)
        if operation == 'check':
            state['last_check'] = {
                'state': 'unavailable',
                'checked_at': when,
                'error': message,
            }
        state['last_failure'] = {
            'operation': operation,
            'failed_at': when,
            'reason': message,
        }
        self._write_state(state)

    def _entry_record(self, directory, verify_bytes=True):
        _reject_reparse_components(directory)
        if _is_reparse(directory) or not directory.is_dir():
            raise release.ReleaseError('Cached release directory is unsafe')
        if {item.name for item in islice(directory.iterdir(), 4)} != {
                ENTRY_NAME, MANIFEST_NAME, BUNDLE_NAME}:
            raise release.ReleaseError('Cached release contains unexpected files')
        entry_path = directory / ENTRY_NAME
        manifest = directory / MANIFEST_NAME
        bundle = directory / BUNDLE_NAME
        _regular_file(entry_path, release.MAX_MANIFEST)
        record = release.read_json(entry_path)
        _record_fields(record, {'schema', 'release_id', 'manifest_sha256',
                                'bundle_sha256', 'staged_at'},
                       'Invalid cached release metadata')
        if (type(record['schema']) is not int or record['schema'] != ENTRY_SCHEMA
                or record['release_id'] != directory.name
                or not isinstance(record['staged_at'], str)
                or not isinstance(record['manifest_sha256'], str)
                or not release.HASH.fullmatch(record['manifest_sha256'])
                or not isinstance(record['bundle_sha256'], str)
                or not release.HASH.fullmatch(record['bundle_sha256'])):
            raise release.ReleaseError('Cached release metadata does not match its directory')
        _regular_file(manifest, release.MAX_MANIFEST)
        _regular_file(bundle, MAX_ENTRY_BYTES)
        if verify_bytes and (
                _digest(manifest, release.MAX_MANIFEST) != record['manifest_sha256']
                or _digest(bundle, MAX_ENTRY_BYTES) != record['bundle_sha256']):
            raise release.ReleaseError('Cached release bytes changed after staging')
        return record, manifest, bundle

    def _cached_records(self):
        self._guard_cache()
        directories = list(islice(self.verified_path.iterdir(), MAX_CACHE_ENTRIES + 1))
        if len(directories) > MAX_CACHE_ENTRIES:
            raise release.ReleaseError('Managed cache entry limit exceeded; remove reviewed old releases explicitly')
        total = 0
        for directory in directories:
            if (not release.ID.fullmatch(directory.name)
                    or _is_reparse(directory) or not directory.is_dir()):
                raise release.ReleaseError('Unexpected object in managed release cache')
            for name, limit in ((ENTRY_NAME, release.MAX_MANIFEST),
                                (MANIFEST_NAME, release.MAX_MANIFEST), (BUNDLE_NAME, MAX_ENTRY_BYTES)):
                path = directory / name
                _regular_file(path, limit)
                total += path.stat().st_size
                if total > MAX_CACHE_BYTES:
                    raise release.ReleaseError('Managed cache byte limit exceeded; remove reviewed old releases explicitly')
        records = [self._entry_record(directory)[0] for directory in directories]
        return sorted(records, key=lambda record: record['release_id'])

    @contextmanager
    def _commit_lock(self):
        self._guard_cache()
        path = self.cache_path / '.commit.lock'
        _reject_reparse_components(path)
        with path.open('a+b') as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise release.ReleaseError('Another release commit is active; retry later') from error
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise release.ReleaseError('Another release commit is active; retry later') from error
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _stage_fits(self, staging):
        records = self._cached_records()
        if len(records) >= MAX_CACHE_ENTRIES:
            raise release.ReleaseError('Managed cache is full; remove reviewed old releases explicitly')
        total = sum((self.verified_path / record['release_id'] / name).stat().st_size
                    for record in records for name in (ENTRY_NAME, MANIFEST_NAME, BUNDLE_NAME))
        total += sum((staging / name).stat().st_size for name in (ENTRY_NAME, MANIFEST_NAME, BUNDLE_NAME))
        if total > MAX_CACHE_BYTES:
            raise release.ReleaseError('Managed cache is full; remove reviewed old releases explicitly')

    def status(self):
        """Read persisted discovery/failure state and staged-entry metadata."""
        _reject_reparse_components(self.cache_path)
        if not self.cache_path.exists():
            return {
                'schema': STATE_SCHEMA,
                'last_check': None,
                'last_failure': None,
                'cached_releases': [],
            }
        state = self._read_state()
        return {
            'schema': STATE_SCHEMA,
            'last_check': state['last_check'],
            'last_failure': state['last_failure'],
            'cached_releases': self._cached_records(),
        }

    def check(self, *, trust, project_map):
        """Verify compatible signed metadata only; this does not download the bundle."""
        when = _now()
        try:
            if not isinstance(trust, Mapping) or not isinstance(project_map, Mapping):
                raise release.ReleaseError('Local trust and project map are required')
            self._ensure_cache()
            config = self._config(dict(trust))
            _envelope, payload, manifest_hash = release.fetch_manifest_verified(
                config['manifest_url'], dict(trust), self.platform, dict(project_map)
            )
            record_by_id = {
                record['release_id']: record for record in self._cached_records()
            }
            cached = record_by_id.get(payload['release_id'])
            if cached and cached['manifest_sha256'] != manifest_hash:
                raise release.ReleaseError('Release ID is already cached with different bytes')
            result = {
                'state': 'verified',
                'checked_at': when,
                'release_id': payload['release_id'],
                'source_revision': payload['source_revision'],
                'manifest_sha256': manifest_hash,
                'components': [
                    {'id': component['id'], 'version': component['version']}
                    for component in payload['components']
                ],
                'payload': payload,
                'downloaded': False,
                'applied': False,
            }
            state = self._read_state()
            state['last_check'] = {
                key: value for key, value in result.items() if key != 'payload'
            }
            state['last_failure'] = None
            self._write_state(state)
            return result
        except (release.ReleaseError, OSError, ValueError, TypeError, KeyboardInterrupt) as error:
            try:
                self._record_failure('check', when, error)
            except (release.ReleaseError, OSError, ValueError, TypeError):
                pass
            return {'state': 'unavailable', 'checked_at': when, 'error': _reason(error)}

    def _verified_entry(self, directory, trust, project_map):
        record, manifest, bundle = self._entry_record(directory)
        envelope = release.read_json(manifest)
        payload = release.verify_bundle(
            envelope, bundle, dict(trust), self.platform, dict(project_map)
        )
        if payload['release_id'] != record['release_id']:
            raise release.ReleaseError('Cached release ID does not match its signed manifest')
        return VerifiedEntry(
            release_id=payload['release_id'],
            payload=payload,
            envelope=envelope,
            bundle_path=bundle,
        )

    def verified_entries(self, *, trust, project_map):
        """Reverify every staged entry with current local trust and project versions."""
        if not isinstance(trust, Mapping) or not isinstance(project_map, Mapping):
            raise release.ReleaseError('Local trust and project map are required')
        return [
            self._verified_entry(self.verified_path / record['release_id'], trust, project_map)
            for record in self._cached_records()
        ]

    def stage(self, release_id, *, trust, project_map):
        """Download, verify and atomically cache one release; never extract or apply."""
        when = _now()
        staging = None
        commit_attempted = False
        final = None
        manifest_hash = bundle_hash = None
        try:
            if (not isinstance(release_id, str) or not release.ID.fullmatch(release_id)
                    or not isinstance(trust, Mapping) or not isinstance(project_map, Mapping)):
                raise release.ReleaseError('A release ID, local trust and project map are required')
            self._ensure_cache()
            config = self._config(dict(trust))
            state = self._read_state()
            last = state['last_check']
            if (not isinstance(last, dict) or last.get('state') != 'verified'
                    or last.get('release_id') != release_id):
                raise release.ReleaseError('Stage requires a matching successful host check')

            record_by_id = {
                record['release_id']: record for record in self._cached_records()
            }
            cached = record_by_id.get(release_id)
            if cached and cached['manifest_sha256'] != last.get('manifest_sha256'):
                raise release.ReleaseError('Release ID is already cached with different bytes')

            staging = Path(tempfile.mkdtemp(prefix='.stage-', dir=self.cache_path))
            payload = release.fetch_verified(
                config['manifest_url'], config['bundle_url'], dict(trust),
                self.platform, dict(project_map), preserve_to=staging,
            )
            if payload['release_id'] != release_id:
                raise release.ReleaseError('Downloaded release does not match the checked release')

            manifest = staging / MANIFEST_NAME
            bundle = staging / BUNDLE_NAME
            manifest_hash = _digest(manifest, release.MAX_MANIFEST)
            bundle_hash = _digest(bundle, MAX_ENTRY_BYTES)
            if manifest_hash != last.get('manifest_sha256'):
                raise release.ReleaseError('Release manifest changed after the host check')

            final = self.verified_path / release_id
            if cached:
                if cached['bundle_sha256'] != bundle_hash:
                    raise release.ReleaseError('Release ID is already cached with different bytes')
                return self._verified_entry(final, trust, project_map)

            if _is_reparse(final) or final.exists():
                raise release.ReleaseError('Release cache destination already exists')
            entry_metadata = {
                'schema': ENTRY_SCHEMA,
                'release_id': release_id,
                'manifest_sha256': manifest_hash,
                'bundle_sha256': bundle_hash,
                'staged_at': when,
            }
            with (staging / ENTRY_NAME).open('xb') as stream:
                stream.write(release.canonical(entry_metadata))
            envelope = release.read_json(manifest)
            entry = VerifiedEntry(
                release_id=release_id,
                payload=payload,
                envelope=envelope,
                bundle_path=final / BUNDLE_NAME,
            )
            self._guard_cache()
            if _is_reparse(final) or final.exists():
                raise release.ReleaseError('Release cache destination already exists')
            with self._commit_lock():
                self._stage_fits(staging)
                commit_attempted = True
                os.rename(staging, final)
                staging = None
            return entry
        except (release.ReleaseError, OSError, ValueError, TypeError, KeyboardInterrupt) as error:
            try:
                self._record_failure('stage', when, error)
            except (release.ReleaseError, OSError, ValueError, TypeError):
                pass
            if isinstance(error, OSError):
                raise release.ReleaseError(_reason(error)) from error
            raise
        finally:
            if staging is not None:
                try:
                    _reject_reparse_components(staging)
                    if staging.parent == self.cache_path and staging.is_dir():
                        shutil.rmtree(staging)
                    elif commit_attempted and final is not None and final.is_dir():
                        record, _, _ = self._entry_record(final)
                        if (record['release_id'] == release_id
                                and record['manifest_sha256'] == manifest_hash
                                and record['bundle_sha256'] == bundle_hash):
                            shutil.rmtree(final)
                except FileNotFoundError:
                    pass


def _load_json(path, label):
    try:
        value = release.read_json(path)
    except (OSError, release.ReleaseError, ValueError) as error:
        raise release.ReleaseError(f'Unable to read {label}') from error
    if not isinstance(value, dict):
        raise release.ReleaseError(f'{label} must be a JSON object')
    return value


def _add_host_args(parser):
    parser.add_argument('--config', required=True, help='Host-local source URL config')
    parser.add_argument('--cache', required=True, help='Explicit managed cache directory')
    parser.add_argument('--platform', default=DEFAULT_PLATFORM,
                        help=f'Target platform identifier (default: {DEFAULT_PLATFORM})')
    parser.add_argument('--trust', required=True, help='Host-local pinned publisher policy')
    parser.add_argument('--project-map', required=True, help='Host-local component version map')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check_parser = commands.add_parser('check', help='Verify release metadata only')
    _add_host_args(check_parser)
    stage_parser = commands.add_parser('stage', help='Download and verify one release')
    _add_host_args(stage_parser)
    stage_parser.add_argument('release_id')
    status_parser = commands.add_parser('status', help='Read persisted host cache metadata')
    status_parser.add_argument('--cache', required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            result = HostCache('', args.cache, 'status').status()
            print(json.dumps(result, sort_keys=True))
            return 0
        trust = _load_json(args.trust, 'publisher policy')
        project_map = _load_json(args.project_map, 'project map')
        host = HostCache(args.config, args.cache, args.platform)
        if args.command == 'check':
            result = host.check(trust=trust, project_map=project_map)
            print(json.dumps({key: value for key, value in result.items() if key != 'payload'}, sort_keys=True))
            return 0 if result['state'] == 'verified' else 2
        check = host.status()['last_check']
        if not isinstance(check, dict) or check.get('state') != 'verified':
            check = host.check(trust=trust, project_map=project_map)
        if check.get('state') != 'verified' or check.get('release_id') != args.release_id:
            print(json.dumps({'state': 'unavailable', 'error': 'a matching successful host check is required'}))
            return 2
        entry = host.stage(args.release_id, trust=trust, project_map=project_map)
        print(json.dumps({
            'state': 'staged',
            'release_id': entry.release_id,
            'components': [
                {'id': item['id'], 'version': item['version']}
                for item in entry.payload['components']
            ],
            'downloaded': True,
            'applied': False,
        }, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({'state': 'unavailable', 'error': 'interrupted'}))
        return 130
    except (release.ReleaseError, OSError, ValueError, TypeError) as error:
        print(json.dumps({'state': 'unavailable', 'error': _reason(error)}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
