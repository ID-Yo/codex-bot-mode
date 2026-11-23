"""Delivery correctness without human help (#422): ambiguous rows, needs-attention, digest, retention."""
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from scripts.team_room import outbox
from tests.test_outbox import OutboxCase, THREAD, BUSY_THREAD

OTHER = "00000000-0000-7000-8000-000000000002"


class ThreadRelay:
    """Several recipient threads. A send registers a new turn at once and is kept in the thread history as a user message.
    messages_in_history lists prompts that are already in a thread (a delivery the pump never saw confirmed).
    busy_reads makes a thread active for that many status reads. incomplete_history makes the history endless."""

    def __init__(self, busy_reads=None, history=None, incomplete_history=False, fail_history=False, page_turns=10,
                 shape=None, late=None, fail_on_read=None):
        self.busy = dict(busy_reads or {})
        self.history = {thread: list(prompts) for thread, prompts in (history or {}).items()}
        self.turns = {}
        self.sent = []
        self.history_reads = 0
        self.incomplete, self.fail_history = incomplete_history, fail_history
        self.page_turns = page_turns  # history turns (one user message each) per page
        self.limits = []
        self.shape = shape  # None, or how a history reply is damaged: no_turns, no_page, bad_page, bad_cursor, bad_items, redacted
        self.fail_on_read = fail_on_read  # number of the history read that raises (1 = the first)
        self.late = dict(late or {})  # thread -> (history read number, prompt): a message that shows up in the history later

    def call(self, name, arguments, timeout=40):
        thread = arguments["threadId"]
        if name == "send_message_to_thread":
            self.turns[thread] = self.turns.get(thread, 0) + 1
            self.history.setdefault(thread, []).append(arguments["prompt"])
            self.sent.append((thread, arguments["prompt"]))
            return {}
        assert name == "read_thread", name
        count = self.turns.get(thread, 0)
        if arguments.get("turnLimit") == 1:
            busy = self.busy.get(thread, 0) > 0
            if busy:
                self.busy[thread] -= 1
            return {"thread": {"status": {"type": "active" if busy else "idle"}},
                    "turns": [{"id": "t%d" % count, "status": "inProgress" if busy else "completed", "startedAt": count + 1}]}
        self.history_reads += 1
        self.limits.append(arguments.get("turnLimit"))
        if self.fail_on_read and self.history_reads == self.fail_on_read:
            raise OSError("this history page cannot be read")
        if arguments.get("turnLimit", 0) > 10:  # the real Desktop tool refuses more than 10 turns per call (#429)
            raise RuntimeError("read_thread received invalid arguments: turnLimit: Too big: expected number to be <=10")
        if self.fail_history:
            raise OSError("history unavailable")
        if thread in self.late and self.history_reads >= self.late[thread][0]:
            self.history.setdefault(thread, []).append(self.late.pop(thread)[1])
        prompts = self.history.get(thread, [])
        if self.shape == "no_turns":
            return {"page": {}}
        if self.shape == "redacted":  # what the relay forwards for a read-only chat: one character per item, no cursor
            return {"turns": [{"id": "h", "items": [{"type": "userMessage", "content": [{"type": "text", "text": p[:1]}]}]} for p in prompts],
                    "page": {}}
        start = int(arguments.get("cursor") or 0)
        size = min(arguments["turnLimit"], self.page_turns)
        turns = [{"id": "h%d" % n, "items": [{"type": "userMessage", "content": [{"type": "text", "text": prompt}]}]}
                 for n, prompt in enumerate(prompts[start:start + size], start)]
        more = start + size < len(prompts) or self.incomplete
        reply = {"turns": turns, "page": {"nextCursor": str(start + size)} if more else {}}
        if self.shape == "no_page":
            del reply["page"]
        elif self.shape == "bad_page":
            reply["page"] = "last"
        elif self.shape == "bad_cursor":
            reply["page"] = {"nextCursor": 7}
        elif self.shape == "bad_items":
            reply["turns"] = [{"id": "h"}]
        elif self.shape == "null_content":  # one user message whose content is missing, next to readable ones
            reply["turns"] = [{"id": "x", "items": [{"type": "userMessage", "content": None}]}] + reply["turns"]
        elif self.shape == "bad_part":
            reply["turns"] = [{"id": "x", "items": [{"type": "userMessage", "content": ["text"]}]}] + reply["turns"]
        elif self.shape == "bad_text":
            reply["turns"] = [{"id": "x", "items": [{"type": "userMessage", "content": [{"type": "text", "text": None}]}]}] + reply["turns"]
        return reply

    def bodies(self, thread=None):
        return [prompt for t, prompt in self.sent if thread in (None, t)]


