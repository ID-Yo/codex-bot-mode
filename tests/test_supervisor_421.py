"""The relay starts without the Manager chat and a supervisor pass restarts what is down (#421, epic #420).
Uses a disposable fake MCP server and fakes for processes; never the live relay or the real Desktop pipe."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'scripts'))
import isolation  # noqa: E402

isolation.isolate()
from team_room import desktop_pipe, desktop_relay, work  # noqa: E402

spec = importlib.util.spec_from_file_location('watchdog421', ROOT / 'scripts/relay_watchdog.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

MANAGER = '11111111-2222-7333-8444-555555555555'
FAKE = textwrap.dedent('''
    import json, os, sys
    pipe = os.environ.get('CODEX_APP_TOOLS_PIPE_PATH', '')
    for line in sys.stdin:
        m = json.loads(line)
        if 'id' not in m: continue
        if m['method'] == 'tools/call' and pipe.endswith('dead'):
            print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'error': {'message': 'pipe closed'}}), flush=True); continue
        if m['method'] == 'tools/list':
            r = {'tools': [{'name': n} for n in ('send_message_to_thread', 'wait_threads', 'read_thread')]}
        elif m['method'] == 'tools/call':
            r = {'content': [{'type': 'text', 'text': json.dumps({'pipe': pipe, 'thread_env': 'CODEX_THREAD_ID' in os.environ})}]}
        else:
            r = {}
        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': r}), flush=True)
''')


class PipeDiscoveryTests(unittest.TestCase):
    def test_only_codex_tool_pipes_are_listed(self):
        with mock.patch.object(desktop_pipe.os, 'name', 'nt'):
            found = desktop_pipe.tool_pipes(['codex-browser-use-1', 'codex-app-tools-b', 'codex-app-tools-a', 'other'])
        self.assertEqual([p.rsplit('\\', 1)[-1] for p in found], ['codex-app-tools-a', 'codex-app-tools-b'])

    def test_newest_package_wins_and_override_beats_it(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ('OpenAI.Codex_26.1013.3656.0_x64__abc', 'OpenAI.Codex_26.1100.100.0_x64__abc', 'Other.App_1.0'):
                binary = base / name / 'app' / 'resources' / 'codex.exe'
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b'x')
            self.assertIn('26.1100.100.0', str(desktop_pipe.find_codex_binary({}, str(base))))
            custom = base / 'mine.exe'
            custom.write_bytes(b'x')
            self.assertEqual(desktop_pipe.find_codex_binary({'CBM_CODEX_BINARY': str(custom)}, str(base)), custom)

    def test_missing_runtime_names_the_remedy(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'CBM_CODEX_BINARY'):
                desktop_pipe.find_codex_binary({}, folder)


class StandaloneBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = Path(self.tmp.name) / 'fake_mcp.py'
        self.script.write_text(FAKE, encoding='utf-8')
        environment = {k: v for k, v in os.environ.items() if k not in ('CODEX_THREAD_ID', 'CODEX_APP_TOOLS_PIPE_PATH')}
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start(); self.addCleanup(patcher.stop)
        manager = mock.patch.object(work, 'MANAGER', MANAGER)
        manager.start(); self.addCleanup(manager.stop)

    def bridge(self, *pipes):
        self.pipes = list(pipes)
        patcher = mock.patch.object(desktop_pipe, 'tool_pipes', side_effect=lambda: list(self.pipes))
        patcher.start(); self.addCleanup(patcher.stop)
        bridge = work.DesktopBridge(sys.executable, str(self.script), standalone=True)
        self.addCleanup(bridge.close)
        return bridge

    def test_starts_without_manager_chat_environment_and_uses_the_found_pipe(self):
        bridge = self.bridge('pipe/codex-app-tools-1-good')
        self.assertEqual(bridge.origin, MANAGER)
        self.assertTrue(bridge.standalone)
        answer = bridge.call('list_threads', {'limit': 1})
        self.assertEqual(answer, {'pipe': 'pipe/codex-app-tools-1-good', 'thread_env': False})
        self.assertEqual(bridge.pipe, 'pipe/codex-app-tools-1-good')

    def test_a_pipe_that_refuses_calls_is_skipped(self):
        bridge = self.bridge('pipe/codex-app-tools-1-dead', 'pipe/codex-app-tools-2-good')
        self.assertEqual(bridge.pipe, 'pipe/codex-app-tools-2-good')

    def test_reconnect_finds_a_renamed_pipe(self):
        bridge = self.bridge('pipe/codex-app-tools-1-good')
        self.pipes[:] = ['pipe/codex-app-tools-2-good']  # Desktop restarted and created a new pipe
        bridge.reconnect()
        self.assertEqual(bridge.pipe, 'pipe/codex-app-tools-2-good')
        self.assertEqual(bridge.call('list_threads', {'limit': 1})['pipe'], 'pipe/codex-app-tools-2-good')

    def test_no_pipe_means_desktop_is_not_running(self):
        with self.assertRaisesRegex(ValueError, 'Desktop'):
            self.bridge()

    def test_a_roster_without_manager_is_refused(self):
        with mock.patch.object(work, 'MANAGER', '00000000-0000-7000-8000-000000000001'):
            with self.assertRaisesRegex(ValueError, 'Manager'):
                self.bridge('pipe/codex-app-tools-1-good')

    def test_the_manager_chat_launch_still_needs_its_own_environment(self):
        with self.assertRaises(ValueError):
            work.DesktopBridge(sys.executable, str(self.script))


class StandaloneStartupTests(unittest.TestCase):
    def parser(self):
        import argparse
        return argparse.ArgumentParser()

    def test_every_standalone_failure_ends_with_a_controlled_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            codex = Path(folder) / 'resources' / 'codex.exe'
            codex.parent.mkdir()
            codex.write_bytes(b'x')
            for error in (ValueError('no pipe'), desktop_relay.Uncertain('pipe refused'), RuntimeError('tool error'), OSError('gone')):
                with self.subTest(type(error).__name__), mock.patch.object(desktop_relay, 'DesktopBridge', side_effect=error):
                    with self.assertRaises(SystemExit) as raised:
                        desktop_relay.open_bridge(codex, True, self.parser())
                    self.assertEqual(raised.exception.code, 2)

    def test_the_manager_chat_start_keeps_raising_the_original_error(self):
        with tempfile.TemporaryDirectory() as folder:
            codex = Path(folder) / 'resources' / 'codex.exe'
            codex.parent.mkdir()
            codex.write_bytes(b'x')
            with mock.patch.object(desktop_relay, 'DesktopBridge', side_effect=desktop_relay.Uncertain('boom')):
                with self.assertRaises(desktop_relay.Uncertain):
                    desktop_relay.open_bridge(codex, False, self.parser())


class PumpTests(unittest.TestCase):
    def test_pump_hung_only_when_lock_held_work_waits_and_heartbeat_is_old(self):
        with tempfile.TemporaryDirectory() as folder:
            beat = Path(folder) / '.bot-mode/outbox/pump.heartbeat.json'
            beat.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            old = (now - timedelta(seconds=w.HUNG_PUMP_SECONDS + 30)).isoformat()
            beat.write_text(json.dumps({'at': old, 'pid': 77}))
            status = {'pending': 2, 'pump_alive': True}
            self.assertEqual(w.pump_hung(folder, status), {'pid': 77, 'at': old})
            self.assertIsNone(w.pump_hung(folder, {'pending': 0, 'pump_alive': True}))
            self.assertIsNone(w.pump_hung(folder, {'pending': 2, 'pump_alive': False}))
            beat.write_text(json.dumps({'at': (now - timedelta(seconds=20)).isoformat(), 'pid': 77}))
            self.assertIsNone(w.pump_hung(folder, status))
            beat.unlink()
            self.assertIsNone(w.pump_hung(folder, status))

    def test_an_unprovable_pump_is_reported_and_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            beat = Path(folder) / '.bot-mode/outbox/pump.heartbeat.json'
            beat.parent.mkdir(parents=True)
            beat.write_text(json.dumps({'at': (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), 'pid': 77}))
            status = {'pending': 1, 'pump_alive': True, 'problems': []}
            with mock.patch.object(w, 'outbox_status', return_value=status), \
                    mock.patch.object(w, 'stop_pump', return_value=(False, 'process is not this project\'s outbox pump')) as stop:
                report = w.project_report(folder, heal_hung=True)
            stop.assert_called_once()
            self.assertFalse(report['hung_pump']['stopped'])
            self.assertTrue(any('not stopped' in p for p in report['outbox']['problems']))


class ProjectPumpIdentityTests(unittest.TestCase):
    """Exact identity of a pump command line: interpreter, this project's outbox.py, --project <this project>, subcommand pump."""
    P = 'C:\\work\\proj'

    def argv(self, script=None, project=None, sub='pump', options=('-B',), python='C:\\Python\\python.exe'):
        return [python, *options, script or self.P + '\\.bot-mode\\outbox.py', '--project', project or self.P, sub, '--max-wait', '86400']

    def test_the_real_pump_command_matches_with_any_slashes_and_case(self):
        self.assertTrue(w.is_project_pump(self.argv(), self.P))
        self.assertTrue(w.is_project_pump(self.argv(script='c:/work/PROJ/.bot-mode/outbox.py', project='c:/work/proj/'), self.P))
        self.assertTrue(w.is_project_pump(self.argv(python='pythonw.exe', options=()), self.P))

    def test_everything_else_is_refused(self):
        refused = {
            'overlapping project prefix': self.argv(script=self.P + '2\\.bot-mode\\outbox.py', project=self.P + '2'),
            'project is a parent of the pump project': self.argv(script=self.P + '\\sub\\.bot-mode\\outbox.py', project=self.P + '\\sub'),
            'other script': self.argv(script='C:\\work\\proj\\.bot-mode\\other.py'),
            'status not pump': self.argv(sub='status'),
            'project only as data': ['python.exe', 'other.py', self.P + '\\.bot-mode\\outbox.py', '--project', self.P, 'pump'],
            'python -m': ['python.exe', '-m', 'x', self.P + '\\.bot-mode\\outbox.py', '--project', self.P, 'pump', '--max-wait', '1'],
            'attached module option': ['python.exe', '-mfoo', self.P + '\\.bot-mode\\outbox.py', '--project', self.P, 'pump', '--max-wait', '1'],
            'attached command option': ['python.exe', '-cprint(1)', self.P + '\\.bot-mode\\outbox.py', '--project', self.P, 'pump', '--max-wait', '1'],
            'unknown interpreter option': self.argv(options=('-X', 'utf8')),
            'extra arguments': self.argv() + ['--extra'],
            'relative script': self.argv(script='.bot-mode/outbox.py'),
            'relative project': self.argv(project='proj'),
            'not python': self.argv(python='C:\\tools\\node.exe'),
            'empty': [],
        }
        for name, argv in refused.items():
            with self.subTest(name):
                self.assertFalse(w.is_project_pump(argv, self.P))


