"""Deterministic evidence projection; no Desktop, model, or real project reads."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.team_room import agent_visibility as av


NOW = '2026-10-20T10:00:00Z'
TID = '00000000-0000-0000-0000-000000000001'


class AgentVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / '.bot-mode/local').mkdir(parents=True)
        (self.root / 'work/tasks').mkdir(parents=True)
        self.bot = dict(bot_key='alpha:developer', bot_id='developer', name='Developer',
                        role='Developer', project_id='alpha', project_name='Alpha',
                        team_name='Development', thread_id=TID, link='codex://threads/' + TID)
        self.activity = dict(project_id='alpha', source='desktop_relay', fetched_at=NOW,
                             bots=[dict(bot='developer', status='active')])

    def tearDown(self):
        self.temp.cleanup()

    def task(self, extra='', state='in_progress'):
        (self.root / 'work/tasks/A.md').write_text(
            'Task ID: A\nTitle: Agent view\nAssigned executor: developer\nState: ' + state +
            '\nUpdated at: ' + NOW + '\nTracking: https://github.com/example/project/issues/1\n' + extra,
            encoding='utf-8')

    def requests(self, items):
        (self.root / '.bot-mode/local/requests.json').write_text(
            json.dumps(dict(schema=1, requests=items)), encoding='utf-8')

    def card(self, **kwargs):
        return av.project_cards(self.root, [self.bot], self.activity, now=NOW, **kwargs)[0]

    def test_active_action_and_workflow_have_distinct_sources(self):
        self.task('Current action: Implement cards\nStage: development\nState since: 2026-10-20T09:42:00Z\n')
        card = self.card()
        self.assertEqual(card['execution']['state'], 'working')
        self.assertEqual(card['execution']['state_since'], None)  # runtime has no start time
        self.assertEqual(card['task']['state_since'], '2026-10-20T09:42:00Z')
        self.assertEqual(card['task']['workflow'], 'development')
        self.assertEqual(card['action'], 'Implement cards')
        self.assertNotEqual(card['task']['source'], card['execution']['source'])

    def test_waiting_owner_and_agent_only_from_explicit_task_evidence(self):
        for target in ('owner', 'code-reviewer'):
            with self.subTest(target=target):
                self.task('Waiting on: ' + target + '\nWaiting request: REQ-A\n')
                self.requests([dict(id='REQ-A', state='open', **{'from': 'developer', 'to': target},
                                    summary='Review candidate', opened='2026-10-20T09:40:00Z',
                                    link='https://github.com/example/project/pull/1')])
                self.activity['bots'][0]['status'] = 'idle'
                card = self.card()
                self.assertEqual(card['execution']['state'], 'waiting')
                self.assertEqual(card['dependencies'][0]['target'], target)
                self.assertEqual(card['dependencies'][0]['url'], 'https://github.com/example/project/pull/1')
                self.assertEqual(card['attention'], target == 'owner')

    def test_open_request_or_receipt_does_not_prove_waiting_or_start(self):
        self.task('Action: Implement later\nLocal request id: REQ-A\n', 'ready')
        self.requests([dict(id='REQ-A', state='open', **{'from': 'manager', 'to': 'developer'},
                            summary='SECRET REQUEST BODY', opened=NOW, delivery_status='delivered',
                            delivery_receipt={'transcript': 'PRIVATE'})])
        self.activity['bots'][0]['status'] = 'idle'
        card = self.card()
        self.assertEqual(card['execution']['state'], 'idle')
        self.assertIsNone(card['action'])
        self.assertEqual(card['task']['workflow'], 'ready')
        self.assertNotIn('SECRET', json.dumps(card))
        self.assertNotIn('PRIVATE', json.dumps(card))

    def test_pause_overrides_fresh_active_and_stale_workflow(self):
        self.task('Updated at: 2020-01-01T00:00:00Z\n')
        (self.root / '.bot-mode/PAUSED').touch()
        card = self.card()
        self.assertEqual(card['execution']['state'], 'paused')
        self.assertTrue(card['uncertain'])
        self.assertEqual(card['execution']['state_since'], None)
        self.assertEqual(self.card(project_pause=True)['execution']['state'], 'paused')

    def test_explicit_failure_and_blocker_are_attention_not_staleness(self):
        self.activity['bots'][0]['status'] = 'idle'
        self.task('Blocker: Missing approval\nWaiting on: owner\n', 'blocked')
        card = self.card()
        self.assertTrue(card['attention'])
        self.assertEqual(card['blocker'], 'Missing approval')
        self.task('Last result: Build failed\n', 'failed')
        self.assertEqual(self.card()['execution']['state'], 'failed')
        self.assertEqual(self.card()['last_result']['text'], 'Build failed')

    def test_stale_disconnected_missing_binding_not_loaded_never_active_or_failed(self):
        for change in ('stale', 'disconnect', 'missing', 'notLoaded', 'future'):
            with self.subTest(change=change):
                self.activity = dict(source='desktop_relay', fetched_at=NOW,
                                     bots=[dict(bot='developer', status='active')])
                self.bot['thread_id'] = TID
                if change == 'stale': self.activity['fetched_at'] = '2026-10-20T09:00:00Z'
                if change == 'future': self.activity['fetched_at'] = '2027-01-01T00:00:00Z'
                if change == 'disconnect': self.activity['bots'] = []
                if change == 'missing': self.bot['thread_id'] = None
                if change == 'notLoaded': self.activity['bots'][0]['status'] = 'notLoaded'
                card = self.card()
                self.assertEqual(card['execution']['state'], 'unknown')
                self.assertTrue(card['uncertain'])
                self.assertFalse(card['attention'])

    def test_completed_turn_never_completes_unfinished_task(self):
        self.task('Local request id: REQ-A\n')
        self.requests([dict(id='REQ-A', state='answered', **{'from': 'manager', 'to': 'developer'},
                            opened=NOW, answered=NOW, outcome='done', reply_link='https://github.com/example/project/pull/1')])
        self.activity['bots'][0]['status'] = 'completed'
        card = self.card()
        self.assertEqual(card['execution']['state'], 'unknown')
        self.assertEqual(card['task']['workflow'], 'in_progress')
        self.assertIn('Request answered', card['last_result']['text'])

    def test_ambiguous_assignment_stays_unknown_not_arbitrary_latest_task(self):
        self.task('Current action: Do A\n')
        (self.root / 'work/tasks/B.md').write_text('Task ID: B\nAssigned executor: developer\nState: review\nUpdated at: ' + NOW, encoding='utf-8')
        card = self.card()
        self.assertIsNone(card['task'])
        self.assertIsNone(card['action'])
        self.assertIn('Multiple', card['coverage'])

    def test_bad_ledger_fails_closed_and_does_not_read_transcripts_or_escape(self):
        self.task('Current action: Testing\n')
        (self.root / '.bot-mode/local/requests.json').write_text('{', encoding='utf-8')
        card = self.card()
        self.assertIn('requests', card['coverage'])
        self.assertEqual(card['execution']['state'], 'working')
        self.assertEqual(card['transitions'], [])

    def test_qualified_existing_executor_requires_exact_team_and_binding(self):
        self.task()
        f = self.root / 'work/tasks/A.md'
        original = f.read_text(encoding='utf-8')
        for assignment, matches in [('Development/developer (' + TID + ')', True),
                                    ('Other/developer (' + TID + ')', False),
                                    ('Development/developer (00000000-0000-0000-0000-000000000002)', False)]:
            f.write_text(original.replace('Assigned executor: developer', 'Assigned executor: ' + assignment), encoding='utf-8')
            self.assertEqual(self.card()['task'] is not None, matches)

    def test_no_task_body_or_other_bot_data_and_stale_wait_is_not_confirmed(self):
        self.task('Updated at: 2020-01-01T00:00:00Z\nWaiting on: owner\nCurrent action: Old action\n'
                  '## Private notes\nCurrent action: SECRET BODY\n')
        self.activity['bots'][0]['status'] = 'idle'
        card = self.card()
        self.assertFalse(card['attention'])
        self.assertEqual(card['execution']['state'], 'idle')
        self.assertIsNone(card['action'])
        self.assertNotIn('SECRET', json.dumps(card))

    def test_future_start_times_and_malformed_link_never_reach_ui(self):
        self.task('State since: 2028-01-01T00:00:00Z\nTracking: javascript:alert(1)\n')
        card = self.card()
        self.assertIsNone(card['task']['url'])
        self.assertIsNone(card['task']['state_since'])

    def test_fresh_idle_is_idle_even_without_task_evidence_and_partial_error_keeps_it(self):
        self.activity['bots'][0]['status'] = 'idle'
        card = self.card()
        self.assertIsNone(card['task'])
        self.assertEqual(card['execution']['state'], 'idle')
        self.assertIn('not proof that no task is assigned', card['coverage'])
        partial = dict(self.activity, partial_failure=True, error='Metadata partially unavailable')
        self.assertEqual(av.project_cards(self.root, [self.bot], partial, now=NOW)[0]['execution']['state'], 'idle')
        partial['bots'][0]['status'] = 'unknown'
        self.assertEqual(av.project_cards(self.root, [self.bot], partial, now=NOW)[0]['execution']['state'], 'unknown')
        self.activity['fetched_at'] = '2026-10-20T09:00:00Z'
        self.assertEqual(self.card()['execution']['state'], 'unknown')

    def test_idle_with_an_explicit_fresh_owner_wait_is_waiting(self):
        self.activity['bots'][0]['status'] = 'idle'
        self.task('Waiting on: owner\nCurrent action: Needs decision\n')
        self.assertEqual(self.card()['execution']['state'], 'waiting')
        self.activity['bots'][0]['status'] = 'active'
        self.assertEqual(self.card()['execution']['state'], 'working')
    def test_failed_or_stale_runtime_refresh_cannot_reuse_active(self):
        for flag in ('error', 'stale'):
            activity = dict(self.activity, **{flag: True})
            card = av.project_cards(self.root, [self.bot], activity, now=NOW)[0]
            self.assertEqual(card['execution']['state'], 'unknown')

    def test_wrong_project_activity_cannot_make_this_bot_working(self):
        activity = dict(self.activity, project_id='other-project')
        self.assertEqual(av.project_cards(self.root, [self.bot], activity, now=NOW)[0]['execution']['state'], 'unknown')

    def test_answered_dependency_cannot_still_confirm_waiting(self):
        self.task('Waiting on: owner\nWaiting request: REQ-A\n')
        self.requests([dict(id='REQ-A', state='answered', **{'from':'developer','to':'owner'}, answered=NOW)])
        self.activity['bots'][0]['status'] = 'idle'
        card = self.card()
        self.assertEqual(card['execution']['state'], 'idle')
        self.assertFalse(card['attention'])
        self.assertIn('resolved', card['coverage'])

    def test_unconfigured_project_and_standalone_are_explicit_unknown(self):
        from scripts.team_room import registry
        standalone = dict(self.bot, bot_key='standalone:cos', project_id=None, project_name='Standalone')
        with patch.object(registry, 'build_overview', return_value=dict(status='ok', bots=[standalone], teams=[])):
            card = av.build(self.root, now=NOW)['agents'][0]
        self.assertEqual(card['execution']['state'], 'unknown')
        self.assertIsNone(card['task'])

    def test_build_uses_registry_roots_and_existing_activity_no_writes(self):
        from scripts.team_room import registry
        config = self.root / '.bot-mode/local/office-project-roots.json'
        config.write_text(json.dumps(dict(schema=1, projects=[dict(id='alpha', root=str(self.root))], standalone=[])))
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        bridge = object()
        with patch.object(registry, 'build_overview', return_value=dict(status='ok', bots=[self.bot], teams=[])), \
             patch.object(av.desktop_relay, 'get_project_activity', return_value=self.activity) as activity:
            result = av.build(self.root, bridge, now=NOW)
        self.assertEqual(result['agents'][0]['execution']['state'], 'working')
        activity.assert_called_once_with(bridge, 'alpha', current_project_root=self.root)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_build_observes_each_project_after_its_serial_activity_fetch(self):
        from scripts.team_room import registry
        config=self.root/'.bot-mode/local/office-project-roots.json'
        roots=[self.root/'alpha',self.root/'beta']
        for path in roots:path.mkdir()
        config.write_text(json.dumps(dict(schema=1,projects=[dict(id=pid,root=str(path)) for pid,path in zip(('alpha','beta'),roots)],standalone=[])))
        bots=[self.bot,dict(self.bot,bot_key='beta:developer',project_id='beta',project_name='Beta')]
        sources=['2026-10-20T10:00:02Z','2026-10-20T10:00:05Z']
        class DelayedBridge:
            index=0
            def activity(self, pid):
                source=sources[self.index];self.index+=1
                return dict(project_id=pid,source='desktop_relay',fetched_at=source,
                            bots=[dict(bot='developer',status='active')])
        clocks=iter(['2026-10-20T10:00:03Z','2026-10-20T10:00:06Z','2026-10-20T10:00:07Z'])
        with patch.object(registry,'build_overview',return_value=dict(status='ok',bots=bots,teams=[])), \
             patch.object(av.desktop_relay,'now_utc',side_effect=lambda: next(clocks)):
            result=av.build(self.root,DelayedBridge())
        self.assertEqual(result['observed_at'],'2026-10-20T10:00:07Z')
        cards={card['project_id']:card for card in result['agents']}
        self.assertEqual([cards[p]['execution']['state'] for p in ('alpha','beta')],['working','working'])
        for pid, observed, source in [('alpha','2026-10-20T10:00:03Z',sources[0]),('beta','2026-10-20T10:00:06Z',sources[1])]:
            self.assertEqual(cards[pid]['execution']['observed_at'],observed)
            self.assertEqual(cards[pid]['execution']['source_at'],source)
            self.assertEqual(cards[pid]['runtime']['source_at'],source)

    def test_real_registry_two_projects_same_bot_id_do_not_share_tasks_or_pauses(self):
        from scripts.team_room import roster
        config = self.root / '.bot-mode/local/office-project-roots.json'
        projects = []
        for name in ('alpha', 'beta'):
            p = self.root / name
            p.mkdir()
            r = roster.empty(p, name)
            roster.add_team(r, 'Development', name + ' Development', 'development')
            roster.add_participant(r, 'developer', 'Developer', 'Development', 'team/Development/developer', TID)
            roster.save(p, r, roster.version(roster.empty(p)))
            (p / 'work/tasks').mkdir(parents=True)
            (p / 'work/tasks/T.md').write_text('Task ID: ' + name + '\nAssigned executor: developer\nState: review\nUpdated at: '+NOW, encoding='utf-8')
            projects.append(dict(id=name, root=str(p)))
        (self.root / 'beta/.bot-mode/PAUSED').touch()
        config.write_text(json.dumps(dict(schema=1,projects=projects,standalone=[])),encoding='utf-8')
        bridge = object()
        with patch.object(av.desktop_relay, 'get_project_activity', return_value=self.activity):
            result = av.build(self.root, bridge, now=NOW)
        cards = {a['bot_key']: a for a in result['agents']}
        self.assertEqual(cards['alpha:developer']['task']['id'], 'alpha')
        self.assertEqual(cards['beta:developer']['task']['id'], 'beta')
        self.assertEqual(cards['alpha:developer']['execution']['state'], 'working')
        self.assertEqual(cards['beta:developer']['execution']['state'], 'paused')

    def test_owner_readmodel_pause_overrides_active_without_refresh_writes(self):
        from scripts.team_room import project_status, registry
        snap = dict(schema=1, generated_at=NOW, generated_by='owner', projects=[
            dict(id='alpha',name='Alpha',status='paused',summary='Owner pause',source=dict(kind='local',label='Owner',available=True,as_of=NOW))])
        project_status.path(self.root).write_text(json.dumps(snap),encoding='utf-8')
        config = self.root / '.bot-mode/local/office-project-roots.json'
        config.write_text(json.dumps(dict(schema=1,projects=[dict(id='alpha',root=str(self.root))],standalone=[])))
        with patch.object(registry, 'build_overview', return_value=dict(status='ok', bots=[self.bot], teams=[])), \
             patch.object(av.desktop_relay, 'get_project_activity', return_value=self.activity), \
             patch.object(project_status, 'assemble_derived_read_model', side_effect=AssertionError('Must not refresh/write')):
            result = av.build(self.root, object(), now=NOW)
        self.assertEqual(result['agents'][0]['execution']['state'], 'paused')

    def test_owner_snapshot_can_clear_old_cached_pause(self):
        from scripts.team_room import project_status
        snap = dict(schema=1,generated_at=NOW,generated_by='owner',projects=[dict(id='alpha',name='Alpha',
                    status='paused',summary='Recorded pause',source=dict(kind='local',label='Owner',available=True,as_of=NOW))])
        project_status.derived_cache_path(self.root).write_text(json.dumps(snap),encoding='utf-8')
        snap['projects'][0]['status'] = 'working'
        project_status.path(self.root).write_text(json.dumps(snap),encoding='utf-8')
        self.assertFalse(av.pause_map(self.root)['alpha'])

    def test_effective_terminal_or_pause_suppresses_action_independently_of_runtime(self):
        for state, stage, workflow, execution in [
            ('in_progress','completed','completed','working'),
            ('completed','development','completed','working'),
            ('in_progress','paused','paused','paused'),
            ('paused','development','paused','paused'),
            ('failed','development','failed','working'),
        ]:
            with self.subTest(state=state,stage=stage):
                self.task('Stage: '+stage+'\nCurrent action: HISTORICAL_ACTION\n',state)
                card=self.card()
                self.assertEqual(card['task']['workflow'],workflow)
                self.assertEqual(card['execution']['state'],execution)
                self.assertIsNone(card['action'])
                self.assertEqual(card['task']['recorded_action'],'HISTORICAL_ACTION')
        self.task('Current action: HISTORICAL_ACTION\n')
        (self.root / '.bot-mode/PAUSED').touch()
        self.assertIsNone(self.card()['action'])

    def test_active_runtime_with_failed_task_has_failure_attention_reason(self):
        self.task('', 'failed')
        card=self.card()
        self.assertEqual(card['execution']['state'],'working')
        self.assertTrue(card['attention'])
        self.assertEqual(card['attention_reasons'],['failure'])

    def test_answered_and_cancelled_dependencies_cannot_confirm_a_current_wait(self):
        for state in ('answered','cancelled'):
            with self.subTest(state=state):
                self.task('Waiting on: owner\nWaiting request: R\n')
                self.requests([dict(id='R',state=state,**{'from':'developer','to':'owner'})])
                self.activity['bots'][0]['status']='idle'
                card=self.card()
                self.assertEqual(card['dependencies'][0]['freshness'],'unknown')
                self.assertEqual(card['dependencies'][0]['source_at'],NOW)
                self.assertEqual(card['execution']['state'],'idle')
                self.assertFalse(card['attention'])

    def test_malformed_request_scalars_are_skipped_without_content_or_endpoint_exception(self):
        self.task('Local request id: R\nWaiting request: R\nWaiting on: owner\n')
        base=dict(id='R',state='answered',**{'from':'developer','to':'owner'},answered=NOW,outcome='done')
        for field, value in [('id',['bad']),('id',{'private':'CANARY'}),('state',[]),('state','done'),
                             ('from',{}),('to',[]),('outcome',{'private_body':'CANARY'}),('outcome','CANARY')]:
            with self.subTest(field=field,value=value):
                self.requests([dict(base,**{field:value})])
                card=self.card()
                self.assertNotIn('CANARY',json.dumps(card))
                self.assertIsNone(card['last_result'])
                self.assertEqual(card['transitions'],[])
                self.assertTrue(card['uncertain'])
                self.assertIn('Invalid request',card['coverage'])
                self.assertFalse(card['attention'])

    def test_only_writer_outcome_labels_can_be_projected(self):
        self.task('Local request id: R\n')
        for outcome in ('done','blocked','failed','refused','partial'):
            with self.subTest(outcome=outcome):
                self.requests([dict(id='R',state='answered',**{'from':'manager','to':'developer'},answered=NOW,outcome=outcome)])
                self.assertEqual(self.card()['last_result']['text'],'Request answered: '+outcome+' (not task completion)')


if __name__ == '__main__':
    unittest.main()
