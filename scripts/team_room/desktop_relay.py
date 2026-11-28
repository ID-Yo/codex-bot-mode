from datetime import datetime, timezone
import copy
import time
"""Keep naturally inherited Desktop context outside the Office host lifecycle."""
import argparse
import atexit
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler

from . import desktop_pipe, project, work
from .work import DesktopBridge, TooLarge, Uncertain
from .transport import safe_error
from .relay_url import relay_base_url
from .web import RoomHTTPServer

TOOLS = {'list_threads', 'read_thread', 'wait_threads', 'send_message_to_thread', 'navigate_to_codex_page'}


REGISTRY_TARGETS_TTL = 60.0
_REGISTRY_TARGETS = {'key': None, 'at': 0.0, 'chats': frozenset(), 'readable': frozenset(), 'sendable': frozenset()}
_REGISTRY_TARGETS_LOCK = threading.Lock()


def invalidate_targets():
    """Drop the cached registry scan; the next targets() call rescans."""
    with _REGISTRY_TARGETS_LOCK:
        _REGISTRY_TARGETS['key'] = None


def _stat(file):
    try:
        info = os.stat(file)
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return None


def _registry_key(reg_file, projects):
    """Cheap fingerprint: the registry file plus the roster and bindings file of every project."""
    from . import roster
    files = [reg_file]
    for p in projects:
        root = p.get('root')
        if isinstance(root, str) and root:
            files.extend((Path(root) / '.bot-mode' / 'roster.json', Path(root) / '.bot-mode' / 'local' / 'bindings.json'))
    return (str(project.PROJECT), tuple((str(f), _stat(f)) for f in files))


def _registry_scan():
    """(Manager chats, every bound chat) of the registry projects.

    The scan builds the registry overview once (about a second of CPU for a dozen projects), so the result is kept
    while the registry, rosters and bindings are unchanged (stat fingerprint), for at most REGISTRY_TARGETS_TTL
    seconds as a safety net. The controller status poll and every relay call share it.
    """
    from . import registry
    reg_file = registry.registry_path(project.PROJECT)
    if not reg_file:
        return frozenset(), frozenset()
    with _REGISTRY_TARGETS_LOCK:
        cached = _REGISTRY_TARGETS
        projects = []
        try:
            raw = json.loads(Path(reg_file).read_text(encoding='utf-8'))
            listed = raw.get('projects') if isinstance(raw, dict) else None
            projects = [p for p in listed if isinstance(p, dict)] if isinstance(listed, list) else []
        except (OSError, ValueError):
            pass
        key = _registry_key(reg_file, projects)
        if cached['key'] == key and time.monotonic() - cached['at'] < REGISTRY_TARGETS_TTL:
            return cached['chats'], cached['readable']
        chats = set()
        reg = registry.load_registry(reg_file)
        overview = registry.build_overview(reg_file, current_project_root=project.PROJECT)
        for p in reg.get('projects', []):
            res = registry.manager_from_overview(overview, p.get('id'))
            if res and res.get('status') == 'ok' and res.get('thread_id'):
                chats.add(res['thread_id'])
        # Every chat bound in a registry project's roster, from the same overview (containment checks included).
        readable = {b['thread_id'] for b in overview.get('bots', []) if isinstance(b.get('thread_id'), str) and b['thread_id']}
        # Chats a report may be sent to: bound in a project roster. Standalone registry entries have no project.
        sendable = {b['thread_id'] for b in overview.get('bots', [])
                    if b.get('project_id') and isinstance(b.get('thread_id'), str) and b['thread_id']}
        _REGISTRY_TARGETS.update(key=key, at=time.monotonic(), chats=frozenset(chats), readable=frozenset(readable | chats),
                                 sendable=frozenset(sendable | chats))
        return _REGISTRY_TARGETS['chats'], _REGISTRY_TARGETS['readable']


def _registry_targets():
    """Appointed Manager chats from the project registry."""
    return _registry_scan()[0]