@unittest.skipUnless(os.name == 'nt', 'process inspection is the Windows path')
class StopPumpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.started = []
        self.addCleanup(lambda: [p.kill() for p in self.started if p.poll() is None])

    def pump(self, name, sub='pump'):
        project = Path(self.tmp.name) / name
        script = project / '.bot-mode' / 'outbox.py'
        script.parent.mkdir(parents=True)
        script.write_text('import time\ntime.sleep(120)\n')
        process = subprocess.Popen([sys.executable, '-B', str(script), '--project', str(project), sub, '--max-wait', '86400'], creationflags=0x08000000)
        self.started.append(process)
        return project, process

    def test_stop_pump_proves_exact_project_command_and_process_age_and_confirms_the_end(self):
        mine, pump = self.pump('proj')
        _, longer_name = self.pump('proj2')           # shares the first project's path as a prefix
        _, status_run = self.pump('proj3', sub='status')
        future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        time.sleep(0.5)
        refused = "process is not this project's outbox pump"
        self.assertEqual(w.stop_pump(longer_name.pid, mine, future), (False, refused))
        self.assertEqual(w.stop_pump(status_run.pid, mine, future), (False, refused))
        self.assertEqual(w.stop_pump(pump.pid, mine, past), (False, 'PID now belongs to a newer process'))  # recycled PID
        for process in (pump, longer_name, status_run):
            self.assertIsNone(process.poll())
        self.assertEqual(w.stop_pump(pump.pid, mine, future), (True, 'stopped'))
        pump.wait(10)
        self.assertIsNotNone(pump.poll())
        self.assertIsNone(longer_name.poll())
        self.assertEqual(w.stop_pump(pump.pid, mine, future), (False, 'process already gone'))

    def test_success_is_not_reported_while_the_process_still_runs(self):
        answers = iter([('{"cmd": "c", "created": "2026-01-01T00:00:00.0000000Z"}', ''),
                        ('stopped', ''),
                        ('{"cmd": "c", "created": "2026-01-01T00:00:00.0000000Z"}', '')])
        with mock.patch.object(w, '_powershell', side_effect=lambda *a, **k: next(answers, ('{"cmd": "c", "created": "2026-01-01T00:00:00.0000000Z"}', ''))), \
                mock.patch.object(w, 'windows_argv', return_value=['python.exe', 'x']), mock.patch.object(w, 'is_project_pump', return_value=True), \
                mock.patch.object(w.time, 'sleep'), mock.patch.object(w.time, 'monotonic', side_effect=[0, 1, 2, 11, 12]):
            self.assertEqual(w.stop_pump(5, 'p', '2026-06-01T00:00:00+00:00'), (False, 'stop was requested but the process is still running'))

    def test_a_failed_stop_is_not_success(self):
        answers = iter([('{"cmd": "c", "created": "2026-01-01T00:00:00.0000000Z"}', ''), ('error', '')])
        with mock.patch.object(w, '_powershell', side_effect=lambda *a, **k: next(answers)), \
                mock.patch.object(w, 'windows_argv', return_value=['python.exe']), mock.patch.object(w, 'is_project_pump', return_value=True):
            self.assertEqual(w.stop_pump(5, 'p', '2026-06-01T00:00:00+00:00'), (False, 'stop failed'))


