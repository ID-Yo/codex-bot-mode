"""The zero-token canary of the outbox (#426, S6-B): a synthetic probe of the whole delivery path that sends nothing.

It runs against the scripted fake Desktop and relay of tests/outbox_chaos.py, with the real pump and the real command in
their own processes."""
import io
import json
import contextlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.team_room import outbox
from tests.outbox_chaos import DEVELOPER, MANAGER, Desktop, Project, wait_for


class CanaryCase(unittest.TestCase):
    def setUp(self):
        self.project = Project()
        self.addCleanup(self.project.close)
        self.desktop = Desktop()
        self.desktop.add_chat(MANAGER)
        self.desktop.add_chat(DEVELOPER)

    def done(self):
        return [c for c in self.project.canaries() if c["state"] == "done"]


class CanaryCommand(unittest.TestCase):
    """The command and the records, in process."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_canary_426_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_command(self, *arguments):
        out = io.StringIO()
        with patch.object(outbox, "spawn_background_pump") as spawn, patch.object(outbox, "pump_alive", return_value=False), \
                contextlib.redirect_stdout(out):
            code = outbox.main(["--project", str(self.tmp), "canary", *arguments])
        return code, json.loads(out.getvalue()), spawn

    def test_it_queues_one_canary_and_starts_a_pump_for_it(self):
        code, report, spawn = self.run_command()
        self.assertEqual((code, report["queued"], report["problems"]), (0, True, []))
        spawn.assert_called_once()
        self.assertIsNone(report["last_latency_seconds"])  # no canary has finished yet: unknown, not zero

    def test_a_second_call_inside_the_interval_or_with_one_pending_queues_nothing(self):
        self.run_command()
        code, report, spawn = self.run_command()
        self.assertEqual(report["queued"], False)
        spawn.assert_called_once()  # a pending canary with no pump running gets a pump

    def test_after_the_interval_a_new_canary_is_queued_once_the_last_is_finished(self):
        self.run_command()
        db = outbox.OutboxDB(self.tmp / ".bot-mode" / "outbox" / "queue.sqlite")
        try:
            db.canary_done(db.canary_pending()["id"], 1.5, 0.2, "idle")
            self.assertIsNone(db.canary_start("manager", 600))  # too young
            self.assertIsNotNone(db.canary_start("manager", 0))
        finally:
            db.close()

    def test_only_the_newest_twenty_records_are_kept(self):
        db = outbox.OutboxDB(self.tmp / ".bot-mode" / "outbox" / "queue.sqlite")
        try:
            for _ in range(outbox.CANARY_KEEP + 5):
                db.canary_done(db.canary_start("manager", 0), 1.0, 0.1, "idle")
            db.canary_start("manager", 0)
            self.assertLessEqual(db.conn.execute("SELECT COUNT(*) FROM canary").fetchone()[0], outbox.CANARY_KEEP + 1)
        finally:
            db.close()

    def test_a_canary_is_never_an_outbox_row_and_changes_no_count_of_the_queue(self):
        self.run_command()
        status = outbox.queue_status(self.tmp)
        self.assertEqual((status["pending"], status["ambiguous"], status["needs_attention"], status["last_delivered_at"]), (0, 0, 0, None))
        self.assertEqual(status["canary"]["last_ok_at"], None)
        db = outbox.OutboxDB(self.tmp / ".bot-mode" / "outbox" / "queue.sqlite")
        try:
            self.assertEqual(db.list_recent(), [])
            self.assertEqual(db.pending_count(), 0)
        finally:
            db.close()


class CanaryRace(unittest.TestCase):
    def test_two_callers_at_the_same_moment_queue_one_canary(self):
        import threading
        tmp = Path(tempfile.mkdtemp(prefix="test_canary_race_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / ".bot-mode" / "outbox" / "queue.sqlite"
        outbox.OutboxDB(path).close()
        results, start = [], threading.Barrier(8)

        def caller():
            db = outbox.OutboxDB(path)
            try:
                start.wait()
                results.append(db.canary_start("manager", 0))
            finally:
                db.close()
        threads = [threading.Thread(target=caller) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len([r for r in results if r]), 1)
        db = outbox.OutboxDB(path)
        try:
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM canary").fetchone()[0], 1)
        finally:
            db.close()


class CanaryThroughThePump(CanaryCase):
    def test_the_pump_processes_the_canary_with_a_latency_and_sends_nothing(self):
        self.project.relay(self.desktop)
        code, report = self.project.canary()
        self.assertEqual((code, report["queued"]), (0, True))
        pump = self.project.pump()
        wait_for(lambda: self.done(), what="the canary")
        record = self.done()[0]
        self.assertEqual(record["chat_status"], "idle")
        self.assertGreaterEqual(record["latency"], 0)
        self.assertLess(record["latency"], 10)
        self.assertEqual(self.desktop.sends, [])  # zero tokens: no message reached any chat
        self.assertEqual(self.project.rows(), {})  # and no report row exists
        self.assertEqual(pump.wait(timeout=20), 0)  # the pump has nothing else to do and ends
        code, report = self.project.canary("--every-minutes", "60")
        self.assertEqual((code, report["queued"], report["problems"]), (0, False, []))
        self.assertEqual(report["last_latency_seconds"], record["latency"])
        self.assertEqual(report["last_chat_status"], "idle")

    def test_a_busy_manager_does_not_delay_the_canary_and_the_status_is_recorded(self):
        self.project.relay(self.desktop)
        self.desktop.set(MANAGER, status="active")
        self.project.canary()
        self.project.pump()
        wait_for(lambda: self.done(), what="the canary")
        self.assertEqual(self.done()[0]["chat_status"], "active")
        self.assertEqual(self.desktop.sends, [])

    def test_a_real_report_and_a_canary_in_the_same_run_each_do_their_own_thing(self):
        self.project.relay(self.desktop)
        row = self.project.enqueue("developer", "manager", "a real report")
        self.project.canary()
        self.project.pump()
        wait_for(lambda: self.done() and self.project.state(row) == "delivered", what="both")
        self.assertEqual(len(self.desktop.sent_to(MANAGER)), 1)
        self.assertIn("a real report", self.desktop.sent_to(MANAGER)[0])
        self.assertNotIn("canary", self.desktop.sent_to(MANAGER)[0].lower())


class CanaryUnknownAnswers(CanaryCase):
    """An answer the pump would not deliver into is never a success (review of #446)."""
    ANSWERS = {
        "empty": {},
        "system error": {"thread": {"status": {"type": "systemError"}}, "turns": []},
        "no text status": {"thread": {"status": {"type": None}}},
        "notLoaded with an unknown newest turn": {"thread": {"status": {"type": "notLoaded"}}, "turns": [{"id": "t1", "status": "weird"}]},
        "notLoaded without turns": {"thread": {"status": {"type": "notLoaded"}}},
        "notLoaded with turns that are not a list": {"thread": {"status": {"type": "notLoaded"}}, "turns": "none"},
        "null": None,
        "text": "ok",
        "a thread without a status": {"thread": {}},
        "a status without a type": {"thread": {"status": {}}},
    }

    def test_each_unreadable_answer_leaves_the_canary_pending_and_becomes_a_problem_after_the_limit(self):
        for name, answer in self.ANSWERS.items():
            with self.subTest(name):
                project, desktop = Project(), Desktop()
                self.addCleanup(project.close)
                desktop.add_chat(MANAGER)
                desktop.set(MANAGER, reply=answer)
                project.relay(desktop)
                project.canary()
                project.pump(max_wait_sec=3)
                wait_for(lambda: project.canaries()[0]["error"], what="the recorded error")
                self.assertEqual([c for c in project.canaries() if c["state"] == "done"], [])
                project.age_canary(6 * 60)
                code, report = project.canary()
                self.assertEqual(code, outbox.EXIT_UNHEALTHY)
                self.assertIsNone(report["last_ok_at"])  # no success was recorded
                self.assertIn("outbox canary has waited 6 min", report["problems"][0])
                self.assertIn("answered with", report["problems"][0])
                self.assertEqual(desktop.sends, [])

    def test_every_answer_the_pump_cannot_deliver_into_is_a_probe_failure_except_a_busy_chat(self):
        """The rule of is_thread_busy, case by case: an unreadable answer, a status outside active/idle/notLoaded and a notLoaded
        chat without a readable turn list are failures; an active chat is busy for the pump but a success for the probe."""
        for name, answer in self.ANSWERS.items():
            with self.subTest(name):
                self.assertTrue(outbox.is_thread_busy(answer))  # the pump would hold every real report for this chat
                self.assertIsNotNone(outbox.canary_chat_status(answer)[1])
        for name, answer in (("idle", {"thread": {"status": {"type": "idle"}}}),
                             ("notLoaded, turns a list and empty", {"thread": {"status": {"type": "notLoaded"}}, "turns": []}),
                             ("notLoaded with a finished turn", {"thread": {"status": {"type": "notLoaded"}}, "turns": [{"id": "t", "status": "completed"}]})):
            with self.subTest(name):
                self.assertFalse(outbox.is_thread_busy(answer))  # the pump would deliver
                self.assertIsNone(outbox.canary_chat_status(answer)[1])
        active = {"thread": {"status": {"type": "active"}}}
        self.assertTrue(outbox.is_thread_busy(active))
        self.assertEqual(outbox.canary_chat_status(active), ("active", None))

    def test_the_status_is_in_the_error_text(self):
        desktop = Desktop()
        desktop.add_chat(MANAGER)
        desktop.set(MANAGER, reply=self.ANSWERS["system error"])
        project = Project()
        self.addCleanup(project.close)
        project.relay(desktop)
        project.canary()
        project.pump(max_wait_sec=3)
        wait_for(lambda: project.canaries()[0]["error"], what="the recorded error")
        self.assertIn("systemError", project.canaries()[0]["error"])

    def test_the_statuses_the_pump_understands_are_successes(self):
        for name, answer in (("idle", {"thread": {"status": {"type": "idle"}}}), ("active", {"thread": {"status": {"type": "active"}}}),
                             ("notLoaded without turns", {"thread": {"status": {"type": "notLoaded"}}, "turns": []}),
                             ("notLoaded with a finished turn", {"thread": {"status": {"type": "notLoaded"}}, "turns": [{"id": "t", "status": "completed"}]})):
            with self.subTest(name):
                self.assertIsNone(outbox.canary_chat_status(answer)[1])


class CanaryFailures(CanaryCase):
    def test_a_dead_relay_leaves_the_canary_waiting_and_raises_the_problem_after_the_limit_then_it_clears(self):
        relay = self.project.relay(self.desktop)
        relay.kill()
        self.project.canary()
        pump = self.project.pump()
        wait_for(lambda: "backing off" in self.project.output(pump) or "canary not processed" in self.project.output(pump), what="the pump noticing")
        self.assertEqual(self.done(), [])
        code, report = self.project.canary()
        self.assertEqual((code, report["problems"]), (0, []))  # inside the limit: waiting is not yet a problem
        self.project.age_canary(6 * 60)
        code, report = self.project.canary()
        self.assertEqual(code, outbox.EXIT_UNHEALTHY)
        self.assertIn("outbox canary has waited 6 min without being processed", report["problems"][0])
        self.project.relay(self.desktop)  # the relay is back with a new port and token
        self.project.settle_canary()
        code, report = self.project.canary("--every-minutes", "60")
        self.assertEqual((code, report["problems"]), (0, []))
        self.assertGreaterEqual(report["last_latency_seconds"], 6 * 60)  # the honest latency of that outage

    def test_a_chat_the_relay_cannot_read_stays_pending_with_the_error_in_the_problem(self):
        self.project.relay(self.desktop)
        self.desktop.unknown.add(MANAGER)
        self.project.canary()
        self.project.pump()
        wait_for(lambda: self.project.canaries()[0]["error"], what="the recorded error")
        self.project.age_canary(6 * 60)
        code, report = self.project.canary()
        self.assertEqual(code, outbox.EXIT_UNHEALTHY)
        self.assertIn("thread not found", report["problems"][0])
        self.assertEqual(self.desktop.sends, [])

    def test_a_roster_without_a_manager_chat_is_a_problem_not_a_success(self):
        self.project.relay(self.desktop)
        (self.project.root / ".bot-mode" / "roster.json").write_text('{"participants": []}', encoding="utf-8")
        self.project.canary()
        self.project.pump()
        wait_for(lambda: self.project.canaries()[0]["error"], what="the recorded error")
        self.project.age_canary(6 * 60)
        code, report = self.project.canary()
        self.assertEqual(code, outbox.EXIT_UNHEALTHY)
        self.assertIn("cannot resolve the manager chat", report["problems"][0])


if __name__ == "__main__":
    unittest.main()
