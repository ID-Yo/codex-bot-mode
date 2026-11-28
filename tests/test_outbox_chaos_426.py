"""Chaos suite for the outbox (#426, S6-A): real processes and real HTTP against a scripted fake relay and Desktop.

Each test names one failure mode. The oracle is always the same: a report is delivered once, never twice, never lost,
and nothing is sent into a chat whose state is unknown. See tests/outbox_chaos.py for what the fake can and cannot prove."""
import time
import unittest

from scripts.team_room import outbox
from tests.outbox_chaos import DEVELOPER, MANAGER, Desktop, Project, wait_for


def marker(row_id):
    return outbox.delivery_marker(row_id)


class ChaosCase(unittest.TestCase):
    def setUp(self):
        self.project = Project()
        self.addCleanup(self.project.close)
        self.desktop = Desktop()
        self.desktop.add_chat(MANAGER)
        self.desktop.add_chat(DEVELOPER)

    def times_in_chat(self, thread, row_id):
        return sum(prompt.count(marker(row_id)) for prompt in self.desktop.sent_to(thread))


class RelayDiesMidDelivery(ChaosCase):
    def test_the_relay_dies_after_taking_the_message_so_the_row_is_confirmed_and_never_sent_again(self):
        first = self.project.relay(self.desktop)
        first.die_after_taking = True
        row = self.project.enqueue("developer", "manager", "report one")
        pump = self.project.pump()
        wait_for(lambda: self.project.state(row) == "ambiguous", what="the unconfirmed row")
        self.assertEqual(len(self.desktop.sends), 1)
        self.project.relay(self.desktop)  # a new relay: new port, new token, new descriptor
        self.project.settle([row])
        self.assertEqual(self.times_in_chat(MANAGER, row), 1)
        self.assertEqual(len(self.desktop.sends), 1)
        pump.wait(timeout=30)

    def test_the_relay_dies_before_taking_the_message_so_it_is_sent_once_after_the_history_proves_it_missing(self):
        first = self.project.relay(self.desktop)
        first.die_before_taking = True
        row = self.project.enqueue("developer", "manager", "report two")
        self.project.pump()
        wait_for(lambda: self.project.state(row) == "ambiguous", what="the unconfirmed row")
        self.assertEqual(self.desktop.sends, [])
        self.project.relay(self.desktop)
        self.project.settle([row])
        self.assertEqual(self.times_in_chat(MANAGER, row), 1)
        self.assertEqual(len(self.desktop.sends), 1)


class PumpKilledMidSend(ChaosCase):
    def test_a_pump_killed_after_the_message_was_taken_does_not_send_it_again(self):
        relay = self.project.relay(self.desktop)
        relay.hold_after_taking = True
        row = self.project.enqueue("developer", "manager", "report three")
        pump = self.project.pump()
        wait_for(relay.held.is_set, what="the held send")
        pump.kill()
        pump.wait()
        relay.release.set()
        self.assertEqual(self.project.state(row), "sending")  # the pump died between the send and the bookkeeping
        self.project.relay(self.desktop)
        self.project.settle([row])
        self.assertEqual(self.times_in_chat(MANAGER, row), 1)
        self.assertEqual(len(self.desktop.sends), 1)

    def test_a_pump_killed_before_the_message_was_taken_sends_it_once_later(self):
        relay = self.project.relay(self.desktop)
        relay.hold_before_taking = True
        row = self.project.enqueue("developer", "manager", "report four")
        pump = self.project.pump()
        wait_for(relay.held.is_set, what="the held send")
        pump.kill()
        pump.wait()
        relay.release.set()
        self.assertEqual(self.desktop.sends, [])
        self.project.relay(self.desktop)
        self.project.settle([row])
        self.assertEqual(self.times_in_chat(MANAGER, row), 1)

    def test_the_pump_lock_is_free_after_the_kill_and_a_second_pump_does_not_run_beside_a_live_one(self):
        self.project.relay(self.desktop)
        self.desktop.set(MANAGER, status="active")  # the first pump waits for the busy Manager
        row = self.project.enqueue("developer", "manager", "report five")
        first = self.project.pump()
        wait_for(lambda: self.desktop.status_reads.get(MANAGER, 0) >= 3, what="the first pump polling")
        second = self.project.pump()
        self.assertEqual(second.wait(timeout=20), 0)  # the lock is held: it leaves the queue alone
        self.assertEqual(self.project.state(row), "pending")
        first.kill()
        first.wait()
        self.desktop.set(MANAGER, status="idle")
        self.project.settle([row])


