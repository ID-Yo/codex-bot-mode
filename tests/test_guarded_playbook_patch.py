"""Guarded PLAYBOOK patch (#343): drift, additions-only and rollback, on disposable copies only."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import guarded_playbook_patch as gp  # noqa: E402

EOLS = {'manager': 'CRLF', 'developer': 'CRLF', 'development-developer': 'LF', 'code-reviewer': 'LF',
        'development-code-reviewer': 'LF', 'verifier': 'CRLF'}
ADDED = ['## Stage handoff', '', '- Accepted rule one.', '- Accepted rule two.']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def enc(lines, eol):
    return ('\r\n' if eol == 'CRLF' else '\n').join(lines).encode('utf-8')


class Disposable(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'project'
        self.proposed = Path(self._tmp.name) / 'proposed'
        self.proposed.mkdir()
        self.before, self.after, targets = {}, {}, []
        self.signed = {}
        for bot, (rel, cid) in gp.ALLOWED.items():
            eol = EOLS[bot]
            old = ['# Playbook ' + bot, '', 'Custom installed line for ' + bot + '.', '', '## Done means', 'Verified.', '']
            new = old[:4] + ADDED + [''] + old[4:]
            path = self.root / rel
            path.parent.mkdir(parents=True)
            path.write_bytes(enc(old, eol))
            (self.proposed / (bot + '.PLAYBOOK.proposed.md')).write_bytes(enc(new, eol))
            self.before[bot], self.after[bot] = sha(enc(old, eol)), sha(enc(new, eol))
            self.signed[cid] = '\n'.join(['# Catalog'] + ADDED + ['end'])
            targets.append({'bot': bot, 'path': rel, 'eol': eol, 'before_sha256': self.before[bot], 'after_sha256': self.after[bot]})
        self.plan = {'schema': 1, 'release_id': 'test-release', 'targets': targets}
        self.snapshot = self.files()

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def target(self, bot):
        return self.root / gp.ALLOWED[bot][0]

    def apply(self):
        return gp.apply(self.root, self.plan, self.proposed, self.signed)


class ApplyAndRollback(Disposable):
    def test_happy_path_writes_only_the_six_files_and_keeps_backups(self):
        txn = self.apply()
        for bot in gp.ALLOWED:
            self.assertEqual(sha(self.target(bot).read_bytes()), self.after[bot])
        changed = {k for k, v in self.files().items() if self.snapshot.get(k) != v}
        allowed = {gp.ALLOWED[b][0].replace('/', '\\') for b in gp.ALLOWED} | {gp.ALLOWED[b][0] for b in gp.ALLOWED}
        state = {k for k in changed if k.replace('\\', '/').startswith(gp.STATE_DIR)}
        self.assertEqual(changed - state - allowed, set())
        folder = self.root / gp.STATE_DIR / txn
        journal = json.loads((folder / 'journal.json').read_text())
        self.assertEqual(journal['status'], 'applied')
        self.assertEqual([t['state'] for t in journal['targets']], ['replaced'] * 6)
        for bot in gp.ALLOWED:
            self.assertEqual(sha((folder / (bot + '.PLAYBOOK.before.md')).read_bytes()), self.before[bot])
        self.assertFalse((self.root / gp.STATE_DIR / '.lock').exists())
        self.assertEqual([p.name for p in self.root.rglob('*.tmp')], [])

    def test_rollback_restores_every_file_byte_for_byte(self):
        txn = self.apply()
        gp.rollback(self.root, txn)
        for k, v in self.snapshot.items():
            self.assertEqual((self.root / k).read_bytes(), v)
        self.assertEqual(json.loads((self.root / gp.STATE_DIR / txn / 'journal.json').read_text())['status'], 'rolled_back')

    def test_rollback_leaves_a_target_edited_after_apply(self):
        txn = self.apply()
        edited = self.target('manager')
        edited.write_bytes(edited.read_bytes() + b'owner edit\r\n')
        with self.assertRaises(gp.PatchError):
            gp.rollback(self.root, txn)
        self.assertTrue(edited.read_bytes().endswith(b'owner edit\r\n'))
        for bot in gp.ALLOWED:
            if bot != 'manager':
                self.assertEqual(sha(self.target(bot).read_bytes()), self.before[bot])

    def test_failure_in_the_middle_restores_the_earlier_targets(self):
        calls = []

        def failing(src, dst):
            calls.append(dst)
            if len(calls) == 4:
                raise OSError('simulated disk failure')
            return __import__('os').replace(src, dst)
        with mock.patch.object(gp, 'REPLACE', failing):
            with self.assertRaises(gp.PatchError):
                self.apply()
        self.assertEqual(self.files().keys() - {k for k in self.files() if gp.STATE_DIR in k.replace('\\', '/')}, self.snapshot.keys())
        for k, v in self.snapshot.items():
            self.assertEqual((self.root / k).read_bytes(), v)
        journals = list((self.root / gp.STATE_DIR).glob('*/journal.json'))
        self.assertEqual(json.loads(journals[0].read_text())['status'], 'rolled_back')


class Refusals(Disposable):
    def assertRefusedWithoutWrite(self, text):
        with self.assertRaises(gp.PatchError) as raised:
            self.apply()
        self.assertIn(text, str(raised.exception))
        self.assertEqual(self.files(), self.snapshot)
        self.assertFalse((self.root / gp.STATE_DIR).exists())

    def test_drift_in_one_target_blocks_all_six(self):
        path = self.target('verifier')
        path.write_bytes(path.read_bytes() + b'drift\r\n')
        self.snapshot = self.files()
        self.assertRefusedWithoutWrite('verifier: drift')

    def test_proposed_file_that_rewrites_a_line_is_refused(self):
        file = self.proposed / 'developer.PLAYBOOK.proposed.md'
        data = file.read_bytes().replace(b'Verified.', b'Changed.')
        file.write_bytes(data)
        for t in self.plan['targets']:
            if t['bot'] == 'developer':
                t['after_sha256'] = sha(data)
        self.assertRefusedWithoutWrite('deletes or rewrites')

    def test_proposed_file_that_drops_a_line_is_refused(self):
        file = self.proposed / 'code-reviewer.PLAYBOOK.proposed.md'
        data = file.read_bytes().replace(b'Verified.\n', b'')
        file.write_bytes(data)
        for t in self.plan['targets']:
            if t['bot'] == 'code-reviewer':
                t['after_sha256'] = sha(data)
        self.assertRefusedWithoutWrite('deletes or rewrites')

    def test_inserted_text_missing_from_the_signed_release_is_refused(self):
        self.signed['manager-profile'] = '# Catalog without the accepted lines'
        self.assertRefusedWithoutWrite('not in the signed public release')

    def test_proposed_hash_mismatch_is_refused(self):
        self.plan['targets'][0]['after_sha256'] = '0' * 64
        self.assertRefusedWithoutWrite('planned after hash')

    def test_line_ending_mismatch_is_refused(self):
        self.plan['targets'][0]['eol'] = 'LF'
        self.assertRefusedWithoutWrite('line endings')

    def test_plan_with_another_path_or_a_missing_target_is_rejected(self):
        for mutate in (lambda p: p['targets'][0].__setitem__('path', 'agents/project-manager/STATE.md'),
                       lambda p: p['targets'].pop(),
                       lambda p: p['targets'][0].__setitem__('bot', 'someone-else')):
            plan = json.loads(json.dumps(self.plan))
            mutate(plan)
            file = Path(self._tmp.name) / 'plan.json'
            file.write_text(json.dumps(plan))
            with self.assertRaises(gp.PatchError):
                gp.load_plan(file)

    def test_an_unchanged_proposal_is_refused(self):
        for bot in gp.ALLOWED:
            data = self.target(bot).read_bytes()
            (self.proposed / (bot + '.PLAYBOOK.proposed.md')).write_bytes(data)
            for t in self.plan['targets']:
                if t['bot'] == bot:
                    t['after_sha256'] = sha(data)
        self.assertRefusedWithoutWrite('inserted line count')


def make_link(link, target):
    """Directory symlink, or a junction on Windows without the symlink privilege; None when neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == 'nt':
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
        return done.returncode == 0
    return False


