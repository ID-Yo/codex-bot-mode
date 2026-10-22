import base64
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
import json
import subprocess
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from scripts import runtime_update
from scripts.office_runtime_adapter import OfficeRuntimeAdapter
from scripts.component_release import canonical


FAKE_HOST = r'''import argparse,json,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

p=argparse.ArgumentParser()
for n in ('runtime_root','runtime_install_root','runtime_slot','runtime_release_id','runtime_slot_sha256','runtime_component_id','codex_path'):
    p.add_argument('--'+n.replace('_','-'),dest=n,required=True)
p.add_argument('--model',default='test-model');p.add_argument('--project');p.add_argument('--port',type=int,required=True)
p.add_argument('--desktop-managed',action='store_true')
a=p.parse_args()
root=Path(a.runtime_root);root.mkdir(parents=True,exist_ok=True)
token='synthetic-adapter-token';instance='synthetic-'+str(__import__('os').getpid())
descriptor={'protocol':1,'service':'codex-office','version':'test','pid':__import__('os').getpid(),
 'parent_pid':__import__('os').getppid(),'port':a.port,'instance_id':instance,'token':token,'state':'running',
 'runtime_release_id':a.runtime_release_id,'runtime_slot_sha256':a.runtime_slot_sha256,
 'runtime_slot':a.runtime_slot,'runtime_component_id':a.runtime_component_id,
 'runtime_version':'1.2.3','runtime_entrypoint':'scripts/team_room/__main__.py','runtime_owner':'codex-office'}
(root/'desktop.json').write_text(json.dumps(descriptor))
stop=threading.Event()
def controls():
 path=root/'fixture-state.json'
 return json.loads(path.read_text()) if path.exists() else {}
class H(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send(self,code,obj):
  b=json.dumps(obj).encode();self.send_response(code);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def authorized(self):return self.headers.get('X-Office-Controller-Token')==token
 def do_GET(self):
  path=urlsplit(self.path).path
  if not self.authorized():return self.send(403,{})
  public={k:v for k,v in descriptor.items() if k!='token'}
  if path=='/desktop/health':return self.send(200,public)
  if path=='/desktop/status':return self.send(200,{**public,'safety':controls().get('safety','safe'),'busy_count':0,'pending_count':0,'stop_pending':False})
  if path=='/api/state':return self.send(200,{'participants':[],'journal':[],'limits':{'healthy':True},'hold':controls().get('hold',False)})
  return self.send(404,{})
 def do_POST(self):
  if not self.authorized():return self.send(403,{})
  if urlsplit(self.path).path!='/desktop/shutdown':return self.send(404,{})
  descriptor['state']='stopped';(root/'desktop.json').write_text(json.dumps(descriptor))
  obj={**descriptor,'state':'stopped','safety':'safe','stop_pending':False,'pending_count':0}
  self.send(200,obj)
  threading.Thread(target=lambda:(stop.set(),threading.Thread(target=httpd.shutdown).start()),daemon=True).start()
httpd=HTTPServer(('127.0.0.1',a.port),H);httpd.serve_forever()
'''

REAL_HOST = FAKE_HOST.split('root=Path(a.runtime_root)')[0] + r'''
from types import SimpleNamespace
from scripts.office_desktop import DesktopLifecycle,verify_runtime_slot,inspect_shutdown
from scripts.team_room.web import create_server
from scripts.team_room import project as served
served.configure(Path(a.project))
root=Path(a.runtime_root);root.mkdir(parents=True,exist_ok=True)
identity=verify_runtime_slot(a.runtime_install_root,a.runtime_slot,a.runtime_release_id,
                            a.runtime_slot_sha256,a.runtime_component_id,Path(__file__))
lifecycle=DesktopLifecycle(root,version=identity['runtime_version'],runtime_identity=identity)
controller=SimpleNamespace(profile_project=Path(a.project),mutex=threading.RLock(),active=None,work=None,
    store=SimpleNamespace(root=root,deliveries=lambda:[]),
    project_managers=SimpleNamespace(close=lambda:None),read_model_worker=SimpleNamespace(stop=lambda:None))
controller.state=lambda:{'participants':[],'journal':[],'limits':{},'hold':False}
server=create_server(controller,a.port,desktop_lifecycle=lifecycle)
lifecycle.mark_running(server.server_port)
worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
while not lifecycle.stop_requested:__import__('time').sleep(.01)
if inspect_shutdown(controller)['safety']=='safe':lifecycle.commit_stop_if_safe()
server.shutdown();server.server_close();worker.join(2);lifecycle.mark_stopped()
__import__('time').sleep(.1)
'''


class OfficeRuntimeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.install=root/'owned';self.install.mkdir()
        self.project=root/'project';self.project.mkdir()
        self.runtime=self.project/'.bot-mode/team-room-v1/office-runtime'
        runtime_update.initialize(self.install,'codex-office')
        self.codex=root/'codex.exe';self.codex.write_bytes(b'synthetic codex binary')
        self.port=self.free_port()
        self.adapter=None
        self.key=Ed25519PrivateKey.generate()
        public=self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
        self.trust={'schema':1,'keys':{'fixture':base64.b64encode(public).decode()},'revoked':[],
                    'component_paths':{'office':['scripts/__init__.py','scripts/team_room/__init__.py','scripts/team_room/__main__.py']}}
        body=FAKE_HOST.encode()
        payload={'schema':1,'release_id':'office-fixture-release','source_revision':'a'*40,
                 'published_at':'2026-01-01T00:00:00Z','expires_at':'2027-01-01T00:00:00Z',
                 'minimum_engine':1,'changelog':'Synthetic host fixture',
                 'components':[{'id':'office','kind':'application','version':'1.2.3','platforms':['windows-x86_64'],
                  'requires':{},'files':{'scripts/__init__.py':{'sha256':hashlib.sha256(b'').hexdigest(),'size':0},
                    'scripts/team_room/__init__.py':{'sha256':hashlib.sha256(b'').hexdigest(),'size':0},
                    'scripts/team_room/__main__.py':{'sha256':hashlib.sha256(body).hexdigest(),'size':len(body)}}}]}
        self.envelope={'payload':payload,'key_id':'fixture',
                       'signature':base64.b64encode(self.key.sign(canonical(payload))).decode()}
        archive=root/'office.zip'
        import zipfile
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('office/scripts/__init__.py',b'')
            z.writestr('office/scripts/team_room/__init__.py',b'')
            z.writestr('office/scripts/team_room/__main__.py',body)
        plan=runtime_update.prepare(self.install,self.envelope,archive,self.trust,'windows-x86_64',['office'],
                                    now=datetime(2026,10,21,tzinfo=timezone.utc))
        runtime_update.activate(self.install,plan,plan['plan_sha256'])
        self.slot=plan['components'][0]['slot']
        self.slot_sha256=plan['components'][0]['slot_sha256']

    @staticmethod
    def free_port():
        import socket
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));return s.getsockname()[1]

    def make_adapter(self):
        self.adapter=OfficeRuntimeAdapter(self.install,self.project,self.codex,port=self.port,
                                          startup_timeout=5,shutdown_timeout=5)
        def cleanup():
            self.adapter.close()
            child=self.adapter.process
            if child is not None and child.poll() is None:
                # This handle is created by this test, never a discovered/live host.
                child.terminate();child.wait(timeout=5)
        self.addCleanup(cleanup)
        return self.adapter

    def stage_files(self,files,release_id='actual-http-fixture',version='1.2.4'):
        payload={'schema':1,'release_id':release_id,'source_revision':'b'*40,
            'published_at':'2026-01-01T00:00:00Z','expires_at':'2027-01-01T00:00:00Z',
            'minimum_engine':1,'changelog':'Synthetic production HTTP handler fixture',
            'components':[{'id':'office','kind':'application','version':version,'platforms':['windows-x86_64'],
                'requires':{},'files':{rel:{'size':len(body),'sha256':hashlib.sha256(body).hexdigest()}
                                     for rel,body in files.items()}}]}
        envelope={'payload':payload,'key_id':'fixture',
                  'signature':base64.b64encode(self.key.sign(canonical(payload))).decode()}
        trust={**self.trust,'component_paths':{'office':list(files)}}
        import zipfile
        bundle=Path(self.temp.name)/(release_id+'.zip')
        with zipfile.ZipFile(bundle,'w') as archive:
            for rel,body in files.items():archive.writestr('office/'+rel,body)
        return runtime_update.prepare(self.install,envelope,bundle,trust,'windows-x86_64',['office'],
                                      now=datetime(2026,10,21,tzinfo=timezone.utc))

    def test_real_desktop_http_handler_and_lifecycle_work_without_token_echo(self):
        source=Path(__file__).resolve().parents[1]
        files={p.relative_to(source).as_posix():p.read_bytes() for p in (source/'scripts').rglob('*.py')}
        files['scripts/__init__.py']=b''
        files['scripts/team_room/__main__.py']=REAL_HOST.encode()
        plan=self.stage_files(files)
        adapter=self.make_adapter();entry=plan['components'][0];slot=self.install/entry['slot']
        self.assertTrue(adapter.start_owned(slot))
        code,health=adapter._request('/desktop/health')
        self.assertEqual(code,200);self.assertNotIn('token',health)
        self.assertEqual(adapter.status()['state'],'idle')
        self.assertTrue(adapter.health_check(slot,entry['release_id'],entry['slot_sha256'])['healthy'])
        self.assertTrue(adapter.stop_owned())

    def test_runtime_transaction_uses_same_adapter_across_slot_switch(self):
        adapter=self.make_adapter();self.assertTrue(adapter.start_owned(self.install/self.slot))
        first_identity=adapter.instance_id
        plan=self.stage_files({'scripts/__init__.py':b'','scripts/team_room/__init__.py':b'',
                               'scripts/team_room/__main__.py':FAKE_HOST.encode()},version='1.2.3')
        result=runtime_update.activate(self.install,plan,plan['plan_sha256'],adapter)
        self.assertEqual(result['status'],'activated')
        self.assertNotEqual(adapter.instance_id,first_identity)
        self.assertEqual(runtime_update.current(self.install)['components']['office']['slot'],plan['components'][0]['slot'])

    def test_owned_child_reports_manifest_identity_and_office_api_health(self):
        adapter=self.make_adapter()
        slot=self.install/self.slot
        self.assertTrue(adapter.start_owned(slot))
        status=adapter.status()
        self.assertEqual(status['ownership'],'owned')
        self.assertEqual(status['state'],'idle')
        self.assertEqual(status['runtime_release_id'],'office-fixture-release')
        self.assertEqual(status['runtime_slot_sha256'],self.slot_sha256)
        self.assertTrue(adapter.health_check(slot,'office-fixture-release',self.slot_sha256)['healthy'])

    def test_stop_owned_waits_for_authenticated_shutdown_and_exact_child_exit(self):
        adapter=self.make_adapter();adapter.start_owned(self.install/self.slot)
        self.assertTrue(adapter.stop_owned())
        self.assertIsNotNone(adapter.process.poll())
        self.assertEqual(adapter.status()['state'],'idle')

    def test_changed_slot_digest_cannot_attest_the_already_running_child(self):
        adapter=self.make_adapter();slot=self.install/self.slot
        self.assertTrue(adapter.start_owned(slot))
        release_id=adapter.identity['runtime_release_id']
        old_hash=adapter.identity['runtime_slot_sha256']
        self.assertTrue(adapter.health_check(slot,release_id,old_hash)['healthy'])

        # Change local slot contents and metadata, without claiming a newly signed release.
        body=b'# changed after process launch\n'
        (slot/'scripts/__init__.py').write_bytes(body)
        meta_path=slot/'slot.json';meta=json.loads(meta_path.read_bytes())
        meta['files']['scripts/__init__.py']={'sha256':hashlib.sha256(body).hexdigest(),'size':len(body)}
        meta_path.write_bytes(canonical(meta)+b'\n')
        _,new_hash=runtime_update._verify_slot(self.install,self.slot)
        self.assertNotEqual(new_hash,old_hash)
        code,health=adapter._request('/desktop/health')
        self.assertEqual(code,200)
        self.assertEqual(health['runtime_slot_sha256'],old_hash)
        self.assertEqual(adapter.identity['runtime_slot_sha256'],old_hash)
        for requested_hash in (old_hash,new_hash):
            with self.subTest(requested_hash=requested_hash):
                self.assertFalse(adapter.health_check(slot,release_id,requested_hash)['healthy'])
        self.assertIsNone(adapter.process.poll())

    def test_unknown_legacy_or_slot_identity_mismatch_defers(self):
        adapter=self.make_adapter();adapter.start_owned(self.install/self.slot)
        identity=dict(adapter.identity)
        adapter.identity['runtime_slot_sha256']='0'*64
        self.assertFalse(adapter.status()['ownership']=='owned')
        self.assertFalse(adapter.stop_owned())
        adapter.identity=identity
        self.assertTrue(adapter.stop_owned())

    def test_unknown_or_paused_descriptor_never_stops_child(self):
        adapter=self.make_adapter();adapter.start_owned(self.install/self.slot)
        descriptor=adapter._descriptor();descriptor['state']='stopping'
        (adapter.runtime_root/'desktop.json').write_text(__import__('json').dumps(descriptor),encoding='utf-8')
        self.assertEqual(adapter.status()['ownership'],'unknown')
        self.assertFalse(adapter.stop_owned())
        descriptor['state']='running'
        (adapter.runtime_root/'desktop.json').write_text(__import__('json').dumps(descriptor),encoding='utf-8')
        self.assertTrue(adapter.stop_owned())

    def test_busy_unknown_and_paused_runtime_defer_without_signaling_child(self):
        adapter=self.make_adapter();self.assertTrue(adapter.start_owned(self.install/self.slot))
        control=adapter.runtime_root/'fixture-state.json'
        for state in ({'safety':'busy'},{'safety':'unknown'},{'hold':True}):
            with self.subTest(state=state):
                control.write_text(json.dumps(state),encoding='utf-8')
                self.assertFalse(adapter.stop_owned())
                self.assertIsNone(adapter.process.poll())
        control.unlink();self.assertTrue(adapter.stop_owned())

    def test_unlisted_python_bytecode_is_rejected_before_launch(self):
        adapter=self.make_adapter();slot=self.install/self.slot
        (slot/'scripts/team_room/__main__.pyc').write_bytes(b'unverified bytecode')
        with self.assertRaises(ValueError):adapter.start_owned(slot)
        self.assertIsNone(adapter.process)

    def test_changed_non_entrypoint_file_is_rejected_before_launch(self):
        adapter=self.make_adapter();slot=self.install/self.slot
        (slot/'scripts/team_room/__init__.py').write_bytes(b'changed import')
        with self.assertRaises(ValueError):adapter.start_owned(slot)
        self.assertIsNone(adapter.process)

    def test_unattached_adapter_never_claims_or_stops_an_existing_process(self):
        adapter=self.make_adapter()
        self.assertEqual(adapter.status()['ownership'],'unknown')
        self.assertFalse(adapter.stop_owned())
        self.assertIsNone(adapter.process)

    def test_frozen_sidecar_is_not_treated_as_a_python_interpreter(self):
        with patch('scripts.office_runtime_adapter.sys.frozen',True,create=True):
            with self.assertRaisesRegex(ValueError,'native packaging is unsupported'):
                self.make_adapter()

    def test_only_exact_owned_slot_entrypoint_is_spawned(self):
        adapter=self.make_adapter()
        with self.assertRaises(ValueError):
            adapter.start_owned(Path(self.temp.name))
        self.assertIsNone(adapter.process)

    def assert_linked_log_rejected(self,name):
        adapter=self.make_adapter();adapter.runtime_root.mkdir(parents=True)
        outside=Path(self.temp.name)/'outside.log';outside.write_bytes(b'outside sentinel\n')
        link=adapter.runtime_root/name
        try:link.symlink_to(outside)
        except (OSError,NotImplementedError):self.skipTest('File symlinks unavailable')
        self.addCleanup(link.unlink)
        with patch('scripts.office_runtime_adapter.subprocess.Popen',wraps=subprocess.Popen) as spawn:
            with self.assertRaises(ValueError):adapter.start_owned(self.install/self.slot)
            spawn.assert_not_called()
        self.assertIsNone(adapter.process)
        self.assertEqual(outside.read_bytes(),b'outside sentinel\n')
        other='office.stderr.log' if name=='office.stdout.log' else 'office.stdout.log'
        self.assertFalse((adapter.runtime_root/other).exists())

    def test_linked_stdout_rejects_both_logs_before_spawn(self):
        self.assert_linked_log_rejected('office.stdout.log')

    def test_linked_stderr_rejects_both_logs_before_spawn(self):
        self.assert_linked_log_rejected('office.stderr.log')

    def test_linked_slot_alias_is_not_followed(self):
        adapter=self.make_adapter()
        versions=self.install/'versions/office'
        link=versions/'alias'
        try:link.symlink_to(self.install/self.slot,target_is_directory=True)
        except (OSError,NotImplementedError):self.skipTest('Directory symlinks unavailable')
        with self.assertRaises(ValueError):adapter.start_owned(link)
        self.assertIsNone(adapter.process)


if __name__=='__main__':unittest.main()
