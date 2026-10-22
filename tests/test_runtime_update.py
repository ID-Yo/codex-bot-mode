import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from scripts import runtime_update as runtime


class FakeOwnedService:
    def __init__(self):
        self.readiness = {'ownership':'owned','state':'idle','paused':False,'pending_writes':False}
        self.calls = []
        self.fail_next_health = False
        self.current = None
        self.on_stop = None
        self.on_status = None
        self.on_health = None
        self.stop_results = []

    def status(self):
        self.calls.append(('status',))
        if self.on_status:self.on_status()
        return dict(self.readiness)

    def stop_owned(self):
        self.calls.append(('stop_owned',))
        if self.on_stop:self.on_stop()
        if self.stop_results:
            result=self.stop_results.pop(0)
            if isinstance(result,Exception):raise result
            return result
        return True

    def start_owned(self, slot):
        self.calls.append(('start_owned',str(slot)))
        self.current = Path(slot)
        return True

    def health_check(self, slot, release_id, slot_sha256):
        self.calls.append(('health_check',str(slot)))
        if self.fail_next_health:
            self.fail_next_health = False
            report={'healthy':False,'release_id':release_id,'slot_sha256':slot_sha256}
        else:
            report={'healthy':True,'release_id':release_id,'slot_sha256':slot_sha256}
        if self.on_health:self.on_health(slot,release_id,slot_sha256)
        return report


class RuntimeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'owned-runtime'
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        (self.project/'STATE.md').write_text('must not change',encoding='utf-8')
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        self.trust = {'schema':1,'keys':{'test':base64.b64encode(public).decode()},'revoked':[],
                      'component_paths':{'office':['bin/office.exe'],'helper':['bin/helper.py']}}
        self.now = datetime(2026,10,21,tzinfo=timezone.utc)
        runtime.initialize(self.root,'codex-office')

    def tearDown(self):
        self.temp.cleanup()

    def release(self, version='1.0.0', kind='application', component_id='office', body=None, release_id=None):
        body = body if body is not None else f'{component_id}-{version}'.encode()
        path = 'bin/office.exe' if component_id == 'office' else 'bin/helper.py'
        payload={'schema':1,'release_id':release_id or f'release-{version.replace(".","-")}',
                 'source_revision':hashlib.sha1(f'{component_id}-{version}'.encode()).hexdigest(),
                 'published_at':'2026-01-01T00:00:00Z','expires_at':'2027-01-01T00:00:00Z',
                 'minimum_engine':1,'changelog':'Synthetic fixture',
                 'components':[{'id':component_id,'kind':kind,'version':version,'platforms':['windows-x86_64'],
                                'requires':{},'files':{path:{'sha256':hashlib.sha256(body).hexdigest(),'size':len(body)}}}]}
        envelope={'payload':payload,'key_id':'test',
                  'signature':base64.b64encode(self.key.sign(runtime.release.canonical(payload))).decode()}
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr(f'{component_id}/{path}',body)
        archive.seek(0)
        bundle=Path(self.temp.name)/f'{payload["release_id"]}.zip'
        bundle.write_bytes(archive.read())
        return envelope,bundle

    def prepare(self, version='1.0.0', **kwargs):
        envelope,bundle=self.release(version,**kwargs)
        return runtime.prepare(self.root,envelope,bundle,self.trust,'windows-x86_64',
                               [envelope['payload']['components'][0]['id']],now=self.now)

    def activate(self, plan, service=None):
        service = service or FakeOwnedService()
        return runtime.activate(self.root,plan,plan['plan_sha256'],service)

    def test_prepare_verifies_then_stages_under_owned_version_store(self):
        plan=self.prepare()
        slot=self.root/plan['components'][0]['slot']
        self.assertEqual((slot/'bin/office.exe').read_bytes(),b'office-1.0.0')
        self.assertTrue((slot/'slot.json').is_file())
        self.assertFalse(runtime.current(self.root)['components'])
        self.assertEqual((self.project/'STATE.md').read_text(), 'must not change')

    def test_busy_paused_and_unknown_runtime_defer_without_switch(self):
        for readiness in ({'ownership':'owned','state':'busy','paused':False,'pending_writes':False},
                          {'ownership':'owned','state':'idle','paused':True,'pending_writes':False},
                          {'ownership':'unknown','state':'idle','paused':False,'pending_writes':False},
                          {'ownership':'owned','state':'unknown','paused':False,'pending_writes':False}):
            plan=self.prepare(release_id='release-'+str(len(readiness))+str(readiness['state']))
            service=FakeOwnedService();service.readiness=readiness
            result=self.activate(plan,service)
            self.assertEqual(result['status'],'deferred')
            self.assertFalse(any(c[0] in ('stop_owned','start_owned') for c in service.calls))
            self.assertFalse(runtime.current(self.root)['components'])

    def test_healthy_activation_switches_current_pointer_after_exact_approval(self):
        plan=self.prepare()
        service=FakeOwnedService()
        result=self.activate(plan,service)
        self.assertEqual(result['status'],'activated')
        entry=runtime.current(self.root)['components']['office']
        self.assertEqual(entry['version'],'1.0.0')
        self.assertEqual(service.current,self.root/entry['slot'])

    def test_failed_health_restores_previous_pointer_and_runtime(self):
        service=FakeOwnedService()
        first=self.prepare('1.0.0')
        self.assertEqual(self.activate(first,service)['status'],'activated')
        old=runtime.current_bytes(self.root)
        second=self.prepare('1.1.0')
        service.fail_next_health=True
        result=self.activate(second,service)
        self.assertEqual(result['status'],'rolled_back')
        self.assertEqual(runtime.current_bytes(self.root),old)
        self.assertEqual(runtime.current(self.root)['components']['office']['version'],'1.0.0')
        self.assertEqual(sum(c[0]=='start_owned' for c in service.calls),3)

    def test_pointer_change_during_safe_stop_resumes_the_winning_slot(self):
        service=FakeOwnedService()
        first=self.prepare('1.0.0');self.activate(first,service)
        second=self.prepare('1.1.0')
        prior=runtime.current(self.root)
        def competing_pointer_write():
            data=runtime.current(self.root);data['concurrent_writer']='winner'
            runtime._atomic(self.root,runtime.CURRENT_FILE,runtime.canonical(data)+b'\n')
        service.on_stop=competing_pointer_write
        result=self.activate(second,service)
        self.assertEqual(result['status'],'deferred')
        self.assertEqual(result['reason'],'current_pointer_changed_during_activation')
        self.assertEqual(runtime.current(self.root)['concurrent_writer'],'winner')
        self.assertEqual(service.current,self.root/prior['components']['office']['slot'])
        self.assertNotEqual(service.current,self.root/second['application']['slot'])

    def test_adapter_callback_cannot_mutate_approved_plan_target(self):
        plan=self.prepare()
        service=FakeOwnedService()
        expected=self.root/plan['application']['slot']
        expected_slot=plan['application']['slot']
        def mutate_caller_plan():
            plan['application']['slot']='../outside/unauthorized-slot'
            plan['components'][0]['slot']='../outside/unauthorized-slot'
        service.on_status=mutate_caller_plan
        result=runtime.activate(self.root,plan,plan['plan_sha256'],service)
        self.assertEqual(result['status'],'activated')
        self.assertEqual(service.current,expected)
        self.assertEqual(runtime.current(self.root)['components']['office']['slot'],expected_slot)

    def test_unconfirmed_candidate_stop_never_claims_rollback_or_starts_old_slot(self):
        for case,cleanup_result in (('false',False),('exception',RuntimeError('stop failed'))):
            with self.subTest(case=case):
                root=Path(self.temp.name)/case
                runtime.initialize(root,'codex-office')
                service=FakeOwnedService()
                envelope,bundle=self.release('1.0.0',release_id=f'old-{case}')
                first=runtime.prepare(root,envelope,bundle,self.trust,'windows-x86_64',['office'],now=self.now)
                runtime.activate(root,first,first['plan_sha256'],service)
                old_pointer=runtime.current_bytes(root)
                envelope,bundle=self.release('1.1.0',release_id=f'new-{case}')
                second=runtime.prepare(root,envelope,bundle,self.trust,'windows-x86_64',['office'],now=self.now)
                service.fail_next_health=True
                service.stop_results=[True,cleanup_result]
                result=runtime.activate(root,second,second['plan_sha256'],service)
                self.assertEqual(result['status'],'recovery_required')
                self.assertEqual(result['reason'],'candidate_stop_unconfirmed')
                self.assertEqual(runtime.current_bytes(root),old_pointer)
                self.assertEqual(service.current,root/second['application']['slot'])
                self.assertEqual(sum(call[0]=='start_owned' for call in service.calls),2)

    def test_competing_pointer_commit_before_activation_commit_is_preserved(self):
        service=FakeOwnedService()
        first=self.prepare('1.0.0');self.activate(first,service)
        second=self.prepare('1.1.0')
        original=runtime.current(self.root)
        committed=[]
        def competing_commit(slot,release_id,slot_hash):
            if committed:return
            winner=runtime.current(self.root)
            winner['concurrent_writer']='winner'
            expected=runtime.current_digest(self.root)
            self.assertTrue(runtime.commit_current(self.root,expected,winner))
            committed.append(True)
        service.on_health=competing_commit
        result=self.activate(second,service)
        self.assertEqual(result['status'],'deferred')
        self.assertEqual(result['reason'],'current_pointer_changed_during_activation')
        pointer=runtime.current(self.root)
        self.assertEqual(pointer['concurrent_writer'],'winner')
        self.assertEqual(pointer['components']['office']['slot'],original['components']['office']['slot'])
        self.assertEqual(service.current,self.root/original['components']['office']['slot'])

    def test_interrupted_staging_is_inactive_and_recoverable(self):
        envelope,bundle=self.release()
        with patch.object(runtime,'_write_release_file',side_effect=runtime.SimulatedCrash('power loss')):
            with self.assertRaises(runtime.SimulatedCrash):
                runtime.prepare(self.root,envelope,bundle,self.trust,'windows-x86_64',['office'],now=self.now)
        self.assertFalse(runtime.current(self.root)['components'])
        self.assertTrue(list((self.root/'versions').rglob('.staging-*')))
        self.assertEqual(runtime.recover(self.root)['removed_staging'],1)
        self.assertFalse(list((self.root/'versions').rglob('.staging-*')))

    def test_double_apply_is_noop_and_does_not_restart_service(self):
        plan=self.prepare();service=FakeOwnedService()
        self.activate(plan,service)
        calls=len(service.calls)
        self.assertEqual(self.activate(plan,service)['status'],'already_active')
        self.assertEqual(len(service.calls),calls)

    def test_update_lock_rejects_overlapping_prepare(self):
        envelope,bundle=self.release()
        with runtime._lock(self.root):
            with self.assertRaisesRegex(runtime.RuntimeUpdateError,'Another runtime update is active'):
                runtime.prepare(self.root,envelope,bundle,self.trust,'windows-x86_64',['office'],now=self.now)
        self.assertFalse(runtime.current(self.root)['components'])

    def test_downgrade_requires_explicit_rollback_plan_hash(self):
        service=FakeOwnedService()
        first=self.prepare('1.0.0');self.activate(first,service)
        newer=self.prepare('1.1.0');self.activate(newer,service)
        with self.assertRaisesRegex(runtime.release.ReleaseError,'Downgrade needs'):
            self.prepare('1.0.0',release_id='older-release')
        entry=first['components'][0]
        rollback_plan=runtime.rollback_plan(self.root,'office',entry['slot'])
        with self.assertRaises(runtime.ApprovalRequired):
            runtime.rollback(self.root,rollback_plan,'wrong-hash',service)
        self.assertEqual(runtime.current(self.root)['components']['office']['version'],'1.1.0')
        result=runtime.rollback(self.root,rollback_plan,rollback_plan['plan_sha256'],service)
        self.assertEqual(result['status'],'rolled_back')
        self.assertEqual(runtime.current(self.root)['components']['office']['version'],'1.0.0')

    def test_tool_only_activation_does_not_restart_application(self):
        envelope,bundle=self.release('1.0.0','tool','helper')
        plan=runtime.prepare(self.root,envelope,bundle,self.trust,'windows-x86_64',['helper'],now=self.now)
        service=FakeOwnedService()
        result=self.activate(plan,service)
        self.assertEqual(result['status'],'activated')
        self.assertFalse(any(c[0] in ('stop_owned','start_owned','health_check') for c in service.calls))

    def test_symlinked_owned_root_and_slot_escape_are_rejected(self):
        other=Path(self.temp.name)/'outside';other.mkdir()
        link=Path(self.temp.name)/'linked-runtime'
        try:link.symlink_to(other,target_is_directory=True)
        except (OSError,NotImplementedError):self.skipTest('Directory symlinks unavailable')
        with self.assertRaises(runtime.RuntimeUpdateError):
            runtime.prepare(link,*self.release(),self.trust,'windows-x86_64',['office'],now=self.now)
        parent_link=Path(self.temp.name)/'linked-parent'
        try:parent_link.symlink_to(self.temp.name,target_is_directory=True)
        except (OSError,NotImplementedError):self.skipTest('Directory symlinks unavailable')
        with self.assertRaisesRegex(runtime.RuntimeUpdateError,'pass through a symlink'):
            runtime.initialize(parent_link/'another-runtime','codex-office')
        plan=self.prepare();slot=self.root/plan['components'][0]['slot']
        (slot/'bin/office.exe').unlink()
        (slot/'bin/office.exe').symlink_to(other/'foreign.exe')
        with self.assertRaises(runtime.RuntimeUpdateError):
            runtime.activate(self.root,plan,plan['plan_sha256'],FakeOwnedService())

    def test_activation_never_targets_processes_outside_owned_adapter_contract(self):
        plan=self.prepare();service=FakeOwnedService()
        self.activate(plan,service)
        self.assertEqual({call[0] for call in service.calls},{'status','stop_owned','start_owned','health_check'})

    def test_cli_prepare_and_activate_without_host_adapter_stays_deferred(self):
        envelope,bundle=self.release()
        manifest=Path(self.temp.name)/'manifest.json';manifest.write_text(json.dumps(envelope),encoding='utf-8')
        trust=Path(self.temp.name)/'trust.json';trust.write_text(json.dumps(self.trust),encoding='utf-8')
        from contextlib import redirect_stdout
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(runtime.main(['prepare','--install-root',str(self.root),'--manifest',str(manifest),
                                           '--bundle',str(bundle),'--trust',str(trust),'--platform','windows-x86_64',
                                           '--component','office']),0)
        plan=json.loads(output.getvalue())['plan']
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(runtime.main(['activate','--install-root',str(self.root),'--plan',plan['plan_sha256'],
                                           '--approve',plan['plan_sha256']]),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'deferred')
        self.assertFalse(runtime.current(self.root)['components'])


if __name__ == '__main__':
    unittest.main()
