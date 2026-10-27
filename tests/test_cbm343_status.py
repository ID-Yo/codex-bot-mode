import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import status_sources as ss  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def iso(hours):
    return (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def state(status="idle", in_progress=False, starts=()):
    return {"status": status, "in_progress": in_progress, "starts": list(starts)}


class Cbm343Tests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fetched = {"issue": [], "pr": []}
        original = ss.github_fetch
        ss.github_fetch = lambda repo: self.fetched
        self.addCleanup(setattr, ss, "github_fetch", original)

    def project(self, requests=None, participants=()):
        root = self.dir / "p"
        (root / ".bot-mode" / "local").mkdir(parents=True, exist_ok=True)
        (root / ".bot-mode" / "local" / "requests.json").write_text(
            json.dumps({"schema": 1, "requests": requests or []}), encoding="utf-8")
        (root / ".bot-mode" / "roster.json").write_text(json.dumps({"participants": list(participants)}), encoding="utf-8")
        return {"id": "p", "name": "p", "root": str(root), "repository": "o/r"}

    def issue(self, number, labels=(), state="OPEN", updated=-2):
        self.fetched["issue"].append({"number": number, "state": state, "title": "t", "updatedAt": iso(updated),
                                      "url": f"https://github.com/o/r/issues/{number}", "labels": [{"name": n} for n in labels]})

    def request(self, rid, number, state="open", outcome=None, answered=None, to="developer"):
        row = {"id": rid, "from": "manager", "to": to, "summary": rid, "link": f"https://github.com/o/r/issues/{number}",
               "opened": iso(-5), "due": iso(1), "state": state, "escalations": []}
        if state == "answered":
            row.update(outcome=outcome, answered=answered)
        return row

    def status(self, project):
        return ss.project_status(project, NOW)

    def test_unlabelled_open_issue_is_untriaged_never_ready(self):
        self.issue(1)
        tasks, summary = self.status(self.project())
        self.assertEqual([i["id"] for i in summary["triage"]["untriaged"]], ["p/issue-1"])
        self.assertFalse(any(t["status"] == "ready" for t in tasks))
        self.assertEqual(summary["ready_work"], [])

    def test_answered_done_on_open_issue_awaits_next_stage_after_grace(self):
        self.issue(2)
        req = self.request("impl-2", 2, "answered", "done", iso(-1))
        _, summary = self.status(self.project([req]))
        self.assertEqual([i["id"] for i in summary["triage"]["awaiting-next-stage"]], ["p/issue-2"])
        codes = lambda now: {f["code"] for f in ss.triage_findings({"p": summary["triage"]}, now)}
        self.assertNotIn("stage_done_no_next", codes(NOW - timedelta(minutes=45)))
        self.assertIn("stage_done_no_next", codes(NOW))

    def test_closed_issue_is_done_and_open_issue_is_not_finished_by_merged_pr(self):
        self.issue(3, state="CLOSED")
        tasks, summary = self.status(self.project())
        self.assertEqual(tasks, [])
        self.assertEqual(summary["triage"]["untriaged"], [])
        self.fetched = {"issue": [], "pr": [{"number": 9, "state": "MERGED", "title": "Fixes #4: x", "updatedAt": iso(-1),
                                             "url": "https://github.com/o/r/pull/9", "headRefOid": "a" * 40}]}
        self.issue(4, ["state:review"])
        tasks, _ = self.status(self.project())
        self.assertEqual([t["status"] for t in tasks], ["review"])

    def test_state_ready_label_alone_is_approval_unknown_not_idle(self):
        self.issue(5, ["state:ready", "bot:developer"])
        bots = [{"bot": "development-developer", "role": "Developer", "status": "active", "thread_id": "t1"}]
        _, summary = self.status(self.project(participants=bots))
        self.assertEqual([i["id"] for i in summary["triage"]["ready_approval_unknown"]], ["p/issue-5"])
        self.assertEqual(summary["ready_work"], [])

    def test_open_request_creates_no_invented_ack_or_start(self):
        self.issue(6, ["state:active"])
        tasks, _ = self.status(self.project([self.request("impl-6", 6)]))
        handoff = tasks[0]["handoffs"][0]
        self.assertIsNone(handoff["acknowledged_at"])
        self.assertIsNone(handoff["started_at"])

    def test_state_blocked_label_maps_to_blocked(self):
        self.issue(9, ["state:blocked"])
        tasks, _ = self.status(self.project())
        self.assertEqual([t["status"] for t in tasks], ["blocked"])
    def test_state_blocked_wins_over_active_and_keeps_its_own_reason(self):
        self.issue(10, ["state:active", "state:blocked"])
        self.issue(11, ["state:needs-owner"])
        tasks, _ = self.status(self.project())
        got = {t["id"]: t for t in tasks}
        self.assertEqual({k: v["status"] for k, v in got.items()}, {"p/issue-10": "blocked", "p/issue-11": "blocked"})
        self.assertIn("state:blocked", got["p/issue-10"]["blocker"]["reason"])
        self.assertIn("state:needs-owner", got["p/issue-11"]["blocker"]["reason"])
    def approved(self, scope="role:Developer"):
        return {"by": "owner", "at": iso(-2), "scope": scope, "evidence": "https://github.com/o/r/issues/7#c1"}

    def test_valid_approval_needs_all_fields_and_a_past_time(self):
        self.assertIsNotNone(ss.valid_approval(self.approved(), NOW))
        self.assertIsNone(ss.valid_approval({**self.approved(), "evidence": ""}, NOW))
        self.assertIsNone(ss.valid_approval({**self.approved(), "at": iso(2)}, NOW))
        self.assertIsNone(ss.valid_approval(None, NOW))

    def test_candidates_respect_roster_busy_and_pause(self):
        row = self.project(participants=[
            {"bot": "development-developer", "role": "Developer", "status": "active"},
            {"bot": "other-developer", "role": "Developer", "status": "active"}])
        root = row["root"]
        ready = {"id": "p/issue-7", "status": "ready", "start_approval": self.approved(), "updated_at": iso(-2)}
        triage = {"ready_approval_unknown": []}
        busy_request = {"state": "open", "to": "development-developer"}
        out = ss.ready_candidates(root, [ready], [busy_request], triage, NOW)
        self.assertEqual(out[0]["eligible"], ["other-developer"])
        paused = {"id": "p/issue-8", "status": "paused", "executor": "other-developer"}
        out = ss.ready_candidates(root, [ready, paused], [busy_request], triage, NOW)
        self.assertEqual(out, [])
        global_id = {**ready, "start_approval": self.approved("bot:nobody-else")}
        self.assertEqual(ss.ready_candidates(root, [global_id], [], triage, NOW), [])
        self.assertEqual([i["id"] for i in triage["ready_approval_unknown"]], ["p/issue-7"])

    def item(self):
        return {"id": "p/issue-7", "approval": ss.valid_approval(self.approved(), NOW),
                "eligible": ["development-developer"], "since": iso(-2)}

    def findings(self, observation, root=None, since=None):
        row = self.project(participants=[{"bot": "development-developer", "role": "Developer", "status": "active", "thread_id": "t1"}])
        item = self.item()
        if since:
            item["since"] = since
        entry = {"root": row["root"], "ready": [item]}
        return {f["code"] for f in ss.ready_findings("p", entry, NOW, lambda tid, roots: observation)}

    def test_idle_approved_bot_triggers_finding(self):
        self.assertEqual(self.findings(("state", state())), {"ready_not_dispatched"})

    def test_not_idle_states_are_unknown_not_findings(self):
        for obs in (("state", state("notLoaded")), ("state", state("completed")), ("state", state(in_progress=True)),
                    ("state", state(starts=[NOW - timedelta(hours=1)])), ("error", "unreadable"),
                    ("relay", "relay down")):
            self.assertEqual(self.findings(obs), {"ready_activity_unknown"}, obs)

    def test_before_grace_nothing_is_reported(self):
        self.assertEqual(self.findings(("state", state()), since=iso(-0.1)), set())


def _entry(root, bots):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".bot-mode").mkdir(exist_ok=True)
    people = [{"bot": b, "name": b, "role": "Developer", "status": "active", "thread_id": f"0000000{i}-2222-3333-4444-555555555555"}
              for i, b in enumerate(bots)]
    (root / ".bot-mode" / "roster.json").write_text(json.dumps({"participants": people}), encoding="utf-8")
    approval = {"by": "owner", "at": iso(-2), "scope": "role:Developer", "evidence": "e"}
    return {"root": str(root), "items": [], "ready": [{"id": "p/issue-7", "approval": approval, "eligible": list(bots), "since": iso(-2)}]}


class ReadinessProbeTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_findings(self, entry, relay):
        return ss.request_start_findings({"p": entry}, NOW, relay)

    def test_explicit_project_pause_suppresses_ready_work(self):
        row = {"id": "p", "name": "p", "root": str(self.dir / "p"), "repository": "o/r"}
        _entry(self.dir / "p", ["a"])
        (self.dir / "p" / ".bot-mode" / "PAUSED").write_text("paused", encoding="utf-8")
        ready = {"id": "p/issue-7", "status": "ready", "start_approval": {"by": "o", "at": iso(-2), "scope": "role:Developer", "evidence": "e"}}
        self.assertEqual(ss.ready_candidates(row["root"], [ready], [], {"ready_approval_unknown": []}, NOW), [])
        entry = _entry(self.dir / "p", ["a"])
        calls = []
        found = self.run_findings(entry, lambda *a, **k: calls.append(a))
        self.assertEqual((found, calls), ([], []))

    def test_http_4xx_on_some_threads_is_not_a_relay_outage_and_scanning_continues(self):
        import io
        import urllib.error
        entry = _entry(self.dir / "p", ["a", "b", "c", "d"])
        idle = {"schemaVersion": 1, "page": {"hasMore": False}, "thread": {"id": "x", "status": {"type": "idle"}},
                "turns": [{"id": "t", "status": "completed", "startedAt": 1, "completedAt": 2}]}

        def relay(root, name, arguments, **kwargs):
            if not arguments["threadId"].endswith("3-2222-3333-4444-555555555555") and not arguments["threadId"].startswith("00000003"):
                raise urllib.error.HTTPError("http://127.0.0.1/call", 400, "Bad Request", {}, io.BytesIO(b"{}"))
            return {"result": {**idle, "thread": {"id": arguments["threadId"], "status": {"type": "idle"}}}}

        found = self.run_findings(entry, relay)
        codes = {f["code"] for f in found}
        self.assertNotIn("desktop_relay_unavailable", codes)
        self.assertIn("ready_not_dispatched", codes)

