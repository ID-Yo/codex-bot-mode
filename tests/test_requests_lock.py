"""Concurrent writers, idempotent terminal states and worktree ledger resolution (#322)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from team_room import requests as rq  # noqa: E402

CLI = str(Path(__file__).resolve().parents[1] / 'scripts' / 'team_room' / 'requests.py')


def run(project, *args):
    return subprocess.Popen([sys.executable, '-B', CLI, '--project', str(project), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()

    def test_separate_processes_lose_no_record_and_no_closure(self):
        for n in range(3):
            self.assertEqual(run(self.project, 'open', f'base-{n}', '--from', 'manager', '--to', 'developer',
                                 '--summary', 'base').wait(), 0)
        procs = []
        for n in range(24):
            procs.append(run(self.project, 'open', f'new-{n}', '--from', 'manager', '--to', 'verifier', '--summary', 'x'))
            if n == 5:
                procs.append(run(self.project, 'reply', 'base-0', '--by', 'developer'))
            if n == 9:
                procs.append(run(self.project, 'cancel', 'base-1', '--by', 'manager', '--reason', 'test'))
        codes = [p.wait() for p in procs]
        self.assertEqual(codes, [0] * len(procs), [p.stderr.read() for p in procs if p.returncode])
        rows = {r['id']: r for r in rq.load(self.project)['requests']}
        self.assertEqual(len(rows), 27)
        self.assertEqual(rows['base-0']['state'], 'answered')
        self.assertEqual(rows['base-1']['state'], 'cancelled')
        self.assertEqual(rows['base-2']['state'], 'open')

    def test_terminal_reply_and_cancel_are_idempotent_and_never_reopen(self):
        data = {'schema': 1, 'requests': []}
        rq.open_request(data, 'A', 'manager', 'developer', 's')
        rq.open_request(data, 'B', 'manager', 'developer', 's')
        rq.reply(data, 'A', 'developer', 'https://x/1')
        first = dict(rq._find(data, 'A'))
        rq.reply(data, 'A', 'developer', 'https://x/2')
        self.assertEqual(rq._find(data, 'A'), first)
        rq.cancel(data, 'B', 'manager', 'why')
        rq.cancel(data, 'B', 'manager', 'other reason')
        self.assertEqual(rq._find(data, 'B')['cancelled_reason'], 'why')
        with self.assertRaises(rq.RequestError):
            rq.reply(data, 'B', 'developer')

    def test_stale_save_is_a_conflict_not_a_silent_overwrite(self):
        data = rq.update(self.project, lambda d: rq.open_request(d, 'A', 'manager', 'developer', 's'))
        stale = rq.load(self.project)
        expected = rq.version(stale)
        rq.update(self.project, lambda d: rq.reply(d, 'A', 'developer'))
        with self.assertRaises(rq.RequestError):
            rq.save(self.project, stale, expected)
        self.assertEqual(rq.load(self.project)['requests'][0]['state'], 'answered')

    def test_linked_worktree_uses_the_main_checkout_ledger(self):
        main = Path(self.temp.name) / 'main'
        (main / '.git' / 'worktrees' / 'wt').mkdir(parents=True)
        (main / '.git' / 'worktrees' / 'wt' / 'commondir').write_text('../..', encoding='utf-8')
        rq.update(main, lambda d: rq.open_request(d, 'A', 'manager', 'developer', 's'))
        tree = Path(self.temp.name) / 'tree'
        tree.mkdir()
        (tree / '.git').write_text('gitdir: ' + str(main / '.git' / 'worktrees' / 'wt'), encoding='utf-8')
        self.assertEqual(rq.path(tree), main.resolve() / '.bot-mode' / 'local' / 'requests.json')
        self.assertEqual([r['id'] for r in rq.load(tree)['requests']], ['A'])
        rq.update(tree, lambda d: rq.reply(d, 'A', 'developer'))
        self.assertEqual(rq.load(main)['requests'][0]['state'], 'answered')
        self.assertFalse((tree / '.bot-mode').exists())


def git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                   env={**__import__('os').environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                        'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'})


class RealWorktreeTests(unittest.TestCase):
    """Real git worktrees (no hand-made .git files), including a relative gitdir and a legacy main ledger."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.main = Path(self.temp.name) / 'main'
        self.main.mkdir()
        git(self.main, 'init', '-q', '-b', 'main')
        (self.main / 'f.txt').write_text('x', encoding='utf-8')
        git(self.main, 'add', '.')
        git(self.main, 'commit', '-q', '-m', 'init')
        self.tree = Path(self.temp.name) / 'tree'
        git(self.main, 'worktree', 'add', '-q', str(self.tree))

    def make_relative(self):
        """A worktree made by git itself with a gitdir relative to the worktree root."""
        self.tree = Path(self.temp.name) / 'rel-tree'
        git(self.main, 'worktree', 'add', '-q', '--relative-paths', str(self.tree))
        recorded = (self.tree / '.git').read_text(encoding='utf-8').split(':', 1)[1].strip()
        self.assertFalse(Path(recorded).is_absolute(), recorded)

    def test_relative_gitdir_resolves_from_the_worktree_root_not_the_process_cwd(self):
        rq.update(self.main, lambda d: rq.open_request(d, 'A', 'manager', 'developer', 's'))
        self.make_relative()
        old = __import__('os').getcwd()
        __import__('os').chdir(tempfile.gettempdir())  # a different working directory
        try:
            self.assertEqual([r['id'] for r in rq.load(self.tree)['requests']], ['A'])
            rq.update(self.tree, lambda d: rq.reply(d, 'A', 'developer'))
        finally:
            __import__('os').chdir(old)
        self.assertEqual(rq.load(self.main)['requests'][0]['state'], 'answered')
        self.assertFalse((self.tree / '.bot-mode').exists())

    def test_main_legacy_ledger_is_found_from_a_worktree(self):
        legacy = self.main / '.bot-mode' / 'requests.json'
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({'schema': 1, 'requests': [
            {'id': 'L', 'from': 'manager', 'to': 'developer', 'summary': 's', 'link': '', 'opened': rq.now().isoformat(),
             'due': rq.now().isoformat(), 'state': 'open', 'escalations': []}]}), encoding='utf-8')
        self.assertEqual([r['id'] for r in rq.load(self.tree)['requests']], ['L'])

    def test_a_worktree_with_its_own_ledger_keeps_it(self):
        own = self.tree / '.bot-mode' / 'local' / 'requests.json'
        own.parent.mkdir(parents=True)
        own.write_text(json.dumps({'schema': 1, 'requests': []}), encoding='utf-8')
        rq.update(self.main, lambda d: rq.open_request(d, 'M', 'manager', 'developer', 's'))
        rq.update(self.tree, lambda d: rq.open_request(d, 'T', 'manager', 'developer', 's'))
        self.assertEqual([r['id'] for r in rq.load(self.tree)['requests']], ['T'])
        self.assertEqual([r['id'] for r in rq.load(self.main)['requests']], ['M'])


if __name__ == '__main__':
    unittest.main()
