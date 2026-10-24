"""Upgrade-time graceful stop (#244, #219): stop-previous-office.ps1 against a fake controller endpoint."""
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'installer' / 'stop-previous-office.ps1'
TOKEN = 'launch-secret'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class FakeService:
    """Answers /desktop/shutdown like the real service: 200 stopped, 202 draining, 409 refused."""

    def __init__(self, mode, helper=None, token=TOKEN):
        self.mode, self.helper, self.token, self.requests = mode, helper, token, []
        outer = self

        class Handler(BaseHandler):
            owner = outer

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class BaseHandler(BaseHTTPRequestHandler):
    owner = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        service = self.owner
        service.requests.append((self.path, self.headers.get('X-Office-Controller-Token'), self.headers.get('Origin')))
        status, body = 403, {'error': 'Controller identity or origin rejected'}
        if self.headers.get('X-Office-Controller-Token') == service.token and self.path == '/desktop/shutdown':
            if service.mode == 'stop':
                service.helper.kill()
                service.helper.wait()
                status, body = 200, {'state': 'stopped', 'safety': 'safe'}
            elif service.mode == 'pending':
                status, body = 202, {'stop_pending': True}
            else:
                status, body = 409, {'reason': 'Shutdown was refused: owned work is active.'}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(sys.platform == 'win32', 'the installer script is Windows PowerShell')
class UpgradeStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.helpers, self.services = [], []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for service in self.services:
            service.close()
        for helper in self.helpers:
            if helper.poll() is None:
                helper.kill()
            helper.wait()
        self.tmp.cleanup()

    def helper(self, executable=None):
        exe = executable or sys.executable
        helper = subprocess.Popen([str(exe), '-c', 'import time; time.sleep(300)'])
        self.helpers.append(helper)
        return helper

    def service(self, *args, **kwargs):
        service = FakeService(*args, **kwargs)
        self.services.append(service)
        return service

    def write(self, pid, port, state='running', raw=None, project_path=None):
        project = self.root / 'project'
        runtime = project / '.bot-mode' / 'team-room-v1' / 'desktop-controller'
        runtime.mkdir(parents=True, exist_ok=True)
        if raw != '__OMIT__':
            (runtime / 'desktop.json').write_text(raw if raw is not None else json.dumps({
                'protocol': 1, 'service': 'codex-office', 'version': '0.1.0', 'pid': pid, 'port': port,
                'instance_id': 'i', 'token': TOKEN, 'state': state}), encoding='utf-8')
        settings = self.root / 'controller.json'
        settings.write_text(json.dumps({'projectPath': project_path if project_path is not None else str(project)}), encoding='utf-8')
        return settings

    def run_script(self, settings, install_dir=None):
        target_dir = install_dir if install_dir is not None else (self.root / 'isolated-install')
        target_dir.mkdir(parents=True, exist_ok=True)
        cmd = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
               '-SettingsPath', str(settings), '-InstallDir', str(target_dir), '-ShutdownTimeoutSec', '4', '-ExitWaitSec', '4']
        return subprocess.run(cmd, capture_output=True, text=True, timeout=90)

    def test_running_service_is_stopped_through_the_authenticated_shutdown(self):
        helper = self.helper()
        service = self.service('stop', helper)
        result = self.run_script(self.write(helper.pid, service.port))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('stopped safely', result.stdout)
        self.assertEqual(service.requests, [('/desktop/shutdown', TOKEN, 'http://tauri.localhost')])
        self.assertIsNotNone(helper.poll())

    def test_extended_length_path_is_normalized_and_stopped(self):
        helper = self.helper()
        service = self.service('stop', helper)
        extended_project = f'\\\\?\\{self.root / "project"}'
        result = self.run_script(self.write(helper.pid, service.port, project_path=extended_project))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('stopped safely', result.stdout)
        self.assertEqual(service.requests, [('/desktop/shutdown', TOKEN, 'http://tauri.localhost')])
        self.assertIsNotNone(helper.poll())

    def test_extended_unc_path_is_normalized(self):
        helper = self.helper()
        service = self.service('stop', helper)
        # Construct \\?\UNC\localhost\c$\... path pointing to self.root / project
        drive = self.root.drive.replace(':', '$')
        rel = str(self.root / 'project')[len(self.root.drive) + 1:]
        extended_unc = f'\\\\?\\UNC\\127.0.0.1\\{drive}\\{rel}'
        result = self.run_script(self.write(helper.pid, service.port, project_path=extended_unc))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('stopped safely', result.stdout)
        self.assertIsNotNone(helper.poll())

    def test_corrupt_descriptor_with_running_installed_service_fails_closed(self):
        install_dir = self.root / 'installed'
        install_dir.mkdir(parents=True, exist_ok=True)
        svc_exe = install_dir / 'office-service.exe'
        shutil.copy(sys.executable, svc_exe)
        helper = self.helper(executable=svc_exe)
        settings = self.write(helper.pid, 12345, raw='{ malformed')
        result = self.run_script(settings, install_dir=install_dir)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('service descriptor could not be read', result.stdout)
        self.assertIsNone(helper.poll(), 'installed process must not be terminated')

    def test_missing_descriptor_with_running_installed_service_fails_closed(self):
        install_dir = self.root / 'installed'
        install_dir.mkdir(parents=True, exist_ok=True)
        svc_exe = install_dir / 'office-service.exe'
        shutil.copy(sys.executable, svc_exe)
        helper = self.helper(executable=svc_exe)
        settings = self.write(helper.pid, 12345, raw='__OMIT__')
        result = self.run_script(settings, install_dir=install_dir)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('service descriptor could not be read', result.stdout)
        self.assertIsNone(helper.poll())

    def test_missing_settings_with_running_installed_service_fails_closed(self):
        install_dir = self.root / 'installed'
        install_dir.mkdir(parents=True, exist_ok=True)
        svc_exe = install_dir / 'office-service.exe'
        shutil.copy(sys.executable, svc_exe)
        helper = self.helper(executable=svc_exe)
        missing_settings = self.root / 'none.json'
        result = self.run_script(missing_settings, install_dir=install_dir)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('controller settings could not be found', result.stdout)
        self.assertIsNone(helper.poll())

    def test_corrupt_descriptor_without_running_service_exits_zero(self):
        install_dir = self.root / 'installed'
        install_dir.mkdir(parents=True, exist_ok=True)
        settings = self.write(1, 1, raw='{ malformed')
        result = self.run_script(settings, install_dir=install_dir)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('nothing to stop', result.stdout)

    def test_busy_service_is_reported_and_never_killed(self):
        helper = self.helper()
        service = self.service('busy', helper)
        result = self.run_script(self.write(helper.pid, service.port))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('owned work is active', result.stdout)
        self.assertIsNone(helper.poll(), 'the service must be left running')

    def test_draining_service_that_does_not_stop_in_time_is_reported(self):
        helper = self.helper()
        service = self.service('pending', helper)
        result = self.run_script(self.write(helper.pid, service.port))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('still finishing work', result.stdout)
        self.assertIsNone(helper.poll())

    def test_rejected_token_is_reported_and_nothing_is_killed(self):
        helper = self.helper()
        service = self.service('stop', helper, token='different')
        result = self.run_script(self.write(helper.pid, service.port))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIsNone(helper.poll())

    def test_live_but_silent_listener_is_not_treated_as_stale(self):
        helper = self.helper()
        with socket.socket() as silent:
            silent.bind(('127.0.0.1', 0))
            silent.listen(5)
            result = self.run_script(self.write(helper.pid, silent.getsockname()[1]))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('did not answer', result.stdout)
        self.assertIsNone(helper.poll())

    def test_dead_service_with_a_free_port_needs_no_stop(self):
        helper = self.helper()
        helper.kill()
        helper.wait()
        result = self.run_script(self.write(helper.pid, free_port()))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('stale descriptor', result.stdout)

    def test_reused_pid_of_another_program_with_a_free_port_is_left_alone(self):
        helper = self.helper()
        result = self.run_script(self.write(helper.pid, free_port()))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(helper.poll(), 'a foreign process is never touched')

    def test_missing_clean_or_unreadable_state_needs_no_stop_when_idle(self):
        install_dir = self.root / 'installed'
        install_dir.mkdir(parents=True, exist_ok=True)
        missing = self.root / 'none.json'
        self.assertEqual(self.run_script(missing, install_dir=install_dir).returncode, 0)
        self.assertEqual(self.run_script(self.write(1, 1, state='stopped'), install_dir=install_dir).returncode, 0)
        self.assertEqual(self.run_script(self.write(1, 1, raw='{ malformed'), install_dir=install_dir).returncode, 0)
        self.assertEqual(self.run_script(self.write(1, 70000), install_dir=install_dir).returncode, 0, 'an out-of-range port is an unreadable descriptor, not an error')


if __name__ == '__main__':
    unittest.main()

