"""Launch requests, reminders and escalations leave a durable intent and outcome that shutdown inspection reads."""
import sys as _sys, pathlib as _pl; _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent)); import isolation; isolation.isolate()  # noqa: E402,E401 (#310)
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))  # the service imports the team_room package under this name

import scripts.office_desktop as office_desktop  # noqa: E402
from team_room import escalation, inflight, launch, project, requests as rq  # noqa: E402

LAUNCH_KEY = 'OFFICE-clientid12345'  # launch() derives it from the client id 'client-id-12345'


class Controller:
    closing = False
    active = None
    store = None
    work = None
    mutex = None


class DurableSendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'proj'
        self.project.mkdir()
        patch = mock.patch.object(project, 'PROJECT', self.project)
        patch.start()
        self.addCleanup(patch.stop)

    def safety(self):
        return office_desktop.inspect_shutdown(Controller())['safety']

    def child(self, code):
        """Run code in another process that is killed with os._exit, like a crashed service."""
        head = ('import sys\nsys.path.insert(0, %r)\nfrom team_room import project, inflight, requests as rq\n'
                'project.configure(%r)\n' % (str(ROOT / 'scripts'), str(self.project)))
        done = subprocess.run([sys.executable, '-c', head + code], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def open_request(self, key):
        ledger = rq.load(self.project)
        rq.open_request(ledger, key, 'owner', 'manager', 'Launch team', None, 60)
        rq.save(self.project, ledger, rq.version(rq.load(self.project)))

    def close_request(self, key):
        ledger = rq.load(self.project)
        next(r for r in ledger['requests'] if r['id'] == key)['state'] = 'answered'
        rq.save(self.project, ledger, rq.version(rq.load(self.project)))

    def request(self, key):
        return next(r for r in rq.load(self.project)['requests'] if r['id'] == key)

    def launch(self, bridge):
        summary = {'team': 't', 'section': 's', 'room': 'r', 'director_proposal': None,
                   'work_tracking': {'mode': 'm', 'authority': 'a'}, 'agents': []}
        lead = {'bot': 'manager', 'thread_id': '11111111-2222-3333-4444-000000000008'}
        with mock.patch.object(launch, 'plan', return_value=summary), mock.patch.object(launch.teams, 'require_tracking'), \
                mock.patch.object(launch.served, 'participants', return_value=[lead]):
            return launch.launch(bridge, 'tpl', 'team', {}, 'client-id-12345')

    def test_launch_marks_a_send_in_flight_and_then_is_done(self):
        seen = []
        root = self.project

        class Bridge:
            def call(self, name, arguments):
                seen.append((inflight.count(), [r['state'] for r in inflight.rows(root)]))

        self.assertEqual(self.launch(Bridge())['state'], 'sent')
        self.assertEqual(seen, [(1, ['attempted'])])
        self.assertEqual(inflight.count(), 0)
        self.assertEqual(self.safety(), 'safe')  # delivered: the work continues in the Manager chat

    def inspect(self):
        return office_desktop.inspect_shutdown(Controller())

    def forget_the_request(self, key):
        ledger = rq.load(self.project)
        ledger['requests'] = [r for r in ledger['requests'] if r['id'] != key]
        rq.save(self.project, ledger, rq.version(rq.load(self.project)))

    def test_uncertain_launch_stays_unknown_when_the_ledger_snapshot_lost_the_request(self):
        class Bridge:
            def call(self, name, arguments):
                raise RuntimeError('connection lost')

        self.assertEqual(self.launch(Bridge())['state'], 'uncertain')
        self.forget_the_request(LAUNCH_KEY)
        self.assertEqual(self.safety(), 'unknown')

    def test_crash_while_the_message_was_leaving_stays_unknown_without_a_ledger_request(self):
        self.child('with inflight.track("launch", "OFFICE-gone") as send:\n    send.attempting()\n    import os\n    os._exit(0)\n')
        self.assertEqual(self.safety(), 'unknown')

    def test_launch_that_failed_before_any_message_left_leaves_nothing_unresolved(self):
        class Bridge:
            def call(self, name, arguments):
                raise AssertionError('must not be called')

        with mock.patch.object(launch.rq, 'save', side_effect=RuntimeError('ledger busy')):
            with self.assertRaises(RuntimeError):
                self.launch(Bridge())
        self.assertEqual([r['state'] for r in inflight.rows(self.project)], [])
        self.assertEqual(self.safety(), 'safe')

    def test_reason_names_the_request_and_the_closing_action(self):
        class Bridge:
            def call(self, name, arguments):
                raise RuntimeError('lost')

        self.launch(Bridge())
        state = self.inspect()
        self.assertIn(LAUNCH_KEY, state['reason'])
        self.assertIn('requests.py reply ' + LAUNCH_KEY, state['reason'])
        self.assertNotIn('could not be verified', state['reason'])

    def test_reason_for_an_interrupted_escalation_names_the_request(self):
        self.open_request('REQ-4')
        self.child('with inflight.track("escalation", "REQ-4:reminder", "manager"):\n    import os\n    os._exit(0)\n')
        state = self.inspect()
        self.assertEqual(state['safety'], 'unknown')
        self.assertIn('REQ-4:reminder', state['reason'])
        self.assertIn('next Office escalation pass', state['reason'])

    def test_inspection_is_busy_while_a_send_is_in_flight(self):
        with inflight.track('launch', 'OFFICE-live'):
            self.assertEqual(self.safety(), 'busy')

    def test_uncertain_launch_is_unknown_until_the_request_is_closed(self):
        class Bridge:
            def call(self, name, arguments):
                raise RuntimeError('connection lost')

        self.assertEqual(self.launch(Bridge())['state'], 'uncertain')
        self.assertEqual(self.request(LAUNCH_KEY)['state'], 'open')
        self.assertEqual(self.safety(), 'unknown')
        self.close_request(LAUNCH_KEY)
        self.assertEqual(self.safety(), 'safe')

    def test_crash_after_the_ledger_write_is_unknown_after_a_restart(self):
        self.child('with inflight.track("launch", "OFFICE-crash-1"):\n'
                   '    ledger = rq.load(project.PROJECT)\n'
                   '    rq.open_request(ledger, "OFFICE-crash-1", "owner", "manager", "Launch team", None, 60)\n'
                   '    rq.save(project.PROJECT, ledger, rq.version(rq.load(project.PROJECT)))\n'
                   '    import os\n    os._exit(0)\n')
        self.assertEqual(self.safety(), 'unknown')
        self.close_request('OFFICE-crash-1')
        self.assertEqual(self.safety(), 'safe')

    def test_crash_before_the_ledger_write_leaves_nothing_to_resolve(self):
        self.child('with inflight.track("launch", "OFFICE-never-written"):\n    import os\n    os._exit(0)\n')
        self.assertEqual(self.safety(), 'safe')

    def test_escalation_crash_is_unknown_then_recorded_without_resending(self):
        self.open_request('REQ-9')
        self.child('with inflight.track("escalation", "REQ-9:reminder", "manager"):\n    import os\n    os._exit(0)\n')
        self.assertEqual(self.safety(), 'unknown')
        sent = []

        class Bridge:
            def call(self, name, arguments):
                sent.append(arguments)

        with mock.patch.object(escalation.rq, 'due_steps', return_value=[]):
            escalation.escalate(Bridge())
        self.assertEqual(sent, [])
        self.assertEqual([(e['step'], e['outcome']) for e in self.request('REQ-9')['escalations']], [('reminder', 'uncertain')])
        self.assertEqual(self.safety(), 'safe')

    def escalate_once(self, bridge, **save):
        item = self.request('REQ-8')
        with mock.patch.object(escalation.rq, 'due_steps', return_value=[(item, 'reminder')]), \
                mock.patch.object(escalation.served, 'threads', return_value={'manager': 'thread-manager'}), \
                mock.patch.object(escalation, 'escalation_target', return_value='manager'):
            if save:
                with mock.patch.object(escalation.rq, 'save', side_effect=RuntimeError('stale')):
                    with self.assertRaises(RuntimeError):
                        escalation.escalate(bridge)
                return None
            return escalation.escalate(bridge)

    def test_escalation_send_is_resolved_only_after_the_ledger_save(self):
        self.open_request('REQ-8')
        states = []
        root = self.project

        class Bridge:
            def call(self, name, arguments):
                states.append(([r['state'] for r in inflight.rows(root)], inflight.count()))

        self.assertEqual(self.escalate_once(Bridge())[0][3], 'sent')
        self.assertEqual(states, [(['attempted'], 1)])
        self.assertEqual(inflight.rows(self.project), [])
        self.assertEqual(self.safety(), 'safe')

    def test_failed_ledger_save_keeps_the_send_unresolved_and_the_next_run_records_it(self):
        self.open_request('REQ-8')

        class Bridge:
            def call(self, name, arguments):
                pass

        self.escalate_once(Bridge(), fail=True)
        self.assertEqual(self.safety(), 'unknown')
        with mock.patch.object(escalation.rq, 'due_steps', return_value=[]):
            escalation.escalate(Bridge())
        self.assertEqual([e['outcome'] for e in self.request('REQ-8')['escalations']], ['sent'])
        self.assertEqual(self.safety(), 'safe')
class ProductionNamespaceTests(unittest.TestCase):
    """The service loads scripts.team_room; project status later puts scripts on sys.path and loads team_room too.

    Only scripts.team_room.project is configured with the served project, exactly as in the running Office.
    """

    def setUp(self):
        import importlib
        self.ns = {name: importlib.import_module("scripts.team_room." + name)
                   for name in ("inflight", "launch", "escalation", "project", "requests")}
        for name in ("inflight", "project", "requests"):  # what work_counters triggers
            importlib.import_module("team_room." + name)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        patch = mock.patch.object(self.ns["project"], "PROJECT", self.project)
        patch.start()
        self.addCleanup(patch.stop)

    def safety(self):
        return office_desktop.inspect_shutdown(Controller())["safety"]

    def launch(self, bridge):
        launch = self.ns["launch"]
        summary = {"team": "t", "section": "s", "room": "r", "director_proposal": None,
                   "work_tracking": {"mode": "m", "authority": "a"}, "agents": []}
        lead = {"bot": "manager", "thread_id": "11111111-2222-3333-4444-000000000008"}
        with mock.patch.object(launch, "plan", return_value=summary), mock.patch.object(launch.teams, "require_tracking"), \
                mock.patch.object(launch.served, "participants", return_value=[lead]):
            return launch.launch(bridge, "tpl", "team", {}, "client-id-12345")

    def test_a_launch_in_flight_is_busy_although_both_namespaces_are_loaded(self):
        seen = []

        class Bridge:
            def call(self, name, arguments):
                seen.append(office_desktop.inspect_shutdown(Controller())["safety"])

        self.assertEqual(self.launch(Bridge())["state"], "sent")
        self.assertEqual(seen, ["busy"])
        self.assertEqual(self.safety(), "safe")

    def test_an_uncertain_launch_is_unknown_although_both_namespaces_are_loaded(self):
        class Bridge:
            def call(self, name, arguments):
                raise RuntimeError("lost")

        self.assertEqual(self.launch(Bridge())["state"], "uncertain")
        self.assertEqual(self.safety(), "unknown")

    def test_an_escalation_in_flight_is_busy_although_both_namespaces_are_loaded(self):
        escalation, requests = self.ns["escalation"], self.ns["requests"]
        ledger = requests.load(self.project)
        requests.open_request(ledger, "REQ-5", "owner", "manager", "Ask", None, 60)
        requests.save(self.project, ledger, requests.version(requests.load(self.project)))
        item = next(r for r in requests.load(self.project)["requests"] if r["id"] == "REQ-5")
        seen = []

        class Bridge:
            def call(self, name, arguments):
                seen.append(office_desktop.inspect_shutdown(Controller())["safety"])

        with mock.patch.object(escalation.rq, "due_steps", return_value=[(item, "reminder")]), \
                mock.patch.object(escalation.served, "threads", return_value={"manager": "thread-manager"}), \
                mock.patch.object(escalation, "escalation_target", return_value="manager"):
            escalation.escalate(Bridge())
        self.assertEqual(seen, ["busy"])
        self.assertEqual(self.safety(), "safe")


if __name__ == '__main__':
    unittest.main()
