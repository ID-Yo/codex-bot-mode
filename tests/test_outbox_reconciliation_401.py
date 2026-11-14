import tempfile
import unittest
from pathlib import Path

from scripts.team_room import outbox


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = outbox.OutboxDB(Path(self.tmp.name) / 'queue.sqlite')
        self.addCleanup(self.db.close)
        self.id = self.db.enqueue('agent', 'manager', None, 'result')
        self.db.mark_sending(self.id, 'target-thread')
        self.db.mark_ambiguous(self.id, 'timeout')

    def test_exact_marker_in_user_message_proves_delivery(self):
        marker = outbox.delivery_marker(self.id)

        class Client:
            def call(self, name, args):
                return {'turns': [{'items': [{'type': 'userMessage', 'content': [
                    {'type': 'text', 'text': marker + '\nresult'}]}]}], 'page': {}}

        outbox.reconcile_ambiguous(self.db, Client())
        self.assertEqual(self.db.count_state('delivered'), 1)

    def test_missing_or_echoed_marker_is_not_delivery(self):
        marker = outbox.delivery_marker(self.id)

        class Client:
            def call(self, name, args):
                return {'turns': [{'items': [{'type': 'agentMessage', 'text': marker}]}], 'page': {}}

        outbox.reconcile_ambiguous(self.db, Client())
        self.assertEqual(self.db.count_state('ambiguous'), 1)
        next_id = self.db.enqueue('agent', 'manager', None, 'later')
        self.assertEqual(self.db.next_pending()['id'], next_id)


if __name__ == '__main__':
    unittest.main()