class RelayIdentityChanges(ChaosCase):
    def test_a_pump_that_finds_the_relay_gone_reads_the_new_descriptor_and_delivers_with_the_new_token(self):
        old = self.project.relay(self.desktop)
        old.kill()
        row = self.project.enqueue("developer", "manager", "report six")
        pump = self.project.pump()
        wait_for(lambda: "backing off" in self.project.output(pump), what="the pump noticing the dead relay")
        new = self.project.relay(self.desktop)
        wait_for(lambda: self.project.state(row) == "delivered", what="the delivery through the new relay")
        self.assertEqual(new.tokens_seen, {new.token})
        self.assertEqual(self.times_in_chat(MANAGER, row), 1)

    def test_the_old_token_is_refused_by_the_new_relay_and_nothing_reaches_desktop(self):
        old = self.project.relay(self.desktop)
        old_client = outbox.RelayClient(self.project.root / ".bot-mode" / "desktop-relay")
        old.kill()
        new = self.project.relay(self.desktop, write=False)
        old_client.url, old_client.token = new.url, old.token
        with self.assertRaises(RuntimeError) as caught:
            old_client.call("send_message_to_thread", {"threadId": MANAGER, "prompt": "late report"})
        self.assertIn("403", str(caught.exception))
        self.assertEqual(self.desktop.sends, [])


class DesktopRestarts(ChaosCase):
    def test_nothing_is_sent_while_desktop_is_down_or_the_chat_is_not_loaded_and_the_report_arrives_once_afterwards(self):
        self.project.relay(self.desktop)
        self.desktop.down_reads = 3
        self.desktop.set(MANAGER, status="notLoaded", readable=False)
        row = self.project.enqueue("developer", "manager", "report seven")
        self.project.pump()
        wait_for(lambda: self.desktop.status_reads.get(MANAGER, 0) >= 4, what="the pump asking while Desktop is restarting")
        self.assertEqual(self.desktop.sends, [])
        self.assertEqual(self.project.state(row), "pending")
        self.desktop.set(MANAGER, status="idle", readable=True)  # Desktop is back and has loaded the chat
        wait_for(lambda: self.project.state(row) == "delivered", what="the delivery after the restart")
        self.assertEqual(self.times_in_chat(MANAGER, row), 1)

    def test_a_chat_that_is_not_loaded_but_readable_with_a_finished_turn_is_delivered_to(self):
        self.project.relay(self.desktop)
        self.desktop.set(MANAGER, status="notLoaded", readable=True)
        row = self.project.enqueue("developer", "manager", "report eight")
        self.project.settle([row])
        self.assertEqual(self.times_in_chat(MANAGER, row), 1)


class ManyAgentsAtOnce(ChaosCase):
    def test_three_agents_report_at_the_same_time_and_each_report_arrives_once(self):
        self.project.relay(self.desktop)
        senders = [self.project.send("agent%d" % n, "manager", "report from agent %d" % n, "key-%d" % n) for n in (1, 2, 3)]
        senders.append(self.project.send("agent1", "manager", "report from agent 1", "key-1"))  # the same dedupe key again
        for process in senders:
            self.assertEqual(process.wait(timeout=60), 0, self.project.output(process))
        rows = self.project.rows()
        self.assertEqual(len(rows), 3)
        self.project.settle(list(rows))
        delivered = "\n".join(self.desktop.sent_to(MANAGER))
        for n in (1, 2, 3):
            self.assertEqual(delivered.count("report from agent %d" % n), 1)
        for row_id in rows:
            self.assertEqual(self.times_in_chat(MANAGER, row_id), 1)


