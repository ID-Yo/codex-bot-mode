"""Shutdown safety blocks only on work Office owns, accepts a not loaded chat and inspects every owner."""
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))  # the service imports the team_room package under this name

import scripts.office_desktop as office_desktop  # noqa: E402


def thread_id(number):
    return f'11111111-2222-3333-4444-{number:012d}'


class Controller:
    closing = False
    active = None
    store = None
    work = None
    mutex = None


class Bridge:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def call(self, name, arguments):
        tid = arguments['threadId']
        self.calls.append(tid)
        reply = self.replies[tid]
        if isinstance(reply, Exception):
            raise reply
        status, turns = reply
        return {'thread': {'id': tid, 'status': {'type': status}}, 'turns': [{'status': t} for t in turns]}


class OpenSession:
    """An Office session for a bound chat; state() is what the inspection reads."""
    _history = None

    def __init__(self, bot, tid, state):
        self.bot, self.thread_id, self._state = bot, tid, state

    def state(self):
        if isinstance(self._state, Exception):
            raise self._state
        return {'personal_thread_id': self.thread_id, **self._state}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        office_desktop._NATIVE_CACHE.clear()

    def inspect(self, replies, sessions=(), outbox=None):
        bridge = Bridge(replies)
        root = Path(self.tmp.name)
        outbox = outbox or {}

        class Personal:
            native_bindings = {f'bot{n}': tid for n, tid in enumerate(replies)}
            native_bridge = bridge
            controllers = {s.bot: s for s in sessions}

            def _native_root(self, bot, tid):
                return root / bot

        for bot, content in outbox.items():
            (root / bot).mkdir(parents=True, exist_ok=True)
            database = root / bot / 'native.sqlite'
            if content == 'corrupt':
                database.write_bytes(b'not a database')
                continue
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE outbox (id TEXT, state TEXT)')
                db.executemany('INSERT INTO outbox VALUES (?, ?)', [(f'd{n}', s) for n, s in enumerate(content)])
                db.commit()
        return office_desktop.inspect_shutdown(Controller(), Personal(), None, None), bridge


class NotLoadedTests(Fixture):
    def test_not_loaded_with_completed_or_no_turn_is_idle(self):
        state, bridge = self.inspect({thread_id(1): ('notLoaded', ['completed']), thread_id(2): ('notLoaded', []),
                                      thread_id(3): ('idle', ['completed'])})
        self.assertEqual(state['safety'], 'safe')
        self.assertEqual(len(bridge.calls), 3)

    def test_active_turns_in_linked_chats_that_office_did_not_start_do_not_block(self):
        state, _ = self.inspect({thread_id(1): ('active', ['inProgress']), thread_id(2): ('notLoaded', ['inProgress'])})
        self.assertEqual(state['safety'], 'safe')

    def test_office_delivery_in_a_linked_chat_blocks(self):
        state, _ = self.inspect({thread_id(1): ('active', ['inProgress'])}, outbox={'bot0': ['sending']})
        self.assertEqual(state['safety'], 'busy')
        self.assertEqual(state['busy_count'], 1)

    def test_unrecognised_status_is_unknown_even_with_a_turn_in_progress(self):
        state, _ = self.inspect({thread_id(1): ('somethingNew', ['inProgress'])})
        self.assertEqual(state['safety'], 'unknown')
        self.assertEqual(state['busy_count'], 0)


