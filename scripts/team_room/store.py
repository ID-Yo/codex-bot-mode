"""Single-owner SQLite room journal. Transactions never span model waits."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


TERMINAL = ('completed', 'interrupted', 'failed', 'cancelled')
# Active participants of the served project; project.configure() replaces the
# contents in place so every importer sees the same list.
BOTS = ['manager', 'developer', 'verifier']
PUBLIC_KEYS = {'message_id', 'delivery_id', 'bot', 'recipient', 'sender', 'text', 'delta',
               'status', 'hold', 'error', 'thread_id', 'turn_id', 'item_id', 'topic', 'turns', 'decision',
               'task_id', 'issue_url', 'path', 'sha256'}


def voluntary_response(text):
    """A completed model turn must explicitly reply or abstain, never imply it."""
    data = json.loads(text)
    if (not isinstance(data, dict) or set(data) != {'decision', 'text'}
            or data['decision'] not in ('reply', 'abstain') or not isinstance(data['text'], str)
            or len(data['text']) > 16000):
        raise ValueError('Invalid voluntary response')
    answer = data['text'].strip()
    if (data['decision'] == 'reply') != bool(answer):
        raise ValueError('Reply needs text; abstention must have empty text')
    return data['decision'], answer


def public_payload(payload):
    def clean(value):
        if isinstance(value, str):
            value = re.sub(r'(?i)(bearer\s+|(?:token|api[_-]?key|secret)\s*[=:]\s*)\S+', r'\1[REDACTED]', value)
            return re.sub(r'\b(?:sk-|eyJ)[A-Za-z0-9_.-]+', '[REDACTED]', value)
        return value
    return {k: clean(v) for k, v in payload.items() if k in PUBLIC_KEYS
            and isinstance(v, (str, int, float, bool, type(None)))}


class ProcessLock:
    def __init__(self, path):
        self.file = open(path, 'a+b')
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another controller owns this runtime') from None

    def close(self):
        if not self.file.closed:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = ProcessLock(self.root / 'controller.lock')
        self.mutex = threading.RLock()
        self.db = sqlite3.connect(self.root / 'room.sqlite', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS room (
                id INTEGER PRIMARY KEY CHECK(id=1), hold INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'idle', topic TEXT NOT NULL DEFAULT '',
                turns INTEGER NOT NULL DEFAULT 0, deadline REAL, error TEXT NOT NULL DEFAULT '');
            INSERT OR IGNORE INTO room(id) VALUES(1);
            CREATE TABLE IF NOT EXISTS participants (
                bot TEXT PRIMARY KEY, thread_id TEXT UNIQUE, cwd TEXT NOT NULL, watermark INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                sender TEXT NOT NULL, recipient TEXT, text TEXT NOT NULL,
                delivery_id TEXT UNIQUE, turn_id TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                message_id TEXT NOT NULL REFERENCES messages(id), bot TEXT NOT NULL,
                client_id TEXT, state TEXT NOT NULL, thread_id TEXT, turn_id TEXT,
                request_id INTEGER, input_text TEXT, baseline TEXT, depends_on TEXT,
                watermark INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                UNIQUE(message_id,bot), UNIQUE(client_id,bot));
            CREATE TABLE IF NOT EXISTS public_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work_tasks (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                client_id TEXT UNIQUE NOT NULL, specification TEXT NOT NULL,
                context TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                hold INTEGER NOT NULL DEFAULT 0, issue_phase TEXT NOT NULL DEFAULT 'pending',
                issue_url TEXT, proposal TEXT, artifact_sha TEXT, verification TEXT,
                error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, deadline REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS work_steps (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES work_tasks(id),
                role TEXT NOT NULL, thread_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                baseline_turn TEXT, cursor TEXT, turn_id TEXT, message_id TEXT,
                prompt TEXT NOT NULL, result TEXT, sent_at REAL,
                UNIQUE(task_id,role));
            CREATE TABLE IF NOT EXISTS message_intents (
                id TEXT PRIMARY KEY REFERENCES messages(id), state TEXT NOT NULL DEFAULT 'pending',
                result TEXT, task_id TEXT UNIQUE REFERENCES work_tasks(id), error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0, deadline REAL);
        ''')
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(deliveries)')}
        for name, definition in (('mode', "TEXT NOT NULL DEFAULT 'direct'"), ('decision', 'TEXT'), ('intent_id','TEXT')):
            if name not in columns:
                self.db.execute(f'ALTER TABLE deliveries ADD COLUMN {name} {definition}')
        intent_columns = {r['name'] for r in self.db.execute('PRAGMA table_info(message_intents)')}
        for name,definition in (('attempts','INTEGER NOT NULL DEFAULT 0'),('deadline','REAL')):
            if name not in intent_columns:self.db.execute(f'ALTER TABLE message_intents ADD COLUMN {name} {definition}')
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.mutex:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def rows(self, sql, params=()):
        with self.mutex:
            return [dict(r) for r in self.db.execute(sql, params)]

    def room(self):
        return self.rows('SELECT * FROM room')[0]

    def deliveries(self):
        return self.rows('SELECT * FROM deliveries ORDER BY seq')

    def participants(self):
        return self.rows('SELECT * FROM participants ORDER BY bot')

    def journal(self):
        return self.rows('SELECT * FROM messages ORDER BY seq')

    def emit(self, db, kind, payload):
        db.execute('INSERT INTO public_events(kind,payload) VALUES(?,?)',
                   (kind, json.dumps(public_payload(payload), ensure_ascii=False)))

    def events(self, after):
        return [{**r, 'payload': json.loads(r['payload'])} for r in self.rows(
            'SELECT * FROM public_events WHERE seq>? ORDER BY seq LIMIT 200', (after,))]

    def client_delivery(self, client_id):
        return self.rows('SELECT * FROM deliveries WHERE client_id=? ORDER BY seq', (client_id,))

    def enqueue(self, db, text, bot, client_id, *, round_mode=False, voluntary=False, intent=False, members=BOTS):
        message_id, delivery_id = uuid.uuid4().hex, uuid.uuid4().hex
        text = public_payload({'text': text})['text']
        target = 'room' if voluntary else 'team' if round_mode else bot
        db.execute('INSERT INTO messages(id,sender,recipient,text,created) VALUES(?,?,?,?,?)',
                   (message_id, 'owner', target, text, time.time()))
        predecessor = None
        intent_id = None
        if intent:
            previous = db.execute("SELECT id FROM message_intents WHERE state='clarification' ORDER BY rowid DESC LIMIT 1").fetchone()
            intent_id = previous['id'] if previous else message_id
            db.execute('INSERT OR IGNORE INTO message_intents(id,deadline) VALUES(?,?)',(intent_id,time.time()+120))
            db.execute("UPDATE message_intents SET state='pending',result=NULL,error='' WHERE id=?",(intent_id,))
        for recipient in (bot,) if intent else members if round_mode or voluntary else (bot,):
            current_id = delivery_id if predecessor is None else uuid.uuid4().hex
            db.execute('INSERT INTO deliveries(id,message_id,bot,client_id,state,depends_on,mode,intent_id) VALUES(?,?,?,?,?,?,?,?)',
                       (current_id, message_id, recipient, client_id, 'pending', predecessor,
                        'intent' if intent else 'voluntary' if voluntary else 'direct',intent_id))
            predecessor = current_id
        self.emit(db, 'message', {'message_id': message_id, 'delivery_id': delivery_id, 'sender': 'owner',
                                'recipient': target, 'text': text})
        return {'message_id': message_id, 'delivery_id': delivery_id}

    def finish(self, db, delivery_id, status, text='', turn_id=None, error=''):
        delivery = dict(db.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone())
        if delivery['state'] in TERMINAL:
            return False
        decision = None
        if status == 'completed' and delivery['mode']=='intent':
            try:
                from .intent import response
                result = response(text)
                decision,text = result['decision'],result['text']
                db.execute("UPDATE message_intents SET state='ready',result=? WHERE id=?",
                    (json.dumps(result,ensure_ascii=False),delivery['intent_id']))
            except (ValueError,TypeError):
                status,text,error = 'failed','','Invalid Manager decision; original turn requires review, no task created'
                db.execute("UPDATE message_intents SET state='failed',error=? WHERE id=?",(error,delivery['intent_id']))
        if status == 'completed' and delivery['mode'] == 'voluntary':
            try:
                decision, text = voluntary_response(text)
            except (ValueError, TypeError):
                status, text, error = 'failed', '', 'Invalid voluntary reply/abstain response; room remains held'
        db.execute('UPDATE deliveries SET state=?,turn_id=COALESCE(?,turn_id),error=?,decision=? WHERE id=?',
                   (status, turn_id, error, decision, delivery_id))
        if status == 'completed' and text:
            text = public_payload({'text': text})['text']
            db.execute('INSERT OR IGNORE INTO messages(id,sender,text,delivery_id,turn_id,created) VALUES(?,?,?,?,?,?)',
                       (uuid.uuid4().hex, delivery['bot'], text, delivery_id, turn_id, time.time()))
        if status == 'completed':
            db.execute('UPDATE participants SET watermark=MAX(watermark,?) WHERE bot=?',
                       (delivery['watermark'], delivery['bot']))
        self.emit(db, 'terminal', {'delivery_id': delivery_id, 'bot': delivery['bot'], 'status': status,
                                  'turn_id': turn_id, 'error': error, 'decision': decision})
        return True

    def close(self):
        with self.mutex:
            self.db.close()
            self.lock.close()
