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
