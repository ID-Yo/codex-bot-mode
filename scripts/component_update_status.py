"""Bounded read-only replay of profile transaction journals for Office status.

This is evidence of file application/recovery, never proof of agent activation.
"""
import json
from itertools import islice
from pathlib import Path
import re
import uuid

MAX_JOURNAL = 4 * 1024 * 1024
MAX_TRANSACTIONS = 256
PHASES = {
    'preparing': 'pending', 'staging': 'pending', 'staged': 'pending',
    'backed_up': 'pending', 'inventory_committed': 'pending', 'committing': 'pending',
    'applied': 'applied_pending_activation', 'rolling_back': 'pending',
    'rolled_back': 'rolled_back', 'failed': 'failed',
}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate journal field')
        result[key] = value
    return result


def _linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def _safe(root, parts):
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink() or (path.exists() and _linked(path)):
            raise ValueError('Linked transaction evidence is not allowed')
        if path.exists() and not path.resolve().is_relative_to(root):
            raise ValueError('Transaction evidence escapes configured project')
    return path


def read_history(projects):
    """Read only explicitly configured roots; never accept a browser path."""
    history, warnings = [], []
    for project_id, location in projects.items():
        try:
            root = Path(location).resolve(strict=True)
            if not root.is_dir():
                raise ValueError('Configured project root is not a directory')
            folder = _safe(root, ('.bot-mode', 'local', 'profile-updates'))
            if not folder.exists():
                continue
            if not folder.is_dir():
                raise ValueError('Transaction directory is unavailable')
            count = 0
            for child in sorted(islice(folder.iterdir(), MAX_TRANSACTIONS + 1), key=lambda p: p.name):
                if count >= MAX_TRANSACTIONS:
                    warnings.append({'project_id': project_id, 'reason': 'transaction_limit'})
                    break
                count += 1
                try:
                    if str(uuid.UUID(child.name)) != child.name:
                        raise ValueError('Invalid transaction identity')
                    file = _safe(root, ('.bot-mode', 'local', 'profile-updates', child.name, 'journal.json'))
                    with file.open('rb') as stream:
                        raw = stream.read(MAX_JOURNAL + 1)
                    if len(raw) > MAX_JOURNAL:
                        raise ValueError('Oversized transaction journal')
                    data = json.loads(raw, object_pairs_hook=_unique,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite journal data')))
                    if (not isinstance(data, dict) or data.get('schema') != 1
                            or data.get('transaction_id') != child.name
                            or data.get('phase') not in PHASES
                            or not isinstance(data.get('plan_sha256'), str)
                            or not re.fullmatch(r'[a-f0-9]{64}', data['plan_sha256'])
                            or not isinstance(data.get('selected_targets'), list)):
                        raise ValueError('Invalid transaction journal')
                    targets = data['selected_targets']
                    components = [t.get('release_component') for t in targets if isinstance(t, dict)]
                    if len(components) != len(targets) or any(not isinstance(c, str) or not c for c in components):
                        raise ValueError('Invalid transaction components')
                    history.append({'project_id': project_id, 'transaction': child.name,
                                    'state': PHASES[data['phase']], 'phase': data['phase'],
                                    'plan_sha256': data['plan_sha256'], 'components': components,
                                    'verified_active': False, 'source': 'engine_journal'})
                except (OSError, ValueError, TypeError, UnicodeError):
                    warnings.append({'project_id': project_id, 'reason': 'invalid_transaction_evidence'})
        except (OSError, ValueError):
            warnings.append({'project_id': project_id, 'reason': 'transaction_history_unavailable'})
    return {'history': history, 'warnings': warnings,
            'history_coverage': 'partial' if warnings else 'complete',
            'durability': 'engine_journal'}
