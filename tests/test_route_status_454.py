"""Route refusal (#454): a project that is not registered in Office gets a named problem instead of silence.

The relay answers an unappointed chat with HTTP 400 and says so in the text. A relay that does not say so answers a plain HTTP 400.
The outbox tells the two apart, records the refusal per recipient and shows it in status and send. Nothing here writes the
Office registry, and nothing touches a live relay, queue or task."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.team_room import desktop_relay, outbox
from scripts.team_room.work import MANAGER as DESKTOP_MANAGER
from tests.outbox_chaos import DEVELOPER, MANAGER, Desktop, Project, wait_for

OLD_RELAY_TEXT = "Invalid Desktop relay request"


class FakeBridge:
    origin = DESKTOP_MANAGER

    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return {}


class RealRelayTests(unittest.TestCase):
    """The HTTP handler of this repository, through the real client of outbox.py."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = FakeBridge()
        for name in ("targets", "read_only_targets", "sendable_targets"):
            patcher = patch.object(desktop_relay, name, return_value=frozenset())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = desktop_relay.create_relay(self.bridge, self.temp.name, port=0)
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = outbox.RelayClient(self.temp.name)

    def refusal(self, name, arguments):
        with self.assertRaises(RuntimeError) as caught:
            self.client.call(name, arguments)
        return caught.exception

    def test_an_unappointed_chat_is_refused_with_the_named_text_for_a_read_and_a_send(self):
        for name, arguments in (("read_thread", {"threadId": "x"}), ("send_message_to_thread", {"threadId": "x", "prompt": "p"})):
            error = self.refusal(name, arguments)
            self.assertTrue(str(error).startswith("HTTP Error 400"), error)
            self.assertIn(outbox.RELAY_REFUSAL, str(error))
            self.assertEqual(outbox.route_refusal_kind(error), "named")
        self.assertEqual(self.bridge.calls, [])

    def test_any_other_invalid_request_keeps_the_generic_text(self):
        error = self.refusal("create_thread", {})
        self.assertIn(OLD_RELAY_TEXT, str(error))
        self.assertNotIn(outbox.RELAY_REFUSAL, str(error))
        self.assertEqual(outbox.route_refusal_kind(error), "probable")

    def test_the_text_of_the_relay_and_of_the_client_are_the_same_string(self):
        self.assertEqual(desktop_relay.REFUSAL_TEXT, outbox.RELAY_REFUSAL)


class ClassificationTests(unittest.TestCase):
    def test_only_http_400_is_a_route_refusal(self):
        self.assertEqual(outbox.route_refusal_kind(RuntimeError("HTTP Error 400: Bad Request (%s)" % outbox.RELAY_REFUSAL)), "named")
        self.assertEqual(outbox.route_refusal_kind(RuntimeError("HTTP Error 400: Bad Request (%s)" % OLD_RELAY_TEXT)), "probable")
        self.assertEqual(outbox.route_refusal_kind(RuntimeError("HTTP Error 400: Bad Request")), "probable")
        for other in (RuntimeError("HTTP Error 503: Service Unavailable (thread not found)"), RuntimeError("HTTP Error 401: Unauthorized"),
                      RuntimeError("HTTP Error 503: x (%s)" % outbox.RELAY_REFUSAL), OSError("HTTP Error 400"), TimeoutError("timed out")):
            self.assertIsNone(outbox.route_refusal_kind(other), other)

    def test_a_missing_or_damaged_record_reads_as_empty_and_a_note_repairs_it(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / ".bot-mode" / "outbox").mkdir(parents=True)
            self.assertEqual(outbox.read_route_status(folder), {})
            for text in ("{not json", "[]", '"x"', '{"recipients": 5}', '{"recipients": {"manager": 5}}'):
                outbox.route_status_file(folder).write_text(text)
                self.assertEqual(outbox.read_route_status(folder), {}, text)
            outbox.note_route_refused(folder, "manager", "named")
            outbox.note_route_refused(folder, "manager", "probable")
            entry = outbox.read_route_status(folder)["manager"]
            self.assertEqual((entry["cause"], entry["count"]), ("probable", 2))
            self.assertLessEqual(entry["first_at"], entry["last_at"])
            outbox.clear_route_refused(folder, "developer")
            self.assertIn("manager", outbox.read_route_status(folder))
            outbox.clear_route_refused(folder, "manager")
            self.assertFalse(outbox.route_status_file(folder).exists())

    def test_the_problem_text_names_the_cause_or_says_probably_and_gives_the_consent_remedy(self):
        named = outbox.route_problem_text("manager", "named")
        probable = outbox.route_problem_text("manager", "probable")
        self.assertIn("the relay refused the manager chat", named)
        self.assertNotIn("probably", named)
        self.assertIn("probably", probable)
        self.assertNotIn("the relay refused", probable)
        for text in (named, probable):
            self.assertIn("owner's consent", text)
            self.assertIn("never write the Office registry", text)
            self.assertFalse(any(character.isdigit() for character in text.replace("HTTP 400", "")),
                             "no count or time in it: the supervisor makes an incident key of the text")


