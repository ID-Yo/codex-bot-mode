"""Sender feedback of 'outbox.py send' and the last-delivery field of 'status' (#424, S4-A)."""
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.team_room import outbox

THREAD = "synthetic-thread-1"
RECEIPT = re.compile(r"Queued outbox handoff ([0-9a-f]{32}) for ")  # what scripts/doctor.py reads


class OutboxCase(unittest.TestCase):
    """A throw-away project with an empty queue (self-contained: the public export has no other outbox test module)."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_outbox_feedback_"))
        self.db_path = self.tmp / ".bot-mode" / "outbox" / "queue.sqlite"
        self.db = outbox.OutboxDB(self.db_path)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    def state(self, msg_id):
        db = outbox.OutboxDB(self.db_path)
        try:
            return dict(db.conn.execute("SELECT * FROM outbox WHERE id = ?", (msg_id,)).fetchone())
        finally:
            db.close()


class SendFeedbackText(unittest.TestCase):
    def test_healthy_send_reports_relay_pump_and_last_delivery_without_warning(self):
        out, err = outbox.send_feedback(3, 40, 300, True, True)
        self.assertEqual(out, ["Queue: 3 pending, oldest 40 s.", "Relay: up. Pump: running. Last delivery: 5 min ago."])
        self.assertEqual(err, [])

    def test_a_pump_that_is_started_by_this_send_and_no_delivery_yet_are_said_plainly(self):
        out, err = outbox.send_feedback(1, None, None, True, False)
        self.assertEqual(out[1], "Relay: up. Pump: started now. Last delivery: none recorded.")
        self.assertEqual(out[0], "Queue: 1 pending, oldest 0 s.")
        self.assertEqual(err, [])

    def test_relay_down_says_not_delivered_and_names_every_fallback(self):
        out, err = outbox.send_feedback(1, 2, 60, False, True)
        self.assertIn("Relay: DOWN.", out[1])
        text = " ".join(err)
        self.assertIn("NOT delivered yet", text)
        self.assertIn("stays queued", text)
        for fallback in ("thread tool", "requests.py reply", "linked issue"):
            self.assertIn(fallback, text)

    def test_an_old_row_with_the_relay_up_warns_with_its_age_and_the_fallbacks(self):
        out, err = outbox.send_feedback(2, 3 * 3600 + 5 * 60, 10, True, True)
        self.assertIn("3 h 05 min", err[0])
        self.assertIn("thread tool", err[0])

    def test_a_row_inside_the_stale_limit_does_not_warn(self):
        self.assertEqual(outbox.send_feedback(2, outbox.STALE_SECONDS, 10, True, True)[1], [])

    def test_a_held_queue_message_is_kept(self):
        held = {"id": "abc", "target_bot": "manager", "state": "ambiguous"}
        with patch.object(outbox, "held_message", return_value="HELD abc"):
            self.assertEqual(outbox.send_feedback(1, 1, None, True, True, held)[1], ["HELD abc"])

    def test_human_age_units(self):
        for seconds, text in ((None, None), (0, "0 s"), (119, "119 s"), (120, "2 min"), (7199, "119 min"),
                              (7200, "2 h 00 min"), (3 * 3600 + 5 * 60, "3 h 05 min"), (172800, "2 d 0 h"), (2 * 86400 + 4 * 3600, "2 d 4 h")):
            self.assertEqual(outbox.human_age(seconds), text, seconds)


class LastDelivery(OutboxCase):
    def test_none_before_any_delivery_then_the_newest_delivered_row(self):
        self.assertIsNone(self.db.last_delivered_at())
        self.assertIsNone(self.db.last_delivered_age())
        first = self.db.enqueue("dev", "manager", THREAD, "one")
        self.db.enqueue("dev", "manager", THREAD, "two")
        self.assertIsNone(self.db.last_delivered_at())
        self.db.mark_delivered(first)
        stamp = self.db.last_delivered_at()
        self.assertEqual(stamp, self.state(first)["delivered_at"])
        self.assertLess(self.db.last_delivered_age(), 5)

    def test_pending_and_skipped_rows_never_count_as_a_delivery(self):
        ambiguous = self.db.enqueue("dev", "manager", THREAD, "unsure")
        self.db.mark_ambiguous(ambiguous, "timeout")
        self.db.resolve(ambiguous, "skipped")
        self.assertIsNone(self.db.last_delivered_at())

    def test_status_reports_last_delivered_at_and_keeps_its_other_fields(self):
        row = self.db.enqueue("dev", "manager", THREAD, "done")
        self.db.mark_delivered(row)
        self.db.close()
        with patch.object(outbox, "relay_reachable", return_value=True):
            status = outbox.queue_status(self.tmp)
        self.assertEqual(status["last_delivered_at"], self.state(row)["delivered_at"])
        for key in ("pending", "oldest_pending_seconds", "ambiguous", "needs_attention", "recheckable", "expired",
                    "relay_reachable", "pump_alive", "problems"):
            self.assertIn(key, status)
        self.db = outbox.OutboxDB(self.db_path)

    def test_status_of_a_project_without_a_queue_has_no_last_delivery(self):
        empty = self.tmp / "other"
        with patch.object(outbox, "relay_reachable", return_value=True):
            self.assertIsNone(outbox.queue_status(empty)["last_delivered_at"])


class SendCommand(OutboxCase):
    def send(self, relay_up, pump_running=False):
        out, err = io.StringIO(), io.StringIO()
        self.db.close()
        with patch("sys.stdout", out), patch("sys.stderr", err), patch.object(outbox, "spawn_background_pump") as spawn, \
                patch.object(outbox, "relay_reachable", return_value=relay_up), patch.object(outbox, "pump_alive", return_value=pump_running):
            code = outbox.main(["--project", str(self.tmp), "send", "--to", "manager", "--by", "dev", "--text", "hi"])
        self.db = outbox.OutboxDB(self.db_path)
        return code, out.getvalue(), err.getvalue(), spawn

    def test_send_prints_the_receipt_first_unchanged_then_the_state_lines(self):
        code, out, err, spawn = self.send(True)
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertRegex(lines[0], RECEIPT)
        self.assertTrue(lines[0].endswith("Turn complete."))
        self.assertEqual(lines[1], "Queue: 1 pending, oldest 0 s.")
        self.assertEqual(lines[2], "Relay: up. Pump: started now. Last delivery: none recorded.")
        self.assertEqual(err, "")
        spawn.assert_called_once()

    def test_send_with_the_relay_down_still_queues_and_starts_the_pump_but_says_it_is_not_delivered(self):
        code, out, err, spawn = self.send(False)
        self.assertEqual(code, 0)
        self.assertIn("Relay: DOWN.", out)
        self.assertIn("NOT delivered yet", err)
        self.assertIn("requests.py reply", err)
        spawn.assert_called_once()
        self.assertEqual(len(self.db.list_recent(state="pending")), 1)

    def test_send_after_a_delivery_shows_its_age(self):
        row = self.db.enqueue("dev", "manager", THREAD, "earlier")
        self.db.mark_delivered(row)
        self.db.conn.execute("UPDATE outbox SET delivered_at = ? WHERE id = ?",
                             ((datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat(), row))
        self.db.conn.commit()
        _, out, _, _ = self.send(True, pump_running=True)
        self.assertIn("Relay: up. Pump: running. Last delivery: 12 min ago.", out)


if __name__ == "__main__":
    unittest.main()
