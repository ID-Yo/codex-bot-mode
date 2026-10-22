"""Plan and apply verified profile/reference releases transactionally.

This module changes project files only after verifying the complete signed
bundle. It never activates a profile or executes package content.
"""
import argparse
import ctypes
from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import component_release as release  # noqa: E402
from team_room import manifest  # noqa: E402

SCHEMA = 1
MAX_INVENTORY = 2 * 1024 * 1024
HASH = re.compile(r'[a-f0-9]{64}\Z')
PLACEHOLDER = re.compile(r'\{\{([a-z_]+)\}\}')
PROFILE_FILES = {'agent/BASE.md', 'agent/SOUL.md', 'agent/PROFILE.md', 'agent/PLAYBOOK.md'}
MAX_BUNDLE_BYTES = release.MAX_TOTAL + 64 * 1024 * 1024
MAX_BACKUP_BYTES = 2 * release.MAX_TOTAL


class UpdateError(ValueError):
    pass


class InventoryCASMismatch(UpdateError):
    pass


class SimulatedCrash(BaseException):
    """Test-only interruption that deliberately leaves a recoverable journal."""


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _snapshot_json(value, label):
    """Detach caller-owned JSON data before an approval check or callback gap."""
    try:
        return json.loads(canonical(value), object_pairs_hook=release._unique)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise UpdateError(f'{label} must be finite JSON data') from error


def _acquire_update_lock(root, lock):
    """Create a Windows lock file deleted automatically when its handle closes."""
    if os.name != 'nt':
        raise UpdateError('Profile-update locking is supported on Windows only')
    _check_chain(root, lock.relative_to(root).as_posix())
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(str(lock), 0xC0010000, 0, None, 1, 0x04000080, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error in (80, 183):  # already exists
            raise UpdateError(f'Another profile update or inventory write is active; lock {lock.name} exists')
        raise UpdateError(f'Cannot create update lock {lock.name} (Windows error {error})')
    try:
        import msvcrt
        return msvcrt.open_osfhandle(handle, os.O_RDWR)
    except Exception as error:
        kernel32.CloseHandle(handle)
        raise UpdateError(f'Cannot hold update lock {lock.name}') from error


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(path, limit=MAX_INVENTORY):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > limit:
            raise UpdateError('JSON file exceeds size limit')
        return json.loads(raw, object_pairs_hook=release._unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(UpdateError('Nonfinite JSON')))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UpdateError(f'Cannot read JSON: {path}') from error


def _linked(path):
    return path.is_symlink() or (path.exists() and bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400))


def _safe_rel(value):
    release.safe_path(value)
    return PurePosixPath(value)


def _contained(root, path):
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (ValueError, OSError):
        return False


def _check_chain(root, relative, allow_missing_leaf=True):
    """Reject junctions/symlinks at every existing segment under project root."""
    rel = _safe_rel(relative)
    cursor = root
    for index, part in enumerate(rel.parts):
        cursor = cursor / part
        if _linked(cursor):
            raise UpdateError(f'Linked target path is forbidden: {relative}')
        if cursor.exists():
            if not _contained(root, cursor):
                raise UpdateError(f'Target escapes project root: {relative}')
            if index < len(rel.parts) - 1 and not cursor.is_dir():
                raise UpdateError(f'Target ancestor is not a directory: {relative}')
        elif not allow_missing_leaf and index == len(rel.parts) - 1:
            raise UpdateError(f'Target is missing: {relative}')
    return root.joinpath(*rel.parts)


def _inventory(project):
    root = Path(project).resolve(strict=True)
    file = root / '.bot-mode' / 'local' / 'installations.json'
    _check_chain(root, '.bot-mode/local/installations.json')
    if not file.is_file():
        raise UpdateError('Schema 1 installation inventory is required; legacy targets need manual review')
    raw = file.read_bytes()
    if len(raw) > MAX_INVENTORY:
        raise UpdateError('Installation inventory exceeds size limit')
    data = _json(file)
    if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1 or not isinstance(data.get('components'), list):
        raise UpdateError('Unsupported installation inventory schema')
    ids = set()
    for component in data['components']:
        if not isinstance(component, dict):
            raise UpdateError('Malformed inventory component')
        required = {'id', 'kind', 'path', 'source', 'source_revision', 'version',
                    'component_schema', 'owned_paths', 'baseline'}
        allowed = required | {'unsafe_paths'}
        if component.get('kind') == 'shared_reference':
            allowed.add('dependents')
        if set(component) - allowed or required - set(component):
            raise UpdateError('Inventory component does not match schema 1')
        if 'dependents' in component:
            roles = component['dependents']
            if (component.get('kind') != 'shared_reference' or not isinstance(roles, list)
                    or any(not isinstance(role, str) or not role for role in roles)
                    or len(roles) != len(set(roles))):
                raise UpdateError(f"{component.get('id', 'component')}: invalid shared-reference dependents")
        if component.get('unsafe_paths'):
            raise UpdateError(f"{component.get('id', 'component')}: unsafe inventory paths require manual review")
        cid, kind, path = component.get('id'), component.get('kind'), component.get('path')
        if not isinstance(cid, str) or cid in ids or kind not in ('profile', 'shared_reference', 'tool'):
            raise UpdateError('Invalid or duplicate inventory component')
        ids.add(cid)
        if not isinstance(path, str):
            raise UpdateError(f'{cid}: invalid managed root')
        if path != '.':
            _safe_rel(path)
        owned, baseline = component.get('owned_paths'), component.get('baseline')
        if not isinstance(owned, list) or not isinstance(baseline, dict):
            raise UpdateError(f'{cid}: invalid owned paths or baseline')
        if any(not isinstance(p, str) or not p for p in owned) or len(set(owned)) != len(owned):
            raise UpdateError(f'{cid}: invalid or duplicate owned path')
        for rel in owned:
            _safe_rel(rel)
            if path != '.' and rel != path and not rel.startswith(path.rstrip('/') + '/'):
                raise UpdateError(f'{cid}: owned path falls outside its managed root')
        for rel, item in baseline.items():
            _safe_rel(rel)
            if rel not in owned or not isinstance(item, dict) or item.get('state') not in ('known', 'unknown'):
                raise UpdateError(f'{cid}: malformed baseline entry')
            if item['state'] == 'known':
                if set(item) != {'state', 'sha256'} or not isinstance(item.get('sha256'), str) or not HASH.fullmatch(item['sha256']):
                    raise UpdateError(f'{cid}: malformed known baseline hash')
            elif set(item) - {'state', 'reason'}:
                raise UpdateError(f'{cid}: malformed unknown baseline')
        if not isinstance(component.get('version'), (str, int, type(None))) or isinstance(component.get('version'), bool):
            raise UpdateError(f'{cid}: invalid version')
        if not isinstance(component.get('source_revision'), (str, type(None))):
            raise UpdateError(f'{cid}: invalid source revision')
        if not isinstance(component.get('source'), (str, type(None))):
            raise UpdateError(f'{cid}: invalid source')
        revision = component.get('source_revision')
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}', revision)):
            raise UpdateError(f'{cid}: source revision must be an immutable commit hash')
        if type(component.get('component_schema')) is not int or component['component_schema'] < 1:
            raise UpdateError(f'{cid}: invalid component schema version')
    return root, file, data, digest(raw)


def _profile_marker_exists(root, relative):
    for name in ('agent.yaml', 'PROFILE.md'):
        if _check_chain(root, f'{relative}/{name}').is_file():
            return True
    return False