class StopPumpPlatformTests(unittest.TestCase):
    def test_another_platform_cannot_prove_identity(self):
        with mock.patch.object(w.os, 'name', 'posix'):
            stopped, reason = w.stop_pump(1, 'p', 'x')
        self.assertFalse(stopped)
        self.assertIn('cannot be verified', reason)


class RelayStartTests(unittest.TestCase):
    def child(self, polls, pid=5):
        process = mock.Mock(pid=pid)
        process.poll.side_effect = polls
        return process

    def start(self, process, probe, wait=5, descriptor_pid=None, ended=True, alive=False, folder=None):
        with tempfile.TemporaryDirectory() as made:
            folder = folder or made
            if descriptor_pid is not None:
                (Path(folder) / 'relay.json').write_text(json.dumps({'pid': descriptor_pid}))
            with mock.patch.object(w.subprocess, 'Popen', return_value=process) as popen, mock.patch.object(w.time, 'sleep'), \
                    mock.patch.object(w, 'end_process_tree', return_value=ended) as end, mock.patch.object(w, 'pid_alive', return_value=alive):
                result = w.start_standalone_relay(folder, wait=wait, probe=probe)
            return result, popen, end, sorted(p.name for p in Path(folder).glob('start-unconfirmed.json'))

    def test_success_uses_the_standalone_flag_and_a_hidden_detached_process(self):
        (ok, detail), popen, end, _ = self.start(self.child([None]), lambda root: True, descriptor_pid=5)
        self.assertTrue(ok)
        self.assertIn('pid 5', detail)
        command = popen.call_args.args[0]
        self.assertIn('--standalone', command)
        self.assertIn('scripts.team_room.desktop_relay', command)
        if os.name == 'nt':
            self.assertTrue(popen.call_args.kwargs['creationflags'] & 0x08000000)  # CREATE_NO_WINDOW
        end.assert_not_called()

    def test_when_another_relay_answers_the_start_is_ended_and_its_fate_is_confirmed(self):
        (ok, detail), _, end, blockers = self.start(self.child([None, None]), lambda root: True, descriptor_pid=99)
        self.assertTrue(ok)
        self.assertIn('another relay (pid 99)', detail)
        end.assert_called_once()
        self.assertEqual(blockers, [])

    def test_an_unconfirmed_end_is_reported_and_blocks_the_next_start(self):
        with tempfile.TemporaryDirectory() as folder:
            (ok, detail), _, end, blockers = self.start(self.child([None] * 50), lambda root: False, wait=0.0, ended=False, folder=folder)
            self.assertFalse(ok)
            self.assertIn('could not be ended', detail)
            self.assertEqual(blockers, ['start-unconfirmed.json'])
            # the next pass: the earlier start still lives => no second starter
            process = self.child([None])
            with mock.patch.object(w.subprocess, 'Popen') as popen, mock.patch.object(w, 'pid_alive', return_value=True):
                ok, detail = w.start_standalone_relay(folder, wait=1, probe=lambda root: True)
            self.assertFalse(ok)
            self.assertIn('earlier start', detail)
            popen.assert_not_called()
            # once it is gone the block is lifted
            with mock.patch.object(w.subprocess, 'Popen', return_value=process), mock.patch.object(w, 'pid_alive', return_value=False):
                ok, _ = w.start_standalone_relay(folder, wait=1, probe=lambda root: True)
            self.assertTrue(ok)
            self.assertFalse((Path(folder) / 'start-unconfirmed.json').exists())

    def test_an_early_exit_is_a_failure_even_with_code_zero(self):
        for code in (7, 0):
            with self.subTest(code):
                (ok, detail), _, end, _ = self.start(self.child([code]), lambda root: False)
                self.assertFalse(ok)
                self.assertIn('exited with code %d before it answered' % code, detail)

    def test_a_timed_out_start_is_ended(self):
        (ok, detail), _, end, blockers = self.start(self.child([None] * 50), lambda root: False, wait=0.0)
        self.assertFalse(ok)
        self.assertIn('did not answer', detail)
        end.assert_called_once()
        self.assertEqual(blockers, [])