def targets():
    """Appointed chats: roster and explicit threads on every call, registry Managers from a short-lived scan."""
    chats = {t for t in (work.MANAGER, *work.WORKERS.values(), *project.threads().values()) if t}
    # Under owner CBM-104 authorization, include appointed Managers resolved via registry
    try:
        chats |= _registry_targets()
    except Exception:
        pass
    return chats


READ_ONLY_TURN_LIMIT = 10


def sendable_targets():
    """Chats bound in a registered project's roster, which may receive a report in addition to targets() (#417).

    Standalone registry entries (no project roster) are not included. Waiting and reading keep their own rules."""
    try:
        from . import registry
        if not registry.registry_path(project.PROJECT):
            return frozenset()
        _registry_scan()
        with _REGISTRY_TARGETS_LOCK:
            return _REGISTRY_TARGETS['sendable']
    except Exception:
        return frozenset()


def read_only_targets():
    """Chats that read_thread may read in addition to targets(): every chat bound in a registry project's roster.

    Reading is metadata only (see prepare); sending, waiting and every other tool keep using targets() exactly."""
    try:
        return _registry_scan()[1]
    except Exception:
        return frozenset()


REFUSAL_TEXT = 'Unappointed Desktop recipient refused'


class UnappointedRecipient(ValueError):
    """The chat is not appointed or bound in a registered project. The caller is told so in the HTTP 400 text (#454): the
    other ValueErrors stay the fixed generic answer."""


def validate(name, arguments):
    if name not in TOOLS or not isinstance(arguments, dict):
        raise ValueError('Unsupported Desktop relay tool')
    if arguments.get('hostId', 'local') != 'local':
        raise ValueError('Only the local Desktop host is linked')
    allowed = targets()
    thread = arguments.get('threadId')
    if name == 'read_thread' and thread not in allowed and thread not in read_only_targets():
        raise UnappointedRecipient(REFUSAL_TEXT)
    if name == 'send_message_to_thread' and thread not in allowed and thread not in sendable_targets():
        # Every chat bound in a registry project's roster may receive a report (owner decision, #417): directors and
        # sub-managers are not "the" registry Manager. Unbound threads stay refused; waiting and reading keep their rules.
        raise UnappointedRecipient(REFUSAL_TEXT)
    if name == 'wait_threads' and any(t.get('threadId') not in allowed or t.get('hostId', 'local') != 'local' for t in arguments.get('targets', [])):
        raise UnappointedRecipient(REFUSAL_TEXT)



_ACTIVITY_CACHE = {}
_ACTIVITY_INFLIGHT = {}
FOLLOWER_WAIT_TIMEOUT = 35.0
_ACTIVITY_CACHE_LOCK = threading.Lock()


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def prepare(name, arguments):
    """Arguments actually forwarded to Desktop for a validated call.

    A read_thread of a chat that is only read-only reachable (not in targets()) is reduced to thread status and turn
    metadata: includeOutputs is forced to false, one character per item at most, and only threadId, hostId and a
    bounded turnLimit are kept, so no message text leaves through this path whatever the caller asked for."""
    if name == 'read_thread' and arguments.get('threadId') not in targets():
        limit = arguments.get('turnLimit')
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= READ_ONLY_TURN_LIMIT else 3
        return {'threadId': arguments['threadId'], 'hostId': 'local', 'turnLimit': limit,
                'includeOutputs': False, 'maxOutputCharsPerItem': 1}
    return arguments