def _profile_layout(root, component):
    """Map the two explicit legacy layouts using project.profile_location rules."""
    inventory_root = component['path']
    if inventory_root == '.':
        _check_chain(root, 'agent')
        nested = root / 'agent'
        if not nested.is_dir() or not _profile_marker_exists(root, 'agent'):
            raise UpdateError('Root-export profile is not recognized by the canonical profile_location')
        return {'layout': 'root_export', 'root': 'agent', 'identity': 'agent/agent.yaml'}
    if inventory_root == 'agents/project-manager':
        _check_chain(root, inventory_root)
        _check_chain(root, inventory_root + '/agent')
        if not (root / inventory_root).is_dir():
            raise UpdateError('Legacy Manager profile directory is missing')
        nested = root / inventory_root / 'agent'
        if nested.is_dir() and _profile_marker_exists(root, inventory_root + '/agent'):
            return {'layout': 'workspace', 'root': inventory_root,
                    'identity': inventory_root + '/agent/agent.yaml'}
        return {'layout': 'flat_manager', 'root': inventory_root,
                'identity': inventory_root + '/agent.yaml'}
    if inventory_root.startswith(('team/', 'agents/')):
        return {'layout': 'workspace', 'root': inventory_root,
                'identity': inventory_root + '/agent/agent.yaml'}
    raise UpdateError('Profile target uses an unsupported managed root')


def _profile_file_path(layout, package_path):
    if layout['layout'] == 'root_export':
        return package_path
    if layout['layout'] == 'flat_manager':
        return f"{layout['root']}/{package_path.removeprefix('agent/')}"
    return f"{layout['root']}/{package_path}"


def _profile_package_path(layout, project_path):
    prefix = layout['root'].rstrip('/') + '/'
    if not project_path.startswith(prefix):
        return None
    relative = project_path[len(prefix):]
    if layout['layout'] == 'root_export' or layout['layout'] == 'flat_manager':
        relative = 'agent/' + relative
    return relative if relative in PROFILE_FILES else None


def _target_map(targets, inventory):
    if (not isinstance(targets, dict) or set(targets) != {'schema', 'targets'}
            or type(targets.get('schema')) is not int or targets['schema'] != 1
            or not isinstance(targets.get('targets'), dict)):
        raise UpdateError('Target map must be schema 1 with a targets object')
    by_id = {item['id']: item for item in inventory['components']}
    result = {}
    used = set()
    for release_id, inventory_id in targets['targets'].items():
        if not isinstance(release_id, str) or not isinstance(inventory_id, str) or inventory_id not in by_id:
            raise UpdateError('Target map references an unknown inventory component')
        if inventory_id in used:
            raise UpdateError('A managed component cannot be mapped more than once')
        used.add(inventory_id)
        component = by_id[inventory_id]
        if component['kind'] == 'tool':
            raise UpdateError('Tool and application components are outside this updater')
        path = component['path']
        if component['kind'] == 'shared_reference' and (not path.startswith('team/references/')
                or len(PurePosixPath(path).parts) != 3):
            raise UpdateError('Reference target map must name one team/references/<bundle> root')
        result[release_id] = component
    return result


def _profile_context(root, component, layout=None, include_answers=True):
    layout = layout or _profile_layout(root, component)
    metadata_rel = layout['identity']
    metadata_path = _check_chain(root, metadata_rel)
    metadata = None
    if metadata_path.exists():
        if not metadata_path.is_file():
            raise UpdateError(f'Profile identity metadata is not a regular file: {metadata_rel}')
        try:
            metadata = manifest.load(metadata_path)
        except Exception as error:
            raise UpdateError(f'Cannot read original profile answers: {metadata_rel}') from error
        if (not isinstance(metadata, dict) or not isinstance(metadata.get('bot_id'), str)
                or not metadata['bot_id'] or not isinstance(metadata.get('role'), str)
                or not metadata['role']):
            raise UpdateError(f'Profile identity metadata is incomplete: {metadata_rel}')
    elif layout['layout'] == 'workspace':
        raise UpdateError(f'Profile identity metadata is missing: {metadata_rel}')
    roster_file = root / '.bot-mode' / 'roster.json'
    participants = []
    if roster_file.exists() or _linked(roster_file):
        _check_chain(root, '.bot-mode/roster.json', allow_missing_leaf=False)
        roster_data = _json(roster_file)
        if not isinstance(roster_data, dict) or not isinstance(roster_data.get('participants'), list):
            raise UpdateError('Project roster has no valid participants list')
        participants = roster_data['participants']
    elif layout['layout'] != 'flat_manager':
        raise UpdateError('Project roster is required to identify this profile layout')
    participant = next((p for p in participants
                        if isinstance(p, dict) and p.get('workspace') == component['path']
                        and p.get('status') == 'active'), None)
    if layout['layout'] == 'flat_manager':
        active_manager = next((p for p in participants if isinstance(p, dict)
                               and p.get('status') == 'active' and p.get('bot') == 'manager'), None)
        if active_manager is not None and active_manager.get('workspace') != component['path']:
            raise UpdateError('Flat Manager path is not the profile_location selected by the active roster')
        if participant is not None:
            bot_id = metadata['bot_id'] if metadata else participant.get('bot_id') or participant.get('bot')
            role = participant.get('role')
            if metadata and (metadata['bot_id'] != participant.get('bot') or metadata['role'] != role):
                raise UpdateError('Flat Manager profile identity does not match its active roster participant')
        elif metadata:
            bot_id, role = metadata['bot_id'], metadata['role']
            if bot_id != 'project-manager':
                raise UpdateError('Unrostered flat Manager identity must match the canonical profile_location bot ID')
        else:
            bot_id, role = 'project-manager', 'Manager'
        if not isinstance(bot_id, str) or not bot_id or role != 'Manager':
            raise UpdateError('Flat Manager profile identity does not match canonical Manager role')
        team = participant.get('team') if participant else None
    else:
        if participant is None:
            raise UpdateError('Profile path is not recognized as the matching active project roster participant')
        bot_id, role = (metadata['bot_id'] if metadata else participant.get('bot_id') or participant['bot'],
                        participant['role'])
        if metadata and (participant['bot'] != metadata['bot_id'] or role != metadata['role']):
            raise UpdateError('Profile path is not recognized as the matching active project roster participant')
        team = participant.get('team')
    context = {'project': root.name, 'bot': bot_id, 'role': role,
               'host': ('.' if layout['layout'] == 'root_export' else
                        os.path.relpath(root, root / component['path']).replace('\\', '/')),
               'skill': f'codex-{bot_id}'}
    if team:
        context['team'] = team
    context_hashes = {}
    if metadata:
        context['date'] = str(metadata.get('installed_at', ''))
        context_hashes[metadata_rel] = digest(metadata_path.read_bytes())
    if roster_file.is_file():
        context_hashes['.bot-mode/roster.json'] = digest(roster_file.read_bytes())
    if not include_answers:
        return context, context_hashes
    params = metadata.get('parameters', {}) if metadata else {}
    if not isinstance(params, dict):
        raise UpdateError('Installed profile parameters are malformed')
    for name, item in params.items():
        if (not isinstance(name, str) or not isinstance(item, dict) or 'value' not in item
                or item.get('source') not in ('owner', 'default', 'inferred')):
            raise UpdateError('Installed profile answers are incomplete; manual review required')
        value = item['value']
        if value is not None:
            if not isinstance(value, (str, int, float, bool)):
                raise UpdateError('Unsupported original profile answer type')
            context[name] = str(value)
    if metadata:
        context['display_name'] = context.get('display_name', role)
    return context, context_hashes


