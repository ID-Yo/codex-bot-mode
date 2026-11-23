"""The supervisor status file (#424, S4-C): schema 1, last restart reason, stale or unreadable file = Unknown."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.team_room import supervisor_status as status

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('watchdog424', ROOT / 'scripts/relay_watchdog.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
PROJECT = {'project': 'X:/work/alpha', 'outbox': {'pending': 2, 'oldest_pending_seconds': 40, 'ambiguous': 0, 'needs_attention': 1,
                                                    'relay_reachable': True, 'pump_alive': True, 'last_delivered_at': '2026-11-23T11:00:00+00:00',
                                                    'recheckable': 0, 'expired': 0, 'problems': ['one problem']}}


def report(**extra):
    return {'relay_ok': True, 'desktop_running': True, 'office_ok': True, 'problems': [], 'projects': [], **extra}


def build(rep, previous=None, **kw):
    kw.setdefault('at', NOW)
    kw.setdefault('pass_seconds', 1.234)
    return status.build(rep, previous, **kw)


class BuildTests(unittest.TestCase):
    def test_document_has_schema_one_and_the_pass_facts(self):
        data = build(report(), interval_seconds=120, pid=77, version='abc123')
        self.assertEqual((data['schema'], data['at'], data['pass_seconds'], data['interval_seconds']), (1, NOW.isoformat(), 1.23, 120))
        self.assertEqual(data['supervisor'], {'pid': 77, 'version': 'abc123'})
        self.assertEqual((data['desktop_running'], data['office_ok'], data['problems']), (True, True, []))
        self.assertEqual(data['relay'], {'ok': True, 'manager_changed': False, 'last_restart': None})
        self.assertIsNone(data['last_pump_action'])

    def test_a_project_keeps_only_the_status_fields_and_its_name(self):
        data = build(report(projects=[PROJECT]))
        row = data['projects'][0]
        self.assertEqual((row['name'], row['pending'], row['needs_attention'], row['last_delivered_at']), ('alpha', 2, 1, '2026-11-23T11:00:00+00:00'))
        self.assertEqual(row['problems'], ['one problem'])
        self.assertNotIn('recheckable', row)
        self.assertNotIn('expired', row)

    def test_a_restart_records_when_why_and_whether_it_worked(self):
        data = build(report(relay_ok=False, restart={'ok': False, 'detail': 'pipe refused'}), restart_reason='relay down')
        self.assertEqual(data['relay']['last_restart'], {'at': NOW.isoformat(), 'reason': 'relay down', 'ok': False, 'detail': 'pipe refused'})
        self.assertFalse(data['relay']['ok'])

    def test_the_last_restart_is_kept_until_a_newer_one_replaces_it(self):
        first = build(report(restart={'ok': True, 'detail': 'started'}), restart_reason='relay down')
        later = build(report(), first, at=NOW + timedelta(minutes=2))
        self.assertEqual(later['relay']['last_restart'], first['relay']['last_restart'])
        newest = build(report(restart={'ok': True, 'detail': 'again'}), later, at=NOW + timedelta(minutes=4), restart_reason='Manager chat replaced')
        self.assertEqual((newest['relay']['last_restart']['reason'], newest['relay']['last_restart']['at']),
                         ('Manager chat replaced', (NOW + timedelta(minutes=4)).isoformat()))

    def test_a_replaced_stuck_pump_and_a_started_pump_are_recorded_and_kept(self):
        stuck = dict(PROJECT, hung_pump={'pid': 9, 'stopped': True, 'reason': 'stopped'})
        data = build(report(projects=[stuck]))
        self.assertEqual((data['last_pump_action']['action'], data['last_pump_action']['pid'], data['last_pump_action']['project']),
                         ('stuck pump replaced', 9, 'X:/work/alpha'))
        kept = build(report(projects=[PROJECT]), data, at=NOW + timedelta(minutes=2))
        self.assertEqual(kept['last_pump_action'], data['last_pump_action'])
        healed = build(report(projects=[{'project': 'p', 'outbox': {'healed': 'pump started'}}]), kept)
        self.assertEqual(healed['last_pump_action']['action'], 'pump started')
        refused = build(report(projects=[dict(PROJECT, hung_pump={'pid': 9, 'stopped': False, 'reason': 'not this pump'})]))
        self.assertEqual(refused['last_pump_action']['action'], 'stuck pump not stopped')

    def test_a_long_detail_is_cut(self):
        data = build(report(restart={'ok': False, 'detail': 'x' * 5000}))
        self.assertEqual(len(data['relay']['last_restart']['detail']), status.TEXT_LIMIT)


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'sub' / 'supervisor-status.json'

    def put(self, **changes):
        data = {**build(report()), **changes}
        status.write(self.path, data)
        return data

    def read(self, seconds_after=30):
        return status.read(self.path, now=NOW + timedelta(seconds=seconds_after))

    def test_a_fresh_document_without_problems_is_ok(self):
        self.put()
        result = self.read(30)
        self.assertEqual((result['state'], result['reason'], result['age_seconds']), ('ok', 'healthy', 30))
        self.assertEqual(result['status']['schema'], 1)

    def test_a_fresh_document_with_problems_says_which(self):
        self.put(problems=['relay down', 'pump stuck'])
        result = self.read()
        self.assertEqual((result['state'], result['reason']), ('problems', 'relay down; pump stuck'))

    def test_missing_unreadable_and_foreign_files_are_unknown_never_healthy(self):
        self.assertEqual(self.read()['state'], 'unknown')
        self.assertIn('has not written', self.read()['reason'])
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{not json', encoding='utf-8')
        self.assertIn('cannot be read', self.read()['reason'])
        self.path.write_text('[]', encoding='utf-8')
        self.assertIn('unsupported schema', self.read()['reason'])
        self.put(schema=2)
        self.assertEqual((self.read()['state'], self.read()['status']), ('unknown', None))
        self.assertIn('unsupported schema', self.read()['reason'])

    def test_a_document_without_a_usable_time_is_unknown(self):
        for bad in (None, 'yesterday', '2026-11-23T11:59:00', 5):
            self.put(at=bad)
            result = self.read()
            self.assertEqual(result['state'], 'unknown', bad)
            self.assertIn('no valid time', result['reason'])

    def test_an_old_document_is_unknown_even_when_it_said_healthy(self):
        self.put()
        limit = status.STALE_FACTOR * 120
        self.assertEqual(self.read(limit)['state'], 'ok')
        result = self.read(limit + 1)
        self.assertEqual(result['state'], 'unknown')
        self.assertIn('may not be running', result['reason'])
        self.assertIn(str(limit + 1), result['reason'])
        self.assertIsNone(result['status'])

    def test_the_expected_interval_of_the_file_sets_the_limit(self):
        self.put(interval_seconds=600)
        self.assertEqual(self.read(1500)['state'], 'ok')
        self.assertEqual(self.read(1801)['state'], 'unknown')

    def test_a_document_dated_in_the_future_is_unknown_but_small_clock_skew_is_fine(self):
        self.put()
        self.assertEqual(self.read(-60)['state'], 'ok')
        self.assertEqual(self.read(-60)['age_seconds'], 0)
        result = self.read(-(status.CLOCK_SKEW + 1))
        self.assertEqual(result['state'], 'unknown')
        self.assertIn('future', result['reason'])

    def test_write_is_atomic_and_leaves_no_temporary_file(self):
        self.put()
        self.put(problems=['new'])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['supervisor-status.json'])
        self.assertEqual(status.load(self.path)['problems'], ['new'])

    def test_load_ignores_a_foreign_schema(self):
        self.put(schema=9)
        self.assertIsNone(status.load(self.path))


class PassTests(unittest.TestCase):
    """One supervisor pass with every process and network call replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def run_pass(self, relay=(True,), manager_changed=(False,), restart=(True, 'started'), project=None, extra=()):
        with ExitStack() as stack:
            patch = lambda name, **kw: stack.enter_context(mock.patch.object(w, name, **kw))
            patch('relay_ok', side_effect=list(relay) + [relay[-1]] * 5)
            patch('desktop_running', return_value=True)
            patch('office_ok', return_value=True)
            patch('relay_manager_changed', side_effect=list(manager_changed) + [manager_changed[-1]] * 5)
            patch('registered_projects', return_value=['P'] if project else [])
            patch('projects_missing_outbox', return_value=[])
            patch('record_incident')
            patch('stop_relay', return_value=True)
            patch('restart_relay', return_value=restart)
            patch('project_report', return_value=project)
            code = w.main(['--office-root', str(self.folder), '--health-file', str(self.folder / 'h.json'), '--office-url', 'http://127.0.0.1:1',
                           '--restart', *(['--all-projects'] if project else []), *extra])
        return code, json.loads((self.folder / 'supervisor-status.json').read_text(encoding='utf-8'))

    def test_the_status_file_is_written_next_to_the_health_file_with_the_interval(self):
        code, data = self.run_pass(extra=('--interval-seconds', '300'))
        self.assertEqual((code, data['schema'], data['interval_seconds'], data['problems']), (0, 1, 300, []))
        self.assertEqual(data['supervisor']['pid'], __import__('os').getpid())
        self.assertEqual(len(data['supervisor']['version']), 12)
        self.assertGreaterEqual(data['pass_seconds'], 0)
        self.assertEqual(status.read(self.folder / 'supervisor-status.json')['state'], 'ok')

    def test_the_default_interval_is_the_tasks_two_minutes(self):
        self.assertEqual(self.run_pass()[1]['interval_seconds'], 120)

    def test_a_dead_relay_that_is_restarted_records_the_reason(self):
        code, data = self.run_pass(relay=(False, True))
        self.assertEqual(code, 0)
        self.assertEqual({k: data['relay']['last_restart'][k] for k in ('reason', 'ok', 'detail')}, {'reason': 'relay down', 'ok': True, 'detail': 'started'})

    def test_a_replaced_manager_chat_records_that_reason(self):
        _, data = self.run_pass(relay=(True, False, True), manager_changed=(True, False))
        self.assertEqual(data['relay']['last_restart']['reason'], 'Manager chat replaced')

    def test_a_failed_restart_is_a_problem_and_is_recorded(self):
        code, data = self.run_pass(relay=(False,), restart=(False, 'pipe refused'))
        self.assertEqual(code, 1)
        self.assertFalse(data['relay']['ok'])
        self.assertFalse(data['relay']['last_restart']['ok'])
        self.assertIn('pipe refused', data['problems'][0])
        self.assertEqual(status.read(self.folder / 'supervisor-status.json')['state'], 'problems')

    def test_the_last_restart_survives_a_quiet_pass(self):
        self.run_pass(relay=(False, True))
        _, data = self.run_pass()
        self.assertEqual(data['relay']['last_restart']['reason'], 'relay down')

    def test_projects_and_their_last_delivery_are_in_the_file(self):
        _, data = self.run_pass(project=PROJECT)
        self.assertEqual((data['projects'][0]['name'], data['projects'][0]['pending'], data['projects'][0]['last_delivered_at']),
                         ('alpha', 2, '2026-11-23T11:00:00+00:00'))

    def test_a_status_file_that_cannot_be_written_does_not_fail_the_pass(self):
        blocker = self.folder / 'blocker'
        blocker.write_text('a file where a folder is needed', encoding='utf-8')
        with ExitStack() as stack:
            for name, value in (('relay_ok', True), ('desktop_running', True), ('office_ok', True), ('relay_manager_changed', False),
                                ('registered_projects', []), ('projects_missing_outbox', [])):
                stack.enter_context(mock.patch.object(w, name, return_value=value))
            code = w.main(['--office-root', str(self.folder), '--health-file', str(self.folder / 'h.json'), '--office-url', 'http://127.0.0.1:1',
                           '--status-file', str(blocker / 'x' / 'status.json')])
        self.assertEqual(code, 0)
        self.assertTrue((self.folder / 'h.json').is_file())


class TaskScriptTests(unittest.TestCase):
    def test_the_scheduled_task_tells_the_watchdog_its_interval_and_status_prints_the_file(self):
        text = (ROOT / 'scripts' / 'supervisor_task.ps1').read_text(encoding='utf-8')
        self.assertIn('--interval-seconds {2}', text)
        self.assertIn('($Interval * 60)', text)
        self.assertIn('supervisor-status.json', text)


if __name__ == '__main__':
    unittest.main()
