"""One Desktop-owned personal thread, viewed and messaged through its native host."""
import json
import re
import sqlite3
import threading
import time
import uuid

from .controller import Conflict
from .store import ProcessLock
from .transport import safe_error
from pathlib import Path
from .work import Uncertain
from .images import Images, MARKER as IMAGE_MARKER


MARKER = re.compile(r'\n\n\[Codex Office delivery: [a-f0-9]{32}\]$')
TERMINAL = ('completed', 'failed', 'interrupted', 'cancelled')


def user_text(item):
    if (item.get('type') == 'functionCallOutput' and item.get('name') == 'send_message_to_thread'
            and item.get('namespace') == 'codex_app'):
        output = item.get('output', {})
        text = output.get('text')
        if not output.get('truncated') and isinstance(text,str):
            match = re.fullmatch(r'<codex_delegation>\s*<source_thread_id>[^<]+</source_thread_id>\s*<input>([\s\S]*)</input>\s*</codex_delegation>',text)
            if match: return match[1]
        return ''
    return '\n'.join(c['text'] for c in item.get('content', [])
                     if c.get('type') == 'text' and isinstance(c.get('text'), str))


def project_history(history, bot, img_store=None):
    """Project only conversation text, never reasoning, tool inputs or outputs."""
    rows = []
    for turn in reversed(history.get('turns', [])):
        for item in turn.get('items', []):
            kind = item.get('type')
            if kind == 'userMessage' or (kind == 'functionCallOutput' and user_text(item)):
                text, sender = user_text(item), 'owner'
            elif kind == 'agentMessage' and isinstance(item.get('text'), str):
                text, sender = item['text'], bot
            else:
                continue
            if text:
                delivery_clean = MARKER.sub('', text).strip()
                msg_images = []
                if img_store:
                    msg_images = [{'id': it['id']} for it in img_store.items(delivery_clean)]
                clean_text = IMAGE_MARKER.sub('', delivery_clean).strip()
                row_dict = {'id':item['id'], 'sender':sender, 'text':clean_text,
                             'created':turn.get('startedAt') or time.time(),
                             'turn_id':turn['id'], 'phase':item.get('phase')}
                if msg_images:
                    row_dict['images'] = msg_images
                rows.append(row_dict)
    return rows


