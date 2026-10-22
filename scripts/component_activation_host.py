"""Persist Office-verified activation receipts beside profile transactions."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import uuid

try:
    from . import component_activation as activation
except ImportError:  # Direct Office execution places scripts/ on sys.path.
    import component_activation as activation


MAX_TRANSACTIONS = 256
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_INVENTORY_BYTES = 2 * 1024 * 1024
HASH = re.compile(r'[a-f0-9]{64}\Z')
PROFILE_FILES = {'BASE.md', 'SOUL.md', 'PROFILE.md'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate activation field')
        result[key] = value
    return result


def _linked(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    )


def _safe(root, relative, *, allow_missing=False):
    rel = PurePosixPath(relative)
    if (not isinstance(relative, str) or not relative or rel.is_absolute()
            or rel.as_posix() != relative or '\\' in relative
            or any(part in ('', '.', '..') for part in rel.parts)):
        raise ValueError('Invalid project-relative activation path')
    path = root
    for index, part in enumerate(rel.parts):
        path = path / part
        if _linked(path):
            raise ValueError('Linked activation path is forbidden')
        if not path.exists():
            if allow_missing and index == len(rel.parts) - 1:
                continue
            if index != len(rel.parts) - 1 or not allow_missing:
                raise ValueError('Activation path is unavailable')
        elif not path.resolve().is_relative_to(root):
            raise ValueError('Activation path escapes project root')
    return path


def _read_json(path, limit):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Activation evidence exceeds its size limit')
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid activation number')))


def _files(value):
    if (not isinstance(value, dict) or not value
            or any(not isinstance(path, str) or not HASH.fullmatch(digest)
                   for path, digest in value.items())):
        raise ValueError('Invalid installed activation file hashes')
    return value


@contextmanager
def _update_locks(root):
    """Reuse the updater's crash-releasing locks; live or unknown owners defer."""
    try:
        from .profile_update import _locks
    except ImportError:  # Direct Office execution places scripts/ on sys.path.
        from profile_update import _locks
    with _locks(root, root / '.bot-mode/local/installations.json'):
        yield


