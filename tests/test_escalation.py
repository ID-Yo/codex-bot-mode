from datetime import timedelta
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import escalation, project, requests as rq, roster, store, teams  # noqa: E402

T = ['0aa00000-0000-7000-8000-00000000001' + str(i) for i in range(4)]


class Bridge:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def call(self, name, arguments):
        if self.fail:
            raise RuntimeError('unconfirmed')
        self.calls.append((name, arguments))
        return {}


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'p'
        self.root.mkdir()
        teams.install(teams.plan_team(self.root, 'development', team_answers={'tracking_mode': 'local'}))
        data = roster.load(self.root)
        for bot, tid in zip(('manager', 'developer', 'verifier'), T):
            roster.set_thread(data, bot, tid, 'appointed')
        roster.save(self.root, data, roster.version(roster.load(self.root)))
        self.saved = list(store.BOTS)
        project.configure(self.root)

    def tearDown(self):
        project.configure(ROOT)
        store.BOTS[:] = self.saved
        self.tmp.cleanup()

    def test_overdue_reply_reminds_then_escalates(self):
        t0 = rq.now()
        data = rq.load(self.root)
        rq.open_request(data, 'REQ-7', 'manager', 'developer', 'Add usage section', None, 10, t0)
        rq.open_request(data, 'REQ-8', 'developer', 'verifier', 'Check it', None, 10, t0)
        rq.save(self.root, data, rq.version(rq.load(self.root)))
        bridge = Bridge()
        self.assertEqual(escalation.escalate(bridge, t0 + timedelta(minutes=5)), [])
        done = escalation.escalate(bridge, t0 + timedelta(minutes=11))
        self.assertEqual({(d[0], d[2], d[3]) for d in done}, {('REQ-7', 'developer', 'sent'), ('REQ-8', 'verifier', 'sent')})
        self.assertEqual(bridge.calls[0][1]['threadId'], T[1])
        self.assertIn('does not reach them', bridge.calls[0][1]['prompt'])
        done = escalation.escalate(bridge, t0 + timedelta(minutes=21))
        self.assertEqual({(d[0], d[2]) for d in done}, {('REQ-7', 'owner'), ('REQ-8', 'manager')})
        done = escalation.escalate(Bridge(), t0 + timedelta(minutes=31))
        self.assertEqual({d[3] for d in done}, {'flagged'})
        data = rq.load(self.root)
        rq.open_request(data, 'REQ-9', 'manager', 'verifier', 'Check again', None, 1, t0)
        rq.save(self.root, data, rq.version(rq.load(self.root)))
        done = escalation.escalate(Bridge(fail=True), t0 + timedelta(minutes=32))
        self.assertEqual(done, [('REQ-9', 'reminder', 'verifier', 'uncertain')])


if __name__ == '__main__':
    unittest.main()
