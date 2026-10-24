import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import project_status as ps  # noqa: E402

UPDATE = json.loads((ROOT / 'tests/fixtures/project-status-update.json').read_text(encoding='utf-8'))


class ProjectStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def update(self, **project):
        data = copy.deepcopy(UPDATE)
        data['projects'][0].update(project)
        return data

    def test_upsert_is_idempotent_and_merges_by_stable_id(self):
        self.assertEqual(ps.read(self.root)['available'], False)
        first = ps.upsert(self.root, copy.deepcopy(UPDATE))
        self.assertEqual((first['result'], first['new_projects']), ('stored', ['example-shop']))
        again = ps.upsert(self.root, copy.deepcopy(UPDATE))
        self.assertEqual(again['updated_projects'], ['example-shop'])
        report = {'id': 'rpt-2', 'at': '2026-10-15T08:00:00Z', 'from': 'Manager', 'text': 'Newer report.'}
        ps.upsert(self.root, {'projects': [{'id': 'example-shop', 'reports': [report]}]})
        project = ps.read(self.root)['snapshot']['projects'][0]
        self.assertEqual([r['id'] for r in project['reports']], ['rpt-2', 'rpt-2026-10-14-1'])
        self.assertEqual(len(project['decisions']), 1)
        self.assertFalse(list(ps.path(self.root).parent.glob('*.tmp')))
        self.assertFalse(ps.path(self.root).with_suffix('.lock').exists())

    def test_answered_decision_requires_authoritative_evidence(self):
        decision = dict(UPDATE['projects'][0]['decisions'][0], state='answered')
        with self.assertRaisesRegex(ps.StatusError, r'decisions\[0\]\.answer'):
            ps.upsert(self.root, self.update(decisions=[decision]))
        decision['answer'] = {'text': 'Keep 50.', 'by': 'Owner', 'at': '2026-10-14T09:00:00Z',
                              'source_url': 'https://github.com/example-org/example-shop/issues/12'}
        ps.upsert(self.root, self.update(decisions=[decision]))
        pending = dict(decision, state='pending')
        with self.assertRaises(ps.StatusError):
            ps.upsert(self.root, self.update(decisions=[pending]))

    def test_rejects_unsafe_or_unbounded_content_without_writing(self):
        source = dict(UPDATE['projects'][0]['source'])
        bad = [
            {'source': {k: v for k, v in source.items() if k != 'available'}},
            {'source': dict(source, url='https://evil.example/x')},
            {'source': dict(source, url='https://github.com.evil.example/x')},
            {'source': dict(source, url='javascript:alert(1)')},
            {'source': dict(source, url='https://user:pw@github.com/x')},
            {'source': dict(source, url='file:///C:/private')},
            {'summary': 'x' * 281},
            {'status': 'done'},
            {'id': 'Bad Id'},
            {'unexpected': 'field'},
            {'decisions': [dict(UPDATE['projects'][0]['decisions'][0], id=f'd{i}') for i in range(11)]},
            {'tasks': [{'id': f't{i}', 'title': 't', 'state': 'open'} for i in range(21)]},
        ]
        for change in bad:
            with self.subTest(change=list(change)), self.assertRaises(ps.StatusError):
                ps.upsert(self.root, self.update(**change))
        self.assertFalse(ps.path(self.root).exists())

    def test_invalid_file_reports_error_without_content_and_blocks_upsert(self):
        file = ps.path(self.root)
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps({'schema': 1, 'secret-note': 'PRIVATE-MARKER'}), encoding='utf-8')
        result = ps.read(self.root)
        self.assertFalse(result['available'])
        self.assertNotIn('PRIVATE-MARKER', json.dumps(result))
        with self.assertRaises(ps.StatusError):
            ps.upsert(self.root, copy.deepcopy(UPDATE))
        file.write_text('x' * (ps.MAX_BYTES + 1), encoding='utf-8')
        self.assertFalse(ps.read(self.root)['available'])

    def test_messages_never_echo_content_and_dry_merge_writes_nothing(self):
        with self.assertRaises(ps.StatusError) as caught:
            ps.upsert(self.root, self.update(**{'secret-key-name': 1}))
        self.assertNotIn('secret-key-name', str(caught.exception))
        duplicated = {'schema': 1, 'generated_at': '2026-10-14T08:00:00Z', 'generated_by': 'chief-of-staff',
                      'projects': [copy.deepcopy(UPDATE['projects'][0]), copy.deepcopy(UPDATE['projects'][0])]}
        with self.assertRaises(ps.StatusError) as caught:
            ps.validate(duplicated)
        self.assertNotIn('example-shop', str(caught.exception))
        ps.upsert(self.root, copy.deepcopy(UPDATE))
        before = ps.path(self.root).read_bytes()
        report = ps.dry_merge(self.root, {'projects': [{'id': 'other-shop', **{k: v for k, v in UPDATE['projects'][0].items() if k != 'id'}}]})
        self.assertEqual(report['new_projects'], ['other-shop'])
        self.assertEqual(ps.path(self.root).read_bytes(), before)
        self.assertEqual(sorted(p.name for p in ps.path(self.root).parent.iterdir()), ['project-status.json'])

    def test_lock_is_held_while_reading_so_updates_are_not_lost(self):
        ps.upsert(self.root, copy.deepcopy(UPDATE))
        seen = []
        original = ps._current
        def spy(project):
            seen.append(ps.path(project).with_suffix('.lock').exists())
            return original(project)
        ps._current = spy
        try:
            ps.upsert(self.root, copy.deepcopy(UPDATE))
        finally:
            ps._current = original
        self.assertEqual(seen, [True])

    def test_concurrent_writer_is_refused(self):
        ps.path(self.root).parent.mkdir(parents=True)
        ps.path(self.root).with_suffix('.lock').write_text('', encoding='utf-8')
        with self.assertRaisesRegex(ps.StatusError, 'writer'):
            ps.upsert(self.root, copy.deepcopy(UPDATE))
        self.assertFalse(ps.path(self.root).exists())


    def test_decision_suggestion_contract(self):
        dec = copy.deepcopy(UPDATE['projects'][0]['decisions'][0])
        dec['suggestion'] = {
            'options': ['Option A', 'Option B'],
            'recommended': 'Option A',
            'context': 'Recommended because of lower cost.',
            'by': 'Manager',
            'at': '2026-10-14T10:00:00Z',
            'source_url': 'https://github.com/example-org/example-shop/issues/12'
        }
        res = ps.upsert(self.root, self.update(decisions=[dec]))
        loaded = ps.read(self.root)['snapshot']['projects'][0]['decisions'][0]['suggestion']
        self.assertEqual(loaded['recommended'], 'Option A')
        self.assertEqual(len(loaded['options']), 2)

        # Invalid cases
        bad_cases = [
            {'options': []},
            {'options': ['1', '2', '3', '4', '5']},
            {'options': ['Dup', 'Dup']},
            {'options': ['A'], 'recommended': 'B'},
            {'options': ['A' * 81]},
            {'options': ['A'], 'context': 'c' * 281, 'by': 'M', 'at': '2026-10-14T10:00:00Z', 'source_url': 'https://github.com/x'},
            {'options': ['A'], 'by': 'M', 'at': '2026-10-14T10:00:00Z', 'source_url': 'https://evil.com'},
            {'options': ['A'], 'by': 'M', 'at': 'not-iso', 'source_url': 'https://github.com/x'},
            {'options': ['A'], 'by': 'M', 'at': '2026-10-14T10:00:00Z', 'source_url': 'https://github.com/x', 'extra': 1},
        ]
        for b in bad_cases:
            bad_dec = copy.deepcopy(dec)
            sugg = {
                'options': ['Option A'],
                'by': 'Manager',
                'at': '2026-10-14T10:00:00Z',
                'source_url': 'https://github.com/example-org/example-shop/issues/12'
            }
            sugg.update(b)
            bad_dec['suggestion'] = sugg
            with self.subTest(b=b), self.assertRaises(ps.StatusError):
                ps.validate(self.update(decisions=[bad_dec]))


if __name__ == '__main__':
    unittest.main()
