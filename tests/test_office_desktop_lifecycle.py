import json
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts.office_desktop import DesktopLifecycle, inspect_shutdown, set_draining
from scripts.office_desktop import verify_runtime_slot


class FakeStore:
    def __init__(self, deliveries=()):
        self._deliveries = list(deliveries)

    def deliveries(self):
        return self._deliveries


class FakeController:
    def __init__(self, *, active=None, deliveries=(), work=None):
        self.active = active
        self.store = FakeStore(deliveries)
        self.work = work
        self.draining = False
        self.mutex = threading.RLock()


class FakeWork:
    def __init__(self, tasks=(), steps=()):
        self._tasks = list(tasks)
        self._steps = list(steps)

    def tasks(self):
        return self._tasks

    def steps(self, task_id):
        return [step for step in self._steps if step['task_id'] == task_id]


class FakePersonal:
    native_bindings = {}
    native_bridge = None
    controllers = {}

    def _native_root(self, bot, thread_id):
        return Path(tempfile.gettempdir()) / ('cbm-controller-test-' + thread_id)


class DesktopLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def lifecycle(self, **kwargs):
        return DesktopLifecycle(self.root, version='0.1.0', **kwargs)

    def test_health_descriptor_binds_version_pid_port_and_secret_token(self):
        lifecycle = self.lifecycle(pid=4321, instance_id='instance-one', token='private-token')
        self.assertNotIn('runtime_release_id',lifecycle.health())
        lifecycle.mark_running(24242)

        descriptor = json.loads((self.root / 'desktop.json').read_text())
        self.assertEqual(descriptor['protocol'], 1)
        self.assertEqual(descriptor['version'], '0.1.0')
        self.assertEqual(descriptor['pid'], 4321)
        self.assertEqual(descriptor['port'], 24242)
        self.assertEqual(descriptor['instance_id'], 'instance-one')
        self.assertEqual(descriptor['token'], 'private-token')
        self.assertNotIn('private-token', json.dumps(lifecycle.health()))

    def runtime_slot(self):
        install=self.root/'owned';slot_rel='versions/office/1.2.3-release';slot=install/slot_rel
        (slot/'scripts/team_room').mkdir(parents=True)
        (install/'.runtime-owner.json').write_text(json.dumps({'schema':1,'owner':'codex-office'}),encoding='utf-8')
        files={'scripts/team_room/__main__.py':b'ENTRYPOINT',
               'scripts/office_desktop.py':b'LIFECYCLE'}
        entries={}
        for rel,body in files.items():
            path=slot/rel;path.write_bytes(body)
            entries[rel]={'sha256':hashlib.sha256(body).hexdigest(),'size':len(body)}
        meta={'schema':1,'owner':'codex-office','slot':slot_rel,'release_id':'release-1',
              'source_revision':'a'*40,'component':'office','kind':'application','version':'1.2.3',
              'requires':{},'files':entries}
        canonical=json.dumps(meta,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
        (slot/'slot.json').write_bytes(canonical+b'\n')
        return install,slot_rel,hashlib.sha256(canonical).hexdigest(),slot/'scripts/team_room/__main__.py'

    def test_runtime_identity_requires_verified_running_slot_files(self):
        install,slot,slot_hash,entry=self.runtime_slot()
        identity=verify_runtime_slot(install,slot,'release-1',slot_hash,'office',entry)
        self.assertEqual(identity['runtime_release_id'],'release-1')
        self.assertEqual(identity['runtime_slot_sha256'],slot_hash)
        with self.assertRaises(ValueError):
            verify_runtime_slot(install,slot,'release-1','0'*64,'office',entry)
        entry.write_bytes(b'modified entrypoint')
        with self.assertRaises(ValueError):
            verify_runtime_slot(install,slot,'release-1',slot_hash,'office',entry)

    def test_descriptor_and_authenticated_health_expose_only_verified_runtime_identity(self):
        install,slot,slot_hash,entry=self.runtime_slot()
        identity=verify_runtime_slot(install,slot,'release-1',slot_hash,'office',entry)
        lifecycle=DesktopLifecycle(self.root/'runtime',version='1.2.3',runtime_identity=identity,
                                   pid=4321,instance_id='runtime-instance',token='private-token')
        lifecycle.mark_running(24242)
        descriptor=json.loads(lifecycle.descriptor_path.read_text(encoding='utf-8'))
        self.assertEqual(descriptor['runtime_release_id'],'release-1')
        self.assertEqual(descriptor['runtime_slot_sha256'],slot_hash)
        self.assertNotIn('token',lifecycle.health())
        self.assertTrue(lifecycle.matches(lifecycle.health()))
        self.assertFalse(lifecycle.matches({**lifecycle.health(),'runtime_slot_sha256':'0'*64}))

    def test_existing_controller_requires_matching_protocol_version_and_identity(self):
        lifecycle = self.lifecycle(pid=4321, instance_id='instance-one', token='private-token')
        lifecycle.mark_running(24242)

        self.assertTrue(lifecycle.matches({'protocol': 1, 'service': 'codex-office', 'version': '0.1.0',
                                           'pid': 4321, 'port': 24242,
                                           'instance_id': 'instance-one'}))
        self.assertFalse(lifecycle.matches({'protocol': 1, 'service': 'codex-office', 'version': '0.1.0',
                                            'pid': 9999, 'port': 24242,
                                            'instance_id': 'instance-one'}))
        self.assertFalse(lifecycle.matches({'protocol': 0, 'service': 'codex-office', 'version': '0.1.0',
                                            'pid': 4321, 'port': 24242,
                                            'instance_id': 'instance-one'}))
        self.assertFalse(lifecycle.matches({'protocol': 1, 'service': 'codex-office', 'version': 'different',
                                            'pid': 4321, 'port': 24242,
                                            'instance_id': 'instance-one'}))

    def test_http_mutation_admission_is_closed_atomically_during_drain(self):
        lifecycle = self.lifecycle()
        entered = threading.Event()
        release = threading.Event()

        def request():
            with lifecycle.operation():
                entered.set()
                release.wait(2)

        worker = threading.Thread(target=request)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(lifecycle.begin_drain(timeout=0.01))
        self.assertTrue(lifecycle.admit_operation())
        lifecycle.end_operation()
        release.set()
        worker.join(1)
        self.assertTrue(lifecycle.begin_drain(timeout=1))
        self.assertFalse(lifecycle.admit_operation())

    def test_cancel_invalidates_a_drain_waiting_for_inflight_operation(self):
        lifecycle = self.lifecycle()
        self.assertTrue(lifecycle.admit_operation())
        result = []
        worker = threading.Thread(target=lambda: result.append(lifecycle.begin_drain(timeout=1)))
        worker.start()
        deadline = time.monotonic() + 1
        while not lifecycle.draining and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertTrue(lifecycle.draining)

        self.assertTrue(lifecycle.cancel_drain())
        lifecycle.end_operation()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [False])
        self.assertFalse(lifecycle.draining)
        self.assertFalse(lifecycle.stop_requested)

    def test_shutdown_refuses_active_and_unknown_delivery_outcomes(self):
        active = FakeController(active={'id': 'turn-one'})
        result = inspect_shutdown(active, FakePersonal(), None)
        self.assertEqual(result['safety'], 'busy')

        ambiguous = FakeController(deliveries=[{'id': 'delivery-one', 'state': 'ambiguous'}])
        result = inspect_shutdown(ambiguous, FakePersonal(), None)
        self.assertEqual(result['safety'], 'unknown')

    def test_shutdown_waits_for_accepted_work_and_preserves_queued_work(self):
        work = FakeWork(
            tasks=[{'id': 'task-one', 'state': 'developer', 'issue_phase': 'created'}],
            steps=[{'task_id': 'task-one', 'state': 'accepted'}],
        )
        controller = FakeController(work=work)
        result = inspect_shutdown(controller, FakePersonal(), None)
        self.assertEqual(result['safety'], 'busy')

        work._steps[0]['state'] = 'completed'
        result = inspect_shutdown(controller, FakePersonal(), None)
        self.assertEqual(result['safety'], 'safe')
        self.assertEqual(result['pending_count'], 1)

    def test_shutdown_requires_confirmed_native_thread_identity_and_idle_state(self):
        class Bridge:
            def __init__(self, response):
                self.response = response

            def call(self, name, arguments):
                self.asserted = (name, arguments)
                return self.response

        personal = FakePersonal()
        personal.native_bindings = {'manager': 'thread-one'}
        personal.native_bridge = Bridge({'thread': {'id': 'other-thread', 'status': {'type': 'idle'}}})
        self.assertEqual(inspect_shutdown(FakeController(), personal, None)['safety'], 'unknown')

        personal.native_bridge = Bridge({'thread': {'id': 'thread-one', 'status': {'type': 'active'}}})
        # An active turn that Office did not start does not block Stop; Office's own outbox does.
        self.assertEqual(inspect_shutdown(FakeController(), personal, None)['safety'], 'safe')

        personal.native_bridge = Bridge({'thread': {'id': 'thread-one', 'status': {'type': 'idle'}}})
        self.assertEqual(inspect_shutdown(FakeController(), personal, None)['safety'], 'safe')

    def test_unknown_native_binding_without_bridge_never_reports_safe(self):
        personal = FakePersonal()
        personal.native_bindings = {'manager': 'thread-one'}
        personal.native_bridge = None
        self.assertEqual(inspect_shutdown(FakeController(), personal, None)['safety'], 'unknown')

    def test_unknown_open_native_and_work_status_never_report_safe(self):
        class Native:
            thread_id = 'thread-one'
            _history = None

            def state(self):
                return {'personal_thread_id':self.thread_id,'native_thread_status':'unknown',
                        'active':None,'deliveries':[]}

        native = Native()
        self.assertEqual(inspect_shutdown(native, None, None)['safety'], 'unknown')
        work = FakeWork(tasks=[{'id':'task-one','state':'unrecognized','issue_phase':'created'}])
        self.assertEqual(inspect_shutdown(FakeController(work=work), None, None)['safety'], 'unknown')

    def test_drain_closes_all_owned_internal_schedulers(self):
        class Holder:
            def __init__(self, session):
                self.lock = threading.RLock()
                self.controllers = {'owned': session}

        class Native:
            def __init__(self):
                self.lock = threading.RLock()
                self.draining = False

        native = Native()
        personal = Holder(native)
        controller = FakeController()
        set_draining(controller, personal, value=True)
        self.assertTrue(controller.draining)
        self.assertTrue(personal.draining)
        self.assertTrue(native.draining)
        set_draining(controller, personal, value=False)
        self.assertFalse(controller.draining)
        self.assertFalse(personal.draining)
        self.assertFalse(native.draining)

    def test_safe_stop_commit_cannot_race_with_cancel(self):
        lifecycle = self.lifecycle()
        self.assertTrue(lifecycle.begin_drain())
        lifecycle.request_shutdown()
        self.assertTrue(lifecycle.commit_stop_if_safe())
        self.assertFalse(lifecycle.cancel_drain())
        self.assertTrue(lifecycle.draining)
        self.assertTrue(lifecycle.stop_requested)
        self.assertTrue(lifecycle.stop_committed)

    def test_legacy_shutdown_flag_cli_is_removed(self):
        from scripts.team_room import __main__ as entry
        with patch('sys.argv', ['team_room', '--shutdown', str(self.root)]):
            with self.assertRaises(SystemExit) as error:
                entry.main()
        self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / 'shutdown.flag').exists())

    def test_secret_redaction_and_log_retention_are_bounded(self):
        lifecycle = self.lifecycle(token='desktop-secret-token', max_log_lines=3)
        for index in range(6):
            lifecycle.log(f'event {index} bearer abc123 and sk-liveSecret{index}')

        logs = lifecycle.logs()
        self.assertEqual(len(logs), 3)
        combined = '\n'.join(logs)
        self.assertIn('event 5', combined)
        self.assertNotIn('desktop-secret-token', combined)
        self.assertNotIn('abc123', combined)
        self.assertNotIn('liveSecret', combined)

    def test_begin_drain_returns_false_when_already_draining_or_stopped(self):
        lifecycle = self.lifecycle()
        self.assertTrue(lifecycle.begin_drain())
        # Second call while draining returns False
        self.assertIs(lifecycle.begin_drain(), False)
        lifecycle.cancel_drain()
        self.assertFalse(lifecycle.draining)

    def test_begin_drain_returns_false_when_inflight_times_out(self):
        lifecycle = self.lifecycle()
        self.assertTrue(lifecycle.admit_operation())
        # Drain with 0.05s timeout while operation is in-flight returns False
        self.assertIs(lifecycle.begin_drain(timeout=0.05), False)
        self.assertFalse(lifecycle.draining)
        lifecycle.end_operation()

    def test_watch_operation_does_not_block_drain_and_rejects_when_draining(self):
        from scripts.office_desktop import LifecycleBusy
        lifecycle = self.lifecycle()
        # Under watch_operation, inflight is NOT incremented
        with lifecycle.watch_operation():
            self.assertEqual(lifecycle._inflight, 0)
            # Drain succeeds immediately even while watch_operation is active
            gen = lifecycle.begin_drain(timeout=1)
            self.assertIsInstance(gen, int)
            self.assertTrue(lifecycle.draining)
        # Once draining, new watch_operation raises LifecycleBusy
        with self.assertRaises(LifecycleBusy):
            with lifecycle.watch_operation():
                pass


if __name__ == '__main__':
    unittest.main()
