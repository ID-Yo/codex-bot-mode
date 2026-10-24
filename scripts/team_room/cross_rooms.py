"""Cross-project room participants: server-side eligibility, hierarchy and lead rules.

Identity is the registry bot_key (project_id:bot_id). No chat content, memory or
transcript is read; only the registry overview (appointed agents and bound threads).
"""
import re

RANKS = {'chief of staff': 3, 'project director': 2, 'director': 2, 'project manager': 1, 'manager': 1}
MAX_PARTICIPANTS = 24
FIELDS = ('bot_key', 'name', 'role', 'rank', 'project_id', 'project_name', 'team_name')


class CrossRoomError(Exception):
    """Typed API error: always serialized as {code, error, ...} with an HTTP status."""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.payload = status, {'code': code, 'error': message, **extra}


def rank(role):
    return RANKS.get(str(role or '').strip().casefold(), 0)


def candidate_index(project_root):
    """All appointed agents with eligibility decided by the server."""
    from . import registry
    overview = registry.build_overview(registry.registry_path(project_root), current_project_root=project_root)
    if overview.get('status') not in ('ok', 'not_configured'):
        # A damaged registry is an error, never an empty candidate list.
        raise CrossRoomError(503, 'registry_unavailable', 'Agent registry is unavailable')
    projects = {p.get('id'): p for p in overview.get('projects', [])}
    rows = []
    for bot in overview.get('bots', []):
        project = projects.get(bot.get('project_id'))
        reason_code = None
        reason = None
        if project is not None and (project.get('status') != 'ok' or not project.get('applicable', True)):
            reason_code, reason = 'project_unavailable', 'Project is unavailable'
        elif not bot.get('thread_id'):
            reason_code, reason = 'no_desktop_thread', 'No Desktop thread is bound to this agent'
        rows.append({'bot_key': bot['bot_key'], 'name': bot.get('name') or bot.get('bot_id'),
                     'role': bot.get('role'), 'rank': rank(bot.get('role')),
                     'project_id': bot.get('project_id'), 'project_name': bot.get('project_name'),
                     'team_name': bot.get('team_name'), 'eligible': reason_code is None,
                     'reason': reason, 'reason_code': reason_code})
    return rows


def search(rows, q='', project=''):
    q = (q or '').strip().casefold()[:80]
    out = []
    for row in rows:
        if project and row['project_id'] != project:
            continue
        if q and not any(q in str(row.get(k) or '').casefold() for k in ('name', 'role', 'project_name', 'bot_key')):
            continue
        out.append(row)
    return out


def resolve_participants(keys, index):
    if (not isinstance(keys, list) or not 2 <= len(keys) <= MAX_PARTICIPANTS
            or any(not isinstance(k, str) or not re.fullmatch(r'[\w.:@-]{1,160}', k) for k in keys)
            or len(set(keys)) != len(keys)):
        raise CrossRoomError(400, 'invalid_participants', 'Select 2 to %d different participants' % MAX_PARTICIPANTS)
    by_key = {row['bot_key']: row for row in index}
    people = []
    for key in keys:
        row = by_key.get(key)
        if row is None:
            raise CrossRoomError(403, 'not_allowed', 'This agent is not an appointed participant',
                                 bot_key=key, reason_code='not_appointed')
        if not row['eligible']:
            raise CrossRoomError(403, 'not_allowed', row['reason'] or 'Agent is not eligible',
                                 bot_key=key, reason_code=row['reason_code'])
        people.append({k: row.get(k) for k in FIELDS})
    return people


def resolve_lead(people, lead=None, lead_override=False):
    """Return {'bot_key', 'basis'} using hierarchy, manual tie-break or explicit override."""
    if lead is not None and not isinstance(lead, str):
        raise CrossRoomError(400, 'invalid_lead', 'Lead must be a participant bot_key')
    if lead_override not in (True, False, None):
        raise CrossRoomError(400, 'invalid_lead', 'lead_override must be boolean')
    top_rank = max(p['rank'] for p in people)
    top = [p['bot_key'] for p in people if p['rank'] == top_rank]
    if lead is None:
        if len(top) == 1:
            return {'bot_key': top[0], 'basis': 'hierarchy'}
        raise CrossRoomError(409, 'lead_required', 'Several participants share the highest rank; choose the lead',
                             candidates=top)
    if lead not in {p['bot_key'] for p in people}:
        raise CrossRoomError(400, 'invalid_lead', 'Lead must be a participant')
    if lead in top:
        return {'bot_key': lead, 'basis': 'hierarchy' if len(top) == 1 else 'manual'}
    if lead_override is True:
        return {'bot_key': lead, 'basis': 'override'}
    raise CrossRoomError(409, 'override_required', 'A lead below the highest rank needs lead_override', top=top)
