"""Owner discussion in a cross-project room (#264).

An owner message is delivered to each chosen participant through that participant's own project Manager
Desktop session (its outbox serializes delivery and waits while the chat is busy). The final answer of the
turn started by that delivery is copied back to the room journal. Discussion never starts work: work only
comes from approved outcomes (#213). Only the delivery marker turn is read; no other private chat, memory
or transcript leaves a project.
"""
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .cross_rooms import CrossRoomError

MAX_TEXT = 4000
MAX_REPLY = 8000
MAX_MESSAGES = 500
TERMINAL = ('completed', 'failed', 'blocked')
# Manager outbox state -> room delivery state.
STATES = {'pending': 'queued', 'sending': 'sending', 'accepted': 'running', 'completed': 'completed',
          'failed': 'failed', 'interrupted': 'failed', 'cancelled': 'failed', 'ambiguous': 'blocked'}


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def prompt(room, message_id, text, person):
    people = '; '.join(f"{p['name']} ({p['role']}, {p['project_name']})" for p in room['participants'])
    lead = next((p for p in room['participants'] if p['bot_key'] == room['lead']['bot_key']), None)
    role = ('You are the room lead: coordinate the discussion and give the owner a short summary of what is agreed. '
            if lead and lead['bot_key'] == person['bot_key'] else
            'The room lead is ' + (lead['name'] if lead else 'unknown') + ' and coordinates the discussion. ')
    return ('Cross-project room discussion (' + room['name'] + ', message ' + message_id + ').\n'
            'The owner writes:\n' + text + '\n\n'
            'Participants: ' + people + '.\n' + role +
            'This is discussion only: do not start, plan or change any work because of this message. Work starts only '
            'through an approved room outcome. Answer the owner for this room in one reply and do not quote private '
            'chat history or memory from your project.')


class Discussion:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.file = self.dir / 'discussion.json'
        self.lock = threading.RLock()

    def _load(self):
        if not self.file.exists():
            return []
        try:
            rows = json.loads(self.file.read_text(encoding='utf-8'))['messages']
            if not isinstance(rows, list):
                raise ValueError
            return rows
        except (OSError, ValueError, KeyError, TypeError):
            raise CrossRoomError(503, 'discussion_unavailable', 'The room discussion could not be read')

    def _save(self, rows):
        self.dir.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.dir, prefix='discussion-', suffix='.tmp')
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as out:
            json.dump({'schema': 1, 'messages': rows}, out, ensure_ascii=False, indent=2)
        os.replace(temporary, self.file)

    @staticmethod
    def view(rows):
        return {'messages': [{k: v for k, v in row.items() if not k.startswith('_')} for row in rows]}

    def list(self, room, deps):
        with self.lock:
            rows = self._load()
            if self._observe(room, rows, deps):
                self._save(rows)
            return self.view(rows)

    def post(self, room, text, client_id, recipients, deps):
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_TEXT:
            raise CrossRoomError(400, 'invalid_message', 'Message must be 1 to %d characters' % MAX_TEXT)
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
            raise CrossRoomError(400, 'invalid_client_id', 'Valid client_message_id required')
        people = {p['bot_key']: p for p in room['participants']}
        if recipients is None:
            recipients = list(people)
        if (not isinstance(recipients, list) or not recipients or len(set(recipients)) != len(recipients)
                or any(not isinstance(r, str) or r not in people for r in recipients)):
            raise CrossRoomError(400, 'invalid_recipients', 'Recipients must be participants of this room')
        text = text.strip()
        with self.lock:
            rows = self._load()
            previous = next((r for r in rows if r.get('client_id') == client_id), None)
            if previous:
                if previous['text'] != text or [d['bot_key'] for d in previous['deliveries']] != recipients:
                    raise CrossRoomError(409, 'idempotency_conflict', 'Message ID already belongs to a different message')
                self._observe(room, rows, deps)
                return self.view(rows)
            if len(rows) >= MAX_MESSAGES:
                raise CrossRoomError(409, 'journal_full', 'The room journal is full')
            message_id = uuid.uuid4().hex
            message = {'id': message_id, 'at': now(), 'kind': 'owner', 'author': 'owner', 'text': text,
                       'client_id': client_id, 'deliveries': []}
            rows.append(message)
            for key in recipients:
                message['deliveries'].append(self._deliver(room, message_id, text, people[key], deps))
            self._save(rows)  # saved once; deliveries are idempotent by message and recipient if the save is repeated
            return self.view(rows)

    @staticmethod
    def _deliver(room, message_id, text, person, deps):
        entry = {'bot_key': person['bot_key'], 'name': person['name'], 'project_id': person['project_id'],
                 'state': 'blocked', 'reason': None, 'delivery_id': None}
        try:
            current = deps.manager(person['project_id'])
        except Exception:
            current = None
        if not current:
            entry['reason'] = 'The project Manager is unavailable; nothing was sent'
        elif current['bot_key'] != person['bot_key']:
            entry['reason'] = ('This participant is not the project Manager; only Manager chats can receive room '
                               'messages today, so nothing was sent')
        elif not current.get('thread_id'):
            entry['reason'] = 'No Desktop chat is bound to this Manager; nothing was sent'
        else:
            try:
                receipt = deps.send(person['project_id'], prompt(room, message_id, text, person),
                                    message_id + ':' + person['bot_key'], current['thread_id'])
                entry.update(state='queued', delivery_id=receipt.get('delivery_id'))
            except Exception:
                entry['reason'] = 'Delivery could not be started; nothing was sent'
        return entry

    def _observe(self, room, rows, deps):
        changed = False
        for message in rows:
            for entry in message.get('deliveries', []):
                if entry['state'] in TERMINAL or not entry.get('delivery_id'):
                    continue
                try:
                    raw = deps.delivery_state(entry['project_id'], entry['delivery_id'])
                except Exception:
                    continue  # an unreadable session keeps the last known state
                state = STATES.get(raw)
                if state is None or state == entry['state']:
                    continue
                changed = True
                entry['state'] = state
                if state == 'blocked':
                    entry['reason'] = 'Delivery outcome is unclear; it will not be resent automatically'
                elif state == 'failed':
                    entry['reason'] = 'Delivery failed'
                elif state == 'completed':
                    try:
                        reply = deps.reply_text(entry['project_id'], entry['delivery_id'])
                    except Exception:
                        reply = None
                    if reply:
                        rows.append({'id': uuid.uuid4().hex, 'at': now(), 'kind': 'reply', 'author': entry['bot_key'],
                                     'name': entry['name'], 'project_id': entry['project_id'], 'in_reply_to': message['id'],
                                     'text': reply[:MAX_REPLY]})
                    else:
                        entry['reason'] = 'The turn finished without an answer text'
        return changed

