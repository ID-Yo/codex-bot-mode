import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import status_sources as ss  # noqa: E402

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
APPROVED = ["state:ready", "state:active", "state:review", "state:blocked", "state:needs-owner", "state:backlog", "future",
            "type:epic", "type:story", "type:task", "priority:high", "priority:low"]


def row(number, labels, state="OPEN", pr=False):
    kind = "pull" if pr else "issues"
    return {"number": number, "state": state, "title": "t", "updatedAt": "2026-10-27T08:00:00Z",
            "url": f"https://github.com/o/r/{kind}/{number}", "labels": [{"name": n} for n in labels]}


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / ".bot-mode/local").mkdir(parents=True)

    def status(self, issues=(), prs=(), truncated=()):
        fetch = lambda repo: {"issue": list(issues), "pr": list(prs), "truncated": list(truncated)}
        return ss.project_status({"id": "p", "name": "p", "root": str(self.dir), "repository": "o/r"}, NOW, fetch=fetch)

    def findings(self, summary):
        return ss.label_findings({"p": summary["triage"]})

    def test_the_approved_set_is_exactly_the_twelve_names(self):
        self.assertEqual(sorted(ss.APPROVED_LABELS), sorted(APPROVED))
        self.assertEqual(len(ss.APPROVED_LABELS), 12)
        self.assertNotIn("priority:normal", ss.APPROVED_LABELS)

    def test_valid_sets_closed_and_merged_items_raise_nothing(self):
        issues = [row(1, ["state:active", "type:task", "priority:high"]), row(2, APPROVED[:3]), row(3, ["bug"], "CLOSED")]
        prs = [row(4, ["type:task"], pr=True), row(5, ["status:review"], "MERGED", pr=True), row(6, ["x"], "CLOSED", pr=True)]
        _, summary = self.status(issues, prs)
        self.assertEqual(self.findings(summary), [])
        self.assertEqual(summary["problems"], [])

    def test_issue_and_pull_request_with_mixed_labels_name_repo_item_and_only_the_bad_labels(self):
        issues = [row(7, ["state:active", "bug", "kind:task"])]
        prs = [row(8, ["state:review", "status:review"], pr=True), row(9, ["plan:future"], pr=True)]
        _, summary = self.status(issues, prs)
        found = {f["scope"]: f["detail"] for f in self.findings(summary)}
        self.assertEqual(sorted(found), ["p/issue-7", "p/pr-8", "p/pr-9"])
        self.assertIn("o/r issue #7", found["p/issue-7"])
        self.assertIn("bug, kind:task", found["p/issue-7"])
        self.assertNotIn("state:active", found["p/issue-7"])
        self.assertIn("o/r pr #8", found["p/pr-8"])
        self.assertIn("status:review", found["p/pr-8"])
        self.assertIn("nothing was changed", found["p/pr-9"])

    def test_findings_are_per_item_bounded_and_overflow_is_counted(self):
        issues = [row(n, ["bug"]) for n in range(1, 16)]
        _, summary = self.status(issues)
        found = self.findings(summary)
        self.assertEqual(len([f for f in found if f["scope"].startswith("p/issue-")]), ss.MAX_LABEL_FINDINGS)
        overflow = [f for f in found if f["code"] == "nonapproved_label_overflow"]
        self.assertEqual(len(overflow), 1)
        self.assertIn("5 more open item(s)", overflow[0]["detail"])

    def test_priority_low_is_allowed_but_normal_and_other_priorities_are_reported(self):
        issues = [row(1, ["state:active", "priority:low", "type:task"]), row(2, ["priority:normal"]), row(3, ["priority:low", "priority:medium"])]
        prs = [row(4, ["priority:low"], pr=True), row(5, ["priority:normal", "priority:high"], pr=True)]
        _, summary = self.status(issues, prs)
        found = {f["scope"]: f["detail"] for f in self.findings(summary)}
        self.assertEqual(sorted(found), ["p/issue-2", "p/issue-3", "p/pr-5"])
        self.assertIn("priority:normal", found["p/issue-2"])
        self.assertIn("priority:medium", found["p/issue-3"])
        self.assertNotIn("priority:low", found["p/issue-3"])
        self.assertNotIn("priority:high", found["p/pr-5"])

    def test_failed_or_truncated_fetch_is_reported_not_treated_as_clean(self):
        def broken(repo):
            raise RuntimeError("gh issue list failed")
        _, summary = ss.project_status({"id": "p", "name": "p", "root": str(self.dir), "repository": "o/r"}, NOW, fetch=broken)
        self.assertTrue(any("github unavailable" in p for p in summary["problems"]))
        self.assertEqual(self.findings(summary), [])
        _, summary = self.status([row(1, ["bug"])], truncated=["open pull requests capped at 1000"])
        self.assertTrue(any("coverage partial" in p and "pull requests" in p for p in summary["problems"]))
        self.assertEqual([f["scope"] for f in self.findings(summary)], ["p/issue-1"])

    def test_manager_signal_routes_the_new_code(self):
        import monitor_run
        registry = mock.Mock()
        registry.resolve_project_manager.return_value = {"status": "ok", "thread_id": "th-1", "bot": "manager"}
        alerts = [{"code": "nonapproved_label", "scope": "p/pr-8", "detail": "o/r pr #8 has non-approved label(s) x"}]
        with mock.patch.object(monitor_run, "room_registry", registry):
            deliveries, problems = monitor_run.manager_signals({"registry_path": "r.json"}, alerts)
        self.assertEqual((problems, len(deliveries)), ([], 1))
        self.assertIn("non-approved label", deliveries[0]["prompt"])
        self.assertIn("pr-8", deliveries[0]["prompt"])


