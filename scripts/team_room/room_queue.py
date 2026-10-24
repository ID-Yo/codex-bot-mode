"""Durable room waiting messages; enqueue never dispatches a model or task."""
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid

from .controller import Conflict


class RoomQueue:
    def __init__(self, controller, *, initialize=True):
        self.controller = controller
        self.lock = threading.RLock()
        if not initialize:
            return
        with controller.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS room_waiting (
                id TEXT PRIMARY KEY, client_id TEXT UNIQUE NOT NULL,
                text TEXT NOT NULL, recipient TEXT, state TEXT NOT NULL,
                created REAL NOT NULL, delivery_id TEXT)''')

    def state(self):
        if not self.controller.store.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='room_waiting'"):
            return []
        return self.controller.store.rows('SELECT id,client_id,text,recipient,state,created,delivery_id '
                                          'FROM room_waiting ORDER BY created,id')

    @staticmethod
    def read_existing(path):
        """Read a stable DB/WAL snapshot without opening SQLite on room files."""
        source = Path(path)
        wal = source.with_name(source.name + '-wal')

        def signature(file):
            try:
                stat = file.stat()
                return stat.st_size, stat.st_mtime_ns
            except FileNotFoundError:
                return None

        with tempfile.TemporaryDirectory(prefix='office-queue-') as temporary:
            snapshot = Path(temporary) / source.name
            snapshot_wal = snapshot.with_name(snapshot.name + '-wal')
            for _ in range(3):
                before = signature(source), signature(wal)
                try:
                    shutil.copyfile(source, snapshot)
                    if before[1] is not None:
                        shutil.copyfile(wal, snapshot_wal)
                except OSError as error:
                    snapshot_wal.unlink(missing_ok=True)
                    if not isinstance(error, FileNotFoundError):
                        raise RuntimeError('Room database is unavailable') from None
                    continue
                after = signature(source), signature(wal)
                if before == after:
                    break
                snapshot_wal.unlink(missing_ok=True)
            else:
                raise RuntimeError('Room database changed; retry queue refresh')

            db = sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)
            db.row_factory = sqlite3.Row
            try:
                table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='room_waiting'").fetchone()
                if not table:
                    return []
                return [dict(row) for row in db.execute(
                    'SELECT id,client_id,text,recipient,state,created,delivery_id FROM room_waiting ORDER BY created,id')]
            except sqlite3.DatabaseError:
                raise RuntimeError('Room database is unavailable') from None
            finally:
                db.close()

    @staticmethod
    def _validate_id(queued_id):
        if not isinstance(queued_id, str) or not re.fullmatch(r'[0-9a-f]{32}', queued_id):
            raise ValueError('Valid queue ID required')

    def enqueue(self, text, recipient, client_id):
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError('Queued message must be 1–16000 characters')
        if recipient is not None and recipient not in self.controller.bots:
            raise ValueError('Unknown room recipient')
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
            raise ValueError('Valid client_message_id required')
        text = text.strip()
        with self.lock, self.controller.mutex:
            if self.controller.closing or self.controller.draining:
                raise Conflict('Office is draining; no message queued')
            with self.controller.store.transaction() as db:
                existing = db.execute('SELECT * FROM room_waiting WHERE client_id=?', (client_id,)).fetchone()
                if existing:
                    if existing['text'] != text or existing['recipient'] != recipient:
                        raise Conflict('Queue ID already belongs to different content')
                    return {'id': existing['id'], 'state': existing['state'], 'duplicate': True}
                queued_id = uuid.uuid4().hex
                db.execute('INSERT INTO room_waiting VALUES(?,?,?,?,?,?,NULL)',
                           (queued_id, client_id, text, recipient, 'waiting', time.time()))
            return {'id': queued_id, 'state': 'waiting', 'duplicate': False}

    def release(self, queued_id, *, confirmed=False):
        self._validate_id(queued_id)
        if confirmed is not True:
            raise ValueError('Explicit queue release confirmation required')
        with self.lock, self.controller.mutex:
            rows = self.controller.store.rows('SELECT * FROM room_waiting WHERE id=?', (queued_id,))
            if not rows:
                raise ValueError('Unknown queued message')
            row = rows[0]
            if row['state'] == 'cancelled':
                raise Conflict('Queued message was cancelled')
            delivery_client = 'queue-' + row['id']
            delivered = self.controller.store.client_delivery(delivery_client)
            if delivered:
                result = {'delivery_id': delivered[0]['id'], 'duplicate': True}
            elif row['state'] == 'released':
                raise Conflict('Queue delivery requires reconciliation; no replay')
            else:
                # A running discussion turn owns the room; the waiting message keeps waiting.
                if self.controller.active or any(d['state'] in ('sending', 'accepted')
                                                for d in self.controller.store.deliveries()):
                    raise Conflict('Room is busy; the waiting message was not sent and keeps waiting')
                # send owns validation, authority and its durable idempotency key.
                # All dispatches go through the existing serialized controller.
                result = self.controller.send(row['text'], row['recipient'], delivery_client)
            with self.controller.store.transaction() as db:
                db.execute("UPDATE room_waiting SET state='released',delivery_id=? WHERE id=?",
                           (result['delivery_id'], queued_id))
            return {**result, 'id': queued_id, 'state': 'released'}

    def cancel(self, queued_id):
        self._validate_id(queued_id)
        with self.lock, self.controller.mutex, self.controller.store.transaction() as db:
            row = db.execute('SELECT * FROM room_waiting WHERE id=?', (queued_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown queued message')
            if row['state'] != 'waiting' or self.controller.store.client_delivery('queue-' + queued_id):
                raise Conflict('Only an unsent waiting message can be cancelled')
            db.execute("UPDATE room_waiting SET state='cancelled' WHERE id=?", (queued_id,))
            return {'id': queued_id, 'state': 'cancelled'}
