import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import task_continuity as tc

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
AT = "2026-01-02T11:00:00Z"
LATER = "2026-01-02T13:00:00Z"


def snapshot():
    return {
        "version": 1, "reconciled_at": AT, "next_check_at": LATER,
        "runtime": {"scheduler": "healthy", "host": "online",
                    "observed_at": AT, "next_check_at": LATER,
                    "evidence": ["ref:observer-run-1"]},
        "tasks": [{"id": "task-1", "manager": "manager", "executor": "builder",
                   "status": "in-progress", "start_approved": True,
                   "next_action": "Finish implementation and hand off exact candidate",
                   "acceptance": ["Targeted checks and independent review accepted"],
                   "evidence": ["ref:issue-1"], "updated_at": AT,
                   "next_check_at": LATER, "blocker": None, "candidate": None,
                   "review": None, "accepted": None,
                   "handoffs": [{"id": "handoff-1", "worker": "builder",
                                 "stage": "implementation", "requested_at": AT,
                                 "acknowledged_at": AT, "started_at": AT,
                                 "finished_at": None, "evidence": ["ref:actual-worker-read"]}]}]}


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.data = snapshot()
        self.task = self.data["tasks"][0]

    def check(self):
        return tc.check(self.data, NOW)

    def codes(self):
        return {item["code"] for item in self.check()["findings"]}

    def test_running_snapshot(self):
        self.assertTrue(self.check()["ok"])

    def test_dispatch_and_ack_are_not_start(self):
        for ack in [None, AT]:
            self.task["handoffs"][-1].update(acknowledged_at=ack, started_at=None)
            self.assertIn("worker_not_started", self.codes())

    def test_finished_turn_requires_successor_or_blocker(self):
        self.task["handoffs"][-1]["finished_at"] = AT
        self.assertIn("stage_finished", self.codes())
        self.task["handoffs"].append({**self.task["handoffs"][0], "id": "handoff-2",
                                      "finished_at": None})
        self.assertTrue(self.check()["ok"])

    def test_blocker_requires_owner_resume_and_deadline(self):
        self.task["status"] = "blocked"
        self.assertIn("blocker_missing", self.codes())
        self.task["blocker"] = {"reason": "Reviewer capacity", "owner": "manager",
                                "resume_condition": "Reviewer available",
                                "next_check_at": LATER, "evidence": ["ref:capacity-check"]}
        self.assertTrue(self.check()["ok"])
        del self.task["blocker"]["owner"]
        self.assertFalse(self.check()["ok"])

    def test_backlog_paused_future_do_not_demand_execution(self):
        for state in ["backlog", "paused", "future"]:
            self.task.update(status=state, executor=None, handoffs=[])
            self.data["runtime"] = {"scheduler": "unknown", "host": "unknown",
                                    "observed_at": None, "next_check_at": None, "evidence": []}
            self.assertTrue(self.check()["ok"], state)

    def test_approved_ready_requires_start(self):
        self.task.update(status="ready", handoffs=[], executor=None)
        self.assertIn("worker_not_started", self.codes())
        self.task["start_approved"] = False
        self.assertTrue(self.check()["ok"])

    def test_started_status_cannot_claim_missing_approval(self):
        self.task["start_approved"] = False
        self.assertIn("approval_mismatch", self.codes())

    def test_unapproved_ready_cannot_hide_reported_actual_start(self):
        self.task.update(status="ready", start_approved=False)
        self.assertIn("approval_mismatch", self.codes())

    def test_successor_cannot_start_before_previous_stage_finished(self):
        self.task["handoffs"][0]["finished_at"] = "2026-01-02T11:30:00Z"
        self.task["handoffs"].append({**self.task["handoffs"][0], "id": "handoff-2", "finished_at": None})
        self.assertIn("handoff_overlap", self.codes())

    def test_stale_reconciliation_and_worker_evidence(self):
        self.data["next_check_at"] = AT
        self.task["next_check_at"] = AT
        self.assertTrue({"reconciliation_overdue", "task_overdue"} <= self.codes())

    def test_runtime_unknown_offline_unhealthy_never_green(self):
        for key, value in [("host", "offline"), ("scheduler", "unknown"),
                           ("scheduler", "not_configured")]:
            data = snapshot()
            data["runtime"][key] = value
            result = tc.check(data, NOW)
            self.assertFalse(result["ok"])
        self.assertIn("runtime_overdue", {f["code"] for f in tc.check(
            {**snapshot(), "runtime": {**snapshot()["runtime"], "next_check_at": AT}}, NOW)["findings"]})

    def test_review_and_acceptance_exact_candidate_independent_identity(self):
        self.task.update(status="done", candidate={"id": "sha-1", "author": "builder"},
                         review={"candidate": "sha-1", "reviewer": "reviewer",
                                 "verdict": "approve", "reviewed_at": AT, "evidence": ["ref:review"]},
                         accepted={"candidate": "sha-1", "by": "manager",
                                   "accepted_at": AT, "evidence": ["ref:acceptance"]})
        self.assertTrue(self.check()["ok"])
        self.task["review"]["candidate"] = "sha-old"
        self.assertIn("review_mismatch", self.codes())
        self.task["review"]["candidate"] = "sha-1"
        self.task["review"]["reviewer"] = "builder"
        self.assertIn("review_not_independent", self.codes())

    def test_done_cannot_use_author_pass_or_dispatch_receipt(self):
        self.task["status"] = "done"
        self.assertTrue({"candidate_missing", "review_missing", "acceptance_missing"} <= self.codes())

    def test_review_stage_needs_exact_candidate_and_independent_current_worker(self):
        self.task["status"] = "review"
        self.assertIn("candidate_missing", self.codes())
        self.task["candidate"] = {"id": "sha-1", "author": "builder"}
        self.task["handoffs"][-1]["stage"] = "review"
        self.assertIn("review_not_independent", self.codes())
        self.task["executor"] = "reviewer"
        self.task["handoffs"][-1]["worker"] = "reviewer"
        self.assertTrue(self.check()["ok"])

    def test_review_handoff_is_independent_even_if_task_status_in_progress(self):
        self.task["candidate"] = {"id": "sha-1", "author": "builder"}
        self.task["handoffs"][-1]["stage"] = "review"
        self.assertIn("review_not_independent", self.codes())

    def test_malformed_future_and_ordered_times(self):
        self.task["handoffs"][0]["started_at"] = LATER
        self.assertFalse(self.check()["ok"])
        for invalid in [None, [], {"version": True}, {**snapshot(), "tasks": [None]}]:
            self.assertFalse(tc.check(invalid, NOW)["ok"])

    def test_untrusted_field_types_fail_without_crashing(self):
        for key in ["id", "status", "manager", "executor", "acceptance", "handoffs"]:
            data = snapshot()
            data["tasks"][0][key] = {"unexpected": True}
            self.assertFalse(tc.check(data, NOW)["ok"], key)
        self.task["handoffs"][0]["id"] = []
        self.assertFalse(self.check()["ok"])

    def test_review_and_manager_acceptance_cannot_precede_candidate_review(self):
        self.task.update(status="done", candidate={"id": "sha-1", "author": "builder"},
                         review={"candidate": "sha-1", "reviewer": "reviewer", "verdict": "approve",
                                 "reviewed_at": AT, "evidence": ["ref:review"]},
                         accepted={"candidate": "sha-1", "by": "manager", "accepted_at": "2026-01-02T10:00:00Z",
                                   "evidence": ["ref:acceptance"]})
        self.assertIn("acceptance_order", self.codes())

    def test_no_mutations(self):
        original = copy.deepcopy(self.data)
        self.check()
        self.assertEqual(original, self.data)

    def test_required_observation_fields_cannot_disappear(self):
        for container, key in [("runtime", "host"), ("runtime", "evidence"),
                               ("task", "handoffs"), ("task", "candidate")]:
            data = snapshot()
            del (data["runtime"] if container == "runtime" else data["tasks"][0])[key]
            self.assertFalse(tc.check(data, NOW)["ok"], key)

    def test_runtime_types_even_when_paused(self):
        self.task["status"] = "paused"
        self.data["runtime"]["scheduler"] = []
        self.assertFalse(self.check()["ok"])

    def test_cli_unknown_missing_and_json_errors_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tasks.json"
            for content in [None, "{", '{"version":1,"version":2}', "[]"]:
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                run = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/task_continuity.py"),
                                      "--file", str(path)], capture_output=True, text=True)
                self.assertNotEqual(0, run.returncode)
                self.assertFalse(json.loads(run.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()


def live_snapshot():
    # Helper that creates a snapshot with future deadlines relative to system clock
    s = snapshot()
    s["reconciled_at"] = "2026-10-16T12:00:00Z"
    s["next_check_at"] = "2028-01-01T00:00:00Z"
    s["runtime"]["observed_at"] = "2026-10-16T12:00:00Z"
    s["runtime"]["next_check_at"] = "2028-01-01T00:00:00Z"
    s["tasks"][0]["updated_at"] = "2026-10-16T12:00:00Z"
    s["tasks"][0]["next_check_at"] = "2028-01-01T00:00:00Z"
    s["tasks"][0]["handoffs"][0]["requested_at"] = "2026-10-16T12:00:00Z"
    s["tasks"][0]["handoffs"][0]["acknowledged_at"] = "2026-10-16T12:00:00Z"
    s["tasks"][0]["handoffs"][0]["started_at"] = "2026-10-16T12:00:00Z"
    return s


class CbmAuditWrapperTests(unittest.TestCase):
    def test_templates_task_json_passes(self):
        template_file = ROOT / "templates" / "task.json"
        self.assertTrue(template_file.exists())
        run = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py"), "check", "--file", str(template_file)],
            capture_output=True, text=True
        )
        self.assertEqual(run.returncode, 0, f"STDOUT: {run.stdout} STDERR: {run.stderr}")
        res = json.loads(run.stdout)
        self.assertTrue(res["ok"])
        self.assertEqual(res["findings"], [])

    def test_explicit_file_required(self):
        for cmd in [
            [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py"), "check"],
            [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py"), "audit"],
            [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py")],
        ]:
            run = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)

    def test_audit_atomic_state_write(self):
        with tempfile.TemporaryDirectory() as temp:
            snap_file = Path(temp) / "snap.json"
            state_file = Path(temp) / "state.json"
            snap_file.write_text(json.dumps(live_snapshot()), encoding="utf-8")
            
            run = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py"), "audit",
                 "--file", str(snap_file), "--state-file", str(state_file)],
                capture_output=True, text=True
            )
            self.assertEqual(run.returncode, 0, f"STDOUT: {run.stdout} STDERR: {run.stderr}")
            self.assertTrue(state_file.exists())
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["last_status"], "clean")
            self.assertEqual(state["last_check"]["active_findings"], 0)
            self.assertIn("as_of_utc", state["last_check"])

    def test_corrupt_state_rejected_without_wipe(self):
        with tempfile.TemporaryDirectory() as temp:
            snap_file = Path(temp) / "snap.json"
            state_file = Path(temp) / "state.json"
            snap_file.write_text(json.dumps(live_snapshot()), encoding="utf-8")
            corrupt_content = "{corrupted json content not valid"
            state_file.write_text(corrupt_content, encoding="utf-8")

            run = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py"), "audit",
                 "--file", str(snap_file), "--state-file", str(state_file)],
                capture_output=True, text=True
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(state_file.read_text(encoding="utf-8"), corrupt_content)

    def test_alert_re_escalation_on_overdue_task(self):
        with tempfile.TemporaryDirectory() as temp:
            snap_file = Path(temp) / "snap.json"
            state_file = Path(temp) / "state.json"
            
            bad_snap = live_snapshot()
            bad_snap["tasks"][0]["handoffs"][-1]["started_at"] = None
            snap_file.write_text(json.dumps(bad_snap), encoding="utf-8")
            
            run1 = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py"), "audit",
                 "--file", str(snap_file), "--state-file", str(state_file)],
                capture_output=True, text=True
            )
            self.assertEqual(run1.returncode, 1)
            state1 = json.loads(state_file.read_text(encoding="utf-8"))
            fp = "worker_not_started:task-1"
            self.assertIn(fp, state1["alert_ledger"])
            self.assertEqual(state1["alert_ledger"][fp]["status"], "new")
            
            run2 = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py"), "audit",
                 "--file", str(snap_file), "--state-file", str(state_file)],
                capture_output=True, text=True
            )
            self.assertEqual(run2.returncode, 1)
            state2 = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state2["alert_ledger"][fp]["status"], "deduplicated")
            
            bad_snap["tasks"][0]["next_check_at"] = "2020-01-01T00:00:00Z"
            snap_file.write_text(json.dumps(bad_snap), encoding="utf-8")
            
            run3 = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py"), "audit",
                 "--file", str(snap_file), "--state-file", str(state_file)],
                capture_output=True, text=True
            )
            self.assertEqual(run3.returncode, 1)
            state3 = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn(state3["alert_ledger"][fp]["status"], ("re_escalated", "escalated"))
            self.assertTrue(state3["alert_ledger"][fp]["escalated"])

    def test_odd_cross_compatibility(self):
        odd_script = Path("E:/Codex_work/open-dreamdocs-enforcement-20261017/scripts/task_continuity.py")
        if not odd_script.exists():
            self.skipTest("ODD script not present on host")
        import importlib.util
        spec = importlib.util.spec_from_file_location("odd_tc", str(odd_script))
        odd_tc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(odd_tc)
        
        res_cbm = tc.check(snapshot(), NOW)
        res_odd = odd_tc.check(snapshot(), NOW)
        self.assertEqual(res_cbm["ok"], res_odd["ok"])
        self.assertEqual(res_cbm["findings"], res_odd["findings"])
        
        broken = snapshot()
        broken["tasks"][0]["status"] = "review"
        broken["tasks"][0]["candidate"] = {"id": "sha-1", "author": "builder"}
        broken["tasks"][0]["handoffs"][-1]["stage"] = "review"
        res_cbm_b = tc.check(broken, NOW)
        res_odd_b = odd_tc.check(broken, NOW)
        self.assertEqual(res_cbm_b["ok"], res_odd_b["ok"])
        self.assertEqual(res_cbm_b["findings"], res_odd_b["findings"])


if __name__ == "__main__":
    unittest.main()
