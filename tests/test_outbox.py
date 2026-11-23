import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from scripts.team_room import outbox

THREAD = "00000000-0000-7000-8000-000000000001"
BUSY_THREAD = "00000000-0000-7000-8000-000000000002"
IDLE_DONE = {"thread": {"status": {"type": "idle"}}, "turns": [{"id": "t0", "status": "completed", "startedAt": 1}]}


class FakeRelay:
    """A Manager thread that reacts to the pump: a sent message shows up as a new turn only after register_after
    reads, the turn runs for busy_reads reads, then the thread is idle again."""

    def __init__(self, register_after=3, busy_reads=2, never_register=False, send_error=None):
        self.turns = [{"id": "t0", "status": "completed", "startedAt": 1}]
        self.register_after, self.busy_reads, self.never_register = register_after, busy_reads, never_register
        self.send_error = send_error
        self.countdown = None
        self.running = None
        self.sent, self.violations, self.log = [], [], []

    def call(self, name, arguments, timeout=40):
        if name == "send_message_to_thread":
            if self.send_error:
                raise self.send_error
            if self.countdown is not None or self.running is not None:
                self.violations.append(arguments["prompt"])
            prompt = arguments["prompt"]
            assert prompt.startswith('[Outbox delivery: '), prompt
            body = prompt.split('\n', 1)[1]
            self.sent.append(body)
            self.log.append("send:" + body)
            if not self.never_register:
                self.countdown = self.register_after
            return {}
        assert name == "read_thread", name
        if self.countdown is not None:
            self.countdown -= 1
            if self.countdown <= 0:
                self.countdown = None
                self.turns.insert(0, {"id": "t%d" % len(self.turns), "status": "inProgress", "startedAt": len(self.turns) + 1})
                self.running = self.busy_reads
        elif self.running is not None:
            self.running -= 1
            if self.running <= 0:
                self.running = None
                self.turns[0]["status"] = "completed"
        status = "active" if self.running is not None else "idle"
        self.log.append("read:" + status)
        return {"thread": {"status": {"type": status}}, "turns": [dict(self.turns[0])]}


class OutboxCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_outbox_"))
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

    def pump(self, client, **kwargs):
        kwargs.setdefault("max_wait_sec", 5)
        kwargs.setdefault("poll_interval", 0.001)
        kwargs.setdefault("digest_max", 1)  # these tests check the order of single messages; digests are tested in test_outbox_delivery_422
        self.db.close()
        with patch("scripts.team_room.outbox.RelayClient", return_value=client), \
             patch("scripts.team_room.outbox.find_relay_descriptor", return_value=(self.tmp, {"url": "http://127.0.0.1:1", "token": "t"})):
            code = outbox.pump_outbox(self.tmp, **kwargs)
        self.db = outbox.OutboxDB(self.db_path)
        return code