class EveryOwnerTests(Fixture):
    def test_read_error_is_unknown_but_later_bindings_are_still_checked(self):
        state, bridge = self.inspect({thread_id(1): RuntimeError('boom'), thread_id(2): ('idle', []),
                                      thread_id(3): ('notLoaded', [])}, outbox={'bot1': ['accepted']})
        self.assertEqual(state['safety'], 'unknown')
        self.assertEqual(bridge.calls, [thread_id(1), thread_id(2), thread_id(3)])
        self.assertEqual(state['busy_count'], 1)

    def test_failing_open_session_does_not_skip_the_remaining_bindings(self):
        broken = OpenSession('bot0', thread_id(1), RuntimeError('state failed'))
        state, bridge = self.inspect({thread_id(1): ('idle', []), thread_id(2): ('idle', [])}, sessions=[broken],
                                     outbox={'bot1': ['sending']})
        self.assertEqual(state['safety'], 'unknown')
        self.assertEqual(bridge.calls, [thread_id(2)])
        self.assertEqual(state['busy_count'], 1)

    def test_open_session_unknown_status_keeps_its_counted_work(self):
        session = OpenSession('bot0', thread_id(1), {'native_thread_status': 'weird', 'active': True,
                                                     'deliveries': [{'state': 'sending'}]})
        state, _ = self.inspect({thread_id(1): ('idle', [])}, sessions=[session])
        self.assertEqual(state['safety'], 'unknown')
        self.assertEqual(state['busy_count'], 1)  # the Office delivery; the native turn is not Office's

    def test_open_session_active_native_turn_alone_does_not_block(self):
        session = OpenSession('bot0', thread_id(1), {'native_thread_status': 'active', 'active': None, 'deliveries': []})
        state, _ = self.inspect({thread_id(1): ('active', [])}, sessions=[session])
        self.assertEqual(state['safety'], 'safe')

    def test_corrupt_outbox_later_keeps_counts_already_observed(self):
        state, _ = self.inspect({thread_id(1): ('idle', []), thread_id(2): ('idle', [])},
                                outbox={'bot0': ['sending'], 'bot1': 'corrupt'})
        self.assertEqual(state['safety'], 'unknown')
        self.assertEqual(state['busy_count'], 1)


class RealNativePersonalTests(Fixture):
    def session(self, status, turns):
        from team_room.native import NativePersonal
        tid = thread_id(7)

        class History:
            def call(self, name, arguments):
                return {'thread': {'id': tid, 'status': {'type': status}}, 'turns': turns, 'page': {}}

        root = Path(self.tmp.name) / 'native-session'
        session = NativePersonal(root, 'manager', tid, History(), start_worker=False)
        self.addCleanup(session.close)
        return session

    def inspect_session(self, session):
        class Holder:
            controllers = {'manager': session}
            native_bindings = {}
            native_bridge = None

        return office_desktop.inspect_shutdown(Controller(), Holder(), None, None)

    def test_native_active_turn_with_an_empty_outbox_does_not_block(self):
        session = self.session('active', [{'id': 't1', 'status': 'inProgress', 'items': []}])
        self.assertIsNotNone(session.state()['active'])  # the page still shows Codex working
        self.assertEqual(self.inspect_session(session)['safety'], 'safe')

    def test_accepted_office_delivery_blocks(self):
        session = self.session('active', [{'id': 't1', 'status': 'inProgress', 'items': []}])
        session.db.execute("INSERT INTO outbox(id,client_id,text,state) VALUES('d1','c1','hello','accepted')")
        session.db.commit()
        state = self.inspect_session(session)
        self.assertEqual(state['safety'], 'busy')
        self.assertEqual(state['busy_count'], 1)


class ClosedManagerSessionTests(Fixture):
    def managers(self, content):
        from team_room.project_manager_chat import ProjectManagerSessions
        root = Path(self.tmp.name) / 'project'
        folder = root / '.bot-mode' / 'team-room-v1' / 'project-managers' / 'alpha'
        folder.mkdir(parents=True)
        database = folder / 'native.sqlite'
        if content == 'corrupt':
            database.write_bytes(b'not a database')
        else:
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE outbox (id TEXT, state TEXT)')
                db.executemany('INSERT INTO outbox VALUES (?, ?)', [(f'd{n}', s) for n, s in enumerate(content)])
                db.commit()
        return ProjectManagerSessions(root, None)

    def inspect(self, managers):
        return office_desktop.inspect_shutdown(Controller(), None, None, managers)

    def test_accepted_delivery_on_disk_blocks_after_a_restart_with_no_open_session(self):
        state = self.inspect(self.managers(['accepted']))
        self.assertEqual(state['safety'], 'busy')

    def test_completed_outbox_on_disk_is_safe(self):
        self.assertEqual(self.inspect(self.managers(['completed']))['safety'], 'safe')

    def test_unreadable_durable_outbox_is_unknown(self):
        self.assertEqual(self.inspect(self.managers('corrupt'))['safety'], 'unknown')

    def test_ambiguous_durable_outbox_is_unknown(self):
        self.assertEqual(self.inspect(self.managers(['ambiguous']))['safety'], 'unknown')


if __name__ == '__main__':
    unittest.main()