def get_project_activity(bridge, project_id, current_project_root=None):
    if not project_id or not isinstance(project_id, str):
        return {
            'project_id': project_id,
            'status': 'unknown',
            'fetched_at': now_utc(),
            'source': 'desktop_relay',
            'active_bots': [],
            'bots': [],
            'error': 'Invalid project_id'
        }

    # Resolve validated registry first so roster/binding changes apply immediately
    from . import registry
    reg_file = registry.registry_path(current_project_root)
    res = registry.resolve_project_bots(reg_file, project_id, current_project_root=current_project_root)
    if res.get('status') != 'ok':
        return {
            'project_id': project_id,
            'status': 'unknown',
            'fetched_at': now_utc(),
            'source': 'desktop_relay',
            'active_bots': [],
            'bots': [],
            'error': 'Project bots could not be resolved from registry'
        }

    bots = res.get('bots', [])
    if not bots:
        return {
            'project_id': project_id,
            'status': 'unknown',
            'fetched_at': now_utc(),
            'source': 'desktop_relay',
            'active_bots': [],
            'bots': [],
            'error': 'No appointed bots resolved'
        }

    # Key cache by project root, project_id, and EVERY appointed bot identity including unbound
    fp = tuple(sorted((b['bot_id'], b.get('thread_id') or '') for b in bots))
    cache_key = (str(current_project_root), project_id, fp)

    with _ACTIVITY_CACHE_LOCK:
        cached = _ACTIVITY_CACHE.get(cache_key)
        if cached and time.monotonic() < cached['expires_at']:
            return copy.deepcopy(cached['data'])
        inflight = _ACTIVITY_INFLIGHT.get(cache_key)
        if inflight is not None:
            evt, res_box = inflight
            wait_needed = True
        else:
            evt = threading.Event()
            res_box = []
            _ACTIVITY_INFLIGHT[cache_key] = (evt, res_box)
            wait_needed = False

    if wait_needed:
        signaled = evt.wait(timeout=FOLLOWER_WAIT_TIMEOUT)
        with _ACTIVITY_CACHE_LOCK:
            if signaled and res_box:
                return copy.deepcopy(res_box[0])
            cached = _ACTIVITY_CACHE.get(cache_key)
            if cached and time.monotonic() < cached['expires_at']:
                return copy.deepcopy(cached['data'])
        # Follower timed out: return explicit timed out unknown result.
        # Must NOT fall through into the fetch body or remove owner's in-flight registration.
        return {
            'project_id': project_id,
            'status': 'unknown',
            'fetched_at': now_utc(),
            'source': 'desktop_relay',
            'active_bots': [],
            'bots': [{'bot': b['bot_id'], 'name': b.get('name') or b.get('role') or b['bot_id'], 'role': b.get('role'), 'status': 'unknown'} for b in bots],
            'partial_failure': True,
            'error': 'Activity metadata fetch in-flight timed out'
        }

    data = None
    try:
        valid_bots = [b for b in bots if b.get('thread_id')]
        if not valid_bots:
            data = {
                'project_id': project_id,
                'status': 'unknown',
                'fetched_at': now_utc(),
                'source': 'desktop_relay',
                'active_bots': [],
                'bots': [{'bot': b['bot_id'], 'name': b.get('name') or b.get('role') or b['bot_id'], 'role': b.get('role'), 'status': 'unknown'} for b in bots],
                'partial_failure': True,
                'error': 'Metadata partially unavailable'
            }
            with _ACTIVITY_CACHE_LOCK:
                _ACTIVITY_CACHE[cache_key] = {'data': copy.deepcopy(data), 'expires_at': time.monotonic() + 10.0}
            return data

        # Query all valid_bots in bounded batches of <= 8
        BATCH_SIZE = 8
        poll_map = {}
        batch_error = False
        source = 'desktop_relay'

        for i in range(0, len(valid_bots), BATCH_SIZE):
            batch = valid_bots[i:i + BATCH_SIZE]
            targets = [{'threadId': b['thread_id'], 'hostId': 'local'} for b in batch]
            try:
                raw_res = bridge.call('wait_threads', {'targets': targets, 'timeoutMs': 0})
                polls = raw_res.get('polls', []) if isinstance(raw_res, dict) else []
                for poll in polls:
                    th = poll.get('thread') or {}
                    tid = th.get('id')
                    host = poll.get('hostId') or th.get('hostId') or 'local'
                    if host != 'local' or not tid:
                        continue
                    st = th.get('status')
                    if isinstance(st, dict):
                        st_type = st.get('type')
                    elif isinstance(st, str):
                        st_type = st
                    else:
                        st_type = None
                    if st_type in ('active', 'idle', 'completed'):
                        poll_map[tid] = st_type
                    else:
                        poll_map[tid] = 'unknown'
            except Exception:
                batch_error = True

        # On wait failure, use bounded list_threads metadata fallback (max limit 50)
        if batch_error:
            try:
                raw_list = bridge.call('list_threads', {'limit': 50})
                all_threads = (raw_list.get('pinnedThreads', []) if isinstance(raw_list, dict) else []) +                               (raw_list.get('threads', []) if isinstance(raw_list, dict) else [])
                list_statuses = {}
                for t in all_threads:
                    tid = t.get('id')
                    host = t.get('hostId') or 'local'
                    st = t.get('status')
                    if host == 'local' and tid and isinstance(st, str):
                        if tid not in list_statuses:
                            list_statuses[tid] = set()
                        list_statuses[tid].add(st)

                # Reconcile status per thread ID without false inactive on conflicting duplicates
                list_map = {}
                for tid, s_set in list_statuses.items():
                    if 'active' in s_set:
                        list_map[tid] = 'active'
                    elif s_set == {'idle'} or s_set == {'completed'} or s_set == {'idle', 'completed'}:
                        list_map[tid] = 'idle'
                    else:
                        # Conflicting or unsupported status (e.g. notLoaded) maps to unknown
                        list_map[tid] = 'unknown'

                source = 'desktop_relay_list_threads'
                fallback_omissions = False

                for b in valid_bots:
                    tid = b['thread_id']
                    st_wait = poll_map.get(tid)
                    st_list = list_map.get(tid)

                    # 1. Any active bot wins (from either wait_threads or list_threads)
                    if st_wait == 'active' or st_list == 'active':
                        poll_map[tid] = 'active'
                    # 2. Preserve clean idle from successful wait_threads batch even if omitted from list_threads
                    elif st_wait == 'idle':
                        poll_map[tid] = 'idle'
                    # 3. If wait_threads failed/unresolved for this target, resolve from list_threads
                    elif st_list == 'idle':
                        poll_map[tid] = 'idle'
                    else:
                        poll_map[tid] = 'unknown'
                        fallback_omissions = True

                batch_error = fallback_omissions
            except Exception:
                batch_error = True

        bot_statuses = []
        has_active = False
        all_idle = True
        partial_failure = batch_error
        active_bot_names = []

        for b in bots:
            tid = b.get('thread_id')
            b_name = b.get('name') or b.get('role') or b.get('bot_id')
            if not tid:
                bot_statuses.append({'bot': b['bot_id'], 'name': b_name, 'role': b.get('role'), 'status': 'unknown'})
                all_idle = False
                partial_failure = True
                continue

            st_type = poll_map.get(tid)
            if st_type == 'active':
                bot_statuses.append({'bot': b['bot_id'], 'name': b_name, 'role': b.get('role'), 'status': 'active'})
                has_active = True
                active_bot_names.append(b_name)
            elif st_type in ('idle', 'completed'):
                bot_statuses.append({'bot': b['bot_id'], 'name': b_name, 'role': b.get('role'), 'status': 'idle'})
            else:
                bot_statuses.append({'bot': b['bot_id'], 'name': b_name, 'role': b.get('role'), 'status': 'unknown'})
                all_idle = False
                partial_failure = True

        if len(valid_bots) < len(bots):
            partial_failure = True
            all_idle = False

        if has_active:
            agg_status = 'active'
        elif all_idle and not partial_failure and len(valid_bots) > 0:
            agg_status = 'inactive'
        else:
            agg_status = 'unknown'

        err_msg = 'Metadata partially unavailable' if (partial_failure and not has_active) else None
        post_fetch_mono = time.monotonic()

        data = {
            'project_id': project_id,
            'status': agg_status,
            'fetched_at': now_utc(),
            'source': source,
            'active_bots': active_bot_names,
            'bots': bot_statuses,
            'partial_failure': partial_failure
        }
        if err_msg:
            data['error'] = err_msg

        with _ACTIVITY_CACHE_LOCK:
            _ACTIVITY_CACHE[cache_key] = {'data': copy.deepcopy(data), 'expires_at': post_fetch_mono + 10.0}

        return data
    finally:
        with _ACTIVITY_CACHE_LOCK:
            cur_inflight = _ACTIVITY_INFLIGHT.get(cache_key)
            if cur_inflight is not None and cur_inflight[0] is evt:
                _ACTIVITY_INFLIGHT.pop(cache_key, None)
            if data is not None:
                res_box.append(data)
            evt.set()


