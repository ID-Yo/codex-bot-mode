import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from team_room import project_status

NOW = datetime.now(timezone.utc)


def iso(hours):
    return (NOW + timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


class DashboardCounterTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fetched = {'issue': [], 'pr': []}
        original = project_status.WORK_FETCH
        project_status.WORK_FETCH = lambda repo: self.fetched
        self.addCleanup(setattr, project_status, 'WORK_FETCH', original)
        self.host = self.dir / 'host'
        (self.host / '.bot-mode' / 'local').mkdir(parents=True)
        self.projects = []

    def project(self, pid, requests=None, repository=None):
        root = self.dir / pid
        (root / '.bot-mode' / 'local').mkdir(parents=True)
        if requests is not None:
            (root / '.bot-mode' / 'local' / 'requests.json').write_text(json.dumps({'schema': 1, 'requests': requests}), encoding='utf-8')
        row = {'id': pid, 'name': pid.title(), 'root': str(root)}
        if repository:
            row['repository'] = repository
        self.projects.append(row)

    def request(self, rid, state='open', outcome=None):
        row = {'id': rid, 'from': 'manager', 'to': 'developer', 'summary': rid, 'link': 'codex://threads/x', 'opened': iso(-3),
               'due': iso(2), 'state': state, 'escalations': []}
        if state == 'answered':
            row.update(outcome=outcome, answered=iso(-2))
        return row

    def model(self):
        (self.host / '.bot-mode' / 'local' / 'office-project-roots.json').write_text(
            json.dumps({'schema': 1, 'projects': self.projects, 'standalone': []}), encoding='utf-8')
        os.environ['CODEX_OFFICE_PROJECT_ROOTS'] = ''
        return {p['id']: p for p in project_status.assemble_derived_read_model(self.host, sources_map={})['projects']}

    def test_counters_come_from_ledger_and_labels_with_fresh_as_of(self):
        self.project('alpha', [self.request('a-1'), self.request('a-2', 'answered', 'done')], repository='o/r')
        self.fetched['issue'] = [{'number': 5, 'state': 'OPEN', 'title': 't', 'updatedAt': iso(-1), 'url': 'https://github.com/o/r/issues/5',
                                  'labels': [{'name': 'state:review'}]}]
        alpha = self.model()['alpha']
        self.assertEqual((alpha['work']['state'], alpha['work']['done'], alpha['work']['review']), ('known', 1, 1))
        self.assertEqual(alpha['open_tasks'], alpha['work']['open'])
        self.assertTrue(alpha['source']['available'])
        self.assertLess((datetime.now(timezone.utc) - datetime.fromisoformat(alpha['source']['as_of'].replace('Z', '+00:00'))).total_seconds(), 60)
        self.assertNotEqual(alpha['status'], 'working' if alpha['work']['open'] == 0 else None)

    def test_unavailable_source_is_unknown_with_a_reason(self):
        def broken(repo):
            raise RuntimeError('offline')
        project_status.WORK_FETCH = broken
        self.project('beta', [self.request('b-1')], repository='o/r')
        self.project('gamma')
        models = self.model()
        self.assertEqual(models['beta']['work']['state'], 'unknown')
        self.assertIn('github unavailable', models['beta']['work']['error'])
        self.assertIsNone(models['beta']['open_tasks'])
        self.assertFalse(models['beta']['source']['available'])
        self.assertEqual(models['gamma']['work']['state'], 'unknown')
        self.assertIn('no GitHub labels', models['gamma']['source']['error'])

    def test_stored_snapshot_is_validated_by_its_written_size(self):
        # Compact JSON fits the limit while the stored indent=2 file does not: it must be rejected (#98).
        projects = [{'id': f'p{n}', 'name': f'P{n}', 'status': 'unknown', 'summary': 's', 'tasks': [],
                     'source': {'kind': 'local', 'label': 'l', 'available': False}} for n in range(20)]
        snapshot = {'schema': 1, 'generated_at': iso(0), 'generated_by': 'chief-of-staff', 'projects': projects}
        size = lambda indent: len(json.dumps(snapshot, ensure_ascii=False, indent=indent).encode()) + (1 if indent else 0)
        added = 0
        while size(2) <= project_status.MAX_BYTES and added < 400:
            projects[added % 20]['tasks'].append({'id': f't{added}', 'title': 'x' * 100, 'state': 'open'})
            added += 1
        self.assertGreater(size(2), project_status.MAX_BYTES)
        self.assertLessEqual(size(None) - 1, project_status.MAX_BYTES, 'fixture must straddle the limit')
        with self.assertRaises(project_status.StatusError):
            project_status.validate(snapshot)
        projects[0]['tasks'].pop(); projects[1]['tasks'].pop(); projects[2]['tasks'].pop(); projects[3]['tasks'].pop()
        project_status.validate(snapshot)

    def test_worker_refreshes_an_aged_cache_without_source_changes(self):
        self.project('alpha', [self.request('a-1')])
        self.model()
        before = project_status.read_model(self.host)['snapshot']['projects'][0]['work']['open']
        ledger = self.dir / 'alpha' / '.bot-mode' / 'local' / 'requests.json'
        ledger.write_text(json.dumps({'schema': 1, 'requests': [self.request('a-1'), self.request('a-2')]}), encoding='utf-8')
        cache = project_status.derived_cache_path(self.host)
        old = time.time() - project_status.WORK_MAX_AGE - 5
        os.utime(cache, (old, old))
        worker = project_status.ReadModelWorker(self.host, interval_seconds=60, sources_map={})
        worker._last_signatures = {}
        worker.refresh_if_changed()
        self.assertGreater(cache.stat().st_mtime, old + project_status.WORK_MAX_AGE)
        after = project_status.read_model(self.host)['snapshot']['projects'][0]['work']['open']
        self.assertEqual((before, after), (1, 2))
        worker._last_signatures = None
        # Worst case a change waits: age limit + worker polling interval + one full read of all projects (measured ~25 s, budget 60).
        self.assertLessEqual(project_status.WORK_MAX_AGE + 60 + 60, 300)


if __name__ == '__main__':
    unittest.main()

