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


def stamp(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class Fixture:
    def __init__(self, test):
        self.dir = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.monitor = self.dir / "monitor"
        (self.monitor / "agent").mkdir(parents=True)
        (self.monitor / "agent" / "STATE.md").write_text("Narrative history.\n", encoding="utf-8")
        self.projects = []

    def project(self, pid, tasks=False, exists=True, tracked_work=None):
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
        self.projects.append({"id": pid, "name": pid, "root": str(root), **({"tracked_work": tracked_work} if tracked_work else {})})
        return root

    def write(self, registry=None):
        registry_path = self.dir / "registry.json"
        registry_path.write_text(json.dumps(registry or {"schema": 1, "projects": self.projects, "standalone": []}), encoding="utf-8")
        (self.monitor / "agent" / "monitoring.json").write_text(json.dumps({
            "report_target_thread_id": "thread-cos", "registry_path": str(registry_path), "interval_minutes": 20}), encoding="utf-8")

    def run(self, run_id="run-1", now=NOW):
        return monitor_run.run(self.monitor, run_id, now=now)

    def checkpoint(self, name="STATE.md"):
        return monitor_checkpoint.load_checkpoint(self.monitor / "agent" / name)

    def lease(self, run_id="other", status="active", age=5, now=NOW, raw=None):
        runs = self.monitor / "agent" / "runs"
        runs.mkdir(exist_ok=True)
        text = raw if raw is not None else json.dumps({"run_id": run_id, "status": status, "updated_at": stamp(now - timedelta(seconds=age))})
        (runs / f"{run_id}.lease.json").write_text(text, encoding="utf-8")


class MonitorRunTests(unittest.TestCase):
    def test_complete_quiet_cycle_builds_snapshot_and_records_checkpoint(self):
        fx = Fixture(self)
        fx.project("alpha", tasks=True)
        fx.project("beta", tracked_work="none")
        fx.write()
        result = fx.run(now=datetime.now(timezone.utc))
        self.assertTrue(result["audit_complete"], result)
        self.assertEqual(result["findings"], [])
        self.assertEqual((result["coverage"], result["projects"], result["projects_covered"], result["tasks"]), ("complete", 2, 2, 1))
        self.assertFalse(result["report_required"])
        self.assertIsNone(result["report_text"])
        self.assertEqual(json.loads(Path(result["snapshot_path"]).read_text())["tasks"][0]["id"], "alpha/T-1")
        saved = fx.checkpoint()
        self.assertEqual((saved["run_id"], saved["audit_status"], saved["delivery_status"]), ("run-1", "complete", "not_required"))
        self.assertIn("Narrative history.", (fx.monitor / "agent" / "STATE.md").read_text(encoding="utf-8"))
        lease = json.loads((fx.monitor / "agent" / "runs" / "run-1.lease.json").read_text())
        self.assertEqual(lease["status"], "finished")

    def test_unavailable_project_makes_incomplete_report_with_english_text(self):
        fx = Fixture(self)
        fx.project("alpha", tracked_work="none")
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

    def test_repeated_invocation_of_the_same_run_still_reports_undelivered_signals(self):
        # Live run 01a16cec: the first invocation wrote the ledger, a second one (same run id, 36 s later)
        # saw every signal as already known, so nothing reached the recipient.
        fx = Fixture(self)
        root = fx.project("alpha", tracked_work="none")
        (root / ".bot-mode" / "local").mkdir(parents=True)
        (root / ".bot-mode" / "local" / "requests.json").write_text(json.dumps({"schema": 1, "requests": [{
            "id": "overdue-1", "from": "manager", "to": "developer", "summary": "s", "link": "codex://threads/x",
            "opened": "2026-10-20T10:00:00+00:00", "due": "2026-10-20T11:00:00+00:00", "state": "open", "escalations": []}]}), encoding="utf-8")
        del fx.projects[0]["tracked_work"]
        fx.write()
        first = fx.run("run-x", now=datetime.now(timezone.utc))
        self.assertTrue(first["report_required"], first)
        second = fx.run("run-x", now=datetime.now(timezone.utc))
        self.assertTrue(second["report_required"])
        self.assertGreater(second["new_alerts"], 0)
        statuses = {e["status"] for e in fx.checkpoint()["alert_ledger"].values()}
        self.assertEqual(statuses, {"new"})
        monitor_checkpoint.set_delivery_status(fx.monitor / "agent" / "STATE.md", "sent")
        third = fx.run("run-y", now=datetime.now(timezone.utc))
        self.assertEqual(third["new_alerts"], 0)
        self.assertFalse(third["report_required"])

    def test_real_checkpoint_of_run_01a16cec_gives_new_alerts_not_re_escalations(self):
        fixture = json.loads((ROOT / "tests" / "fixtures" / "cbm259-run-01a16cec-checkpoint.json").read_text(encoding="utf-8"))
        ledger = fixture["alert_ledger"]
        self.assertEqual(len(ledger), 38)
        self.assertIn("re_escalated", {e["status"] for e in ledger.values()})
        state = Path(tempfile.mkdtemp()) / "STATE.md"
        self.addCleanup(shutil.rmtree, state.parent, True)
        monitor_checkpoint.save_checkpoint(state, {"version": 1, "run_id": fixture["run_id"], "delivery_status": "not_required",
                                                   "alert_ledger": ledger})
        findings = [{"code": fp.split(":", 1)[0], "scope": fp.split(":", 1)[1], "detail": "d"} for fp in ledger]
        snapshot = {"monitor_audit": {"run_id": fixture["run_id"], "coverage": "complete", "observed_at": stamp(NOW),
                                      "next_check_at": stamp(NOW + timedelta(minutes=30)), "unavailable_scopes": []}}
        _, new_alerts = tc.update_monitor_state(state, findings, snapshot, NOW, complete=True, run_id=fixture["run_id"])
        self.assertEqual(len(new_alerts), 38)
        self.assertEqual({e["status"] for e in monitor_checkpoint.load_checkpoint(state)["alert_ledger"].values()}, {"new"})

    def test_record_whose_github_issue_is_closed_is_not_an_alert_and_is_noted_once_per_project(self):
        fx = Fixture(self)
        root = fx.project("alpha", tasks=True)
        (root / ".bot-mode" / "local").mkdir(parents=True, exist_ok=True)
        (root / ".bot-mode" / "local" / "requests.json").write_text(json.dumps({"schema": 1, "requests": [{
            "id": "ODD-RC-PREFLIGHT-20261023", "from": "chief-of-staff", "to": "manager", "summary": "s",
            "link": "codex://threads/x", "opened": "2026-10-23T14:20:55+00:00", "due": "2026-10-23T17:20:55+00:00",
            "state": "open", "escalations": []}]}), encoding="utf-8")
        registry = json.loads((fx.dir / "registry.json").read_text()) if (fx.dir / "registry.json").exists() else None
        fx.projects[0]["repository"] = "o/alpha"
        fx.write()
        import status_sources
        original = status_sources.github_fetch
        status_sources.github_fetch = lambda repo: {"issue": [{"number": 157, "state": "CLOSED", "title": "ODD-RC-PREFLIGHT-20261023: preparation",
                                                               "updatedAt": "2026-10-24T05:00:00Z", "url": "https://github.com/o/alpha/issues/157", "labels": []}], "pr": []}
        self.addCleanup(setattr, status_sources, "github_fetch", original)
        result = fx.run("run-h", now=datetime.now(timezone.utc))
        codes = {(f["code"], f["scope"]) for f in result["findings"]}
        self.assertNotIn(("task_overdue", "alpha/issue-157"), codes)
        self.assertFalse(any(c == "task_overdue" and "ODD-RC" in s for c, s in codes))
        self.assertIn(("record_not_closed", "alpha/ODD-RC-PREFLIGHT-20261023"), codes)
        self.assertIn("Hygiene (reported once)", result["report_text"])
        self.assertIn("alpha 1", result["report_text"])
        monitor_checkpoint.set_delivery_status(fx.monitor / "agent" / "STATE.md", "sent")
        again = fx.run("run-h2", now=datetime.now(timezone.utc))
        self.assertEqual(again["new_alerts"], 0)

    def test_second_fresh_lease_uses_private_state_and_leaves_shared_state(self):
        fx = Fixture(self)
        fx.project("alpha", tracked_work="none")
        fx.write()
        fx.lease()
        before = (fx.monitor / "agent" / "STATE.md").read_bytes()
        result = fx.run()
        self.assertTrue(result["private_state"])
        self.assertIn("concurrent_audit", {f["code"] for f in result["findings"]})
        self.assertTrue(result["report_required"])
        self.assertEqual((fx.monitor / "agent" / "STATE.md").read_bytes(), before)
        self.assertEqual(fx.checkpoint("run-run-1-state.md")["run_id"], "run-1")

    def test_build_failure_with_fresh_second_lease_uses_private_state_and_reports(self):
        fx = Fixture(self)
        fx.write()
        (fx.dir / "registry.json").write_text("not json", encoding="utf-8")
        fx.lease()
        before = (fx.monitor / "agent" / "STATE.md").read_bytes()
        result = fx.run("run-bad")
        self.assertTrue(result["private_state"])
        self.assertIn("concurrent_audit", {f["code"] for f in result["findings"]})
        self.assertTrue(result["report_required"])
        self.assertEqual(result["report_thread_id"], "thread-cos")
        self.assertEqual((fx.monitor / "agent" / "STATE.md").read_bytes(), before)
        self.assertEqual(fx.checkpoint("run-run-bad-state.md")["run_id"], "run-bad")

    def test_stale_and_finished_leases_are_not_concurrency(self):
        fx = Fixture(self)
        fx.project("alpha", tracked_work="none")
        fx.write()
        fx.lease("old", age=600)
        fx.lease("done", status="finished", age=5)
        result = fx.run()
        self.assertFalse(result["private_state"])
        self.assertTrue(result["audit_complete"], result)

    def test_unreadable_lease_makes_invocation_metadata_unavailable(self):
        fx = Fixture(self)
        fx.project("alpha", tracked_work="none")
        fx.write()
        fx.lease("broken", raw="{not json")
        result = fx.run()
        self.assertFalse(result["audit_complete"])
        self.assertIn("invocation-metadata", result["unavailable_scopes"])
        self.assertTrue(result["report_required"])

    def test_unobservable_leases_write_only_private_state_and_do_not_claim_concurrency(self):
        for name, registry_ok in (("valid empty registry", True), ("bad registry", False)):
            with self.subTest(name):
                fx = Fixture(self)
                fx.write()
                if registry_ok:
                    (fx.dir / "registry.json").write_text(json.dumps({"schema": 1, "projects": [], "standalone": []}), encoding="utf-8")
                else:
                    (fx.dir / "registry.json").write_text("{broken", encoding="utf-8")
                fx.lease("broken", raw="{not json")
                before = (fx.monitor / "agent" / "STATE.md").read_bytes()
                result = fx.run("run-x")
                self.assertTrue(result["private_state"])
                self.assertFalse(result["audit_complete"])
                self.assertTrue(result["report_required"])
                self.assertEqual(result["report_thread_id"], "thread-cos")
                self.assertIn("invocation-metadata", result["unavailable_scopes"])
                self.assertNotIn("concurrent_audit", {f["code"] for f in result["findings"]})
                self.assertEqual((fx.monitor / "agent" / "STATE.md").read_bytes(), before)
                self.assertEqual(fx.checkpoint("run-run-x-state.md")["audit_status"], "incomplete")

    def test_failed_own_lease_registration_writes_only_private_state(self):
        fx = Fixture(self)
        fx.project("alpha", tracked_work="none")
        fx.write()
        (fx.monitor / "agent" / "runs").write_text("a file where the runs folder should be", encoding="utf-8")
        before = (fx.monitor / "agent" / "STATE.md").read_bytes()
        result = fx.run("run-y")
        self.assertTrue(result["private_state"])
        self.assertFalse(result["audit_complete"])
        self.assertIn("invocation-metadata", result["unavailable_scopes"])
        self.assertNotIn("concurrent_audit", {f["code"] for f in result["findings"]})
        self.assertEqual((fx.monitor / "agent" / "STATE.md").read_bytes(), before)

    def test_delivery_status_ends_the_lease(self):
        fx = Fixture(self)
        fx.project("gone", exists=False)
        fx.write()
        result = fx.run()
        lease_file = fx.monitor / "agent" / "runs" / "run-1.lease.json"
        self.assertEqual(json.loads(lease_file.read_text())["status"], "active")
        monitor_checkpoint.set_delivery_status(Path(result["state_file"]), "sent")
        self.assertEqual(json.loads(lease_file.read_text())["status"], "finished")
        self.assertEqual(fx.checkpoint()["delivery_status"], "sent")

    def test_other_sessions_are_never_opened(self):
        source = (ROOT / "scripts" / "monitor_run.py").read_text(encoding="utf-8")
        self.assertNotIn(".codex", source)
        self.assertNotIn("sessions", source.replace("sessions, chats", ""))
        self.assertNotIn("rollout", source)

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

    def test_corrupt_registry_still_reports_to_recipient_from_monitoring_json(self):
        fx = Fixture(self)
        fx.write()
        (fx.dir / "registry.json").write_text("{broken", encoding="utf-8")
        result = fx.run()
        self.assertFalse(result["audit_complete"])
        self.assertTrue(result["report_required"])
        self.assertEqual(result["report_thread_id"], "thread-cos")
        self.assertIn("snapshot_build_failed", {f["code"] for f in result["findings"]})

    def test_unreadable_configuration_is_reported_as_missing_recipient(self):
        fx = Fixture(self)
        result = fx.run()
        self.assertFalse(result["audit_complete"])
        self.assertTrue(result["report_required"])
        self.assertIsNone(result["report_thread_id"])
        self.assertIn("config_unavailable", {f["code"] for f in result["findings"]})
        self.assertIn("no report recipient", result["report_text"])

    def test_registry_entry_without_root_is_an_unavailable_scope(self):
        fx = Fixture(self)
        fx.project("alpha", tracked_work="none")
        fx.write({"schema": 1, "projects": [*fx.projects, {"id": "norot", "name": "No root"}, {"name": "No id", "root": "x"}], "standalone": []})
        result = fx.run(now=datetime.now(timezone.utc))
        self.assertFalse(result["audit_complete"])
        self.assertEqual((result["projects"], result["projects_covered"]), (3, 1))
        self.assertTrue(any(s.startswith("registry-entry:norot") for s in result["unavailable_scopes"]))
        self.assertTrue(any(s.startswith("registry-entry:2") for s in result["unavailable_scopes"]))
        self.assertTrue(result["report_required"])

    def test_cli_prints_compact_json_only(self):
        fx = Fixture(self)
        fx.project("alpha", tracked_work="none")
        fx.write()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = monitor_run.main(["--run-id", "run-cli", "--monitor-dir", str(fx.monitor)])
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
