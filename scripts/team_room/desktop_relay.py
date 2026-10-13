"""Keep naturally inherited Desktop context outside the Office host lifecycle."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler

from . import project, work
from .work import DesktopBridge, Uncertain
from .transport import safe_error
from .web import RoomHTTPServer

TOOLS = {'list_threads', 'read_thread', 'wait_threads', 'send_message_to_thread', 'navigate_to_codex_page'}


def targets():
    """Appointed chats, read from the roster on every call so replaced chats apply at once."""
    return {t for t in (work.MANAGER, *work.WORKERS.values(), *project.threads().values()) if t}


def validate(name, arguments):
    if name not in TOOLS or not isinstance(arguments, dict):
        raise ValueError('Unsupported Desktop relay tool')
    if arguments.get('hostId', 'local') != 'local':
        raise ValueError('Only the local Desktop host is linked')
    allowed = targets()
    if name in ('send_message_to_thread', 'read_thread') and arguments.get('threadId') not in allowed:
        raise ValueError('Unappointed Desktop recipient refused')
    if name == 'wait_threads' and any(t.get('threadId') not in allowed or t.get('hostId', 'local') != 'local' for t in arguments.get('targets', [])):
        raise ValueError('Unappointed Desktop recipient refused')


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
                if self.path != '/call': raise ValueError('Unknown relay endpoint')
                name, arguments = data.get('name'), data.get('arguments')
                validate(name, arguments)
                self.respond(200, {'result':bridge.call(name, arguments)})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {'error':'Invalid Desktop relay request'})
            except Exception as error:
                # A failed delivery can have reached Desktop. Never retry it here.
                self.respond(503, {'error':safe_error(str(error))})
    server = RoomHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.bridge_token = token
    descriptor = {'url':f'http://localhost:{server.server_port}', 'token':token,
                  'origin':work.MANAGER, 'pid':os.getpid(), 'bridge_pid':getattr(bridge, 'pid', None)}
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
        url = urlparse(data['url'])
        if data.get('origin') != work.MANAGER or url.scheme != 'http' or url.hostname != 'localhost' or not url.port or url.path or url.query or url.fragment or url.username or url.password:
            raise ValueError('Invalid Desktop relay registration')
        if not isinstance(data.get('token'), str) or len(data['token']) < 32:
            raise ValueError('Invalid Desktop relay registration')
        return data
    def _post(self, path, data):
        descriptor = self._descriptor()
        request = Request(descriptor['url'] + path, data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type':'application/json', 'X-Office-Relay':descriptor['token']})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=40) as response: return json.load(response)
        except (HTTPError, URLError, OSError, ValueError):
            raise Uncertain('Desktop relay unavailable or request outcome unconfirmed; do not resend') from None
    def health(self): return self._post('/health', {})
    def call(self, name, arguments):
        validate(name, arguments)
        return self._post('/call', {'name':name, 'arguments':arguments})['result']
    def close(self): pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-path', type=Path)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--probe', action='store_true', help='Check the registered relay without executor context')
    args = parser.parse_args()
    from .controller import ROOT
    root = args.root.resolve()
    allowed = (ROOT / '.bot-mode/desktop-relay').resolve()
    if root != allowed: parser.error('Relay registration must use the project-owned relay directory')
    try:
        RelayBridge(root).health()
        print('Registered Desktop relay is already available.'); return
    except (OSError, ValueError, Uncertain):
        if args.probe: parser.exit(1, 'Desktop relay is unavailable.\n')
    if not args.codex_path: parser.error('--codex-path is required to register a relay')
    resources = args.codex_path.resolve(strict=True).parent
    bridge = DesktopBridge(resources / 'cua_node/bin/node.exe', resources / 'plugins/openai-bundled/plugins/codex-app-tools/server.mjs')
    try:
        bridge.call('list_threads', {'limit':1})
        server = create_relay(bridge, root)
        print('Desktop relay ready on localhost:24243.', flush=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        stop = root / 'shutdown.flag'; stop.unlink(missing_ok=True)
        while not stop.exists(): threading.Event().wait(.5)
        server.shutdown(); server.server_close(); worker.join()
    finally: bridge.close()


if __name__ == '__main__': main()