class TestOutbox(OutboxCase):
    def test_enqueue_and_fifo(self):
        id1 = self.db.enqueue("dev", "manager", "thread-1", "Msg 1")
        id2 = self.db.enqueue("rev", "manager", "thread-1", "Msg 2")

        first = self.db.next_pending()
        self.assertIsNotNone(first)
        self.assertEqual(first["id"], id1)
        self.assertEqual(first["text"], "Msg 1")
        self.assertEqual(first["state"], "pending")

        self.db.mark_delivered(id1)

        second = self.db.next_pending()
        self.assertIsNotNone(second)
        self.assertEqual(second["id"], id2)
        self.assertEqual(second["text"], "Msg 2")

        self.db.mark_delivered(id2)
        self.assertIsNone(self.db.next_pending())

    def test_target_thread_resolution(self):
        roster_data = {
            "participants": [
                {"bot": "manager", "role": "Manager", "thread_id": "00000000-0000-7000-8000-000000000001"},
                {"bot": "developer", "role": "Developer", "thread_id": "00000000-0000-7000-8000-000000000003"}
            ]
        }
        roster_path = self.tmp / ".bot-mode" / "roster.json"
        roster_path.parent.mkdir(parents=True, exist_ok=True)
        roster_path.write_text(json.dumps(roster_data), encoding="utf-8")

        tid = outbox.resolve_target_thread(self.tmp, "manager")
        self.assertEqual(tid, "00000000-0000-7000-8000-000000000001")

        direct = outbox.resolve_target_thread(self.tmp, "00000000-0000-7000-8000-000000000003")
        self.assertEqual(direct, "00000000-0000-7000-8000-000000000003")

    def test_thread_busy_detection(self):
        self.assertTrue(outbox.is_thread_busy({"thread": {"status": {"type": "active"}}}))
        self.assertTrue(outbox.is_thread_busy({
            "thread": {"status": {"type": "idle"}},
            "turns": [{"status": "inProgress"}]
        }))
        self.assertFalse(outbox.is_thread_busy({
            "thread": {"status": {"type": "idle"}},
            "turns": [{"status": "completed"}]
        }))
        self.assertFalse(outbox.is_thread_busy({"thread": {"status": {"type": "idle"}}}))
        self.assertTrue(outbox.is_thread_busy({"thread": {"status": {"type": "unknown"}}}))

    def test_a_chat_that_desktop_has_not_loaded_is_deliverable_unless_a_turn_runs(self):
        not_loaded = {"thread": {"status": {"type": "notLoaded"}}}
        self.assertFalse(outbox.is_thread_busy({**not_loaded, "turns": [{"status": "completed"}]}))
        self.assertFalse(outbox.is_thread_busy({**not_loaded, "turns": []}))
        self.assertTrue(outbox.is_thread_busy({**not_loaded, "turns": [{"status": "inProgress"}]}))
        self.assertTrue(outbox.is_thread_busy(not_loaded))  # no readable history: not proof of an idle chat
        for status in ("completed", "interrupted", "failed"):
            self.assertFalse(outbox.is_thread_busy({**not_loaded, "turns": [{"status": status}]}), status)
        for turns in ([{}], [None], [{"status": "weird"}], [{"status": None}], ["completed"], [{"status": []}], [{"status": {"x": 1}}], {"status": "completed"}, "x", 7):
            self.assertTrue(outbox.is_thread_busy({**not_loaded, "turns": turns}), repr(turns))
        for status in ([], {"x": 1}, 7):
            self.assertTrue(outbox.is_thread_busy({"thread": {"status": status}, "turns": []}), repr(status))
        for status_type in ([], ["idle"], {"x": 1}, {"idle": True}, 7, None):  # the type itself malformed
            self.assertTrue(outbox.is_thread_busy({"thread": {"status": {"type": status_type}}, "turns": []}), repr(status_type))
            self.assertTrue(outbox.is_thread_busy({"thread": {"status": {"type": status_type}}}), repr(status_type))
        for history in (None, [], {"thread": None}, {"thread": {"status": None}}, {"thread": {"status": {"type": "idle"}}, "turns": [None]}):
            outbox.is_thread_busy(history)  # malformed answers never raise

    def test_install_cli_writes_the_outbox_and_a_missing_config_but_keeps_an_existing_one(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            target = outbox.install_cli(project, "D:/Office/.bot-mode/desktop-relay")
            self.assertEqual(target, project / ".bot-mode" / "outbox.py")
            self.assertEqual(target.read_bytes(), Path(outbox.__file__).read_bytes())
            config = project / ".bot-mode" / "outbox-config.json"
            self.assertEqual(json.loads(config.read_text(encoding="utf-8")), {"relay_root": "D:/Office/.bot-mode/desktop-relay"})
            config.write_text('{"relay_root": "kept"}', encoding="utf-8")
            outbox.install_cli(project, "D:/Other")
            self.assertEqual(json.loads(config.read_text(encoding="utf-8")), {"relay_root": "kept"})

    def test_sending_row_is_recovered_to_ambiguous_only_by_the_pump_recovery(self):
        msg_id = self.db.enqueue("dev", "manager", "thread-mgr", "Report text")
        self.db.mark_sending(msg_id)
        self.db.close()

        # Opening the queue (send, list) while a pump is mid send must not change the row.
        db2 = outbox.OutboxDB(self.db_path)
        self.assertEqual(db2.conn.execute("SELECT state FROM outbox WHERE id = ?", (msg_id,)).fetchone()["state"], "sending")
        db2.recover_sending()
        self.assertIsNone(db2.next_pending())
        row = db2.conn.execute("SELECT state FROM outbox WHERE id = ?", (msg_id,)).fetchone()
        self.assertEqual(row["state"], "ambiguous")
        db2.mark_ambiguous(msg_id, "relay outcome unknown")
        row = db2.conn.execute("SELECT state, error FROM outbox WHERE id = ?", (msg_id,)).fetchone()
        self.assertEqual(row["error"], "relay outcome unknown")
        db2.mark_delivered(msg_id)
        row = db2.conn.execute("SELECT state, error FROM outbox WHERE id = ?", (msg_id,)).fetchone()
        self.assertEqual(row["state"], "delivered")
        self.assertIsNone(row["error"])
        db2.close()

    def test_dedupe_key_reuses_existing_message(self):
        first = self.db.enqueue("dev", "manager", "thread-1", "Msg", "request:req-1:dev:done")
        second = self.db.enqueue("dev", "manager", "thread-1", "Msg", "request:req-1:dev:done")
        self.assertEqual(first, second)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_a_concurrent_writer_with_the_same_dedupe_key_returns_the_existing_row(self):
        winner = {}

        class Racing:
            """Another writer inserts the same key between this writer's check and its insert."""
            def __init__(self, real):
                self.real, self.hit = real, False

            def execute(self, sql, params=()):
                if sql.startswith("SELECT id FROM outbox WHERE dedupe_key") and not self.hit:
                    self.hit = True
                    other = outbox.OutboxDB(self_path)
                    winner["id"] = other.enqueue("dev", "manager", "thread-1", "Msg", "request:req-2:dev:done")
                    other.close()
                    return self.real.execute("SELECT id FROM outbox WHERE 0")
                return self.real.execute(sql, params)

            def __enter__(self):
                return self.real.__enter__()

            def close(self):
                self.real.close()

            def __exit__(self, *exc):
                return self.real.__exit__(*exc)

        self_path = self.db_path
        self.db.conn = Racing(self.db.conn)
        got = self.db.enqueue("dev", "manager", "thread-1", "Msg", "request:req-2:dev:done")
        self.assertEqual(got, winner["id"])
        self.assertEqual(self.db.conn.real.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_many_threads_with_one_dedupe_key_queue_one_row(self):
        ids, errors, barrier = [], [], threading.Barrier(6)

        def worker():
            db = outbox.OutboxDB(self.db_path)
            try:
                barrier.wait()
                ids.append(db.enqueue("dev", "manager", "thread-1", "Msg", "request:req-3:dev:done"))
            except Exception as error:  # the test reports it below
                errors.append(error)
            finally:
                db.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 1)

    def test_pump_delivers_when_idle(self):
        msg_id = self.db.enqueue("dev", THREAD, THREAD, "Report text")
        client = MagicMock()
        client.call.side_effect = [
            {"thread": {"status": {"type": "active"}}},  # busy
            IDLE_DONE,  # idle
            {"threadId": "thread-mgr"},  # send_message_to_thread result
            {"thread": {"status": {"type": "active"}}, "turns": [{"id": "t1", "status": "inProgress"}]},  # new turn registered
        ]
        self.assertEqual(self.pump(client), 0)
        self.assertEqual(self.state(msg_id)["state"], "delivered")

    def test_pump_resolves_pending_manager_again_after_roster_change(self):
        old = "11111111-1111-1111-1111-111111111111"
        roster = self.tmp / ".bot-mode" / "roster.json"
        roster.write_text(json.dumps({"participants": [{"bot": "manager", "thread_id": THREAD}]}), encoding="utf-8")
        msg_id = self.db.enqueue("dev", "manager", old, "Report text")
        relay = FakeRelay(register_after=1, busy_reads=1)
        relay.call = MagicMock(side_effect=relay.call)
        self.assertEqual(self.pump(relay), 0)
        send = next(c for c in relay.call.call_args_list if c.args[0] == "send_message_to_thread")
        self.assertEqual(send.args[1]["threadId"], THREAD)
        self.assertEqual(self.state(msg_id)["state"], "delivered")

    def test_pump_does_not_use_stale_manager_when_roster_unavailable(self):
        old = "11111111-1111-1111-1111-111111111111"
        msg_id = self.db.enqueue("dev", "manager", old, "Report text")
        relay = FakeRelay()
        self.assertEqual(self.pump(relay), 0)
        self.assertEqual(relay.sent, [])
        self.assertEqual(self.state(msg_id)["state"], "failed")

    def test_cli_send_and_list(self):
        project_dir = self.tmp / "proj"
        project_dir.mkdir(parents=True, exist_ok=True)
        roster_data = {"participants": [{"bot": "manager", "role": "Manager", "thread_id": "00000000-0000-7000-8000-000000000001"}]}
        (project_dir / ".bot-mode").mkdir(parents=True, exist_ok=True)
        (project_dir / ".bot-mode" / "roster.json").write_text(json.dumps(roster_data), encoding="utf-8")

        with patch("scripts.team_room.outbox.spawn_background_pump") as mock_spawn:
            ret = outbox.main(["--project", str(project_dir), "send", "--to", "manager", "--by", "developer", "--text", "Finished stage 1"])
            self.assertEqual(ret, 0)
            mock_spawn.assert_called_once_with(project_dir.resolve())

        db = outbox.OutboxDB(project_dir / ".bot-mode" / "outbox" / "queue.sqlite")
        item = db.next_pending()
        self.assertIsNotNone(item)
        self.assertEqual(item["sender_bot"], "developer")
        self.assertEqual(item["target_bot"], "manager")
        self.assertEqual(item["target_thread_id"], "00000000-0000-7000-8000-000000000001")
        self.assertEqual(item["text"], "Finished stage 1")
        db.close()

    def test_background_pump_places_global_project_option_before_subcommand(self):
        with patch("scripts.team_room.outbox.subprocess.Popen") as popen:
            outbox.spawn_background_pump(self.tmp)
        args = popen.call_args.args[0]
        self.assertEqual(args[3], "--project")
        self.assertEqual(args[5], "pump")


class TestPumpOrderAndTurns(OutboxCase):
    def test_two_queued_items_are_sent_one_turn_at_a_time(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "A")
        b = self.db.enqueue("rev", THREAD, THREAD, "B")
        relay = FakeRelay(register_after=3, busy_reads=2)
        self.assertEqual(self.pump(relay), 0)
        self.assertEqual(relay.sent, ["A", "B"])
        self.assertEqual(relay.violations, [])
        first, second = relay.log.index("send:A"), relay.log.index("send:B")
        between = relay.log[first:second]
        self.assertIn("read:active", between)  # A registered as a Manager turn and ran before B was sent
        self.assertEqual(relay.log[second - 1], "read:idle")  # and it had finished
        self.assertEqual([self.state(a)["state"], self.state(b)["state"]], ["delivered", "delivered"])

    def test_a_fast_turn_that_finished_between_reads_still_counts_as_registered(self):
        self.db.enqueue("dev", THREAD, THREAD, "A")
        self.db.enqueue("rev", THREAD, THREAD, "B")
        relay = FakeRelay(register_after=1, busy_reads=1)  # registers and finishes within consecutive reads
        self.assertEqual(self.pump(relay), 0)
        self.assertEqual(relay.sent, ["A", "B"])
        self.assertEqual(relay.violations, [])

    def test_no_new_turn_stops_the_pump_before_the_next_item(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "A")
        b = self.db.enqueue("rev", THREAD, THREAD, "B")
        relay = FakeRelay(never_register=True)
        self.assertEqual(self.pump(relay, turn_timeout=0.05), outbox.EXIT_NOT_REGISTERED)
        self.assertEqual(relay.sent, ["A"])
        self.assertEqual(self.state(a)["state"], "delivered")
        self.assertIn("no new Manager turn", self.state(a)["error"])
        self.assertEqual(self.state(b)["state"], "pending")

    def test_ambiguous_send_is_quarantined_without_replay(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "A")
        b = self.db.enqueue("rev", THREAD, THREAD, "B")
        failing = FakeRelay(send_error=TimeoutError("relay timed out after the call"))
        self.assertEqual(self.pump(failing), 0)
        self.assertEqual(self.state(a)["state"], "ambiguous")
        self.assertIn("timed out", self.state(a)["error"])
        self.assertEqual(self.state(b)["state"], "pending")  # B waits behind the unconfirmed A (same recipient)
        # A new pump never replays the uncertain delivery, and B keeps waiting.
        relay = FakeRelay()
        self.assertEqual(self.pump(relay, max_wait_sec=0.3), 0)
        self.assertEqual(relay.sent, [])
        self.db.resolve(a, "retry", "checked the thread: not delivered")
        relay = FakeRelay()
        self.assertEqual(self.pump(relay), 0)
        self.assertEqual(relay.sent, ["A", "B"])

    def test_skipping_the_ambiguous_row_releases_the_rest(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "A")
        self.db.enqueue("rev", THREAD, THREAD, "B")
        self.db.mark_ambiguous(a, "unknown")
        self.db.resolve(a, "skipped")
        relay = FakeRelay()
        self.assertEqual(self.pump(relay), 0)
        self.assertEqual(relay.sent, ["B"])
        self.assertEqual(self.state(a)["state"], "skipped")

    def test_a_stopped_pump_quarantines_sending_and_holds_only_later_rows_of_that_recipient(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "A")
        b = self.db.enqueue("rev", THREAD, THREAD, "B")
        c = self.db.enqueue("rev", BUSY_THREAD, BUSY_THREAD, "C")
        self.db.mark_sending(a)
        relay = MultiRelay(busy_reads=0)
        self.assertEqual(self.pump(relay, max_wait_sec=0.3), 0)
        self.assertEqual(relay.sent, [(BUSY_THREAD, "C")])  # B waits behind the unconfirmed A, C is another recipient
        self.assertEqual([self.state(a)["state"], self.state(b)["state"], self.state(c)["state"]], ["ambiguous", "pending", "delivered"])

    def test_unknown_thread_status_and_read_errors_never_send(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "A")
        weird = MagicMock()
        weird.call.return_value = {"thread": {"status": {"type": "systemError"}}, "turns": []}
        self.assertEqual(self.pump(weird, max_wait_sec=0.05), 0)
        self.assertEqual([c.args[0] for c in weird.call.call_args_list if c.args[0] != "read_thread"], [])
        broken = MagicMock()
        broken.call.side_effect = OSError("relay down")
        self.assertEqual(self.pump(broken, max_wait_sec=0.05), 0)
        self.assertEqual(self.state(a)["state"], "pending")

    def test_unresolvable_target_fails_the_row_and_does_not_hold_later_ones(self):
        a = self.db.enqueue("dev", "nobody", None, "A")
        b = self.db.enqueue("rev", THREAD, THREAD, "B")
        relay = FakeRelay()
        self.assertEqual(self.pump(relay), 0)
        self.assertEqual(self.state(a)["state"], "failed")
        self.assertEqual(relay.sent, ["B"])
        # failed is terminal: the same dedupe key is not queued again; only an explicit retry re-queues it
        self.assertEqual(self.state(b)["state"], "delivered")



class MultiRelay:
    """Two recipient threads: BUSY is occupied for busy_reads reads of its status, IDLE never is. A send registers a new turn at once."""

    def __init__(self, busy_reads):
        self.countdown = busy_reads
        self.turns = {}
        self.sent = []

    def call(self, name, arguments, timeout=40):
        thread = arguments["threadId"]
        if name == "send_message_to_thread":
            self.turns[thread] = self.turns.get(thread, 0) + 1
            self.sent.append((thread, arguments["prompt"].split("\n", 1)[1]))
            return {}
        assert name == "read_thread", name
        busy = thread == BUSY_THREAD and self.countdown > 0
        if thread == BUSY_THREAD:
            self.countdown -= 1
        count = self.turns.get(thread, 0)
        return {"thread": {"status": {"type": "active" if busy else "idle"}},
                "turns": [{"id": "t%d" % count, "status": "inProgress" if busy else "completed", "startedAt": count + 1}]}


class TestBusyRecipientDoesNotBlockOthers(OutboxCase):
    def test_messages_to_an_idle_thread_pass_a_head_row_for_a_busy_thread(self):
        self.db.enqueue("a", BUSY_THREAD, BUSY_THREAD, "A1")
        self.db.enqueue("b", THREAD, THREAD, "M1")
        self.db.enqueue("a", BUSY_THREAD, BUSY_THREAD, "A2")
        self.db.enqueue("c", THREAD, THREAD, "M2")
        relay = MultiRelay(busy_reads=30)
        self.assertEqual(self.pump(relay), 0)
        bodies = [body for _, body in relay.sent]
        self.assertEqual(sorted(bodies), ["A1", "A2", "M1", "M2"])  # all delivered in the end
        self.assertLess(bodies.index("M1"), bodies.index("A1"))  # the idle thread did not wait for the busy one
        self.assertLess(bodies.index("M2"), bodies.index("A1"))
        self.assertLess(bodies.index("M1"), bodies.index("M2"))  # order per recipient is kept
        self.assertLess(bodies.index("A1"), bodies.index("A2"))

    def test_when_every_recipient_is_busy_nothing_is_sent_until_one_is_free(self):
        self.db.enqueue("a", BUSY_THREAD, BUSY_THREAD, "A1")
        relay = MultiRelay(busy_reads=5)
        self.assertEqual(self.pump(relay), 0)
        self.assertEqual(relay.sent, [(BUSY_THREAD, "A1")])

class TestResolve(OutboxCase):
    def test_resolve_rules(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "A")
        with self.assertRaises(ValueError):
            self.db.resolve(a, "skipped")  # pending rows are not resolved
        self.db.mark_ambiguous(a, "unknown")
        self.assertEqual(self.db.resolve(a, "delivered", "arrived"), "delivered")
        row = self.state(a)
        self.assertIsNotNone(row["delivered_at"])
        self.assertIn("resolved by operator: delivered: arrived", row["error"])
        with self.assertRaises(ValueError):
            self.db.resolve(a, "retry")  # delivered rows are final
        with self.assertRaises(ValueError):
            self.db.resolve(a, "bogus")
        f = self.db.enqueue("dev", "nobody", None, "F")
        self.db.mark_failed(f, "no thread")
        with self.assertRaises(ValueError):
            self.db.resolve(f, "skipped")
        self.assertEqual(self.db.resolve(f, "retry"), "pending")

    def test_retry_keeps_the_original_position(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "A")
        b = self.db.enqueue("rev", THREAD, THREAD, "B")
        self.db.mark_ambiguous(a, "unknown")
        self.db.resolve(a, "retry")
        self.assertEqual(self.db.next_pending()["id"], a)
        self.assertEqual(self.state(b)["state"], "pending")

    def test_cli_resolve_and_list_by_state(self):
        a = self.db.enqueue("dev", THREAD, THREAD, "A")
        self.db.enqueue("rev", THREAD, THREAD, "B")
        self.db.mark_ambiguous(a, "unknown")
        self.db.close()
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(outbox.main(["--project", str(self.tmp), "list", "--state", "ambiguous"]), 0)
        rows = json.loads(out.getvalue())
        self.assertEqual([r["id"] for r in rows], [a])
        with patch("scripts.team_room.outbox.spawn_background_pump") as spawn:
            self.assertEqual(outbox.main(["--project", str(self.tmp), "resolve", a, "--as", "skipped", "--note", "operator"]), 0)
            spawn.assert_called_once()  # a pending row remains, so the pump is started again
            with patch("sys.stderr", io.StringIO()):
                self.assertEqual(outbox.main(["--project", str(self.tmp), "resolve", a, "--as", "retry"]), 2)
        self.db = outbox.OutboxDB(self.db_path)
        self.assertEqual(self.state(a)["state"], "skipped")


class TestRelayUrl(OutboxCase):
    def test_only_http_loopback_is_accepted(self):
        for url in ("http://127.0.0.1:24243", "http://localhost:1/", "http://[::1]:24243"):
            self.assertEqual(outbox.validate_relay_url(url), url.rstrip("/"))
        for url in ("https://127.0.0.1:24243", "http://example.com:24243", "http://127.0.0.1.evil.example", "http://evil.example@127.0.0.1:1",
                    "http://127.0.0.1@evil.example", "http://0.0.0.0:1", "http://192.168.1.5:1", "ftp://127.0.0.1", "http://127.0.0.1:99999",
                    "http://127.0.0.1:1/?x=1", "http://127.0.0.1:1/#x", "file:///etc/passwd", "", "127.0.0.1:1"):
            with self.assertRaises(ValueError, msg=url):
                outbox.validate_relay_url(url)

    def descriptor(self, url):
        folder = self.tmp / "relay"
        folder.mkdir(exist_ok=True)
        (folder / "relay.json").write_text(json.dumps({"url": url, "token": "secret-token"}), encoding="utf-8")
        return folder

    def test_client_refuses_an_external_host_before_any_network_call(self):
        folder = self.descriptor("http://relay.example.com:24243")
        with patch("scripts.team_room.outbox.urllib.request.build_opener") as opener:
            with self.assertRaises(ValueError):
                outbox.RelayClient(folder)
            opener.assert_not_called()

    def test_client_sends_the_token_only_to_the_loopback_relay_and_never_follows_redirects(self):
        folder = self.descriptor("http://127.0.0.1:24243")
        client = outbox.RelayClient(folder)
        fake = MagicMock()
        fake.open.return_value.__enter__.return_value.read.return_value = b'{"result": {"ok": true}}'
        with patch("scripts.team_room.outbox.urllib.request.build_opener", return_value=fake) as build:
            self.assertEqual(client.call("read_thread", {}), {"ok": True})
        request = fake.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:24243/call")
        self.assertEqual(request.get_header("X-office-relay"), "secret-token")
        handlers = build.call_args.args
        self.assertTrue(any(h is outbox._NoRedirect for h in handlers))
        self.assertIsNone(outbox._NoRedirect().redirect_request(MagicMock(), MagicMock(), 302, "Found", {}, "http://evil.example"))

    def test_descriptor_search_skips_an_external_relay(self):
        project = self.tmp / "proj"
        folder = project / ".bot-mode" / "desktop-relay"
        folder.mkdir(parents=True)
        (folder / "relay.json").write_text(json.dumps({"url": "http://relay.example.com:1", "token": "t"}), encoding="utf-8")
        err = io.StringIO()
        with patch("sys.stderr", err), patch.dict(os.environ, {"CODEX_RELAY_ROOT": ""}):
            self.assertEqual(outbox.find_relay_descriptor(project), (None, None))
        self.assertIn("ignoring relay descriptor", err.getvalue())
        self.assertNotIn("t\n", err.getvalue().replace("ignoring", ""))
        (folder / "relay.json").write_text(json.dumps({"url": "http://127.0.0.1:2", "token": "t"}), encoding="utf-8")
        root, data = outbox.find_relay_descriptor(project)
        self.assertEqual(root, folder)

    def test_pump_without_a_loopback_descriptor_sends_nothing(self):
        project = self.tmp / "proj2"
        folder = project / ".bot-mode" / "desktop-relay"
        folder.mkdir(parents=True)
        (folder / "relay.json").write_text(json.dumps({"url": "https://relay.example.com", "token": "t"}), encoding="utf-8")
        with patch("scripts.team_room.outbox.RelayClient") as client, patch("sys.stderr", io.StringIO()), \
             patch.dict(os.environ, {"CODEX_RELAY_ROOT": ""}):
            self.assertEqual(outbox.pump_outbox(project, max_wait_sec=0.05, poll_interval=0.001), 0)  # waits for a relay, sends nothing
            client.assert_not_called()


class TestPumpLock(OutboxCase):
    def test_second_pump_cannot_take_a_held_lock_and_the_lock_is_released(self):
        path = self.tmp / "pump.lock"
        first, second = outbox.PumpLock(path), outbox.PumpLock(path)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()
        self.assertTrue(path.exists())  # the file stays; nothing to take over or delete

    def test_a_pump_that_finds_the_lock_held_leaves_the_queue_alone(self):
        self.db.enqueue("dev", THREAD, THREAD, "A")
        self.db.close()
        lock = outbox.PumpLock(self.tmp / ".bot-mode" / "outbox" / "pump.lock")
        self.assertTrue(lock.acquire())
        try:
            with patch("scripts.team_room.outbox.RelayClient") as client:
                self.assertEqual(outbox.pump_outbox(self.tmp, max_wait_sec=0.05), 0)
                client.assert_not_called()
        finally:
            lock.release()
        self.db = outbox.OutboxDB(self.db_path)
        self.assertIsNotNone(self.db.next_pending())


if __name__ == "__main__":
    unittest.main()

