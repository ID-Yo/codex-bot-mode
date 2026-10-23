import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import monitor_checkpoint
import monitor_run
import task_continuity as tc

NOW = datetime(2026, 10, 23, 19, 0, tzinfo=timezone.utc)


class Fixture:
    def __init__(self, test):
        self.dir = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.monitor = self.dir / "monitor"
        (self.monitor / "agent").mkdir(parents=True)
        (self.monitor / "agent" / "STATE.md").write_text("Narrative history.\n", encoding="utf-8")
        self.sessions = self.dir / "sessions"
        self.sessions.mkdir()
        self.projects = []

    def project(self, pid, tasks=False, exists=True):
        root = self.dir / pid
        if exists:
            root.mkdir()
        if exists and tasks:
            (root / "work" / "tasks").mkdir(parents=True)
            (root / "work" / "tasks" / "T-1.md").write_text(
                "- Task ID: T-1\n- State: backlog\n- Responsible Manager: manager\n", encoding="utf-8")
            (root / ".bot-mode").mkdir()
            ledger = ROOT / ".bot-mode" / "requests.py"
            shutil.copy(ledger if ledger.exists() else ROOT / "scripts" / "team_room" / "requests.py",
                        root / ".bot-mode" / "requests.py")
        self.projects.append({"id": pid, "name": pid, "root": str(root)})
        return root

    def write(self, registry=None):
        registry_path = self.dir / "registry.json"
        registry_path.write_text(json.dumps(registry or {"schema": 1, "projects": self.projects, "standalone": []}), encoding="utf-8")
        (self.monitor / "agent" / "monitoring.json").write_text(json.dumps({
            "report_target_thread_id": "thread-cos", "registry_path": str(registry_path), "interval_minutes": 20}), encoding="utf-8")

    def run(self, run_id="run-1", now=NOW):
        return monitor_run.run(self.monitor, run_id, now=now, sessions_root=self.sessions)

    def checkpoint(self, name="STATE.md"):
        return monitor_checkpoint.load_checkpoint(self.monitor / "agent" / name)


