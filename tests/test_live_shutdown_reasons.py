"""Live 0.1.6 state: long Verifier/Reviewer threads make the full history read fail, which made shutdown inspection
unknown with no actionable reason. Inspection now reads only the outbox and a light status, history reads fall back to
smaller answers, and every busy or unknown cause names its chat, delivery or turn and what to do."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import scripts.office_desktop as office_desktop  # noqa: E402
from team_room.desktop_relay import RelayBridge, create_relay  # noqa: E402
from team_room.native import NativePersonal  # noqa: E402
from team_room import work  # noqa: E402
from team_room.work import TooLarge, Uncertain  # noqa: E402

TID = '11111111-2222-3333-4444-{:012d}'


class Controller:
    closing = False
    active = None
    store = None
    work = None
    mutex = None


class LongThreadBridge:
    """Desktop as seen live: a full history read of a long thread is refused, a light read works."""

    def __init__(self, status='active', turn='inProgress'):
        self.calls = []
        self.status, self.turn = status, turn

    def call(self, name, arguments):
        self.calls.append((arguments['turnLimit'], arguments['includeOutputs']))
        if arguments['turnLimit'] > 1 and arguments['includeOutputs'] and arguments['maxOutputCharsPerItem'] > 2000:
            raise TooLarge('Desktop answer exceeds the size limit')
        return {'thread': {'id': arguments['threadId'], 'status': {'type': self.status}},
                'turns': [{'id': 't1', 'status': self.turn, 'items': []}][:arguments['turnLimit']], 'page': {}}


class LiveStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def session(self, bridge, number=1, bot='verifier'):
        session = NativePersonal(Path(self.tmp.name) / bot, bot, TID.format(number), bridge, start_worker=False)
        self.addCleanup(session.close)
        return session

    def inspect(self, *sessions):
        class Holder:
            controllers = {s.bot: s for s in sessions}
            native_bindings = {}
            native_bridge = None

        return office_desktop.inspect_shutdown(Controller(), Holder(), None, None)

    def test_history_falls_back_to_smaller_reads_when_desktop_says_too_large(self):
        bridge = LongThreadBridge()
        state = self.session(bridge).state()
        self.assertEqual(state['native_thread_status'], 'active')
        self.assertEqual(bridge.calls[0], (10, True))
        self.assertLess(len(bridge.calls), 5)

    def test_open_session_of_a_long_thread_is_safe_and_reads_no_history(self):
        bridge = LongThreadBridge()
        state = self.inspect(self.session(bridge))
        self.assertEqual(state['safety'], 'safe')
        self.assertEqual(bridge.calls, [(1, False)])

    def test_unreadable_chat_is_named_with_a_recovery_path(self):
        class Down:
            def call(self, name, arguments):
                raise Uncertain('Desktop relay unavailable or request outcome unconfirmed; do not resend')

        session = self.session(LongThreadBridge())
        session.bridge = Down()
        state = self.inspect(session)
        self.assertEqual(state['safety'], 'unknown')
        self.assertIn('Chat verifier', state['reason'])
        self.assertIn('retry Quit', state['reason'])

    def test_busy_delivery_is_named_by_chat_and_id(self):
        session = self.session(LongThreadBridge('idle', 'completed'))
        session.db.execute("INSERT INTO outbox(id,client_id,text,state) VALUES('delivery-77','c1','hi','accepted')")
        session.db.commit()
        state = self.inspect(session)
        self.assertEqual(state['safety'], 'busy')
        self.assertIn('Chat verifier', state['reason'])
        self.assertIn('delivery-77', state['reason'])

    def test_ambiguous_delivery_says_how_to_settle_it(self):
        session = self.session(LongThreadBridge('idle', 'completed'))
        session.db.execute("INSERT INTO outbox(id,client_id,text,state) VALUES('delivery-78','c1','hi','ambiguous')")
        session.db.commit()
        state = self.inspect(session)
        self.assertEqual(state['safety'], 'unknown')
        self.assertIn('Resume', state['reason'])

    def test_office_turn_in_the_room_controller_is_named(self):
        class Busy(Controller):
            active = {'id': 'turn'}
            lock = threading.RLock()

        state = office_desktop.inspect_shutdown(Busy())
        self.assertEqual(state['safety'], 'busy')
        self.assertIn('an Office turn', state['reason'])
        self.assertIn('retry Quit', state['reason'])

    def test_relay_maps_desktop_size_refusals_to_too_large(self):
        class Refusing:
            origin = property(lambda self: work.MANAGER)  # read at call time: other tests reconfigure the project

            def call(self, name, arguments):
                raise RuntimeError('Tool response exceeds the size limit. Request fewer turns or omit outputs.')

        root = Path(self.tmp.name) / 'relay'
        server = create_relay(Refusing(), root, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(timeout=5.0)))
        with self.assertRaises(TooLarge):
            RelayBridge(root).call('read_thread', {'threadId': work.MANAGER, 'hostId': 'local', 'turnLimit': 10,
                                                   'includeOutputs': True, 'maxOutputCharsPerItem': 20000})


if __name__ == '__main__':
    unittest.main()