class Case(OutboxCase):
    def backdate(self, msg_id, minutes):
        old = outbox.datetime.fromtimestamp(outbox.time.time() - minutes * 60, outbox.timezone.utc).isoformat()
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET ambiguous_at = ? WHERE id = ?", (old, msg_id))

    def pump(self, client, **kwargs):
        kwargs.setdefault("reconcile_every", 0)
        kwargs.setdefault("digest_max", outbox.DEFAULT_DIGEST_MAX)
        kwargs.setdefault("max_wait_sec", 2)
        return super().pump(client, **kwargs)

    def ambiguous(self, sender, thread, text, target=None):
        msg_id = self.db.enqueue(sender, target or thread, thread, text)
        self.db.mark_sending(msg_id, thread)
        self.db.mark_ambiguous(msg_id, "timeout after the call")
        return msg_id


class TestAmbiguousReconciliation(Case):
    def test_a_marker_in_the_thread_history_proves_delivery_and_releases_the_recipient(self):
        a = self.ambiguous("dev", THREAD, "A")
        b = self.db.enqueue("rev", THREAD, THREAD, "B")
        relay = ThreadRelay(history={THREAD: [outbox.delivery_marker(a) + "\nA"]})
        self.assertEqual(self.pump(relay), 0)
        self.assertEqual(self.state(a)["state"], "delivered")
        self.assertEqual(self.state(b)["state"], "delivered")
        self.assertEqual([p.split("\n", 1)[1] for p in relay.bodies()], ["B"])  # A was not sent again

    def test_a_row_without_proof_is_never_resent_and_holds_only_its_own_recipient(self):
        a = self.ambiguous("dev", THREAD, "A")
        b = self.db.enqueue("rev", THREAD, THREAD, "B")
        c = self.db.enqueue("rev", OTHER, OTHER, "C")
        relay = ThreadRelay()
        self.assertEqual(self.pump(relay, retry_grace_minutes=0, max_wait_sec=0.3), 0)
        self.assertEqual(self.state(a)["state"], "ambiguous")
        self.assertEqual(self.state(b)["state"], "pending")  # waits behind A: same recipient
        self.assertEqual(self.state(c)["state"], "delivered")  # another recipient is not held
        self.assertEqual([p.split("\n", 1)[1] for p in relay.bodies()], ["C"])

    def test_a_row_ambiguous_for_a_role_name_also_holds_that_role_only(self):
        a = self.db.enqueue("dev", "manager", None, "A")
        self.db.mark_ambiguous(a, "x")
        b = self.db.enqueue("rev", "manager", None, "B")
        c = self.db.enqueue("rev", OTHER, OTHER, "C")
        self.assertEqual([r["id"] for r in self.db.next_batch(max_rows=10)], [c])
        self.assertNotIn(b, [r["id"] for r in self.db.next_batch(max_rows=10)])

    def test_complete_history_without_marker_after_the_grace_period_retries(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 11)
        relay = ThreadRelay(history={THREAD: ["earlier conversation"]})
        self.assertEqual(self.pump(relay, retry_grace_minutes=10), 0)
        self.assertEqual([p.split("\n", 1)[1] for p in relay.bodies()], ["A"])
        self.assertEqual(self.state(a)["state"], "delivered")

    def test_a_damaged_or_redacted_history_never_proves_non_delivery(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 60)
        for shape in ("no_turns", "no_page", "bad_page", "bad_cursor", "bad_items", "redacted"):
            relay = ThreadRelay(history={THREAD: ["earlier conversation", "and more"]}, shape=shape)
            self.pump(relay, retry_grace_minutes=10, attention_minutes=1000, max_wait_sec=0.3, rechecks=1)
            self.assertEqual(relay.sent, [], shape)
            self.assertEqual(self.state(a)["state"], "ambiguous", shape)

    def test_an_unreadable_user_message_makes_the_read_incomplete_for_retry(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 60)
        for shape in ("null_content", "bad_part", "bad_text"):
            relay = ThreadRelay(history={THREAD: ["earlier conversation", "and more"]}, shape=shape)
            self.pump(relay, retry_grace_minutes=10, attention_minutes=1000, max_wait_sec=0.3, rechecks=1)
            self.assertEqual(relay.sent, [], shape)
            self.assertEqual(self.state(a)["state"], "ambiguous", shape)

    def test_a_marker_next_to_an_unreadable_user_message_still_proves_delivery(self):
        a = self.ambiguous("dev", THREAD, "A")
        for shape in ("null_content", "bad_part", "bad_text"):
            self.db.conn.execute("UPDATE outbox SET state = 'ambiguous' WHERE id = ?", (a,))
            self.db.conn.commit()
            relay = ThreadRelay(history={THREAD: [outbox.delivery_marker(a) + "\nA"]}, shape=shape)
            outbox.reconcile_ambiguous(self.db, relay)
            self.assertEqual(self.state(a)["state"], "delivered", shape)

    def test_non_text_parts_of_a_user_message_do_not_make_the_read_incomplete(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 60)

        class Relay(ThreadRelay):
            def call(self, name, arguments, timeout=40):
                reply = super().call(name, arguments, timeout)
                if arguments.get("turnLimit") != 1:
                    reply["turns"][0]["items"][0]["content"].append({"type": "image", "url": "x"})
                return reply

        relay = Relay(history={THREAD: ["earlier conversation"]})
        outbox.reconcile_ambiguous(self.db, relay, retry_grace_minutes=10)
        self.assertEqual(self.state(a)["state"], "pending")  # a complete, readable history without the marker

    def test_a_marker_seen_on_an_earlier_page_survives_an_error_on_a_later_page(self):
        a, b = self.ambiguous("dev", THREAD, "A"), self.ambiguous("rev", THREAD, "B")
        self.backdate(a, 60)
        self.backdate(b, 60)
        # page one holds A's marker; B's marker is on the second page, which fails
        history = [outbox.delivery_marker(a) + "\nA"] + ["filler %d" % n for n in range(10)] + [outbox.delivery_marker(b) + "\nB"]
        relay = ThreadRelay(history={THREAD: history}, fail_on_read=2)
        with patch("sys.stderr", io.StringIO()):
            outbox.reconcile_ambiguous(self.db, relay, retry_grace_minutes=10)
        self.assertEqual(relay.history_reads, 2)
        self.assertEqual(self.state(a)["state"], "delivered")
        self.assertEqual(self.state(b)["state"], "ambiguous")  # the unreadable page rules out a retry, nothing is resent

    def test_reading_stops_as_soon_as_every_wanted_marker_is_found(self):
        a = self.ambiguous("dev", THREAD, "A")
        relay = ThreadRelay(history={THREAD: [outbox.delivery_marker(a) + "\nA"] + ["x%d" % n for n in range(30)]}, page_turns=1)
        outbox.reconcile_ambiguous(self.db, relay)
        self.assertEqual(relay.history_reads, 1)
        self.assertEqual(self.state(a)["state"], "delivered")

    def test_a_marker_before_a_damaged_page_still_proves_delivery(self):
        a = self.ambiguous("dev", THREAD, "A")
        relay = ThreadRelay(history={THREAD: [outbox.delivery_marker(a) + "\nA"]}, shape="bad_page")
        outbox.reconcile_ambiguous(self.db, relay)
        self.assertEqual(self.state(a)["state"], "delivered")

    def test_an_empty_history_is_not_a_full_read(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 60)
        outbox.reconcile_ambiguous(self.db, ThreadRelay(), retry_grace_minutes=10)
        self.assertEqual(self.state(a)["state"], "ambiguous")

    def test_no_retry_before_the_grace_period(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 2)
        relay = ThreadRelay()
        self.pump(relay, retry_grace_minutes=10, max_wait_sec=0.3)
        self.assertEqual(relay.sent, [])
        self.assertEqual(self.state(a)["state"], "ambiguous")

    def test_no_retry_when_the_history_is_incomplete_or_unreadable(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 60)
        for relay in (ThreadRelay(incomplete_history=True), ThreadRelay(fail_history=True)):
            with patch("sys.stderr", io.StringIO()):
                self.pump(relay, retry_grace_minutes=10, attention_minutes=1000, max_wait_sec=0.3)
            self.assertEqual(relay.sent, [])
            self.assertEqual(self.state(a)["state"], "ambiguous")

    def test_retry_grace_zero_never_retries(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 600)
        relay = ThreadRelay()
        self.pump(relay, retry_grace_minutes=0, attention_minutes=1000, max_wait_sec=0.3)
        self.assertEqual(relay.sent, [])

    def test_history_is_read_once_per_thread_for_all_its_rows(self):
        a, b = self.ambiguous("dev", THREAD, "A"), self.ambiguous("rev", THREAD, "B")
        relay = ThreadRelay(history={THREAD: [outbox.delivery_marker(a) + "\nA", outbox.delivery_marker(b) + "\nB"]})
        outbox.reconcile_ambiguous(self.db, relay)
        self.assertEqual(relay.history_reads, 1)
        self.assertEqual(self.db.count_state("delivered"), 2)

    def test_history_is_read_in_pages_of_at_most_ten_turns_and_the_marker_on_a_later_page_is_found(self):
        a = self.ambiguous("dev", THREAD, "A")
        old = [outbox.delivery_marker("older%d" % n) + "\nx" for n in range(10)]  # a first full page without the marker
        relay = ThreadRelay(history={THREAD: old + [outbox.delivery_marker(a) + "\nA"]})
        outbox.reconcile_ambiguous(self.db, relay)
        self.assertEqual(self.state(a)["state"], "delivered")
        self.assertEqual(relay.history_reads, 2)
        self.assertTrue(all(limit <= 10 for limit in relay.limits))

    def test_a_small_page_still_reaches_the_second_page(self):
        a = self.ambiguous("dev", THREAD, "A")
        relay = ThreadRelay(history={THREAD: ["other", outbox.delivery_marker(a) + "\nA"]}, page_turns=1)
        outbox.reconcile_ambiguous(self.db, relay)
        self.assertEqual(self.state(a)["state"], "delivered")
        self.assertEqual(relay.history_reads, 2)

    def test_the_search_covers_about_fifty_turns_and_a_longer_history_is_not_proof(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 60)
        relay = ThreadRelay(history={THREAD: ["x%d" % n for n in range(80)]})
        outbox.reconcile_ambiguous(self.db, relay, retry_grace_minutes=10)
        self.assertEqual(relay.history_reads, outbox.RECONCILE_PAGES)
        self.assertEqual(outbox.RECONCILE_PAGES * outbox.RECONCILE_TURNS, 50)
        self.assertEqual(self.state(a)["state"], "ambiguous")  # 30 turns were not read: no auto-retry

    def test_a_marker_quoted_in_an_unrelated_user_message_is_not_proof(self):
        a = self.ambiguous("dev", THREAD, "A")
        relay = ThreadRelay(history={THREAD: ["please look at " + outbox.delivery_marker(a) + "\nthanks",
                                              "note\n" + outbox.delivery_marker(a)]})
        outbox.reconcile_ambiguous(self.db, relay)
        self.assertEqual(self.state(a)["state"], "ambiguous")