class MonitorRunTests(unittest.TestCase):
    def test_complete_quiet_cycle_builds_snapshot_and_records_checkpoint(self):
        fx = Fixture(self)
        fx.project("alpha", tasks=True)
        fx.project("beta")
        fx.write()
        result = fx.run(now=datetime.now(timezone.utc))
        self.assertTrue(result["audit_complete"], result)
        self.assertEqual(result["findings"], [])
        self.assertEqual((result["coverage"], result["projects"], result["projects_covered"], result["tasks"]), ("complete", 2, 2, 1))
        self.assertFalse(result["report_required"])
        self.assertIsNone(result["report_text"])
        self.assertTrue(Path(result["snapshot_path"]).exists())
        self.assertEqual(json.loads(Path(result["snapshot_path"]).read_text())["tasks"][0]["id"], "alpha/T-1")
        saved = fx.checkpoint()
        self.assertEqual((saved["run_id"], saved["audit_status"], saved["delivery_status"]), ("run-1", "complete", "not_required"))
        self.assertIn("Narrative history.", (fx.monitor / "agent" / "STATE.md").read_text(encoding="utf-8"))

    def test_unavailable_project_makes_incomplete_report_with_english_text(self):
        fx = Fixture(self)
        fx.project("alpha")
        fx.project("gone", exists=False)
        fx.write()
        result = fx.run()
        self.assertFalse(result["audit_complete"])
        self.assertEqual(result["coverage"], "partial")
        self.assertTrue(any(s.startswith("project:gone") for s in result["unavailable_scopes"]))
        self.assertTrue(result["report_required"])
        self.assertEqual(result["report_thread_id"], "thread-cos")
        self.assertIn("Agent Monitor report for run run-1", result["report_text"])
        self.assertIn("INCOMPLETE", result["report_text"])
        self.assertEqual(fx.checkpoint()["delivery_status"], "not_attempted")
        self.assertEqual(fx.checkpoint()["audit_status"], "incomplete")

    def test_second_fresh_active_audit_uses_private_state_and_leaves_shared_state(self):
        fx = Fixture(self)
        fx.project("alpha")
        fx.write()
        local = datetime.now()
        folder = fx.sessions / f"{local:%Y}" / f"{local:%m}" / f"{local:%d}"
        folder.mkdir(parents=True)
        other = folder / "rollout-2026-10-23T20-00-00-other-thread.jsonl"
        other.write_text('{"text":"Automation: Agent Monitor"}\n', encoding="utf-8")
        before = (fx.monitor / "agent" / "STATE.md").read_bytes()
        result = fx.run(now=datetime.now(timezone.utc))
        self.assertTrue(result["private_state"])
        self.assertIn("concurrent_audit", {f["code"] for f in result["findings"]})
        self.assertTrue(result["report_required"])
        self.assertEqual((fx.monitor / "agent" / "STATE.md").read_bytes(), before)
        self.assertEqual(fx.checkpoint("run-run-1-state.md")["run_id"], "run-1")

    def test_missing_sessions_source_is_unavailable_not_healthy(self):
        fx = Fixture(self)
        fx.project("alpha")
        fx.write()
        shutil.rmtree(fx.sessions)
        result = fx.run()
        self.assertFalse(result["audit_complete"])
        self.assertIn("invocation-metadata", result["unavailable_scopes"])

    def test_build_failure_still_records_this_run_as_incomplete_and_reports(self):
        fx = Fixture(self)
        fx.write()
        (fx.dir / "registry.json").write_text("not json", encoding="utf-8")
        result = fx.run("run-bad")
        self.assertFalse(result["audit_complete"])
        self.assertTrue(result["report_required"])
        self.assertIsNone(result["snapshot_path"])
        self.assertIn("snapshot_build_failed", {f["code"] for f in result["findings"]})
        saved = fx.checkpoint()
        self.assertEqual((saved["run_id"], saved["audit_status"], saved["delivery_status"]), ("run-bad", "incomplete", "not_attempted"))

    def test_cli_prints_compact_json_only(self):
        fx = Fixture(self)
        fx.project("alpha")
        fx.write()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = monitor_run.main(["--run-id", "run-cli", "--monitor-dir", str(fx.monitor), "--sessions-root", str(fx.sessions)])
        payload = json.loads(out.getvalue())
        self.assertIn(code, (0, 1))
        self.assertEqual(payload["run_id"], "run-cli")
        self.assertLess(len(out.getvalue()), 3000)


class InvalidSnapshotCheckpointTests(unittest.TestCase):
    def test_monitor_audit_cli_records_run_id_when_snapshot_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "STATE.md"
            monitor_checkpoint.save_checkpoint(state, {"version": 1, "run_id": "bootstrap-cbm227", "audit_status": "unknown",
                                                       "coverage": "none", "observed_at": "x", "next_check_at": "x",
                                                       "active_alert_count": 0, "delivery_status": "unconfirmed",
                                                       "unavailable_scopes": [], "alert_ledger": []})
            code = tc.main(["monitor-audit", "--file", str(Path(temp) / "missing.json"), "--state-file", str(state), "--run-id", "run-now"])
            saved = monitor_checkpoint.load_checkpoint(state)
            self.assertEqual(code, 1)
            self.assertEqual((saved["run_id"], saved["audit_status"]), ("run-now", "incomplete"))
            self.assertIn("audit_incomplete:monitor", saved["alert_ledger"])

    def test_delivery_status_accepts_not_attempted(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "STATE.md"
            monitor_checkpoint.save_checkpoint(state, {"version": 1, "delivery_status": "unconfirmed"})
            monitor_checkpoint.set_delivery_status(state, "not_attempted")
            self.assertEqual(monitor_checkpoint.load_checkpoint(state)["delivery_status"], "not_attempted")


if __name__ == "__main__":
    unittest.main()