class SupervisorTests(unittest.TestCase):
    def test_relay_manager_changed(self):
        with tempfile.TemporaryDirectory() as folder:
            relay = Path(folder) / 'relay'
            self.assertFalse(w.relay_manager_changed(relay, folder))  # no descriptor
            relay.mkdir()
            (relay / 'relay.json').write_text(json.dumps({'origin': 'old', 'pid': 1}))
            with mock.patch.object(w, 'current_manager', return_value='new'):
                self.assertTrue(w.relay_manager_changed(relay, folder))
            with mock.patch.object(w, 'current_manager', return_value='old'):
                self.assertFalse(w.relay_manager_changed(relay, folder))
            with mock.patch.object(w, 'current_manager', return_value=None):
                self.assertFalse(w.relay_manager_changed(relay, folder))

    def test_an_unended_standalone_start_blocks_the_manager_fallback_too(self):
        with tempfile.TemporaryDirectory() as folder:
            record = Path(folder) / 'start-unconfirmed.json'
            def failing_start(root, **kw):
                record.write_text(json.dumps({'pid': 4242}))
                return False, 'relay did not answer within 75 s; the start (pid 4242) could not be ended'
            env = {'CODEX_THREAD_ID': 'x', 'CODEX_APP_TOOLS_PIPE_PATH': 'y'}
            with mock.patch.object(w, 'desktop_running', return_value=True), mock.patch.object(w, 'pid_alive', return_value=True), \
                    mock.patch.object(w, 'start_standalone_relay', side_effect=failing_start) as start, \
                    mock.patch.object(w, 'run_starter', return_value=(True, 'x')) as fallback, mock.patch.dict(os.environ, env):
                ok, detail = w.restart_relay(folder)           # first pass: standalone fails and leaves a child
                self.assertFalse(ok)
                self.assertIn('could not be ended', detail)
                fallback.assert_not_called()
                ok, detail = w.restart_relay(folder)           # next pass: neither launch path may run
                self.assertFalse(ok)
                self.assertIn('earlier start', detail)
                start.assert_called_once()
                fallback.assert_not_called()
            with mock.patch.object(w, 'desktop_running', return_value=True), mock.patch.object(w, 'pid_alive', return_value=False), \
                    mock.patch.object(w, 'start_standalone_relay', return_value=(True, 'ok')) as start:
                self.assertEqual(w.restart_relay(folder), (True, 'ok'))  # the child is gone: the block is lifted
            self.assertFalse(record.exists())

    def test_restart_waits_for_desktop_instead_of_starting_a_relay(self):
        with mock.patch.object(w, 'desktop_running', return_value=False), \
                mock.patch.object(w, 'start_standalone_relay') as start:
            ok, detail = w.restart_relay('unused')
        self.assertFalse(ok)
        self.assertIn('not running', detail)
        start.assert_not_called()

    def test_restart_keeps_the_previous_relay_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'stdout.log').write_text('current')
            for number in range(7):
                old = root / f'stdout.2026010{number}T000000.log'
                old.write_text('old')
                os.utime(old, (1000 + number, 1000 + number))
            w.rotate_relay_logs(root, keep=5)
            self.assertFalse((root / 'stdout.log').exists())
            kept = list(root.glob('stdout.*.log'))
            self.assertEqual(len(kept), 5)
            self.assertTrue(any(p.read_text() == 'current' for p in kept))


