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

    def test_open_request_stores_candidate_and_author(self):
        data = {'schema': 1, 'requests': []}
        sha = '92625269294c9f0185073dc7c4818c8cb2c9992c'
        data = rq.open_request(data, 'REQ-CAND', 'manager', 'code-reviewer', 'Review portal',
                               candidate=sha, author='gw-web-portal')
        req = data['requests'][0]
        self.assertEqual(req['candidate'], sha)
        self.assertEqual(req['author'], 'gw-web-portal')

    def test_open_request_validates_candidate_sha_and_author(self):
        data = {'schema': 1, 'requests': []}
        with self.assertRaises(rq.RequestError):
            rq.open_request(data, 'REQ-BAD-SHA', 'manager', 'code-reviewer', 'Review', candidate='not-a-sha')
        with self.assertRaises(rq.RequestError):
            rq.open_request(data, 'REQ-BAD-AUTH', 'manager', 'code-reviewer', 'Review', author='Invalid Author!')

    def test_open_request_cli_supports_candidate_and_author(self):
        with tempfile.TemporaryDirectory() as project:
            sha = '92625269294c9f0185073dc7c4818c8cb2c9992c'
            code = rq.main(['--project', project, 'open', 'REQ-CLI', '--from', 'manager', '--to', 'code-reviewer',
                            '--summary', 'Review portal', '--candidate', sha, '--author', 'gw-web-portal'])
            self.assertEqual(code, 0)
            req = rq.load(project)['requests'][0]
            self.assertEqual(req['candidate'], sha)
            self.assertEqual(req['author'], 'gw-web-portal')


if __name__ == '__main__':
    unittest.main()
