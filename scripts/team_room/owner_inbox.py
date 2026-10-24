"""Read-only owner dependencies over approved summaries and explicit requests.

No chat, STATE, memory, network access or visibility filtering. A missing source
is incomplete coverage, never evidence that the owner has nothing to do.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from . import project_status, registry, roster

MAX_BYTES = 256 * 1024
MAX_ITEMS = 500
MAX_SCOPES = 100
ISSUE = re.compile(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*$')


def stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def text(value, limit=600):
    return value[:limit] if isinstance(value, str) else ''


def issue_url(value):
    return value if isinstance(value, str) and ISSUE.fullmatch(value) else None


def freshness(value, at):
    source = stamp(value)
    if not source or not 0 <= (at - source).total_seconds():
        return 'unknown'
    return 'fresh' if (at - source).total_seconds() <= 48 * 3600 else 'stale'


def summary_model(root):
    """Read existing snapshots only, refusing escaped paths before opening.

    Do not call read_model: its cache-miss path assembles approved reports and
    may write a cache. This projection must stay bounded and read-only.
    """
    errors = []
    for file in (project_status.derived_cache_path(root), project_status.path(root)):
        try:
            resolved = file.resolve()
            if not resolved.is_relative_to(root):
                raise ValueError('Summary path escapes project')
            if not resolved.exists():
                continue
            if not resolved.is_file() or resolved.stat().st_size > project_status.MAX_BYTES:
                raise ValueError('Invalid summary size')
            raw = json.loads(resolved.read_text(encoding='utf-8'))
            snapshot = raw.get('snapshot', raw) if isinstance(raw, dict) else raw
            return {'available': True, 'snapshot': project_status.validate(snapshot), 'warnings': errors}
        except (OSError, ValueError, AttributeError, UnicodeError):
            errors.append('A project summary is invalid or outside its allowed boundary')
    return {'available': False, 'warnings': errors}


def room_blockers(root, warnings):
    """Blocked or failed cross-project room outcomes that have no tracking issue (read-only)."""
    rooms_dir = root / '.bot-mode' / 'team-room-v1' / 'rooms'
    index = rooms_dir / 'rooms.json'
    if not index.exists():
        return []
    found = []
    try:
        if not index.resolve(strict=True).is_relative_to(root) or index.stat().st_size > MAX_BYTES:
            raise ValueError('Invalid rooms index')
        rows = json.loads(index.read_text(encoding='utf-8'))
        if not isinstance(rows, list):
            raise ValueError('Invalid rooms index')
    except (OSError, ValueError, UnicodeError):
        warnings.append('The room index is unavailable or invalid')
        return []
    for row in rows[:MAX_SCOPES]:
        if not isinstance(row, dict) or row.get('kind') != 'cross_project' or not re.fullmatch(r'[0-9a-f]{32}', str(row.get('id'))):
            continue
        names = {p.get('project_id'): p.get('project_name') for p in row.get('participants', []) if isinstance(p, dict)}
        file = rooms_dir / row['id'] / 'outcomes.json'
        if not file.exists():
            continue
        try:
            if not file.resolve(strict=True).is_relative_to(root) or file.stat().st_size > MAX_BYTES:
                raise ValueError('Invalid outcomes file')
            outcomes = json.loads(file.read_text(encoding='utf-8'))
            if not isinstance(outcomes, list):
                raise ValueError('Invalid outcomes file')
            for outcome in outcomes[:MAX_ITEMS]:
                blocker = outcome.get('blocker') if isinstance(outcome, dict) else None
                if not isinstance(blocker, dict) or outcome.get('state') not in ('blocked', 'failed') or issue_url(blocker.get('issue_url')):
                    continue
                blocked = next((a for a in outcome.get('assignments', []) if isinstance(a, dict) and a.get('state') in ('blocked', 'failed')), {})
                found.append({'project_id': text(blocked.get('project_id'), 80) or 'current',
                              'assignment_id': text(blocked.get('id'), 20),
                              'project_name': text(names.get(blocked.get('project_id')), 120) or text(blocked.get('project_id'), 80) or root.name,
                              'outcome_id': text(outcome.get('id'), 80), 'room_id': row['id'],
                              'title': text(outcome.get('text'), 240), 'reason': text(blocker.get('reason'), 300),
                              'created': outcome.get('created')})
        except (OSError, ValueError, AttributeError, UnicodeError):
            warnings.append('A room outcomes file is unavailable or invalid')
    return found


def pending_teams(scopes, warnings):
    """Created projects whose team is still unappointed (read-only marker plus roster)."""
    found = []
    for pid, name, scope in scopes:
        marker = scope / '.bot-mode' / 'local' / 'pending-team.json'
        if not marker.exists():
            continue
        try:
            if not marker.resolve(strict=True).is_relative_to(scope) or marker.stat().st_size > MAX_BYTES:
                raise ValueError('Invalid marker')
            body = json.loads(marker.read_text(encoding='utf-8'))
            appointed = False
            if (scope / '.bot-mode' / 'roster.json').exists():
                # roster.load merges local chat bindings; the canonical file alone has no thread ids.
                data = roster.load(scope)
                appointed = any(p.get('status') == 'active' and p.get('role') == 'Manager' and p.get('thread_id')
                                for p in data.get('participants', []))
            if not appointed:
                found.append({'project_id': pid, 'project_name': name, 'handoff': text(body.get('handoff'), 600),
                              'issue_url': issue_url(body.get('issue_url')),
                              'created': datetime.fromtimestamp(marker.stat().st_mtime, timezone.utc).isoformat()})
        except (OSError, ValueError, AttributeError, UnicodeError):
            warnings.append('A pending team marker is unavailable or invalid')
    return found


def build(project_root, *, now=None):
    root = Path(project_root).resolve(strict=True)
    observed = now or datetime.now(timezone.utc).isoformat()
    at = stamp(observed)
    if at is None:
        raise ValueError('Timezone-aware observation time required')
    warnings, candidates = [], []
    config = registry.registry_path(root)
    try:
        if config and (not config.is_file() or config.stat().st_size > MAX_BYTES):
            raise ValueError('Invalid registry size')
        registered = registry.load_registry(config)
    except (OSError, ValueError):
        registered = {'status': 'invalid_config', 'projects': []}
    scopes = []
    registered_projects = registered.get('projects', [])
    if len(registered_projects) > MAX_SCOPES:
        warnings.append('Project scope limit reached')
    for project in registered_projects[:MAX_SCOPES]:
        try:
            project_root = Path(project['root']).resolve(strict=True)
            if project_root.is_dir():
                scopes.append((project['id'], text(project.get('name')) or project['id'], project_root))
            else:
                warnings.append('A registered project root is not a directory')
        except OSError:
            warnings.append('A registered project root is unavailable')
    if not any(scope[2] == root for scope in scopes):
        scopes.append(('current', root.name, root))
    if registered.get('status') == 'invalid_config':
        warnings.append('Project registry is invalid')

    model = summary_model(root)
    warnings.extend(model.get('warnings', []))
    if not model.get('available'):
        warnings.append('Approved project summaries are unavailable')
    snapshot = model.get('snapshot') or {}
    for project in snapshot.get('projects', []):
        source = project.get('source') or {}
        if not source.get('available'):
            warnings.append('Project summary source is unavailable')
        source_at = source.get('as_of')
        for decision in project.get('decisions', []):
            if decision.get('state') != 'pending':
                continue
            url = issue_url(decision.get('source_url'))
            due = decision.get('due')
            candidates.append({
                'project_id': project['id'], 'project_name': project['name'],
                'agent': text(decision.get('requested_by'), 80), 'kind': 'decision',
                'title': text(decision.get('question'), 280),
                'blocked_work': text(decision.get('context')), 'due': due,
                'urgent': decision.get('urgency') == 'urgent' or bool(stamp(due) and stamp(due) <= at),
                'issue_url': url, 'source_at': source_at,
                'freshness': freshness(source_at, at) if source.get('available') else 'unknown',
                'source': {'kind': 'project_decision', 'id': text(decision.get('id'), 80),
                           'project_id': project['id'], 'url': url, 'due': due}})
    for pid, name, scope in scopes:
        if len(candidates) >= MAX_ITEMS:
            warnings.append('Owner inbox collection limit reached')
            break
        ledger = scope / '.bot-mode/local/requests.json'
        if not ledger.exists():
            ledger = scope / '.bot-mode/requests.json'
        if not ledger.exists():
            warnings.append('An owner request ledger is missing')
            continue
        try:
            if not ledger.resolve(strict=True).is_relative_to(scope) or ledger.stat().st_size > MAX_BYTES:
                raise ValueError('Invalid ledger boundary')
            data = json.loads(ledger.read_text(encoding='utf-8'))
            rows = data.get('requests')
            if data.get('schema') != 1 or not isinstance(rows, list) or len(rows) > 2000:
                raise ValueError('Invalid request ledger')
            for row in rows:
                if len(candidates) >= MAX_ITEMS:
                    warnings.append('Owner inbox collection limit reached')
                    break
                if not isinstance(row, dict) or row.get('state') != 'open' or row.get('to') != 'owner':
                    continue
                if not isinstance(row.get('id'), str) or not isinstance(row.get('summary'), str):
                    warnings.append('An owner request is malformed')
                    continue
                url, due = issue_url(row.get('link')), row.get('due')
                opened_at = stamp(row.get('opened'))
                opened = opened_at.isoformat() if opened_at else None
                if opened is None or (due is not None and stamp(due) is None):
                    warnings.append('An owner request has invalid dates')
                candidates.append({
                    'project_id': pid, 'project_name': name, 'agent': text(row.get('from'), 80),
                    'kind': 'request', 'title': text(row['summary'], 300),
                    'blocked_work': '', 'due': stamp(due).isoformat() if stamp(due) else None,
                    'urgent': bool(stamp(due) and stamp(due) <= at),
                    'issue_url': url, 'source_at': opened,
                    'freshness': freshness(opened, at),
                    'source': {'kind': 'owner_request', 'id': text(row['id'], 80),
                               'project_id': pid, 'url': url,
                               'due': stamp(due).isoformat() if stamp(due) else None}})
        except (OSError, ValueError, AttributeError, UnicodeError):
            warnings.append('An owner request ledger is unavailable or invalid')
    for pending in pending_teams(scopes, warnings):
        candidates.append({
            'project_id': pending['project_id'], 'project_name': pending['project_name'], 'agent': 'project setup',
            'kind': 'setup', 'title': 'Appoint the team for the new project ' + pending['project_name'],
            'blocked_work': pending['handoff'], 'due': None, 'urgent': True, 'issue_url': pending['issue_url'],
            'action_hint': 'appoint_team', 'source_at': pending['created'],
            'freshness': freshness(pending['created'], at),
            'source': {'kind': 'pending_team', 'id': pending['project_id'], 'project_id': pending['project_id'],
                       'url': pending['issue_url'], 'due': None}})
    grouped = {}
    for blocker in room_blockers(root, warnings):
        created = stamp(blocker['created'])
        candidates.append({
            'project_id': blocker['project_id'], 'project_name': blocker['project_name'], 'agent': 'cross-project room',
            'kind': 'blocker', 'title': 'Blocked room outcome without an issue: ' + blocker['title'],
            'blocked_work': blocker['reason'], 'due': None, 'urgent': True, 'issue_url': None,
            'source_at': created.isoformat() if created else None,
            'freshness': freshness(blocker['created'], at),
            'source': {'kind': 'room_outcome', 'id': blocker['outcome_id'], 'project_id': blocker['project_id'],
                       'room_id': blocker['room_id'], 'assignment_id': blocker['assignment_id'],
                       'url': None, 'due': None}})
    for row in candidates:
        source = row.pop('source')
        source.update(source_at=row['source_at'], freshness=row['freshness'])
        # One actionable issue across sources; unlinked requests keep scoped IDs.
        key = row['issue_url'] or f"{source['project_id']}:{source['kind']}:{source['id']}"
        if key in grouped:
            item = grouped[key]
            if source not in item['sources']:
                item['sources'].append(source)
            item['urgent'] = item['urgent'] or row['urgent']
            if stamp(row['source_at']) and (not stamp(item['source_at']) or stamp(row['source_at']) > stamp(item['source_at'])):
                item['source_at'], item['freshness'] = row['source_at'], row['freshness']
            if stamp(row['due']) and (not stamp(item['due']) or stamp(row['due']) < stamp(item['due'])):
                item['due'] = row['due']
            continue
        grouped[key] = {**row, 'id': hashlib.sha256(key.encode()).hexdigest()[:24],
                        'sources': [source], 'tracking_required': row['issue_url'] is None,
                        'mixed_freshness': False,
                        'action': ('appoint_team' if row.get('action_hint') == 'appoint_team'
                                   else 'open_source' if row['issue_url'] else 'record_issue')}
    for item in grouped.values():
        item['mixed_freshness'] = len({source['freshness'] for source in item['sources']}) > 1
    items = sorted(grouped.values(), key=lambda item: (not item['urgent'], item['due'] or '9999', item['id']))
    if len(items) > MAX_ITEMS:
        warnings.append('Owner inbox result limit reached')
    return {'schema': 1, 'observed_at': observed, 'items': items[:MAX_ITEMS],
            'complete': not warnings, 'warnings': sorted(set(warnings)),
            'coverage': 'Explicit open owner requests and pending approved project decisions only; no remote issue synchronization.'}
