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
from scripts.office_desktop import DesktopLifecycle, LifecycleBusy, inspect_shutdown, set_draining, verify_runtime_slot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-path', type=Path)
    parser.add_argument('--port', type=int, default=24242, help='Loopback port (default: 24242; 0 selects a test port)')
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--runtime-root', type=Path)
    parser.add_argument('--enable-work',action='store_true',help='Use naturally inherited Manager Desktop MCP context for Run task')
    parser.add_argument('--desktop-relay',action='store_true',help='Reconnect to the locally registered Desktop relay without executor environment')
    parser.add_argument('--project', type=Path, help='Project to serve (default: this repository); participants come from its roster')
    parser.add_argument('--desktop-managed', action='store_true', help='Enable authenticated native controller lifecycle endpoints')
    parser.add_argument('--runtime-install-root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--runtime-slot', help=argparse.SUPPRESS)
    parser.add_argument('--runtime-release-id', help=argparse.SUPPRESS)
    parser.add_argument('--runtime-slot-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--runtime-component-id', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    project_root = served.configure(args.project or ROOT)
    allowed = (project_root / '.bot-mode/team-room-v1').resolve()
    if not args.codex_path: parser.error('--codex-path is required')
    identity_values=(args.runtime_install_root,args.runtime_slot,args.runtime_release_id,
                     args.runtime_slot_sha256,args.runtime_component_id)
    runtime_identity=None
    if any(value is not None for value in identity_values):
        if not args.desktop_managed or not all(value is not None for value in identity_values):
            parser.error('A complete owned runtime identity requires --desktop-managed')
        try:
            runtime_identity=verify_runtime_slot(
                args.runtime_install_root,args.runtime_slot,args.runtime_release_id,
                args.runtime_slot_sha256,args.runtime_component_id,Path(__file__).resolve())
        except (ValueError,OSError) as error:
            parser.error('Office runtime slot validation failed: '+str(error))
    runtime = (args.runtime_root or allowed / ('room-' + uuid.uuid4().hex[:8])).resolve()
    if runtime == allowed or not runtime.is_relative_to(allowed):
        parser.error('Runtime must be a child of .bot-mode/team-room-v1')
    runtime.mkdir(parents=True, exist_ok=True)
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
        controller = Controller(runtime,binary,args.model,bots=lead_room,profile_project=project_root,
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
        from .update_host import build as build_updates
        controller.updates = build_updates(project_root)
    except Exception:
        for holder in ('rooms', 'personal'):
            resource = getattr(controller, holder, None)
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        controller.close()
        raise
    lifecycle = (DesktopLifecycle(runtime,version=(runtime_identity or {}).get('runtime_version','0.1.0'),
                                  runtime_identity=runtime_identity) if args.desktop_managed else None)
    try:
        server = (create_server(controller,args.port,desktop_lifecycle=lifecycle) if lifecycle
                  else create_server(controller,args.port))
        try:
            ipv6_server = (create_server(controller,server.server_port,ipv6=True,token=server.room_token,
                                         desktop_lifecycle=lifecycle) if lifecycle
                           else create_server(controller,server.server_port,ipv6=True,token=server.room_token))
        except OSError:
            server.server_close()
            raise
    except OSError as error:
        if lifecycle:
            lifecycle.log('Could not claim configured loopback port; the existing listener was not contacted. '+safe_error(error))
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
    if lifecycle:
        lifecycle.mark_running(server.server_port)
        info.update({'desktop_protocol': 1, 'desktop_instance_id': lifecycle.instance_id})
    print(json.dumps(info),flush=True)
    servers = (server,ipv6_server)
    workers = [threading.Thread(target=s.serve_forever,daemon=True) for s in servers]
    for worker in workers: worker.start()
    escalation_worker = None
    if bridge:
        def escalate_overdue():
            from .escalation import escalate
            while not (lifecycle and lifecycle.stop_requested):
                try:
                    if lifecycle:
                        with lifecycle.operation():
                            if not lifecycle.stop_requested:
                                escalate(bridge)
                    else:
                        escalate(bridge)
                except Exception as error:
                    if lifecycle and lifecycle.stop_requested:
                        break
                    print(f'Request escalation skipped: {safe_error(error)}', file=sys.stderr, flush=True)
                for _ in range(240):
                    if lifecycle and lifecycle.stop_requested: break
                    time.sleep(.25)
        escalation_worker = threading.Thread(target=escalate_overdue, daemon=True)
        escalation_worker.start()
    try:
        while True:
            if lifecycle and lifecycle.stop_requested:
                state = inspect_shutdown(controller, getattr(controller, 'personal', None),
                    getattr(controller, 'rooms', None), getattr(controller, 'project_managers', None))
                if state['safety'] == 'safe':
                    if not lifecycle.commit_stop_if_safe():
                        time.sleep(.05)
                        continue
                    break
                time.sleep(.5)
            else:
                time.sleep(.25)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_errors = []
        set_draining(controller, getattr(controller, 'personal', None),
            getattr(controller, 'rooms', None), getattr(controller, 'project_managers', None), True)
        if lifecycle:
            lifecycle.mark_stopped()
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
        if escalation_worker:
            escalation_worker.join(timeout=3)
            if escalation_worker.is_alive():
                cleanup_errors.append('escalation worker did not stop within 3 seconds')
        info['stopped'] = not cleanup_errors
        if cleanup_errors:
            info['cleanup_errors'] = cleanup_errors
        try:
            (runtime/'running.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
        except OSError as error:
            print(f'Could not persist shutdown state: {error}', file=sys.stderr)
            raise
        if cleanup_errors:
            if lifecycle:
                lifecycle.mark_stopped(clean=False)
            print('Codex Office cleanup incomplete: ' + '; '.join(cleanup_errors), file=sys.stderr)
            raise SystemExit(1)


if __name__ == '__main__': main()