class TestUnresolvedOnlyQueue(Case):
    def late_marker(self, a):
        return {THREAD: (2, outbox.delivery_marker(a) + "\nA")}  # visible from the second history read on

    def test_a_late_marker_proves_an_ambiguous_row_without_any_new_enqueue(self):
        a = self.ambiguous("dev", THREAD, "A")
        relay = ThreadRelay(history={THREAD: ["earlier conversation"]}, late=self.late_marker(a))
        self.assertEqual(self.pump(relay, retry_grace_minutes=0), 0)
        self.assertEqual(self.state(a)["state"], "delivered")
        self.assertEqual(relay.sent, [])

    def test_a_late_marker_proves_a_needs_attention_row_too(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 45)
        self.db.escalate_ambiguous(30)
        relay = ThreadRelay(history={THREAD: ["earlier conversation"]}, late=self.late_marker(a))
        self.assertEqual(self.pump(relay, retry_grace_minutes=0), 0)
        self.assertEqual(self.state(a)["state"], "delivered")

    def test_rereading_is_bounded_and_the_row_stays_unresolved(self):
        a = self.ambiguous("dev", THREAD, "A")
        relay = ThreadRelay(history={THREAD: ["earlier conversation"]})
        self.assertEqual(self.pump(relay, retry_grace_minutes=0, rechecks=3, attention_minutes=1000), 0)
        self.assertEqual(relay.history_reads, 3)
        self.assertEqual(relay.sent, [])
        self.assertEqual(self.state(a)["state"], "ambiguous")

    def test_a_row_older_than_the_ttl_is_left_to_the_operator(self):
        a = self.ambiguous("dev", THREAD, "A")
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (a,))
        relay = ThreadRelay(history={THREAD: [outbox.delivery_marker(a) + "\nA"]})
        self.assertEqual(self.pump(relay, retry_grace_minutes=0), 0)
        self.assertEqual(relay.history_reads, 0)
        self.assertEqual(self.state(a)["state"], "ambiguous")

    def heal(self):
        self.db.close()
        out = io.StringIO()
        with patch("sys.stdout", out), patch("scripts.team_room.outbox.relay_reachable", return_value=True), \
                patch("scripts.team_room.outbox.spawn_background_pump") as spawn:
            outbox.main(["--project", str(self.tmp), "status", "--heal"])
        self.db = outbox.OutboxDB(self.db_path)
        return json.loads(out.getvalue()), spawn

    def test_status_heal_starts_a_pump_for_a_needs_attention_only_queue(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 45)
        status, spawn = self.heal()
        self.assertEqual((status["pending"], status["needs_attention"], status["recheckable"]), (0, 1, 1))
        spawn.assert_called_once()
        self.assertEqual(status.get("healed"), "pump started")

    def test_status_heal_leaves_a_stale_unresolved_row_alone(self):
        a = self.ambiguous("dev", THREAD, "A")
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET created_at = '2020-01-01T00:00:00+00:00', state = 'needs-attention' WHERE id = ?", (a,))
        status, spawn = self.heal()
        self.assertEqual((status["needs_attention"], status["recheckable"]), (1, 0))
        spawn.assert_not_called()


