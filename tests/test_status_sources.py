import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import monitor_run
import status_sources
import task_continuity as tc

NOW = datetime(2026, 10, 24, 4, 37, tzinfo=timezone.utc)


def iso(delta_hours):
    return (NOW + timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class StatusSourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fetched = {"issue": [], "pr": []}
        original = status_sources.github_fetch
        status_sources.github_fetch = lambda repo: self.fetched
        self.addCleanup(setattr, status_sources, "github_fetch", original)

    def project(self, pid="p", requests=None, repository=None, **extra):
        root = self.dir / pid
        (root / ".bot-mode" / "local").mkdir(parents=True)
        if requests is not None:
            (root / ".bot-mode" / "local" / "requests.json").write_text(
                json.dumps({"schema": 1, "requests": requests}), encoding="utf-8")
        row = {"id": pid, "name": pid, "root": str(root), **extra}
        if repository:
            row["repository"] = repository
        return row

    def status(self, project):
        return status_sources.project_status(project, NOW)

    def snapshot(self, tasks):
        return {"version": 1, "reconciled_at": iso(0), "next_check_at": iso(1),
                "runtime": {"scheduler": "healthy", "host": "online", "observed_at": iso(0),
                            "next_check_at": iso(1), "evidence": ["ref:test"]}, "tasks": tasks}

    def codes(self, tasks):
        return {(f["code"], f["scope"]) for f in tc.check(self.snapshot(tasks), NOW)["findings"]}

    def request(self, rid, to, opened, state="open", outcome=None, answered=None, link="https://github.com/o/r/issues/7", due=None):
        row = {"id": rid, "from": "manager", "to": to, "summary": rid, "link": link, "opened": opened,
               "due": due or iso(1), "state": state, "escalations": []}
        if state == "answered":
            row.update(outcome=outcome, answered=answered)
        return row

    def test_project_without_task_folder_gets_status_from_ledger_not_backlog(self):
        tasks, summary = self.status(self.project(requests=[self.request("impl-1", "developer", iso(-2))]))
        self.assertEqual([t["status"] for t in tasks], ["in-progress"])
        self.assertEqual(summary["state"], "known")
        self.assertIn("ledger", tasks[0]["source"])
        self.assertTrue(any(e.startswith("ref:ledger:") for e in tasks[0]["evidence"]))

    def test_task_record_without_state_is_not_reported_as_backlog(self):
        row = self.project()
        (Path(row["root"]) / "work" / "tasks").mkdir(parents=True)
        (Path(row["root"]) / "work" / "tasks" / "T-1.md").write_text("- Task ID: T-1\n", encoding="utf-8")
        ledger = ROOT / ".bot-mode" / "requests.py"
        shutil.copy(ledger if ledger.exists() else ROOT / "scripts" / "team_room" / "requests.py",
                    Path(row["root"]) / ".bot-mode" / "requests.py")
        tasks, summary = self.status(row)
        self.assertEqual((tasks, summary["state"]), ([], "unknown"))

    def test_github_state_labels_give_status_with_evidence(self):
        self.fetched["issue"] = [
            {"number": n, "state": "OPEN", "title": f"t{n}", "updatedAt": iso(-1), "url": f"https://github.com/o/r/issues/{n}",
             "labels": [{"name": label}]}
            for n, label in ((1, "state:ready"), (2, "state:active"), (3, "state:review"), (4, "state:needs-owner"), (5, "backlog"), (6, "future"))]
        self.fetched["issue"].append({"number": 8, "state": "OPEN", "title": "x", "updatedAt": iso(-1),
                                      "url": "https://github.com/o/r/issues/8", "labels": [{"name": "bug"}]})
        tasks, summary = self.status(self.project(repository="o/r"))
        got = {t["id"]: t["status"] for t in tasks}
        self.assertEqual(got, {"p/issue-1": "ready", "p/issue-2": "in-progress", "p/issue-3": "review",
                               "p/issue-4": "blocked", "p/issue-5": "backlog", "p/issue-6": "future"})
        self.assertTrue(all(t["evidence"][0].startswith("ref:github:https://github.com/o/r/issues/") for t in tasks))
        self.assertEqual(summary["total"], 6)

    def test_project_without_any_status_is_unknown_unless_declared_empty(self):
        _, summary = self.status(self.project("a"))
        self.assertEqual(summary["state"], "unknown")
        _, summary = self.status(self.project("b", tracked_work="none"))
        self.assertEqual(summary["state"], "declared-none")

    def test_request_changes_without_successor_is_a_stall(self):
        # Reviewer answered partial (REQUEST CHANGES); the issue is still open and no successor request exists.
        self.fetched["issue"] = [{"number": 7, "state": "OPEN", "title": "t", "updatedAt": iso(-9),
                                  "url": "https://github.com/o/r/issues/7", "labels": [{"name": "state:active"}]}]
        requests = [self.request("impl-1", "developer", iso(-12), "answered", "done", iso(-10)),
                    self.request("rev-1", "code-reviewer", iso(-10), "answered", "partial", iso(-9.5))]
        tasks, _ = self.status(self.project(requests=requests, repository="o/r"))
        self.assertIn(("stage_finished", "p/issue-7"), self.codes(tasks))

    def test_idle_review_queue_is_reported(self):
        # PR labelled for review whose only review request was answered long ago and nothing is running.
        self.fetched["issue"] = [{"number": 7, "state": "OPEN", "title": "t", "updatedAt": iso(-7),
                                  "url": "https://github.com/o/r/issues/7", "labels": [{"name": "state:review"}]}]
        requests = [self.request("rev-1", "code-reviewer", iso(-7.5), "answered", "done", iso(-7))]
        tasks, _ = self.status(self.project(requests=requests, repository="o/r"))
        self.assertEqual(tasks[0]["status"], "review")
        self.assertIn(("stage_finished", "p/issue-7"), self.codes(tasks))
        self.assertNotEqual(tasks[0]["status"], "backlog")

    def test_review_candidate_comes_from_request_id_sha_and_implementation_chain(self):
        requests = [self.request("cbm9-impl", "developer", iso(-5), "answered", "done", iso(-4), link="codex://threads/x"),
                    self.request("cbm9-review-abc1234", "code-reviewer", iso(-1), link="https://github.com/o/r/pull/9")]
        tasks, _ = self.status(self.project(requests=requests))
        self.assertEqual(tasks[0]["candidate"], {"id": "abc1234", "author": "developer"})
        self.assertNotIn(("candidate_missing", "p/pr-9"), self.codes(tasks))

    def test_review_without_any_recorded_author_still_reports_missing_candidate(self):
        requests = [self.request("cbm8-review-abc1234", "code-reviewer", iso(-1), link="https://github.com/o/r/pull/8")]
        tasks, _ = self.status(self.project(requests=requests))
        self.assertIn(("candidate_missing", "p/pr-8"), self.codes(tasks))

    def test_checkpoint_holds_the_findings_of_many_projects(self):
        from monitor_checkpoint import STATE_READ_LIMIT
        requests = [self.request(f"impl-{n}", "developer", iso(-30), due=iso(-29), link=f"https://github.com/o/r/issues/{n}")
                    for n in range(300)]
        row = self.project(requests=requests)
        (self.dir / "registry.json").write_text(json.dumps({"projects": [row]}), encoding="utf-8")
        monitor = self.dir / "monitor"
        (monitor / "agent").mkdir(parents=True)
        (monitor / "agent" / "monitoring.json").write_text(json.dumps({
            "report_target_thread_id": "t", "registry_path": str(self.dir / "registry.json"), "interval_minutes": 20}), encoding="utf-8")
        result = monitor_run.run(monitor, "run-big", now=datetime.now(timezone.utc))
        self.assertIsNone(result.get("error"))
        self.assertGreaterEqual(result["new_alerts"], 300)
        self.assertLess((monitor / "agent" / "STATE.md").stat().st_size, STATE_READ_LIMIT)

    def test_report_groups_many_findings_by_project(self):
        findings = [{"code": "task_overdue", "scope": f"{pid}/t{n}", "detail": "d"}
                    for pid in ("alpha", "beta") for n in range(monitor_run.MAX_FINDINGS)]
        text = monitor_run.report_text("r", True, "complete", [], findings, "STATE.md", "x")
        self.assertIn(f"alpha {monitor_run.MAX_FINDINGS} (task_overdue {monitor_run.MAX_FINDINGS})", text)
        self.assertIn("beta", text)

    def test_overdue_reviewer_request_without_successor_is_reported(self):
        requests = [self.request("rev-9", "code-reviewer", iso(-100), due=iso(-99), link="https://github.com/o/r/pull/9")]
        tasks, _ = self.status(self.project(requests=requests))
        self.assertEqual(tasks[0]["status"], "review")
        self.assertIn(("task_overdue", "p/pr-9"), self.codes(tasks))

    def test_overdue_open_request_is_reported(self):
        tasks, _ = self.status(self.project(requests=[self.request("impl-1", "developer", iso(-20), due=iso(-18), link="codex://threads/x")]))
        self.assertIn(("task_overdue", "p/req-impl-1"), self.codes(tasks))

    def test_closed_issue_and_old_unfinished_outcome_are_not_live_stalls(self):
        self.fetched["issue"] = [{"number": 7, "state": "CLOSED", "title": "t", "updatedAt": iso(-9),
                                  "url": "https://github.com/o/r/issues/7", "labels": []}]
        requests = [self.request("rev-1", "code-reviewer", iso(-10), "answered", "partial", iso(-9)),
                    self.request("old-1", "developer", iso(-500), "answered", "partial", iso(-499), link="codex://threads/x")]
        tasks, summary = self.status(self.project(requests=requests, repository="o/r"))
        self.assertEqual(tasks, [])
        self.assertEqual(summary["by_status"], {"done": 1, "stale-partial": 1})

    def test_github_failure_makes_project_incomplete_in_the_monitor(self):
        def broken(repo):
            raise RuntimeError("offline")
        status_sources.github_fetch = broken
        row = self.project(requests=[self.request("impl-1", "developer", iso(-2))], repository="o/r")
        config = {"registry_path": str(self.dir / "registry.json"), "interval_minutes": 20}
        (self.dir / "registry.json").write_text(json.dumps({"projects": [row]}), encoding="utf-8")
        snapshot, stats = monitor_run.build(config, "run-1", NOW, [], True)
        self.assertEqual(snapshot["monitor_audit"]["coverage"], "partial")
        self.assertTrue(snapshot["monitor_audit"]["unavailable_scopes"][0].startswith("project:p (github unavailable"))
        self.assertEqual(stats["projects_covered"], 0)

    def test_monitor_covers_every_root_only_when_each_has_status(self):
        good = self.project("good", requests=[self.request("impl-1", "developer", iso(-2))])
        empty = self.project("empty")
        (self.dir / "registry.json").write_text(json.dumps({"projects": [good, empty]}), encoding="utf-8")
        config = {"registry_path": str(self.dir / "registry.json"), "interval_minutes": 20}
        snapshot, stats = monitor_run.build(config, "run-1", NOW, [], True)
        audit = snapshot["monitor_audit"]
        self.assertEqual((audit["coverage"], stats["projects_covered"]), ("partial", 1))
        self.assertIn("project:empty (UNKNOWN", audit["unavailable_scopes"][0])
        self.assertEqual(audit["project_status"]["good"]["by_status"], {"in-progress": 1})
        empty["tracked_work"] = "none"
        (self.dir / "registry.json").write_text(json.dumps({"projects": [good, empty]}), encoding="utf-8")
        snapshot, _ = monitor_run.build(config, "run-2", NOW, [], True)
        self.assertEqual(snapshot["monitor_audit"]["coverage"], "complete")


if __name__ == "__main__":
    unittest.main()