def _reference_dependents(root, inventory, reference):
    """Resolve inventory-recorded dependent roles to verified active roster IDs."""
    roles = reference.get('dependents')
    unknown, resolved, context_hashes = [], [], {}
    profile_records = {}
    for component in inventory['components']:
        if component['kind'] == 'profile':
            profile_records.setdefault(component['path'], []).append(component)
    if roles is None:
        roles = []
        unknown.append({'reason': 'inventory-dependent-roles-not-recorded'})
    roster_file = root / '.bot-mode' / 'roster.json'
    try:
        _check_chain(root, '.bot-mode/roster.json', allow_missing_leaf=False)
        roster = _json(roster_file)
        participants = roster.get('participants') if isinstance(roster, dict) else None
        if not isinstance(participants, list):
            raise UpdateError('Project roster has no valid participants list')
    except (UpdateError, release.ReleaseError):
        participants = []
        unknown.append({'reason': 'current-roster-unavailable'})
    for role in roles:
        active = [person for person in participants
                  if isinstance(person, dict) and person.get('status') == 'active'
                  and person.get('role') == role]
        matched = False
        for person in active:
            workspace = person.get('workspace')
            if not isinstance(workspace, str) or workspace != '.' and not (
                    workspace.startswith('team/') or workspace.startswith('agents/')):
                continue
            try:
                context, hashes = _profile_context(root, {'path': workspace}, include_answers=False)
            except (UpdateError, release.ReleaseError):
                continue
            records = profile_records.get(workspace, [])
            if len(records) > 1:
                continue
            resolved.append({'role': role, 'roster_id': person.get('bot'), 'bot_id': context['bot'],
                             'workspace': workspace,
                             'inventory_component': records[0]['id'] if records else None,
                             'version': records[0]['version'] if records else None})
            context_hashes.update(hashes)
            matched = True
        if not matched:
            unknown.append({'role': role, 'reason': 'no-active-verified-roster-profile'})
    resolved.sort(key=lambda item: (item['workspace'], item['roster_id'] or ''))
    return {'inventory_roles': list(roles), 'verified_roster_profiles': resolved,
            'unknown': unknown, 'coverage': 'known' if not unknown else 'unknown'}, context_hashes


def _render(data, context, relative):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as error:
        raise UpdateError(f'Profile template is not UTF-8: {relative}') from error
    rendered = PLACEHOLDER.sub(lambda match: str(context.get(match.group(1), match.group(0))), text)
    if '{{' in rendered or '}}' in rendered:
        raise UpdateError(f'Unresolved template placeholder: {relative}')
    return rendered.encode('utf-8')


def _zip_content(bundle, component_id, relative):
    try:
        with zipfile.ZipFile(bundle) as archive:
            with archive.open(f'{component_id}/{relative}') as stream:
                return stream.read(release.MAX_FILE + 1)
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError) as error:
        raise UpdateError(f'Cannot read verified component file: {component_id}/{relative}') from error


@contextmanager
def _frozen_bundle(bundle):
    """Snapshot an untrusted bundle so verification and extraction share bytes."""
    fd, name = tempfile.mkstemp(prefix='cbm-update-', suffix='.zip')
    checksum, total = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, 'wb') as destination, Path(bundle).open('rb') as source:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BUNDLE_BYTES:
                    raise UpdateError('Release bundle exceeds compressed size limit')
                checksum.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        yield Path(name), checksum.hexdigest()
    finally:
        Path(name).unlink(missing_ok=True)


def _file_diff(path, old, new):
    try:
        before = old.decode('utf-8').splitlines(keepends=True) if old is not None else []
        after = new.decode('utf-8').splitlines(keepends=True) if new is not None else []
    except UnicodeDecodeError as error:
        raise UpdateError(f'Current target is not UTF-8 text and needs manual review: {path}') from error
    return ''.join(difflib.unified_diff(before, after, fromfile=path if old is not None else '/dev/null',
                                        tofile=path if new is not None else '/dev/null'))


def _build_plan(project, envelope, bundle, trust, platform, targets, components,
                installed=None, now=None, resolutions=None, delete_paths=(),
                legacy_version_resolutions=None):
    legacy_version_resolutions = (_snapshot_json(legacy_version_resolutions, 'Legacy version resolutions')
                                  if legacy_version_resolutions is not None else {})
    with _frozen_bundle(bundle) as (frozen, bundle_hash):
        return _build_plan_frozen(project, envelope, frozen, trust, platform, targets,
                                  components, installed, now, resolutions, delete_paths, bundle_hash,
                                  legacy_version_resolutions)


