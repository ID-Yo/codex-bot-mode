"""Retry or skip an unconfirmed outbox row from the Office panel (#424, S4-E1): the decision goes only through the project's
own outbox.py, every refusal is a refusal, and every attempt is journaled without report text or paths."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.team_room import outbox, outbox_actions, supervisor_status

SECRET_TEXT = "SECRET REPORT TEXT"
SECRET_ERROR = "relay timed out at C:/secret/path/relay.json"
NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=timezone.utc)

FAKE_OUTBOX = r'''import json, sys, time
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
with (here / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
config = json.loads((here / "fake.json").read_text(encoding="utf-8"))
mode = config["mode"]
if mode == "old":
    sys.stderr.write("usage: outbox.py [-h]\noutbox.py: error: argument action: invalid choice\n")
    sys.exit(2)
if mode == "sleep" or (mode == "sleep_resolve" and args[2] == "resolve"):
    time.sleep(10)
command = args[2]
if command == "list":
    sys.stdout.write(config["list_out"] if mode == "garbage" else json.dumps(config["rows"].get(args[4], [])))
    sys.exit(0)
sys.stdout.write(config["resolve_out"])
sys.stderr.write(config["resolve_err"])
sys.exit(config["resolve_code"])
'''


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.office, self.project = base / "office", base / "alpha"
        self.office.mkdir()
        self.project.mkdir()
        self.put_status()

    def put_status(self, at=None, projects=None):
        data = {"schema": 1, "at": (at or NOW - timedelta(seconds=30)).isoformat(), "interval_seconds": 120, "problems": [],
                "projects": projects if projects is not None else [{"project": str(self.project), "name": "alpha"}]}
        supervisor_status.write(self.office / ".bot-mode" / "supervisor-status.json", data)

    def real_outbox(self, with_pending=False):
        """The real outbox.py installed in the project, with one unconfirmed, one needs-attention and one delivered row.
        A pending row makes the real resolve start a pump, so it exists only for tests that never reach resolve."""
        outbox.install_cli(self.project)
        (self.project / ".bot-mode" / "outbox").mkdir(parents=True, exist_ok=True)
        database = outbox.OutboxDB(self.project / ".bot-mode" / "outbox" / "queue.sqlite")
        self.rows = {}
        for name in ("ambiguous", "needs-attention", "delivered") + (("pending",) if with_pending else ()):
            row = database.enqueue("developer", "manager", "thread", SECRET_TEXT + " " + name, dedupe_key=name)
            if name in ("ambiguous", "needs-attention"):
                database.mark_ambiguous(row, SECRET_ERROR)
            if name == "needs-attention":
                with database.conn:
                    database.conn.execute("UPDATE outbox SET state = 'needs-attention' WHERE id = ?", (row,))
            if name == "delivered":
                database.mark_delivered_many([row])
            self.rows[name] = row
        database.close()

    def fake_outbox(self, **config):
        """A stand-in outbox.py that records every call it gets."""
        folder = self.project / ".bot-mode"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "outbox.py").write_text(FAKE_OUTBOX, encoding="utf-8")
        row = {"id": "a" * 32, "state": "ambiguous", "sender_bot": "developer", "target_bot": "manager",
               "created_at": (NOW - timedelta(minutes=5)).isoformat(), "error": SECRET_ERROR, "text": SECRET_TEXT}
        settings = {"mode": "ok", "rows": {"ambiguous": [row], "needs-attention": []}, "resolve_out": "Resolved " + "a" * 32 + ": pending.\n",
                    "resolve_err": "", "resolve_code": 0, "list_out": ""}
        settings.update(config)
        (folder / "fake.json").write_text(json.dumps(settings), encoding="utf-8")
        (folder / "calls.jsonl").unlink(missing_ok=True)

    def calls(self):
        path = self.project / ".bot-mode" / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def resolve_calls(self):
        return [call for call in self.calls() if call[2] == "resolve"]

    def journal(self):
        path = self.office / ".bot-mode" / outbox_actions.JOURNAL
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def decide(self, project="alpha", row_id="a" * 32, action="retry", confirm=True):
        return outbox_actions.resolve(self.office, project, row_id, action, confirm, NOW)

    def refused(self, status, reason, **call):
        with self.assertRaises(outbox_actions.Refused) as caught:
            self.decide(**call)
        self.assertEqual((caught.exception.status, caught.exception.reason), (status, reason))
        return caught.exception

    def state_of(self, row_id):
        database = outbox.OutboxDB(self.project / ".bot-mode" / "outbox" / "queue.sqlite")
        try:
            return database.conn.execute("SELECT state FROM outbox WHERE id = ?", (row_id,)).fetchone()[0]
        finally:
            database.close()


class ListingTests(Fixture):
    def test_only_unconfirmed_rows_are_listed_and_only_safe_fields(self):
        self.real_outbox()
        rows = outbox_actions.waiting_rows(self.office, "alpha", NOW)
        self.assertEqual({row["id"]: row["state"] for row in rows},
                         {self.rows["ambiguous"]: "ambiguous", self.rows["needs-attention"]: "needs-attention"})
        for row in rows:
            self.assertEqual(sorted(row), ["age_seconds", "id", "sender", "state", "target"])
        shown = json.dumps(rows)
        for secret in (SECRET_TEXT, "C:/secret", "relay timed out", str(self.project)):
            self.assertNotIn(secret, shown)

    def test_an_unknown_project_or_a_stale_or_missing_status_lists_nothing(self):
        self.fake_outbox()
        with self.assertRaises(outbox_actions.Refused) as unknown:
            outbox_actions.waiting_rows(self.office, "beta", NOW)
        self.assertEqual((unknown.exception.status, unknown.exception.reason), (404, "unknown_project"))
        self.put_status(at=NOW - timedelta(hours=1))
        with self.assertRaises(outbox_actions.Refused) as stale:
            outbox_actions.waiting_rows(self.office, "alpha", NOW)
        self.assertEqual(stale.exception.reason, "status_unknown")
        (self.office / ".bot-mode" / "supervisor-status.json").unlink()
        with self.assertRaises(outbox_actions.Refused) as missing:
            outbox_actions.waiting_rows(self.office, "alpha", NOW)
        self.assertEqual(missing.exception.reason, "status_unknown")
        self.assertEqual(self.calls(), [])

    def test_two_projects_with_one_name_are_refused(self):
        self.fake_outbox()
        self.put_status(projects=[{"project": str(self.project), "name": "alpha"}, {"project": str(self.project), "name": "alpha"}])
        with self.assertRaises(outbox_actions.Refused) as caught:
            outbox_actions.waiting_rows(self.office, "alpha", NOW)
        self.assertEqual(caught.exception.reason, "project_not_unique")

    def test_an_old_outbox_is_a_clear_refusal(self):
        self.fake_outbox(mode="old")
        with self.assertRaises(outbox_actions.Refused) as caught:
            outbox_actions.waiting_rows(self.office, "alpha", NOW)
        self.assertEqual((caught.exception.status, caught.exception.reason), (409, "outbox_too_old"))
        self.refused(409, "outbox_too_old")
        self.assertEqual(self.resolve_calls(), [])

    def test_an_unreadable_list_is_a_refusal_and_never_an_empty_list(self):
        self.fake_outbox(mode="garbage", list_out="not json")
        with self.assertRaises(outbox_actions.Refused) as caught:
            outbox_actions.waiting_rows(self.office, "alpha", NOW)
        self.assertEqual((caught.exception.status, caught.exception.reason), (503, "list_unreadable"))

    def test_the_list_says_when_it_may_be_cut_at_the_newest_rows(self):
        def rows(count):
            return [{"id": format(index, "032x"), "state": "ambiguous", "sender_bot": "developer", "target_bot": "manager",
                     "created_at": (NOW - timedelta(minutes=index)).isoformat()} for index in range(1, count + 1)]

        self.fake_outbox(rows={"ambiguous": rows(outbox_actions.LIST_LIMIT - 1), "needs-attention": []})
        self.assertFalse(outbox_actions.list_is_cut(outbox_actions.waiting_rows(self.office, "alpha", NOW)))
        self.fake_outbox(rows={"ambiguous": rows(outbox_actions.LIST_LIMIT), "needs-attention": []})
        listed = outbox_actions.waiting_rows(self.office, "alpha", NOW)
        self.assertEqual(len(listed), outbox_actions.LIST_LIMIT)
        self.assertTrue(outbox_actions.list_is_cut(listed))

    def test_a_project_without_an_installed_outbox_is_refused(self):
        with self.assertRaises(outbox_actions.Refused) as caught:
            outbox_actions.waiting_rows(self.office, "alpha", NOW)
        self.assertEqual(caught.exception.reason, "no_outbox")


class DecisionTests(Fixture):
    def test_retry_runs_exactly_one_resolve_with_fixed_arguments(self):
        self.fake_outbox()
        result = self.decide(action="retry")
        self.assertEqual((result["state"], result["action"], result["journal_written"]), ("pending", "retry", True))
        self.assertEqual(self.resolve_calls(), [["--project", str(self.project), "resolve", "a" * 32, "--as", "retry", "--note", "office panel"]])

    def test_skip_uses_the_skipped_resolution(self):
        self.fake_outbox(resolve_out="Resolved " + "a" * 32 + ": skipped.\n")
        self.assertEqual(self.decide(action="skip")["state"], "skipped")
        self.assertEqual(self.resolve_calls()[0][4:6], ["--as", "skipped"])

    def test_the_real_outbox_skips_a_row_and_keeps_the_note(self):
        self.real_outbox()
        result = self.decide(row_id=self.rows["needs-attention"], action="skip")
        self.assertEqual(result["state"], "skipped")
        self.assertEqual(self.state_of(self.rows["needs-attention"]), "skipped")
        self.assertEqual(self.state_of(self.rows["ambiguous"]), "ambiguous")
        database = outbox.OutboxDB(self.project / ".bot-mode" / "outbox" / "queue.sqlite")
        try:
            note = database.conn.execute("SELECT error FROM outbox WHERE id = ?", (self.rows["needs-attention"],)).fetchone()[0]
        finally:
            database.close()
        self.assertIn("office panel", note)

    def test_every_refusal_starts_no_resolve_and_is_journaled_without_text_or_paths(self):
        self.real_outbox(with_pending=True)
        arguments_seen = []
        real_run = outbox_actions._run

        def recording_run(folder, arguments):
            arguments_seen.append(arguments)
            return real_run(folder, arguments)

        with patch.object(outbox_actions, "_run", side_effect=recording_run):
            self.refused(400, "bad_action", action="delete", row_id=self.rows["ambiguous"])
            self.refused(400, "bad_id", row_id="../../x")
            self.refused(400, "bad_id", row_id=None)
            self.refused(400, "bad_project", project="..\\..\\alpha")
            self.refused(400, "not_confirmed", row_id=self.rows["ambiguous"], confirm="true")
            self.refused(400, "not_confirmed", row_id=self.rows["ambiguous"], confirm=None)
            self.refused(404, "unknown_project", project="beta", row_id=self.rows["ambiguous"])
            self.refused(409, "not_unconfirmed", row_id=self.rows["pending"])
            self.refused(409, "not_unconfirmed", row_id=self.rows["delivered"], action="skip")
            self.refused(409, "not_unconfirmed", row_id="b" * 32)
            self.put_status(at=NOW - timedelta(hours=2))
            self.refused(409, "status_unknown", row_id=self.rows["ambiguous"])
        self.assertFalse([arguments for arguments in arguments_seen if arguments[0] == "resolve"])
        for name in ("pending", "delivered", "ambiguous"):
            self.assertEqual(self.state_of(self.rows[name]), name)
        entries = self.journal()
        self.assertEqual(len(entries), 11)
        self.assertTrue(all(entry["result"] == "refused" and entry["by"] == "office-owner" and entry["reason"] for entry in entries))
        shown = json.dumps(entries)
        for secret in (SECRET_TEXT, "C:/secret", str(self.project), "alpha\\"):
            self.assertNotIn(secret, shown)

    def test_a_timeout_of_the_resolve_leaves_the_outcome_unknown_in_the_journal(self):
        self.fake_outbox(mode="sleep_resolve")
        with patch.object(outbox_actions, "TIMEOUT_SECONDS", 2):
            error = self.refused(503, "timeout_outcome_unknown")
        self.assertIn("unknown", error.message)
        self.assertEqual(self.journal()[-1]["outcome"], "unknown")

    def test_a_timeout_of_the_list_changes_nothing_and_says_so(self):
        self.fake_outbox(mode="sleep")
        with patch.object(outbox_actions, "TIMEOUT_SECONDS", 1):
            error = self.refused(503, "list_timeout")
        self.assertIn("Nothing was changed", error.message)
        self.assertEqual(self.journal()[-1]["outcome"], "none")
        self.assertEqual(self.resolve_calls(), [])

    def test_an_action_that_is_a_list_an_object_or_a_number_is_a_400_and_is_journaled(self):
        self.fake_outbox()
        for action in (["retry"], {"retry": 1}, 7, None, True):
            self.refused(400, "bad_action", action=action)
        entries = self.journal()
        self.assertEqual(len(entries), 5)
        self.assertTrue(all(entry["action"] is None and entry["reason"] == "bad_action" and entry["outcome"] == "none" for entry in entries))
        self.assertEqual(self.calls(), [])

    def test_an_unclear_answer_is_never_a_success(self):
        for out, code in (("", 0), ("Resolved something else.\n", 0), ("Resolved " + "a" * 32 + ": pending.\n", 1), ("Resolved " + "c" * 32 + ": pending.\n", 0)):
            self.fake_outbox(resolve_out=out, resolve_code=code)
            self.refused(503, "resolve_unclear")
            self.assertEqual(self.journal()[-1]["outcome"], "unknown")

    def test_a_row_that_changed_state_meanwhile_is_a_conflict(self):
        self.fake_outbox(resolve_code=2, resolve_err="refused: no ambiguous or needs-attention row with id " + "a" * 32 + "\n", resolve_out="")
        self.refused(409, "state_changed")

    def test_two_decisions_for_one_row_give_one_result_and_one_conflict(self):
        self.real_outbox()
        row = self.rows["ambiguous"]
        gate, outcomes = threading.Barrier(2), []

        def decide():
            gate.wait()
            try:
                outcomes.append(("ok", self.decide(row_id=row, action="skip")["state"]))
            except outbox_actions.Refused as refusal:
                outcomes.append(("refused", refusal.status))

        threads = [threading.Thread(target=decide) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(sorted(outcomes), [("ok", "skipped"), ("refused", 409)])
        self.assertEqual(self.state_of(row), "skipped")
        self.assertEqual(sorted(entry["result"] for entry in self.journal()), ["refused", "resolved"])


if __name__ == "__main__":
    unittest.main()