class TestNeedsAttention(Case):
    def test_an_unresolved_row_becomes_needs_attention_and_stops_holding(self):
        a = self.ambiguous("dev", THREAD, "A")
        b = self.db.enqueue("rev", THREAD, THREAD, "B")
        self.backdate(a, 31)
        relay = ThreadRelay()
        self.assertEqual(self.pump(relay, retry_grace_minutes=0), 0)
        self.assertEqual(self.state(a)["state"], "needs-attention")
        self.assertIn("needs attention", self.state(a)["error"])
        self.assertIn("timeout after the call", self.state(a)["error"])
        self.assertEqual(self.state(b)["state"], "delivered")
        self.assertEqual([p.split("\n", 1)[1] for p in relay.bodies()], ["B"])  # A is still never resent

    def test_the_time_is_configurable_and_the_default_is_thirty_minutes(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 29)
        self.assertEqual(self.db.escalate_ambiguous(outbox.DEFAULT_ATTENTION_MINUTES), 0)
        self.backdate(a, 31)
        self.assertEqual(self.db.escalate_ambiguous(outbox.DEFAULT_ATTENTION_MINUTES), 1)
        b = self.ambiguous("dev", THREAD, "B")
        self.backdate(b, 6)
        self.assertEqual(self.db.escalate_ambiguous(5), 1)

    def test_the_time_comes_from_the_config_file(self):
        (self.tmp / ".bot-mode" / "outbox-config.json").write_text(json.dumps({"attention_minutes": 2}), encoding="utf-8")
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 3)
        self.pump(ThreadRelay(), retry_grace_minutes=0, max_wait_sec=0.3)
        self.assertEqual(self.state(a)["state"], "needs-attention")

    def test_a_legacy_ambiguous_row_without_a_time_starts_its_clock_when_first_seen(self):
        a = self.ambiguous("dev", THREAD, "A")
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET ambiguous_at = NULL")
        self.assertEqual(self.db.escalate_ambiguous(30), 0)
        self.assertIsNotNone(self.state(a)["ambiguous_at"])

    def test_status_and_list_show_it_and_status_reports_a_problem(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 45)
        self.db.close()
        out = io.StringIO()
        with patch("sys.stdout", out), patch("scripts.team_room.outbox.relay_reachable", return_value=True):
            code = outbox.main(["--project", str(self.tmp), "status"])
        status = json.loads(out.getvalue())
        self.assertEqual(code, outbox.EXIT_UNHEALTHY)
        self.assertEqual((status["needs_attention"], status["ambiguous"]), (1, 0))
        self.assertTrue(any("needs-attention" in p for p in status["problems"]))
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(outbox.main(["--project", str(self.tmp), "list", "--state", "needs-attention"]), 0)
        self.assertEqual([r["id"] for r in json.loads(out.getvalue())], [a])
        self.db = outbox.OutboxDB(self.db_path)

    def test_a_late_proof_still_turns_needs_attention_into_delivered(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 45)
        self.db.escalate_ambiguous(30)
        relay = ThreadRelay(history={THREAD: [outbox.delivery_marker(a) + "\nA"]})
        outbox.reconcile_ambiguous(self.db, relay, retry_grace_minutes=1)
        self.assertEqual(self.state(a)["state"], "delivered")

    def test_needs_attention_is_never_retried_automatically_but_an_operator_can_resolve_it(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.backdate(a, 45)
        self.db.escalate_ambiguous(30)
        outbox.reconcile_ambiguous(self.db, ThreadRelay(), retry_grace_minutes=1)
        self.assertEqual(self.state(a)["state"], "needs-attention")
        self.assertEqual(self.db.resolve(a, "retry", "checked"), "pending")
        b = self.ambiguous("dev", THREAD, "B")
        self.backdate(b, 45)
        self.db.escalate_ambiguous(30)
        self.assertEqual(self.db.resolve(b, "skipped"), "skipped")


class TestDigest(Case):
    def test_rows_waiting_for_a_busy_recipient_go_out_as_one_ordered_message(self):
        ids = [self.db.enqueue(sender, THREAD, THREAD, text) for sender, text in (("dev", "first"), ("rev", "second"), ("qa", "third"))]
        relay = ThreadRelay(busy_reads={THREAD: 3})
        self.assertEqual(self.pump(relay), 0)
        self.assertEqual(len(relay.sent), 1)
        prompt = relay.bodies()[0]
        self.assertTrue(prompt.startswith(outbox.DIGEST_HEADER + "3 reports"))
        positions = [prompt.index(outbox.delivery_marker(i)) for i in ids]
        self.assertEqual(positions, sorted(positions))
        for sender, text in (("dev", "first"), ("rev", "second"), ("qa", "third")):
            self.assertIn("From: " + sender, prompt)
            self.assertIn(text, prompt)
        self.assertEqual([self.state(i)["state"] for i in ids], ["delivered"] * 3)

    def test_one_row_keeps_the_single_message_format(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "only")
        relay = ThreadRelay()
        self.pump(relay)
        self.assertEqual(relay.bodies(), [outbox.delivery_marker(a) + "\nonly"])

    def test_a_digest_never_mixes_recipients(self):
        self.db.enqueue("a", THREAD, THREAD, "T1")
        self.db.enqueue("b", OTHER, OTHER, "O1")
        self.db.enqueue("c", THREAD, THREAD, "T2")
        relay = ThreadRelay()
        self.pump(relay)
        self.assertEqual(len(relay.sent), 2)
        self.assertTrue(all(("T1" in p) == ("T2" in p) for p in relay.bodies()))
        self.assertTrue(all(("O1" in p) != ("T1" in p) for p in relay.bodies()))

    def test_digest_max_splits_into_messages_and_one_disables_the_digest(self):
        for n in range(5):
            self.db.enqueue("dev", THREAD, THREAD, "m%d" % n)
        relay = ThreadRelay()
        self.pump(relay, digest_max=2)
        self.assertEqual([p.count("[Outbox delivery: ") for p in relay.bodies()], [2, 2, 1])
        self.assertEqual(self.db.count_state("delivered"), 5)
        for n in range(3):
            self.db.enqueue("dev", THREAD, THREAD, "x%d" % n)
        relay = ThreadRelay()
        self.pump(relay, digest_max=1)
        self.assertEqual([p.count("[Outbox delivery: ") for p in relay.bodies()], [1, 1, 1])

    def test_a_long_backlog_stops_at_the_character_limit(self):
        for n in range(3):
            self.db.enqueue("dev", THREAD, THREAD, "x" * 15000)
        self.assertEqual(len(self.db.next_batch(max_rows=20)), 1)
        self.assertEqual(len(self.db.next_batch(max_rows=20, max_chars=40000)), 2)

    def test_a_digest_does_not_pass_an_earlier_ambiguous_row_of_its_recipient(self):
        a = self.ambiguous("dev", THREAD, "A")
        self.db.enqueue("rev", THREAD, THREAD, "B")
        self.db.enqueue("rev", THREAD, THREAD, "C")
        self.assertEqual(self.db.next_batch(max_rows=10), [])

    def test_a_failed_digest_send_makes_every_row_ambiguous_and_each_is_confirmed_on_its_own(self):
        ids = [self.db.enqueue("dev", THREAD, THREAD, t) for t in ("A", "B", "C")]
        relay = ThreadRelay()
        real_call = relay.call

        def flaky(name, arguments, timeout=40):
            if name == "send_message_to_thread":
                real_call(name, arguments)  # the relay took it ...
                raise TimeoutError("... but the answer was lost")
            return real_call(name, arguments, timeout)

        relay.call = flaky
        with patch("sys.stderr", io.StringIO()):
            self.pump(relay, retry_grace_minutes=0, max_wait_sec=0.5)
        self.assertEqual(len(relay.sent), 1)
        self.assertEqual([self.state(i)["state"] for i in ids], ["delivered"] * 3)  # the history holds one digest with all markers

    def test_a_crash_during_a_digest_makes_all_its_rows_ambiguous_and_none_is_resent(self):
        ids = [self.db.enqueue("dev", THREAD, THREAD, t) for t in ("A", "B")]
        self.db.mark_sending_many(ids, THREAD)
        later = self.db.enqueue("rev", OTHER, OTHER, "C")
        relay = ThreadRelay()
        self.pump(relay, retry_grace_minutes=0, max_wait_sec=0.3)
        self.assertEqual([self.state(i)["state"] for i in ids], ["ambiguous", "ambiguous"])
        self.assertEqual([p.split("\n", 1)[1] for p in relay.bodies()], ["C"])
        self.assertEqual(self.state(later)["state"], "delivered")


class TestPrune(Case):
    def rows(self, n, state="delivered"):
        ids = []
        for k in range(n):
            msg_id = self.db.enqueue("dev", THREAD, THREAD, "r%d" % k)
            if state == "delivered":
                self.db.mark_delivered(msg_id)
            ids.append(msg_id)
        return ids

    def backups(self):
        return sorted((self.tmp / ".bot-mode" / "outbox" / "backups").glob("queue-*.sqlite"))

    def prune(self, **kwargs):
        self.db.close()
        try:
            return outbox.prune_outbox(self.tmp, **kwargs)
        finally:
            self.db = outbox.OutboxDB(self.db_path)

    def test_keeps_the_newest_delivered_rows_and_backs_up_first(self):
        ids = self.rows(10)
        pending = self.db.enqueue("dev", THREAD, THREAD, "waiting")
        amb = self.ambiguous("dev", OTHER, "unsure")
        summary = self.prune(keep_rows=3)
        self.assertEqual(summary["deleted"], 7)
        self.assertEqual(self.db.count_state("delivered"), 3)
        self.assertEqual(self.state(pending)["state"], "pending")
        self.assertEqual(self.state(amb)["state"], "ambiguous")
        left = {r["id"] for r in self.db.conn.execute("SELECT id FROM outbox WHERE state = 'delivered'")}
        self.assertEqual(left, set(ids[-3:]))
        backup = sqlite3.connect(str(self.backups()[-1]))
        try:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM outbox WHERE state = 'delivered'").fetchone()[0], 10)
        finally:
            backup.close()

    def test_nothing_to_do_makes_no_backup_and_dry_run_changes_nothing(self):
        self.rows(2)
        self.assertEqual(self.prune(keep_rows=5)["backup"], None)
        self.assertEqual(self.backups(), [])
        self.rows(6)
        summary = self.prune(keep_rows=3, dry_run=True)
        self.assertEqual((summary["deleted"], summary["backup"]), (5, None))
        self.assertEqual(self.db.count_state("delivered"), 8)
        self.assertEqual(self.backups(), [])

    def test_old_pending_rows_expire_by_the_ttl_after_a_backup(self):
        old = self.db.enqueue("dev", THREAD, THREAD, "old")
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,))
        fresh = self.db.enqueue("dev", THREAD, THREAD, "fresh")
        summary = self.prune(keep_rows=10, ttl_hours=12)
        self.assertEqual(summary["expired"], 1)
        self.assertEqual((self.state(old)["state"], self.state(fresh)["state"]), ("expired", "pending"))
        self.assertEqual(len(self.backups()), 1)

    def test_backups_are_bounded(self):
        for _ in range(5):
            self.rows(2)
            self.prune(keep_rows=1, max_backups=2)
        self.assertEqual(len(self.backups()), 2)

    def test_skipped_and_expired_rows_are_pruned_and_failed_rows_are_kept(self):
        a, b = self.rows(2, state="pending")
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET state = 'skipped' WHERE id = ?", (a,))
            self.db.conn.execute("UPDATE outbox SET state = 'expired' WHERE id = ?", (b,))
        failed = self.db.enqueue("dev", "nobody", None, "F")
        self.db.mark_failed(failed, "no thread")
        self.prune(keep_rows=0)
        self.assertEqual(self.db.count_state("skipped") + self.db.count_state("expired"), 0)
        self.assertEqual(self.state(failed)["state"], "failed")

    def test_cli_prune_and_the_pump_prunes_a_large_backlog_by_itself(self):
        self.rows(9)
        self.db.close()
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(outbox.main(["--project", str(self.tmp), "prune", "--keep", "4", "--max-backups", "1"]), 0)
        self.assertEqual(json.loads(out.getvalue())["deleted"], 5)
        self.db = outbox.OutboxDB(self.db_path)
        self.rows(10)
        (self.tmp / ".bot-mode" / "outbox-config.json").write_text(json.dumps({"keep_rows": 3, "max_backups": 1}), encoding="utf-8")
        self.pump(ThreadRelay())  # 14 delivered rows are more than twice the 3 kept: the pump prunes at its start
        self.assertEqual(self.db.count_state("delivered"), 3)
        self.assertEqual(len(self.backups()), 1)


