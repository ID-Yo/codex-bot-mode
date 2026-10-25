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

    def test_routeport_portal_review_summary_fallback_clears_candidate_missing(self):
        # Replay of the real RoutePort case from issue #281
        sha = "92625269294c9f0185073dc7c4818c8cb2c9992c"
        summary = f"Code Review for portal changes: commit {sha} by author gw-web-portal"
        req = {
            "id": "req-RPT196-PORTAL-REVIEW-9262526-20261024",
            "from": "manager",
            "to": "code-reviewer",
            "summary": summary,
            "opened": iso(-2),
            "due": iso(-1),
            "state": "answered",
            "answered": iso(-1),
            "link": "https://github.com/example/routeport/pull/196"
        }
        tasks, _ = self.status(self.project(requests=[req]))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["candidate"], {"id": sha, "author": "gw-web-portal"})
        self.assertNotIn(("candidate_missing", "p/pr-196"), self.codes(tasks))

    def test_structured_candidate_and_author_in_request_clears_candidate_missing(self):
        sha = "1111222233334444555566667777888899990000"
        req = {
            "id": "req-197-review",
            "from": "manager",
            "to": "code-reviewer",
            "summary": "Review PR 197",
            "opened": iso(-2),
            "due": iso(-1),
            "state": "open",
            "candidate": sha,
            "author": "developer",
            "link": "https://github.com/example/routeport/pull/197"
        }
        tasks, _ = self.status(self.project(requests=[req]))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["candidate"], {"id": sha, "author": "developer"})
        self.assertNotIn(("candidate_missing", "p/pr-197"), self.codes(tasks))

    def test_summary_with_candidate_and_base_shas_picks_marked_candidate(self):
        cand_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        base_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        summary = f"Review PR 270 candidate {cand_sha} based on {base_sha} by author developer"
        req = {
            "id": "req-270-review",
            "from": "manager",
            "to": "code-reviewer",
            "summary": summary,
            "opened": iso(-2),
            "due": iso(-1),
            "state": "open",
            "link": "https://github.com/example/routeport/pull/270"
        }
        tasks, _ = self.status(self.project(requests=[req]))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["candidate"], {"id": cand_sha, "author": "developer"})
        self.assertNotIn(("candidate_missing", "p/pr-270"), self.codes(tasks))

    def test_summary_with_two_shas_without_marker_stays_candidate_missing(self):
        sha1 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        sha2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        summary = f"Review comparing {sha1} and {sha2} by author developer"
        req = {
            "id": "req-271-review",
            "from": "manager",
            "to": "code-reviewer",
            "summary": summary,
            "opened": iso(-2),
            "due": iso(-1),
            "state": "open",
            "link": "https://github.com/example/routeport/pull/271"
        }
        tasks, _ = self.status(self.project(requests=[req]))
        self.assertEqual(len(tasks), 1)
        self.assertIsNone(tasks[0]["candidate"])
        self.assertIn(("candidate_missing", "p/pr-271"), self.codes(tasks))

    def test_summary_with_commit_marker_picks_marked_sha(self):
        sha1 = "1111111111111111111111111111111111111111"
        sha2 = "2222222222222222222222222222222222222222"
        summary = f"Audit base {sha1} vs commit: {sha2} by author developer"
        req = {
            "id": "req-272-review",
            "from": "manager",
            "to": "code-reviewer",
            "summary": summary,
            "opened": iso(-2),
            "due": iso(-1),
            "state": "open",
            "link": "https://github.com/example/routeport/pull/272"
        }
        tasks, _ = self.status(self.project(requests=[req]))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["candidate"], {"id": sha2, "author": "developer"})
        self.assertNotIn(("candidate_missing", "p/pr-272"), self.codes(tasks))

    def test_remsup_97_pr_head_clears_candidate_missing(self):
        # Replay of issue #284 support case:
        # Issue #97 with label state:review; request links to issue #97; PR #98 is linked to issue #97.
        # PR #98 has headRefOid="ab148d99294c9f0185073dc7c4818c8cb2c9992c".
        head_sha = "ab148d99294c9f0185073dc7c4818c8cb2c9992c"
        self.fetched["issue"] = [{
            "number": 97, "state": "OPEN", "title": "Silver WebView2 support",
            "updatedAt": iso(-2), "url": "https://github.com/example/support/issues/97",
            "labels": [{"name": "state:review"}]
        }]
        self.fetched["pr"] = [{
            "number": 98, "state": "OPEN", "title": "Fixes #97: Silver WebView2",
            "updatedAt": iso(-1), "url": "https://github.com/example/support/pull/98",
            "headRefOid": head_sha
        }]
        requests = [
            self.request("sup-impl-97", "developer", iso(-4), "answered", "done", iso(-2),
                         link="https://github.com/example/support/issues/97"),
            self.request("sup-review-97", "code-reviewer", iso(-2), "open",
                         link="https://github.com/example/support/issues/97")
        ]
        tasks, _ = self.status(self.project(requests=requests, repository="example/support"))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["candidate"], {"id": head_sha, "author": "developer"})
        self.assertNotIn(("candidate_missing", "p/issue-97"), self.codes(tasks))

    def check_foreign_title(self, title):
        sha = "ab148d99294c9f0185073dc7c4818c8cb2c9992c"
        if True:
            if True:
                self.fetched["issue"] = [{"number": 97, "state": "OPEN", "title": "Support", "updatedAt": iso(-2),
                    "url": "https://github.com/example/support/issues/97", "labels": [{"name": "state:review"}]}]
                self.fetched["pr"] = [{"number": 98, "state": "OPEN", "title": title, "updatedAt": iso(-1),
                    "url": "https://github.com/example/support/pull/98", "headRefOid": sha}]
                requests = [
                    self.request("sup-impl-97", "developer", iso(-4), "answered", "done", iso(-2),
                                 link="https://github.com/example/support/issues/97"),
                    self.request("sup-review-97", "code-reviewer", iso(-2), "open",
                                 link="https://github.com/example/support/issues/97")]
                tasks, _ = self.status(self.project(requests=requests, repository="example/support"))
                self.assertNotEqual((tasks[0].get("candidate") or {}).get("id"), sha)

    def test_fixes_970_does_not_match_issue_97(self):
        self.check_foreign_title("Fixes #970: unrelated")

    def test_fixes_other_repo_97_does_not_match_issue_97(self):
        self.check_foreign_title("Fixes other/repo#97")

    def test_bare_mention_does_not_match_issue_97(self):
        self.check_foreign_title("Mentions #97 only")

    def linked_case(self, prs, **extra):
        self.fetched["issue"] = [{"number": 97, "state": "OPEN", "title": "Support", "updatedAt": iso(-2),
            "url": "https://github.com/example/support/issues/97", "labels": [{"name": "state:review"}]}]
        self.fetched["pr"] = [{"number": n, "state": state, "title": "Fixes #97: x", "updatedAt": iso(-1),
            "url": f"https://github.com/example/support/pull/{n}", "headRefOid": sha} for n, state, sha in prs]
        requests = [
            self.request("sup-impl-97", "developer", iso(-4), "answered", "done", iso(-2),
                         link="https://github.com/example/support/issues/97"),
            {**self.request("sup-review-97", "code-reviewer", iso(-2), "open",
                            link="https://github.com/example/support/issues/97"), **extra}]
        return self.status(self.project(requests=requests, repository="example/support"))[0]

    def check_ambiguous(self, prs):
        tasks = self.linked_case(prs)
        self.assertIsNone(tasks[0].get("candidate"))
        self.assertIn(("candidate_missing", "p/issue-97"), self.codes(tasks))

    def test_two_open_prs_with_different_heads_are_ambiguous(self):
        self.check_ambiguous([(98, "OPEN", "a" * 40), (99, "OPEN", "b" * 40)])

    def test_ambiguity_does_not_depend_on_pr_order(self):
        self.check_ambiguous([(99, "OPEN", "b" * 40), (98, "OPEN", "a" * 40)])

    def test_structured_candidate_wins_over_ambiguous_prs(self):
        tasks = self.linked_case([(98, "OPEN", "a" * 40), (99, "OPEN", "b" * 40)], candidate="c" * 40)
        self.assertEqual(tasks[0]["candidate"]["id"], "c" * 40)

    def test_single_open_pr_among_closed_ones_is_the_candidate(self):
        tasks = self.linked_case([(90, "MERGED", "a" * 40), (99, "OPEN", "b" * 40)])
        self.assertEqual(tasks[0]["candidate"]["id"], "b" * 40)

    def test_active_issue_with_old_closed_pr_and_live_implementation_stays_in_progress(self):
        self.fetched["issue"] = [{"number": 97, "state": "OPEN", "title": "Support", "updatedAt": iso(-2),
            "url": "https://github.com/example/support/issues/97", "labels": [{"name": "state:active"}]}]
        self.fetched["pr"] = [{"number": 90, "state": "CLOSED", "title": "Fixes #97: old attempt", "updatedAt": iso(-9),
            "url": "https://github.com/example/support/pull/90", "headRefOid": "a" * 40}]
        requests = [self.request("sup-impl-97", "developer", iso(-1), "open",
                                 link="https://github.com/example/support/issues/97")]
        tasks, _ = self.status(self.project(requests=requests, repository="example/support"))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "in-progress")

    def test_review_label_with_live_implementation_request_and_closed_pr_stays_open(self):
        self.fetched["issue"] = [{"number": 97, "state": "OPEN", "title": "Support", "updatedAt": iso(-2),
            "url": "https://github.com/example/support/issues/97", "labels": [{"name": "state:review"}]}]
        self.fetched["pr"] = [{"number": 90, "state": "CLOSED", "title": "Fixes #97: old attempt", "updatedAt": iso(-9),
            "url": "https://github.com/example/support/pull/90", "headRefOid": "a" * 40}]
        requests = [self.request("sup-impl-97", "developer", iso(-1), "open",
                                 link="https://github.com/example/support/issues/97")]
        tasks, _ = self.status(self.project(requests=requests, repository="example/support"))
        self.assertEqual(len(tasks), 1)

    def check_review_named_to_developer(self, rid):
        self.fetched["issue"] = [{"number": 97, "state": "OPEN", "title": "Support", "updatedAt": iso(-2),
            "url": "https://github.com/example/support/issues/97", "labels": [{"name": "state:review"}]}]
        self.fetched["pr"] = [{"number": 90, "state": "CLOSED", "title": "Fixes #97: old", "updatedAt": iso(-9),
            "url": "https://github.com/example/support/pull/90", "headRefOid": "a" * 40}]
        requests = [self.request(rid, "developer", iso(-1), "open",
                                 link="https://github.com/example/support/issues/97")]
        tasks, _ = self.status(self.project(requests=requests, repository="example/support"))
        self.assertEqual(len(tasks), 1)

    def check_developer_like_bot_keeps_task_open(self, bot):
        self.fetched["issue"] = [{"number": 97, "state": "OPEN", "title": "Support", "updatedAt": iso(-2),
            "url": "https://github.com/example/support/issues/97", "labels": [{"name": "state:review"}]}]
        self.fetched["pr"] = [{"number": 90, "state": "CLOSED", "title": "Fixes #97: old", "updatedAt": iso(-9),
            "url": "https://github.com/example/support/pull/90", "headRefOid": "a" * 40}]
        requests = [self.request("sup-impl-97", bot, iso(-1), "open",
                                 link="https://github.com/example/support/issues/97")]
        tasks, _ = self.status(self.project(requests=requests, repository="example/support"))
        self.assertEqual(len(tasks), 1)

    def test_preview_developer_keeps_the_task_open(self):
        self.check_developer_like_bot_keeps_task_open("preview-developer")

    def test_nonverifier_developer_keeps_the_task_open(self):
        self.check_developer_like_bot_keeps_task_open("nonverifier-developer")

    def test_review_fix_request_to_developer_keeps_the_task_open(self):
        self.check_review_named_to_developer("sup-review-fix-97")

    def test_correction_review_feedback_request_to_developer_keeps_the_task_open(self):
        self.check_review_named_to_developer("sup-correction-review-feedback-97")

    def test_open_issue_with_only_merged_or_closed_prs_is_finished(self):
        tasks = self.linked_case([(98, "MERGED", "a" * 40), (99, "CLOSED", "b" * 40)])
        self.assertEqual(tasks, [])

    def test_pr_linked_in_request_url_resolves_candidate_head_sha(self):
        head_sha = "ab148d99294c9f0185073dc7c4818c8cb2c9992c"
        self.fetched["pr"] = [{
            "number": 98, "state": "OPEN", "title": "Silver WebView2",
            "updatedAt": iso(-1), "url": "https://github.com/example/support/pull/98",
            "headRefOid": head_sha
        }]
        requests = [
            self.request("sup-impl-98", "developer", iso(-4), "answered", "done", iso(-2),
                         link="https://github.com/example/support/pull/98"),
            self.request("sup-review-98", "code-reviewer", iso(-2), "open",
                         link="https://github.com/example/support/pull/98")
        ]
        tasks, _ = self.status(self.project(requests=requests, repository="example/support"))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["candidate"], {"id": head_sha, "author": "developer"})
        self.assertNotIn(("candidate_missing", "p/pr-98"), self.codes(tasks))

    def test_merged_or_closed_linked_pr_raises_no_candidate_signal(self):
        # When linked PR is merged or closed and work is finished, stage is done and no candidate_missing
        self.fetched["issue"] = [{
            "number": 97, "state": "CLOSED", "title": "Silver WebView2",
            "updatedAt": iso(-1), "url": "https://github.com/example/support/issues/97",
            "labels": []
        }]
        self.fetched["pr"] = [{
            "number": 98, "state": "MERGED", "title": "Fixes #97: Silver WebView2",
            "updatedAt": iso(-1), "url": "https://github.com/example/support/pull/98",
            "headRefOid": "ab148d99294c9f0185073dc7c4818c8cb2c9992c"
        }]
        requests = [
            self.request("sup-review-97", "code-reviewer", iso(-2), "answered", "done", iso(-1),
                         link="https://github.com/example/support/pull/98")
        ]
        tasks, _ = self.status(self.project(requests=requests, repository="example/support"))
        self.assertEqual(tasks, [])

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

