"""Monitor signal for open state:active issues without current work (#372)."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import status_sources as ss  # noqa: E402

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def iso(hours):
    return (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(number, labels, state="OPEN", updated=-2):
    return {"number": number, "state": state, "title": "t", "updatedAt": iso(updated),
            "url": f"https://github.com/o/r/issues/{number}", "labels": [{"name": n} for n in labels]}


def request(rid, number, state="open"):
    row = {"id": rid, "from": "manager", "to": "developer", "summary": rid, "link": f"https://github.com/o/r/issues/{number}",
           "opened": iso(-5), "due": iso(1), "state": state, "escalations": []}
    if state == "answered":
        row.update(outcome="done", answered=iso(-1))
    return row


class ActiveWithoutWorkTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / ".bot-mode/local").mkdir(parents=True)

    def run_status(self, issues, requests=(), prs=()):
        (self.dir / ".bot-mode/local/requests.json").write_text(json.dumps({"schema": 1, "requests": list(requests)}), encoding="utf-8")
        fetch = lambda repo: {"issue": list(issues), "pr": list(prs), "truncated": []}
        _, summary = ss.project_status({"id": "p", "name": "p", "root": str(self.dir), "repository": "o/r"}, NOW, fetch=fetch)
        return summary

    def ids(self, summary):
        return [i["id"] for i in summary["triage"]["active_without_work"]]

    def test_active_issue_without_request_or_executor_is_listed(self):
        self.assertEqual(self.ids(self.run_status([issue(1, ["state:active", "type:task"])])), ["p/issue-1"])

    def test_issue_with_a_live_request_is_not_listed(self):
        self.assertEqual(self.ids(self.run_status([issue(2, ["state:active"])], [request("impl-2", 2)])), [])

    def test_answered_request_alone_is_not_current_work(self):
        self.assertEqual(self.ids(self.run_status([issue(3, ["state:active"])], [request("impl-3", 3, "answered")])), ["p/issue-3"])

    def test_other_states_closed_issues_and_pull_requests_are_never_listed(self):
        issues = [issue(n, [label]) for n, label in enumerate(
            ["state:backlog", "state:ready", "state:review", "state:blocked", "state:needs-owner", "future", "type:task"], 10)]
        issues += [issue(30, ["state:active"], "CLOSED"), issue(31, ["state:active", "state:blocked"]), issue(32, ["state:active", "state:review"])]
        prs = [dict(issue(40, ["state:active"]), url="https://github.com/o/r/pull/40")]
        self.assertEqual(self.ids(self.run_status(issues, prs=prs)), [])

    def test_a_project_pause_silences_it(self):
        (self.dir / ".bot-mode/PAUSED").write_text("paused", encoding="utf-8")
        self.assertEqual(self.ids(self.run_status([issue(4, ["state:active"])])), [])

    def task(self, number, status, executor="developer", updated=-1):
        return {"source": "task-record", "authority": f"https://github.com/o/r/issues/{number}", "status": status,
                "executor": executor, "updated_at": iso(updated)}

    def direct(self, number, tasks):
        issues = {("issues", str(number)): issue(number, ["state:active"])}
        return ss.active_without_work(issues, {}, tasks, "o/r", "p", self.dir, NOW)

    def test_task_record_with_a_fresh_executor_blocked_or_paused_counts_as_current(self):
        for status in ("in-progress", "review", "blocked", "paused"):
            self.assertEqual(self.direct(5, [self.task(5, status)]), [], status)

    def test_stale_or_executorless_in_progress_record_does_not_count(self):
        self.assertEqual(len(self.direct(6, [self.task(6, "in-progress", updated=-24)])), 1)
        self.assertEqual(len(self.direct(6, [self.task(6, "in-progress", executor=None)])), 1)
        self.assertEqual(len(self.direct(6, [self.task(7, "in-progress")])), 1)

    def test_finding_waits_30_minutes_names_the_issue_and_never_claims_idle(self):
        summary = self.run_status([issue(8, ["state:active"], updated=-0.2), issue(9, ["state:active"], updated=-3)])
        found = ss.triage_findings({"p": summary["triage"]}, NOW)
        mine = [f for f in found if f["code"] == "active_without_work"]
        self.assertEqual(len(mine), 1)
        self.assertIn("p/issue-9", mine[0]["detail"])
        self.assertNotIn("issue-8", mine[0]["detail"])
        self.assertNotIn("is idle", mine[0]["detail"])
        self.assertIn("idleness is not claimed", mine[0]["detail"])
        self.assertEqual(mine[0]["scope"], "p")

    def test_manager_prompt_asks_for_assignment_or_blocker_and_dedups_after_sent(self):
        import monitor_run
        summary = self.run_status([issue(9, ["state:active"], updated=-3)])
        findings = [f for f in ss.triage_findings({"p": summary["triage"]}, NOW) if f["code"] == "active_without_work"]
        ledger = {f"{f['code']}:{f['scope']}": {"status": "new"} for f in findings}
        registry = mock.Mock()
        registry.resolve_project_manager.return_value = {"status": "ok", "thread_id": "th-1", "bot": "manager"}

        def route(led):
            fresh = [f for f in findings if led[f"{f['code']}:{f['scope']}"].get("manager") is None]
            with mock.patch.object(monitor_run, "room_registry", registry):
                return monitor_run.route_manager_signals({"registry_path": "r.json"}, {"alert_ledger": led}, findings, fresh)
        deliveries, problems, _ = route(ledger)
        self.assertEqual((problems, len(deliveries)), ([], 1))
        prompt = deliveries[0]["prompt"]
        self.assertIn("active issues without current work", prompt)
        self.assertIn("record a blocker or wait with the next check", prompt)
        self.assertIn("does not claim any agent is idle", prompt)
        self.assertIn("not reporting an outage", prompt)
        for entry in ledger.values():
            entry["manager"] = "sent"
        self.assertEqual(route(ledger)[0], [])


if __name__ == "__main__":
    unittest.main()