def create_relay(bridge, root, port=24243):
    if bridge.origin != work.MANAGER:
        raise ValueError('Only the appointed Manager can register the Desktop relay')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            if self.headers.get('Origin') or not hmac.compare_digest(self.headers.get('X-Office-Relay', ''), token):
                self.respond(403, {'error':'Desktop relay authorization required'}); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1048576: raise ValueError('Invalid relay request size')
                data = json.loads(self.rfile.read(length))
                if self.path == '/health':
                    bridge.call('list_threads', {'limit':1})
                    self.respond(200, {'origin':work.MANAGER}); return
                if self.path == '/activity':
                    pid = data.get('project_id')
                    if not pid or not isinstance(pid, str):
                        raise ValueError('Valid project_id required')
                    from . import project as served
                    res = get_project_activity(bridge, pid, current_project_root=served.PROJECT)
                    self.respond(200, res); return
                if self.path != '/call': raise ValueError('Unknown relay endpoint')
                name, arguments = data.get('name'), data.get('arguments')
                validate(name, arguments)
                self.respond(200, {'result':bridge.call(name, prepare(name, arguments))})
            except UnappointedRecipient:
                self.respond(400, {'error':REFUSAL_TEXT})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {'error':'Invalid Desktop relay request'})
            except Exception as error:
                # A failed delivery can have reached Desktop. Never retry it here.
                self.respond(503, {'error':safe_error(str(error))})
    server = RoomHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.bridge_token = token
    descriptor = {'url':f'http://127.0.0.1:{server.server_port}', 'token':token,
                  'origin':work.MANAGER, 'pid':os.getpid(), 'bridge_pid':getattr(bridge, 'pid', None),
                  'standalone':bool(getattr(bridge, 'standalone', False))}
    path = root / 'relay.json'
    temporary = root / 'relay.tmp'
    temporary.write_text(json.dumps(descriptor), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)
    return server