class TestCompatibility(Case):
    def test_a_database_from_an_older_copy_is_migrated_and_keeps_its_rows(self):
        old_path = self.tmp / "old.sqlite"
        conn = sqlite3.connect(str(old_path))
        conn.execute("CREATE TABLE outbox (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, sender_bot TEXT NOT NULL, target_bot TEXT NOT NULL,"
                     " target_thread_id TEXT, text TEXT NOT NULL, dedupe_key TEXT UNIQUE, state TEXT NOT NULL DEFAULT 'pending',"
                     " delivered_at TEXT, error TEXT)")
        conn.execute("INSERT INTO outbox (id, created_at, sender_bot, target_bot, text, state) VALUES ('x', '2026-01-01T00:00:00+00:00', 'a', 'b', 't', 'ambiguous')")
        conn.commit()
        conn.close()
        db = outbox.OutboxDB(old_path)
        try:
            self.assertEqual(db.count_state("ambiguous"), 1)
            self.assertEqual(db.escalate_ambiguous(30), 0)
        finally:
            db.close()

    def test_send_list_status_keep_their_output(self):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err), patch("scripts.team_room.outbox.spawn_background_pump"), \
                patch("scripts.team_room.outbox.relay_reachable", return_value=True):
            self.assertEqual(outbox.main(["--project", str(self.tmp), "send", "--to", THREAD, "--by", "dev", "--text", "hi"]), 0)
        self.assertIn("Queued outbox handoff", out.getvalue())
        self.assertIn("Queue: 1 pending", out.getvalue())
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(outbox.main(["--project", str(self.tmp), "list"]), 0)
        self.assertEqual(json.loads(out.getvalue())[0]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