class PumpTests(unittest.TestCase):
    def setUp(self):
        self.project = Project()
        self.addCleanup(self.project.close)
        self.desktop = Desktop()
        self.desktop.add_chat(MANAGER)
        self.desktop.add_chat(DEVELOPER)
        self.relay = self.project.relay(self.desktop)

    def recorded(self):
        return outbox.read_route_status(self.project.root).get("manager")

    def refuse_and_pump(self, text):
        self.relay.refused_reads[MANAGER] = text
        row = self.project.enqueue("developer", "manager", "report")
        self.project.pump()
        wait_for(lambda: self.recorded() is not None, what="the route status record")
        return row

    def test_the_named_refusal_is_recorded_shown_in_status_and_the_row_waits(self):
        row = self.refuse_and_pump(outbox.RELAY_REFUSAL)
        self.assertEqual(self.recorded()["cause"], "named")
        status = outbox.queue_status(self.project.root)
        self.assertEqual([item["recipient"] for item in status["route_refused"]], ["manager"])
        self.assertIn(outbox.route_problem_text("manager", "named"), status["problems"])
        self.assertEqual(self.project.state(row), "pending")
        self.assertEqual(self.desktop.sends, [])

    def test_a_plain_400_from_an_older_relay_is_recorded_as_probable(self):
        self.refuse_and_pump(OLD_RELAY_TEXT)
        self.assertEqual(self.recorded()["cause"], "probable")
        problems = outbox.queue_status(self.project.root)["problems"]
        self.assertTrue(any("probably" in problem for problem in problems), problems)

    def test_another_recipient_is_delivered_while_the_refused_one_waits(self):
        self.refuse_and_pump(outbox.RELAY_REFUSAL)
        other = self.project.enqueue("manager", "developer", "to the developer")
        wait_for(lambda: self.project.state(other) == "delivered", what="the delivery to the other recipient")
        self.assertEqual(self.recorded()["cause"], "named")

    def test_the_record_ends_with_the_next_answered_read_and_the_row_is_then_delivered(self):
        row = self.refuse_and_pump(outbox.RELAY_REFUSAL)
        self.relay.refused_reads.clear()
        wait_for(lambda: self.project.state(row) == "delivered", timeout=40, what="the delivery after the route works")
        wait_for(lambda: self.recorded() is None, what="the record to end")
        status = outbox.queue_status(self.project.root)
        self.assertEqual(status["route_refused"], [])
        self.assertFalse(any("Office registry" in problem for problem in status["problems"]))

    def test_a_503_for_the_chat_is_not_a_route_refusal(self):
        self.desktop.unknown.add(MANAGER)
        row = self.project.enqueue("developer", "manager", "report")
        pump = self.project.pump()
        wait_for(lambda: "setting it aside" in self.project.output(pump), what="the pump to set the recipient aside")
        self.assertEqual(self.project.state(row), "pending")
        self.assertIsNone(self.recorded())
        self.assertEqual(outbox.queue_status(self.project.root)["route_refused"], [])

    def test_send_warns_on_stderr_and_keeps_the_first_two_lines_and_the_receipt(self):
        self.refuse_and_pump(outbox.RELAY_REFUSAL)
        process = self.project.send("developer", "manager", "second report", "k2")
        process.wait(timeout=60)
        lines = self.project.output(process).splitlines()
        stdout = [line for line in lines if line.startswith(("Queued outbox handoff ", "Queue: ", "Relay: "))]
        self.assertEqual([line.split(" ")[0] for line in stdout], ["Queued", "Queue:", "Relay:"], lines)  # the order doctor relies on
        self.assertTrue(any(line.startswith("WARNING: reports to manager wait because the relay refused the manager chat") for line in lines), lines)

    def test_the_canary_records_and_clears_the_refusal_too(self):
        self.relay.refused_reads[MANAGER] = outbox.RELAY_REFUSAL
        self.project.canary()
        self.project.pump()
        wait_for(lambda: self.recorded() is not None, what="the route status record of the canary")
        self.relay.refused_reads.clear()
        self.project.settle_canary()
        wait_for(lambda: self.recorded() is None, what="the record to end")

    def test_nothing_writes_an_office_registry(self):
        self.refuse_and_pump(outbox.RELAY_REFUSAL)
        outbox.queue_status(self.project.root)
        self.assertFalse((self.project.root / ".bot-mode" / "local").exists())
        self.assertEqual(list(self.project.root.rglob("office-project-roots.json")), [])


if __name__ == "__main__":
    unittest.main()
