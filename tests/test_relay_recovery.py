"""The Desktop relay survives an app-server restart (#315): its MCP bridge is restarted in place, an unhealthy relay is
replaced, and the Monitor names the remedy. Uses a disposable fake MCP server and port 0, never the live relay."""
import io
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'scripts'))
import isolation  # noqa: E402

isolation.isolate()
from team_room import desktop_relay, work  # noqa: E402
import status_sources  # noqa: E402

FAKE = textwrap.dedent('''
    import json, os, sys
    marker = os.environ['FAKE_MARKER']
    dead = os.path.exists(marker)  # Desktop refuses this process: the old pipe is gone
    for line in sys.stdin:
        m = json.loads(line)
        if 'id' not in m: continue
        if dead and m['method'] == 'tools/call':
            print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'error': {'message': 'pipe closed'}}), flush=True); continue
        if m['method'] == 'tools/list':
            r = {'tools': [{'name': n} for n in ('send_message_to_thread', 'wait_threads', 'read_thread')]}
        elif m['method'] == 'tools/call':
            r = {'content': [{'type': 'text', 'text': json.dumps({'threads': []})}]}
        else:
            r = {}
        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': r}), flush=True)
''')


class Bridge(work.DesktopBridge):
    pass


class RelayRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.script = self.dir / 'fake_mcp.py'
        self.script.write_text(FAKE, encoding='utf-8')
        self.marker = self.dir / 'dead'
        patcher = mock.patch.dict(os.environ, {'CODEX_THREAD_ID': work.MANAGER, 'CODEX_APP_TOOLS_PIPE_PATH': 'fake', 'FAKE_MARKER': str(self.marker)})
        patcher.start(); self.addCleanup(patcher.stop)
        self.bridge = Bridge(sys.executable, f'{self.script}')
        self.addCleanup(self.bridge.close)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bridge_restarts_after_its_process_dies(self):
        self.assertEqual(self.bridge.call('list_threads', {'limit': 1}), {'threads': []})
        self.bridge.process.kill(); self.bridge.process.wait()
        self.assertFalse(self.bridge.alive())
        self.bridge.reconnect()
        self.assertTrue(self.bridge.alive())
        self.assertEqual(self.bridge.call('list_threads', {'limit': 1}), {'threads': []})

    def test_watchdog_recovers_a_killed_bridge_without_stopping_the_relay(self):
        self.bridge.process.kill(); self.bridge.process.wait()
        said = io.StringIO()
        ticks = iter([False, False, True])
        finished = desktop_relay.watch(self.bridge, lambda: next(ticks), say=lambda text, flush=True: said.write(text + '\n'), interval=0.01)
        self.assertTrue(finished)
        self.assertIn('recovered', said.getvalue())
        self.assertEqual(self.bridge.call('list_threads', {'limit': 1}), {'threads': []})

    def test_watchdog_stops_the_relay_when_desktop_never_accepts_it_again(self):
        self.marker.write_text('x')  # every new process is refused, like a stale pipe after a restart
        self.bridge.reconnect()
        said = io.StringIO()
        now = [0.0]
        def clock():
            now[0] += 60.0
            return now[0]
        finished = desktop_relay.watch(self.bridge, lambda: False, say=lambda text, flush=True: said.write(text + '\n'),
                                       interval=0.01, give_up=120.0, clock=clock)
        self.assertFalse(finished)
        self.assertIn('Remedy: restart the Desktop relay', said.getvalue())

    def test_replace_unhealthy_stops_a_live_relay_through_the_shutdown_flag(self):
        root = self.dir / 'relay'
        root.mkdir()
        child = __import__('subprocess').Popen([sys.executable, '-c',
            'import sys,time,pathlib\nf=pathlib.Path(sys.argv[1])\nwhile not f.exists(): time.sleep(.1)', str(root / 'shutdown.flag')])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        (root / 'relay.json').write_text(json.dumps({'pid': child.pid}), encoding='utf-8')
        desktop_relay.replace_unhealthy(root, say=lambda *a, **k: None, wait=10.0)
        child.wait(timeout=5)
        self.assertFalse((root / 'relay.json').exists())
        self.assertFalse((root / 'shutdown.flag').exists())

    def test_replace_unhealthy_ignores_a_stale_descriptor(self):
        root = self.dir / 'relay2'
        root.mkdir()
        (root / 'relay.json').write_text(json.dumps({'pid': 2 ** 22 + 12345}), encoding='utf-8')
        desktop_relay.replace_unhealthy(root, say=lambda *a, **k: None)
        self.assertFalse((root / 'relay.json').exists())

    def test_disposable_relay_answers_again_after_its_bridge_is_killed(self):
        root = self.dir / 'live'
        server = desktop_relay.create_relay(self.bridge, root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stopped = threading.Event()
        watcher = threading.Thread(target=desktop_relay.watch, args=(self.bridge, stopped.is_set),
                                   kwargs={'say': lambda *a, **k: None, 'interval': 0.05}, daemon=True)
        watcher.start()
        self.addCleanup(lambda: (stopped.set(), server.shutdown(), server.server_close(), watcher.join(timeout=5)))
        client = desktop_relay.RelayBridge(root)
        self.assertEqual(client.health()['origin'], work.MANAGER)
        self.bridge.process.kill()
        deadline = time.monotonic() + 15
        while True:
            try:
                client.health(); break
            except work.Uncertain:
                self.assertLess(time.monotonic(), deadline, 'relay did not recover')
                time.sleep(0.1)

    def test_sustained_failure_needs_every_recheck_to_fail(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) == 1: raise work.Uncertain('transient')
        self.assertFalse(desktop_relay.sustained_failure(flaky, 3, 0, sleep=lambda s: None))
        self.assertEqual(len(calls), 2)
        def dead(): raise OSError('refused')
        self.assertTrue(desktop_relay.sustained_failure(dead, 3, 0, sleep=lambda s: None))

    def test_registration_keeps_a_relay_after_a_transient_error(self):
        from team_room import controller
        root = self.dir / '.bot-mode' / 'desktop-relay'
        server = desktop_relay.create_relay(self.bridge, root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        real = desktop_relay.RelayBridge.health
        state = {'n': 0}
        def health(bridge):
            state['n'] += 1
            if state['n'] == 1: raise work.Uncertain('transient')
            return real(bridge)
        out = io.StringIO()
        with mock.patch.object(controller, 'ROOT', self.dir), mock.patch.object(desktop_relay.RelayBridge, 'health', health), \
                mock.patch.object(desktop_relay, 'RECHECK_GAP', 0.01), mock.patch.object(desktop_relay, 'replace_unhealthy') as replace, \
                mock.patch.object(sys, 'argv', ['desktop_relay', '--root', str(root), '--codex-path', sys.executable]), \
                mock.patch('sys.stdout', out):
            desktop_relay.main()
        replace.assert_not_called()
        self.assertIn('keeping it', out.getvalue())
        self.assertTrue((root / 'relay.json').exists())

    def test_monitor_names_the_remedy(self):
        self.assertIn('Manager chat', status_sources.RELAY_REMEDY)
        self.assertIn('start_codex_office.ps1 -RelayOnly', status_sources.RELAY_REMEDY)
        self.assertIn('start_codex_office.ps1 -RelayOnly', desktop_relay.RELAY_REMEDY)
        self.assertIn('Restart the Desktop relay', status_sources.RELAY_REMEDY.replace('restart', 'Restart', 1))


if __name__ == '__main__':
    unittest.main()