class RelayBridge(DesktopBridge):
    """Authenticated client; closing Office leaves the registered relay running."""
    def __init__(self, root):
        self.root = Path(root)
        self.origin = work.MANAGER
        self.pid = None
        self._descriptor()
    def _descriptor(self):
        data = json.loads((self.root / 'relay.json').read_text(encoding='utf-8'))
        relay_base_url(data['url'])
        if data.get('origin') != work.MANAGER:
            raise ValueError('Invalid Desktop relay registration')
        if not isinstance(data.get('token'), str) or len(data['token']) < 32:
            raise ValueError('Invalid Desktop relay registration')
        return data
    def _post(self, path, data):
        descriptor = self._descriptor()
        request = Request(relay_base_url(descriptor['url']) + path, data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type':'application/json', 'X-Office-Relay':descriptor['token']})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=40) as response: return json.load(response)
        except HTTPError as error:
            if b'size limit' in error.read(2000):
                raise TooLarge('Desktop answer exceeds the size limit') from None
            raise Uncertain('Desktop relay unavailable or request outcome unconfirmed; do not resend') from None
        except (URLError, OSError, ValueError):
            raise Uncertain('Desktop relay unavailable or request outcome unconfirmed; do not resend') from None
    def health(self): return self._post('/health', {})
    def activity(self, project_id):
        return self._post('/activity', {'project_id': project_id})
    def call(self, name, arguments):
        validate(name, arguments)
        return self._post('/call', {'name':name, 'arguments':arguments})['result']
    def close(self): pass


RELAY_REMEDY = ("restart the Desktop relay from the appointed Manager chat: run "
                "'powershell -File scripts/start_codex_office.ps1 -RelayOnly' (it replaces an unhealthy relay and works while "
                "Office is running); the supervisor task also restarts it without the Manager chat "
                "(python -B scripts/relay_watchdog.py --restart); see docs/operations/team-room.md, 'Restart the Desktop relay'")
RECHECKS = 3
RECHECK_GAP = 5.0


def sustained_failure(check, checks=RECHECKS, gap=RECHECK_GAP, sleep=time.sleep):
    """True only when every one of several checks, spread over about (checks - 1) * gap seconds, fails.

    A single failed health check can be a transient error (a busy relay, a slow Desktop answer); only a sustained
    failure justifies stopping a live relay."""
    for number in range(checks):
        if number: sleep(gap)
        try:
            check(); return False
        except (OSError, ValueError, Uncertain):
            continue
    return True
WATCHDOG_SECONDS = 5.0
GIVE_UP_SECONDS = 120.0  # an outage this long is announced as degraded; the relay stays registered and keeps retrying
RETRY_CAP_SECONDS = 60.0  # retries back off up to this gap so a struggling Desktop is not hit with a process storm
ABANDON_SECONDS = 6 * 3600.0  # only an outage this long removes the registration, so a dead Desktop leaves no relay behind


def watch(bridge, stop, say=print, interval=WATCHDOG_SECONDS, give_up=GIVE_UP_SECONDS, clock=time.monotonic,
          retry_cap=RETRY_CAP_SECONDS, abandon=ABANDON_SECONDS):
    """Keep the relay answering while Desktop restarts or stalls.

    The MCP server process behind the relay dies or loses its pipe when Codex Desktop's app-server restarts, and Desktop
    can also stop answering it for a while without any restart (#315). Every interval the bridge is probed; a dead or
    unanswering bridge is restarted in place, retrying with a growing gap up to retry_cap. After give_up seconds the
    outage is announced once and the relay keeps its registration, answering 503 until Desktop accepts it again, so a
    stall heals without a manual restart. Only after abandon seconds does it stop, so nothing keeps a registration that
    cannot work. Returns True when stopped by the flag and False when it gave up."""
    down_since, wait, announced = None, interval, False
    while not stop():
        remaining = wait
        while True:  # sleep in slices of one interval so the shutdown flag is honoured during a long retry gap
            nap = min(interval, remaining); time.sleep(nap); remaining -= nap
            if remaining <= 0: break
            if stop(): return True
        if stop(): return True
        try:
            if not bridge.alive(): raise Uncertain('Desktop bridge process exited (%s)' % bridge.diagnosis())
            bridge.call('list_threads', {'limit': 1})
            if down_since is not None: say('Desktop relay recovered after %d s.' % (clock() - down_since), flush=True)
            down_since, wait, announced = None, interval, False
            continue
        except Exception as error:
            down_since = down_since if down_since is not None else clock()
            say('Desktop bridge not answering (%s); restarting it.' % safe_error(str(error))[:300], flush=True)
        try:
            bridge.reconnect(); bridge.call('list_threads', {'limit': 1})
            say('Desktop relay recovered after %d s.' % (clock() - down_since), flush=True)
            down_since, wait, announced = None, interval, False
            continue
        except Exception as error:
            say('Desktop bridge restart failed (%s).' % safe_error(str(error))[:300], flush=True)
        outage = clock() - down_since
        if outage >= abandon:
            say('Desktop relay stopping: Desktop did not accept it again for %d s. Remedy: %s' % (outage, RELAY_REMEDY), flush=True)
            return False
        if outage >= give_up and not announced:
            announced = True
            say('Desktop relay degraded: Desktop has not answered for %d s. The relay stays registered, answers 503 and keeps '
                'retrying every %d s at most. Remedy if it does not recover: %s' % (outage, retry_cap, RELAY_REMEDY), flush=True)
        wait = min(max(wait * 2, interval), retry_cap)
    return True


def replace_unhealthy(root, say=print, wait=15.0):
    """Stop a registered relay that is alive but no longer answers, so a new one can be registered."""
    try: pid = json.loads((root / 'relay.json').read_text(encoding='utf-8')).get('pid')
    except (OSError, ValueError): pid = None
    flag = root / 'shutdown.flag'
    if isinstance(pid, int) and pid != os.getpid() and _pid_alive(pid):
        say('Stopping the unhealthy relay process %d.' % pid, flush=True)
        flag.write_text('shutdown', encoding='utf-8')
        deadline = time.monotonic() + wait
        while _pid_alive(pid) and time.monotonic() < deadline: time.sleep(.3)
        if _pid_alive(pid): raise SystemExit('The unhealthy relay process %d did not stop; end it manually, then retry.' % pid)
    flag.unlink(missing_ok=True)
    (root / 'relay.json').unlink(missing_ok=True)


def trace_lifecycle(say=print):
    """Write a start line now and a stop line on any exit Python can see, so the next log shows whether the relay ended
    cleanly, by a signal, or was killed without a trace (a start line with no stop line: the whole process tree was ended,
    for example with the Desktop app-server, #388). The previous log is kept by the starter, not overwritten."""
    def stamp(event):
        return '%s Desktop relay %s (pid %d, parent %s).' % (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), event, os.getpid(), os.getppid())
    say(stamp('started'), flush=True)
    atexit.register(lambda: say(stamp('exited'), flush=True))
    def on_signal(number, frame):
        say(stamp('received signal %d' % number), flush=True)
        raise SystemExit(128 + number)
    for name in ('SIGTERM', 'SIGBREAK'):
        number = getattr(signal, name, None)
        if number is not None:
            try: signal.signal(number, on_signal)
            except (ValueError, OSError): pass


