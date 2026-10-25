from pathlib import Path
import tempfile
import unittest

from scripts.team_room.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(lambda: self.store.close())

    def test_unique_deliveries_and_atomic_rollback(self):
        with self.store.transaction() as db:
            first = self.store.enqueue(db, 'Topic', 'developer', 'browser-1')
        self.assertEqual(self.store.client_delivery('browser-1')[0]['id'], first['delivery_id'])
        with self.assertRaises(Exception), self.store.transaction() as db:
            self.store.enqueue(db, 'Duplicate', 'developer', 'browser-1')
        self.assertEqual(len(self.store.journal()), 1)
        self.assertEqual(len(self.store.deliveries()), 1)

    def test_terminal_duplicate_cannot_publish_twice(self):
        with self.store.transaction() as db:
            d = self.store.enqueue(db, 'Topic', 'developer', 'one')
            self.store.finish(db, d['delivery_id'], 'completed', 'Answer', 'turn')
            self.assertFalse(self.store.finish(db, d['delivery_id'], 'completed', 'Answer', 'turn'))
        self.assertEqual([m['text'] for m in self.store.journal()], ['Topic', 'Answer'])
        self.assertEqual(len([e for e in self.store.events(0) if e['kind'] == 'terminal']), 1)

    def test_three_recipient_round_rolls_back_if_last_insert_fails(self):
        self.store.db.execute("CREATE TEMP TRIGGER fail_verifier BEFORE INSERT ON deliveries WHEN NEW.bot='verifier' BEGIN SELECT RAISE(ABORT,'failed reservation'); END")
        with self.assertRaises(Exception), self.store.transaction() as db:
            self.store.enqueue(db,'Topic','manager','round',round_mode=True)
        self.assertEqual(self.store.journal(),[])
        self.assertEqual(self.store.deliveries(),[])
        self.assertEqual(self.store.events(0),[])

    def test_voluntary_abstention_records_terminal_decision_without_journal_reply(self):
        with self.store.transaction() as db:
            db.execute("INSERT INTO participants(bot,thread_id,cwd) VALUES('manager','owned','workspace')")
            d = self.store.enqueue(db,'General','manager','general',voluntary=True)
            db.execute('UPDATE deliveries SET watermark=7 WHERE id=?',(d['delivery_id'],))
            self.store.finish(db,d['delivery_id'],'completed','{"decision":"abstain","text":""}','turn')
        self.assertEqual(self.store.deliveries()[0]['state'],'completed')
        self.assertEqual(self.store.deliveries()[0]['decision'],'abstain')
        self.assertEqual(self.store.participants()[0]['watermark'],7)
        self.assertEqual([m['text'] for m in self.store.journal()],['General'])
        self.assertEqual(self.store.events(0)[-1]['payload']['decision'],'abstain')

    def test_voluntary_contract_rejects_malformed_empty_reply_and_nonempty_abstention(self):
        invalid = ('','not json','{}','{"decision":"reply","text":""}',
                   '{"decision":"abstain","text":"a fake reply"}',
                   '{"decision":"reply","text":"answer","extra":true}')
        for i,text in enumerate(invalid):
            with self.subTest(text=text),self.store.transaction() as db:
                d = self.store.enqueue(db,'General','manager',str(i),voluntary=True)
                self.store.finish(db,d['delivery_id'],'completed',text,'turn')
                row = db.execute('SELECT * FROM deliveries WHERE id=?',(d['delivery_id'],)).fetchone()
                self.assertEqual(row['state'],'failed');self.assertIsNone(row['decision'])
        self.assertTrue(all(m['sender']=='owner' for m in self.store.journal()))

    def test_voluntary_three_recipient_reservation_rolls_back_on_last_insert_failure(self):
        self.store.db.execute("CREATE TEMP TRIGGER fail_verifier BEFORE INSERT ON deliveries WHEN NEW.bot='verifier' BEGIN SELECT RAISE(ABORT,'failed reservation'); END")
        with self.assertRaises(Exception),self.store.transaction() as db:
            self.store.enqueue(db,'General','manager','general',voluntary=True)
        self.assertEqual(self.store.journal(),[]);self.assertEqual(self.store.deliveries(),[])

    def test_additive_decision_columns_preserve_legacy_delivery_and_journal(self):
        with self.store.transaction() as db:
            self.store.enqueue(db,'Legacy','developer','legacy')
            db.execute('ALTER TABLE deliveries DROP COLUMN mode')
            db.execute('ALTER TABLE deliveries DROP COLUMN decision')
        journal = self.store.journal()
        self.store.close();self.store = Store(self.temp.name)
        self.assertEqual(self.store.journal(),journal)
        self.assertEqual(self.store.deliveries()[0]['mode'],'direct')
        self.assertIsNone(self.store.deliveries()[0]['decision'])

    def test_reopen_preserves_hold_limits_bindings_and_pending(self):
        with self.store.transaction() as db:
            db.execute("UPDATE room SET hold=1,turns=3,deadline=12345,topic='topic'")
            db.execute('INSERT INTO participants(bot,thread_id,cwd,watermark) VALUES(?,?,?,?)',
                       ('developer', 'owned', 'room-only', 12))
            self.store.enqueue(db, 'Pending', 'developer', 'pending')
        self.store.close()
        self.store = Store(self.temp.name)
        self.assertEqual(self.store.room()['deadline'], 12345)
        self.assertEqual(self.store.room()['turns'], 3)
        self.assertEqual(self.store.room()['hold'], 1)
        self.assertEqual(self.store.participants()[0]['watermark'], 12)
        self.assertEqual(self.store.deliveries()[0]['state'], 'pending')

    def test_second_controller_lock_and_release(self):
        with self.assertRaisesRegex(RuntimeError, 'Another controller'):
            Store(self.temp.name)

    def test_public_event_redaction_and_monotonic_cursor(self):
        with self.store.transaction() as db:
            self.store.emit(db, 'status', {'text': 'Bearer SECRET', 'reasoning': 'hidden', 'auth': 'private'})
            self.store.emit(db, 'status', {'status': 'held'})
        first = self.store.events(0)
        self.assertEqual(first[0]['payload'], {'text': 'Bearer [REDACTED]'})
        self.assertEqual(len(self.store.events(first[0]['seq'])), 1)
        self.assertLess(first[0]['seq'], first[1]['seq'])


if __name__ == '__main__':
    unittest.main()