class OpenPullRequestEnumerationTests(unittest.TestCase):
    def test_an_old_open_pull_request_beyond_the_recent_window_is_found(self):
        recent = [row(n, ["type:task"], "MERGED", pr=True) for n in range(100, 600)]
        old_open = row(12, ["wip"], pr=True)

        def fake_run(args, **kwargs):
            if args[1] == "pr" and "open" in args:
                out = [old_open]
            elif args[1] == "pr":
                out = recent
            elif "open" in args:
                out = []
            else:
                out = []
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")

        with mock.patch.object(ss.shutil, "which", return_value="gh"), mock.patch.object(ss.subprocess, "run", fake_run):
            rows = ss.github_fetch("o/r")
        self.assertIn(12, [r["number"] for r in rows["pr"]])
        self.assertEqual(rows["truncated"], [])
        found = ss.label_violations({("pull", str(r["number"])): r for r in rows["pr"]}, "o/r")
        self.assertEqual([(f["kind"], f["number"], f["labels"]) for f in found], [("pr", "12", ["wip"])])

    def test_a_capped_open_pull_request_list_is_marked_truncated(self):
        full = [row(n, ["type:task"], pr=True) for n in range(1, ss.OPEN_LIMIT + 1)]

        def fake_run(args, **kwargs):
            out = full if args[1] == "pr" and "open" in args else []
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")

        with mock.patch.object(ss.shutil, "which", return_value="gh"), mock.patch.object(ss.subprocess, "run", fake_run):
            rows = ss.github_fetch("o/r")
        self.assertEqual(rows["truncated"], [f"open pull requests capped at {ss.OPEN_LIMIT}"])


class OverflowDeliveryEndToEndTests(unittest.TestCase):
    """15 offending issues: findings -> Manager payload -> receipt -> checkpoint round trip -> dedup."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / ".bot-mode/local").mkdir(parents=True)
        issues = [row(n, ["bug"]) for n in range(1, 16)] + [row(40, ["state:active", "wip"], pr=True)]
        fetch = lambda repo: {"issue": issues[:15], "pr": issues[15:], "truncated": []}
        _, summary = ss.project_status({"id": "p", "name": "p", "root": str(self.dir), "repository": "o/r"}, NOW, fetch=fetch)
        self.findings = ss.label_findings({"p": summary["triage"]})
        self.state_file = self.dir / "STATE.md"

    def route(self, ledger):
        import monitor_run
        registry = mock.Mock()
        registry.resolve_project_manager.return_value = {"status": "ok", "thread_id": "th-1", "bot": "manager"}
        state = {"alert_ledger": ledger}
        fresh = [f for f in self.findings if f"{f['code']}:{f['scope']}" in ledger and ledger[f"{f['code']}:{f['scope']}"].get("manager") is None]
        with mock.patch.object(monitor_run, "room_registry", registry):
            return monitor_run.route_manager_signals({"registry_path": "r.json"}, state, self.findings, fresh)

    def test_ten_items_and_the_true_overflow_count_reach_the_manager_and_dedup_after_sent(self):
        import monitor_checkpoint
        codes = sorted({f["code"] for f in self.findings})
        self.assertEqual(codes, ["nonapproved_label", "nonapproved_label_overflow"])
        self.assertEqual(len(self.findings), 11)
        ledger = {f"{f['code']}:{f['scope']}": {"status": "new"} for f in self.findings}
        deliveries, problems, extra = self.route(ledger)
        self.assertEqual((problems, extra, len(deliveries)), ([], [], 1))
        prompt = deliveries[0]["prompt"]
        self.assertEqual(prompt.count("has non-approved label(s)"), 10)
        self.assertIn("6 more open item(s)", prompt)
        self.assertNotIn("and 1 more", prompt)
        self.assertTrue(all(e["manager"] == "pending" for e in ledger.values()))
        monitor_checkpoint.save_checkpoint(self.state_file, {"version": 1, "run_id": "r1", "audit_status": "complete", "coverage": "complete",
            "observed_at": "2026-10-27T12:00:00Z", "next_check_at": "2026-10-27T12:30:00Z", "active_alert_count": 11,
            "delivery_status": "sent", "unavailable_scopes": [], "alert_ledger": ledger})
        self.assertEqual(monitor_checkpoint.set_manager_delivery(self.state_file, "p", "sent"), 11)
        saved = monitor_checkpoint.load_checkpoint(self.state_file)["alert_ledger"]
        self.assertEqual({e["manager"] for e in saved.values()}, {"sent"})
        again, _, _ = self.route(saved)
        self.assertEqual(again, [])

if __name__ == "__main__":
    unittest.main()