def _pid_alive(pid):
    if os.name == 'nt':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle: return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)); ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259
    try: os.kill(pid, 0)
    except OSError: return False
    return True


def start_diagnostics(root, bridge, say=print, interval=60.0, log_every=900.0, clock=time.time):
    """Leave evidence for the next relay death (#401). A process ended with TerminateProcess leaves no stop line, so:
    relay-alive.json is rewritten every interval (last proof of life, parent and bridge state), a log line is written when
    the parent or the bridge changes state and every log_every seconds, fatal errors and Windows console events are
    logged, and faulthandler writes native crashes to stderr. The watchdog reads relay-alive.json when the relay is down.
    Returns a function that stops the writer thread and waits for it (the relay itself never calls it; tests do)."""
    import faulthandler, sys
    try: faulthandler.enable()
    except (RuntimeError, ValueError, OSError): pass
    def log_exception(kind, value, trace):
        say('%s Desktop relay fatal %s: %s' % (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), kind, safe_error(repr(value))[:300]), flush=True)
    previous_hook = sys.excepthook
    def excepthook(kind, value, trace):
        log_exception(kind.__name__, value, trace); previous_hook(kind, value, trace)
    sys.excepthook = excepthook
    threading.excepthook = lambda args: log_exception('thread ' + (args.thread.name if args.thread else '?'), args.exc_value, args.exc_traceback)
    if os.name == 'nt':
        try:
            import ctypes
            handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
            names = {0: 'CTRL_C', 1: 'CTRL_BREAK', 2: 'CLOSE', 5: 'LOGOFF', 6: 'SHUTDOWN'}
            def on_console(event):
                say('%s Desktop relay console event %s' % (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), names.get(event, event)), flush=True)
                return 0
            diagnostics_handlers.append(handler_type(on_console))
            ctypes.windll.kernel32.SetConsoleCtrlHandler(diagnostics_handlers[-1], True)
        except (OSError, AttributeError): pass
    started, parent = clock(), os.getppid()
    def snapshot():
        bridge_pid = getattr(bridge, 'pid', None)
        return {'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'pid': os.getpid(), 'parent_pid': parent,
                'parent_alive': _pid_alive(parent), 'bridge_pid': bridge_pid,
                'bridge_alive': _pid_alive(bridge_pid) if isinstance(bridge_pid, int) else None,
                'standalone': bool(getattr(bridge, 'standalone', False)),
                'pipe': _fingerprint(getattr(bridge, 'pipe', None)),
                'uptime_seconds': int(clock() - started)}
    stopping = threading.Event()
    def loop():
        last_state, last_log = None, 0.0
        while not stopping.is_set():
            try:
                data = snapshot()
                temporary = root / 'relay-alive.json.tmp'
                temporary.write_text(json.dumps(data), encoding='utf-8'); temporary.replace(root / 'relay-alive.json')
                state = (data['parent_alive'], data['bridge_alive'])
                if state != last_state or clock() - last_log >= log_every:
                    say('%s Desktop relay alive: parent %s %s, bridge %s %s, up %d s' % (
                        data['at'], data['parent_pid'], 'alive' if data['parent_alive'] else 'GONE', data['bridge_pid'],
                        {True: 'alive', False: 'GONE', None: 'unknown'}[data['bridge_alive']], data['uptime_seconds']), flush=True)
                    last_state, last_log = state, clock()
            except OSError: pass
            stopping.wait(interval)
    writer = threading.Thread(target=loop, daemon=True, name='relay-diagnostics')
    writer.start()
    def stop():
        stopping.set()
        writer.join()
    return stop


