"""The supervisor runs the zero-token canary of every project and turns its failure into one incident (#426, S6-C)."""
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.team_room import incidents as inc
from scripts.team_room import supervisor_status

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("watchdog426c", ROOT / "scripts/relay_watchdog.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

PROBLEM = "outbox canary has waited 6 min without being processed (last error: HTTP Error 503: Service Unavailable (thread not found)): check the relay, the pump and the Manager chat (outbox.py status)"
STUCK = "alpha: " + PROBLEM
GOOD = {"queued": False, "pending_seconds": None, "last_ok_at": "2026-11-23T12:00:00+00:00", "last_latency_seconds": 1.5,
        "last_read_seconds": 0.2, "last_chat_status": "idle", "last_error": None, "problems": []}
BAD = {**GOOD, "pending_seconds": 400, "problems": [PROBLEM]}


class Classification(unittest.TestCase):
    def test_a_stuck_canary_is_one_manager_incident_per_project_with_a_number_free_key(self):
        first = inc.classify(STUCK, ["alpha", "beta"])
        later = inc.classify(STUCK.replace("6 min", "47 min").replace("503", "502"), ["alpha", "beta"])
        other = inc.classify(STUCK.replace("alpha", "beta"), ["alpha", "beta"])
        self.assertEqual((first["key"], first["severity"]), ("outbox-canary:alpha", inc.MANAGER))
        self.assertEqual(later["key"], first["key"])
        self.assertEqual(other["key"], "outbox-canary:beta")

    def test_a_stuck_canary_never_asks_for_a_windows_message(self):
        state = inc.empty()
        stamp = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
        for minutes in (0, 1, 15, 40):
            events, actions = inc.step(state, [inc.classify(STUCK, ["alpha"])], stamp.replace(minute=0) if minutes == 0 else stamp.replace(hour=12 + minutes // 60, minute=minutes % 60))
            self.assertNotIn("popup", [a["do"] for a in actions])


class CanaryCommandRunner(unittest.TestCase):
    def run_with(self, **result):
        with mock.patch.object(w.subprocess, "run", **result) as run:
            return w.outbox_canary("X:/alpha", 10.0, 5.0), run

    def test_the_report_of_the_command_is_returned_and_the_arguments_are_passed(self):
        report, run = self.run_with(return_value=mock.Mock(stdout="noise\n" + json.dumps(BAD) + "\n", stderr="", returncode=5))
        self.assertEqual(report["problems"], BAD["problems"])
        command = run.call_args[0][0]
        self.assertEqual(command[-4:], ["--every-minutes", "10.0", "--limit-minutes", "5.0"])
        self.assertIn("canary", command)

    def test_an_outbox_without_the_command_or_a_silent_one_is_unknown_not_a_problem(self):
        for result in (dict(return_value=mock.Mock(stdout="", stderr="usage: outbox.py ... invalid choice: 'canary'", returncode=2)),
                       dict(return_value=mock.Mock(stdout="not json", stderr="", returncode=0)),
                       dict(return_value=mock.Mock(stdout='{"no": "problems"}', stderr="", returncode=0)),
                       dict(side_effect=subprocess.TimeoutExpired("outbox.py", 60)),
                       dict(side_effect=OSError("gone"))):
            self.assertIsNone(self.run_with(**result)[0])


class SupervisorPass(unittest.TestCase):
    """A real supervisor pass with every process replaced; the canary report is what the project's outbox.py would print."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.office = Path(self.tmp.name) / "office"
        self.project = Path(self.tmp.name) / "alpha"
        for folder in (self.office / ".bot-mode", self.project / ".bot-mode"):
            folder.mkdir(parents=True)
        (self.project / ".bot-mode" / "outbox.py").write_bytes((ROOT / "scripts" / "team_room" / "outbox.py").read_bytes())  # no drift
        (self.project / ".bot-mode" / "roster.json").write_text(
            json.dumps({"participants": [{"bot": "manager", "role": "Manager", "thread_id": "thread-manager-1"}]}), encoding="utf-8")

    def run_pass(self, canary, relay_ok=True, extra=(), runs=1):
        status = {"pending": 0, "relay_reachable": relay_ok, "pump_alive": True, "problems": []}
        with ExitStack() as stack:
            values = {"relay_ok": relay_ok, "desktop_running": True, "office_ok": True, "relay_manager_changed": False,
                      "registered_projects": [], "projects_missing_outbox": [], "restart_relay": (False, "pipe refused"), "record_incident": None}
            for name, value in values.items():
                stack.enter_context(mock.patch.object(w, name, return_value=value))
            stack.enter_context(mock.patch.object(w, "outbox_status", return_value=status))
            self.canary = stack.enter_context(mock.patch.object(w, "outbox_canary", return_value=canary))
            self.popup = stack.enter_context(mock.patch.object(w, "show_owner_message"))
            argv = ["--office-root", str(self.office), "--health-file", str(self.office / ".bot-mode" / "relay-health.json"),
                    "--office-url", "http://127.0.0.1:1", "--project", str(self.project), "--notify", *extra]
            return [w.main(argv) for _ in range(runs)]

    def health(self):
        return json.loads((self.office / ".bot-mode" / "relay-health.json").read_text(encoding="utf-8"))

    def status_file(self):
        return json.loads((self.office / ".bot-mode" / "supervisor-status.json").read_text(encoding="utf-8"))

    def canary_incidents(self):
        return [i for i in self.health()["incidents"] if i["key"].startswith("outbox-canary")]

    def manager_rows(self):
        conn = sqlite3.connect(str(self.project / ".bot-mode" / "outbox" / "queue.sqlite"))
        try:
            return [r[0] for r in conn.execute("SELECT text FROM outbox")]
        finally:
            conn.close()

    def test_a_stuck_canary_is_one_incident_one_manager_row_and_no_popup_however_many_passes(self):
        codes = self.run_pass(BAD, runs=4)
        self.assertEqual(set(codes), {1})
        self.assertEqual([i["key"] for i in self.canary_incidents()], ["outbox-canary:alpha"])
        self.assertEqual(len([row for row in self.manager_rows() if "outbox-canary" in row]), 1)
        self.popup.assert_not_called()  # Manager severity: no Windows message, so no repeating windows
        self.assertTrue(any("outbox canary has waited" in problem for problem in self.status_file()["problems"]))

    def test_a_dead_relay_is_one_incident_of_its_own_and_the_canary_adds_none(self):
        self.run_pass(BAD, relay_ok=False, extra=["--restart"])
        self.assertEqual(self.canary_incidents(), [])
        self.assertIn("relay-down", [i["key"] for i in self.health()["incidents"]])

    def test_a_good_canary_is_recorded_in_the_status_file_with_its_latency_and_raises_nothing(self):
        self.assertEqual(self.run_pass(GOOD), [0])
        self.assertEqual(self.canary_incidents(), [])
        entry = self.status_file()["projects"][0]
        self.assertEqual(entry["canary"], {"last_ok_at": GOOD["last_ok_at"], "last_latency_seconds": 1.5, "last_read_seconds": 0.2,
                                           "last_chat_status": "idle", "pending_seconds": None})

    def test_an_outbox_without_the_canary_is_unknown_in_the_status_file_and_not_a_problem(self):
        self.run_pass(None)
        self.assertEqual(self.canary_incidents(), [])
        self.assertIsNone(self.status_file()["projects"][0]["canary"])

    def test_the_canary_can_be_switched_off(self):
        self.run_pass(GOOD, extra=["--canary-every-minutes", "0"])
        self.canary.assert_not_called()
        self.assertIsNone(self.status_file()["projects"][0]["canary"])

    def test_the_interval_and_the_limit_reach_the_command(self):
        self.run_pass(GOOD, extra=["--canary-every-minutes", "3", "--canary-limit-minutes", "2"])
        self.canary.assert_called_once_with(str(self.project), 3.0, 2.0)

    def test_damaged_canary_facts_in_a_report_become_unknown_values(self):
        self.run_pass({"problems": [], "last_latency_seconds": "fast", "last_ok_at": 5, "pending_seconds": True})
        entry = self.status_file()["projects"][0]["canary"]
        self.assertIsNone(entry["last_latency_seconds"])
        self.assertIsNone(entry["pending_seconds"])


if __name__ == "__main__":
    unittest.main()

