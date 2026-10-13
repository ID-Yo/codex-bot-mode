"""Launch one real localhost room. No fake backend option is exposed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from . import project as served
from .controller import Controller, ROOT
from .transport import safe_error
from .web import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-path', type=Path)
    parser.add_argument('--port', type=int, default=24242, help='Loopback port (default: 24242; 0 selects a test port)')
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--runtime-root', type=Path)
    parser.add_argument('--enable-work',action='store_true',help='Use naturally inherited Manager Desktop MCP context for Run task')
    parser.add_argument('--desktop-relay',action='store_true',help='Reconnect to the locally registered Desktop relay without executor environment')
    parser.add_argument('--shutdown', type=Path, help='Signal graceful shutdown of an owned runtime')
    parser.add_argument('--project', type=Path, help='Project to serve (default: this repository); participants come from its roster')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    project_root = served.configure(args.project or ROOT)
    allowed = (project_root / '.bot-mode/team-room-v1').resolve()
    if args.shutdown:
        runtime = args.shutdown.resolve(strict=True)
        if runtime == allowed or not runtime.is_relative_to(allowed):
            parser.error('Shutdown target must be an owned runtime under .bot-mode/team-room-v1')
        (runtime / 'shutdown.flag').write_text('shutdown', encoding='utf-8')
        return
    if not args.codex_path: parser.error('--codex-path is required')
    runtime = (args.runtime_root or allowed / ('room-' + uuid.uuid4().hex[:8])).resolve()
    if runtime == allowed or not runtime.is_relative_to(allowed):
        parser.error('Runtime must be a child of .bot-mode/team-room-v1')
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / 'shutdown.flag').unlink(missing_ok=True)
    binary = args.codex_path.resolve(strict=True)
    version = subprocess.run([str(binary),'--version'],capture_output=True,text=True,check=True,timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).stdout.strip()
    bridge = None
    if args.desktop_relay:
        from .desktop_relay import RelayBridge
        bridge = RelayBridge(project_root / '.bot-mode/desktop-relay')
    elif args.enable_work:
        from .work import DesktopBridge
        resources = binary.parent
        bridge = DesktopBridge(resources/'cua_node/bin/node.exe',
            resources/'plugins/openai-bundled/plugins/codex-app-tools/server.mjs')
    try:
        rooms = served.teams()
        lead_room = rooms[0]['bots'] if rooms else None
        controller = Controller(runtime,binary,args.model,bots=lead_room,profile_project=ROOT,
                                **({'work_bridge':bridge} if bridge else {}))
    except Exception:
        if bridge: bridge.close()
        raise
    from .personal import PersonalSessions
    from .rooms import Rooms
    try:
        from .store import BOTS
        controller.personal = PersonalSessions(controller,binary,args.model,bots=tuple(BOTS),
                                               **({'native_bridge':bridge} if bridge else {}))
        controller.rooms = Rooms(controller,binary)
    except Exception:
        controller.close()
        raise
    try:
        server = create_server(controller,args.port)
        try:
            ipv6_server = create_server(controller,server.server_port,ipv6=True,token=server.room_token)
        except OSError:
            server.server_close()
            raise
    except OSError:
        cleanup_errors = []
        try:
            controller.personal.close()
        except Exception as error:
            cleanup_errors.append(error)
        try:
            controller.close()
        except Exception as error:
            cleanup_errors.append(error)
        suffix = (' Cleanup failed: ' + '; '.join(safe_error(error) for error in cleanup_errors)
                  if cleanup_errors else '')
        parser.exit(1, f'Cannot bind http://localhost:{args.port}. The port may be occupied. '
                       'Choose --port <free-port> or stop the known listener first; no automatic fallback.'
                       + suffix + '\n')
    info = {'pid':os.getpid(),'child_pid':controller.transport.pid,'url':f'http://localhost:{server.server_port}',
            'runtime_root':str(runtime),'version':version,'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
            'work_enabled':bool(bridge),'work_bridge_pid':bridge.pid if bridge else None,
            'desktop_connection':'relay' if args.desktop_relay else 'inherited' if bridge else 'unavailable'}
    (runtime/'running.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
    print(json.dumps(info),flush=True)
    servers = (server,ipv6_server)
    workers = [threading.Thread(target=s.serve_forever,daemon=True) for s in servers]
    for worker in workers: worker.start()
    if bridge:
        def escalate_overdue():
            from .escalation import escalate
            while not (runtime/'shutdown.flag').exists():
                try:
                    escalate(bridge)
                except Exception as error:
                    print(f'Request escalation skipped: {safe_error(error)}', file=sys.stderr, flush=True)
                for _ in range(240):
                    if (runtime/'shutdown.flag').exists(): break
                    time.sleep(.25)
        threading.Thread(target=escalate_overdue, daemon=True).start()
    try:
        while not (runtime/'shutdown.flag').exists():time.sleep(.25)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_errors = []
        for s in servers:
            try:
                s.shutdown()
            except Exception as error:
                cleanup_errors.append(f'server shutdown: {safe_error(error)}')
            try:
                s.server_close()
            except Exception as error:
                cleanup_errors.append(f'server close: {safe_error(error)}')
        with controller.personal.lock:
            controller.personal.closed = True
            info['personal_child_pids'] = [c.transport.pid
                for c in controller.personal.controllers.values()
                if hasattr(c,'transport') and isinstance(c.transport.pid, int)]
        with controller.rooms.lock:
            controller.rooms.closed = True
            info['room_child_pids'] = [c.transport.pid for c in controller.rooms.controllers.values()]
        try:
            controller.rooms.close()
        except Exception as error:
            cleanup_errors.append(f'rooms close: {safe_error(error)}')
        try:
            controller.personal.close()
        except Exception as error:
            cleanup_errors.append(f'personal controllers close: {safe_error(error)}')
        finally:
            try:
                controller.close()
            except Exception as error:
                cleanup_errors.append(f'room controller close: {safe_error(error)}')
        for worker in workers:
            worker.join(timeout=3)
            if worker.is_alive():
                cleanup_errors.append('server worker did not stop within 3 seconds')
        info['stopped'] = not cleanup_errors
        if cleanup_errors:
            info['cleanup_errors'] = cleanup_errors
        try:
            (runtime/'running.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
        except OSError as error:
            print(f'Could not persist shutdown state: {error}', file=sys.stderr)
            raise
        if cleanup_errors:
            print('Codex Office cleanup incomplete: ' + '; '.join(cleanup_errors), file=sys.stderr)
            raise SystemExit(1)


if __name__ == '__main__': main()
