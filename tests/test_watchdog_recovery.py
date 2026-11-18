import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('watchdog', Path(__file__).parents[1] / 'scripts/relay_watchdog.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

class WatchdogTests(unittest.TestCase):
    def test_incident_is_recorded_once_per_dead_relay_with_last_proof_of_life(self):
        with tempfile.TemporaryDirectory() as folder:
            relay = Path(folder) / '.bot-mode/desktop-relay'
            relay.mkdir(parents=True)
            (relay / 'relay.json').write_text('{"pid": 4242, "bridge_pid": 4243}')
            (relay / 'relay-alive.json').write_text('{"at": "2026-11-14T17:00:00Z", "parent_alive": false}')
            (relay / 'stdout.log').write_text('started\n')
            with mock.patch.object(w, 'pid_alive', return_value=False), \
                    mock.patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
                first = w.record_incident(relay)
                second = w.record_incident(relay)
            self.assertIsNone(second)
            self.assertEqual(first['relay_pid'], 4242)
            self.assertFalse(first['parent_alive_at_last_proof'])
            self.assertEqual(first['last_proof_of_life']['at'], '2026-11-14T17:00:00Z')
            self.assertEqual(len((relay.parent / 'relay-incidents.jsonl').read_text().splitlines()), 1)

    def test_projects_missing_outbox_lists_roots_with_requests_but_no_outbox(self):
        with tempfile.TemporaryDirectory() as folder:
            office = Path(folder) / 'office'
            complete, partial, empty = (Path(folder) / n for n in ('complete', 'partial', 'empty'))
            for root, files in ((complete, ('requests.py', 'outbox.py')), (partial, ('requests.py',)), (empty, ())):
                (root / '.bot-mode').mkdir(parents=True)
                for name in files:
                    (root / '.bot-mode' / name).write_text('# installed')
            registry = office / '.bot-mode/local/office-project-roots.json'
            registry.parent.mkdir(parents=True)
            registry.write_text('{"projects":[%s]}' % ','.join('{"id":"%s","root":"%s"}' % (r.name, r.as_posix())
                                                                 for r in (complete, partial, empty)))
            self.assertEqual(w.projects_missing_outbox(office), [str(partial)])

    def test_registered_projects_lists_only_roots_with_an_outbox(self):
        with tempfile.TemporaryDirectory() as folder:
            office, with_outbox, without = (Path(folder) / n for n in ('office', 'a', 'b'))
            (with_outbox / '.bot-mode').mkdir(parents=True)
            (with_outbox / '.bot-mode/outbox.py').write_text('# installed')
            without.mkdir()
            registry = office / '.bot-mode/local/office-project-roots.json'
            registry.parent.mkdir(parents=True)
            registry.write_text('{"projects":[{"id":"a","root":"%s"},{"id":"b","root":"%s"}]}' % (
                with_outbox.as_posix(), without.as_posix()))
            self.assertEqual(w.registered_projects(office), [str(with_outbox)])
            self.assertEqual(w.registered_projects(Path(folder) / 'missing'), [])

    def test_probe_timeout_is_unhealthy_instead_of_crashing(self):
        with mock.patch.object(w.subprocess, 'run', side_effect=subprocess.TimeoutExpired('probe', 60)):
            self.assertFalse(w.relay_ok('unused'))

    def test_project_uses_installed_runtime_not_worktree_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / '.bot-mode/outbox.py'
            script.parent.mkdir()
            script.write_text('# installed runtime')
            result = subprocess.CompletedProcess([], 0, '{"pending":0,"problems":[]}', '')
            with mock.patch.object(w.subprocess, 'run', return_value=result) as run:
                w.project_report(root)
            self.assertEqual(Path(run.call_args.args[0][2]), script)

    def test_busy_manager_suppresses_only_age_warning(self):
        status = {'pending': 1, 'pump_alive': True, 'problems': ['oldest pending row is 900 s old although relay and pump are up; check the Manager thread', 'unconfirmed delivery requires reconciliation']}
        with mock.patch.object(w, 'manager_busy', return_value=True):
            w.classify_wait('unused', status)
        self.assertEqual(status['waiting_reason'], 'manager busy')
        self.assertEqual(status['problems'], ['unconfirmed delivery requires reconciliation'])

    def test_supervisor_continues_after_failed_check(self):
        with mock.patch.object(w, 'main', side_effect=[RuntimeError('temporary'), 0]) as run, mock.patch.object(w.time, 'sleep', side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                w.supervise([], interval=600)
        self.assertEqual(run.call_count, 2)

    def test_starter_returns_while_its_child_keeps_running(self):
        import sys, time
        child = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(25)']);print('started')"
        began = time.time()
        ok, detail = w.run_starter([sys.executable, '-c', child], timeout=20)
        self.assertTrue(ok)
        self.assertIn('started', detail)
        self.assertLess(time.time() - began, 15)