def _build_plan_frozen(project, envelope, bundle, trust, platform, targets, components,
                       installed, now, resolutions, delete_paths, bundle_hash,
                       legacy_version_resolutions=None):
    root, inventory_file, inventory, inventory_hash = _inventory(project)
    mapped = _target_map(targets, inventory)
    selected = list(components)
    if (not selected or any(not isinstance(cid, str) for cid in selected)
            or len(selected) != len(set(selected))):
        raise UpdateError('Select one or more unique release component IDs')
    if not isinstance(installed, (dict, type(None))):
        raise UpdateError('Installed versions must be a release-component-ID map')
    installed_versions = {} if installed is None else dict(installed)
    legacy_version_resolutions = ({} if legacy_version_resolutions is None
                                  else legacy_version_resolutions)
    if not isinstance(legacy_version_resolutions, dict):
        raise UpdateError('Legacy version resolutions must be an object keyed by inventory component ID')
    selected_set = set(selected)
    # Authenticate and validate manifest structure before using any IDs or versions
    # as mapping keys or compatibility inputs.
    compatibility_versions = dict(installed_versions)
    for release_id, record in mapped.items():
        version = record['version']
        if isinstance(version, str):
            try:
                release._version(version)
            except release.ReleaseError:
                compatibility_versions.pop(release_id, None)
            else:
                compatibility_versions[release_id] = version
        else:
            compatibility_versions.pop(release_id, None)
    payload_hint = release.verify_manifest(envelope, trust, platform, compatibility_versions, now)
    hinted_release_id = payload_hint['release_id']
    hinted_versions = {item['id']: item['version'] for item in payload_hint['components']}
    eligible_legacy_ids = {mapped[cid]['id']: cid for cid in selected
                           if cid in mapped and type(mapped[cid]['version']) is int}
    if set(legacy_version_resolutions) - set(eligible_legacy_ids):
        raise UpdateError('Legacy version resolution does not name a selected integer-version inventory component')
    mapping_fields = {'inventory_sha256', 'component', 'legacy_version', 'release_id',
                      'declared_version', 'projection_sha256'}
    for release_id in eligible_legacy_ids.values():
        record = mapped[release_id]
        resolution = legacy_version_resolutions.get(record['id'])
        if resolution is None:
            continue
        expected = {'inventory_sha256': inventory_hash, 'component': release_id,
                    'legacy_version': record['version'], 'release_id': hinted_release_id,
                    'declared_version': hinted_versions.get(release_id)}
        if (not isinstance(resolution, dict) or set(resolution) != mapping_fields
                or canonical({key: resolution[key] for key in expected}) != canonical(expected)
                or not isinstance(resolution.get('projection_sha256'), str)
                or not HASH.fullmatch(resolution['projection_sha256'])):
            raise UpdateError(f'{record["id"]}: legacy version resolution is stale or mismatched')
        installed_versions[release_id] = resolution['declared_version']
    for release_id, record in mapped.items():
        version = record['version']
        if isinstance(version, str):
            try:
                release._version(version)
            except release.ReleaseError:
                if release_id in selected_set:
                    raise UpdateError(f'{release_id}: installed version is unknown; manual review required') from None
                installed_versions.pop(release_id, None)
            else:
                # Inventory identity is translated explicitly to the release ID.
                installed_versions[release_id] = version
        elif type(version) is int:
            if record['id'] not in legacy_version_resolutions:
                installed_versions.pop(release_id, None)
        else:
            if release_id in selected_set:
                raise UpdateError(f'{release_id}: installed version is unknown; manual review required')
            installed_versions.pop(release_id, None)
    # The verifier hashes the complete archive before any project target writes.
    payload = release.verify_bundle(envelope, bundle, trust, platform, installed_versions, now)
    proposed = {item['id']: item for item in payload['components']}
    if any(cid not in proposed or cid not in mapped for cid in selected):
        raise UpdateError('Every selected release component needs an explicit managed target mapping')
    legacy_candidates = {}
    selected_versions = {cid: proposed[cid]['version'] for cid in selected}
    for cid in selected:
        for dependency, version in proposed[cid]['requires'].items():
            if selected_versions.get(dependency, installed_versions.get(dependency)) != version:
                raise UpdateError(f'{cid}: select compatible component {dependency} or provide its installed version')
    resolutions = {} if resolutions is None else resolutions
    if not isinstance(resolutions, dict):
        raise UpdateError('Manual resolutions must be an object keyed by project-relative path')
    delete_paths = set(delete_paths)
    operations, conflicts, delete_candidates, selected_targets, context_hashes = [], [], [], [], {}
    approved_resolutions = {}
    for release_id in selected:
        manifest_component = proposed[release_id]
        record = mapped[release_id]
        expected_kind = 'profile' if manifest_component['kind'] == 'profile' else 'shared_reference'
        if manifest_component['kind'] not in ('profile', 'reference') or record['kind'] != expected_kind:
            raise UpdateError(f'{release_id}: release kind does not match the mapped installation component')
        base = record['path']
        if expected_kind == 'profile':
            profile_layout = _profile_layout(root, record)
            context, context_hash = _profile_context(root, record, profile_layout)
            context_hashes.update(context_hash)
            projection_context_hashes = context_hash
            dependents = None
        else:
            if not base.startswith('team/references/') or len(PurePosixPath(base).parts) != 3:
                raise UpdateError('Shared references must map to one team/references/<bundle> root')
            context = {}
            dependents, dependent_hashes = _reference_dependents(root, inventory, record)
            context_hashes.update(dependent_hashes)
            projection_context_hashes = dependent_hashes
        target_file_hashes = {}
        projection_files = {}
        selected_target = {'release_component': release_id, 'inventory_component': record['id'],
                           'kind': manifest_component['kind'], 'root': base,
                           'version': manifest_component['version']}
        if expected_kind == 'profile':
            selected_target['profile_root'] = profile_layout['root']
            selected_target['profile_layout'] = profile_layout['layout']
        if dependents is not None:
            selected_target['dependents'] = dependents
        legacy = type(record['version']) is int
        if legacy:
            selected_target['historical_version_known'] = False
            selected_target['legacy_version'] = record['version']
        selected_targets.append(selected_target)
        incoming = set()
        for package_path in sorted(manifest_component['files']):
            if expected_kind == 'profile' and package_path not in PROFILE_FILES:
                raise UpdateError(f'Profile path is outside the four-file update allowlist: {package_path}')
            incoming.add(package_path)
            project_rel = (_profile_file_path(profile_layout, package_path)
                           if expected_kind == 'profile' else f'{base}/{package_path}')
            target = _check_chain(root, project_rel)
            if target.exists() and not target.is_file():
                raise UpdateError(f'Target file path is occupied by a directory: {project_rel}')
            old = target.read_bytes() if target.is_file() else None
            old_hash = digest(old) if old is not None else None
            baseline = record['baseline'].get(project_rel)
            owned = project_rel in record['owned_paths']
            raw = _zip_content(bundle, release_id, package_path)
            new = _render(raw, context, project_rel) if expected_kind == 'profile' else raw
            expected_hash = digest(new)
            target_file_hashes[project_rel] = expected_hash
            if legacy:
                projection_files[project_rel] = {
                    'current_sha256': old_hash,
                    'baseline': ({'state': 'known', 'sha256': baseline['sha256']}
                                 if baseline and baseline.get('state') == 'known' else {'state': 'unknown'}),
                    'proposed_sha256': expected_hash}
            resolution = resolutions.get(project_rel)
            classification = None
            if not owned and old is None:
                classification = 'safe-new-file'
            elif baseline and baseline.get('state') == 'known' and old_hash == baseline.get('sha256'):
                classification = 'unchanged-owned'
            elif old_hash == expected_hash and owned and baseline and baseline.get('state') == 'known':
                classification = 'already-at-release-content'
            else:
                reason = ('unknown-baseline' if not baseline or baseline.get('state') != 'known'
                          else 'missing-owned-file' if old is None else 'local-changes-or-unowned-file')
                if (isinstance(resolution, dict) and set(resolution) == {'action', 'expected_sha256'}
                        and resolution['action'] == 'replace' and resolution['expected_sha256'] == old_hash):
                    classification = 'manually-resolved-conflict'
                    approved_resolutions[project_rel] = resolution
                else:
                    conflicts.append({'path': project_rel, 'reason': reason,
                                      'current_sha256': old_hash, 'baseline': baseline,
                                      'proposed_sha256': expected_hash,
                                      'resolution_required': {'action': 'replace', 'expected_sha256': old_hash}})
                    continue
            if old_hash != expected_hash:
                operations.append({'path': project_rel, 'action': 'write', 'classification': classification,
                                   'before_sha256': old_hash, 'after_sha256': expected_hash,
                                   'content': new.decode('utf-8'), 'diff': _file_diff(project_rel, old, new)})
        selected_targets[-1]['files'] = target_file_hashes
        selected_targets[-1]['installed_sha256'] = digest(canonical(target_file_hashes))
        for old_rel in record['owned_paths']:
            if expected_kind == 'profile':
                package_rel = _profile_package_path(profile_layout, old_rel)
                if package_rel is None:
                    if old_rel in delete_paths:
                        raise UpdateError(f'Deletion path is outside the profile update allowlist: {old_rel}')
                    continue
            else:
                if not old_rel.startswith(base.rstrip('/') + '/'):
                    continue
                package_rel = old_rel[len(base.rstrip('/')) + 1:]
            if package_rel in incoming:
                continue
            baseline = record['baseline'].get(old_rel)
            target = _check_chain(root, old_rel)
            old = target.read_bytes() if target.is_file() else None
            old_hash = digest(old) if old is not None else None
            if legacy:
                projection_files[old_rel] = {
                    'current_sha256': old_hash,
                    'baseline': ({'state': 'known', 'sha256': baseline['sha256']}
                                 if baseline and baseline.get('state') == 'known' else {'state': 'unknown'}),
                    'proposed_sha256': None}
            if expected_kind == 'profile':
                deletion_allowed = package_rel in PROFILE_FILES
                if not deletion_allowed and old_rel in delete_paths:
                    raise UpdateError(f'Deletion path is outside the profile update allowlist: {old_rel}')
            else:
                deletion_allowed = release._allowed(release_id, 'reference', package_rel, trust)
                if not deletion_allowed and old_rel in delete_paths:
                    raise UpdateError(f'Deletion path is outside the verified reference namespace: {old_rel}')
            if not deletion_allowed:
                continue
            if baseline and baseline.get('state') == 'known' and old_hash == baseline.get('sha256'):
                delete_candidates.append(old_rel)
                if old_rel in delete_paths:
                    operations.append({'path': old_rel, 'action': 'delete', 'classification': 'unchanged-owned-deletion',
                                       'before_sha256': old_hash, 'after_sha256': None, 'content': None,
                                       'diff': _file_diff(old_rel, old, None)})
            elif old_rel in delete_paths:
                conflicts.append({'path': old_rel, 'reason': 'deletion-requires-known-unchanged-owned-file',
                                  'current_sha256': old_hash, 'baseline': baseline})
        if legacy:
            projection = {'inventory_sha256': inventory_hash, 'inventory_component': record['id'],
                          'component': release_id, 'legacy_version': record['version'],
                          'release_id': payload['release_id'], 'declared_version': manifest_component['version'],
                          'manifest_sha256': digest(canonical(envelope)), 'bundle_sha256': bundle_hash,
                          'root': base, 'files': projection_files,
                          'context_sha256': projection_context_hashes}
            projection_sha256 = digest(canonical(projection))
            candidate = {'inventory_sha256': inventory_hash, 'component': release_id,
                         'legacy_version': record['version'], 'release_id': payload['release_id'],
                         'declared_version': manifest_component['version'],
                         'projection_sha256': projection_sha256}
            resolution = legacy_version_resolutions.get(record['id'])
            acknowledged = resolution is not None
            if acknowledged and canonical(resolution) != canonical(candidate):
                raise UpdateError(f'{record["id"]}: legacy version resolution is stale or does not match the reviewed source projection')
            legacy_candidates[record['id']] = {
                'candidate': candidate, 'projection': projection,
                'historical_version_known': False, 'acknowledged': acknowledged}
            selected_target['legacy_version_projection_sha256'] = projection_sha256
            if not acknowledged:
                conflicts.append({'component': record['id'], 'reason': 'legacy-version-review-required',
                                  'resolution_required': candidate})
    conflict_paths = {item['path'] for item in conflicts if isinstance(item.get('path'), str)}
    unknown_resolutions = set(resolutions) - conflict_paths - {op['path'] for op in operations}
    if unknown_resolutions:
        raise UpdateError('Resolution path is not a current conflict: ' + ', '.join(sorted(unknown_resolutions)))
    unknown_delete_targets = delete_paths - set(delete_candidates)
    if unknown_delete_targets:
        raise UpdateError('Deletion requires an owned, unchanged file omitted by the verified release: '
                          + ', '.join(sorted(unknown_delete_targets)))
    operations.sort(key=lambda item: item['path'].casefold())
    plan = {'schema': SCHEMA, 'action': 'profile-reference-update', 'project': str(root),
            'release': {'id': payload['release_id'], 'source_revision': payload['source_revision'],
                        'key_id': envelope['key_id'], 'manifest_sha256': digest(canonical(envelope)),
                        'bundle_sha256': bundle_hash},
            'inventory_sha256': inventory_hash, 'context_sha256': context_hashes,
            'selected_targets': selected_targets, 'operations': operations,
            'resolutions': approved_resolutions,
            'legacy_version_resolutions': {cid: value['candidate'] for cid, value in legacy_candidates.items()
                                           if value['acknowledged']},
            'legacy_version_candidates': [{**value['candidate'], 'inventory_component': cid,
                'historical_version_known': False, 'acknowledged': value['acknowledged']}
                for cid, value in legacy_candidates.items()],
            'legacy_version_projections': {cid: value['projection'] for cid, value in legacy_candidates.items()},
            'conflicts': conflicts, 'delete_candidates': sorted(set(delete_candidates)),
            'pending_activation': {'state': 'pending_activation', 'activated': False,
                                   'release_id': payload['release_id'],
                                   'source_revision': payload['source_revision'],
                                   'targets': [{'component': t['inventory_component'],
                                                'release_component': t['release_component'],
                                                'path': (t['profile_root'] if t['root'] == '.' else t['root']),
                                                'version': t['version'], 'files': t['files'],
                                                'installed_sha256': t['installed_sha256'],
                                                **({'dependents': t['dependents']} if 'dependents' in t else {})}
                                               for t in selected_targets]}}
    plan['plan_sha256'] = digest(canonical(plan))
    return plan


