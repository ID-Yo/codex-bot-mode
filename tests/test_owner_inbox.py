import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.team_room import owner_inbox


NOW = '2026-10-23T13:00:00Z'
ISSUE = 'https://github.com/example/project/issues/7'


class OwnerInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / '.bot-mode/local'
        self.local.mkdir(parents=True)

    def snapshot(self, decisions):
        return {'available': True, 'snapshot': {'projects': [{
            'id': 'alpha', 'name': 'Alpha', 'source': {'available': True, 'as_of': NOW},
            'decisions': decisions}], 'unassigned': []}}

    def decision(self, **extra):
        return dict(id='d1', question='Approve candidate', requested_by='Manager',
                    requested_at=NOW, source_url=ISSUE, state='pending', **extra)

    def build(self, snapshot):
        with patch.object(owner_inbox.registry, 'registry_path', return_value=None), \
             patch.object(owner_inbox, 'summary_model', return_value=snapshot):
            return owner_inbox.build(self.root, now=NOW)

    def test_pending_decisions_include_hidden_projects_without_visibility_filter(self):
        result = self.build(self.snapshot([self.decision(urgency='urgent')]))
        item = result['items'][0]
        self.assertEqual(item['title'], 'Approve candidate')
        self.assertEqual(item['issue_url'], ISSUE)
        self.assertEqual(item['project_id'], 'alpha')
        self.assertTrue(item['urgent'])
        self.assertEqual(item['freshness'], 'fresh')
        self.assertEqual(item['action'], 'open_source')

    def test_answered_and_cancelled_are_not_actionable(self):
        decisions = [self.decision()]
        decisions[0]['state'] = 'answered'
        self.assertEqual(self.build(self.snapshot(decisions))['items'], [])
        decisions[0]['state'] = 'cancelled'
        self.assertEqual(self.build(self.snapshot(decisions))['items'], [])

    def test_owner_request_and_decision_share_one_issue_with_all_provenance(self):
        (self.local / 'requests.json').write_text(json.dumps({'schema': 1, 'requests': [{
            'id': 'R1', 'from': 'developer', 'to': 'owner', 'state': 'open',
            'summary': 'Review candidate', 'link': ISSUE, 'opened': NOW,
            'due': '2026-10-23T12:00:00Z'}]}), encoding='utf-8')
        result = self.build(self.snapshot([self.decision()]))
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(len(result['items'][0]['sources']), 2)
        self.assertTrue(result['items'][0]['urgent'])

    def test_stale_or_missing_sources_remain_explicit(self):
        data = self.snapshot([self.decision()])
        data['snapshot']['projects'][0]['source']['as_of'] = '2026-10-01T10:00:00Z'
        self.assertEqual(self.build(data)['items'][0]['freshness'], 'stale')
        result = self.build({'available': False})
        self.assertEqual(result['items'], [])
        self.assertFalse(result['complete'])

    def test_answered_request_and_nonowner_request_are_excluded(self):
        (self.local / 'requests.json').write_text(json.dumps({'schema': 1, 'requests': [
            dict(id='r1', **{'from': 'developer', 'to': 'owner'}, state='answered', summary='Old'),
            dict(id='r2', **{'from': 'developer', 'to': 'verifier'}, state='open', summary='Internal')
        ]}), encoding='utf-8')
        self.assertEqual(self.build({'available': False})['items'], [])

    def test_request_without_issue_is_exposed_as_tracking_gap(self):
        (self.local / 'requests.json').write_text(json.dumps({'schema': 1, 'requests': [{
            'id': 'r1', 'from': 'developer', 'to': 'owner', 'state': 'open',
            'summary': 'Need permission', 'opened': NOW, 'link': 'javascript:alert(1)'}]}), encoding='utf-8')
        item = self.build({'available': False})['items'][0]
        self.assertIsNone(item['issue_url'])
        self.assertTrue(item['tracking_required'])
        self.assertEqual(item['action'], 'record_issue')

    def test_oversized_or_escaped_ledger_does_not_expose_content(self):
        (self.local / 'requests.json').write_bytes(b'X' * (owner_inbox.MAX_BYTES + 1))
        result = self.build({'available': False})
        self.assertEqual(result['items'], [])
        self.assertFalse(result['complete'])

    def test_valid_ledger_above_the_old_limit_is_read_and_oversize_is_named(self):
        rows = [dict(id=f'r{i}', **{'from': 'developer'}, to='developer', summary='x' * 200, state='answered', opened=NOW)
                for i in range(1300)]
        rows.append(dict(id='o1', **{'from': 'developer'}, to='owner', summary='Decide', state='open', opened=NOW))
        data = json.dumps({'schema': 1, 'requests': rows})
        self.assertGreater(len(data), owner_inbox.MAX_BYTES)
        (self.local / 'requests.json').write_text(data, encoding='utf-8')
        result = self.build({'available': False})
        self.assertEqual([i['title'] for i in result['items']], ['Decide'])
        self.assertFalse(any('ledger' in w for w in result['warnings']))
        (self.local / 'requests.json').write_bytes(b'X' * (owner_inbox.LEDGER_BYTES + 1))
        result = self.build({'available': False})
        self.assertTrue(any('exceeds the size limit' in w for w in result['warnings']))
        self.assertNotIn('X' * 20, json.dumps(result))
    def test_missing_ledger_marks_supported_coverage_incomplete(self):
        result = self.build(self.snapshot([]))
        self.assertFalse(result['complete'])
        self.assertTrue(any('ledger' in warning for warning in result['warnings']))

    def test_invalid_request_dates_never_echo_nested_content(self):
        (self.local / 'requests.json').write_text(json.dumps({'schema': 1, 'requests': [{
            'id': 'r1', 'from': 'developer', 'to': 'owner', 'state': 'open',
            'summary': 'Need permission', 'opened': {'private': 'CANARY'},
            'due': {'private': 'DUE_CANARY'}, 'link': ISSUE}]}), encoding='utf-8')
        result = self.build(self.snapshot([]))
        self.assertNotIn('CANARY', json.dumps(result))
        self.assertIsNone(result['items'][0]['source_at'])
        self.assertEqual(result['items'][0]['freshness'], 'unknown')
        self.assertFalse(result['complete'])

    def test_summary_cannot_read_outside_root_or_invoke_assembler(self):
        with tempfile.TemporaryDirectory() as outside:
            file = Path(outside) / 'derived-read-model.json'
            file.write_text('OUTSIDE_CANARY', encoding='utf-8')
            with patch.object(owner_inbox.project_status, 'derived_cache_path', return_value=file), \
                 patch.object(owner_inbox.project_status, 'read_model', side_effect=AssertionError('Assembler called')):
                result = owner_inbox.summary_model(self.root)
            self.assertFalse(result['available'])
            self.assertNotIn('OUTSIDE_CANARY', json.dumps(result))

    def test_actual_windows_junction_summary_is_refused(self):
        import os
        import subprocess
        if os.name != 'nt':
            self.skipTest('Windows junction regression')
        with tempfile.TemporaryDirectory() as outside:
            child = self.root / 'alpha'
            (child / '.bot-mode').mkdir(parents=True)
            junction = child / '.bot-mode/local'
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), outside],
                           check=True, capture_output=True)
            try:
                snapshot = self.snapshot([self.decision()])['snapshot']
                snapshot.update(schema=1, generated_at=NOW, generated_by='manager')
                project = snapshot['projects'][0]
                project.update(status='blocked', summary='OUTSIDE_CANARY')
                project['source'].update(kind='local', label='Fixture summary')
                project['decisions'][0]['question'] = 'OUTSIDE_CANARY'
                owner_inbox.project_status.validate(snapshot)
                cache = Path(outside) / owner_inbox.project_status.DERIVED_CACHE_NAME
                fallback = Path(outside) / 'project-status.json'
                for target in (cache, fallback):
                    target.write_text(json.dumps(snapshot), encoding='utf-8')
                for cache_exists in (True, False):
                    if not cache_exists:
                        cache.unlink()
                    result = owner_inbox.summary_model(child)
                    self.assertFalse(result['available'])
                    self.assertNotIn('OUTSIDE_CANARY', json.dumps(result))
            finally:
                junction.rmdir()

    def test_registry_is_bounded_before_loading(self):
        file = self.root / 'registry.json'
        file.write_bytes(b'X' * (owner_inbox.MAX_BYTES + 1))
        with patch.object(owner_inbox.registry, 'registry_path', return_value=file), \
             patch.object(owner_inbox.registry, 'load_registry', side_effect=AssertionError('Oversized read')), \
             patch.object(owner_inbox, 'summary_model', return_value=self.snapshot([])):
            result = owner_inbox.build(self.root, now=NOW)
        self.assertFalse(result['complete'])
        self.assertTrue(any('registry' in warning for warning in result['warnings']))

    def test_registered_regular_file_root_marks_coverage_incomplete(self):
        invalid_root = self.root / 'not-a-directory'
        invalid_root.write_text('Not a project', encoding='utf-8')
        (self.local / 'requests.json').write_text('{"schema":1,"requests":[]}', encoding='utf-8')
        config = self.root / 'registry.json'
        config.write_text(json.dumps({'schema': 1, 'projects': [
            {'id': 'invalid', 'root': str(invalid_root)}]}), encoding='utf-8')
        with patch.object(owner_inbox.registry, 'registry_path', return_value=config), \
             patch.object(owner_inbox, 'summary_model', return_value=self.snapshot([])):
            result = owner_inbox.build(self.root, now=NOW)
        self.assertFalse(result['complete'])
        self.assertTrue(any('not a directory' in warning for warning in result['warnings']))

    def test_deduplicated_sources_keep_individual_and_latest_freshness(self):
        (self.local / 'requests.json').write_text(json.dumps({'schema': 1, 'requests': [{
            'id': 'R1', 'from': 'developer', 'to': 'owner', 'state': 'open',
            'summary': 'Review candidate', 'link': ISSUE, 'opened': NOW}]}), encoding='utf-8')
        data = self.snapshot([self.decision()])
        data['snapshot']['projects'][0]['source']['as_of'] = '2026-10-01T10:00:00Z'
        item = self.build(data)['items'][0]
        self.assertEqual(item['freshness'], 'fresh')
        self.assertEqual({source['freshness'] for source in item['sources']}, {'fresh', 'stale'})
        self.assertTrue(item['mixed_freshness'])

    def test_collection_is_bounded_before_projecting_whole_ledger(self):
        rows = [dict(id=f'R{i}', **{'from': 'developer', 'to': 'owner'}, state='open',
                     summary='Owner input', opened=NOW) for i in range(20)]
        (self.local / 'requests.json').write_text(json.dumps({'schema': 1, 'requests': rows}), encoding='utf-8')
        with patch.object(owner_inbox, 'MAX_ITEMS', 5):
            result = self.build(self.snapshot([]))
        self.assertEqual(len(result['items']), 5)
        self.assertFalse(result['complete'])
        self.assertTrue(any('collection limit' in warning for warning in result['warnings']))
