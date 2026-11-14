import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts.team_room import owner_inbox, owner_issues

NOW = '2026-11-09T12:00:00+00:00'
URL = 'https://github.com/example-org/alpha/issues/12'


def gh(rows=None, code=0, err=''):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, code, json.dumps(rows if rows is not None else []), err)
    run.calls = calls
    return run


ROW = {'number': 12, 'title': 'Approve release', 'url': URL, 'updatedAt': NOW,
       'labels': [{'name': 'state:needs-owner'}, {'name': 'priority:high'}], 'assignees': [{'login': 'ivan'}]}


class OwnerIssues(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build(self, ensure=None):
        warnings, candidates = [], []
        at = datetime.fromisoformat(NOW)
        projects = [('alpha', 'Alpha', self.root, ['example-org/alpha'], True)]
        owner_inbox.github_issue_candidates(self.root, projects, at, warnings, candidates, ensure=ensure or (lambda *a: None))
        return warnings, candidates

    def cache(self, **entry):
        path = self.root / owner_issues.CACHE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'repos': {'example-org/alpha': entry}}), encoding='utf-8')

    def test_fetch_uses_label_and_parses_issue(self):
        run = gh([ROW])
        issues = owner_issues.fetch_repo('example-org/alpha', run)
        self.assertIn('state:needs-owner', run.calls[0])
        self.assertEqual(issues[0]['number'], 12)
        self.assertEqual(issues[0]['assignees'], ['ivan'])

    def test_failed_gh_keeps_previous_issues_and_records_error(self):
        owner_issues.refresh(self.root, ['example-org/alpha'], gh([ROW]))
        owner_issues.refresh(self.root, ['example-org/alpha'], gh(code=1, err='HTTP 502'))
        entry = owner_issues.read_cache(self.root)['repos']['example-org/alpha']
        self.assertEqual(len(entry['issues']), 1)
        self.assertIn('HTTP 502', entry['error'])

    def test_cached_issue_becomes_urgent_inbox_row(self):
        self.cache(issues=owner_issues.fetch_repo('example-org/alpha', gh([ROW])), fetched_at=NOW, error=None)
        warnings, rows = self.build()
        self.assertEqual(warnings, [])
        self.assertEqual((rows[0]['kind'], rows[0]['issue_url'], rows[0]['urgent']), ('issue', URL, True))
        self.assertEqual(rows[0]['source']['id'], 'example-org/alpha#12')

    def test_missing_cache_is_incomplete_not_empty(self):
        warnings, rows = self.build()
        self.assertEqual(rows, [])
        self.assertTrue(any('not loaded yet' in w for w in warnings))

    def test_stale_cache_is_reported(self):
        old = (datetime.fromisoformat(NOW) - timedelta(hours=2)).isoformat()
        self.cache(issues=[], fetched_at=old, error=None)
        self.assertTrue(any('stale' in w for w in self.build()[0]))

    def test_refresh_failure_is_reported(self):
        self.cache(issues=[], fetched_at=NOW, error='gh failed: HTTP 502')
        self.assertTrue(any('could not be refreshed' in w for w in self.build()[0]))

    def test_ensure_fresh_skips_recent_and_fetches_missing(self):
        run = gh([ROW])
        self.assertTrue(owner_issues.ensure_fresh(self.root, ['example-org/alpha'], run, background=False))
        self.assertFalse(owner_issues.ensure_fresh(self.root, ['example-org/alpha'], run, background=False))
        self.assertEqual(len(run.calls), 1)

    def test_ledger_and_issue_share_one_row(self):
        self.cache(issues=owner_issues.fetch_repo('example-org/alpha', gh([ROW])), fetched_at=NOW, error=None)
        local = self.root / '.bot-mode/local'
        local.mkdir(parents=True, exist_ok=True)
        (local / 'requests.json').write_text(json.dumps({'schema': 1, 'requests': [
            {'id': 'r1', 'state': 'open', 'to': 'owner', 'from': 'manager', 'summary': 'Approve release',
             'link': URL, 'opened': NOW}]}), encoding='utf-8')
        entry = {'id': 'alpha', 'name': 'Alpha', 'root': str(self.root), 'repositories': ['example-org/alpha']}
        (local / 'office-project-roots.json').write_text(json.dumps({'schema': 1, 'projects': [entry]}), encoding='utf-8')
        with patch.object(owner_inbox, 'summary_model', return_value={'available': False}), \
             patch.object(owner_issues, 'ensure_fresh', lambda *a, **k: False):
            result = owner_inbox.build(self.root, now=NOW)
        rows = [r for r in result['items'] if r['issue_url'] == URL]
        self.assertEqual(len(rows), 1)
        self.assertEqual({s['kind'] for s in rows[0]['sources']}, {'github_issue', 'owner_request'})

    def test_repos_from_registry_or_git_remote(self):
        self.assertEqual(owner_issues.repos_for({'repositories': []}, self.root), [])
        self.assertEqual(owner_issues.repos_for({'repositories': ['a/b'], 'repository': 'c/d'}, self.root), ['a/b', 'c/d'])
        run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, 'git@github.com:example-org/routeport.git\n', '')
        self.assertEqual(owner_issues.repos_for({}, self.root, run), ['example-org/routeport'])

    def test_gh_runs_without_a_console_window(self):
        seen = {}

        def run(cmd, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(cmd, 0, '[]', '')
        owner_issues.fetch_repo('a/b', run)
        self.assertIn('creationflags', seen)


if __name__ == '__main__':
    unittest.main()

