"""Lazy, durable private sessions, one isolated controller per roster bot."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading

from .controller import Controller
from .sqlite_uri import readonly_uri
from .store import BOTS


class PersonalSessions:
    def __init__(self, room_controller, codex_path=None, model=None, controller_factory=Controller,
                 native_bridge=None, bots=None):
        self.room_controller = room_controller
        self.bots = tuple(room_controller.bots if bots is None else bots)
        if not self.bots or any(bot not in BOTS for bot in self.bots):
            raise ValueError('Personal sessions require a valid room roster')
        self.root = room_controller.store.root / 'personal'
        self.codex_path = codex_path
        self.model = model or room_controller.model
        self.controller_factory = controller_factory
        self.native_bridge = native_bridge
        self.controllers = {}
        self.lock = threading.RLock()
        self.closed = False
        self.draining = False
        binding_file = self.root / 'desktop-bindings.json'
        self.native_bindings = json.loads(binding_file.read_text(encoding='utf-8')) if binding_file.exists() else {}
        if native_bridge:
            # The roster is the source of truth. A replaced chat gets a fresh native
            # binding folder; the previous folder and its outbox are kept.
            from . import project
            current = project.threads()
            expected = {bot: current[bot] for bot in self.bots if current.get(bot)}
            self.native_bindings = expected
            self.root.mkdir(parents=True, exist_ok=True)
            binding_file.write_text(json.dumps(expected,indent=2),encoding='utf-8')

    def get(self, bot):
        if bot not in self.bots:
            raise ValueError('Unknown agent.')
        with self.lock:
            if self.closed or self.draining:
                raise RuntimeError('Personal sessions are closing; no new work was accepted')
            if self.native_bindings and not self.native_bridge:
                raise RuntimeError('Desktop connection unavailable; this personal chat remains bound to Codex')
            controller = self.controllers.get(bot)
            if controller is None:
                if bot in self.native_bindings:
                    from .native import NativePersonal
                    thread_id = self.native_bindings[bot]
                    controller = NativePersonal(self._native_root(bot, thread_id), bot, thread_id, self.native_bridge)
                    self.controllers[bot] = controller
                    return controller
                runtime = self.root / bot
                runtime.mkdir(parents=True, exist_ok=True)
                controller = self.controller_factory(
                    runtime, self.codex_path, self.model, bots=(bot,),
                    session_kind='private', profile_project=self.room_controller.profile_project)
                self.controllers[bot] = controller
            return controller

    def metadata(self):
        """List per-bot thread bindings without opening controllers or reading chat content."""
        return self._metadata()

    def _native_root(self, bot, thread_id):
        """Original folder while it is bound to thread_id; a per-thread folder after a replacement."""
        original = self.root / bot / 'desktop'
        database = original / 'native.sqlite'
        if not database.exists():
            return original
        with closing(sqlite3.connect(readonly_uri(database), uri=True)) as db:
            row = db.execute('SELECT thread_id FROM binding WHERE id=1').fetchone()
        return original if row is None or row[0] == thread_id else self.root / bot / f'desktop-{thread_id}'

    def _metadata(self):
        """List per-bot thread bindings without opening controllers or reading chat content."""
        rows = []
        with self.lock:
            for bot in self.bots:
                if self.native_bindings and bot in self.native_bindings:
                    thread_id = self.native_bindings[bot]
                    rows.append({'bot':bot,'thread_id':thread_id,'personal_thread_id':thread_id,
                        'kind':'private','backend':'desktop','status':'Codex Desktop' if self.native_bridge else 'Desktop connection unavailable',
                        'active':False,'opened':bot in self.controllers})
                    continue
                root = self.root / bot
                db_path = root / 'room.sqlite'
                thread_id = None
                status = 'not-started'
                active = False
                if db_path.is_file():
                    uri = readonly_uri(db_path)
                    with closing(sqlite3.connect(uri, uri=True)) as db:
                        db.row_factory = sqlite3.Row
                        participants = db.execute('SELECT bot,thread_id FROM participants').fetchall()
                        participant = next((row for row in participants if row['bot'] == bot), None)
                        thread_id = participant['thread_id'] if participant else None
                        room = db.execute('SELECT status FROM room WHERE id=1').fetchone()
                        if room:
                            status = room['status']
                        active = bool(db.execute(
                            "SELECT 1 FROM deliveries WHERE state IN ('sending','accepted','ambiguous') LIMIT 1"
                        ).fetchone())
                rows.append({'bot': bot, 'thread_id': thread_id, 'personal_thread_id': thread_id,
                             'kind': 'private', 'status': status, 'active': active,
                             'opened': bot in self.controllers})
        return rows

    def close(self):
        with self.lock:
            self.closed = True
            controllers = list(self.controllers.values())
            self.controllers.clear()
        first_error = None
        for controller in controllers:
            try:
                controller.close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error