class MainPassTests(unittest.TestCase):
    """One supervisor pass with every process and network call replaced; each test states the world and the expected verdict."""

    def run_main(self, relay=(False,), desktop=True, office=True, manager_changed=(False,), restart=(True, 'started'), stopped=True,
                 report=None, extra=()):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            patch = lambda name, **kw: stack.enter_context(mock.patch.object(w, name, **kw))
            patch('relay_ok', side_effect=list(relay) + [relay[-1]] * 5)
            patch('desktop_running', return_value=desktop)
            patch('office_ok', return_value=office)
            patch('relay_manager_changed', side_effect=list(manager_changed) + [manager_changed[-1]] * 5)
            patch('registered_projects', return_value=['P'] if report else [])
            patch('projects_missing_outbox', return_value=[])
            incident = patch('record_incident')
            stop = patch('stop_relay', return_value=stopped)
            starter = patch('restart_relay', return_value=restart)
            office_starter = patch('restart_office', return_value=(True, 'started'))
            patch('project_report', return_value=report)
            args = ['--office-root', folder, '--health-file', str(Path(folder) / 'h.json'), '--office-url', 'http://127.0.0.1:1',
                    '--restart', *(['--all-projects'] if report else []), *extra]
            code = w.main(args)
            data = json.loads((Path(folder) / 'h.json').read_text(encoding='utf-8'))
        return SimpleNamespace(code=code, data=data, incident=incident, stop=stop, starter=starter, office_starter=office_starter)

    def test_closed_desktop_without_waiting_reports_is_quiet_even_without_office(self):
        run = self.run_main(relay=(False,), desktop=False, office=False, restart=(False, 'Codex Desktop is not running'))
        self.assertEqual((run.code, run.data['problems']), (0, []))
        run.incident.assert_not_called()
        self.assertFalse(run.data['office_ok'])  # still visible in the report

    def test_closed_desktop_with_waiting_reports_is_one_clear_problem(self):
        run = self.run_main(relay=(False,), desktop=False, restart=(False, 'x'), report={'project': 'P', 'outbox': {'pending': 2, 'problems': []}})
        self.assertEqual(run.code, 1)
        self.assertEqual(len(run.data['problems']), 1)
        self.assertIn('Desktop is not running', run.data['problems'][0])

    def test_office_down_is_a_problem_only_when_office_is_expected(self):
        self.assertEqual(self.run_main(relay=(True,), office=False).code, 0)
        run = self.run_main(relay=(True,), office=False, extra=('--restart-office',))
        run.office_starter.assert_called_once()  # a launcher is stubbed, never the real one
        self.assertEqual(run.code, 1)
        self.assertIn('Codex Office does not answer', run.data['problems'][0])

    def test_a_down_relay_is_restarted_without_the_manager_chat(self):
        run = self.run_main(relay=(False, True))
        self.assertEqual((run.code, run.data['problems']), (0, []))
        run.starter.assert_called_once()
        run.incident.assert_called_once()
        self.assertTrue(run.data['restart']['ok'])

    def test_a_relay_that_cannot_be_restarted_is_a_problem_with_the_reason(self):
        run = self.run_main(relay=(False,), restart=(False, 'pipe refused'))
        self.assertEqual(run.code, 1)
        self.assertIn('pipe refused', run.data['problems'][0])

    def test_a_replaced_manager_chat_restarts_the_relay_and_is_confirmed(self):
        run = self.run_main(relay=(True, False, True), manager_changed=(True, False))
        self.assertEqual((run.code, run.data['problems']), (0, []))
        run.stop.assert_called_once()
        run.starter.assert_called_once()
        self.assertTrue(run.data['manager_changed']['relay_stopped'])

    def test_a_relay_that_will_not_stop_is_reported_and_no_second_relay_is_started(self):
        run = self.run_main(relay=(True,), manager_changed=(True,), stopped=False)
        self.assertEqual(run.code, 1)
        run.starter.assert_not_called()
        self.assertIn('replaced Manager chat and did not stop in time', run.data['problems'][0])

    def test_a_restart_that_still_serves_the_old_manager_is_not_healthy(self):
        run = self.run_main(relay=(True, False, True), manager_changed=(True, True))
        self.assertEqual(run.code, 1)
        self.assertIn('even after a restart', run.data['problems'][0])


if __name__ == '__main__':
    unittest.main()