class ManagerRoutingAndCoverageTests(unittest.TestCase):
    CODES = ("issue_untriaged", "stage_done_no_next", "ready_approval_unknown", "ready_not_dispatched", "ready_activity_unknown")

    def test_new_findings_reach_the_project_manager(self):
        import monitor_run
        from unittest import mock
        registry = mock.Mock()
        registry.resolve_project_manager.return_value = {"status": "ok", "thread_id": "th-1", "bot": "manager"}
        alerts = [{"code": c, "scope": "p" if c.endswith("unknown") and c != "ready_approval_unknown" else "p/issue-7", "detail": "d"}
                  for c in self.CODES]
        with mock.patch.object(monitor_run, "room_registry", registry):
            deliveries, findings = monitor_run.manager_signals({"registry_path": "r.json"}, alerts)
        self.assertEqual(findings, [])
        self.assertEqual(len(deliveries), 1)
        for code in self.CODES:
            self.assertIn(monitor_run.SIGNAL_CODES[code][0], deliveries[0]["prompt"])
        self.assertIn("Informational", monitor_run.SIGNAL_CODES["ready_activity_unknown"][2])

    def test_naive_approval_time_is_not_valid(self):
        good = {"by": "o", "at": iso(-2), "scope": "role:Developer", "evidence": "e"}
        self.assertIsNotNone(ss.valid_approval(good, NOW))
        self.assertIsNone(ss.valid_approval({**good, "at": "2026-10-26T09:00:00"}, NOW))

    def test_capped_open_enumeration_is_reported_as_partial(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / ".bot-mode" / "local").mkdir(parents=True)
        row = {"id": "p", "name": "p", "root": str(root), "repository": "o/r"}
        _, summary = ss.project_status(row, NOW, fetch=lambda repo: {"issue": [], "pr": [], "truncated": ["open issues capped at 1000"]})
        self.assertTrue(any("coverage partial" in p for p in summary["problems"]))
        _, summary = ss.project_status(row, NOW, fetch=lambda repo: {"issue": [], "pr": []})
        self.assertEqual(summary["problems"], [])

class ReadyAssociationTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "work/tasks").mkdir(parents=True)
        (self.dir / ".bot-mode/local").mkdir(parents=True)
        (self.dir / ".bot-mode/roster.json").write_text(json.dumps({"participants": [
            {"bot": "dev-1", "role": "Developer", "status": "active"}]}), encoding="utf-8")
        self.rows = []

    def record(self, name, authority="https://github.com/o/r/issues/339", approval=True, at=-3, evidence="https://github.com/o/r/issues/339#issuecomment-1", header="Authority"):
        lines = [f"# {name}", "", f"- {header}: {authority}", "- State: ready", "- Assigned executor: dev-1"]
        if approval:
            lines += ["- Start approval by: manager", f"- Start approval at: {iso(at)}", "- Start approval scope: role:Developer",
                      f"- Start approval evidence: {evidence}"]
        (self.dir / "work/tasks" / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def issue(self, number=339, state="OPEN", labels=("state:ready",)):
        self.rows.append({"number": number, "state": state, "title": "t", "updatedAt": iso(-5),
                          "url": f"https://github.com/o/r/issues/{number}", "labels": [{"name": n} for n in labels]})

    def run_status(self):
        fetch = lambda repo: {"issue": self.rows, "pr": []}
        tasks, summary = ss.project_status({"id": "p", "name": "p", "root": str(self.dir), "repository": "o/r"}, NOW, fetch=fetch)
        return tasks, summary, [c["id"] for c in summary["ready_work"]], [i["id"] for i in summary["triage"]["ready_approval_unknown"]]

    def test_real_duplicate_becomes_one_approved_github_candidate(self):
        self.record("CBM-339"); self.issue()
        _, _, candidates, unknown = self.run_status()
        self.assertEqual((candidates, unknown), (["p/issue-339"], []))

    def test_wrong_repo_pull_request_and_disagreeing_lines_do_not_associate(self):
        self.issue()
        for authority in ("https://github.com/other/r/issues/339", "https://github.com/o/r/pull/339", "https://github.com/o/r/issues/340"):
            self.record("CBM-339", authority)
            _, _, candidates, unknown = self.run_status()
            self.assertEqual(unknown, ["p/issue-339"], authority)
            self.assertNotIn("p/issue-339", candidates)
        (self.dir / "work/tasks/CBM-339.md").write_text(
            "- Authority: https://github.com/o/r/issues/339\n- Tracking: https://github.com/o/r/issues/340\n- State: ready\n", encoding="utf-8")
        self.assertIn("p/issue-339", self.run_status()[3])

    def test_github_closed_or_not_ready_wins_and_nothing_is_revived(self):
        self.record("CBM-339")
        self.issue(state="CLOSED")
        self.assertEqual(self.run_status()[2:], ([], []))
        self.rows.clear()
        self.issue(labels=("state:active",))
        tasks, _, candidates, unknown = self.run_status()
        self.assertEqual((candidates, unknown), ([], []))
        self.assertEqual({t["id"]: t["status"] for t in tasks}["p/issue-339"], "in-progress")

    def test_missing_invalid_future_or_conflicting_approvals_stay_unknown(self):
        self.issue()
        cases = {"missing": [dict(name="A", approval=False)],
                 "no evidence": [dict(name="A", evidence="")],
                 "future": [dict(name="A", at=2)],
                 "conflict": [dict(name="A"), dict(name="B", evidence="https://github.com/o/r/issues/339#issuecomment-2")]}
        for label, records in cases.items():
            for f in (self.dir / "work/tasks").glob("*.md"):
                f.unlink()
            for kw in records:
                self.record(**kw)
            _, _, candidates, unknown = self.run_status()
            self.assertEqual((candidates, unknown), ([], ["p/issue-339"]), label)

class GithubRetryTests(unittest.TestCase):
    def run_fetch(self, outcomes):
        from unittest import mock
        import subprocess
        calls, sleeps = [], []

        def fake_run(args, **kwargs):
            calls.append(args)
            code, err = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            return subprocess.CompletedProcess(args, code, "[]", err)

        with mock.patch.object(ss.shutil, "which", return_value="gh"), mock.patch.object(ss.subprocess, "run", fake_run), \
                mock.patch.object(ss.time, "sleep", lambda s: sleeps.append(s)):
            try:
                ss.github_fetch("o/r")
                return calls, sleeps, None
            except RuntimeError as error:
                return calls, sleeps, str(error)

    def test_one_http_5xx_is_retried_once_after_a_short_pause(self):
        calls, sleeps, error = self.run_fetch([(1, "HTTP 502: 502 Bad Gateway (https://api.github.com/graphql)"), (0, "")])
        self.assertIsNone(error)
        self.assertEqual(sleeps, [ss.GH_RETRY_SECONDS])
        self.assertEqual(len(calls), 5)  # issues (2 tries), prs, open issues, open prs

    def test_a_second_5xx_or_a_non_5xx_failure_still_fails_without_more_retries(self):
        calls, sleeps, error = self.run_fetch([(1, "HTTP 502: Bad Gateway")])
        self.assertEqual((error, len(calls), len(sleeps)), ("gh issue list failed", 2, 1))
        calls, sleeps, error = self.run_fetch([(1, "HTTP 401: Bad credentials")])
        self.assertEqual((error, len(calls), sleeps), ("gh issue list failed", 1, []))

if __name__ == "__main__":
    unittest.main()