def drop_link(link):
    try:
        os.unlink(link)
    except (IsADirectoryError, PermissionError, OSError):
        os.rmdir(link)


class Containment(Disposable):
    def test_rollback_ignores_a_journal_path_outside_the_allowlist(self):
        txn = self.apply()
        outside = Path(self._tmp.name) / 'outside.txt'
        outside.write_bytes(b'must stay')
        journal_file = self.root / gp.STATE_DIR / txn / 'journal.json'
        for field, value in (('path', '../../outside.txt'), ('path', 'agents/project-manager/STATE.md'),
                             ('backup', '../../../outside.txt'), ('bot', 'someone-else')):
            journal = json.loads(journal_file.read_text())
            journal['targets'][0][field] = value
            journal_file.write_text(json.dumps(journal))
            applied = self.files()
            with self.assertRaises(gp.PatchError):
                gp.rollback(self.root, txn)
            self.assertEqual(outside.read_bytes(), b'must stay')
            self.assertEqual(self.files().keys(), applied.keys())
            for bot in gp.ALLOWED:
                self.assertEqual(sha(self.target(bot).read_bytes()), self.after[bot])

    def test_apply_refuses_a_linked_state_folder_and_writes_nothing_outside(self):
        for linked in (STATE_PARTS[:2], STATE_PARTS):
            elsewhere = Path(self._tmp.name) / ('elsewhere-' + str(len(linked)))
            elsewhere.mkdir()
            parent = self.root.joinpath(*linked[:-1])
            parent.mkdir(parents=True, exist_ok=True)
            link = parent / linked[-1]
            if not make_link(link, elsewhere):
                self.skipTest('cannot create a link or junction here')
            try:
                with self.assertRaises(gp.PatchError) as raised:
                    self.apply()
                self.assertIn('link or junction', str(raised.exception))
                self.assertEqual(list(elsewhere.iterdir()), [])
                for bot in gp.ALLOWED:
                    self.assertEqual(sha(self.target(bot).read_bytes()), self.before[bot])
                with self.assertRaises(gp.PatchError):
                    gp.rollback(self.root, '20260101T000000Z-abcdef')
                self.assertEqual(list(elsewhere.iterdir()), [])
            finally:
                drop_link(link)


