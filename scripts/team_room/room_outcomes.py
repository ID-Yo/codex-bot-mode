"""Desired outcomes for cross-project rooms: plan approval or explicit organize and start (#213).

Discussion never starts work. An outcome becomes one assignment per participating project, addressed to
that project's appointed Manager. Dispatch goes through the Manager's existing Desktop session outbox;
an assignment is shown as accepted only from outbox evidence, and linked to a task only from an issue URL.
"""
import json
import re
import threading
import uuid
from datetime import datetime, timezone

from .cross_rooms import CrossRoomError

MODES = ('plan_approval', 'organize_start')
ISSUE_URL = re.compile(r'https://github\.com/[\w.-]+/[\w.-]+/issues/\d{1,9}')
SETTLED = ('accepted', 'task_linked', 'completed')
MAX_OUTCOMES = 200


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Deps:
    """Server-side bridge to project Managers: identity, delivery and delivery evidence."""

    def __init__(self, project_root, project_managers):
        self.root, self.managers = project_root, project_managers

    def manager(self, project_id):
        from . import registry
        found = registry.resolve_project_manager(registry.registry_path(self.root), project_id, current_project_root=self.root)
        return found if found and found.get('status') == 'ok' else None

    def send(self, project_id, text, client_id, expected_thread=None):
        if not self.managers or not self.managers.bridge:
            raise RuntimeError('Desktop connection is unavailable')
        session, _ = self.managers.get(project_id, expected_thread)
        return session.send(text, session.bot, client_id)

    def delivery_state(self, project_id, delivery_id):
        session, _ = self.managers.get(project_id)
        with session.lock:
            row = session.db.execute('SELECT state FROM outbox WHERE id=?', (delivery_id,)).fetchone()
        return row['state'] if row else None

    def reply_text(self, project_id, delivery_id):
        session, _ = self.managers.get(project_id)
        return reply_of(session, delivery_id)


def reply_of(session, delivery_id):
    """Final answer text of the single turn started by one delivery; nothing else of the chat is read."""
    with session.lock:
        row = session.db.execute('SELECT turn_id FROM outbox WHERE id=?', (delivery_id,)).fetchone()
    turn = row['turn_id'] if row else None
    if not turn:
        return None
    answers = [m for m in session.state().get('journal', []) if m.get('turn_id') == turn and m.get('sender') != 'owner'
               and isinstance(m.get('text'), str) and m['text'].strip()]
    final = [m for m in answers if m.get('phase') == 'final_answer'] or answers
    return final[-1]['text'].strip() if final else None


def outcome_view(item):
    keys = ('id', 'version', 'text', 'mode', 'state', 'lead_bot_key', 'created', 'assignments', 'blocker')
    view = {k: item[k] for k in keys}
    view['assignments'] = [{k: v for k, v in a.items() if v is not None and not k.startswith('_')} for a in item['assignments']]
    return view


def derive_state(item):
    if item['state'] == 'cancelled':
        return 'cancelled'
    states = [a['state'] for a in item['assignments']]
    if item['state'] == 'awaiting_approval':
        return 'awaiting_approval'
    if 'failed' in states:
        return 'failed'
    if 'blocked' in states:
        return 'blocked'
    if states and all(s in SETTLED for s in states):
        return 'dispatched'
    return 'organizing'


