"""requests.py pending and the agent fallback when the relay is down (#425, S5): the ledger is the truth, the outbox only wakes."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.team_room import outbox, requests


class ProjectCase(unittest.TestCase):
    """A throw-away project with its own ledger and outbox (self-contained: the public export has no other helper for this)."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_pending_425_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / ".bot-mode" / "outbox" / "queue.sqlite"
        self.ledger = self.tmp / ".bot-mode" / "local" / "requests.json"

    def open_request(self, key, sender, recipient, summary="do the thing", minutes=60, link="https://github.com/o/r/issues/1", at=None):
        requests.update(self.tmp, lambda data: requests.open_request(data, key, sender, recipient, summary, link, minutes, at=at))

    def queue(self, text, target="manager", sender="developer", state="pending", key=None, age_seconds=0):
        db = outbox.OutboxDB(self.db_path)
        try:
            msg_id = db.enqueue(sender, target, None, text, key)
            with db.conn:
                db.conn.execute("UPDATE outbox SET state = ?, created_at = ? WHERE id = ?",
                                (state, (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(), msg_id))
            return msg_id
        finally:
            db.close()

    def pending(self, bot="manager"):
        return "\n".join(requests.pending_lines(self.tmp, bot))

    def cli(self, *argv):
        """requests.py main with captured streams (it switches stdout to UTF-8, so stdout is a real text wrapper)."""
        raw = io.BytesIO()
        out, err = io.TextIOWrapper(raw, encoding="utf-8"), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = requests.main(["--project", str(self.tmp)] + list(argv))
            out.flush()
        return code, raw.getvalue().decode("utf-8"), err.getvalue()


class PendingReport(ProjectCase):
    def test_an_empty_project_says_nothing_is_waiting_and_that_there_is_no_queue(self):
        text = self.pending()
        self.assertIn("0 open request(s) addressed to manager", text)
        self.assertIn("no queue file", text)
        self.assertIn("Nothing waiting for manager.", text)

    def test_an_open_request_for_the_manager_is_listed_with_age_due_link_and_summary(self):
        self.open_request("cbm1-review", "developer", "manager", summary="please review the helper")
        text = self.pending()
        self.assertIn("1 open request(s) addressed to manager", text)
        self.assertIn("cbm1-review  from developer", text)
        self.assertIn("due in", text)
        self.assertIn("https://github.com/o/r/issues/1", text)
        self.assertIn("please review the helper", text)
        self.assertNotIn("Nothing waiting", text)

    def test_requests_for_other_bots_are_not_listed_and_do_not_count(self):
        self.open_request("cbm2", "manager", "developer")
        text = self.pending()
        self.assertNotIn("cbm2  from", text)
        self.assertIn("Waiting on others: 1 open request(s) from manager, 0 overdue.", text)
        self.assertIn("Nothing waiting for manager.", text)

    def test_an_overdue_request_the_manager_waits_on_is_named(self):
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        self.open_request("cbm3", "manager", "reviewer", minutes=60, at=past)
        text = self.pending()
        self.assertIn("1 overdue", text)
        self.assertIn("cbm3  to reviewer  OVERDUE by", text)
        self.assertNotIn("Nothing waiting", text)

    def test_a_closed_request_is_not_pending(self):
        self.open_request("cbm4", "developer", "manager")
        requests.update(self.tmp, lambda data: requests.cancel(data, "cbm4", "developer", "not needed"))
        self.assertIn("0 open request(s) addressed to manager", self.pending())

    def test_undelivered_outbox_rows_for_the_manager_are_listed_with_their_text(self):
        row = self.queue("developer finished the helper", state="ambiguous", age_seconds=630)
        self.queue("already read", state="delivered")
        self.queue("for somebody else", target="developer")
        self.queue("late report", state="needs-attention")
        text = self.pending()
        self.assertIn("2 report(s) for manager not delivered yet", text)
        self.assertIn(f"{row}  from developer  ambiguous  10 min old", text)
        self.assertIn("developer finished the helper", text)
        self.assertIn("late report", text)
        self.assertNotIn("already read", text)
        self.assertNotIn("for somebody else", text)
        self.assertIn("outbox.py status", text)
        self.assertIn("resolve <id> --as delivered|skipped|retry", text)

    def test_expired_rows_are_counted_not_listed(self):
        self.queue("too old", state="expired")
        text = self.pending()
        self.assertIn("1 expired row(s) were never delivered", text)
        self.assertNotIn("too old", text)
        self.assertIn("0 report(s) for manager not delivered yet", text)

    def test_a_long_queue_is_capped_and_a_long_text_is_cut_to_one_line(self):
        for number in range(requests.SHOWN_ROWS + 3):
            self.queue("row %d\n" % number + "x" * 500, key="k%d" % number)
        text = self.pending()
        self.assertIn(f"{requests.SHOWN_ROWS + 3} report(s)", text)
        self.assertIn("... and 3 more: outbox.py list", text)
        self.assertNotIn("x" * 201, text)
        self.assertIn("x" * 150, text)

    def test_an_unreadable_ledger_is_unknown_not_empty_and_the_outbox_is_still_shown(self):
        self.queue("still visible")
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{not json", encoding="utf-8")
        text = self.pending()
        self.assertIn("Ledger: UNKNOWN", text)
        self.assertIn("still visible", text)
        self.assertNotIn("Nothing waiting", text)

    def test_a_damaged_ledger_entry_is_unknown_not_a_crash(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text('{"schema": 1, "requests": [{"state": "open"}]}', encoding="utf-8")
        self.assertIn("Ledger: UNKNOWN", self.pending())

    def test_an_unreadable_outbox_is_unknown_not_empty(self):
        self.open_request("cbm5", "developer", "manager")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_bytes(b"this is not a database" * 50)
        text = self.pending()
        self.assertIn("Outbox: UNKNOWN", text)
        self.assertIn("cbm5  from developer", text)
        self.assertIn("Cannot tell what waits for manager: the outbox queue is UNKNOWN; look by hand.", text.splitlines()[-1])
        self.assertNotIn("Nothing waiting", text)

    def test_an_unreadable_ledger_with_an_empty_outbox_is_not_nothing_waiting(self):
        outbox.OutboxDB(self.db_path).close()  # a readable queue without any row
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{not json", encoding="utf-8")
        text = self.pending()
        self.assertIn("Ledger: UNKNOWN", text)
        self.assertIn("0 report(s) for manager not delivered yet", text)
        self.assertEqual(text.splitlines()[-1], "Cannot tell what waits for manager: the request ledger is UNKNOWN; look by hand.")
        self.assertNotIn("Nothing waiting", text)

    def test_an_unreadable_outbox_with_an_empty_ledger_is_not_nothing_waiting(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_bytes(b"this is not a database" * 50)
        text = self.pending()
        self.assertIn("0 open request(s) addressed to manager", text)
        self.assertEqual(text.splitlines()[-1], "Cannot tell what waits for manager: the outbox queue is UNKNOWN; look by hand.")
        self.assertNotIn("Nothing waiting", text)

    def test_two_unreadable_sources_are_both_named_on_the_last_line(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_bytes(b"this is not a database" * 50)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.pending().splitlines()[-1],
                         "Cannot tell what waits for manager: the request ledger and the outbox queue are UNKNOWN; look by hand.")

    def test_a_missing_queue_file_is_an_outbox_that_does_not_exist_not_an_unknown_one(self):
        text = self.pending()
        self.assertNotIn("Cannot tell", text)
        self.assertEqual(text.splitlines()[-1], "Nothing waiting for manager.")

    def answer(self, key, by="developer", at=None, outcome="done", link="https://github.com/o/r/pull/9"):
        requests.update(self.tmp, lambda data: requests.reply(data, key, by, link, outcome, at=at))

    def test_a_request_the_manager_sent_and_the_agent_answered_stays_visible_after_the_reply(self):
        self.open_request("cbm11", "manager", "developer", summary="build the helper")
        self.assertIn("Nothing waiting", self.pending())
        self.answer("cbm11")
        text = self.pending()
        self.assertIn("Answered: 1 request(s) from manager", text)
        self.assertIn("cbm11  by developer  done  ", text)
        self.assertIn("https://github.com/o/r/pull/9", text)
        self.assertIn("build the helper", text)
        self.assertNotIn("Nothing waiting", text)
        self.assertIn("a comment on the linked issue is not read here", text)
        self.assertIn("Nothing waiting", self.pending("developer"))  # the answering agent has nothing new to read

    def test_the_closeout_without_any_outbox_is_found_by_the_manager_through_the_ledger(self):
        self.open_request("cbm12", "manager", "developer", summary="build it")
        code, _, err = self.cli("reply", "cbm12", "--by", "developer", "--link", "https://github.com/o/r/pull/3")
        self.assertEqual(code, 0)
        self.assertIn("by hand", err)  # no outbox in this project: the ledger is the only record
        code, out, _ = self.cli("pending")
        self.assertEqual(code, 0)
        self.assertIn("cbm12  by developer  done", out)
        self.assertIn("no queue file", out)

    def test_an_answer_older_than_the_window_is_not_listed_but_an_unaccepted_partial_one_stays(self):
        long_ago = datetime.now(timezone.utc) - timedelta(hours=requests.ANSWERED_HOURS + 5)
        for key in ("cbm13", "cbm14"):
            self.open_request(key, "manager", "developer", at=long_ago - timedelta(hours=1))
        self.answer("cbm13", at=long_ago)
        self.answer("cbm14", at=long_ago, outcome="partial")
        text = self.pending()
        self.assertNotIn("cbm13  by", text)
        self.assertIn("cbm14  by developer  partial", text)
        self.assertIn("partial, not accepted yet", text)
        requests.update(self.tmp, lambda data: requests.accept(data, "cbm14", "manager", "https://github.com/o/r/issues/10"))
        text = self.pending()
        self.assertNotIn("cbm14  by", text)
        self.assertIn("Nothing waiting", text)

    def test_a_cancelled_request_and_a_request_to_the_manager_are_not_answers_for_the_manager(self):
        self.open_request("cbm15", "manager", "developer")
        requests.update(self.tmp, lambda data: requests.cancel(data, "cbm15", "manager", "not needed"))
        self.open_request("cbm16", "developer", "manager")
        self.answer("cbm16", by="manager")
        self.assertIn("Answered: 0 request(s) from manager", self.pending())

    def test_an_answer_without_a_usable_time_is_shown_not_hidden(self):
        self.open_request("cbm17", "manager", "developer")
        self.answer("cbm17")
        data = requests.load(self.tmp)
        for item in data["requests"]:
            item["answered"] = "not a time"
        self.ledger.write_text(json.dumps(data), encoding="utf-8")
        text = self.pending()
        self.assertIn("cbm17  by developer  done  at an unknown time", text)
        self.assertNotIn("Ledger: UNKNOWN", text)

    def test_a_long_list_of_answers_shows_the_newest_and_counts_the_rest(self):
        for number in range(requests.SHOWN_ROWS + 2):
            self.open_request("many%d" % number, "manager", "developer")
            self.answer("many%d" % number)
        text = self.pending()
        self.assertIn("... and 2 older answered request(s) not shown", text)
        self.assertIn("many%d  by" % (requests.SHOWN_ROWS + 1), text)
        self.assertNotIn("many0  by", text)

    def test_it_changes_nothing(self):
        self.open_request("cbm10", "manager", "developer")
        self.answer("cbm10")
        self.open_request("cbm6", "developer", "manager")
        self.queue("keep me", state="ambiguous")
        ledger_before = self.ledger.read_bytes()
        db = outbox.OutboxDB(self.db_path)
        rows_before = [tuple(r) for r in db.conn.execute("SELECT * FROM outbox ORDER BY id")]
        db.close()
        self.pending()
        self.assertEqual(self.ledger.read_bytes(), ledger_before)
        db = outbox.OutboxDB(self.db_path)
        try:
            self.assertEqual([tuple(r) for r in db.conn.execute("SELECT * FROM outbox ORDER BY id")], rows_before)
        finally:
            db.close()

    def test_it_does_not_create_a_queue_or_a_ledger(self):
        self.pending()
        self.assertFalse(self.db_path.exists())
        self.assertFalse(self.ledger.exists())

    def test_the_command_prints_the_report_for_the_default_and_a_named_bot(self):
        self.open_request("cbm7", "manager", "developer")
        for argv, expected in ((["pending"], "Pending for manager at"), (["pending", "--for", "developer"], "cbm7  from manager")):
            code, out, _ = self.cli(*argv)
            self.assertEqual(code, 0)
            self.assertIn(expected, out)


class RelayDownFallback(ProjectCase):
    """The agent still reaches the Manager when the relay is down: nothing is lost and pending shows it."""
    def send(self, text, key):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(outbox, "relay_reachable", return_value=False), patch.object(outbox, "spawn_background_pump"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = outbox.main(["--project", str(self.tmp), "send", "--to", "manager", "--by", "developer",
                                "--dedupe-key", key, "--text", text])
        return code, out.getvalue(), err.getvalue()

    def test_send_with_the_relay_down_queues_warns_and_names_all_three_fallbacks(self):
        code, out, err = self.send("result of the task", "fallback-1")
        self.assertEqual(code, 0)
        self.assertIn("Queued outbox handoff", out)
        self.assertIn("Relay: DOWN", out)
        self.assertIn("NOT delivered yet", err)
        for fallback in ("thread tool", "requests.py reply", "linked issue"):
            self.assertIn(fallback, err)

    def test_the_manager_finds_the_queued_report_and_the_ledger_entry_without_a_relay(self):
        self.open_request("cbm8", "developer", "manager", summary="decide on the plan")
        self.send("plan is ready", "fallback-2")
        text = self.pending()
        self.assertIn("cbm8  from developer", text)
        self.assertIn("plan is ready", text)

    def test_a_closeout_without_an_outbox_stays_in_the_ledger_and_says_to_report_by_hand(self):
        self.open_request("cbm9", "manager", "developer", summary="build it")
        code, _, err = self.cli("reply", "cbm9", "--by", "developer", "--link", "https://github.com/o/r/pull/2")
        self.assertEqual(code, 0)
        self.assertIn("send the report to the requester by hand", err)
        item = [i for i in requests.load(self.tmp)["requests"] if i["id"] == "cbm9"][0]
        self.assertEqual(item["state"], "answered")
        self.assertEqual(item["reply_link"], "https://github.com/o/r/pull/2")
        self.assertIn("0 open request(s) addressed to developer", self.pending("developer"))


if __name__ == "__main__":
    unittest.main()