STATE_PARTS = gp.STATE_DIR.split('/')


class Locking(Disposable):
    def lock(self, age=0):
        base = self.root / gp.STATE_DIR
        base.mkdir(parents=True, exist_ok=True)
        path = base / '.lock'
        path.write_text('999999')
        if age:
            old = path.stat().st_mtime - age
            os.utime(path, (old, old))
        return path

    def test_a_fresh_lock_blocks_apply_and_rollback(self):
        txn = self.apply()
        path = self.lock()
        with self.assertRaises(gp.PatchError) as raised:
            gp.rollback(self.root, txn)
        self.assertIn('lock', str(raised.exception))
        for bot in gp.ALLOWED:
            self.assertEqual(sha(self.target(bot).read_bytes()), self.after[bot])
        with self.assertRaises(gp.PatchError):
            self.apply()
        self.assertTrue(path.exists())

    def test_a_stale_lock_is_replaced_and_released(self):
        txn = self.apply()
        path = self.lock(age=gp.STALE_LOCK_SECONDS + 60)
        gp.rollback(self.root, txn)
        for k, v in self.snapshot.items():
            self.assertEqual((self.root / k).read_bytes(), v)
        self.assertFalse(path.exists())


class CommandLine(Disposable):
    def run_cli(self, *extra):
        plan = Path(self._tmp.name) / 'plan.json'
        plan.write_text(json.dumps(self.plan))
        args = ['--project', str(self.root), '--plan', str(plan), '--proposed', str(self.proposed), '--manifest', 'm', '--bundle', 'b', '--trust', 't']
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gp, 'signed_sources', return_value=self.signed), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gp.main(list(extra[:1]) + args + list(extra[1:]))
        return code, out.getvalue(), err.getvalue()

    def test_check_never_writes_and_apply_needs_confirm(self):
        code, out, _ = self.run_cli('check')
        self.assertEqual((code, self.files()), (0, self.snapshot))
        self.assertIn('nothing was written', out)
        code, _, err = self.run_cli('apply')
        self.assertEqual((code, self.files()), (2, self.snapshot))
        self.assertIn('--confirm', err)
        code, out, _ = self.run_cli('apply', '--confirm')
        self.assertEqual(code, 0)
        txn = json.loads(out)['applied']
        code, _, _ = gp.main(['rollback', '--project', str(self.root), '--transaction', txn]), 0, 0
        self.assertEqual(code, 0)
        for k, v in self.snapshot.items():
            self.assertEqual((self.root / k).read_bytes(), v)


