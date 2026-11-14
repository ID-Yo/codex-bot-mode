import tempfile
import unittest
from pathlib import Path

from scripts.team_room.native import NativePersonal, project_history
from scripts.team_room.work import Uncertain


class Bridge:
    def __init__(self):
        self.busy=False
        self.calls=[]
        self.turns=[]
        self.fail=False
        self.updated=None

    def call(self,name,args):
        self.calls.append((name,args))
        if name=='read_thread':
            thread={'id':args['threadId'],'status':{'type':'active' if self.busy else 'idle'}}
            if self.updated is not None: thread['updatedAt']=self.updated
            return {'thread':thread,
                    'turns':self.turns,'page':{'hasMore':False}}
        if name=='send_message_to_thread':
            if self.fail: raise Uncertain('unconfirmed')
            self.turns.insert(0,{'id':'accepted-turn','status':'inProgress','items':[
                {'id':'owner-item','type':'userMessage','content':[{'type':'text','text':args['prompt']}]}]})
            return {'threadId':args['threadId']}
        return {'opened':True}


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge=Bridge()
        self.c=NativePersonal(Path(self.temp.name),'manager','native-manager',self.bridge,start_worker=False)
        self.addCleanup(self.c.close)

    def test_projection_keeps_only_public_user_and_assistant_text(self):
        data={'turns':[{'id':'turn','startedAt':10,'items':[
            {'id':'u','type':'userMessage','content':[{'type':'text','text':'hello'}]},
            {'id':'r','type':'reasoning','text':'REASONING_PRIVATE'},
            {'id':'cmd','type':'commandExecution','text':'TOOL_OUTPUT'},
            {'id':'a','type':'agentMessage','text':'reply','phase':'final_answer'}]}]}
        rows=project_history(data,'manager')
        self.assertEqual([r['text'] for r in rows],['hello','reply'])
        self.assertEqual([r['sender'] for r in rows],['owner','manager'])

    def test_busy_queue_does_not_send_or_steer_and_duplicate_is_stable(self):
        self.bridge.busy=True
        self.c.send('queued','manager','id1')
        self.c.pump()
        self.assertFalse(any(n=='send_message_to_thread' for n,_ in self.bridge.calls))
        self.assertTrue(self.c.send('queued','manager','id1')['duplicate'])
        self.bridge.busy=False
        self.c.pump()
        self.assertEqual(len([n for n,_ in self.bridge.calls if n=='send_message_to_thread']),1)
        self.c.pump()
        self.assertEqual(self.c.state()['deliveries'][0]['turn_id'],'accepted-turn')

    def test_unknown_result_survives_restart_without_replay(self):
        self.bridge.fail=True
        self.c.send('unknown','manager','id1')
        self.c.pump()
        self.assertEqual(self.c.state()['deliveries'][0]['state'],'ambiguous')
        self.c.close()
        self.c=NativePersonal(Path(self.temp.name),'manager','native-manager',self.bridge,start_worker=False)
        self.addCleanup(self.c.close)
        self.c.pump()
        self.assertEqual(len([n for n,_ in self.bridge.calls if n=='send_message_to_thread']),1)

    def test_same_id_different_text_and_wrong_bot_are_rejected(self):
        self.c.send('hello','manager','id1')
        with self.assertRaises(ValueError): self.c.send('changed','manager','id1')
        with self.assertRaises(ValueError): self.c.send('hello','developer','id2')

    def test_desktop_messages_project_into_private_state(self):
        self.bridge.turns=[{'id':'native-turn','status':'completed','items':[
            {'id':'u','type':'userMessage','content':[{'type':'text','text':'FROM_DESKTOP'}]},
            {'id':'a','type':'agentMessage','text':'native reply','phase':'final_answer'}]}]
        state=self.c.state()
        self.assertEqual(state['personal_thread_id'],'native-manager')
        self.assertEqual(state['journal'][0]['text'],'FROM_DESKTOP')
        self.assertEqual(state['backend'],'desktop')

    def test_native_relay_projects_only_input_and_reconciles_accepted_delivery(self):
        result=self.c.send('sent via Office','manager','relay1')
        row=self.c.db.execute('SELECT * FROM outbox').fetchone()
        prompt=self.c.prompt(row)
        self.bridge.turns=[{'id':'relay-turn','status':'completed','items':[
            {'id':'f','type':'functionCallOutput','name':'send_message_to_thread',
             'namespace':'codex_app','output':{'text':'<codex_delegation>\n  <source_thread_id>source</source_thread_id>\n  <input>'+prompt+'</input>\n</codex_delegation>','truncated':False}},
            {'id':'a','type':'agentMessage','text':'done','phase':'final_answer'}]}]
        self.c.db.execute("UPDATE outbox SET state='accepted' WHERE id=?",(result['delivery_id'],))
        self.c.db.commit()
        state=self.c.state()
        self.assertEqual(state['journal'][0]['text'],'sent via Office')
        self.assertEqual(state['deliveries'][0]['state'],'completed')
        self.assertEqual(state['deliveries'][0]['turn_id'],'relay-turn')

    def test_pending_restart_requires_explicit_resume_and_does_not_start_work(self):
        self.c.send('wait for me','manager','pending')
        self.c.close()
        self.c=NativePersonal(Path(self.temp.name),'manager','native-manager',self.bridge,start_worker=False)
        self.addCleanup(self.c.close)
        self.c.pump()
        self.assertFalse(any(n=='send_message_to_thread' for n,_ in self.bridge.calls))
        self.assertTrue(self.c.state()['hold'])
        self.c.resume()
        self.c.pump()
        self.assertEqual(len([n for n,_ in self.bridge.calls if n=='send_message_to_thread']),1)

    def test_unfinished_native_turn_blocks_send_even_with_idle_metadata(self):
        self.bridge.turns=[{'id':'unfinished','status':'inProgress','items':[]}]
        self.c.send('later','manager','later')
        self.c.pump()
        self.assertFalse(any(n=='send_message_to_thread' for n,_ in self.bridge.calls))