class NativePersonal:
    def __init__(self, root, bot, thread_id, bridge, *, start_worker=True):
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.images = Images(root / 'images')
        self.bot, self.thread_id, self.bridge = bot, thread_id, bridge
        self.lock = threading.RLock()
        self.lease = ProcessLock(root / 'native.lock')
        self.db = sqlite3.connect(root / 'native.sqlite', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS binding(id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL, hold INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                client_id TEXT UNIQUE NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL,
                turn_id TEXT, error TEXT NOT NULL DEFAULT '');
        ''')
        existing = self.db.execute('SELECT thread_id FROM binding WHERE id=1').fetchone()
        if existing and existing['thread_id'] != thread_id:
            self.db.close(); self.lease.close()
            raise ValueError('Native personal thread binding changed; preserve and review the original outbox')
        self.db.execute('INSERT OR IGNORE INTO binding(id,thread_id) VALUES(1,?)',(thread_id,))
        self.db.execute("UPDATE outbox SET state='ambiguous' WHERE state='sending'")
        if existing and self.db.execute("SELECT 1 FROM outbox WHERE state NOT IN ('completed','failed','interrupted','cancelled')").fetchone():
            self.db.execute('UPDATE binding SET hold=1 WHERE id=1')
        self.db.commit()
        self.closed = False
        self.draining = False
        self.wake = threading.Event()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        if start_worker: self.worker.start()

    def _history(self):
        result = self.bridge.call('read_thread', {'threadId':self.thread_id,'hostId':'local',
            'turnLimit':10,'includeOutputs':True,'maxOutputCharsPerItem':20000})
        if result.get('thread',{}).get('id') != self.thread_id:
            raise RuntimeError('Desktop thread identity mismatch')
        return result

    @staticmethod
    def busy(history):
        return (history['thread'].get('status',{}).get('type') == 'active'
                or bool(history.get('turns') and history['turns'][0].get('status') == 'inProgress'))

    @staticmethod
    def prompt(row):
        return row['text'] + '\n\n[Codex Office delivery: ' + row['id'] + ']'

    def _reconcile(self, history):
        rows = self.db.execute("SELECT * FROM outbox WHERE state IN ('sending','accepted','ambiguous')").fetchall()
        for row in rows:
            for turn in history.get('turns', []):
                if any(user_text(item) == self.prompt(row)
                       for item in turn.get('items', [])):
                    status = turn.get('status')
                    state = status if status in TERMINAL else 'accepted'
                    self.db.execute('UPDATE outbox SET state=?,turn_id=?,error=? WHERE id=?',
                                    (state,turn['id'],'',row['id']))
                    break
        self.db.commit()

    def state(self):
        history = self._history()
        with self.lock:
            self._reconcile(history)
            hold = bool(self.db.execute('SELECT hold FROM binding WHERE id=1').fetchone()['hold'])
            rows = [dict(r) for r in self.db.execute('SELECT * FROM outbox ORDER BY seq')]
        busy = self.busy(history)
        unclear = any(r['state'] in ('ambiguous','sending') for r in rows)
        return {'bot':self.bot,'channel':'private','backend':'desktop',
            'personal_thread_id':self.thread_id,
            'native_thread_status':history.get('thread',{}).get('status',{}).get('type'),
            'journal':project_history(history,self.bot,self.images),
            'history_has_more':bool(history.get('page',{}).get('hasMore')),
            'deliveries':[{'id':r['id'],'client_id':r['client_id'],'bot':self.bot,
                'state':r['state'],'turn_id':r['turn_id'],'error':r['error']} for r in rows],
            'hold':hold,'active':{'bot':self.bot} if busy else None,
            'status':'held' if hold else 'recovering' if unclear else 'working in Codex' if busy else 'idle',
            'error':'Delivery outcome uncertain; original request will not be resent.' if unclear else '',
            'work':{'enabled':False},'intents':[],'provisional':{},'participants':[{'bot':self.bot}]}

    def send(self, text, bot, client_id, image_keys=None):
        if bot != self.bot or not isinstance(text,str) or (not text.strip() and not image_keys) or len(text)>16000:
            raise ValueError('Enter a personal message for the selected bot (maximum16000 characters)')
        if not isinstance(client_id,str) or not client_id or len(client_id)>128:
            raise ValueError('Valid client_message_id required')
        text = text.strip()
        if image_keys:
            raise ValueError('Native Desktop chat does not support image attachments; remove images to send text')
        with self.lock:
            if self.closed or self.draining: raise Conflict('Personal session is closing')
            old = self.db.execute('SELECT * FROM outbox WHERE client_id=?',(client_id,)).fetchone()
            if old:
                if old['text'] != text: raise Conflict('Message ID already belongs to different text')
                return {'delivery_id':old['id'],'duplicate':True,'state':old['state']}
            delivery = uuid.uuid4().hex
            self.db.execute("INSERT INTO outbox(id,client_id,text,state) VALUES(?,?,?,'pending')",(delivery,client_id,text))
            self.db.commit()
        self.wake.set()
        return {'delivery_id':delivery,'duplicate':False,'state':'pending'}

    def pump(self):
        with self.lock:
            if self.closed or self.draining: return
            unresolved = self.db.execute("SELECT 1 FROM outbox WHERE state NOT IN ('completed','failed','interrupted','cancelled')").fetchone()
            if not unresolved: return
        history = self._history()
        with self.lock:
            if self.closed or self.draining: return
            self._reconcile(history)
            if self.db.execute('SELECT hold FROM binding WHERE id=1').fetchone()['hold']: return
            if self.busy(history): return
            if self.db.execute("SELECT 1 FROM outbox WHERE state IN ('sending','accepted','ambiguous')").fetchone(): return
            row = self.db.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY seq LIMIT 1").fetchone()
            if not row: return
            self.db.execute("UPDATE outbox SET state='sending' WHERE id=?",(row['id'],)); self.db.commit()
            try:
                self.bridge.call('send_message_to_thread', {'threadId':self.thread_id,
                    'hostId':'local','prompt':self.prompt(row)})
                self.db.execute("UPDATE outbox SET state='accepted' WHERE id=?",(row['id'],))
            except Uncertain as error:
                self.db.execute("UPDATE outbox SET state='ambiguous',error=? WHERE id=?",(safe_error(error),row['id']))
            except Exception as error:
                self.db.execute("UPDATE outbox SET state='failed',error=? WHERE id=?",(safe_error(error),row['id']))
            self.db.commit()

    def hold(self):
        with self.lock:
            self.db.execute('UPDATE binding SET hold=1 WHERE id=1'); self.db.commit()
        return self.state()

    def resume(self, *, fresh_conversation=False):
        history = self._history()
        with self.lock:
            self._reconcile(history)
            if self.db.execute("SELECT 1 FROM outbox WHERE state IN ('ambiguous','sending')").fetchone():
                raise Conflict('Original delivery unresolved; no automatic resend')
            self.db.execute('UPDATE binding SET hold=0 WHERE id=1'); self.db.commit()
        self.wake.set()
        return self.state()

    def _worker(self):
        while not self.closed:
            self.wake.wait(2); self.wake.clear()
            try: self.pump()
            except Exception: pass  # Disconnected Desktop: retain the outbox for retrying the read only.

    def close(self):
        with self.lock:
            if self.closed: return
            self.closed = True
        self.wake.set()
        if self.worker.is_alive(): self.worker.join(timeout=40)
        with self.lock:
            self.db.close(); self.lease.close()
