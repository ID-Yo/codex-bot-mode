"""Versioned, token-protected lifecycle state for the Windows Office controller."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

from scripts.team_room.transport import safe_error


PROTOCOL = 1
TERMINAL = {'completed', 'interrupted', 'failed', 'cancelled', 'review_required'}
MAX_LOG_LINES = 200
MAX_LOG_BYTES = 64 * 1024


class LifecycleBusy(RuntimeError):
    pass


def verify_runtime_slot(install_root, slot, release_id, slot_sha256, component_id, entrypoint):
    """Bind the Office entrypoint to an already verified owned application slot."""
    from scripts import runtime_update
    root=runtime_update._root(install_root)
    try:
        runtime_update.release.safe_path(slot)
        slot_path=runtime_update._safe_path(root,slot,missing=False).resolve(strict=True)
        relative=Path(entrypoint).resolve(strict=True).relative_to(slot_path).as_posix()
        slot_relative=slot_path.relative_to(root).as_posix()
    except (ValueError,OSError) as error:
        raise ValueError('Office entrypoint is outside the verified slot') from error
    try:
        runtime_update.release.safe_path(relative)
        meta,actual=runtime_update._verify_slot(root,slot_relative,slot_sha256)
        for folder,dirs,files in os.walk(slot_path):
            for name in dirs+files:
                child=Path(folder)/name
                child_relative=child.relative_to(slot_path).as_posix()
                runtime_update._safe_path(root,slot_relative+'/'+child_relative,missing=False)
                if (name=='__pycache__' or child.suffix.lower() in ('.pyc','.pyo')
                        or name in files and child_relative not in ('slot.json','.stage.json')
                        and child_relative not in meta['files']):
                    raise ValueError('Office source slot contains an unverified file or bytecode')
        owner=json.loads((root/runtime_update.OWNER_FILE).read_text(encoding='utf-8'))['owner']
        file=runtime_update._safe_path(root,slot_relative+'/'+relative,missing=False)
        spec=meta['files'].get(relative)
        if (meta.get('component')!=component_id or meta.get('kind')!='application'
                or meta.get('owner')!=owner or meta.get('release_id')!=release_id
                or actual!=slot_sha256 or not spec or not file.is_file()
                or runtime_update._hash_file(file)!=spec.get('sha256')):
            raise ValueError('Office slot identity does not match the verified release entrypoint')
        return {'runtime_release_id':release_id,'runtime_slot_sha256':actual,
                'runtime_slot':slot_relative,'runtime_component_id':component_id,
                'runtime_version':meta['version'],'runtime_entrypoint':relative,'runtime_owner':owner}
    except runtime_update.RuntimeUpdateError as error:
        raise ValueError(str(error)) from error


def inspect_shutdown(controller, personal=None, rooms=None, managers=None):
    """Inspect durable and native state. Any unverified owner makes stop unsafe."""
    owned = [controller]
    for holder in (personal, rooms, managers):
        if holder is not None:
            owned.extend(getattr(holder, 'controllers', {}).values())
            owned.extend(getattr(holder, 'sessions', {}).values())

    busy = 0
    pending = 0
    seen = set()
    try:
        for item in owned:
            if id(item) in seen:
                continue
            seen.add(id(item))
            if hasattr(item, '_history'):
                state = item.state()
                if state.get('personal_thread_id') != item.thread_id:
                    raise ValueError('Native thread identity mismatch')
                native_status = state.get('native_thread_status')
                if native_status == 'active':
                    busy += 1
                elif native_status != 'idle':
                    raise ValueError('Native thread state is unknown')
                if state.get('active'):
                    busy += 1
                for delivery in state.get('deliveries', []):
                    status = delivery.get('state')
                    if status in ('sending', 'accepted'):
                        busy += 1
                    elif status == 'ambiguous':
                        raise ValueError('Native delivery outcome is unresolved')
                    elif status not in TERMINAL | {'pending'}:
                        raise ValueError('Native delivery state is unknown')
                    if status == 'pending':
                        pending += 1
                continue

            mutex = getattr(item, 'mutex', None)
            if mutex is None:
                mutex = getattr(item, 'lock', threading.RLock())
            with mutex:
                if getattr(item, 'active', None):
                    busy += 1
                store = getattr(item, 'store', None)
                if store is not None and hasattr(store, 'deliveries'):
                    for delivery in store.deliveries():
                        status = delivery.get('state')
                        if status in ('sending', 'accepted'):
                            busy += 1
                        elif status == 'ambiguous':
                            raise ValueError('Delivery outcome is unresolved')
                        elif status not in TERMINAL | {'pending'}:
                            raise ValueError('Delivery state is unknown')
                        if status == 'pending':
                            pending += 1
                work = getattr(item, 'work', None)
                if work is not None:
                    for task in work.tasks():
                        if task.get('state') in TERMINAL:
                            continue
                        state = task.get('state')
                        phase = task.get('issue_phase')
                        if state not in {'queued', 'creating_issue', 'developer', 'applying', 'verifier', 'ambiguous'} | TERMINAL:
                            raise ValueError('Work task state is unknown')
                        if phase not in {'pending', 'creating', 'created'}:
                            raise ValueError('Work issue state is unknown')
                        if state == 'ambiguous':
                            raise ValueError('Work outcome is unresolved')
                        if phase == 'creating' or state in ('creating_issue', 'applying'):
                            busy += 1
                        else:
                            pending += 1
                        for step in work.steps(task['id']):
                            if step.get('state') in ('sending', 'accepted'):
                                busy += 1
                            elif step.get('state') == 'ambiguous':
                                raise ValueError('Native work outcome is unresolved')
                            elif step.get('state') not in TERMINAL | {'pending'}:
                                raise ValueError('Native work state is unknown')

        # Bindings not opened as Office sessions still belong to native Desktop.
        if personal is not None:
            bindings = getattr(personal, 'native_bindings', {})
            bridge = getattr(personal, 'native_bridge', None)
            opened = {(getattr(session, 'bot', None), getattr(session, 'thread_id', None))
                      for session in getattr(personal, 'controllers', {}).values()}
            for bot, thread_id in bindings.items():
                if (bot, thread_id) in opened:
                    continue
                if bridge is None:
                    raise ValueError('Native Desktop connection is unavailable')
                result = bridge.call('read_thread', {'threadId': thread_id, 'hostId': 'local',
                    'turnLimit': 1, 'includeOutputs': False, 'maxOutputCharsPerItem': 1})
                thread = result.get('thread', {})
                if thread.get('id') != thread_id:
                    raise ValueError('Native thread identity mismatch')
                thread_status = thread.get('status', {}).get('type')
                if thread_status == 'active' or any(
                        turn.get('status') == 'inProgress' for turn in result.get('turns', [])):
                    busy += 1
                elif thread_status != 'idle':
                    raise ValueError('Native thread state is unknown')
                root = personal._native_root(bot, thread_id)
                database = root / 'native.sqlite'
                if database.exists():
                    uri = database.resolve().as_uri() + '?mode=ro'
                    with sqlite3.connect(uri, uri=True, timeout=1) as db:
                        rows = db.execute("SELECT state FROM outbox WHERE state NOT IN "
                            "('completed','failed','interrupted','cancelled')").fetchall()
                    for (status,) in rows:
                        if status in ('sending', 'accepted'):
                            busy += 1
                        elif status == 'ambiguous':
                            raise ValueError('Native delivery outcome is unresolved')
                        elif status == 'pending':
                            pending += 1
                        else:
                            raise ValueError('Native delivery state is unknown')
    except Exception:
        return {'safety': 'unknown', 'busy_count': busy, 'pending_count': pending,
                'reason': 'A service or native Desktop operation could not be verified.'}

    return {'safety': 'busy' if busy else 'safe', 'busy_count': busy,
            'pending_count': pending,
            'reason': 'Waiting for owned work to reach a safe boundary.' if busy else ''}


def set_draining(controller, personal=None, rooms=None, managers=None, value=True):
    """Stop internal schedulers from accepting another queued operation."""
    owned = [controller]
    for holder in (personal, rooms, managers):
        if holder is not None:
            owned.append(holder)
            owned.extend(getattr(holder, 'controllers', {}).values())
            owned.extend(getattr(holder, 'sessions', {}).values())
    seen = set()
    for target in owned:
        if id(target) in seen:
            continue
        seen.add(id(target))
        mutex = getattr(target, 'mutex', getattr(target, 'lock', None))
        if mutex is not None:
            with mutex:
                target.draining = value
        else:
            target.draining = value


class DesktopLifecycle:
    def __init__(self, runtime, *, version, pid=None, instance_id=None, token=None,
                 max_log_lines=MAX_LOG_LINES, runtime_identity=None, parent_pid=None):
        self.runtime = Path(runtime).resolve()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.version = version
        self.pid = os.getpid() if pid is None else pid
        self.parent_pid = os.getppid() if parent_pid is None else parent_pid
        self.instance_id = instance_id or uuid.uuid4().hex
        self.token = token or secrets.token_urlsafe(32)
        if runtime_identity is not None:
            required={'runtime_release_id','runtime_slot_sha256','runtime_slot','runtime_component_id',
                      'runtime_version','runtime_entrypoint','runtime_owner'}
            if not isinstance(runtime_identity,dict) or set(runtime_identity)!=required:
                raise ValueError('Runtime identity must come from verified owned-slot validation')
        self.runtime_identity=dict(runtime_identity) if runtime_identity is not None else None
        self.max_log_lines = min(max(1, max_log_lines), MAX_LOG_LINES)
        self.descriptor_path = self.runtime / 'desktop.json'
        self.log_path = self.runtime / 'office-desktop.log'
        self._condition = threading.Condition()
        self._inflight = 0
        self._drain_generation = 0
        self._draining = False
        self._stop_requested = False
        self._stop_committed = False
        self._stopped = threading.Event()
        self.port = None

    @property
    def draining(self):
        with self._condition:
            return self._draining

    @property
    def stop_requested(self):
        with self._condition:
            return self._stop_requested

    @property
    def stop_committed(self):
        with self._condition:
            return self._stop_committed

    @property
    def stopped(self):
        return self._stopped

    def admit_operation(self):
        with self._condition:
            if self._draining:
                return False
            self._inflight += 1
            return True

    def end_operation(self):
        with self._condition:
            self._inflight = max(0, self._inflight - 1)
            self._condition.notify_all()

    @contextmanager
    def operation(self):
        if not self.admit_operation():
            raise LifecycleBusy('Office is draining; no new operation was accepted.')
        try:
            yield
        finally:
            self.end_operation()

    @contextmanager
    def watch_operation(self):
        """Observational / long-polling requests do not block drain; they exit when draining starts."""
        if self.draining:
            raise LifecycleBusy('Office is draining; observational request closed.')
        yield

    def begin_drain(self, timeout=5):
        with self._condition:
            if self._draining or self._stop_requested or self._stop_committed:
                return False
            self._drain_generation += 1
            generation = self._drain_generation
            self._draining = True
            deadline = time.monotonic() + timeout
            while self._inflight:
                if generation != self._drain_generation or not self._draining:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._draining = False
                    self._drain_generation += 1
                    self._condition.notify_all()
                    return False
                self._condition.wait(remaining)
            return generation if generation == self._drain_generation and self._draining else False

    def cancel_drain(self, generation=None, *, on_cancel=None):
        with self._condition:
            if self._stop_committed or (generation is not None and generation != self._drain_generation):
                return False
            self._drain_generation += 1
            self._draining = False
            self._stop_requested = False
            if on_cancel is not None:
                on_cancel()
            self._condition.notify_all()
            return True

    def request_shutdown(self, generation=None, *, on_request=None):
        with self._condition:
            if (not self._draining or self._inflight or self._stop_committed or
                    (generation is not None and generation != self._drain_generation)):
                return False
            if on_request is not None:
                on_request()
            self._stop_requested = True
            self._condition.notify_all()
            return True

    def shutdown_is_current(self, generation):
        with self._condition:
            return (generation == self._drain_generation and self._draining and
                    self._stop_requested and not self._stop_committed)

    def wait_for_stop(self, generation, timeout):
        with self._condition:
            deadline = time.monotonic() + timeout
            while not self._stopped.is_set():
                if (generation != self._drain_generation or not self._draining or
                        not self._stop_requested):
                    return 'cancelled'
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 'pending'
                self._condition.wait(remaining)
            return 'stopped'

    def commit_stop_if_safe(self, generation=None):
        """Linearize the final safe-stop decision against a concurrent cancel."""
        with self._condition:
            if (not self._draining or not self._stop_requested or self._stop_committed or
                    (generation is not None and generation != self._drain_generation)):
                return False
            self._stop_committed = True
            return True

    def mark_running(self, port):
        self.port = port
        data = {'protocol': PROTOCOL, 'service': 'codex-office', 'version': self.version,
                'pid': self.pid, 'parent_pid':self.parent_pid,'port': port, 'instance_id': self.instance_id,
                'token': self.token, 'started_at': int(time.time()), 'state': 'running'}
        if self.runtime_identity is not None:data.update(self.runtime_identity)
        temporary = self.descriptor_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data), encoding='utf-8')
        if os.name != 'nt':
            temporary.chmod(0o600)
        temporary.replace(self.descriptor_path)
        self.log('Office service started; lifecycle protocol ready.')

    def health(self):
        return {'protocol': PROTOCOL, 'service': 'codex-office', 'version': self.version,
                'pid': self.pid, 'parent_pid':self.parent_pid,'port': self.port, 'instance_id': self.instance_id,
                'state': 'stopping' if self.draining else 'running',
                **(self.runtime_identity or {})}

    def matches(self, value):
        expected = self.health()
        keys=['protocol','service','version','pid','port','instance_id']
        if self.runtime_identity is not None or 'parent_pid' in value:keys.append('parent_pid')
        if self.runtime_identity is not None:
            keys.extend(self.runtime_identity)
        return all(value.get(key) == expected[key] for key in keys)

    def log(self, message):
        line = safe_error(message).replace('\r', ' ').replace('\n', ' ')
        if self.token:
            line = line.replace(self.token, '[REDACTED]')
        line = re.sub(r'(?i)\b(?:token|secret|api[_-]?key)\s*[=:]\s*\S+',
                      '[REDACTED]', line)
        line = f'{int(time.time())} {line[:500]}'
        try:
            lines = self.log_path.read_text(encoding='utf-8').splitlines() if self.log_path.exists() else []
        except OSError:
            lines = []
        lines.append(line)
        encoded = '\n'.join(lines[-self.max_log_lines:]) + '\n'
        while len(encoded.encode('utf-8')) > MAX_LOG_BYTES:
            lines = lines[1:]
            encoded = '\n'.join(lines) + ('\n' if lines else '')
        temporary = self.log_path.with_suffix('.tmp')
        temporary.write_text(encoded, encoding='utf-8')
        temporary.replace(self.log_path)

    def logs(self):
        if not self.log_path.exists():
            return []
        try:
            return self.log_path.read_text(encoding='utf-8')[-MAX_LOG_BYTES:].splitlines()[-self.max_log_lines:]
        except OSError:
            return []

    def mark_stopped(self, clean=True):
        try:
            if self.descriptor_path.exists():
                data = json.loads(self.descriptor_path.read_text(encoding='utf-8'))
                if self.matches(data):
                    data['state'] = 'stopped' if clean else 'unhealthy'
                    temporary = self.descriptor_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(data), encoding='utf-8')
                    temporary.replace(self.descriptor_path)
            self.log('Office service stopped after the drain completed.' if clean
                     else 'Office service cleanup failed; state is unhealthy.')
        finally:
            self._stopped.set()
            with self._condition:
                self._condition.notify_all()
