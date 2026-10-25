"""The PowerShell starter recovers a dead relay while Office already answers (#316). Runs a copy of the script in a
disposable tree with a stub relay program and a stub Office on a random port; never touches the live Office or relay."""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'start_codex_office.ps1'
MANAGER = '11111111-2222-3333-4444-555555555555'

STUB_RELAY = '''
import sys, json, time, pathlib
a = sys.argv[1:]
root = pathlib.Path(a[a.index('--root') + 1])
marker = root.parent / 'relay-started'
mode = root.parent / 'stub-mode'
mode = mode.read_text() if mode.exists() else 'ok'
if '--probe' in a:
    sys.exit(0 if marker.exists() else 1)
if mode == 'fail':
    sys.exit(7)
if mode == 'delay':
    time.sleep(4)
marker.write_text('started')
root.mkdir(parents=True, exist_ok=True)
(root / 'relay.json').write_text(json.dumps({'url': 'http://127.0.0.1:1', 'pid': 1}))
time.sleep(8)
'''


class Office(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self):
        body = json.dumps({'participants': [], 'journal': [], 'limits': {}}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


@unittest.skipUnless(sys.platform == 'win32', 'PowerShell starter')
class StarterRelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'scripts' / 'team_room').mkdir(parents=True)
        shutil.copyfile(SCRIPT, root / 'scripts' / 'start_codex_office.ps1')
        (root / 'scripts' / '__init__.py').write_text('')
        (root / 'scripts' / 'team_room' / '__init__.py').write_text('')
        (root / 'scripts' / 'team_room' / 'project.py').write_text('def configure(*a, **k): pass\n')
        (root / 'scripts' / 'team_room' / 'work.py').write_text(f"MANAGER = '{MANAGER}'\n")
        (root / 'scripts' / 'team_room' / 'desktop_relay.py').write_text(STUB_RELAY)
        self.root = root
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Office)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.tmp.cleanup()))

    def run_starter(self, *flags, manager=True):
        env = dict(os.environ, CBM_OFFICE_URL=f'http://127.0.0.1:{self.server.server_port}', CBM_CODEX_BINARY=sys.executable)
        env.pop('CODEX_THREAD_ID', None); env.pop('CODEX_APP_TOOLS_PIPE_PATH', None)
        if manager:
            env.update(CODEX_THREAD_ID=MANAGER, CODEX_APP_TOOLS_PIPE_PATH='fake')
        return subprocess.run(['powershell', '-NoProfile', '-File', str(self.root / 'scripts' / 'start_codex_office.ps1'),
                               '-NoBrowser', *flags], cwd=self.root, env=env, capture_output=True, text=True, timeout=90)

    def test_relay_only_recovers_a_dead_relay_while_office_answers(self):
        result = self.run_starter('-RelayOnly')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / 'relay-started').exists() or (self.root / '.bot-mode' / 'relay-started').exists())
        self.assertTrue((self.root / '.bot-mode' / 'desktop-relay' / 'relay.json').exists())

    def test_plain_starter_also_recovers_the_relay_with_a_healthy_office(self):
        result = self.run_starter()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / '.bot-mode' / 'desktop-relay' / 'relay.json').exists())
        self.assertIn('Codex Office is ready', result.stdout)

    def stale_descriptor(self, mode):
        bot = self.root / '.bot-mode'
        (bot / 'desktop-relay').mkdir(parents=True)
        (bot / 'desktop-relay' / 'relay.json').write_text(json.dumps({'url': 'http://127.0.0.1:1', 'pid': 1}))
        (bot / 'stub-mode').write_text(mode)

    def test_stale_descriptor_with_a_failed_replacement_fails_with_the_exit_code(self):
        self.stale_descriptor('fail')
        result = self.run_starter('-RelayOnly')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('exit code 7', result.stderr)  # errors go to stderr, not only the host stream
        self.assertNotIn('Desktop relay is ready', result.stdout)

    def test_stale_descriptor_with_a_delayed_replacement_waits_and_then_succeeds(self):
        self.stale_descriptor('delay')
        result = self.run_starter('-RelayOnly')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / '.bot-mode' / 'relay-started').exists())
        self.assertIn('Desktop relay is ready', result.stdout)  # the success line is on stdout

    def test_relay_only_outside_the_manager_chat_fails_with_a_clear_message(self):
        result = self.run_starter('-RelayOnly', manager=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('appointed Manager chat', result.stderr)


if __name__ == '__main__':
    unittest.main()