class BusyAndUnknownRecipients(ChaosCase):
    def test_a_busy_manager_gets_nothing_until_it_is_idle_and_another_recipient_is_not_held_back(self):
        self.project.relay(self.desktop)
        self.desktop.set(MANAGER, status="active")
        for_manager = self.project.enqueue("developer", "manager", "for the busy Manager")
        for_developer = self.project.enqueue("manager", "developer", "for the free Developer")
        self.project.pump()
        wait_for(lambda: self.project.state(for_developer) == "delivered", what="the Developer report")
        wait_for(lambda: self.desktop.status_reads.get(MANAGER, 0) >= 5, what="the pump asking again")
        self.assertEqual(self.desktop.sent_to(MANAGER), [])
        self.assertEqual(self.project.state(for_manager), "pending")
        self.desktop.set(MANAGER, status="idle")
        wait_for(lambda: self.project.state(for_manager) == "delivered", what="the Manager report")
        self.assertEqual(self.times_in_chat(MANAGER, for_manager), 1)

    def test_an_unconfirmed_row_holds_only_its_own_recipient(self):
        self.project.relay(self.desktop)
        self.desktop.set(MANAGER, readable=False)  # the history cannot be read, so the row cannot be confirmed
        held = self.project.enqueue("developer", "manager", "held report")
        db = self.project._db()
        try:
            db.mark_sending(held, MANAGER)
            db.mark_ambiguous(held, "timeout after the call")
        finally:
            db.close()
        behind = self.project.enqueue("developer", "manager", "behind the held row")
        other = self.project.enqueue("manager", "developer", "for the other recipient")
        self.project.pump()
        wait_for(lambda: self.project.state(other) == "delivered", what="the other recipient's report")
        self.assertEqual(self.project.state(held), "ambiguous")
        self.assertEqual(self.project.state(behind), "pending")
        self.assertEqual(self.desktop.sent_to(MANAGER), [])


class UnreadableRecipient(ChaosCase):
    def test_a_recipient_whose_chat_the_relay_cannot_read_does_not_hold_the_other_recipients(self):
        self.project.relay(self.desktop)
        self.desktop.unknown.add(MANAGER)  # Desktop answers 503 "thread not found" for this chat
        unreadable = self.project.enqueue("developer", "manager", "for the chat that cannot be read")
        other = self.project.enqueue("manager", "developer", "for the readable chat")
        self.project.pump()
        wait_for(lambda: self.project.state(other) == "delivered", what="the report for the readable chat")
        self.assertEqual(self.project.state(unreadable), "pending")  # kept, not sent, not failed
        self.assertEqual(self.desktop.sent_to(MANAGER), [])
        self.desktop.unknown.discard(MANAGER)  # the chat is known again
        wait_for(lambda: self.project.state(unreadable) == "delivered", what="the report once the chat can be read")
        self.assertEqual(self.times_in_chat(MANAGER, unreadable), 1)

    def test_a_stale_token_still_sends_the_pump_back_to_the_descriptor(self):
        relay = self.project.relay(self.desktop, write=False)
        stale = self.project.root / ".bot-mode" / "desktop-relay"
        stale.mkdir(parents=True)
        (stale / "relay.json").write_text('{"url": "%s", "token": "stale"}' % relay.url, encoding="utf-8")
        row = self.project.enqueue("developer", "manager", "report with a stale token")
        pump = self.project.pump()
        wait_for(lambda: "backing off" in self.project.output(pump), what="the pump refused by the relay")
        self.assertEqual(self.desktop.sends, [])
        relay.write_descriptor(self.project.root)
        wait_for(lambda: self.project.state(row) == "delivered", what="the delivery with the current token")
        self.assertEqual(self.times_in_chat(MANAGER, row), 1)


STEER = "Cannot steer conversation %s without an active turn id"
UNCONFIRMED = "Desktop request outcome unconfirmed; do not resend (no answer within 35s; bridge process alive)"