class ActivationHost:
    """Issue receipts only for an exact profile snapshot used by a completed Office turn."""

    def __init__(self, project):
        self.root = Path(project).resolve(strict=True)
        self._journal_cache = {}

    def _journals(self):
        folder = _safe(self.root, '.bot-mode/local/profile-updates', allow_missing=True)
        if not folder.exists():
            self._journal_cache.clear()
            return []
        if _linked(folder) or not folder.is_dir() or not folder.resolve().is_relative_to(self.root):
            return []
        children = list(itertools.islice(folder.iterdir(), MAX_TRANSACTIONS + 1))
        if len(children) > MAX_TRANSACTIONS:
            return []
        found, cache, total, bytes_read = [], {}, 0, 0
        for child in children:
            try:
                valid_id = str(uuid.UUID(child.name)) == child.name
            except ValueError:
                valid_id = False
            if (not valid_id or _linked(child) or not child.is_dir()
                    or not child.resolve().is_relative_to(folder.resolve())):
                continue
            path = _safe(self.root, f'.bot-mode/local/profile-updates/{child.name}/journal.json')
            if _linked(path) or not path.is_file():
                continue
            details = path.stat()
            fingerprint = (details.st_mtime_ns, details.st_size)
            cached = self._journal_cache.get(child.name)
            if cached and cached[:2] == fingerprint:
                summary = cached[2]
            else:
                if details.st_size > MAX_JOURNAL_BYTES:
                    summary = None
                elif bytes_read + details.st_size > MAX_INDEX_BYTES:
                    self._journal_cache.clear()
                    return []
                else:
                    bytes_read += details.st_size
                    summary = None
                    data = _read_json(path, MAX_JOURNAL_BYTES)
                    if (not isinstance(data, dict) or data.get('schema') != 1
                            or data.get('transaction_id') != child.name):
                        summary = None
                    else:
                        summary = {key: data.get(key) for key in
                                   ('phase', 'pending_activation', 'selected_targets')}
                cache[child.name] = (*fingerprint, summary)
            if summary is None or summary.get('phase') != 'applied':
                continue
            size = len(_canonical(summary))
            total += size
            if total > MAX_INDEX_BYTES:
                self._journal_cache.clear()
                return []
            found.append((child.name, summary))
        self._journal_cache = cache
        return found

    def _read_journal(self, tx_id):
        path = _safe(self.root, f'.bot-mode/local/profile-updates/{tx_id}/journal.json')
        if _linked(path) or not path.is_file():
            return None
        journal = _read_json(path, MAX_JOURNAL_BYTES)
        if (not isinstance(journal, dict) or journal.get('schema') != 1
                or journal.get('transaction_id') != tx_id or journal.get('phase') != 'applied'):
            return None
        return journal

    @staticmethod
    def _candidate_targets(journal, workspace, profile_files):
        pending = journal.get('pending_activation')
        if (not isinstance(pending, dict) or pending.get('state') != 'pending_activation'
                or pending.get('activated') is not False or not isinstance(pending.get('targets'), list)):
            return []
        selected = {item.get('inventory_component'): item for item in journal.get('selected_targets', [])
                    if isinstance(item, dict)}
        result = []
        for target in pending['targets']:
            if not isinstance(target, dict) or target.get('path') != workspace:
                continue
            component_id = target.get('component')
            selection = selected.get(component_id)
            if (not isinstance(selection, dict) or selection.get('kind') != 'profile'
                    or selection.get('root') != workspace):
                continue
            try:
                files = _files(target.get('files'))
            except ValueError:
                continue
            prefix = workspace.rstrip('/') + '/agent/'
            if (all(path.startswith(prefix) and profile_files.get(path) == digest
                    for path, digest in files.items())):
                result.append(target)
        return result

    @staticmethod
    def _workspace(profile_files):
        workspaces = set()
        for path in profile_files:
            parts = PurePosixPath(path).parts
            if len(parts) < 3 or parts[-2] != 'agent' or parts[-1] not in PROFILE_FILES:
                return None
            workspaces.add(PurePosixPath(*parts[:-2]).as_posix())
        return next(iter(workspaces)) if len(workspaces) == 1 else None

    def _target_matches(self, target, bot, profile_files, inventory):
        if (not isinstance(target, dict) or target.get('path') is None
                or target.get('component') is None or target.get('version') is None):
            return None
        try:
            files = _files(target.get('files'))
            revision = target.get('installed_sha256')
            if not isinstance(revision, str) or hashlib.sha256(_canonical(files)).hexdigest() != revision:
                return None
            # Every transaction file must have been included in the Office prompt.
            if any(profile_files.get(path) != digest for path, digest in files.items()):
                return None
            # The entire profile snapshot must still be installed at completion.
            for path, digest in profile_files.items():
                if PurePosixPath(path).name not in PROFILE_FILES:
                    return None
                installed = _safe(self.root, path)
                if not installed.is_file() or hashlib.sha256(installed.read_bytes()).hexdigest() != digest:
                    return None
            component_id, target_path = target['component'], target['path']
            current = inventory.get(component_id)
            if (current is None or current.get('kind') != 'profile'
                    or current.get('path') != target_path or current.get('version') != target['version']):
                return None
            if not isinstance(target_path, str) or not isinstance(bot, str) or not bot:
                return None
            prefix = target_path.rstrip('/') + '/'
            if not any(path.startswith(prefix) for path in files):
                return None
            return component_id, revision
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _save_activation(self, tx_id, component_id, revision, bot, thread_id,
                         turn_id, completed_at, current_bindings, now):
        txrel = f'.bot-mode/local/profile-updates/{tx_id}'
        txroot = _safe(self.root, txrel)
        path = _safe(self.root, txrel + '/activation.json', allow_missing=True)
        if _linked(path):
            return False
        try:
            state = _read_json(path, MAX_JOURNAL_BYTES) if path.exists() else {
                'schema': 1, 'records': {}}
            if (not isinstance(state, dict) or set(state) != {'schema', 'records'}
                    or type(state.get('schema')) is not int or state['schema'] != 1
                    or not isinstance(state.get('records'), dict)):
                return False
            records = state['records']
            record = records.get(component_id)
            if record is None:
                record = activation.pending(component_id, revision,
                                            [{'bot': bot, 'thread_id': thread_id}])
            elif (not isinstance(record, dict) or record.get('installed_revision') != revision
                  or not isinstance(record.get('consumers'), list)
                  or not any(isinstance(consumer, dict)
                             and consumer.get('bot') == bot
                             and consumer.get('thread_id') == thread_id
                             for consumer in record['consumers'])):
                # Do not rebind a different host's consumer to this Office thread.
                return False
            receipt = {
                'bot': bot,
                'thread_id': thread_id,
                'turn_id': turn_id,
                'component': component_id,
                'revision': revision,
                'completed_at': completed_at,
                'host_verified': True,
            }
            activity = {
                'bot': bot, 'thread_id': thread_id, 'as_of': now.isoformat(),
                'state': 'idle', 'paused': False, 'pending_writes': False,
                'ownership': 'office',
            }
            records[component_id] = activation.acknowledge(
                record, receipt, revision, {bot: thread_id}, activity, now=now)
            encoded = _canonical(state) + b'\n'
            if len(encoded) > MAX_JOURNAL_BYTES:
                return False
            descriptor, name = tempfile.mkstemp(prefix='.activation-', suffix='.tmp', dir=txroot)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return True
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def complete_office_turn(self, *, bot, thread_id, turn_id, terminal_status,
                             profile_files, current_bindings, idle, paused,
                             pending_writes=False, ownership='office',
                             completed_at=None, now=None):
        """Persist eligible receipts; all failures defer activation without affecting the turn."""
        if (terminal_status != 'completed' or ownership != 'office' or idle is not True
                or paused is not False or pending_writes is not False
                or not all(isinstance(value, str) and value for value in (bot, thread_id, turn_id))
                or not isinstance(profile_files, dict) or not isinstance(current_bindings, dict)
                or current_bindings.get(bot) != thread_id):
            return 0
        checked_at = now or datetime.now(timezone.utc)
        completed_at = completed_at or checked_at.isoformat().replace('+00:00', 'Z')
        if not isinstance(completed_at, str):
            return 0
        try:
            snapshot = _files(profile_files)
            workspace = self._workspace(snapshot)
            if workspace is None or self._office_workspace(bot) != workspace:
                return 0
            candidates = []
            for tx_id, journal in self._journals():
                candidates.extend((tx_id, target['component'])
                                  for target in self._candidate_targets(journal, workspace, snapshot))
            count = 0
            for tx_id, component_id in candidates:
                try:
                    with _update_locks(self.root):
                        journal = self._read_journal(tx_id)
                        if journal is None or self._office_workspace(bot) != workspace:
                            continue
                        targets = self._candidate_targets(journal, workspace, snapshot)
                        target = next((item for item in targets if item.get('component') == component_id), None)
                        if target is None:
                            continue
                        matched = self._target_matches(target, bot, snapshot, self._installed_inventory())
                        if matched is None:
                            continue
                        component_id, revision = matched
                        active_at = now or datetime.now(timezone.utc)
                        if self._save_activation(tx_id, component_id, revision, bot, thread_id,
                                                 turn_id, completed_at, current_bindings, active_at):
                            count += 1
                except (OSError, ValueError, TypeError, KeyError):
                    continue
            return count
        except (OSError, ValueError, TypeError, KeyError):
            return 0

    def _installed_inventory(self):
        path = _safe(self.root, '.bot-mode/local/installations.json')
        if _linked(path) or not path.is_file():
            return {}
        data = _read_json(path, MAX_INVENTORY_BYTES)
        if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('components'), list):
            return {}
        rows = {}
        for item in data['components']:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] in rows:
                return {}
            rows[item['id']] = item
        return rows

    def _office_workspace(self, bot):
        try:
            from .team_room import project as office_project, roster
            data = roster.load(self.root)
            participants = [item for item in roster.active(data) if item.get('bot') == bot]
            if len(participants) != 1 or not isinstance(participants[0].get('workspace'), str):
                return None
            workspace = participants[0]['workspace']
            profile, _, _, _, _ = office_project.profile_location(bot, self.root)
            expected = _safe(self.root, workspace + '/agent')
            return workspace if profile.resolve(strict=True) == expected.resolve(strict=True) else None
        except (OSError, ValueError, TypeError):
            return None