def plan_update(project, envelope, bundle, trust, platform, targets, components,
                installed=None, now=None, resolutions=None, delete_paths=(),
                legacy_version_resolutions=None):
    """Return deterministic review data; never writes the project."""
    return _build_plan(project, envelope, bundle, trust, platform, targets, components,
                       installed, now, resolutions, delete_paths, legacy_version_resolutions)


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.profile-update-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save_journal(path, journal):
    _atomic(path, canonical(journal) + b'\n')


@contextmanager
def _locks(root, inventory_file):
    local = root / '.bot-mode' / 'local'
    _check_chain(root, '.bot-mode/local/profile-updates.lock')
    _check_chain(root, '.bot-mode/local/installations.lock')
    local.mkdir(parents=True, exist_ok=True)
    locks = [local / 'profile-updates.lock', inventory_file.with_suffix('.lock')]
    held = []
    try:
        for lock in locks:
            held.append(_acquire_update_lock(root, lock))
        yield
    finally:
        for fd in reversed(held):
            os.close(fd)


def _validate_plan(plan):
    if not isinstance(plan, dict) or plan.get('schema') != SCHEMA or plan.get('action') != 'profile-reference-update':
        raise UpdateError('Unsupported update plan')
    expected = plan.get('plan_sha256')
    unsigned = dict(plan)
    unsigned.pop('plan_sha256', None)
    if not isinstance(expected, str) or digest(canonical(unsigned)) != expected:
        raise UpdateError('Plan hash mismatch')
    if any(not candidate.get('acknowledged') for candidate in plan.get('legacy_version_candidates', [])):
        raise UpdateError('Legacy version review is unresolved; create and approve a mapped plan')
    if plan.get('conflicts'):
        raise UpdateError('Plan has unresolved conflicts; resolve them and create a new plan')


def _activation_record(plan, plan_sha256=None):
    pending = json.loads(json.dumps(plan['pending_activation']))
    pending['manifest_sha256'] = plan['release']['manifest_sha256']
    pending['bundle_sha256'] = plan['release']['bundle_sha256']
    pending['plan_sha256'] = plan_sha256 or plan['plan_sha256']
    return pending


