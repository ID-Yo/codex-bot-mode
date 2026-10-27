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


def monitor_audit(now):
    observed = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"run_id": "run-current", "status": "complete", "coverage": "complete",
            "observed_at": observed, "next_check_at": LATER, "covered_tasks": ["task-1"],
            "unavailable_scopes": [],
            "evidence": ["ref:current-run"],
            "invocations": [{"run_id": "run-current", "status": "completed", "updated_at": observed}]}


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

    def test_monitor_cycle_requires_fresh_complete_audit_and_current_invocation(self):
        data = snapshot()
        data["monitor_audit"] = monitor_audit(NOW)
        self.assertTrue(tc.check_monitor_cycle(data, NOW, current_run_id="run-current")["ok"])

        data["monitor_audit"]["status"] = "started"
        incomplete = tc.check_monitor_cycle(data, NOW, current_run_id="run-current")
        self.assertFalse(incomplete["ok"])
        self.assertIn("audit_incomplete", {item["code"] for item in incomplete["findings"]})

    def test_stale_active_invocation_is_unknown_not_concurrency(self):
        data = snapshot()
        audit = monitor_audit(NOW)
        audit["invocations"].append({"run_id": "run-old", "status": "active", "updated_at": AT})
        data["monitor_audit"] = audit
        result = tc.check_monitor_cycle(data, NOW, current_run_id="run-current")
        codes = {item["code"] for item in result["findings"]}
        self.assertNotIn("concurrent_audit", codes)
        self.assertIn("concurrency_unknown", codes)
        self.assertFalse(result["ok"])

    def test_fresh_second_active_invocation_is_concurrency(self):
        data = snapshot()
        audit = monitor_audit(NOW)
        audit["invocations"].append({"run_id": "run-other", "status": "active", "updated_at": audit["observed_at"]})
        data["monitor_audit"] = audit
        result = tc.check_monitor_cycle(data, NOW, current_run_id="run-current")
        self.assertIn("concurrent_audit", {item["code"] for item in result["findings"]})

    def test_partial_coverage_keeps_findings_for_known_scoped_tasks(self):
        data = snapshot()
        data["tasks"][0]["handoffs"][0]["started_at"] = None
        audit = monitor_audit(NOW)
        audit["coverage"] = "partial"
        data["monitor_audit"] = audit
        result = tc.check_monitor_cycle(data, NOW, current_run_id="run-current")
        codes = {item["code"] for item in result["findings"]}
        self.assertIn("worker_not_started", codes)
        self.assertIn("audit_incomplete", codes)
        self.assertFalse(result["ok"])

    def test_incomplete_audit_deduplicates_and_preserves_unobserved_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "monitor-state.json"
            state_path.write_text(json.dumps({"alert_ledger": {
                "worker_not_started:unseen-task": {"status": "new", "detail": "preserve"}}}), encoding="utf-8")
            finding = {"code": "audit_incomplete", "scope": "monitor", "detail": "Coverage partial"}
            state, new = tc.update_monitor_state(state_path, [finding], complete=False, now=NOW)
            self.assertEqual(state["last_status"], "audit_incomplete")
            self.assertEqual(len(new), 1)
            self.assertIn("worker_not_started:unseen-task", state["alert_ledger"])
            state, new = tc.update_monitor_state(state_path, [finding], complete=False, now=NOW)
            self.assertEqual(new, [])
            self.assertEqual(state["alert_ledger"]["audit_incomplete:monitor"]["status"], "deduplicated")

    def test_completed_audit_clears_findings_for_paused_task(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "monitor-state.json"
            state_path.write_text(json.dumps({"alert_ledger": {
                "worker_not_started:task-1": {"status": "new", "detail": "old"}}}), encoding="utf-8")
            state, _ = tc.update_monitor_state(state_path, [], complete=True, now=NOW)
            self.assertNotIn("worker_not_started:task-1", state["alert_ledger"])

    def test_monitor_audit_cli_records_incomplete_as_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            source, state = Path(temp) / "snapshot.json", Path(temp) / "STATE.md"
            state.write_text("Existing narrative history.\\n", encoding="utf-8")
            data = snapshot()
            data["monitor_audit"] = monitor_audit(datetime.now(timezone.utc))
            data["monitor_audit"]["status"] = "started"
            source.write_text(json.dumps(data), encoding="utf-8")
            code = tc.main(["monitor-audit", "--file", str(source), "--state-file", str(state), "--run-id", "run-current"])
            self.assertEqual(code, 1)
            import monitor_checkpoint
            saved = monitor_checkpoint.load_checkpoint(state)
            self.assertFalse(saved["audit_success"])
            self.assertEqual(saved["last_status"], "audit_incomplete")
            self.assertIn("audit_incomplete:monitor", saved["alert_ledger"])
            self.assertEqual(saved["coverage"], "complete")
            self.assertEqual(saved["run_id"], "run-current")
            self.assertEqual(saved["unavailable_scopes"], [])
            self.assertIn("Existing narrative history.", state.read_text(encoding="utf-8"))
            delivery = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "monitor_checkpoint.py"),
                                       "--state", str(state), "--set-delivery-status", "delivery_unknown"],
                                      capture_output=True, text=True)
            self.assertEqual(delivery.returncode, 0, delivery.stderr)
            self.assertEqual(monitor_checkpoint.load_checkpoint(state)["delivery_status"], "delivery_unknown")

    def test_monitor_audit_cli_ok_reflects_whole_cycle(self):
        import contextlib, io
        from unittest import mock
        with tempfile.TemporaryDirectory() as temp:
            source, state = Path(temp) / "snapshot.json", Path(temp) / "STATE.md"
            data = snapshot()
            data["monitor_audit"] = monitor_audit(datetime.now(timezone.utc))
            data["monitor_audit"]["status"] = "started"
            source.write_text(json.dumps(data), encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(tc, "check", return_value={"ok": True, "findings": []}), \
                    contextlib.redirect_stdout(out):
                code = tc.main(["monitor-audit", "--file", str(source), "--state-file", str(state), "--run-id", "run-current"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(payload["audit_complete"])
            self.assertFalse(payload["ok"])

    def test_bounded_checkpoint_read_omits_large_history_and_memory(self):
        import monitor_checkpoint
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "STATE.md"
            memory_path = Path(temp) / "memory.md"
            checkpoint = {"version": 1, "run_id": "run-1", "audit_status": "incomplete",
                          "coverage": "partial", "observed_at": AT, "next_check_at": LATER,
                          "active_alert_count": 2, "delivery_status": "delivery_unknown"}
            state_path.write_text("<!-- monitor-checkpoint\n" + json.dumps(checkpoint) + "\n-->\n" +
                                  ("PRIVATE_HISTORY_SENTINEL " * 55000), encoding="utf-8")
            memory_path.write_text("PRIVATE_MEMORY_SENTINEL " * 8000, encoding="utf-8")
            result = monitor_checkpoint.read_initial_context(state_path, memory_path)
            rendered = json.dumps(result)
            self.assertLess(len(rendered), 2048)
            self.assertNotIn("PRIVATE_HISTORY_SENTINEL", rendered)
            self.assertNotIn("PRIVATE_MEMORY_SENTINEL", rendered)
            self.assertFalse(result["memory_loaded"])
            self.assertEqual(result["memory_bytes"], memory_path.stat().st_size)
            run = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "monitor_checkpoint.py"),
                                  "--state", str(state_path), "--memory", str(memory_path)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0)
            self.assertLess(len(run.stdout), 2048)
            self.assertNotIn("PRIVATE_MEMORY_SENTINEL", run.stdout)

    def test_checkpoint_after_read_limit_is_unknown(self):
        import monitor_checkpoint
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "STATE.md"
            state_path.write_text("HISTORY " * 50000 + "<!-- monitor-checkpoint\n{}\n-->", encoding="utf-8")
            result = monitor_checkpoint.read_initial_context(state_path)
            self.assertEqual(result["checkpoint_status"], "unknown")
            self.assertLessEqual(result["state_bytes_read"], monitor_checkpoint.STATE_READ_LIMIT)

    def test_markdown_checkpoint_update_preserves_history_and_ledger(self):
        import monitor_checkpoint
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "STATE.md"
            history = "Old narrative history\\n" + ("past details\\n" * 1000)
            state_path.write_text(history, encoding="utf-8")
            state, _ = tc.update_monitor_state(state_path,
                [{"code": "audit_incomplete", "scope": "monitor", "detail": "partial"}],
                complete=False, now=NOW)
            reread = monitor_checkpoint.load_checkpoint(state_path)
            self.assertEqual(state["last_status"], "audit_incomplete")
            self.assertIn("audit_incomplete:monitor", reread["alert_ledger"])
            self.assertIn(history, state_path.read_text(encoding="utf-8"))

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

class Phase2EnforcementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_path = Path(self.temp_dir.name)
        self.bot_mode = self.project_path / ".bot-mode"
        self.local_dir = self.bot_mode / "local"
        self.tasks_dir = self.project_path / "work" / "tasks"
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        self.roster_data = {
            "schema": 1,
            "project": {"name": "test-project"},
            "teams": [{"name": "Development"}],
            "participants": [
                {"bot": "manager", "role": "Manager", "team": "Development", "status": "active"},
                {"bot": "developer", "role": "Developer", "team": "Development", "status": "active"},
                {"bot": "code-reviewer", "role": "Code Reviewer", "team": "Development", "status": "active"},
                {"bot": "verifier", "role": "Verifier", "team": "Development", "status": "active"}
            ]
        }
        (self.bot_mode / "roster.json").write_text(json.dumps(self.roster_data), encoding="utf-8")
        (self.local_dir / "requests.json").write_text(json.dumps({"schema": 1, "requests": []}), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_task(self, task_id="TASK-1", state="in_progress", start_approved="true", local_req_id=None, review_verdict=None):
        content = f"""# Task: {task_id}
## Metadata
- **Task ID**: {task_id}
- **Project**: test-project
- **Team**: Development
- **Responsible Manager**: manager
- **Assigned Executor**: developer
- **State**: {state}
- **Start Approved**: {start_approved}
- **Local Request ID**: {local_req_id or 'none'}
- **Latest Evidence**: ref:test-evidence
"""
        if review_verdict:
            content += f"- **Review Verdict**: {review_verdict}" + chr(10)
        content += """## Acceptance Criteria
1. Unit tests pass
2. Clean public export
## Next Action
- **Action**: Implement features
## Evidence and Artifacts
- **Branch / PR**: ref:pr-1
"""
        (self.tasks_dir / f"{task_id}.md").write_text(content, encoding="utf-8")

    def test_snapshot_generation_from_fixture_passes_check(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "acknowledged_at": now_str,
                "started_at": now_str,
                "state": "open",
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        snap = tc.produce_snapshot(self.project_path)
        self.assertEqual(snap["version"], 1)
        self.assertEqual(len(snap["tasks"]), 1)
        res = tc.check(snap)
        self.assertTrue(res["ok"], f"Snapshot findings: {res['findings']}")

    def test_stage_routing_on_finished_implementation_opens_review_request(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        # Dry-run first
        dry = tc.route_stage(self.project_path, apply=False)
        self.assertTrue(dry["ok"])
        self.assertTrue(dry["dry_run"])
        self.assertEqual(len(dry["plan"]), 1)
        self.assertEqual(dry["plan"][0]["action"], "route_review")
        self.assertEqual(dry["plan"][0]["target"], "code-reviewer")

        # Apply routing
        res = tc.route_stage(self.project_path, apply=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["dry_run"])
        self.assertEqual(len(res["routed"]), 1)
        self.assertEqual(res["routed"][0]["action"], "route_review")

        # Check requests.json has review request with outbox_queued
        updated_reqs = json.loads((self.local_dir / "requests.json").read_text(encoding="utf-8"))["requests"]
        self.assertEqual(len(updated_reqs), 2)
        review_req = updated_reqs[1]
        self.assertEqual(review_req["to"], "code-reviewer")
        self.assertEqual(review_req["delivery_status"], "outbox_queued")

        # Check task state in Markdown transitions to ready, NOT in_progress or review
        task_txt = (self.tasks_dir / "TASK-1.md").read_text(encoding="utf-8")
        self.assertIn("- **State**: ready", task_txt)

    def test_route_stage_keeps_a_request_written_during_delivery(self):
        self._write_task("TASK-1", state="review", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.local_dir / "requests.json").write_text(json.dumps({"schema": 1, "requests": [{
            "id": "req-1", "from": "manager", "to": "developer", "summary": "Implement TASK-1", "link": "work/tasks/TASK-1.md",
            "opened": now_str, "started_at": now_str, "state": "done",
            "reply": {"by": "developer", "link": "ref:c", "outcome": "done", "at": now_str}, "escalations": []}]}), encoding="utf-8")
        ledger = tc.get_req_ledger(self.project_path)
        calls = []

        def adapter(project, target, request_id, summary):
            calls.append(request_id)
            ledger.update(self.project_path, lambda d: ledger.open_request(d, "other-1", "manager", "developer", "Concurrent"))
            return {"status": "delivered", "receipt_id": "r1"}

        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=adapter)
        self.assertEqual(res["routed"][0]["delivery"], "delivered")
        rows = {r["id"]: r for r in json.loads((self.local_dir / "requests.json").read_text(encoding="utf-8"))["requests"]}
        self.assertIn("other-1", rows)
        self.assertEqual(rows[calls[0]]["delivery_status"], "delivered")
        # A second run in the same minute deduplicates against the open request: no second delivery.
        tc.route_stage(self.project_path, apply=True, delivery_adapter=adapter)
        self.assertEqual(len(calls), 1)
    def test_route_stage_apply_refuses_a_helper_without_update(self):
        self._write_task("TASK-1", state="review", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        before = json.dumps({"schema": 1, "requests": [{
            "id": "req-1", "from": "manager", "to": "developer", "summary": "Implement TASK-1", "link": "work/tasks/TASK-1.md",
            "opened": now_str, "started_at": now_str, "state": "done",
            "reply": {"by": "developer", "link": "ref:c", "outcome": "done", "at": now_str}, "escalations": []}]})
        (self.local_dir / "requests.json").write_text(before, encoding="utf-8")
        task_before = (self.tasks_dir / "TASK-1.md").read_text(encoding="utf-8")
        legacy = type("Legacy", (), {"load": staticmethod(lambda p: json.loads(before)), "version": staticmethod(lambda d: "v"),
                                     "save": staticmethod(lambda *a: self.fail("legacy save used"))})
        sent = []
        original = tc.get_req_ledger
        tc.get_req_ledger = lambda project: legacy
        self.addCleanup(setattr, tc, "get_req_ledger", original)
        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=lambda *a: sent.append(a))
        self.assertFalse(res["ok"])
        self.assertIn("update the project's requests.py helper first", res["errors"][0]["error"])
        self.assertEqual(sent, [])
        self.assertEqual((self.local_dir / "requests.json").read_text(encoding="utf-8"), before)
        self.assertEqual((self.tasks_dir / "TASK-1.md").read_text(encoding="utf-8"), task_before)
        self.assertTrue(tc.route_stage(self.project_path, apply=False)["ok"])
    def test_stage_routing_on_request_changes_opens_corrections_request(self):
        self._write_task("TASK-1", state="review", start_approved="true", local_req_id="req-2", review_verdict="request_changes")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-2",
                "from": "manager",
                "to": "code-reviewer",
                "summary": "Review TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "code-reviewer", "link": "ref:review-1", "outcome": "request_changes", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        res = tc.route_stage(self.project_path, apply=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["routed"][0]["action"], "route_corrections")
        self.assertEqual(res["routed"][0]["target"], "developer")

        # Check task state updated to ready awaiting developer start
        task_txt = (self.tasks_dir / "TASK-1.md").read_text(encoding="utf-8")
        self.assertIn("- **State**: ready", task_txt)

    def test_stage_routing_on_approved_review_opens_manager_integration(self):
        self._write_task("TASK-1", state="review", start_approved="true", local_req_id="req-2", review_verdict="approve")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-2",
                "from": "manager",
                "to": "code-reviewer",
                "summary": "Review TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "code-reviewer", "link": "ref:review-1", "outcome": "approve", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        res = tc.route_stage(self.project_path, apply=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["routed"][0]["action"], "route_integration")
        self.assertEqual(res["routed"][0]["target"], "manager")
        task_txt = (self.tasks_dir / "TASK-1.md").read_text(encoding="utf-8")
        self.assertIn("- **State**: ready", task_txt)

    def test_deduplication_never_opens_duplicate_request(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        # First apply routes review
        res1 = tc.route_stage(self.project_path, apply=True)
        self.assertEqual(len(res1["routed"]), 1)
        self.assertEqual(res1["routed"][0]["action"], "route_review")

        # Second apply detects stage already queued and does not create duplicate
        res2 = tc.route_stage(self.project_path, apply=True)
        self.assertEqual(len(res2.get("routed", [])), 0)
        reqs = json.loads((self.local_dir / "requests.json").read_text(encoding="utf-8"))["requests"]
        self.assertEqual(len(reqs), 2)

    def test_pause_preservation(self):
        self._write_task("TASK-1", state="paused", start_approved="true", local_req_id="req-1")
        res = tc.route_stage(self.project_path, apply=True)
        self.assertEqual(len(res.get("routed", [])), 0)

        # Project level pause
        (self.bot_mode / "PAUSED").write_text("Paused by owner", encoding="utf-8")
        self._write_task("TASK-2", state="in_progress", start_approved="true")
        res_p = tc.route_stage(self.project_path, apply=True)
        self.assertIn("project_paused", res_p.get("skipped", []))
        self.assertEqual(len(res_p.get("routed", [])), 0)

    def test_unapproved_task_never_routed(self):
        self._write_task("TASK-1", state="ready", start_approved="false", local_req_id="req-1")
        res = tc.route_stage(self.project_path, apply=True)
        self.assertEqual(len(res.get("routed", [])), 0)

    def test_concurrent_routing_lock_prevents_duplicate_runs(self):
        lock_file = self.local_dir / "route.lock"
        with tc.RouteLock(lock_file, lease_seconds=60):
            with self.assertRaises(RuntimeError):
                tc.route_stage(self.project_path, apply=True)

    def test_cli_rejects_mutually_exclusive_apply_and_dry_run(self):
        run = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / "task_continuity.py"), "route",
             "--project", str(self.project_path), "--apply", "--dry-run"],
            capture_output=True, text=True
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("not allowed with argument", run.stderr)

    def test_task_markdown_state_transitions_to_ready_not_in_progress(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")
        res = tc.route_stage(self.project_path, apply=True)
        task_txt = (self.tasks_dir / "TASK-1.md").read_text(encoding="utf-8")
        self.assertIn("- **State**: ready", task_txt)
        self.assertNotIn("- **State**: in_progress", task_txt)
        self.assertNotIn("- **State**: review", task_txt)

    def test_delivery_adapter_outbox_queued_when_relay_unavailable(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        # No delivery adapter passed and no relay.json present -> outbox_queued
        res = tc.route_stage(self.project_path, apply=True)
        self.assertEqual(res["routed"][0]["delivery"], "outbox_queued")
        reqs = json.loads((self.local_dir / "requests.json").read_text(encoding="utf-8"))["requests"]
        self.assertEqual(reqs[1]["delivery_status"], "outbox_queued")

    def test_interrupted_delivery_marks_delivery_unknown_no_resend(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        def failing_adapter(project, bot, req_id, summary):
            raise TimeoutError("Renderer eligibility timeout / locked desktop")

        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=failing_adapter)
        self.assertEqual(res["routed"][0]["delivery"], "delivery_unknown")
        self.assertIn("Renderer eligibility", res["routed"][0]["delivery_error"])

        reqs = json.loads((self.local_dir / "requests.json").read_text(encoding="utf-8"))["requests"]
        self.assertEqual(reqs[1]["delivery_status"], "delivery_unknown")

        res2 = tc.route_stage(self.project_path, apply=True)
        self.assertEqual(len(res2.get("routed", [])), 0)
        reqs2 = json.loads((self.local_dir / "requests.json").read_text(encoding="utf-8"))["requests"]
        self.assertEqual(len(reqs2), 2)

    def test_delivery_successful_but_worker_turn_fails_or_aborts(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        def mock_adapter(project, bot, req_id, summary):
            return {"receipt_id": "turn-aborted", "status": "sent"}

        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=mock_adapter)
        self.assertEqual(res["routed"][0]["delivery"], "delivered")

        task_txt = (self.tasks_dir / "TASK-1.md").read_text(encoding="utf-8")
        self.assertIn("- **State**: ready", task_txt)

        snap = tc.produce_snapshot(self.project_path)
        check_res = tc.check(snap)
        self.assertIn("worker_not_started", {f["code"] for f in check_res["findings"]})

    def test_concurrent_apply_and_pause_race(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        (self.bot_mode / "PAUSED").write_text("Paused during concurrent run", encoding="utf-8")
        res = tc.route_stage(self.project_path, apply=True)
        self.assertIn("project_paused", res.get("skipped", []))
        self.assertEqual(len(res.get("routed", [])), 0)

    def test_adapter_returning_none_marks_delivery_unknown(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=lambda *a: None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["routed"][0]["delivery"], "delivery_unknown")
        self.assertIn("Malformed or empty receipt", res["routed"][0]["delivery_error"])
        reqs = json.loads((self.local_dir / "requests.json").read_text(encoding="utf-8"))["requests"]
        self.assertEqual(reqs[1]["delivery_status"], "delivery_unknown")

    def test_adapter_returning_empty_dict_marks_delivery_unknown(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=lambda *a: {})
        self.assertTrue(res["ok"])
        self.assertEqual(res["routed"][0]["delivery"], "delivery_unknown")
        self.assertIn("Malformed or empty receipt", res["routed"][0]["delivery_error"])

    def test_adapter_returning_error_marks_delivery_unknown(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        self._write_task("TASK-2", state="in_progress", start_approved="true", local_req_id="req-2")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [
                {
                    "id": "req-1",
                    "from": "manager",
                    "to": "developer",
                    "summary": "Implement TASK-1",
                    "link": "work/tasks/TASK-1.md",
                    "opened": now_str,
                    "started_at": now_str,
                    "state": "done",
                    "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                    "escalations": []
                },
                {
                    "id": "req-2",
                    "from": "manager",
                    "to": "developer",
                    "summary": "Implement TASK-2",
                    "link": "work/tasks/TASK-2.md",
                    "opened": now_str,
                    "started_at": now_str,
                    "state": "done",
                    "reply": {"by": "developer", "link": "ref:commit-2", "outcome": "done", "at": now_str},
                    "escalations": []
                }
            ]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        def adapter(project, bot, req_id, summary):
            if "task-1" in req_id.lower():
                return {"isError": True}
            return {"error": "Relay rejected"}

        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=adapter)
        self.assertEqual(len(res["routed"]), 2)
        self.assertEqual(res["routed"][0]["delivery"], "delivery_unknown")
        self.assertEqual(res["routed"][1]["delivery"], "delivery_unknown")

    def test_adapter_returning_malformed_dict_without_receipt_id_marks_delivery_unknown(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=lambda *a: {"status": "delivered"})
        self.assertEqual(res["routed"][0]["delivery"], "delivery_unknown")
        self.assertIn("missing valid delivery confirmation", res["routed"][0]["delivery_error"])

    def test_inflight_timeout_or_connection_drop_marks_delivery_unknown(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        def drop_adapter(*a):
            raise ConnectionResetError("Connection reset by peer during response read")

        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=drop_adapter)
        self.assertEqual(res["routed"][0]["delivery"], "delivery_unknown")
        self.assertIn("Connection reset", res["routed"][0]["delivery_error"])

    def test_preconnect_missing_relay_marks_outbox_queued(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")
        # Ensure thread_id is set in bindings so preconnect check tests missing relay.json
        (self.local_dir / "bindings.json").write_text(json.dumps({"code-reviewer": "thread-rev-123"}), encoding="utf-8")

        res = tc.route_stage(self.project_path, apply=True)
        self.assertEqual(res["routed"][0]["delivery"], "outbox_queued")
        reqs = json.loads((self.local_dir / "requests.json").read_text(encoding="utf-8"))["requests"]
        self.assertEqual(reqs[1]["delivery_status"], "outbox_queued")
        self.assertIn("relay.json not found", reqs[1]["delivery_note"])

    def test_role_binding_change_missing_reviewer(self):
        roster_data = {
            "schema": 1,
            "project": {"name": "test-project"},
            "teams": [{"name": "Development"}],
            "participants": [
                {"bot": "manager", "role": "Manager", "team": "Development", "status": "active"},
                {"bot": "developer", "role": "Developer", "team": "Development", "status": "active"}
            ]
        }
        (self.bot_mode / "roster.json").write_text(json.dumps(roster_data), encoding="utf-8")
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        res = tc.route_stage(self.project_path, apply=True)
        self.assertEqual(len(res.get("routed", [])), 0)
        self.assertEqual(len(res.get("errors", [])), 1)

    def test_allowlist_enforcement(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true")
        with self.assertRaises(PermissionError):
            tc.route_stage(self.project_path, apply=True, allowlist=["/different/path/only"])

    def test_default_desktop_relay_adapter_refuses_untrusted_relay_address(self):
        import unittest.mock as mock
        (self.local_dir / "bindings.json").write_text(json.dumps({"developer": "thread-dev-123"}), encoding="utf-8")
        bad = ("http://example.invalid:24243", "http://localhost.evil.invalid:24243", "http://127.0.0.1.evil.invalid:24243",
               "http://localhost:24243@example.invalid", "https://127.0.0.1:24243", "http://127.0.0.1:24243/x",
               "http://127.0.0.1:24243?a=1", "http://user@127.0.0.1:24243", "http://127.0.0.1:99999", "http://127.0.0.1")
        for url in bad:
            with self.subTest(url=url):
                (self.project_path / ".bot-mode" / "relay.json").write_text(json.dumps({"url": url, "token": "test-secret-token"}), encoding="utf-8")
                with mock.patch("urllib.request.OpenerDirector.open") as mock_open:
                    with self.assertRaises(tc.PreconnectConfigurationError):
                        tc.default_desktop_relay_adapter(self.project_path, "developer", "req-1", "summary")
                    mock_open.assert_not_called()

    def test_default_desktop_relay_adapter_sends_to_ipv4_loopback_for_localhost_descriptor(self):
        import unittest.mock as mock
        (self.local_dir / "bindings.json").write_text(json.dumps({"developer": "thread-dev-123"}), encoding="utf-8")
        (self.project_path / ".bot-mode" / "relay.json").write_text(json.dumps({"url": "http://LOCALHOST:24243", "token": "test-secret-token"}), encoding="utf-8")
        with mock.patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"error": "x"}).encode("utf-8")
            mock_open.return_value.__enter__.return_value = mock_resp
            with self.assertRaises(RuntimeError):
                tc.default_desktop_relay_adapter(self.project_path, "developer", "req-1", "summary")
            self.assertEqual(mock_open.call_args[0][0].full_url, "http://127.0.0.1:24243/call")

    def test_default_desktop_relay_adapter_receipt_handling(self):
        import unittest.mock as mock
        from urllib.request import Request

        # Create valid relay.json and bindings
        (self.project_path / ".bot-mode" / "relay.json").write_text(json.dumps({
            "url": "http://localhost:24243",
            "token": "test-secret-token"
        }), encoding="utf-8")
        (self.local_dir / "bindings.json").write_text(json.dumps({
            "developer": "thread-dev-123"
        }), encoding="utf-8")

        # Case 1: Relay returns error key -> raises RuntimeError
        with mock.patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"error": "Desktop locked"}).encode("utf-8")
            mock_open.return_value.__enter__.return_value = mock_resp
            with self.assertRaises(RuntimeError) as cm:
                tc.default_desktop_relay_adapter(self.project_path, "developer", "req-1", "summary")
            self.assertIn("Relay error: Desktop locked", str(cm.exception))

        # Case 2: Relay returns isError -> raises RuntimeError
        with mock.patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"result": {"isError": True, "error": "failed"}}).encode("utf-8")
            mock_open.return_value.__enter__.return_value = mock_resp
            with self.assertRaises(RuntimeError) as cm:
                tc.default_desktop_relay_adapter(self.project_path, "developer", "req-1", "summary")
            self.assertIn("Relay call failed", str(cm.exception))

        # Case 3: Relay returns empty result -> raises RuntimeError
        with mock.patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"result": {}}).encode("utf-8")
            mock_open.return_value.__enter__.return_value = mock_resp
            with self.assertRaises(RuntimeError) as cm:
                tc.default_desktop_relay_adapter(self.project_path, "developer", "req-1", "summary")
            self.assertIn("no delivery confirmation", str(cm.exception))

        # Case 4: Relay returns valid threadId -> success delivered
        with mock.patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps({"result": {"threadId": "thread-dev-123"}}).encode("utf-8")
            mock_open.return_value.__enter__.return_value = mock_resp
            receipt = tc.default_desktop_relay_adapter(self.project_path, "developer", "req-1", "summary")
            self.assertEqual(receipt["status"], "delivered")
            self.assertEqual(receipt["receipt_id"], "thread-dev-123")


    def test_real_demonstration_bounded_handoff(self):
        self._write_task("TASK-1", state="in_progress", start_approved="true", local_req_id="req-1")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_data = {
            "schema": 1,
            "requests": [{
                "id": "req-1",
                "from": "manager",
                "to": "developer",
                "summary": "Implement TASK-1",
                "link": "work/tasks/TASK-1.md",
                "opened": now_str,
                "started_at": now_str,
                "state": "done",
                "reply": {"by": "developer", "link": "ref:commit-1", "outcome": "done", "at": now_str},
                "escalations": []
            }]
        }
        (self.local_dir / "requests.json").write_text(json.dumps(req_data), encoding="utf-8")

        delivered_messages = []
        def mock_delivery_adapter(project, bot, req_id, summary):
            delivered_messages.append({"bot": bot, "req_id": req_id, "summary": summary})
            return {"status": "sent", "receipt_id": "receipt-123"}

        res = tc.route_stage(self.project_path, apply=True, delivery_adapter=mock_delivery_adapter)
        self.assertTrue(res["ok"])
        self.assertEqual(len(delivered_messages), 1)
        self.assertEqual(delivered_messages[0]["bot"], "code-reviewer")
        self.assertEqual(res["routed"][0]["delivery"], "delivered")


if __name__ == "__main__":
    unittest.main()
