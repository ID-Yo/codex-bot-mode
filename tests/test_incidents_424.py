"""One alert per incident (#424, S4-B): stable keys, grace period, one Manager row, one owner message."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from scripts.team_room import incidents as inc

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('watchdog424b', ROOT / 'scripts/relay_watchdog.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

T0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
RELAY_DOWN = ('Desktop relay down and could not be restarted (pipe refused): run python -B scripts/relay_watchdog.py --restart, '
              'or in the Manager chat powershell -File scripts/start_codex_office.ps1 -RelayOnly')
NEEDS = 'alpha: 2 report(s) could not be confirmed and need a decision (they no longer hold anything): outbox.py list --state needs-attention'


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def install_outbox(root, manager='thread-manager-1'):
    """An installed outbox.py (a stand-in file) and, unless manager is None, a roster that names the Manager."""
    folder = Path(root) / '.bot-mode'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'outbox.py').write_text('# installed', encoding='utf-8')
    if manager:
        (folder / 'roster.json').write_text(json.dumps({'participants': [{'bot': 'manager', 'role': 'Manager', 'thread_id': manager}]}), encoding='utf-8')


def problems(*texts, names=('alpha',)):
    return [inc.classify(text, names) for text in texts]


class ClassifyTests(unittest.TestCase):
    def kinds(self, text):
        found = inc.classify(text, ('alpha',))
        return found['key'], found['severity']

    def test_every_problem_the_supervisor_and_the_outbox_can_report_has_a_stable_key(self):
        cases = {
            RELAY_DOWN: ('relay-down', 'owner'),
            'Desktop relay serves a replaced Manager chat even after a restart': ('relay-wrong-manager', 'owner'),
            'Codex Office does not answer on http://localhost:24242': ('office-down', 'manager'),
            'Codex Desktop is not running and reports are waiting; they are delivered when Desktop starts': ('desktop-closed', 'info'),
            'alpha: .bot-mode/requests.py is installed but .bot-mode/outbox.py is missing, so completion reports are not delivered': ('outbox-missing:alpha', 'manager'),
            'alpha: installed outbox.py, requests.py differ from the repo': ('outbox-drift:alpha', 'manager'),
            'alpha: unconfirmed delivery is being reconciled; later reports for the same recipient wait for it': ('ambiguous:alpha', 'info'),
            NEEDS: ('needs-attention:alpha', 'manager'),
            'alpha: work is waiting and no pump is running (outbox.py status --heal starts one)': ('pump-down:alpha', 'manager'),
            'alpha: oldest pending row is 700 s old although relay and pump are up; check the Manager thread': ('pending-stale:alpha', 'info'),
            'alpha: pump 123 holds the lock but wrote no heartbeat for 180 s; not stopped (not this pump)': ('pump-stuck:alpha', 'manager'),
            'alpha: health check failed: OSError': ('check-failed:alpha', 'manager'),
            'alpha: outbox status failed: boom': ('status-failed:alpha', 'manager'),
        }
        for text, expected in cases.items():
            self.assertEqual(self.kinds(text), expected, text)

    def test_numbers_and_pids_do_not_change_the_key(self):
        one = inc.classify('alpha: 2 report(s) could not be confirmed and need a decision', ('alpha',))['key']
        two = inc.classify('alpha: 9 report(s) could not be confirmed and need a decision', ('alpha',))['key']
        self.assertEqual(one, two)
        self.assertEqual(inc.classify('alpha: pump 1 holds the lock but wrote no heartbeat for 180 s', ('alpha',))['key'],
                         inc.classify('alpha: pump 99 holds the lock but wrote no heartbeat for 181 s', ('alpha',))['key'])

    def test_the_same_fault_in_two_projects_is_two_incidents(self):
        a = inc.classify(NEEDS, ('alpha', 'beta'))
        b = inc.classify(NEEDS.replace('alpha', 'beta'), ('alpha', 'beta'))
        self.assertNotEqual(a['key'], b['key'])
        self.assertEqual((a['project'], b['project']), ('alpha', 'beta'))

    def test_an_unknown_problem_gets_a_number_free_fallback_key_for_the_manager(self):
        one = inc.classify('alpha: strange thing 12 happened', ('alpha',))
        two = inc.classify('alpha: strange thing 77 happened', ('alpha',))
        self.assertEqual(one['key'], two['key'])
        self.assertTrue(one['key'].startswith('other:'))
        self.assertEqual(one['severity'], 'manager')

    def test_a_prefix_of_another_project_name_is_not_a_match(self):
        self.assertIsNone(inc.classify('alphabet: health check failed: x', ('alpha',))['project'])

    def test_a_project_that_is_not_registered_is_named_by_the_prefix_of_its_text(self):
        gamma = inc.classify('gamma: .bot-mode/requests.py is installed but .bot-mode/outbox.py is missing, so completion reports are not delivered', ('alpha',))
        delta = inc.classify('delta: .bot-mode/requests.py is installed but .bot-mode/outbox.py is missing, so completion reports are not delivered', ('alpha',))
        self.assertEqual((gamma['key'], gamma['project']), ('outbox-missing:gamma', 'gamma'))
        self.assertEqual((delta['key'], delta['project']), ('outbox-missing:delta', 'delta'))

    def test_machine_problems_never_take_a_project_from_their_text(self):
        for text, key in ((RELAY_DOWN, 'relay-down'), ('Codex Office does not answer on http://x: 24242', 'office-down'),
                          ('Desktop relay serves a replaced Manager chat: even after a restart', 'relay-wrong-manager')):
            found = inc.classify(text, ())
            self.assertEqual((found['key'], found['project']), (key, None), text)

    def test_the_problem_texts_the_real_outbox_status_produces_are_all_known(self):
        from scripts.team_room import outbox
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'alpha'
            db = outbox.OutboxDB(root / '.bot-mode' / 'outbox' / 'queue.sqlite')
            try:
                old = (T0 - timedelta(hours=2)).isoformat()
                unsure = db.enqueue('dev', 'manager', 't', 'unsure')
                db.mark_ambiguous(unsure, 'timeout')
                lost = db.enqueue('dev', 'manager', 't', 'lost')
                db.mark_ambiguous(lost, 'timeout')
                db.conn.execute('UPDATE outbox SET ambiguous_at = ? WHERE id = ?', (old, lost))
                db.conn.execute("INSERT INTO outbox (id, created_at, sender_bot, target_bot, text, state) VALUES ('w', ?, 'dev', 'other', 'wait', 'pending')", (old,))
                db.conn.commit()
            finally:
                db.close()
            texts = []
            for relay, pump in ((True, False), (True, True), (False, True)):
                with mock.patch.object(outbox, 'relay_reachable', return_value=relay), mock.patch.object(outbox, 'pump_alive', return_value=pump):
                    # the supervisor drops this one: the relay's own problem covers it (relay_watchdog.main)
                    texts += [f"alpha: {p}" for p in outbox.queue_status(root)['problems'] if 'relay is not reachable' not in p]
        self.assertGreaterEqual(len(texts), 4)
        for text in texts:
            found = inc.classify(text, ('alpha',))
            self.assertFalse(found['key'].startswith('other:'), text)


class StepTests(unittest.TestCase):
    def run_steps(self, plan):
        """plan: list of (minute, problem texts). Carries out every action as the watchdog does. Returns (state, all events, all actions)."""
        state, events, actions = inc.empty(), [], []
        for minute, texts in plan:
            e, a = inc.step(state, problems(*texts), at(minute))
            events += e
            for action in a:
                inc.mark(state, action['key'], 'row' if action['do'] == 'manager_row' else 'popup', at(minute), action.get('upto'))
            actions += [(minute, x) for x in a]
        return state, events, actions

    def test_a_problem_opens_one_incident_and_asks_for_one_manager_row(self):
        state, events, actions = self.run_steps([(0, [NEEDS]), (2, [NEEDS]), (4, [NEEDS]), (6, [NEEDS])])
        self.assertEqual([e['event'] for e in events], ['opened'])
        self.assertEqual([(m, a['do'], a['n']) for m, a in actions], [(0, 'manager_row', 0)])
        self.assertEqual(list(state['incidents']), ['needs-attention:alpha'])

    def test_a_row_that_failed_is_asked_for_again_until_it_is_marked(self):
        state = inc.empty()
        self.assertEqual(len(inc.step(state, problems(NEEDS), at(0))[1]), 1)
        self.assertEqual(len(inc.step(state, problems(NEEDS), at(2))[1]), 1)  # nobody marked it
        inc.mark(state, 'needs-attention:alpha', 'row', at(2), 0)
        self.assertEqual(inc.step(state, problems(NEEDS), at(4))[1], [])

    def test_a_flapping_fault_inside_the_grace_period_is_one_incident_and_one_row(self):
        plan = [(0, [NEEDS]), (2, []), (4, [NEEDS]), (6, []), (8, [NEEDS]), (10, []), (12, [NEEDS])]
        state, events, actions = self.run_steps(plan)
        self.assertEqual([e['event'] for e in events], ['opened', 'returned', 'returned', 'returned'])
        self.assertEqual(len(actions), 1)
        self.assertEqual(state['incidents']['needs-attention:alpha']['reopened'], 3)

    def test_nothing_is_announced_while_an_incident_is_clearing(self):
        state = inc.empty()
        inc.step(state, problems(RELAY_DOWN), at(0))
        inc.mark(state, 'relay-down', 'row', at(0), 0)
        events, actions = inc.step(state, [], at(11))  # absent, but the owner timer must not fire for a gone problem
        self.assertEqual((events, actions), ([], []))
        self.assertTrue(state['incidents']['relay-down']['absent_since'])

    def test_it_closes_only_after_the_grace_period_and_three_clean_passes_and_a_return_is_a_new_incident(self):
        grace = inc.GRACE_SECONDS // 60
        state, events, actions = self.run_steps([(0, [NEEDS]), (10, []), (12, []), (10 + grace, [])])
        closed = [e for e in events if e['event'] == 'closed']
        self.assertEqual(len(closed), 1)
        self.assertEqual((closed[0]['key'], closed[0]['open_seconds']), ('needs-attention:alpha', 600))
        self.assertEqual(state['incidents'], {})
        state, events, actions = self.run_steps([(0, [NEEDS]), (10, []), (12, []), (10 + grace, []), (40, [NEEDS])])
        self.assertEqual([e['event'] for e in events], ['opened', 'closed', 'opened'])
        self.assertEqual([a['do'] for _, a in actions], ['manager_row', 'manager_row'])

    def test_two_clean_passes_far_apart_do_not_close_it(self):
        state, events, _ = self.run_steps([(0, [NEEDS]), (10, []), (10 + 24 * 60, [])])  # a day later, but only two passes saw it gone
        self.assertEqual([e['event'] for e in events], ['opened'])
        self.assertEqual(state['incidents']['needs-attention:alpha']['absent_passes'], 2)
        _, events, _ = self.run_steps([(0, [NEEDS]), (10, []), (10 + 24 * 60, []), (10 + 24 * 60 + 2, [])])
        self.assertEqual([e['event'] for e in events], ['opened', 'closed'])

    def test_three_clean_passes_inside_the_grace_period_do_not_close_it(self):
        state, events, _ = self.run_steps([(0, [NEEDS]), (2, []), (4, []), (6, []), (8, [])])
        self.assertEqual([e['event'] for e in events], ['opened'])
        self.assertEqual(state['incidents']['needs-attention:alpha']['absent_passes'], 4)

    def test_a_return_starts_the_clean_pass_count_again(self):
        grace = inc.GRACE_SECONDS // 60
        # three clean passes before the return do not count after it: only two have happened since, however late the second is
        _, events, _ = self.run_steps([(0, [NEEDS]), (2, []), (4, []), (6, [NEEDS]), (8, []), (8 + grace, [])])
        self.assertEqual([e['event'] for e in events], ['opened', 'returned'])
        _, events, _ = self.run_steps([(0, [NEEDS]), (2, []), (4, []), (6, [NEEDS]), (8, []), (10, []), (8 + grace, [])])
        self.assertEqual([e['event'] for e in events], ['opened', 'returned', 'closed'])

    def test_a_new_incident_has_a_new_dedupe_key_so_its_row_is_not_swallowed(self):
        state = inc.empty()
        inc.step(state, problems(NEEDS), at(0))
        first = inc.dedupe_key(state['incidents']['needs-attention:alpha'])
        state = inc.empty()
        inc.step(state, problems(NEEDS), at(60))
        self.assertNotEqual(first, inc.dedupe_key(state['incidents']['needs-attention:alpha']))

    def test_the_owner_message_comes_once_and_only_after_ten_minutes_for_owner_incidents(self):
        state, events, actions = self.run_steps([(0, [RELAY_DOWN]), (2, [RELAY_DOWN]), (9, [RELAY_DOWN]), (10, [RELAY_DOWN]), (12, [RELAY_DOWN]), (30, [RELAY_DOWN])])
        self.assertEqual([(m, a['do']) for m, a in actions], [(0, 'manager_row'), (10, 'popup')])

    def test_a_fault_that_heals_itself_never_pops_up(self):
        state, events, actions = self.run_steps([(0, [RELAY_DOWN]), (2, [RELAY_DOWN]), (4, []), (20, []), (40, [])])
        self.assertEqual([a['do'] for _, a in actions], ['manager_row'])
        self.assertEqual(state['incidents'], {})

    def test_manager_and_info_incidents_never_pop_up_and_info_sends_no_row(self):
        info = 'alpha: unconfirmed delivery is being reconciled; later reports wait'
        state, events, actions = self.run_steps([(0, [NEEDS, info]), (30, [NEEDS, info]), (90, [NEEDS, info])])
        self.assertEqual([a['do'] for _, a in actions], ['manager_row'])
        self.assertEqual(actions[0][1]['key'], 'needs-attention:alpha')
        self.assertEqual(sorted(state['incidents']), ['ambiguous:alpha', 'needs-attention:alpha'])

    def test_a_reminder_row_follows_every_six_hours_while_it_is_open(self):
        hours = lambda n: n * 60
        plan = [(0, [NEEDS]), (hours(5), [NEEDS]), (hours(6), [NEEDS]), (hours(7), [NEEDS]), (hours(12), [NEEDS])]
        _, _, actions = self.run_steps(plan)
        self.assertEqual([(m, a['n']) for m, a in actions], [(0, 0), (hours(6), 1), (hours(12), 2)])

    def test_a_late_first_row_is_not_followed_by_an_instant_reminder(self):
        state = inc.empty()
        inc.step(state, problems(NEEDS), at(0))
        late = at(60 * 7)
        actions = inc.step(state, problems(NEEDS), late)[1]
        self.assertEqual((actions[0]['n'], actions[0]['upto']), (0, 1))
        inc.mark(state, 'needs-attention:alpha', 'row', late, actions[0]['upto'])
        self.assertEqual(inc.step(state, problems(NEEDS), late + timedelta(minutes=2))[1], [])

    def test_the_text_of_an_open_incident_follows_the_latest_pass(self):
        state = inc.empty()
        inc.step(state, problems('alpha: 2 report(s) could not be confirmed and need a decision'), at(0))
        inc.step(state, problems('alpha: 5 report(s) could not be confirmed and need a decision'), at(2))
        self.assertIn('5 report', state['incidents']['needs-attention:alpha']['text'])

    def test_state_survives_save_and_load_and_foreign_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'local' / 'incidents.json'
            state = inc.empty()
            inc.step(state, problems(NEEDS), at(0))
            inc.save(path, state)
            self.assertEqual(inc.load(path), state)
            self.assertEqual([p.name for p in path.parent.iterdir()], ['incidents.json'])
            path.write_text('{broken', encoding='utf-8')
            self.assertEqual(inc.load(path), inc.empty())
            path.write_text(json.dumps({'schema': 9, 'incidents': {'x': {}}}), encoding='utf-8')
            self.assertEqual(inc.load(path), inc.empty())
            self.assertEqual(inc.load(Path(folder) / 'missing.json'), inc.empty())

    def valid_incident(self):
        state = inc.empty()
        inc.step(state, problems(RELAY_DOWN), at(0))
        inc.mark(state, 'relay-down', 'row', at(0), 0)
        return state['incidents']['relay-down']

    def stored(self, folder, incidents):
        path = Path(folder) / 'incidents.json'
        path.write_text(json.dumps({'schema': 1, 'incidents': incidents}), encoding='utf-8')
        return path

    def test_the_reviewers_damaged_record_gives_an_empty_state_instead_of_a_keyerror(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.stored(folder, {'relay-down': {}})
            self.assertEqual(inc.read(path), (inc.empty(), ['relay-down']))
            state = inc.load(path)
            events, actions = inc.step(state, problems(RELAY_DOWN), at(0))  # opens it afresh; no KeyError
            self.assertEqual([e['event'] for e in events], ['opened'])
            self.assertEqual(len(actions), 1)

    def test_every_kind_of_damage_to_a_record_is_dropped(self):
        good = self.valid_incident()
        damage = {
            'missing key': lambda i: i.pop('key'), 'other key': lambda i: i.update(key='office-down'), 'no opened': lambda i: i.pop('opened'),
            'opened not a time': lambda i: i.update(opened='yesterday'), 'opened without zone': lambda i: i.update(opened='2026-11-23T12:00:00'),
            'opened a number': lambda i: i.update(opened=5), 'last_seen gone': lambda i: i.pop('last_seen'),
            'severity unknown': lambda i: i.update(severity='bogus'), 'severity gone': lambda i: i.pop('severity'),
            'text a number': lambda i: i.update(text=7), 'remedy gone': lambda i: i.pop('remedy'),
            'absent_since not a time': lambda i: i.update(absent_since='x'), 'reopened text': lambda i: i.update(reopened='1'),
            'reopened bool': lambda i: i.update(reopened=True), 'project a number': lambda i: i.update(project=5),
            'done a string': lambda i: i.update(done='x'), 'done row text': lambda i: i.update(done={'row': '0'}),
            'done row bool': lambda i: i.update(done={'row': True}), 'done popup number': lambda i: i.update(done={'popup': 1}),
            'a list': lambda i: None,
        }
        with tempfile.TemporaryDirectory() as folder:
            for name, hurt in damage.items():
                record = json.loads(json.dumps(good))
                hurt(record)
                if name == 'a list':
                    record = [record]
                path = self.stored(folder, {'relay-down': record})
                state, dropped = inc.read(path)
                self.assertEqual((state['incidents'], dropped), ({}, ['relay-down']), name)
            self.assertEqual(inc.read(self.stored(folder, {'relay-down': good}))[1], [])  # the undamaged record is kept

    def test_a_damaged_record_does_not_take_its_valid_neighbours_with_it(self):
        good = self.valid_incident()
        other = dict(json.loads(json.dumps(good)), key='office-down')
        with tempfile.TemporaryDirectory() as folder:
            state, dropped = inc.read(self.stored(folder, {'relay-down': good, 'office-down': other, 'pump-down:alpha': {'key': 'pump-down:alpha'}}))
        self.assertEqual((sorted(state['incidents']), dropped), (['office-down', 'relay-down'], ['pump-down:alpha']))

    def test_a_state_that_is_not_an_object_of_incidents_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            for body in ('[]', '{"schema": 1, "incidents": []}', '{"schema": 1}', '{"schema": 1, "incidents": null}', '""'):
                path = Path(folder) / 'incidents.json'
                path.write_text(body, encoding='utf-8')
                self.assertEqual(inc.read(path), (inc.empty(), []), body)

    def test_summary_lists_open_incidents_in_stable_order(self):
        state, _, _ = self.run_steps([(0, [RELAY_DOWN, NEEDS])])
        rows = inc.summary(state)
        self.assertEqual([r['key'] for r in rows], ['needs-attention:alpha', 'relay-down'])
        self.assertEqual((rows[0]['manager_notified'], rows[0]['owner_notified'], rows[0]['clearing']), (True, False, False))


class QueueRowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def install_outbox(self):
        install_outbox(self.root)

    def rows(self):
        conn = sqlite3.connect(str(self.root / '.bot-mode' / 'outbox' / 'queue.sqlite'))
        try:
            return conn.execute('SELECT sender_bot, target_bot, state, text, dedupe_key FROM outbox').fetchall()
        finally:
            conn.close()

    def test_the_row_is_one_pending_report_from_the_supervisor_to_the_manager(self):
        self.install_outbox()
        state = inc.empty()
        inc.step(state, problems(NEEDS), at(0))
        incident = state['incidents']['needs-attention:alpha']
        self.assertEqual(inc.queue_row(self.root, inc.row_text(incident), inc.dedupe_key(incident)), (True, 'queued'))
        self.assertEqual(inc.queue_row(self.root, inc.row_text(incident), inc.dedupe_key(incident)), (True, 'queued'))  # a retry adds nothing
        (sender, target, row_state, text, key), = self.rows()
        self.assertEqual((sender, target, row_state), ('supervisor', 'manager', 'pending'))
        self.assertIn('needs-attention:alpha', text)
        self.assertIn('Remedy:', text)
        self.assertEqual(key, inc.dedupe_key(incident))

    def test_a_reminder_is_a_second_row(self):
        self.install_outbox()
        state = inc.empty()
        inc.step(state, problems(NEEDS), at(0))
        incident = state['incidents']['needs-attention:alpha']
        inc.queue_row(self.root, inc.row_text(incident), inc.dedupe_key(incident))
        inc.queue_row(self.root, inc.row_text(incident, 1), inc.dedupe_key(incident, 1))
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(inc.row_text(incident, 1).startswith('[Supervisor reminder 1]'))

    def test_no_row_is_queued_when_the_roster_names_no_manager(self):
        install_outbox(self.root, manager=None)
        ok, detail = inc.queue_row(self.root, 'text', 'k')
        self.assertFalse(ok)
        self.assertTrue(detail.startswith(inc.NO_MANAGER), detail)
        self.assertFalse((self.root / '.bot-mode' / 'outbox').exists())  # not even an empty queue is created

    def test_the_row_carries_the_managers_thread(self):
        install_outbox(self.root, manager='thread-manager-9')
        inc.queue_row(self.root, 'text', 'k')
        conn = sqlite3.connect(str(self.root / '.bot-mode' / 'outbox' / 'queue.sqlite'))
        try:
            self.assertEqual(conn.execute('SELECT target_thread_id FROM outbox').fetchall(), [('thread-manager-9',)])
        finally:
            conn.close()

    def test_nothing_is_queued_where_no_outbox_is_installed(self):
        ok, detail = inc.queue_row(self.root, 'text', 'k')
        self.assertFalse(ok)
        self.assertIn('no outbox installed', detail)
        self.assertFalse((self.root / '.bot-mode' / 'outbox').exists())


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        install_outbox(self.folder)

    def args(self, **kw):
        defaults = dict(incident_file=str(self.folder / '.bot-mode' / 'local' / 'incidents.json'), project=[], notify=True)
        defaults.update(kw)
        return mock.Mock(**defaults)

    def rows(self):
        path = self.folder / '.bot-mode' / 'outbox' / 'queue.sqlite'
        if not path.is_file():
            return []
        conn = sqlite3.connect(str(path))
        try:
            return conn.execute('SELECT text FROM outbox').fetchall()
        finally:
            conn.close()

    def log(self):
        path = self.folder / '.bot-mode' / 'local' / 'incidents.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.is_file() else []

    def test_a_damaged_state_file_is_repaired_and_the_pass_goes_on(self):
        state_file = Path(self.args().incident_file)
        state_file.parent.mkdir(parents=True)
        state_file.write_text('{"schema":1,"incidents":{"relay-down":{}}}', encoding='utf-8')
        with mock.patch.object(w, 'OFFICE', self.folder), mock.patch.object(w, 'show_owner_message', return_value=True):
            shown = w.run_incidents(self.args(), {'problems': [RELAY_DOWN]}, at(0))
        self.assertEqual([i['key'] for i in shown], ['relay-down'])
        repaired = [e for e in self.log() if e['event'] == 'state_repaired']
        self.assertEqual([e['dropped'] for e in repaired], [['relay-down']])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(inc.read(state_file)[1], [])  # the file is whole again

    def test_a_missing_manager_is_logged_once_tried_every_pass_and_queued_when_it_appears(self):
        (self.folder / '.bot-mode' / 'roster.json').unlink()  # setUp named a Manager; here the roster has none
        with mock.patch.object(w, 'OFFICE', self.folder):
            for minute in (0, 2, 4):
                shown = w.run_incidents(self.args(), {'problems': [NEEDS.replace('alpha: ', '')]}, at(minute))
            self.assertEqual(self.rows(), [])
            self.assertFalse(shown[0]['manager_notified'])
            skipped = [e for e in self.log() if e['event'] == 'row_skipped']
            self.assertEqual(len(skipped), 1)
            self.assertIn('no Manager in the roster', skipped[0]['detail'])
            install_outbox(self.folder)  # the roster now names a Manager
            shown = w.run_incidents(self.args(), {'problems': [NEEDS.replace('alpha: ', '')]}, at(6))
        self.assertTrue(shown[0]['manager_notified'])
        self.assertEqual(len(self.rows()), 1)

    def test_a_flapping_relay_gives_one_row_one_owner_message_and_one_incident_line(self):
        with mock.patch.object(w, 'OFFICE', self.folder), mock.patch.object(w, 'show_owner_message', return_value=True) as popup:
            for minute, down in ((0, True), (2, False), (4, True), (6, False), (8, True), (10, True), (12, False), (14, True), (30, True)):
                shown = w.run_incidents(self.args(), {'problems': [RELAY_DOWN] if down else []}, at(minute))
        self.assertEqual(len(self.rows()), 1)
        popup.assert_called_once()
        self.assertEqual([e['event'] for e in self.log()].count('opened'), 1)
        self.assertEqual([e['event'] for e in self.log()].count('owner_message'), 1)
        self.assertEqual((shown[0]['key'], shown[0]['owner_notified'], shown[0]['manager_notified']), ('relay-down', True, True))

    def test_without_notify_there_is_no_windows_message_and_it_is_asked_for_again_later(self):
        with mock.patch.object(w, 'OFFICE', self.folder), mock.patch.object(w, 'show_owner_message', return_value=True) as popup:
            for minute in (0, 11, 20):
                w.run_incidents(self.args(notify=False), {'problems': [RELAY_DOWN]}, at(minute))
            popup.assert_not_called()
            w.run_incidents(self.args(notify=True), {'problems': [RELAY_DOWN]}, at(21))
            popup.assert_called_once()

    def test_a_failed_windows_message_is_tried_again_in_the_next_pass(self):
        with mock.patch.object(w, 'OFFICE', self.folder), mock.patch.object(w, 'show_owner_message', side_effect=[False, True]) as popup:
            for minute in (0, 11, 13, 15):
                shown = w.run_incidents(self.args(), {'problems': [RELAY_DOWN]}, at(minute))
        self.assertEqual(popup.call_count, 2)
        self.assertTrue(shown[0]['owner_notified'])

    def test_a_project_incident_goes_to_that_projects_outbox_and_others_to_the_office(self):
        project = self.folder / 'work' / 'alpha'
        install_outbox(project)
        with mock.patch.object(w, 'OFFICE', self.folder):
            w.run_incidents(self.args(project=[str(project)]), {'problems': [NEEDS, 'Codex Office does not answer on http://x']}, at(0))
        conn = sqlite3.connect(str(project / '.bot-mode' / 'outbox' / 'queue.sqlite'))
        try:
            project_rows = conn.execute('SELECT text FROM outbox').fetchall()
        finally:
            conn.close()
        self.assertEqual(len(project_rows), 1)
        self.assertIn('needs-attention:alpha', project_rows[0][0])
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('office-down', self.rows()[0][0])

    def test_a_project_without_an_outbox_is_logged_once_and_not_retried(self):
        project = self.folder / 'work' / 'beta'
        project.mkdir(parents=True)
        problem = 'beta: health check failed: OSError'
        with mock.patch.object(w, 'OFFICE', self.folder):
            for minute in (0, 2, 4):
                w.run_incidents(self.args(project=[str(project)]), {'problems': [problem]}, at(minute))
        self.assertEqual([e['event'] for e in self.log()].count('row_skipped'), 1)
        self.assertEqual(self.rows(), [])

    def test_a_row_that_cannot_be_written_is_retried_without_failing_the_pass(self):
        with mock.patch.object(w, 'OFFICE', self.folder), mock.patch.object(w.incidents_module(), 'queue_row', side_effect=[sqlite3.OperationalError('locked'), (True, 'queued')]):
            first = w.run_incidents(self.args(), {'problems': [NEEDS.replace('alpha: ', '')]}, at(0))
            self.assertFalse(first[0]['manager_notified'])
            second = w.run_incidents(self.args(), {'problems': [NEEDS.replace('alpha: ', '')]}, at(2))
        self.assertTrue(second[0]['manager_notified'])

    def test_show_owner_message_uses_msg_with_the_text_and_survives_a_missing_command(self):
        incident = {'text': 'relay down', 'remedy': 'run it'}
        with mock.patch.object(w.os, 'name', 'nt'), mock.patch.object(w.subprocess, 'run') as run:
            self.assertTrue(w.show_owner_message(incident))
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ['msg', '*', '/TIME:600'])
            self.assertIn('relay down', command[3])
        with mock.patch.object(w.os, 'name', 'nt'), mock.patch.object(w.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertFalse(w.show_owner_message(incident))
        with mock.patch.object(w.os, 'name', 'posix'), mock.patch.object(w.subprocess, 'run') as run:
            self.assertFalse(w.show_owner_message(incident))
            run.assert_not_called()


class MainPassTests(unittest.TestCase):
    """A real supervisor pass with every process replaced: the health file carries the open incidents."""

    def passes(self, folder, **patches):
        stack = ExitStack()
        values = {'relay_ok': True, 'desktop_running': True, 'office_ok': True, 'relay_manager_changed': False,
                  'registered_projects': [], 'projects_missing_outbox': []}
        values.update(patches)
        for name, value in values.items():
            stack.enter_context(mock.patch.object(w, name, return_value=value))
        self.addCleanup(stack.close)
        return ['--office-root', str(folder), '--health-file', str(Path(folder) / '.bot-mode' / 'relay-health.json'), '--office-url', 'http://127.0.0.1:1']

    def test_a_damaged_incident_file_does_not_stop_the_pass_or_the_health_file(self):
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / '.bot-mode' / 'local'
            local.mkdir(parents=True)
            (local / 'incidents.json').write_text('{"schema":1,"incidents":{"relay-down":{}}}', encoding='utf-8')
            argv = self.passes(folder, relay_ok=False, restart_relay=(False, 'pipe refused'), record_incident=None)
            self.assertEqual(w.main(argv + ['--restart']), 1)
            health = json.loads((Path(folder) / '.bot-mode' / 'relay-health.json').read_text(encoding='utf-8'))
            self.assertEqual([i['key'] for i in health['incidents']], ['relay-down'])
            self.assertNotIn('incidents_error', health)

    def test_a_failure_in_the_incident_bookkeeping_is_recorded_and_never_stops_the_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            argv = self.passes(folder)
            with mock.patch.object(w, 'run_incidents', side_effect=KeyError('opened')):
                self.assertEqual(w.main(argv), 0)  # the pass itself was healthy
            health = json.loads((Path(folder) / '.bot-mode' / 'relay-health.json').read_text(encoding='utf-8'))
            self.assertEqual((health['incidents'], health['incidents_error']), ([], "KeyError: 'opened'"))
            status = json.loads((Path(folder) / '.bot-mode' / 'supervisor-status.json').read_text(encoding='utf-8'))
            self.assertEqual(status['incidents_error'], "KeyError: 'opened'")
            self.assertEqual(status['schema'], 1)

    def test_a_pass_without_an_incident_error_says_none_in_the_status_file(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(w.main(self.passes(folder)), 0)
            status = json.loads((Path(folder) / '.bot-mode' / 'supervisor-status.json').read_text(encoding='utf-8'))
            self.assertIsNone(status['incidents_error'])

    def test_a_pass_with_a_dead_relay_opens_the_incident_in_the_health_file_and_queues_one_row(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            folder = Path(folder)
            install_outbox(folder)
            for name, value in (('relay_ok', False), ('desktop_running', True), ('office_ok', True), ('relay_manager_changed', False),
                                ('registered_projects', []), ('projects_missing_outbox', []), ('record_incident', None),
                                ('restart_relay', (False, 'pipe refused'))):
                stack.enter_context(mock.patch.object(w, name, return_value=value))
            argv = ['--office-root', str(folder), '--health-file', str(folder / '.bot-mode' / 'relay-health.json'), '--office-url', 'http://127.0.0.1:1', '--restart']
            for _ in range(3):
                self.assertEqual(w.main(argv), 1)
            health = json.loads((folder / '.bot-mode' / 'relay-health.json').read_text(encoding='utf-8'))
            self.assertEqual([i['key'] for i in health['incidents']], ['relay-down'])
            self.assertTrue(health['incidents'][0]['manager_notified'])
            conn = sqlite3.connect(str(folder / '.bot-mode' / 'outbox' / 'queue.sqlite'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
            conn.close()

    def test_two_projects_without_an_outbox_are_two_incidents_and_two_rows_in_a_real_pass(self):
        """The Verifier's reproduction: gamma is missing its outbox, then gamma and delta; delta must be announced too."""
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            install_outbox(folder)
            gamma, delta = folder / 'work' / 'gamma', folder / 'work' / 'delta'
            for project in (gamma, delta):
                (project / '.bot-mode').mkdir(parents=True)
                (project / '.bot-mode' / 'requests.py').write_text('# installed', encoding='utf-8')
            argv = self.passes(folder)
            health_file = folder / '.bot-mode' / 'relay-health.json'
            with mock.patch.object(w, 'projects_missing_outbox', return_value=[str(gamma)]):
                self.assertEqual(w.main(argv), 1)
            with mock.patch.object(w, 'projects_missing_outbox', return_value=[str(gamma), str(delta)]):
                self.assertEqual(w.main(argv), 1)
                self.assertEqual(w.main(argv), 1)  # a repeated pass adds nothing
            health = json.loads(health_file.read_text(encoding='utf-8'))
            self.assertEqual(sorted(i['key'] for i in health['incidents']), ['outbox-missing:delta', 'outbox-missing:gamma'])
            self.assertTrue(all(i['manager_notified'] for i in health['incidents']))
            conn = sqlite3.connect(str(folder / '.bot-mode' / 'outbox' / 'queue.sqlite'))
            try:
                texts = [row[0] for row in conn.execute('SELECT text FROM outbox ORDER BY created_at')]
            finally:
                conn.close()
            self.assertEqual(len(texts), 2)  # projects without an outbox are announced to the Manager of this checkout
            self.assertEqual([('gamma' in t, 'delta' in t) for t in texts], [(True, False), (False, True)])

    def test_a_healthy_pass_has_no_incidents(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            for name, value in (('relay_ok', True), ('desktop_running', True), ('office_ok', True), ('relay_manager_changed', False),
                                ('registered_projects', []), ('projects_missing_outbox', [])):
                stack.enter_context(mock.patch.object(w, name, return_value=value))
            code = w.main(['--office-root', folder, '--health-file', str(Path(folder) / 'h.json'), '--office-url', 'http://127.0.0.1:1'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads((Path(folder) / 'h.json').read_text(encoding='utf-8'))['incidents'], [])


if __name__ == '__main__':
    unittest.main()
