from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import component_activation as a


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 18, tzinfo=timezone.utc)
        self.record = a.pending('editorial', 'a' * 64, [{'bot': 'writer', 'thread_id': 'current'}, {'bot': 'reviewer', 'thread_id': 'review'}])
        self.activity = {'bot': 'writer', 'thread_id': 'current', 'as_of': self.now.isoformat(),
                         'state': 'idle', 'paused': False, 'pending_writes': False, 'ownership': 'native_desktop'}
        self.receipt = {'bot': 'writer', 'thread_id': 'current', 'turn_id': 'completed-turn',
                        'component': 'editorial', 'revision': 'a' * 64, 'completed_at': self.now.isoformat(), 'host_verified': True}

    def test_busy_paused_stale_and_unknown_never_authorize(self):
        for change in [{'state': 'working'}, {'paused': True}, {'paused': None}, {'pending_writes': True},
                       {'ownership': 'unknown'}, {'as_of': '2020-01-01T00:00:00Z'}, {'thread_id': 'old'}]:
            with self.subTest(change=change):
                self.assertFalse(a.eligibility({**self.activity, **change}, 'writer', 'current', self.now)['eligible'])
        safe = a.eligibility(self.activity, 'writer', 'current', self.now)
        self.assertTrue(safe['eligible'])
        self.assertFalse(safe['dispatch_authorized'])

    def test_no_copy_or_single_consumer_implies_all_loaded(self):
        self.assertEqual(self.record['state'], 'pending_activation')
        result = a.acknowledge(self.record, self.receipt, 'a' * 64, {'writer': 'current'}, self.activity, self.now)
        self.assertEqual(result['state'], 'partial_activation')
        self.assertEqual(self.record['state'], 'pending_activation')
        self.assertEqual(result, a.acknowledge(result, self.receipt, 'a' * 64, {'writer': 'current'}, self.activity, self.now))

    def test_stale_receipt_or_binding_cannot_activate(self):
        for change in [{'host_verified': False}, {'revision': 'b' * 64}, {'thread_id': 'old'},
                       {'turn_id': ''}, {'completed_at': '2020-01-01T00:00:00Z'}]:
            with self.subTest(change=change), self.assertRaises(a.ActivationError):
                a.acknowledge(self.record, {**self.receipt, **change}, 'a' * 64, {'writer': 'current'}, self.activity, self.now)
        with self.assertRaises(a.ActivationError):
            a.acknowledge(self.record, self.receipt, 'b' * 64, {'writer': 'current'}, self.activity, self.now)

    def test_replaced_verified_peer_invalidates_aggregate(self):
        bindings = {'writer': 'current', 'reviewer': 'review'}
        partial = a.acknowledge(self.record, self.receipt, 'a' * 64, bindings, self.activity, self.now)
        receipt = {**self.receipt, 'bot': 'reviewer', 'thread_id': 'review', 'turn_id': 'review-turn'}
        activity = {**self.activity, 'bot': 'reviewer', 'thread_id': 'review'}
        result = a.acknowledge(partial, receipt, 'a' * 64, {**bindings, 'writer': 'replacement'}, activity, self.now)
        self.assertEqual(result['state'], 'partial_activation')
        self.assertEqual(result['consumers'][0]['thread_id'], 'replacement')
        self.assertIsNone(result['consumers'][0]['receipt'])
        self.assertEqual(a.reconcile(partial, {'writer': 'replacement', 'reviewer': 'review'})['state'], 'pending_activation')

    def test_identical_receipt_is_idempotent_after_freshness_window(self):
        bindings = {'writer': 'current', 'reviewer': 'review'}
        result = a.acknowledge(self.record, self.receipt, 'a' * 64, bindings, self.activity, self.now)
        later = self.now + timedelta(seconds=121)
        self.assertEqual(result, a.acknowledge(result, self.receipt, 'a' * 64, bindings,
                                              {**self.activity, 'as_of': later.isoformat()}, later))

    def test_empty_or_malformed_identity_is_refused(self):
        for value in (None, '', [], {}):
            self.assertFalse(a.eligibility({**self.activity, 'bot': value, 'thread_id': value}, value, value, self.now)['eligible'])
            with self.assertRaises(a.ActivationError):
                a.acknowledge(self.record, {**self.receipt, 'bot': value}, 'a' * 64,
                              {'writer': 'current'}, self.activity, self.now)

    def test_corrupt_internal_record_is_a_controlled_error(self):
        missing = deepcopy(self.record)
        del missing['component']
        invalid = deepcopy(self.record)
        invalid['consumers'][0]['state'] = []
        for record in (missing, invalid):
            with self.assertRaises(a.ActivationError):
                a.acknowledge(record, self.receipt, 'a' * 64, {'writer': 'current'}, self.activity, self.now)


if __name__ == '__main__':
    unittest.main()