def apply_plan(plan, approved_plan_hash, envelope, bundle, trust, platform, targets,
               installed=None, now=None, failpoint=None):
    """Apply one exact reviewed plan; interruptions leave a recoverable journal."""
    plan = _snapshot_json(plan, 'Update plan')
    envelope = _snapshot_json(envelope, 'Release envelope')
    trust = _snapshot_json(trust, 'Publisher trust')
    targets = _snapshot_json(targets, 'Target map')
    installed = _snapshot_json(installed, 'Installed version map') if installed is not None else None
    bundle = Path(bundle)
    _validate_plan(plan)
    if not isinstance(approved_plan_hash, str) or approved_plan_hash != plan['plan_sha256']:
        raise UpdateError('Approval must match the exact plan hash')
    # Rebuild from current inventory/files and reverify every signed archive entry.
    fresh = _build_plan(plan['project'], envelope, bundle, trust, platform, targets,
                        [item['release_component'] for item in plan['selected_targets']],
                        installed, now,
                        plan.get('resolutions', {}),
                        [op['path'] for op in plan['operations'] if op['action'] == 'delete'],
                        plan.get('legacy_version_resolutions', {}))
    if fresh['plan_sha256'] != approved_plan_hash:
        raise UpdateError('Plan is stale; target, inventory, release or rendering context changed')
    plan = fresh
    root, inventory_file, inventory, inventory_hash = _inventory(plan['project'])
    if inventory_hash != plan['inventory_sha256']:
        raise UpdateError('Installation inventory changed since preview')
    transaction_id = str(uuid.uuid4())
    txroot = root / '.bot-mode' / 'local' / 'profile-updates' / transaction_id
    _check_chain(root, f'.bot-mode/local/profile-updates/{transaction_id}')
    legacy_adoptions = [{'inventory_component': inventory_id, **mapping,
                         'historical_version_known': False}
                        for inventory_id, mapping in plan.get('legacy_version_resolutions', {}).items()]
    with _locks(root, inventory_file):
        root2, inventory_file, inventory, current_inventory_hash = _inventory(root)
        if current_inventory_hash != plan['inventory_sha256']:
            raise UpdateError('Installation inventory changed while acquiring the transaction lock')
        for rel, expected in plan['context_sha256'].items():
            context_file = _check_chain(root, rel, allow_missing_leaf=False)
            if digest(context_file.read_bytes()) != expected:
                raise UpdateError(f'Original profile answers changed since preview: {rel}')
        fresh = _build_plan(plan['project'], envelope, bundle, trust, platform, targets,
                            [item['release_component'] for item in plan['selected_targets']],
                            installed, now,
                            plan.get('resolutions', {}),
                            [op['path'] for op in plan['operations'] if op['action'] == 'delete'],
                            plan.get('legacy_version_resolutions', {}))
        if fresh['plan_sha256'] != approved_plan_hash:
            raise UpdateError('Plan became stale while acquiring the transaction lock')
        plan = fresh
        backup_bytes, staged_bytes = 0, 0
        for op in plan['operations']:
            target = _check_chain(root, op['path'])
            current = target.read_bytes() if target.is_file() else None
            if (digest(current) if current is not None else None) != op['before_sha256']:
                raise UpdateError(f"Target changed before staging: {op['path']}")
            backup_bytes += len(current or b'')
            staged_bytes += len((op['content'] or '').encode('utf-8'))
        if backup_bytes > MAX_BACKUP_BYTES:
            raise UpdateError('Transaction backup exceeds its bounded size limit')
        required = backup_bytes + staged_bytes + inventory_file.stat().st_size + 1024 * 1024
        if shutil.disk_usage(root).free < required:
            raise UpdateError('Insufficient free space for staged files and recoverable backups')
        if not plan['operations'] and all(t['version'] == next(c['version'] for c in inventory['components'] if c['id'] == t['inventory_component'])
                                          for t in plan['selected_targets']):
            return {'status': 'no_op', 'plan_sha256': approved_plan_hash,
                    'pending_activation': _activation_record(plan, approved_plan_hash), 'transaction': None,
                    'legacy_version_adoptions': legacy_adoptions}
        txroot.mkdir(parents=True)
        backup_root, staged_root = txroot / 'backup', txroot / 'staged'
        journal_file = txroot / 'journal.json'
        new_inventory = json.loads(json.dumps(inventory))
        selected_by_id = {item['inventory_component']: item for item in plan['selected_targets']}
        for component in new_inventory['components']:
            if component['id'] not in selected_by_id:
                continue
            target = selected_by_id[component['id']]
            component['version'] = target['version']
            component['source_revision'] = plan['release']['source_revision']
            component['source'] = f"release:{plan['release']['id']}"
            component['component_schema'] = 1
            for rel, file_hash in target['files'].items():
                if rel not in component['owned_paths']:
                    component['owned_paths'].append(rel)
                component['baseline'][rel] = {'state': 'known', 'sha256': file_hash}
            for op in plan['operations']:
                if op['action'] != 'delete' or op['path'] not in component['owned_paths']:
                    continue
                if target['kind'] == 'profile':
                    layout = {'layout': target['profile_layout'], 'root': target['profile_root']}
                    belongs = _profile_package_path(layout, op['path']) is not None
                else:
                    belongs = op['path'].startswith(target['root'].rstrip('/') + '/')
                if belongs:
                    component['owned_paths'].remove(op['path'])
                    component['baseline'].pop(op['path'], None)
        old_inventory = inventory_file.read_bytes()
        new_inventory_bytes = json.dumps(new_inventory, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
        journal = {'schema': SCHEMA, 'transaction_id': transaction_id, 'phase': 'preparing',
                   'plan_sha256': approved_plan_hash, 'inventory_path': '.bot-mode/local/installations.json',
                   'old_inventory_sha256': digest(old_inventory), 'new_inventory_sha256': digest(new_inventory_bytes),
                   'pending_activation': _activation_record(plan, approved_plan_hash),
                   'legacy_version_adoptions': legacy_adoptions,
                   'selected_targets': plan['selected_targets'], 'operations': []}
        _save_journal(journal_file, journal)
        try:
            if failpoint:
                failpoint('journal_created')
            backup_root.mkdir()
            staged_root.mkdir()
            journal['inventory_backup'] = 'backup/installations.json'
            (backup_root / 'installations.json').write_bytes(old_inventory)
            journal['phase'] = 'staging'
            _save_journal(journal_file, journal)
            backed_up_once = False
            for index, op in enumerate(plan['operations']):
                _check_chain(root, op['path'])
                target = root.joinpath(*_safe_rel(op['path']).parts)
                current = target.read_bytes() if target.is_file() else None
                if (digest(current) if current is not None else None) != op['before_sha256']:
                    raise UpdateError(f"Target changed before staging: {op['path']}")
                entry = {key: op[key] for key in ('path', 'action', 'before_sha256', 'after_sha256')}
                entry['backup'] = None
                entry['staged'] = None
                if current is not None:
                    name = f'{index:04d}.bin'
                    (backup_root / name).write_bytes(current)
                    entry['backup'] = f'backup/{name}'
                    journal['operations'].append(entry)
                    _save_journal(journal_file, journal)
                    if failpoint and not backed_up_once:
                        backed_up_once = True
                        failpoint('first_backup')
                if op['action'] == 'write':
                    name = f'{index:04d}.bin'
                    body = op['content'].encode('utf-8')
                    if digest(body) != op['after_sha256']:
                        raise UpdateError('Staged content hash does not match approved plan')
                    (staged_root / name).write_bytes(body)
                    entry['staged'] = f'staged/{name}'
                if not journal['operations'] or journal['operations'][-1] is not entry:
                    journal['operations'].append(entry)
                _save_journal(journal_file, journal)
            (staged_root / 'installations.json').write_bytes(new_inventory_bytes)
            journal['phase'] = 'staged'
            _save_journal(journal_file, journal)
            if failpoint:
                failpoint('staged')
            journal['phase'] = 'backed_up'
            _save_journal(journal_file, journal)
            if failpoint:
                failpoint('backup_complete')
                failpoint('backed_up')
            if digest(inventory_file.read_bytes()) != journal['old_inventory_sha256']:
                raise InventoryCASMismatch('Installation inventory compare-and-swap failed before target commit')
            journal['phase'] = 'committing'
            _save_journal(journal_file, journal)
            for index, entry in enumerate(journal['operations']):
                target = _check_chain(root, entry['path'])
                current = target.read_bytes() if target.is_file() else None
                if (digest(current) if current is not None else None) != entry['before_sha256']:
                    raise UpdateError(f"Target changed after staging: {entry['path']}")
                if entry['action'] == 'delete':
                    target.unlink()
                else:
                    staged = txroot / entry['staged']
                    _atomic(target, staged.read_bytes())
                if failpoint and index == 0:
                    failpoint('first_write')
            staged_inventory = staged_root / 'installations.json'
            _atomic(inventory_file, staged_inventory.read_bytes())
            journal['phase'] = 'inventory_committed'
            _save_journal(journal_file, journal)
            if failpoint:
                failpoint('metadata_commit')
            journal['phase'] = 'applied'
            _save_journal(journal_file, journal)
            return {'status': 'applied', 'plan_sha256': approved_plan_hash,
                    'transaction': transaction_id,
                    'pending_activation': _activation_record(plan, approved_plan_hash),
                    'legacy_version_adoptions': legacy_adoptions}
        except SimulatedCrash:
            raise
        except InventoryCASMismatch:
            for entry in journal['operations']:
                target = _check_chain(root, entry['path'])
                current = target.read_bytes() if target.is_file() else None
                if (digest(current) if current is not None else None) != entry['before_sha256']:
                    raise UpdateError('Inventory changed and target files also changed; manual recovery required')
            journal['phase'] = 'rolled_back'
            _save_journal(journal_file, journal)
            raise
        except Exception:
            _recover_locked(root, txroot, journal_file)
            raise


def _tx_path(project, transaction_id):
    if not isinstance(transaction_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', transaction_id):
        raise UpdateError('Invalid transaction ID')
    root = Path(project).resolve(strict=True)
    rel = f'.bot-mode/local/profile-updates/{transaction_id}'
    path = _check_chain(root, rel, allow_missing_leaf=False)
    if not path.is_dir():
        raise UpdateError('Transaction directory is missing')
    return root, path


def _restore_entry(root, txroot, entry, rollback=False):
    target = _check_chain(root, entry['path'])
    current = target.read_bytes() if target.is_file() else None
    current_hash = digest(current) if current is not None else None
    allowed = {entry['before_sha256'], entry['after_sha256']}
    if current_hash not in allowed:
        raise UpdateError(f"Recovery/rollback guard: target changed outside transaction: {entry['path']}")
    backup_rel = entry.get('backup')
    if entry['before_sha256'] is None:
        if backup_rel is not None:
            raise UpdateError('Invalid backup for a previously absent file')
        if current is not None:
            target.unlink()
        return
    if not isinstance(backup_rel, str) or not backup_rel.startswith('backup/'):
        raise UpdateError('Transaction backup is missing')
    backup = txroot / backup_rel
    if _linked(backup) or not backup.is_file():
        raise UpdateError('Transaction backup is unavailable')
    original = backup.read_bytes()
    if digest(original) != entry['before_sha256']:
        raise UpdateError('Transaction backup integrity check failed')
    if current_hash != entry['before_sha256']:
        _atomic(target, original)


def _recover_locked(root, txroot, journal_file):
    if _linked(journal_file) or not journal_file.is_file():
        raise UpdateError('Transaction journal is missing or linked')
    journal = _json(journal_file, 4 * 1024 * 1024)
    _validate_journal(root, txroot, journal)
    if journal.get('phase') == 'rolled_back':
        return {'status': 'already_rolled_back', 'transaction': journal['transaction_id']}
    _, inventory_file, _, current_inventory_hash = _inventory(root)
    all_new = all((digest((root / op['path']).read_bytes()) if (root / op['path']).is_file() else None)
                  == op['after_sha256'] for op in journal.get('operations', []))
    if journal.get('phase') == 'applied':
        return {'status': 'already_applied', 'transaction': journal['transaction_id'],
                'pending_activation': journal.get('pending_activation')}
    if journal.get('phase') == 'rolling_back':
        return _finish_rollback(root, txroot, journal_file, journal)
    if current_inventory_hash == journal.get('new_inventory_sha256') and all_new:
        journal['phase'] = 'applied'
        _save_journal(journal_file, journal)
        return {'status': 'applied', 'transaction': journal['transaction_id'],
                'pending_activation': journal.get('pending_activation')}
    if current_inventory_hash not in (journal.get('old_inventory_sha256'), journal.get('new_inventory_sha256')):
        raise UpdateError('Recovery guard: installation inventory changed outside transaction')
    if journal.get('phase') == 'preparing':
        if current_inventory_hash != journal.get('old_inventory_sha256') or journal.get('operations'):
            raise UpdateError('Recovery guard: incomplete staging has unexpected target changes')
        journal['phase'] = 'rolled_back'
        _save_journal(journal_file, journal)
        return {'status': 'recovered_rollback', 'transaction': journal['transaction_id']}
    journal['phase'] = 'rolling_back'
    _save_journal(journal_file, journal)
    return _finish_rollback(root, txroot, journal_file, journal)


def _check_backups(txroot, journal):
    for entry in journal.get('operations', []):
        if entry['before_sha256'] is None:
            continue
        backup = _transaction_file(txroot, entry.get('backup'))
        if not backup.is_file() or digest(backup.read_bytes()) != entry['before_sha256']:
            raise UpdateError(f"Transaction backup integrity check failed: {entry['path']}")
    if journal.get('phase') != 'staging':
        backup = _transaction_file(txroot, journal.get('inventory_backup'))
        if (not backup.is_file()
                or digest(backup.read_bytes()) != journal.get('old_inventory_sha256')):
            raise UpdateError('Installation inventory backup integrity check failed')


def _finish_rollback(root, txroot, journal_file, journal):
    _, inventory_file, _, current_inventory_hash = _inventory(root)
    if current_inventory_hash not in (journal.get('old_inventory_sha256'), journal.get('new_inventory_sha256')):
        raise UpdateError('Recovery/rollback guard: installation inventory changed outside transaction')
    _check_backups(txroot, journal)
    for entry in journal.get('operations', []):
        target = _check_chain(root, entry['path'])
        current = target.read_bytes() if target.is_file() else None
        if (digest(current) if current is not None else None) not in {entry['before_sha256'], entry['after_sha256']}:
            raise UpdateError(f"Recovery/rollback guard: target changed outside transaction: {entry['path']}")
    for entry in reversed(journal.get('operations', [])):
        _restore_entry(root, txroot, entry)
    if current_inventory_hash == journal.get('new_inventory_sha256'):
        backup = txroot / journal['inventory_backup']
        _atomic(inventory_file, backup.read_bytes())
    journal['phase'] = 'rolled_back'
    _save_journal(journal_file, journal)
    return {'status': 'recovered_rollback', 'transaction': journal['transaction_id']}


def _transaction_file(txroot, relative):
    if not isinstance(relative, str):
        raise UpdateError('Invalid transaction artifact path')
    rel = _safe_rel(relative)
    path = txroot.joinpath(*rel.parts)
    cursor = txroot
    for part in rel.parts:
        cursor = cursor / part
        if _linked(cursor):
            raise UpdateError('Linked transaction artifact is forbidden')
        if cursor.exists() and not _contained(txroot, cursor):
            raise UpdateError('Transaction artifact escapes transaction directory')
    return path


def _validate_journal(root, txroot, journal):
    phases = {'preparing', 'staging', 'staged', 'backed_up', 'committing', 'inventory_committed',
              'applied', 'rolling_back', 'rolled_back'}
    if (not isinstance(journal, dict) or journal.get('schema') != SCHEMA
            or journal.get('transaction_id') != txroot.name or journal.get('phase') not in phases
            or journal.get('inventory_path') != '.bot-mode/local/installations.json'
            or not HASH.fullmatch(journal.get('old_inventory_sha256', ''))
            or not HASH.fullmatch(journal.get('new_inventory_sha256', ''))
            or not isinstance(journal.get('operations'), list)):
        raise UpdateError('Invalid transaction journal')
    try:
        if str(uuid.UUID(txroot.name)) != txroot.name:
            raise ValueError()
    except ValueError as error:
        raise UpdateError('Invalid transaction ID in journal') from error
    selected = journal.get('selected_targets')
    if not isinstance(selected, list):
        raise UpdateError('Transaction journal has no selected targets')
    allowed = set()
    for target in selected:
        if (not isinstance(target, dict) or target.get('kind') not in ('profile', 'reference')
                or not isinstance(target.get('root'), str) or not isinstance(target.get('release_component'), str)):
            raise UpdateError('Invalid target in transaction journal')
        base = None if target['kind'] == 'profile' and target['root'] == '.' else _safe_rel(target['root'])
        if target['kind'] == 'profile':
            if target['root'] != '.' and not (target['root'].startswith('team/') or target['root'].startswith('agents/')):
                raise UpdateError('Invalid profile target in transaction journal')
            profile_root = target.get('profile_root')
            if profile_root is None:
                profile_root = target['root']
                package_prefix = ''
            else:
                _safe_rel(profile_root)
                layout = target.get('profile_layout')
                if (layout == 'root_export' and (target['root'] != '.' or profile_root != 'agent')
                        or layout == 'flat_manager' and (target['root'] != 'agents/project-manager'
                                                         or profile_root != target['root'])
                        or layout == 'workspace' and (target['root'] == '.' or profile_root != target['root'])
                        or layout not in ('root_export', 'flat_manager', 'workspace')):
                    raise UpdateError('Invalid canonical profile path in transaction journal')
                package_prefix = '' if layout == 'workspace' else 'agent/'
            paths = frozenset(PROFILE_FILES)
        else:
            if not target['root'].startswith('team/references/') or len(base.parts) != 3:
                raise UpdateError('Invalid reference target in transaction journal')
            paths = None
            profile_root = target['root']
            package_prefix = ''
        allowed.add((profile_root, target['release_component'], paths, package_prefix))
    seen = set()
    for entry in journal['operations']:
        if (not isinstance(entry, dict) or set(entry) != {'path', 'action', 'before_sha256', 'after_sha256', 'backup', 'staged'}
                or entry['action'] not in ('write', 'delete') or entry['path'] in seen):
            raise UpdateError('Invalid operation in transaction journal')
        seen.add(entry['path'])
        rel = _safe_rel(entry['path'])
        contained_target = False
        for base, cid, paths, package_prefix in allowed:
            prefix = base.rstrip('/') + '/'
            if not entry['path'].startswith(prefix):
                continue
            package_path = package_prefix + entry['path'][len(prefix):]
            if paths is not None and package_path not in paths:
                continue
            if paths is None and not release._allowed(cid, 'reference', package_path, {}):
                continue
            contained_target = True
            break
        if not contained_target:
            raise UpdateError('Transaction path is outside selected profile/reference targets')
        for key in ('before_sha256', 'after_sha256'):
            if entry[key] is not None and (not isinstance(entry[key], str) or not HASH.fullmatch(entry[key])):
                raise UpdateError('Invalid operation hash in transaction journal')
        if (entry['action'] == 'write' and entry['after_sha256'] is None
                or entry['action'] == 'delete' and entry['after_sha256'] is not None):
            raise UpdateError('Invalid operation result hash in transaction journal')
        for key, prefix in (('backup', 'backup/'), ('staged', 'staged/')):
            value = entry[key]
            if value is not None and (not isinstance(value, str) or not value.startswith(prefix)):
                raise UpdateError('Invalid operation artifact reference')
            if value is not None:
                _transaction_file(txroot, value)
    if journal.get('inventory_backup') is not None and journal['inventory_backup'] != 'backup/installations.json':
        raise UpdateError('Invalid inventory backup path')


def recover(project, transaction_id):
    root, txroot = _tx_path(project, transaction_id)
    _, inventory_file, _, _ = _inventory(root)
    with _locks(root, inventory_file):
        return _recover_locked(root, txroot, txroot / 'journal.json')


def rollback(project, transaction_id):
    root, txroot = _tx_path(project, transaction_id)
    _, inventory_file, _, current_hash = _inventory(root)
    with _locks(root, inventory_file):
        journal_file = txroot / 'journal.json'
        journal = _json(journal_file, 4 * 1024 * 1024)
        if journal.get('phase') == 'rolled_back':
            return {'status': 'already_rolled_back', 'transaction': transaction_id}
        if journal.get('phase') not in ('applied', 'rolling_back'):
            return _recover_locked(root, txroot, journal_file)
        if journal.get('phase') == 'applied':
            _, inventory_file, _, current_hash = _inventory(root)
            if current_hash != journal.get('new_inventory_sha256'):
                raise UpdateError('Rollback guard: inventory changed after apply')
            for entry in journal.get('operations', []):
                target = _check_chain(root, entry['path'])
                current = target.read_bytes() if target.is_file() else None
                if (digest(current) if current is not None else None) != entry['after_sha256']:
                    raise UpdateError(f"Rollback guard: file changed after apply: {entry['path']}")
            _check_backups(txroot, journal)
            journal['phase'] = 'rolling_back'
            _save_journal(journal_file, journal)
        result = _finish_rollback(root, txroot, journal_file, journal)
        result['status'] = 'rolled_back'
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    plan = sub.add_parser('plan')
    for command in (plan,):
        command.add_argument('--project', required=True)
        command.add_argument('--manifest', required=True)
        command.add_argument('--bundle', required=True)
        command.add_argument('--trust', required=True)
        command.add_argument('--targets', required=True, help='Schema 1 release-component to inventory-ID map')
        command.add_argument('--platform', required=True)
        command.add_argument('--component', action='append', required=True)
        command.add_argument('--resolution', help='JSON object of explicit replace resolutions')
        command.add_argument('--legacy-version-resolutions',
                             help='JSON mapping acknowledging an integer-version inventory projection')
        command.add_argument('--delete', action='append', default=[])
    apply = sub.add_parser('apply')
    apply.add_argument('--plan', required=True)
    apply.add_argument('--approve', required=True, help='Exact plan SHA-256 reviewed by the owner')
    for name in ('manifest', 'bundle', 'trust', 'targets'):
        apply.add_argument('--' + name, required=True)
    apply.add_argument('--platform', required=True)
    for name in ('recover', 'rollback'):
        command = sub.add_parser(name)
        command.add_argument('--project', required=True)
        command.add_argument('--transaction', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'plan':
            plan_data = plan_update(args.project, _json(args.manifest, release.MAX_MANIFEST), args.bundle,
                                    _json(args.trust, release.MAX_MANIFEST), args.platform,
                                    _json(args.targets), args.component,
                                    resolutions=_json(args.resolution) if args.resolution else None,
                                    delete_paths=args.delete,
                                    legacy_version_resolutions=(
                                        _json(args.legacy_version_resolutions)
                                        if args.legacy_version_resolutions else None))
            print(json.dumps(plan_data, ensure_ascii=False, indent=2))
        elif args.action == 'apply':
            plan_data = _json(args.plan, 600 * 1024 * 1024)
            targets = _json(args.targets)
            result = apply_plan(plan_data, args.approve, _json(args.manifest, release.MAX_MANIFEST),
                                args.bundle, _json(args.trust, release.MAX_MANIFEST), args.platform, targets)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            result = recover(args.project, args.transaction) if args.action == 'recover' else rollback(args.project, args.transaction)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (UpdateError, release.ReleaseError, OSError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