diagnostics_handlers = []


def open_bridge(codex_path, standalone, parser):
    """Start the MCP bridge. A standalone start that cannot reach Desktop (closed, pipe refused, tool error) ends with an
    actionable message and exit code 2 so the supervisor retries; the Manager-chat start keeps raising as before."""
    try:
        codex = codex_path or desktop_pipe.find_codex_binary()
        resources = Path(codex).resolve(strict=True).parent
        return DesktopBridge(resources / 'cua_node/bin/node.exe', resources / 'plugins/openai-bundled/plugins/codex-app-tools/server.mjs',
                             standalone=standalone)
    except Exception as error:
        if not standalone: raise
        parser.exit(2, 'Desktop relay cannot start yet (%s): %s\n' % (type(error).__name__, safe_error(str(error))[:300]))


def _fingerprint(pipe):
    """Short hash of the Desktop pipe name: shows in the log when it changed, without printing the name."""
    return hashlib.sha256(pipe.encode('utf-8')).hexdigest()[:10] if pipe else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-path', type=Path)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--probe', action='store_true', help='Check the registered relay without executor context')
    parser.add_argument('--standalone', action='store_true',
                        help='Start without the Manager chat: Manager from the roster, Desktop pipe found by name, never gives up')
    args = parser.parse_args()
    from .controller import ROOT
    root = args.root.resolve()
    allowed = (ROOT / '.bot-mode/desktop-relay').resolve()
    if root != allowed: parser.error('Relay registration must use the project-owned relay directory')
    try:
        RelayBridge(root).health()
        print('Registered Desktop relay is already available.'); return
    except (OSError, ValueError, Uncertain):
        if args.probe: parser.exit(1, 'Desktop relay is unavailable. Remedy: ' + RELAY_REMEDY + '\n')
        if not sustained_failure(lambda: RelayBridge(root).health(), RECHECKS, RECHECK_GAP):
            print('Registered Desktop relay answered again; keeping it.'); return
    if not args.codex_path and not args.standalone: parser.error('--codex-path is required to register a relay')
    replace_unhealthy(root)
    bridge = open_bridge(args.codex_path, args.standalone, parser)
    try:
        bridge.call('list_threads', {'limit':1})
        server = create_relay(bridge, root)
        trace_lifecycle()
        start_diagnostics(root, bridge)
        print('Desktop relay ready on 127.0.0.1:24243.', flush=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        stop = root / 'shutdown.flag'; stop.unlink(missing_ok=True)
        finished = watch(bridge, stop.exists, abandon=float('inf') if args.standalone else ABANDON_SECONDS)
        server.shutdown(); server.server_close(); worker.join()
        if not finished:
            (root / 'relay.json').unlink(missing_ok=True)
            raise SystemExit(3)
    finally: bridge.close()


if __name__ == '__main__': main()