class Outcomes:
    def __init__(self, directory):
        self.file = directory / 'outcomes.json'
        self.lock = threading.RLock()

    def _load(self):
        return json.loads(self.file.read_text(encoding='utf-8')) if self.file.exists() else []

    def _save(self, rows, after_dispatch=False):
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.file.with_suffix('.tmp')
            temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.file)
        except OSError:
            if after_dispatch:
                raise CrossRoomError(503, 'outcome_state_unsaved', 'Work may already be delivered but its state could not be saved; '
                                     'refresh before retrying (the same request will not be sent twice)')
            raise CrossRoomError(503, 'storage_unavailable', 'Outcome storage is unavailable; nothing was sent')

    def _find(self, rows, oid):
        item = next((r for r in rows if r['id'] == oid), None)
        if item is None:
            raise CrossRoomError(404, 'unknown_outcome', 'Unknown outcome')
        return item

    @staticmethod
    def _version(item, version):
        if not isinstance(version, int) or isinstance(version, bool):
            raise CrossRoomError(400, 'invalid_version', 'Outcome version required')
        if version != item['version']:
            raise CrossRoomError(409, 'version_conflict', 'Outcome changed; reload before acting', version=item['version'])

    def list(self, room, deps):
        with self.lock:
            rows = self._load()
            changed = False
            for item in rows:
                changed |= self._advance(room, item, deps)
            if changed:
                self._save(rows, after_dispatch=True)
            return {'items': [outcome_view(r) for r in rows]}

    def create(self, room, text, mode, client_id, confirmed, deps):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
            raise CrossRoomError(400, 'invalid_text', 'Describe the desired result (1 to 4000 characters)')
        if mode not in MODES:
            raise CrossRoomError(400, 'invalid_mode', 'Mode must be plan_approval or organize_start')
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
            raise CrossRoomError(400, 'invalid_client_message_id', 'Valid client_message_id required')
        text = text.strip()
        with self.lock:
            rows = self._load()
            previous = next((r for r in rows if r['client_id'] == client_id), None)
            if previous:
                if (previous['text'], previous['mode']) != (text, mode):
                    raise CrossRoomError(409, 'idempotency_conflict', 'Request ID already belongs to a different outcome')
                return outcome_view(previous)
            if mode == 'organize_start' and confirmed is not True:
                raise CrossRoomError(403, 'not_authorized', 'Organize and start needs explicit owner confirmation')
            if len(rows) >= MAX_OUTCOMES:
                raise CrossRoomError(409, 'too_many_outcomes', 'This room has too many outcomes')
            assignments = self._propose(room, text, deps)
            item = {'id': uuid.uuid4().hex, 'version': 1, 'text': text, 'mode': mode, 'state': 'awaiting_approval',
                    'lead_bot_key': room['lead']['bot_key'], 'created': now(), 'assignments': assignments,
                    'blocker': None, 'client_id': client_id}
            item['blocker'] = self._blocker(item)  # a blocked proposal is visible before approval
            if mode == 'organize_start':
                self._approve(item)
            rows.append(item)
            self._save(rows)  # durable outcome first; nothing is sent before this succeeds
            if mode == 'organize_start':
                self._advance(room, item, deps)
                self._save(rows, after_dispatch=True)
            return outcome_view(item)

    def _propose(self, room, text, deps):
        projects = []
        for person in room['participants']:
            if person.get('project_id') and person['project_id'] not in [p[0] for p in projects]:
                projects.append((person['project_id'], person.get('project_name') or person['project_id']))
        if not projects:
            raise CrossRoomError(409, 'no_projects', 'No participating project can receive work')
        lead_project = next((p['project_id'] for p in room['participants'] if p['bot_key'] == room['lead']['bot_key']), None)
        projects.sort(key=lambda p: p[0] != lead_project)
        first_line = text.splitlines()[0][:80]
        rows = []
        for index, (pid, name) in enumerate(projects, 1):
            manager = deps.manager(pid)
            row = {'id': 'a%d' % index, 'project_id': pid, 'manager_bot_key': manager['bot_key'] if manager else None,
                   'title': f'{name}: {first_line}', 'depends_on': [], 'state': 'proposed'}
            if manager is None:
                row.update(state='blocked', reason='No single appointed Manager is available for this project')
            elif not manager.get('thread_id'):
                row.update(state='blocked', reason='The project Manager has no Desktop chat bound; nothing can be sent')
            else:
                row['_thread'] = manager['thread_id']  # approved Desktop binding, kept private
            rows.append(row)
        return rows

    def _approve(self, item):
        item['state'] = 'organizing'
        item['approved_at'] = now()
        for row in item['assignments']:
            if row['state'] == 'proposed':
                row['state'] = 'approved'

    def approve(self, room, oid, version, confirmed, deps):
        with self.lock:
            rows = self._load()
            item = self._find(rows, oid)
            self._version(item, version)
            if confirmed is not True:
                raise CrossRoomError(403, 'not_authorized', 'Approval needs explicit owner confirmation')
            if item['state'] != 'awaiting_approval':
                raise CrossRoomError(409, 'not_awaiting_approval', 'Outcome is not waiting for approval')
            self._approve(item)
            item['version'] += 1
            self._save(rows)  # durable approval first; nothing is sent before this succeeds
            self._advance(room, item, deps)
            self._save(rows, after_dispatch=True)
            return outcome_view(item)

    def cancel(self, room, oid, version):
        with self.lock:
            rows = self._load()
            item = self._find(rows, oid)
            self._version(item, version)
            if item['state'] != 'awaiting_approval':
                raise CrossRoomError(409, 'not_cancellable', 'Work was already dispatched and cannot be recalled here')
            item['state'] = 'cancelled'
            item['version'] += 1
            self._save(rows)
            return outcome_view(item)

    def link_issue(self, room, oid, aid, version, url, deps):
        with self.lock:
            rows = self._load()
            item = self._find(rows, oid)
            self._version(item, version)
            row = next((a for a in item['assignments'] if a['id'] == aid), None)
            if row is None:
                raise CrossRoomError(404, 'unknown_assignment', 'Unknown assignment')
            if not isinstance(url, str) or not ISSUE_URL.fullmatch(url):
                raise CrossRoomError(400, 'invalid_issue_url', 'A GitHub issue URL is required')
            row['issue_url'] = url
            if row['state'] == 'accepted':
                row['state'] = 'task_linked'
            item['version'] += 1
            self._advance(room, item, deps)
            self._save(rows, after_dispatch=True)
            return outcome_view(item)

    def _advance(self, room, item, deps):
        """Dispatch ready assignments and refresh delivery evidence. Returns True when anything changed."""
        if item['state'] in ('awaiting_approval', 'cancelled'):
            return False
        before = json.dumps(item, sort_keys=True)
        by_id = {a['id']: a for a in item['assignments']}
        for row in item['assignments']:
            if row['state'] == 'approved' and all(by_id[d]['state'] in SETTLED for d in row['depends_on'] if d in by_id):
                self._dispatch(room, item, row, deps)
            elif row['state'] == 'dispatching' and row.get('delivery_id'):
                self._observe(row, deps)
        blocked = [a for a in item['assignments'] if a['state'] in ('blocked', 'failed')]
        item['blocker'] = self._blocker(item)
        item['state'] = derive_state(item)
        return json.dumps(item, sort_keys=True) != before

    @staticmethod
    def _blocker(item):
        blocked = [a for a in item['assignments'] if a['state'] in ('blocked', 'failed')]
        if not blocked:
            return None
        linked = next((a.get('issue_url') for a in blocked if a.get('issue_url')), None)
        return {'issue_url': linked, 'issue_required': linked is None,
                'reason': blocked[0].get('reason') or 'Assignment could not be dispatched'}

    def _dispatch(self, room, item, row, deps):
        try:
            current = deps.manager(row['project_id'])
        except Exception:
            current = None
        if not current:
            row.update(state='blocked', reason='The project Manager is unavailable; nothing was sent')
            return
        if current['bot_key'] != row['manager_bot_key']:
            row.update(state='blocked', reason='The project Manager changed since the plan was proposed; nothing was sent')
            return
        approved_thread = row.get('_thread')
        if not approved_thread:
            row.update(state='blocked', reason='The approved Manager Desktop chat binding is missing; nothing was sent')
            return
        if current.get('thread_id') != approved_thread:
            row.update(state='blocked', reason='The Manager Desktop chat changed since the plan was proposed; nothing was sent')
            return
        message = ('Cross-project room request (' + room['name'] + ', outcome ' + item['id'] + ').\n'
                   'Desired result: ' + item['text'] + '\n'
                   'Your assignment: ' + row['title'] + '\n'
                   'Room lead: ' + room['lead']['bot_key'] + '. Plan the work in your project, record it as a task, '
                   'and report the task link to the lead and the owner.')
        try:
            receipt = deps.send(row['project_id'], message, item['id'] + ':' + row['id'], approved_thread)
        except Exception:
            row.update(state='blocked', reason='Manager delivery could not be started; nothing was sent')
            return
        row['delivery_id'] = receipt.get('delivery_id')
        row['state'] = 'dispatching'
        self._observe(row, deps)

    def _observe(self, row, deps):
        try:
            state = deps.delivery_state(row['project_id'], row['delivery_id'])
        except Exception:
            return
        if state in ('accepted', 'completed'):
            row['state'] = 'task_linked' if row.get('issue_url') else 'accepted'
            row.setdefault('accepted_at', now())
        elif state in ('failed', 'interrupted', 'cancelled'):
            row.update(state='failed', reason='Manager delivery failed')
        elif state in ('ambiguous',):
            row.update(state='blocked', reason='Delivery outcome is unclear; it will not be resent automatically')
