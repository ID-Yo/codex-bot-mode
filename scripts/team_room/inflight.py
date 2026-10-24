"""Durable record of Office sends to Desktop chats (launch requests, reminders, escalations).

Each send writes an intent row before the message leaves and an outcome row afterwards, in
.bot-mode/team-room-v1/office-sends.sqlite. Shutdown inspection reads it: a send in this process is busy, a send that
an earlier process left unfinished or uncertain is unresolved until the ledger shows it closed or recorded.
"""
from contextlib import closing, contextmanager
import os
import sqlite3
import uuid

from . import project as served
from .sqlite_uri import readonly_uri

# The same value in every import path of the package (scripts.team_room and team_room) inside one process, and a
# different one in every other process, so a crashed service is never mistaken for a live one.
_TOKEN = os.environ.setdefault('CBM_OFFICE_SEND_TOKEN', uuid.uuid4().hex)
BOOT = f'{os.getpid()}:{_TOKEN}'
ROOTS_ENV = 'CBM_OFFICE_SEND_ROOTS'  # project roots with sends in this process, shared by both import paths


def database(root=None):
    base = (root or served.PROJECT)
    return base / '.bot-mode' / 'team-room-v1' / 'office-sends.sqlite'


def _connect(root=None):
    file = database(root)
    file.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(file, timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS sends(id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, '
               'detail TEXT NOT NULL DEFAULT "", state TEXT NOT NULL, boot TEXT NOT NULL)')
    return db


class Send:
    def __init__(self, ident, root):
        self.id, self.root, self.state = ident, root, 'sending'

    def finish(self, state):
        """state: sent (delivered), uncertain (may have reached Desktop), resolved (recorded in the ledger)."""
        self.state = state
        set_state(self.id, state, self.root)

    def attempting(self):
        """Durable marker that the message is about to leave; call right before the bridge call."""
        self.finish('attempted')


def set_state(ident, state, root=None):
    with closing(_connect(root)) as db:
        db.execute('UPDATE sends SET state=? WHERE id=?', (state, ident))
        db.commit()


@contextmanager
def track(kind, ref, detail='', root=None):
    """Intent row before the send. A block that ends after attempting() without an outcome is uncertain; one that ends
    before the message was attempted is unsent (nothing left the process)."""
    ident = uuid.uuid4().hex
    with closing(_connect(root)) as db:
        db.execute('INSERT INTO sends(id, kind, ref, detail, state, boot) VALUES (?,?,?,?,?,?)',
                   (ident, kind, ref, detail, 'sending', BOOT))
        db.commit()
    known = os.environ.get(ROOTS_ENV, '').split(os.pathsep)
    if str(database(root).parents[2]) not in known:
        os.environ[ROOTS_ENV] = os.pathsep.join([*[k for k in known if k], str(database(root).parents[2])])
    send = Send(ident, root)
    try:
        yield send
    finally:
        if send.state in ('sending', 'attempted'):
            try:
                send.finish('uncertain' if send.state == 'attempted' else 'unsent')
            except Exception:
                pass


def count(root=None):
    """Sends of this process that have not finished (read from the durable record, so import paths cannot split it)."""
    return sum(1 for row in rows(root, ('sending', 'attempted')) if row['boot'] == BOOT)


def rows(root=None, states=('sending', 'attempted', 'sent', 'uncertain')):
    file = database(root)
    if not file.exists():
        return []
    marks = ','.join('?' * len(states))
    with closing(sqlite3.connect(readonly_uri(file), uri=True, timeout=1)) as db:
        return [dict(zip(('id', 'kind', 'ref', 'detail', 'state', 'boot'), row)) for row in db.execute(
            f'SELECT id, kind, ref, detail, state, boot FROM sends WHERE state IN ({marks})', states).fetchall()]
