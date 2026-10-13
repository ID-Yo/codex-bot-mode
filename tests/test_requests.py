from datetime import timedelta
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from team_room import requests as rq  # noqa: E402


class RequestTests(unittest.TestCase):
    def test_reply_only_in_own_chat_escalates_until_reply(self):
        t0 = rq.now()
        data = {'schema': 1, 'requests': []}
        rq.open_request(data, 'REQ-1', 'manager', 'developer', 'Add usage section', 'https://x/1', 30, t0)
        self.assertEqual(rq.due_steps(data, t0 + timedelta(minutes=29)), [])
        self.assertEqual(rq.summary(data, t0)['developer'], {'open': 1, 'overdue': 0})
        steps = rq.due_steps(data, t0 + timedelta(minutes=31))
        self.assertEqual([s for _, s in steps], ['reminder'])
        rq.record_escalation(data, 'REQ-1', 'reminder', 'developer', 'sent', t0 + timedelta(minutes=31))
        self.assertEqual(rq.due_steps(data, t0 + timedelta(minutes=50)), [])
        self.assertEqual([s for _, s in rq.due_steps(data, t0 + timedelta(minutes=61))], ['manager'])
        rq.record_escalation(data, 'REQ-1', 'manager', 'manager', 'sent')
        self.assertEqual([s for _, s in rq.due_steps(data, t0 + timedelta(minutes=91))], ['owner'])
        with self.assertRaises(rq.RequestError):
            rq.reply(data, 'REQ-1', 'verifier')
        rq.reply(data, 'REQ-1', 'developer', 'https://x/2', 'partial')
        self.assertEqual(rq.due_steps(data, t0 + timedelta(days=1)), [])
        self.assertEqual(rq.summary(data), {})

    def test_cli_and_validation(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertEqual(rq.main(['--project', project, 'open', 'REQ-2', '--from', 'manager', '--to', 'developer',
                                      '--summary', 'Check']), 0)
            self.assertEqual(rq.main(['--project', project, 'open', 'REQ-2', '--from', 'manager', '--to', 'developer',
                                      '--summary', 'Again']), 2)
            self.assertEqual(rq.main(['--project', project, 'open', 'bad id', '--from', 'manager', '--to', 'x',
                                      '--summary', 'x']), 2)
            self.assertEqual(rq.main(['--project', project, 'reply', 'REQ-2', '--by', 'developer']), 0)
            self.assertEqual(rq.load(project)['requests'][0]['state'], 'answered')


if __name__ == '__main__':
    unittest.main()