class RefusedSteer(ChaosCase):
    """Desktop refuses a send with 'Cannot steer conversation ... without an active turn id' (#426): the chat is not ready, so the
    row stays pending and only that recipient waits. Any other 503 stays unconfirmed."""

    def states_until(self, row, wanted="delivered"):
        seen = set()

        def done():
            seen.add(self.project.state(row))
            return wanted in seen
        wait_for(done, what="the report to be " + wanted)
        return seen

    def test_a_refused_report_stays_pending_is_never_ambiguous_and_arrives_once_when_the_chat_is_ready(self):
        self.project.relay(self.desktop)
        self.desktop.refuse_next(MANAGER, 2, STEER % MANAGER)
        row = self.project.enqueue("developer", "manager", "report for a chat that is not ready")
        other = self.project.enqueue("manager", "developer", "for the other recipient")
        pump = self.project.pump()
        seen = self.states_until(row)
        self.assertEqual(seen - {"pending", "sending", "delivered"}, set())  # never ambiguous, failed or needs-attention
        self.assertEqual(self.project.state(other), "delivered")  # nothing else was held
        self.assertEqual(self.times_in_chat(MANAGER, row), 1)
        self.assertEqual(len(self.desktop.sent_to(MANAGER)), 1)
        self.assertEqual(self.desktop.refusals[MANAGER][0], 0)  # both refusals were really met
        log = self.project.output(pump)
        self.assertEqual(log.count("no active turn id to steer"), 2)
        self.assertNotIn("unconfirmed; later reports", log)

    def test_a_whole_digest_that_is_refused_goes_back_to_pending_and_arrives_as_one_digest_once(self):
        self.project.relay(self.desktop)
        self.desktop.refuse_next(MANAGER, 1, STEER % MANAGER)
        rows = [self.project.enqueue("agent%d" % n, "manager", "digest report %d" % n) for n in (1, 2, 3)]
        self.project.pump()
        wait_for(lambda: all(self.project.state(r) == "delivered" for r in rows), what="the digest")
        for row in rows:
            self.assertEqual(self.times_in_chat(MANAGER, row), 1)
        self.assertEqual(len(self.desktop.sent_to(MANAGER)), 1)
        self.assertEqual([self.project.rows()[r]["error"] for r in rows], [None, None, None])

    def test_the_refusal_is_set_aside_with_a_pause_and_not_hammered(self):
        self.project.relay(self.desktop)
        self.desktop.refuse_next(MANAGER, 10 ** 6, STEER % MANAGER)
        row = self.project.enqueue("developer", "manager", "report for a chat that stays refused")
        self.project.pump(max_wait_sec=6, poll_interval=0.2)
        wait_for(lambda: self.desktop.refusals[MANAGER][0] < 10 ** 6 - 2, what="a few refusals")
        time.sleep(4)
        self.assertLess(10 ** 6 - self.desktop.refusals[MANAGER][0], 8)  # growing pauses: far fewer tries than polls
        self.assertEqual(self.project.state(row), "pending")
        self.assertEqual(self.desktop.sent_to(MANAGER), [])

    def test_any_other_503_stays_unconfirmed_holds_its_recipient_and_is_not_sent_again(self):
        self.assert_stays_unconfirmed(UNCONFIRMED)

    def test_an_unconfirmed_outcome_whose_stderr_tail_quotes_the_refusal_stays_unconfirmed(self):
        self.assert_stays_unconfirmed(UNCONFIRMED[:-1] + "; stderr: " + STEER % "x" + ")")

    def assert_stays_unconfirmed(self, message):
        self.project.relay(self.desktop)
        self.desktop.refuse_next(MANAGER, 1, message)
        first = self.project.enqueue("developer", "manager", "report with an unconfirmed outcome")
        self.project.pump(retry_grace_minutes=0)  # never retried by proof of absence: only the operator decides
        wait_for(lambda: self.project.state(first) == "ambiguous", what="the unconfirmed row")
        behind = self.project.enqueue("developer", "manager", "report behind it")
        other = self.project.enqueue("manager", "developer", "for the other recipient")
        wait_for(lambda: self.project.state(other) == "delivered", what="the other recipient")
        time.sleep(1.5)
        self.assertEqual(self.project.state(first), "ambiguous")
        self.assertEqual(self.project.state(behind), "pending")
        self.assertEqual(self.desktop.sent_to(MANAGER), [])

    def test_the_same_text_with_another_status_or_a_timeout_is_not_taken_for_the_refusal(self):
        text = STEER % "x"
        self.assertTrue(outbox.refused_for_missing_turn(RuntimeError("HTTP Error 503: Service Unavailable (" + text + ")")))
        for error in (RuntimeError("HTTP Error 502: Bad Gateway (" + text + ")"), RuntimeError("HTTP Error 403: Forbidden (" + text + ")"),
                      RuntimeError("HTTP Error 503: Service Unavailable (" + UNCONFIRMED + ")"),
                      RuntimeError("HTTP Error 503: Service Unavailable (" + UNCONFIRMED[:-1] + "; stderr: " + text + "))"),
                      RuntimeError("HTTP Error 503: Service Unavailable (" + text + "; stderr: Desktop request outcome unconfirmed; do not resend)"),
                      RuntimeError("HTTP Error 503: Service Unavailable (" + text + ") and more"),
                      RuntimeError("note: HTTP Error 503: Service Unavailable (" + text + ")"),
                      RuntimeError("HTTP Error 503: Service Unavailable (Cannot steer conversation x)"),
                      RuntimeError("HTTP Error 503: Service Unavailable (without an active turn id)"),
                      OSError("HTTP Error 503: " + text), TimeoutError("timed out"), ConnectionError("reset")):
            self.assertFalse(outbox.refused_for_missing_turn(error), repr(error))


if __name__ == "__main__":
    unittest.main()
