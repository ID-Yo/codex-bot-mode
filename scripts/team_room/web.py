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
from .store import BOTS
from .work import WorkConflict
from .images import Images, MAX_BYTES


STATIC = Path(__file__).parent / 'static'


class RoomHTTPServer(ThreadingHTTPServer):
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


def create_server(controller, port=0, *, ipv6=False, token=None):
    token = token or secrets.token_urlsafe(32)
    from .settings import BotSettings, OfficeSettings
    settings = BotSettings(controller.profile_project)
    office_settings = OfficeSettings(controller.store.root)
    settings_controller = controller

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

        def selected_room(self):
            self.room_id = 'development'
            if not self.path.startswith('/api/rooms/'): return controller
            if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                raise ValueError('Local access token required')
            parts = self.path.split('/',4)
            if len(parts) != 5 or parts[4].split('/')[0].split('?')[0] not in ('state','events','messages','start','stop','resume','images'):
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
            if self.path.startswith('/api/rooms/') and not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                return self.reply(403, {'error':'Local access token required'})
            try: controller = self.selected_room()
            except ValueError as error: return self.reply(404, {'error':str(error)})
            except Exception: return self.reply(503, {'error':'Room could not be opened; no message submitted'})
            url = urlsplit(self.path)
            if url.path == '/api/teams':
                if not secrets.compare_digest(self.headers.get('X-Room-Token',''),token):
                    return self.reply(403, {'error':'Local access token required'})
                from . import launch, project as served
                try:
                    return self.reply(200, {'teams':served.teams(),'templates':launch.catalog()})
                except ValueError as error:
                    return self.reply(400, {'error':str(error)})
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
                    while not controller.closing and time.monotonic() < deadline:
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
            files = {'/': ('index.html','text/html'), '/room.css': ('room.css','text/css'),
                     '/room.js': ('room.js','text/javascript'),
                     '/media.js': ('media.js','text/javascript'),
                     '/speech.js': ('speech.js','text/javascript'),
                     '/settings.js': ('settings.js','text/javascript'),
                     '/teams.js': ('teams.js','text/javascript'),
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
            self.connection.settimeout(5)
            # Reject only after draining a bounded body. Closing a Windows TCP
            # socket with unread input may reset it before the 403 is delivered.
            try:
                length = int(self.headers.get('Content-Length','0'))
            except ValueError:
                length = 0
            upload = self.path.startswith('/api/images/') or bool(re.fullmatch(r'/api/rooms/(?:development|[a-f0-9]{32})/images/room',self.path))
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
                    parts = self.path.split('/')
                    if len(parts) != 4: raise ValueError('Invalid image upload path')
                    if length <= 0 or length > MAX_BYTES:
                        self.close_connection = True
                        raise ValueError('Images must be 1–8 MB')
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
                if self.path.startswith('/api/settings/'):
                    parts = self.path.split('/')
                    if len(parts) != 5: raise ValueError('Unknown settings')
                    if parts[3:] == ['office','general']: result = office_settings.save(data.get('name'),data.get('version'))
                    elif parts[3] == 'bots': result = settings.save(parts[4],data.get('file'),data.get('content'),data.get('version'))
                    elif parts[3] == 'rooms': result = controller.rooms.update(parts[4],data.get('name'),data.get('instructions'),data.get('version'))
                    else: raise ValueError('Unknown settings')
                elif self.path == '/api/rooms':
                    rooms = getattr(controller,'rooms',None)
                    if rooms is None: raise ValueError('Rooms are unavailable')
                    result = rooms.create(data.get('name'),data.get('bots'),data.get('client_message_id'))
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
            except (Conflict, WorkConflict) as error:
                return self.reply(409, {'error':str(error)})
            except (ValueError, UnicodeDecodeError) as error:
                return self.reply(400, {'error':str(error)})
            except OSError:
                return self.reply(409, {'error':'Task baseline/filesystem unavailable; no submission accepted'})

    server = (IPv6RoomHTTPServer(('::1', port), Handler) if ipv6 else
              RoomHTTPServer(('127.0.0.1', port), Handler))
    server.room_token = token
    server.daemon_threads = True
    return server