class ManagerLineReplacements(Disposable):
    """Two hardcoded whole-line Manager rewrites (#364); everything else stays insert-only."""

    def setUp(self):
        super().setUp()
        (o1, n1), (o2, n2) = gp.MANAGER_REPLACEMENTS
        self.o1, self.n1, self.o2, self.n2 = o1, n1, o2, n2
        self.old = ['# Playbook manager', '', 'Custom installed line.', o1, o2, '', '## Done means', 'Verified.', '']
        self.new = ['# Playbook manager', '', 'Custom installed line.', n1, n2] + ADDED + ['', '## Done means', 'Verified.', '']
        self.signed['manager-profile'] = '\n'.join(['# Catalog', n1, n2] + ADDED + ['end'])
        self.write(self.old, self.new)

    def write(self, old, new):
        self.target('manager').write_bytes(enc(old, 'CRLF'))
        (self.proposed / 'manager.PLAYBOOK.proposed.md').write_bytes(enc(new, 'CRLF'))
        item = next(t for t in self.plan['targets'] if t['bot'] == 'manager')
        item['before_sha256'], item['after_sha256'] = sha(enc(old, 'CRLF')), sha(enc(new, 'CRLF'))

    def refused(self, text=''):
        with self.assertRaises(gp.PatchError) as ctx:
            gp.preflight(self.root, self.plan, self.proposed, self.signed)
        self.assertIn(text, str(ctx.exception))

    def test_the_two_listed_pairs_are_accepted(self):
        checked = gp.preflight(self.root, self.plan, self.proposed, self.signed)
        self.assertEqual(next(c for c in checked if c['bot'] == 'manager')['added'], len(ADDED))

    def test_apply_and_rollback_round_trip(self):
        txn = self.apply()
        self.assertEqual(self.target('manager').read_bytes(), enc(self.new, 'CRLF'))
        gp.rollback(self.root, txn)
        self.assertEqual(self.target('manager').read_bytes(), enc(self.old, 'CRLF'))

    def test_an_arbitrary_replacement_is_rejected(self):
        self.write(self.old, [l if l != 'Custom installed line.' else 'Rewritten line.' for l in self.new])
        self.signed['manager-profile'] += '\nRewritten line.'
        self.refused('deletes or rewrites')

    def test_a_listed_old_line_replaced_by_text_not_in_the_signed_source_is_rejected(self):
        self.signed['manager-profile'] = self.signed['manager-profile'].replace(self.n1, 'other')
        self.refused('not in the signed public release')

    def test_an_ambiguous_old_line_is_rejected(self):
        old = self.old + [self.o1]
        self.write(old, self.new + [self.n1])
        self.refused('ambiguous')

    def test_a_listed_line_in_a_non_manager_file_is_rejected(self):
        bot = 'developer'
        old = ['# Playbook', self.o1, '']
        new = ['# Playbook', self.n1] + ADDED + ['']
        self.target(bot).write_bytes(enc(old, 'CRLF'))
        (self.proposed / (bot + '.PLAYBOOK.proposed.md')).write_bytes(enc(new, 'CRLF'))
        item = next(t for t in self.plan['targets'] if t['bot'] == bot)
        item['before_sha256'], item['after_sha256'] = sha(enc(old, 'CRLF')), sha(enc(new, 'CRLF'))
        self.signed['developer-profile'] = '\n'.join([self.n1] + ADDED)
        with self.assertRaises(gp.PatchError) as ctx:
            gp.preflight(self.root, self.plan, self.proposed, self.signed)
        self.assertIn('developer: the change deletes or rewrites', str(ctx.exception))

    def test_the_pair_list_has_exactly_two_whole_lines(self):
        self.assertEqual(len(gp.MANAGER_REPLACEMENTS), 2)
        for old, new in gp.MANAGER_REPLACEMENTS:
            self.assertNotIn('\n', old + new)
            self.assertNotEqual(old, new)


