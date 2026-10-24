"""Loopback-only HTTP/SSE facade. No filesystem or model tools in the public API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import secrets
import socket
import time
from urllib.parse import urlsplit, parse_qs

from .controller import Conflict
from .cross_rooms import CrossRoomError
from .store import BOTS
from .work import WorkConflict
from .images import Images, MAX_BYTES
from scripts.office_desktop import LifecycleBusy, inspect_shutdown, set_draining


STATIC = Path(__file__).parent / 'static'


class RoomHTTPServer(ThreadingHTTPServer):
    def server_close(self):
        super().server_close()
        pm_holder = getattr(getattr(self, 'controller', None), 'project_managers', None)
        if pm_holder:
            try: pm_holder.close()
            except Exception: pass
        worker = getattr(getattr(self, 'controller', None), 'read_model_worker', None)
        if worker:
            try: worker.stop()
            except Exception: pass
    # Windows SO_REUSEADDR permits a competing listener to take the room port.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class IPv6RoomHTTPServer(RoomHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


def create_server(controller, port=0, *, ipv6=False, token=None, desktop_lifecycle=None):
    token = token or secrets.token_urlsafe(32)
    from .settings import BotSettings, OfficeSettings
    settings = BotSettings(controller.profile_project)
    office_settings = OfficeSettings(controller.store.root)
    settings_controller = controller
    if not hasattr(controller, 'project_managers'):
        from .project_manager_chat import ProjectManagerSessions
        personal_sess = getattr(controller, 'personal', None)
        bridge = getattr(personal_sess, 'native_bridge', None)
        from . import project as served
        controller.project_managers = ProjectManagerSessions(served.PROJECT, bridge, personal_sessions=personal_sess)
    if not hasattr(controller, 'read_model_worker'):
        from . import project_status, project as served
        worker = project_status.ReadModelWorker(served.PROJECT, interval_seconds=60)
        worker.start()
        controller.read_model_worker = worker

    def images(scope, owner=controller):
        if scope not in ('room', *BOTS): raise ValueError('Unknown image conversation')
        root = owner.store.root if scope == 'room' else owner.store.root / 'personal' / scope
        return Images(root / 'images')

    def project(state, scope, owner=controller, room_id='development'):
        rows = [images(scope,owner).project(message,scope) for message in state['journal']]
        if scope == 'room' and room_id != 'development':
            for message in rows:
                for image in message.get('images',[]): image['room_id'] = room_id
        return {**state, 'journal': rows}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass  # No owner inputs/tokens in request logs.

        def origin_ok(self):
            allowed = {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}',
                       f'[::1]:{self.server.server_port}'}
            host = self.headers.get('Host')
            return host in allowed and self.headers.get('Origin', 'http://' + host) == 'http://' + host

        def desktop_origin_ok(self):
            origin = self.headers.get('Origin')
            return self.headers.get('Host') in {
                f'localhost:{self.server.server_port}',
                f'127.0.0.1:{self.server.server_port}',
            } and origin in (None, 'http://tauri.localhost', 'http://localhost',
                             f'http://localhost:{self.server.server_port}')

        def desktop_authorized(self):
            return (desktop_lifecycle is not None and self.desktop_origin_ok()
                    and secrets.compare_digest(
                        self.headers.get('X-Office-Controller-Token', ''), desktop_lifecycle.token))

        def selected_room(self):
            self.room_id = 'development'
            if not self.path.startswith('/api/rooms/'): return controller
            if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                raise ValueError('Local access token required')
            parts = self.path.split('/',4)
            if (len(parts) == 5 and re.fullmatch(r'[0-9a-f]{32}', parts[3]) and re.fullmatch(
                    r'participants|discussion|outcomes(?:/[0-9a-f]{32}/(?:approve|cancel)|/[0-9a-f]{32}/assignments/a\d{1,3}/issue)?',
                    parts[4].split('?')[0])):
                self.room_id = parts[3]  # cross-project membership has no session controller; path stays unchanged
                return controller
            if len(parts) != 5 or parts[4].split('/')[0].split('?')[0] not in ('state','events','messages','start','stop','resume','images','queue'):
                raise ValueError('Unknown room action')
            rooms = getattr(controller,'rooms',None)
            if rooms is None: raise ValueError('Rooms are unavailable')
            selected = rooms.get(parts[3])
            self.room_id = parts[3]
            self.path = '/api/' + parts[4]
            return selected

        def reply(self, status, data, content_type='application/json'):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8') if content_type == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path.startswith('/desktop/'):
                if not self.desktop_authorized():
                    return self.reply(403, {'error': 'Controller identity or origin rejected'})
                if url.path == '/desktop/health':
                    return self.reply(200, desktop_lifecycle.health())
                if url.path == '/desktop/status':
                    state = inspect_shutdown(controller, getattr(controller, 'personal', None),
                        getattr(controller, 'rooms', None), getattr(controller, 'project_managers', None))
                    return self.reply(200, {**desktop_lifecycle.health(), **state,
                        'stop_pending': desktop_lifecycle.stop_requested})
                if url.path == '/desktop/logs':
                    return self.reply(200, {'lines': desktop_lifecycle.logs()})
                return self.reply(404, {'error': 'Unknown controller endpoint'})
            if desktop_lifecycle is None:
                return self._do_get_guarded()
            try:
                with desktop_lifecycle.watch_operation():
                    return self._do_get_guarded()
            except LifecycleBusy as error:
                return self.reply(409, {'error': str(error), 'state': 'stopping'})

        def _do_get_guarded(self):
            if self.path.startswith('/api/settings/'):
                if not self.origin_ok() or not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                try:
                    url = urlsplit(self.path); parts = url.path.split('/')
                    if len(parts) != 5: raise ValueError('Unknown settings')
                    if parts[3:] == ['office','general']: return self.reply(200, office_settings.read())
                    if parts[3] == 'bots': return self.reply(200, settings.read(parts[4], parse_qs(url.query).get('file',['SOUL.md'])[0]))
                    if parts[3] == 'rooms': return self.reply(200, settings_controller.rooms.settings(parts[4]))
                    raise ValueError('Unknown settings')
                except (ValueError, OSError) as error: return self.reply(400, {'error':str(error)})
            if not self.origin_ok():
                return self.reply(403, {'error': 'Local same-origin access required'})
            url = urlsplit(self.path)
            if self.path.startswith('/api/rooms/') and not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                return self.reply(403, {'error':'Local access token required'})
            parts = url.path.split('/')
            if (len(parts) == 5 and parts[:3] == ['', 'api', 'rooms'] and parts[4] == 'queue'):
                rooms = getattr(settings_controller, 'rooms', None)
                if rooms is None: return self.reply(404, {'error':'Rooms are unavailable'})
                try:
                    return self.reply(200, {'items':rooms.queue_state(parts[3])})
                except ValueError as error:
                    return self.reply(404, {'error':str(error)})
                except OSError:
                    return self.reply(503, {'error':'Room queue is temporarily unavailable'})
                except RuntimeError:
                    return self.reply(503, {'error':'Room unavailable'})
            if url.path == '/api/rooms/candidates':
                from . import project as served, cross_rooms
                query = parse_qs(url.query)
                try:
                    rows = cross_rooms.search(cross_rooms.candidate_index(served.PROJECT),
                                              query.get('q',[''])[0], query.get('project',[''])[0][:160])
                except CrossRoomError as error:
                    return self.reply(error.status, error.payload)
                except (ValueError, OSError):
                    return self.reply(503, {'code':'registry_unavailable','error':'Agent registry is unavailable'})
                return self.reply(200, {'agents':rows})
            try: controller = self.selected_room()
            except ValueError as error: return self.reply(404, {'error':str(error)})
            except Exception: return self.reply(503, {'error':'Room could not be opened; no message submitted'})
            url = urlsplit(self.path)
            discussion_match = re.fullmatch(r'/api/rooms/([0-9a-f]{32})/discussion', url.path)
            if discussion_match:
                from . import project as served
                from .room_outcomes import Deps
                rooms = getattr(controller, 'rooms', None)
                if rooms is None: return self.reply(404, {'code':'rooms_unavailable','error':'Rooms are unavailable'})
                try:
                    room, store = rooms.cross_discussion(discussion_match[1])
                    return self.reply(200, store.list(room, Deps(served.PROJECT, getattr(controller, 'project_managers', None))))
                except CrossRoomError as error:
                    return self.reply(error.status, error.payload)
                except (RuntimeError, OSError, ValueError):
                    return self.reply(503, {'code':'discussion_unavailable','error':'The room discussion is temporarily unavailable'})
            outcomes_match = re.fullmatch(r'/api/rooms/([0-9a-f]{32})/outcomes', url.path)
            if outcomes_match:
                from . import project as served
                from .room_outcomes import Deps
                rooms = getattr(controller, 'rooms', None)
                if rooms is None: return self.reply(404, {'code':'rooms_unavailable','error':'Rooms are unavailable'})
                try:
                    room, store = rooms.cross_outcomes(outcomes_match[1])
                    return self.reply(200, store.list(room, Deps(served.PROJECT, getattr(controller, 'project_managers', None))))
                except CrossRoomError as error:
                    return self.reply(error.status, error.payload)
                except (RuntimeError, OSError, ValueError):
                    return self.reply(503, {'code':'outcomes_unavailable','error':'Outcomes are temporarily unavailable'})
            if url.path == '/api/updates/status':
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''), token):
                    return self.reply(403, {'error': 'Local access token required'})
                service = getattr(controller, 'updates', None)
                try:
                    update_status = service.status() if service else {
                    'configured': False, 'releases': [], 'history': [],
                    'discovery_freshness': 'not_checked',
                    'reason': 'A trusted release source has not been configured.'}
                    return self.reply(200, update_status)
                except (ValueError, OSError):
                    return self.reply(503, {'error': 'Update evidence unavailable; last confirmed state retained'})
            if url.path == '/api/teams':
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                from . import launch, project as served
                try:
                    return self.reply(200, {'teams':served.teams(),'templates':launch.catalog()})
                except ValueError as error:
                    return self.reply(400, {'error':str(error)})
            if url.path == '/api/registry/agents':
                if not self.origin_ok() or not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                from . import project as served, agent_visibility
                bridge = getattr(getattr(controller, 'project_managers', None), 'bridge', None)
                if not bridge:
                    bridge = getattr(getattr(controller, 'personal', None), 'native_bridge', None)
                if not bridge and controller.work:
                    bridge = controller.work.bridge
                try:
                    return self.reply(200, agent_visibility.build(served.PROJECT, bridge))
                except (ValueError, OSError):
                    return self.reply(200, {'status':'unknown', 'agents':[], 'source_warnings':['Agent evidence unavailable']})
            if url.path == '/api/registry/overview':
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                from . import project as served, registry
                reg_file = registry.registry_path(served.PROJECT)
                return self.reply(200, registry.build_overview(reg_file, current_project_root=served.PROJECT))
            if url.path.startswith('/api/projects/') and url.path.endswith('/chat'):
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                parts = url.path.split('/')
                if len(parts) != 5: return self.reply(400, {'error':'Invalid project chat path'})
                pid = parts[3]
                from . import project as served, registry
                reg_file = registry.registry_path(served.PROJECT)
                mgr = registry.resolve_project_manager(reg_file, pid, current_project_root=served.PROJECT)
                if not mgr or mgr.get('status') in ('not_configured', 'invalid_id'):
                    return self.reply(404, {'error': (mgr.get('error') if mgr else None) or f'Project {pid} not configured in registry'})
                if mgr.get('status') != 'ok':
                    return self.reply(200, {'available': False, 'status': mgr['status'], 'manager': mgr, 'error': mgr.get('error', 'Manager unavailable'), 'journal': [], 'deliveries': []})
                if not mgr.get('thread_id'):
                    return self.reply(200, {'available': False, 'status': 'unappointed', 'manager': mgr, 'error': 'No chat thread appointed for this Manager', 'journal': [], 'deliveries': []})
                pm_holder = getattr(controller, 'project_managers', None)
                if not pm_holder or not pm_holder.bridge:
                    return self.reply(200, {'available': False, 'status': 'bridge_unavailable', 'manager': mgr, 'error': 'Desktop bridge unavailable (read-only)', 'journal': [], 'deliveries': []})
                try:
                    sess, _ = pm_holder.get(pid)
                    st = sess.state()
                    return self.reply(200, {'available': True, 'manager': mgr, **st})
                except Exception as e:
                    return self.reply(503, {'error': f'Could not read Manager chat: {str(e)}'})
            if url.path.startswith('/api/projects/') and '/images/' in url.path:
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                parts = url.path.split('/')
                if len(parts) != 6: return self.reply(400, {'error':'Invalid project image path'})
                pid, image_id = parts[3], parts[5]
                pm_holder = getattr(controller, 'project_managers', None)
                if not pm_holder or not pm_holder.bridge:
                    return self.reply(503, {'error':'Desktop bridge unavailable; chat is read-only'})
                try:
                    sess, _ = pm_holder.get(pid)
                    body, mime = sess.images.read(image_id)
                    return self.reply(200, body, mime)
                except Exception as e:
                    return self.reply(400, {'error':str(e)})
            if url.path.startswith('/api/projects/') and url.path.endswith('/activity'):
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                parts = url.path.split('/')
                if len(parts) != 5: return self.reply(400, {'error':'Invalid project activity path'})
                pid = parts[3]
                from . import project as served, desktop_relay
                bridge = getattr(getattr(controller, 'project_managers', None), 'bridge', None)
                if not bridge:
                    bridge = getattr(getattr(controller, 'personal', None), 'native_bridge', None)
                if not bridge and controller.work:
                    bridge = controller.work.bridge
                if not bridge:
                    return self.reply(200, {
                        'project_id': pid, 'status': 'unknown',
                        'fetched_at': desktop_relay.now_utc(), 'source': 'none',
                        'active_bots': [], 'bots': [], 'error': 'Desktop bridge unavailable (read-only)'
                    })
                try:
                    if hasattr(bridge, 'activity'):
                        res = bridge.activity(pid)
                    else:
                        res = desktop_relay.get_project_activity(bridge, pid, current_project_root=served.PROJECT)
                    return self.reply(200, res)
                except Exception as e:
                    return self.reply(200, {
                        'project_id': pid, 'status': 'unknown',
                        'fetched_at': desktop_relay.now_utc(), 'source': 'error',
                        'active_bots': [], 'bots': [], 'error': 'Metadata unavailable'
                    })
            if url.path == '/api/owner-inbox':
                if not self.origin_ok() or not secrets.compare_digest(self.headers.get('X-Room-Token', ''), token):
                    return self.reply(403, {'error': 'Local access token required'})
                from . import project as served, owner_inbox
                return self.reply(200, owner_inbox.build(served.PROJECT))
            if url.path == '/api/projects':
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                from . import project as served, project_status
                return self.reply(200, project_status.read_model(served.PROJECT))
            if url.path == '/api/rooms':
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                rooms = getattr(controller,'rooms',None)
                return self.reply(200, {'rooms':rooms.metadata() if rooms else [{'id':'development','name':'Development','bots':list(controller.bots)}]})
            if url.path.startswith('/api/images/'):
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                try:
                    parts = url.path.split('/')
                    if len(parts) != 5: raise ValueError('Invalid image path')
                    body, mime = images(parts[3],controller).read(parts[4])
                    return self.reply(200, body, mime)
                except ValueError as error:
                    return self.reply(400, {'error':str(error)})
            if url.path == '/api/bots' or url.path.startswith('/api/private/'):
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                personal = getattr(controller,'personal',None)
                if personal is None:
                    return self.reply(503, {'error':'Personal sessions are unavailable'})
                if url.path == '/api/bots':
                    room_ids = {p['bot']:p['thread_id'] for p in controller.store.participants()}
                    from . import project as served
                    from . import requests as rq
                    roles = {p['bot']: p['role'] for p in served.participants()}
                    pending = rq.summary(rq.load(served.PROJECT))
                    return self.reply(200, {'team':(served.teams() or [{'team':'Development'}])[0]['team'],'lead':BOTS[0],'bots':[
                        {'bot':p['bot'],'personal_thread_id':p.get('thread_id'),
                         'name':served.label(p['bot']),'role':roles.get(p['bot'], p['bot'].title()),
                         'requests':pending.get(p['bot'], {'open':0,'overdue':0}),
                         'backend':p.get('backend','office'),
                         'room_thread_id':room_ids.get(p['bot'])} for p in personal.metadata()]})
                bot = url.path.removeprefix('/api/private/')
                if bot not in BOTS: return self.reply(400, {'error':'Unknown personal bot'})
                try:
                    private = personal.get(bot)
                    state = project(private.state(), bot)
                    return self.reply(200, {**state,'bot':bot,'channel':'private',
                        'personal_thread_id':state.get('personal_thread_id') or private.store.participants()[0]['thread_id']})
                except Exception:
                    return self.reply(503, {'error':'Personal session could not be loaded; no message submitted'})
            if url.path == '/api/sessions':
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''), token):
                    return self.reply(403, {'error': 'Local access token required'})
                personal = getattr(controller,'personal',None)
                if not controller.work and personal is None:
                    return self.reply(503, {'error': 'Desktop sessions require the Manager Desktop connection'})
                from .sessions import list_sessions, office_sessions
                try:
                    local = office_sessions(personal)
                    try:
                        native = list_sessions(controller.work.bridge) if controller.work else {'sessions':[],'unavailable_hosts':False}
                    except Exception:
                        if personal is None: raise
                        native = {'sessions':[],'unavailable_hosts':True}
                    return self.reply(200,{**native,'sessions':local+native['sessions'],
                        'desktop_connected':bool(controller.work)})
                except Exception:
                    return self.reply(503, {'error': 'Codex sessions are unavailable; retry when Desktop is connected'})
            if url.path == '/api/queue':
                if not self.origin_ok() or not secrets.compare_digest(self.headers.get('X-Room-Token', ''), token):
                    return self.reply(403, {'error': 'Local access token required'})
                from .room_queue import RoomQueue
                return self.reply(200, {'items': RoomQueue(controller, initialize=False).state()})
            if url.path == '/api/state':
                return self.reply(200, project(controller.state(), 'room',controller,self.room_id))
            if url.path == '/api/events':
                try:
                    cursor = int(parse_qs(url.query).get('after', [self.headers.get('Last-Event-ID','0')])[0])
                    if cursor < 0: raise ValueError()
                except ValueError:
                    return self.reply(400, {'error': 'Invalid event cursor'})
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    latest = controller.state()['event_cursor']
                    if cursor > latest:
                        self.wfile.write(b'event: reset\ndata: {}\n\n')
                        cursor = 0
                    # Bounded streams reconnect using durable cursor; no dispatch on GET.
                    deadline = time.monotonic() + 20
                    while not controller.closing and not getattr(controller, 'draining', False) and (desktop_lifecycle is None or not desktop_lifecycle.draining) and time.monotonic() < deadline:
                        events = controller.store.events(cursor)
                        for event in events:
                            body = json.dumps(event, ensure_ascii=False)
                            self.wfile.write(f"id: {event['seq']}\nevent: room\ndata: {body}\n\n".encode('utf-8'))
                            cursor = event['seq']
                        if not events:
                            self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                        time.sleep(.2)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                self.close_connection = True
                return
            files = {'/atelier.css': ('atelier.css','text/css'), '/shell.js': ('shell.js','text/javascript'),
                     '/i18n.js': ('i18n.js','text/javascript'), '/': ('index.html','text/html'), '/room.css': ('room.css','text/css'),
                     '/room.js': ('room.js','text/javascript'),
                     '/media.js': ('media.js','text/javascript'),
                     '/speech.js': ('speech.js','text/javascript'),
                     '/settings.js': ('settings.js','text/javascript'),
                     '/teams.js': ('teams.js','text/javascript'),
                     '/projects.js': ('projects.js','text/javascript'),
                     '/projects.css': ('projects.css','text/css'),
                     '/agents.js': ('agents.js','text/javascript'),
                     '/updates.js': ('updates.js','text/javascript'),
                     '/updates.css': ('updates.css','text/css'),
                     '/agents.css': ('agents.css','text/css'),
                     '/newproject.js': ('newproject.js','text/javascript'),
                     '/newproject.css': ('newproject.css','text/css'),
                     '/crossrooms.js': ('crossrooms.js','text/javascript'),
                     '/crossrooms.css': ('crossrooms.css','text/css'),
                     '/outcomes.js': ('outcomes.js','text/javascript'),
                     '/discussion.js': ('discussion.js','text/javascript'),
                     '/discussion.css': ('discussion.css','text/css'),
                     '/favicon.svg': ('favicon.svg','image/svg+xml'),
                     '/polish.css': ('polish.css','text/css'),
                     '/markdown.js': ('markdown.js','text/javascript')}
            if url.path not in files:
                return self.reply(404, {'error': 'Not found'})
            filename, content_type = files[url.path]
            content = (STATIC / filename).read_bytes()
            if filename == 'index.html': content = content.replace(b'__ROOM_TOKEN__', token.encode('ascii'))
            self.reply(200, content, content_type)

        def do_POST(self):
            if urlsplit(self.path).path.startswith('/desktop/'):
                if not self.desktop_authorized():
                    return self.reply(403, {'error': 'Controller identity or origin rejected'})
                if urlsplit(self.path).path == '/desktop/cancel-stop':
                    if not desktop_lifecycle.cancel_drain(on_cancel=lambda: set_draining(
                            controller, getattr(controller, 'personal', None),
                            getattr(controller, 'rooms', None), getattr(controller, 'project_managers', None), False)):
                        return self.reply(409, {**desktop_lifecycle.health(),
                            'stop_pending': True, 'reason': 'Shutdown has reached its safe commit point.'})
                    return self.reply(200, {**desktop_lifecycle.health(), 'stop_pending': False})
                if urlsplit(self.path).path != '/desktop/shutdown':
                    return self.reply(404, {'error': 'Unknown controller endpoint'})
                generation = desktop_lifecycle.begin_drain(timeout=5)
                if generation is None:
                    return self.reply(409, {**desktop_lifecycle.health(), 'safety': 'busy',
                        'stop_pending': desktop_lifecycle.stop_requested,
                        'reason': 'Shutdown could not acquire an uncancelled drain boundary.'})
                if generation is False:
                    if desktop_lifecycle.draining or desktop_lifecycle.stop_requested or desktop_lifecycle.stop_committed:
                        return self.reply(202, {**desktop_lifecycle.health(), 'safety': 'busy',
                            'stop_pending': True,
                            'reason': 'Office shutdown is already in progress.'})
                    return self.reply(409, {**desktop_lifecycle.health(), 'safety': 'busy',
                        'stop_pending': False,
                        'reason': 'Office is busy with in-flight operations and could not begin draining within timeout.'})
                if not desktop_lifecycle.request_shutdown(generation, on_request=lambda: set_draining(
                        controller, getattr(controller, 'personal', None),
                        getattr(controller, 'rooms', None), getattr(controller, 'project_managers', None), True)):
                    return self.reply(409, {**desktop_lifecycle.health(), 'safety': 'safe',
                        'stop_pending': desktop_lifecycle.stop_requested,
                        'reason': 'Shutdown was cancelled before it could be requested.'})
                state = inspect_shutdown(controller, getattr(controller, 'personal', None),
                    getattr(controller, 'rooms', None), getattr(controller, 'project_managers', None))
                if state['safety'] == 'unknown':
                    cancelled = desktop_lifecycle.cancel_drain(generation, on_cancel=lambda: set_draining(
                        controller, getattr(controller, 'personal', None),
                        getattr(controller, 'rooms', None), getattr(controller, 'project_managers', None), False))
                    desktop_lifecycle.log('Shutdown refused: current service state is unknown.')
                    return self.reply(409, {**desktop_lifecycle.health(), **state,
                        'stop_pending': not cancelled and desktop_lifecycle.stop_requested})
                if not desktop_lifecycle.shutdown_is_current(generation):
                    return self.reply(409, {**desktop_lifecycle.health(), **state,
                        'stop_pending': desktop_lifecycle.stop_requested,
                        'reason': 'Shutdown was cancelled before it reached a safe boundary.'})
                if state['safety'] != 'safe':
                    desktop_lifecycle.log('Shutdown pending: ' + state['safety'])
                    return self.reply(202, {**desktop_lifecycle.health(), **state, 'stop_pending': True})
                desktop_lifecycle.log('Safe shutdown requested.')
                stop_result = desktop_lifecycle.wait_for_stop(generation, 60)
                if stop_result == 'stopped':
                    return self.reply(200, {**desktop_lifecycle.health(), 'state': 'stopped',
                        'safety': 'safe', 'pending_count': state['pending_count'], 'stop_pending': False})
                if stop_result == 'cancelled':
                    return self.reply(409, {**desktop_lifecycle.health(), **state,
                        'stop_pending': False, 'reason': 'Shutdown was cancelled before its safe commit point.'})
                return self.reply(202, {**desktop_lifecycle.health(), **state, 'stop_pending': True})
            if desktop_lifecycle is None:
                return self._do_post_guarded()
            try:
                with desktop_lifecycle.operation():
                    return self._do_post_guarded()
            except LifecycleBusy as error:
                return self.reply(409, {'error': str(error), 'state': 'stopping'})

        def _do_post_guarded(self):
            self.connection.settimeout(5)
            # Reject only after draining a bounded body. Closing a Windows TCP
            # socket with unread input may reset it before the 403 is delivered.
            try:
                length = int(self.headers.get('Content-Length','0'))
            except ValueError:
                length = 0
            project_image_upload = bool(re.fullmatch(r'/api/projects/[^/]+/images', self.path))
            upload = self.path.startswith('/api/images/') or bool(re.fullmatch(r'/api/rooms/(?:development|[a-f0-9]{32})/images/room',self.path)) or project_image_upload
            limit = MAX_BYTES if upload else 65536
            raw = self.rfile.read(length) if 0 < length <= limit else b''
            if not self.origin_ok() or not secrets.compare_digest(self.headers.get('X-Room-Token',''), token):
                self.close_connection = True
                return self.reply(403, {'error': 'Local mutation token and same origin required'})
            try:
                try: controller = self.selected_room()
                except ValueError: raise
                except Exception: return self.reply(503, {'error':'Room could not be opened; no message submitted'})
                if upload:
                    if project_image_upload:
                        parts = self.path.split('/')
                        if len(parts) != 5: raise ValueError('Invalid project image upload path')
                        pid = parts[3]
                        if length <= 0 or length > MAX_BYTES:
                            self.close_connection = True
                            raise ValueError('Images must be 1 8 MB')
                        pm_holder = getattr(controller, 'project_managers', None)
                        if not pm_holder or not pm_holder.bridge:
                            return self.reply(503, {'error': 'Desktop bridge unavailable; chat is read-only'})
                        sess, mgr = pm_holder.get(pid)
                        content_type = self.headers.get('Content-Type','').split(';')[0]
                        result = sess.images.save(raw, content_type)
                        return self.reply(200, {'id': result['id']})
                    parts = self.path.split('/')
                    if len(parts) != 4: raise ValueError('Invalid image upload path')
                    if length <= 0 or length > MAX_BYTES:
                        self.close_connection = True
                        raise ValueError('Images must be 1 8 MB')
                    if self.room_id != 'development' and parts[3] != 'room': raise ValueError('Unknown room image channel')
                    result = images(parts[3],controller).save(raw, self.headers.get('Content-Type','').split(';')[0])
                    return self.reply(200, {'id':result['id']})
                if length <= 0 or length > 16384:
                    self.close_connection = True
                    return self.reply(400, {'error': 'JSON body must be 1–16384 bytes'})
                if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    raise ValueError('Content-Type must be application/json')
                data = json.loads(raw.decode('utf-8'))
                if not isinstance(data, dict): raise ValueError('JSON object required')
                if self.path in ('/api/queue', '/api/queue/release', '/api/queue/cancel'):
                    from .room_queue import RoomQueue
                    queue = RoomQueue(controller)
                    if self.path == '/api/queue':
                        result = queue.enqueue(data.get('text'), data.get('recipient'), data.get('client_message_id'))
                    elif self.path.endswith('/release'):
                        result = queue.release(data.get('id'), confirmed=data.get('confirmed', False))
                    else:
                        result = queue.cancel(data.get('id'))
                elif self.path == '/api/projects/pending-team/issue':
                    from . import project as served, project_onboarding
                    result = project_onboarding.link_pending_issue(served.PROJECT, data.get('project_id'), data.get('issue_url'))
                elif self.path in ('/api/projects/prepare', '/api/projects/confirm'):
                    from . import project as served, project_onboarding
                    if self.path.endswith('/prepare'):
                        result = project_onboarding.prepare(served.PROJECT, data.get('configuration'))
                    else:
                        result = project_onboarding.confirm(served.PROJECT, data.get('configuration'),
                            data.get('version'), confirmed=data.get('confirmed', False))
                elif self.path.startswith('/api/updates/'):
                    from .updates import dispatch
                    result = dispatch(getattr(controller, 'updates', None), self.path, data)
                elif self.path.startswith('/api/settings/'):
                    parts = self.path.split('/')
                    if len(parts) != 5: raise ValueError('Unknown settings')
                    if parts[3:] == ['office','general']: result = office_settings.save(data.get('name'),data.get('version'))
                    elif parts[3] == 'bots': result = settings.save(parts[4],data.get('file'),data.get('content'),data.get('version'))
                    elif parts[3] == 'rooms': result = controller.rooms.update(parts[4],data.get('name'),data.get('instructions'),data.get('version'))
                    else: raise ValueError('Unknown settings')
                elif self.path == '/api/rooms':
                    rooms = getattr(controller,'rooms',None)
                    if rooms is None: raise ValueError('Rooms are unavailable')
                    if 'participants' in data:
                        from . import project as served, cross_rooms
                        result = rooms.create_cross(data.get('name'),data.get('participants'),data.get('lead'),
                            data.get('lead_override'),data.get('client_message_id'),
                            lambda: cross_rooms.candidate_index(served.PROJECT))
                    else:
                        result = rooms.create(data.get('name'),data.get('bots'),data.get('client_message_id'))
                elif re.fullmatch(r'/api/rooms/[0-9a-f]{32}/outcomes(?:/.+)?', self.path):
                    from . import project as served
                    from .room_outcomes import Deps
                    rooms = getattr(controller,'rooms',None)
                    if rooms is None: raise ValueError('Rooms are unavailable')
                    segments = self.path.split('/')
                    room, store = rooms.cross_outcomes(self.room_id)
                    deps = Deps(served.PROJECT, getattr(controller,'project_managers',None))
                    if len(segments) == 5:
                        result = store.create(room, data.get('text'), data.get('mode'), data.get('client_message_id'),
                                              data.get('confirmed'), deps)
                    elif len(segments) == 7 and segments[6] == 'approve':
                        result = store.approve(room, segments[5], data.get('version'), data.get('confirmed'), deps)
                    elif len(segments) == 7 and segments[6] == 'cancel':
                        result = store.cancel(room, segments[5], data.get('version'))
                    elif len(segments) == 9 and segments[6] == 'assignments' and segments[8] == 'issue':
                        result = store.link_issue(room, segments[5], segments[7], data.get('version'), data.get('issue_url'), deps)
                    else:
                        raise ValueError('Unknown outcome action')
                elif re.fullmatch(r'/api/rooms/[0-9a-f]{32}/discussion', self.path):
                    from . import project as served
                    from .room_outcomes import Deps
                    rooms = getattr(controller,'rooms',None)
                    if rooms is None: raise ValueError('Rooms are unavailable')
                    room, store = rooms.cross_discussion(self.room_id)
                    result = store.post(room, data.get('text'), data.get('client_message_id'), data.get('recipients'),
                                        Deps(served.PROJECT, getattr(controller,'project_managers',None)))
                elif re.fullmatch(r'/api/rooms/[0-9a-f]{32}/participants', self.path):
                    rooms = getattr(controller,'rooms',None)
                    if rooms is None: raise ValueError('Rooms are unavailable')
                    from . import project as served, cross_rooms
                    result = rooms.set_participants(self.room_id,data.get('version'),data.get('participants'),
                        data.get('lead'),data.get('lead_override'),lambda: cross_rooms.candidate_index(served.PROJECT))
                elif self.path in ('/api/teams/plan','/api/teams/launch'):
                    from . import launch
                    if self.path == '/api/teams/plan':
                        result = launch.plan(data.get('template'),data.get('team_name'),data.get('answers'),data.get('team_answers'))
                    else:
                        bridge = getattr(getattr(controller,'personal',None),'native_bridge',None)
                        result = launch.launch(bridge,data.get('template'),data.get('team_name'),data.get('answers'),data.get('client_message_id'),data.get('team_answers'))
                elif self.path.startswith('/api/private/'):
                    parts = self.path.split('/')
                    if len(parts)!=5 or parts[3] not in BOTS or parts[4] not in ('messages','resume','stop','open'):
                        raise ValueError('Unknown personal bot or action')
                    personal = getattr(controller,'personal',None)
                    if personal is None: raise ValueError('Personal sessions are unavailable')
                    try:
                        private = personal.get(parts[3])
                    except ValueError:
                        raise
                    except Exception:
                        return self.reply(503, {'error':'Personal session could not be loaded; no message submitted'})
                    if parts[4]=='messages':
                        text = data.get('text')
                        if isinstance(text,str) and re.search(r'(?<![\w@])@[\w-]+',text):
                            raise ValueError('Personal messages cannot route to another bot; select its personal chat')
                        text = images(parts[3]).attach(text, data.get('images', []))
                        result = private.send(text,parts[3],data.get('client_message_id'))
                    elif parts[4]=='resume':
                        fresh=data.get('fresh_conversation',False)
                        if not isinstance(fresh,bool): raise ValueError('fresh_conversation must be boolean')
                        result=private.resume(fresh_conversation=fresh)
                    elif parts[4]=='open':
                        if not hasattr(private,'thread_id'): raise ValueError('This is an Office-only conversation')
                        result=private.bridge.call('navigate_to_codex_page',{'threadId':private.thread_id})
                    else: result=private.hold()
                elif self.path.startswith('/api/projects/') and any(self.path.endswith('/' + a) for a in ('messages', 'resume', 'stop')):
                    parts = self.path.split('/')
                    if len(parts) != 5: raise ValueError('Invalid project action path')
                    pid = parts[3]
                    action = parts[4]
                    pm_holder = getattr(controller, 'project_managers', None)
                    if not pm_holder or not pm_holder.bridge:
                        return self.reply(503, {'error': 'Desktop bridge unavailable; chat is read-only'})
                    expected_tid = data.get('expected_manager_thread')
                    sess, mgr = pm_holder.get(pid, expected_thread=expected_tid)
                    if action == 'messages':
                        text = data.get('text', '')
                        attached_images = data.get('images', [])
                        if attached_images:
                            return self.reply(400, {'error': 'Native Desktop chat does not support image content delivery; remove image attachments to send'})
                        if not isinstance(text, str) or not text.strip():
                            raise ValueError('Message text required')
                        result = sess.send(text.strip(), sess.bot, data.get('client_message_id'))
                    elif action == 'resume':
                        fresh = data.get('fresh_conversation', False)
                        if not isinstance(fresh, bool): raise ValueError('fresh_conversation must be boolean')
                        result = sess.resume(fresh_conversation=fresh)
                    elif action == 'stop':
                        result = sess.hold()
                elif self.path == '/api/registry/open':
                    if not controller.work: raise ValueError('Desktop sessions are unavailable')
                    from . import project as served, registry
                    bot_key = data.get('bot_key')
                    if not bot_key or not isinstance(bot_key, str):
                        raise ValueError('Valid bot_key required')
                    reg_file = registry.registry_path(served.PROJECT)
                    thread_id = registry.resolve_bot_thread(reg_file, bot_key, current_project_root=served.PROJECT)
                    if not thread_id:
                        return self.reply(404, {'error': 'Appointed thread unavailable for this bot'})
                    try:
                        result = controller.work.bridge.call('navigate_to_codex_page', {'threadId': thread_id})
                    except Exception:
                        return self.reply(503, {'error': 'Could not confirm opening in Codex; check Desktop before retrying'})
                elif self.path == '/api/sessions/open':
                    if not controller.work: raise ValueError('Desktop sessions are unavailable')
                    from .sessions import open_session
                    try:
                        result = open_session(controller.work.bridge, data.get('thread_id'))
                    except ValueError:
                        raise
                    except Exception:
                        return self.reply(503, {'error': 'Could not confirm opening in Codex; check Desktop before retrying'})
                elif self.path in ('/api/messages','/api/start'):
                    text = images('room',controller).attach(data.get('text'), data.get('images', []))
                    result = controller.send(text, 'developer' if self.path == '/api/start' else data.get('recipient'),
                        data.get('client_message_id'), round_mode=self.path == '/api/start')
                elif self.path == '/api/stop': result = controller.hold()
                elif self.path == '/api/resume':
                    fresh = data.get('fresh_conversation',False)
                    if not isinstance(fresh,bool): raise ValueError('fresh_conversation must be boolean')
                    result = controller.resume(fresh_conversation=fresh)
                elif self.path == '/api/intent/cancel': result = controller.cancel_intent(data.get('intent_id'))
                elif self.path == '/api/intent/reconcile': result = controller.reconcile_intent(data.get('intent_id'))
                elif self.path in ('/api/tasks','/api/work/stop','/api/work/resume','/api/work/reconcile'):
                    if not controller.work: raise ValueError('Work host is unavailable; launch from Manager with --enable-work')
                    result = (controller.work.submit(data) if self.path == '/api/tasks' else
                        getattr(controller.work,{'/api/work/stop':'hold','/api/work/resume':'resume',
                            '/api/work/reconcile':'reconcile'}[self.path])())
                else: return self.reply(404, {'error':'Not found'})
                return self.reply(200, result)
            except CrossRoomError as error:
                return self.reply(error.status, error.payload)
            except (Conflict, WorkConflict) as error:
                return self.reply(409, {'error':str(error)})
            except (ValueError, UnicodeDecodeError) as error:
                return self.reply(400, {'error':str(error)})
            except OSError:
                return self.reply(409, {'error':'Task baseline/filesystem unavailable; no submission accepted'})

        def do_DELETE(self):
            if desktop_lifecycle is None:
                return self._do_delete_guarded()
            try:
                with desktop_lifecycle.operation():
                    return self._do_delete_guarded()
            except LifecycleBusy as error:
                return self.reply(409, {'error': str(error), 'state': 'stopping'})

        def _do_delete_guarded(self):
            if not self.origin_ok() or not secrets.compare_digest(self.headers.get('X-Room-Token',''), token):
                self.close_connection = True
                return self.reply(403, {'error': 'Local mutation token and same origin required'})
            url = urlsplit(self.path)
            if url.path.startswith('/api/projects/') and '/images/' in url.path:
                parts = url.path.split('/')
                if len(parts) != 6: return self.reply(400, {'error': 'Invalid project image path'})
                pid, image_id = parts[3], parts[5]
                try: controller = self.selected_room()
                except Exception: return self.reply(503, {'error': 'Room unavailable'})
                pm_holder = getattr(controller, 'project_managers', None)
                if not pm_holder or not pm_holder.bridge:
                    return self.reply(503, {'error': 'Desktop bridge unavailable; chat is read-only'})
                try:
                    sess, _ = pm_holder.get(pid)
                    deleted = sess.images.delete(image_id)
                    return self.reply(200, {'deleted': deleted})
                except Exception as e:
                    return self.reply(400, {'error': str(e)})
            return self.reply(404, {'error': 'Unknown DELETE path'})

    server = (IPv6RoomHTTPServer(('::1', port), Handler) if ipv6 else
              RoomHTTPServer(('127.0.0.1', port), Handler))
    server.room_token = token
    server.daemon_threads = True
    server.controller = controller
    server.lifecycle = desktop_lifecycle
    return server
