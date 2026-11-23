"""The doctor tells a new user what is wrong with the outbox setup and repairs the safe parts (#423, epic #420)."""
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('doctor423', ROOT / 'scripts/doctor.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
OUTBOX = d.outbox_module()

GOOD_TASK = {'state': 'Ready', 'battery': True, 'hidden': True, 'multi': 'IgnoreNew',
             'actions': [{'execute': 'pythonw.exe',
                          'arguments': '-B "%s" --office-root "%s" --all-projects --restart' % (ROOT / 'scripts/relay_watchdog.py', ROOT)}],
             'triggers': [{'enabled': True, 'repeat': ''}, {'enabled': True, 'repeat': 'PT2M'}]}


def good_task(**change):
    state = json.loads(json.dumps(GOOD_TASK))
    state.update(change)
    return state


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FakeWatchdog:
    def __init__(self, manager='11111111-2222-7333-8444-555555555555'):
        self.manager = manager
        self.restarted = []
    def current_manager(self, root): return self.manager
    def relay_ok(self, root): return False
    def restart_relay(self, root):
        self.restarted.append(root); return True, 'started'
    def outbox_status(self, project): raise AssertionError('the doctor must not run the project outbox status (it heals)')


class FakeOutbox:
    """Stands in for the outbox module: the effective relay descriptor of a project and whether its port answers."""
    def __init__(self, descriptor=True, reachable=True):
        self.descriptor, self.reachable = descriptor, reachable
    def find_relay_descriptor(self, project):
        return (Path(project) / 'relay', {'url': 'http://127.0.0.1:1', 'token': 't'}) if self.descriptor else (None, None)
    def relay_reachable(self, project): return self.reachable


class PythonVersionTests(unittest.TestCase):
    def test_python_3_11_fails_and_3_12_passes(self):
        self.assertEqual(d.check_python((3, 11, 9))['status'], 'fail')
        self.assertIn('3.12', d.check_python((3, 11, 9))['fix'])
        self.assertEqual(d.check_python((3, 12, 0))['status'], 'ok')
        self.assertEqual(d.check_python((3, 13, 2))['status'], 'ok')


class RepeatTests(unittest.TestCase):
    def test_repeat_minutes(self):
        self.assertEqual(d.repeat_minutes('PT2M'), 2)
        self.assertEqual(d.repeat_minutes('PT1H'), 60)
        self.assertEqual(d.repeat_minutes('PT10M,PT2M'), 2)
        self.assertIsNone(d.repeat_minutes(''))
        self.assertIsNone(d.repeat_minutes(',,'))


class SupervisorTaskTests(unittest.TestCase):
    def evaluate(self, state, office=ROOT):
        return d.evaluate_task(state, office)

    def test_missing_task_fails_with_the_installer_command(self):
        result = self.evaluate(None)
        self.assertEqual(result['status'], 'fail')
        self.assertIn('supervisor_task.ps1', result['fix'])

    def test_good_task_is_ok_and_the_equals_form_of_office_root_too(self):
        self.assertEqual(self.evaluate(good_task())['status'], 'ok')
        state = good_task()
        state['actions'][0]['arguments'] = '-B "%s" --office-root="%s" --restart' % (ROOT / 'scripts/relay_watchdog.py', ROOT)
        self.assertEqual(self.evaluate(state)['status'], 'ok')

    def test_each_bad_setting_is_named(self):
        other = ROOT.parent / 'other-checkout'
        wrong = {'execute': 'pythonw.exe', 'arguments': '-B "%s" --office-root "%s"' % (other / 'scripts/relay_watchdog.py', other)}
        for change, word in (({'battery': False}, 'battery'), ({'hidden': False}, 'hidden'), ({'multi': 'Parallel'}, 'overlapping'),
                             ({'triggers': [{'enabled': True, 'repeat': 'PT30M'}]}, 'at most every 5'), ({'state': 'Disabled'}, 'disabled'),
                             ({'actions': [wrong]}, "relay_watchdog.py")):
            with self.subTest(word):
                result = self.evaluate(good_task(**change))
                self.assertEqual(result['status'], 'fail')
                self.assertIn(word, result['detail'])

    def test_a_sibling_checkout_that_shares_the_path_prefix_does_not_pass(self):
        sibling = Path(str(ROOT) + '-two')
        for script, root in ((sibling / 'scripts/relay_watchdog.py', ROOT), (ROOT / 'scripts/relay_watchdog.py', sibling)):
            state = good_task(actions=[{'execute': 'pythonw.exe', 'arguments': '-B "%s" --office-root "%s" --restart' % (script, root)}])
            self.assertEqual(self.evaluate(state)['status'], 'fail')

    def test_only_the_script_operand_counts(self):
        script = ROOT / 'scripts/relay_watchdog.py'
        for arguments in ('-m other "%s" --office-root "%s"' % (script, ROOT),            # -m: no script operand
                          '-B other.py "%s" --office-root "%s"' % (script, ROOT),         # the script only appears later, as data
                          '-B "%s"' % script,                                            # no --office-root at all
                          '-B "%s" --office-root' % script):                              # --office-root without a value
            with self.subTest(arguments):
                self.assertEqual(self.evaluate(good_task(actions=[{'execute': 'pythonw.exe', 'arguments': arguments}]))['status'], 'fail')
        not_python = good_task(actions=[{'execute': 'cmd.exe', 'arguments': '-B "%s" --office-root "%s"' % (script, ROOT)}])
        self.assertEqual(self.evaluate(not_python)['status'], 'fail')

    def test_a_disabled_quick_trigger_does_not_make_a_slow_schedule_pass(self):
        state = good_task(triggers=[{'enabled': False, 'repeat': 'PT1M'}, {'enabled': True, 'repeat': 'PT30M'}])
        self.assertEqual(self.evaluate(state)['status'], 'fail')
        state = good_task(triggers=[{'enabled': True, 'repeat': 'PT30M'}, {'enabled': True, 'repeat': 'PT3M'}])
        self.assertEqual(self.evaluate(state)['status'], 'ok')
        self.assertEqual(self.evaluate(good_task(triggers=[]))['status'], 'fail')

    def test_a_single_action_or_trigger_object_is_read_like_a_list(self):
        state = good_task(actions=good_task()['actions'][0], triggers={'enabled': True, 'repeat': 'PT2M'})
        self.assertEqual(self.evaluate(state)['status'], 'ok')

    def test_an_empty_or_malformed_scheduler_answer_is_an_error_not_a_crash(self):
        for out, error in (('', OSError), ('\n  \n', OSError), ('[1, 2]', ValueError), ('"text"', ValueError), ('not json', ValueError)):
            with self.subTest(out), mock.patch.object(d, 'run', return_value=(0, out)):
                with self.assertRaises(error):
                    d.task_state()
        with mock.patch.object(d, 'run', return_value=(0, 'null')):
            self.assertIsNone(d.task_state())

    @unittest.skipUnless(os.name == 'nt', 'Task Scheduler checks are Windows only')
    def test_an_unreadable_task_is_a_warning_not_a_crash(self):
        for failure in (OSError('denied'), ValueError('bad'), subprocess.TimeoutExpired('x', 1)):
            with self.subTest(type(failure).__name__), mock.patch.object(d, 'task_state', side_effect=failure):
                self.assertEqual(d.check_supervisor(ROOT)['status'], 'warn')
        with mock.patch.object(d, 'run', return_value=(0, '')):
            self.assertEqual(d.check_supervisor(ROOT)['status'], 'warn')


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.make_project('proj')
        self.relay = self.base / 'relay'
        self.relay.mkdir()
        (self.relay / 'relay.json').write_text('{}')

    def make_project(self, *parts):
        path = self.base.joinpath(*parts)
        (path / '.bot-mode').mkdir(parents=True)
        return path

    def install(self, project=None, same=True, config=True):
        project = project or self.project
        source = ROOT / 'scripts/team_room/outbox.py'
        (project / '.bot-mode/outbox.py').write_bytes(source.read_bytes() if same else b'# old copy')
        if config:
            (project / '.bot-mode/outbox-config.json').write_text(json.dumps({'relay_root': str(self.relay)}))

    def by_name(self, results):
        return {r['name'].split(': ', 1)[1]: r for r in results}

    def check(self, project=None, ob=None, wd=None):
        return d.check_project(project or self.project, ROOT, wd or FakeWatchdog(), ob or FakeOutbox())


class ProjectTests(ProjectCase):
    def test_missing_outbox_is_a_failure_keyed_by_the_full_project_path(self):
        results = self.check()
        self.assertEqual(results[0]['status'], 'fail')
        self.assertEqual((results[0]['code'], results[0]['project']), ('outbox_missing', str(self.project)))
        self.assertIn('doctor.py --fix', results[0]['fix'])

    def test_a_complete_install_is_ok(self):
        self.install()
        self.assertEqual({r['status'] for r in self.check()}, {'ok'})

    def test_a_stale_copy_a_missing_manager_and_queue_problems_are_warnings(self):
        self.install(same=False)
        db = OUTBOX.OutboxDB(self.project / '.bot-mode/outbox/queue.sqlite')
        try:
            for state in ('pending', 'ambiguous', 'needs-attention'):
                db.enqueue('dev', 'manager', None, state)
            with db.conn:
                db.conn.execute("UPDATE outbox SET state = text")
                db.conn.execute("UPDATE outbox SET created_at = '2020-01-01T00:00:00+00:00'")
        finally:
            db.close()
        named = self.by_name(self.check(wd=FakeWatchdog(manager=None)))
        self.assertEqual((named['outbox']['status'], named['outbox']['code']), ('warn', 'outbox_differs'))
        self.assertIn('never overwrites', named['outbox']['fix'])
        self.assertEqual(named['Manager chat']['status'], 'warn')
        self.assertEqual(named['queue']['status'], 'warn')
        for word in ('unconfirmed', 'need a decision', 'oldest pending'):
            self.assertIn(word, named['queue']['detail'])

    def test_a_missing_config_is_a_fixable_warning_and_an_invalid_one_is_a_manual_failure(self):
        self.install(config=False)
        named = self.by_name(self.check())
        self.assertEqual((named['outbox config']['status'], named['outbox config']['code']), ('warn', 'config_missing'))
        path = self.project / '.bot-mode/outbox-config.json'
        for text in ('{not json', '[]', '"x"', json.dumps({'relay_root': 5})):
            with self.subTest(text):
                path.write_text(text)
                result = self.by_name(self.check())['outbox config']
                self.assertEqual((result['status'], result['code']), ('fail', 'config_invalid'))
                self.assertIn('by hand', result['fix'])

    def test_the_queue_check_is_read_only_and_never_runs_the_project_cli(self):
        self.install()
        db = OUTBOX.OutboxDB(self.project / '.bot-mode/outbox/queue.sqlite')
        try:
            row = db.enqueue('dev', 'manager', None, 'x')
            db.mark_ambiguous(row, 'unknown')
            with db.conn:
                db.conn.execute("UPDATE outbox SET created_at = '2020-01-01T00:00:00+00:00'")
        finally:
            db.close()
        before = digest(self.project / '.bot-mode/outbox/queue.sqlite')
        with mock.patch.object(d, 'run', side_effect=AssertionError('no subprocess')), \
                mock.patch.object(subprocess, 'run', side_effect=AssertionError('no subprocess')):
            results = self.check()
        self.assertEqual(digest(self.project / '.bot-mode/outbox/queue.sqlite'), before)
        db = OUTBOX.OutboxDB(self.project / '.bot-mode/outbox/queue.sqlite')
        try:
            self.assertEqual(db.count_state('ambiguous'), 1)  # an old unconfirmed row is not promoted by a check
        finally:
            db.close()
        self.assertEqual(self.by_name(results)['queue']['status'], 'warn')

    def test_the_project_relay_route_is_checked_not_the_office_relay(self):
        self.install()
        no_descriptor = self.by_name(self.check(ob=FakeOutbox(descriptor=False)))['relay route']
        self.assertEqual(no_descriptor['status'], 'fail')
        unreachable = self.by_name(self.check(ob=FakeOutbox(reachable=False)))['relay route']
        self.assertEqual(unreachable['status'], 'fail')
        self.assertEqual(self.by_name(self.check())['relay route']['status'], 'ok')

    def test_an_empty_descriptor_is_not_a_usable_route(self):
        self.install()  # relay/relay.json holds {} and the config names that folder
        with mock.patch.object(OUTBOX, 'relay_reachable', return_value=True):
            result = d.check_route('proj', self.project, OUTBOX)
        self.assertEqual(result['status'], 'fail')
        self.assertIn('no usable relay.json', result['detail'])
        (self.relay / 'relay.json').write_text(json.dumps({'url': 'http://127.0.0.1:1', 'token': 't'}))
        with mock.patch.object(OUTBOX, 'relay_reachable', return_value=True):
            self.assertEqual(d.check_route('proj', self.project, OUTBOX)['status'], 'ok')
        (self.relay / 'relay.json').write_text(json.dumps({'url': 'http://example.com:1', 'token': 't'}))  # not loopback
        with mock.patch.object(OUTBOX, 'relay_reachable', return_value=True):
            self.assertEqual(d.check_route('proj', self.project, OUTBOX)['status'], 'fail')

    def test_a_missing_manager_is_reported_separately_from_a_working_route(self):
        self.install()
        named = self.by_name(self.check(wd=FakeWatchdog(manager=None)))
        self.assertEqual((named['relay route']['status'], named['Manager chat']['status']), ('ok', 'warn'))


class FixTests(ProjectCase):
    def test_fix_installs_task_restarts_relay_and_installs_the_outbox_only_where_it_is_missing(self):
        other = self.make_project('b', 'proj')
        self.install(other)
        results = [d.check('supervisor task', 'fail', 'x'), d.check('relay', 'fail', 'x')]
        results += self.check() + self.check(other)
        wd = FakeWatchdog()
        with mock.patch.object(d, 'run', return_value=(0, '')) as run:
            done = d.apply_fixes(results, ROOT, [str(self.project), str(other)], wd)
        self.assertEqual(wd.restarted, [ROOT / '.bot-mode' / 'desktop-relay'])
        self.assertTrue((self.project / '.bot-mode/outbox.py').is_file())
        self.assertEqual((self.project / '.bot-mode/outbox.py').read_bytes(), (ROOT / 'scripts/team_room/outbox.py').read_bytes())
        config = json.loads((self.project / '.bot-mode/outbox-config.json').read_text())
        self.assertEqual(config['relay_root'], (ROOT / '.bot-mode' / 'desktop-relay').as_posix())
        if os.name == 'nt':
            self.assertIn('supervisor_task.ps1', ' '.join(run.call_args.args[0]))
        self.assertTrue(any('installed outbox.py' in line for line in done))

    def test_a_project_with_the_same_folder_name_is_never_touched(self):
        first, second = self.make_project('a', 'proj'), self.make_project('b', 'proj')
        self.install(second, same=False)
        old = (second / '.bot-mode/outbox.py').read_bytes()
        config_before = (second / '.bot-mode/outbox-config.json').read_bytes()
        results = self.check(first) + self.check(second)  # first: missing; second: a stale copy, only a warning
        d.apply_fixes(results, ROOT, [str(first), str(second)], FakeWatchdog())
        self.assertTrue((first / '.bot-mode/outbox.py').is_file())
        self.assertEqual((second / '.bot-mode/outbox.py').read_bytes(), old)
        self.assertEqual((second / '.bot-mode/outbox-config.json').read_bytes(), config_before)

    def test_a_result_for_a_project_that_was_not_checked_is_ignored(self):
        results = self.check()
        d.apply_fixes(results, ROOT, [str(self.base / 'elsewhere')], FakeWatchdog())
        self.assertFalse((self.project / '.bot-mode/outbox.py').exists())

    def test_a_copy_that_appeared_after_the_check_is_kept(self):
        results = self.check()
        (self.project / '.bot-mode/outbox.py').write_bytes(b'# a newer hand-made copy')
        done = d.apply_fixes(results, ROOT, [str(self.project)], FakeWatchdog())
        self.assertEqual((self.project / '.bot-mode/outbox.py').read_bytes(), b'# a newer hand-made copy')
        self.assertEqual(done, [self.project.name + ': installed outbox-config.json'])  # only what was still missing

    def test_a_missing_config_is_created_and_an_invalid_one_is_left_alone(self):
        self.install(config=False)
        d.apply_fixes(self.check(), ROOT, [str(self.project)], FakeWatchdog())
        self.assertTrue((self.project / '.bot-mode/outbox-config.json').is_file())
        config = self.project / '.bot-mode/outbox-config.json'
        config.write_text('{broken')
        results = self.check()
        d.apply_fixes(results, ROOT, [str(self.project)], FakeWatchdog())
        self.assertEqual(config.read_text(), '{broken')

    def test_install_outbox_keeps_an_existing_config(self):
        (self.project / '.bot-mode/outbox-config.json').write_text('{"relay_root": "keep-me"}')
        made = d.install_outbox(self.project, ROOT)
        self.assertEqual(made, ['outbox.py'])
        self.assertEqual((self.project / '.bot-mode/outbox-config.json').read_text(), '{"relay_root": "keep-me"}')

    def test_nothing_to_fix_does_nothing(self):
        wd = FakeWatchdog()
        with mock.patch.object(d, 'run') as run:
            self.assertEqual(d.apply_fixes([d.check('relay', 'ok', 'x')], ROOT, ['C:/p'], wd), [])
        run.assert_not_called()


class CanaryTests(ProjectCase):
    """The canary runs the real outbox.py command line parser in process; only the pump start and the relay are replaced."""

    def cli(self, states=None, drop_receipt=False, list_code=0, list_text=None, send_error=None):
        """A replacement for d.run that answers like the outbox CLI. states: the state the canary row takes on each list."""
        states = list(states or ['delivered'])
        project = str(self.project)

        def call(argv):
            out = io.StringIO()
            with redirect_stdout(out), mock.patch.object(OUTBOX, 'spawn_background_pump'), \
                    mock.patch.object(OUTBOX, 'relay_reachable', return_value=True), mock.patch('sys.stderr', io.StringIO()):
                try:
                    code = OUTBOX.main(['--project', project] + argv)
                except SystemExit as exit_:
                    code = exit_.code
            return code, out.getvalue()

        def run(command, timeout=60):
            argv = command[command.index(project) + 1:]
            if 'send' in argv:
                if send_error:
                    raise send_error
                self.sent = argv
                code, out = call(argv)
                return code, ('' if drop_receipt else out)
            if list_text is not None or list_code:
                return list_code, list_text or ''
            row = OUTBOX.OutboxDB(self.project / '.bot-mode/outbox/queue.sqlite')
            try:
                with row.conn:
                    row.conn.execute('UPDATE outbox SET state = ?', (states.pop(0) if len(states) > 1 else states[0],))
            finally:
                row.close()
            return call(argv)
        return run

    def canary(self, runner, wait=0.05):
        (self.project / '.bot-mode/outbox.py').write_text('#')
        with mock.patch.object(d, 'run', runner), mock.patch.object(d.time, 'sleep'):
            return d.canary(self.project, ROOT, wait=wait)

    def test_the_command_line_is_accepted_by_the_real_outbox_parser_and_the_row_is_delivered(self):
        result = self.canary(self.cli(['pending', 'delivered']), wait=5)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('--by', self.sent)
        self.assertNotIn('--from', self.sent)
        self.assertEqual(self.sent[self.sent.index('--by') + 1], 'doctor')

    def test_the_row_must_be_the_one_that_was_queued(self):
        runner = self.cli(['pending'])
        other = OUTBOX.OutboxDB(self.project / '.bot-mode/outbox/queue.sqlite')
        try:
            other_id = other.enqueue('x', 'manager', None, 'someone else')
            other.mark_delivered(other_id)
        finally:
            other.close()
        result = self.canary(runner)
        self.assertEqual(result['status'], 'fail')
        self.assertIn('not delivered', result['detail'])

    def test_failed_expired_and_skipped_rows_fail_and_unconfirmed_ones_are_unknown(self):
        for state in ('failed', 'expired', 'skipped'):
            with self.subTest(state):
                self.assertEqual(self.canary(self.cli([state]))['status'], 'fail')
        for state in ('ambiguous', 'needs-attention'):
            with self.subTest(state):
                result = self.canary(self.cli([state]))
                self.assertEqual(result['status'], 'warn')
                self.assertIn('unknown', result['detail'])

    def test_a_missing_receipt_or_a_send_timeout_is_unknown_not_a_failure(self):
        for runner in (self.cli(drop_receipt=True), self.cli(send_error=subprocess.TimeoutExpired('send', 60))):
            result = self.canary(runner)
            self.assertEqual(result['status'], 'warn')
            self.assertIn('unknown', result['detail'])

    def test_a_queue_that_cannot_be_listed_is_unknown(self):
        for runner in (self.cli(list_code=1, list_text='boom'), self.cli(list_text='not json')):
            result = self.canary(runner)
            self.assertEqual(result['status'], 'warn')
            self.assertIn('could not be read', result['detail'])

    def test_a_send_that_exits_non_zero_fails(self):
        (self.project / '.bot-mode/outbox.py').write_text('#')
        with mock.patch.object(d, 'run', return_value=(2, 'refused')):
            self.assertEqual(d.canary(self.project, ROOT, wait=0.01)['status'], 'fail')

    def test_canary_needs_an_explicit_project_or_an_explicit_opt_in(self):
        wd = FakeWatchdog()
        wd.team_room = lambda: (SimpleNamespace(), None)
        wd.registered_projects = lambda office: [str(self.project)]
        wd.projects_missing_outbox = lambda office: []
        with mock.patch.object(d, 'watchdog', return_value=wd), mock.patch.object(d, 'collect', return_value=[]), \
                mock.patch.object(d, 'canary', return_value=d.check('c', 'ok', 'y')) as canary, \
                mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(d.main(['--office-root', str(ROOT), '--canary', '--json']), 2)
            canary.assert_not_called()
            self.assertIn('--project', err.getvalue())
        with mock.patch.object(d, 'watchdog', return_value=wd), mock.patch.object(d, 'collect', return_value=[]), \
                mock.patch.object(d, 'canary', return_value=d.check('c', 'ok', 'y')) as canary, redirect_stdout(io.StringIO()):
            d.main(['--office-root', str(ROOT), '--project', str(self.project), '--canary', '--json'])
            self.assertEqual([c.args[0] for c in canary.call_args_list], [str(self.project)])
        with mock.patch.object(d, 'watchdog', return_value=wd), mock.patch.object(d, 'collect', return_value=[]), \
                mock.patch.object(d, 'canary', return_value=d.check('c', 'ok', 'y')) as canary, redirect_stdout(io.StringIO()):
            d.main(['--office-root', str(ROOT), '--canary-all-projects', '--json'])
            self.assertEqual([c.args[0] for c in canary.call_args_list], [str(self.project)])


class OutputTests(unittest.TestCase):
    def test_render_shows_the_fix_only_for_problems(self):
        text = d.render([d.check('a', 'ok', 'fine', 'ignored'), d.check('b', 'fail', 'bad', 'do this')])
        self.assertIn('[FAIL] b: bad', text)
        self.assertIn('fix: do this', text)
        self.assertNotIn('ignored', text)

    def test_exit_code_is_one_only_when_a_check_failed(self):
        wd = FakeWatchdog()
        wd.team_room = lambda: (SimpleNamespace(), None)
        wd.registered_projects = lambda office: []
        wd.projects_missing_outbox = lambda office: []
        for status, code in (('warn', 0), ('fail', 1)):
            with self.subTest(status), mock.patch.object(d, 'watchdog', return_value=wd), \
                    mock.patch.object(d, 'collect', return_value=[d.check('x', status, 'y')]):
                self.assertEqual(d.main(['--office-root', str(ROOT), '--json']), code)


class ExportTests(unittest.TestCase):
    """The public export carries every file its own tests need: the exported doctor and supervisor tests are imported in a
    clean directory that holds only the scripts and tests of the export manifest (the dependency tests/isolation.py was
    once missing, so the exported test could not be collected)."""

    @unittest.skipIf(os.environ.get('CBM_CLEAN_EXPORT_CHILD'), 'already running inside the clean copy')
    def test_exported_tests_import_in_a_clean_directory(self):
        if not (ROOT / 'scripts/export_public.py').exists():
            return  # the public export does not carry the export script itself
        spec = importlib.util.spec_from_file_location('export_public423', ROOT / 'scripts/export_public.py')
        export = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(export)
        entries = [e for e in export.INCLUDE if e.startswith(('scripts/', 'tests/'))]
        self.assertIn('tests/isolation.py', entries)
        import shutil
        with tempfile.TemporaryDirectory() as clean:
            for entry in entries:
                source = ROOT / entry
                if not source.exists():
                    continue  # a sparse checkout; the export itself fails on a really missing entry
                target = Path(clean) / entry
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                else:
                    shutil.copyfile(source, target)
            code = ('import sys; sys.path.insert(0, "tests"); import test_supervisor_421, test_doctor_423; '
                    'import unittest; r = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromName("test_doctor_423.RepeatTests")); '
                    'sys.exit(0 if r.wasSuccessful() else 1)')
            result = subprocess.run([sys.executable, '-B', '-c', code], cwd=clean, capture_output=True, text=True, timeout=180,
                                    env=dict(os.environ, CBM_CLEAN_EXPORT_CHILD='1'))
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])


if __name__ == '__main__':
    unittest.main()
