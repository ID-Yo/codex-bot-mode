
import json, os, sqlite3, sys, tempfile, time, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.team_room import outbox


def old(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class RelayReliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / ".bot-mode" / "outbox"
        self.db = outbox.OutboxDB(self.dir / "queue.sqlite")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_stale_pending_expires_and_fresh_stays(self):
        a = self.db.enqueue("agent", "manager", None, "old")
        self.db.enqueue("agent", "manager", None, "new")
        with self.db.conn:
            self.db.conn.execute("UPDATE outbox SET created_at = ? WHERE id = ?", (old(13), a))
        self.assertEqual(self.db.expire_stale(12), 1)
        self.assertEqual(self.db.count_state("expired"), 1)
        self.assertEqual(self.db.pending_count(), 1)

    def test_status_flags_waiting_work_without_relay_or_pump(self):
        self.db.enqueue("agent", "manager", None, "x")
        with mock.patch.object(outbox, "relay_reachable", return_value=False):
            s = outbox.queue_status(self.root)
        self.assertEqual(s["pending"], 1)
        self.assertTrue(any("relay is not reachable" in p for p in s["problems"]))
        self.assertTrue(any("no pump" in p for p in s["problems"]))

    def test_status_ok_when_empty(self):
        with mock.patch.object(outbox, "relay_reachable", return_value=False):
            self.assertEqual(outbox.queue_status(self.root)["problems"], [])

    def test_pump_waits_for_relay_then_delivers_after_restart(self):
        mid = self.db.enqueue("agent", "00000000-0000-0000-0000-000000000001", None, "report")
        calls = {"n": 0}

        class Client:
            def call(self, name, args, timeout=40):
                if name == "read_thread":
                    return {"thread": {"status": {"type": "idle"}}, "turns": [{"id": "t%d" % calls["n"], "status": "completed"}]}
                calls["n"] += 1  # send_message_to_thread
                return {}

        descriptors = iter([(None, None), (None, None), (self.root, {"url": "http://127.0.0.1:1", "token": "x"})] + [(self.root, {})] * 50)
        with mock.patch.object(outbox, "find_relay_descriptor", side_effect=lambda p: next(descriptors)), \
             mock.patch.object(outbox, "RelayClient", return_value=Client()), \
             mock.patch.object(outbox, "await_turn", return_value=True), \
             mock.patch.object(outbox.time, "sleep"):
            rc = outbox.pump_outbox(self.root, max_wait_sec=30, poll_interval=0.01)
        self.assertEqual(rc, 0)
        self.assertEqual(self.db.conn.execute("SELECT state FROM outbox WHERE id=?", (mid,)).fetchone()[0], "delivered")
        self.assertTrue((self.dir / "pump.heartbeat.json").is_file())

    def test_pump_rereads_descriptor_after_relay_failure(self):
        self.db.enqueue("agent", "00000000-0000-0000-0000-000000000001", None, "report")
        built = []

        class Dead:
            def call(self, *a, **k):
                raise OSError("connection refused")

        class Live:
            def call(self, name, args, timeout=40):
                if name == "read_thread":
                    return {"thread": {"status": {"type": "idle"}}, "turns": []}
                return {}

        clients = iter([Dead(), Live()])
        def make(_root):
            c = next(clients)
            built.append(c)
            return c
        with mock.patch.object(outbox, "find_relay_descriptor", return_value=(self.root, {})), \
             mock.patch.object(outbox, "RelayClient", side_effect=make), \
             mock.patch.object(outbox, "await_turn", return_value=True), \
             mock.patch.object(outbox.time, "sleep"):
            outbox.pump_outbox(self.root, max_wait_sec=30, poll_interval=0.01)
        self.assertEqual(len(built), 2)  # a new client (new port/token) was built after the failure
        self.assertEqual(self.db.pending_count(), 0)

    def test_status_cli_exit_codes(self):
        self.db.enqueue("agent", "manager", None, "x")
        with mock.patch.object(outbox, "relay_reachable", return_value=False):
            self.assertEqual(outbox.main(["--project", str(self.root), "status"]), outbox.EXIT_UNHEALTHY)


class OfficeCheck(unittest.TestCase):
    def test_office_down_is_reported_false(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import relay_watchdog
        self.assertFalse(relay_watchdog.office_ok("http://127.0.0.1:1", timeout=1))


if __name__ == "__main__":
    unittest.main()