class HistoryFreshnessTests(unittest.TestCase):
    """Desktop's history projection can stop at an old turn while thread.updatedAt keeps moving (#352)."""

    def freshness(self, updated, turns):
        from scripts.team_room.native import history_freshness
        return history_freshness({'thread': {'updatedAt': updated}, 'turns': turns})

    def test_updated_far_after_the_newest_turn_is_reported_as_behind_with_both_times(self):
        got = self.freshness(1793194347, [{'id': 'a', 'startedAt': 1793125325, 'completedAt': 1793125330}])
        self.assertTrue(got['behind'])
        self.assertEqual((got['latest_turn_at'], got['thread_updated_at'], got['lag_seconds']), (1793125330, 1793194347, 69017))

    def test_a_current_or_slightly_newer_history_is_not_behind(self):
        self.assertFalse(self.freshness(1000, [{'id': 'a', 'startedAt': 990, 'completedAt': 995}])['behind'])
        self.assertFalse(self.freshness(1500, [{'id': 'a', 'startedAt': 990, 'completedAt': 1000}])['behind'])

    def test_missing_or_invalid_timestamps_never_claim_behind(self):
        self.assertFalse(self.freshness(None, [{'id': 'a', 'startedAt': 1}])['behind'])
        self.assertFalse(self.freshness(5000, [])['behind'])
        self.assertFalse(self.freshness('later', [{'id': 'a', 'startedAt': 1}])['behind'])
        self.assertFalse(self.freshness(True, [{'id': 'a', 'startedAt': 1}])['behind'])

    def test_an_in_progress_turn_uses_its_start_time(self):
        self.assertFalse(self.freshness(2000, [{'id': 'a', 'startedAt': 1900, 'completedAt': None}])['behind'])

    def test_state_carries_the_history_freshness_without_changing_the_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = Bridge()
            bridge.updated = 1793194347
            bridge.turns = [{'id': 'old', 'status': 'completed', 'startedAt': 1793125325, 'completedAt': 1793125330, 'items': [
                {'id': 'm', 'type': 'agentMessage', 'text': 'old reply', 'phase': 'final_answer'}]}]
            session = NativePersonal(Path(temp), 'manager', 'native-manager', bridge, start_worker=False)
            try:
                state = session.state()
            finally:
                session.close()
        self.assertTrue(state['history']['behind'])
        self.assertEqual([m['text'] for m in state['journal']], ['old reply'])


if __name__=='__main__': unittest.main()
