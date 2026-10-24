"""Persistent, isolated NativePersonal sessions for project Managers (CBM-104).

Resolves project-to-Manager bindings strictly on the server from the explicit local registry.
Preserves separate SQLite outbox, hold state, and thread lease per project Manager.
Reuses existing served PersonalSessions Manager session when available to avoid duplicate workers.
"""
from pathlib import Path
import threading

from .native import NativePersonal
from .registry import registry_path, resolve_project_manager
from .sqlite_uri import readonly_uri


class ProjectManagerSessions:
    def __init__(self, project_root, bridge, personal_sessions=None):
        self.project_root = Path(project_root)
        self.bridge = bridge
        self.personal_sessions = personal_sessions
        self.sessions = {}
        self.lock = threading.RLock()
        self.closed = False
        self.draining = False

    def get(self, project_id, expected_thread=None):
        if not project_id or not isinstance(project_id, str):
            raise ValueError('Valid project_id required')
        with self.lock:
            if self.closed or self.draining:
                raise RuntimeError('Project Manager sessions are closing')

            reg_file = registry_path(self.project_root)
            mgr = resolve_project_manager(reg_file, project_id, current_project_root=self.project_root)
            if not mgr or mgr.get('status') != 'ok':
                err = (mgr.get('error') if mgr else None) or f'Project {project_id} Manager unavailable'
                raise ValueError(err)

            thread_id = mgr.get('thread_id')
            if not thread_id:
                raise ValueError(f'Project {project_id} Manager has no appointed chat thread')

            # Precondition validation: atomic under lock before session acquisition or worker start
            if expected_thread is not None:
                if not isinstance(expected_thread, str) or thread_id != expected_thread:
                    raise ValueError('Manager binding changed since form load; delivery cancelled to prevent mismatched send')

            # Single-writer gate: if this is the served project, verify native binding exists before get
            if mgr.get('is_current') and self.personal_sessions:
                lead_bot = mgr.get('bot') or 'manager'
                if lead_bot in self.personal_sessions.bots:
                    # Precheck native binding to avoid lazy Office-only controller creation
                    bindings = getattr(self.personal_sessions, 'native_bindings', None)
                    if not bindings or lead_bot not in bindings:
                        raise ValueError(f'Served project Manager {lead_bot} has no native Desktop binding; Office restart required')
                    if bindings.get(lead_bot) != thread_id:
                        raise ValueError(f'Served project Manager thread changed to {thread_id}; Office restart required to rebind native personal session')
                    sess = self.personal_sessions.get(lead_bot)
                    if not isinstance(sess, NativePersonal) or sess.thread_id != thread_id:
                        raise ValueError(f'Served project Manager is not a native Desktop session matching thread {thread_id}; Office restart required')
                    return sess, mgr

            if not self.bridge:
                raise RuntimeError('Desktop connection is unavailable; project Manager chat is read-only')

            sess = self.sessions.get(project_id)
            if sess is not None:
                if sess.thread_id != thread_id:
                    sess.close()
                    sess = None
            if sess is None:
                # Per-thread root isolation: if thread was replaced, preserve old folder/outbox
                base_dir = self.project_root / '.bot-mode' / 'team-room-v1' / 'project-managers' / project_id
                db_file = base_dir / 'native.sqlite'
                if db_file.exists():
                    import sqlite3
                    try:
                        con = sqlite3.connect(readonly_uri(db_file), uri=True)
                        row = con.execute('SELECT thread_id FROM binding WHERE id=1').fetchone()
                        con.close()
                        bound_tid = row[0] if row else None
                    except Exception:
                        bound_tid = None
                    root = base_dir if (bound_tid is None or bound_tid == thread_id) else (base_dir / f'thread-{thread_id}')
                else:
                    root = base_dir

                sess = NativePersonal(root, f'manager-{project_id}', thread_id, self.bridge, start_worker=True)
                self.sessions[project_id] = sess
            return sess, mgr

    def native_roots(self):
        """Every durable outbox folder of project Manager chats on disk, including replaced-thread folders."""
        base = self.project_root / '.bot-mode' / 'team-room-v1' / 'project-managers'
        if not base.is_dir():
            return []
        return sorted({file.parent for pattern in ('*/native.sqlite', '*/thread-*/native.sqlite') for file in base.glob(pattern)})


    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