class InsertedBlockWholeLines(Disposable):
    """An inserted block must be a contiguous run of whole signed lines, not a fragment of one (#395)."""

    def insert(self, inserted, signed_text, bot='developer'):
        old = ['# Playbook ' + bot, '', 'Custom installed line.', '']
        new = old[:3] + inserted + old[3:]
        eol = EOLS[bot]
        self.target(bot).write_bytes(enc(old, eol))
        (self.proposed / (bot + '.PLAYBOOK.proposed.md')).write_bytes(enc(new, eol))
        item = next(t for t in self.plan['targets'] if t['bot'] == bot)
        item['before_sha256'], item['after_sha256'] = sha(enc(old, eol)), sha(enc(new, eol))
        self.signed[gp.ALLOWED[bot][1]] = signed_text

    def passes(self, inserted, signed_text):
        self.insert(inserted, signed_text)
        checked = gp.preflight(self.root, self.plan, self.proposed, self.signed)
        self.assertEqual(next(c for c in checked if c['bot'] == 'developer')['added'], len(inserted))

    def refused(self, inserted, signed_text):
        self.insert(inserted, signed_text)
        with self.assertRaises(gp.PatchError) as raised:
            gp.preflight(self.root, self.plan, self.proposed, self.signed)
        self.assertIn('developer: an inserted block is not in the signed public release', str(raised.exception))

    def test_a_fragment_of_a_signed_line_is_refused(self):
        self.refused(['def'], 'abc def')
        self.refused(['abc'], 'abc def')
        self.refused(['push to main'], 'Never push to main; open a pull request.')

    def test_a_whole_signed_line_is_accepted(self):
        self.passes(['def'], 'abc\ndef\nghi')
        self.passes(['abc def'], '# Catalog\nabc def\nend')

    def test_a_contiguous_run_of_whole_signed_lines_is_accepted(self):
        self.passes(['b', 'c'], 'a\nb\nc\nd')
        self.passes(['a', '', 'b'], 'x\na\n\nb\ny')

    def test_empty_separator_lines_at_the_edges_of_a_block_do_not_need_to_border_the_signed_run(self):
        self.passes(['', 'def'], 'abc\ndef\nghi')
        self.passes(['def', ''], 'abc\ndef\nghi')
        self.refused(['', 'de', ''], 'abc\ndef\nghi')

    def test_a_block_that_is_not_one_contiguous_run_is_refused(self):
        self.refused(['a', 'c'], 'a\nb\nc')
        self.refused(['c', 'b'], 'a\nb\nc')
        self.refused(['b', 'c'], 'a\nb\nc d')
        self.refused(['b', 'x'], 'a\nb\nc')

    def test_a_line_that_only_contains_the_inserted_text_is_refused_in_a_multiline_block(self):
        self.refused(['abc', 'de'], 'abc\ndef')

    def test_a_fragment_is_refused_by_apply_without_writing(self):
        self.insert(['def'], 'abc def')
        self.snapshot = self.files()
        with self.assertRaises(gp.PatchError):
            self.apply()
        self.assertEqual(self.files(), self.snapshot)
        self.assertFalse((self.root / gp.STATE_DIR).exists())


if __name__ == '__main__':
    unittest.main()

