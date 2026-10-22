"""Manage only an Office child launched from a verified owned runtime slot."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import time
from urllib.parse import urlsplit

from scripts import runtime_update
from scripts.office_desktop import verify_runtime_slot


class OfficeRuntimeAdapter:
    PORT = 24242
    COMPONENT = 'office'
    ENTRYPOINT = 'scripts/team_room/__main__.py'

    def __init__(self, install_root, project_root, codex_path, *, model='gpt-6.1-sol',
                 port=PORT, runtime_root=None, enable_work=False, desktop_relay=False, startup_timeout=45,
                 shutdown_timeout=65):
        self.install_root=runtime_update._root(install_root)
        self.project_root=Path(project_root).resolve(strict=True)
        self.codex_path=Path(codex_path).resolve(strict=True)
        if not self.codex_path.is_file():raise ValueError('Codex CLI must be an existing configured file')
        if type(port) is not int or not 1<=port<=65535:raise ValueError('Office loopback port is invalid')
        self.model=model;self.port=port;self.enable_work=bool(enable_work);self.desktop_relay=bool(desktop_relay)
        self.startup_timeout=startup_timeout;self.shutdown_timeout=shutdown_timeout
        allowed=(self.project_root/'.bot-mode/team-room-v1').resolve()
        self.runtime_root=Path(runtime_root).absolute() if runtime_root else allowed/'office-runtime'/'active'
        if not self.runtime_root.resolve().is_relative_to(allowed):raise ValueError('Office runtime root must be project-owned')
        if getattr(sys,'frozen',False):raise ValueError('Source-slot adapter requires a Python host; native packaging is unsupported')
        self.python=Path(sys.executable).resolve(strict=True)
        if not self.python.is_file():raise ValueError('Trusted existing Office Python host is unavailable')
        self.process=None;self.identity=None;self.instance_id=None;self._stopped=False
        self._stdout=None;self._stderr=None

    def _slot_identity(self, slot_path):
        supplied=Path(slot_path).absolute()
        try:relative=supplied.relative_to(self.install_root).as_posix()
        except ValueError as error:raise ValueError('Office slot is outside the owned runtime root') from error
        try:slot=runtime_update._safe_path(self.install_root,relative,missing=False).resolve(strict=True)
        except runtime_update.RuntimeUpdateError as error:raise ValueError('Office slot path is not a safe owned path') from error
        parts=Path(relative).parts
        if len(parts)<3 or parts[:2]!=('versions',self.COMPONENT):
            raise ValueError('Office slot is outside the owned application component')
        meta,slot_hash=runtime_update._verify_slot(self.install_root,relative)
        identity=verify_runtime_slot(self.install_root,relative,meta.get('release_id'),slot_hash,
                                     self.COMPONENT,slot/self.ENTRYPOINT)
        return relative,slot,identity

    def start_owned(self, slot_path):
        if self.process is not None and self.process.poll() is None:return False
        relative,slot,identity=self._slot_identity(slot_path)
        with socket.socket() as probe:
            probe.settimeout(.2)
            if probe.connect_ex(('127.0.0.1',self.port))==0:
                raise ValueError('Office port is occupied; refusing to manage its listener')
        instance_root=self.runtime_root
        allowed=(self.project_root/'.bot-mode/team-room-v1').resolve()
        cursor=self.project_root
        for part in instance_root.relative_to(self.project_root).parts:
            cursor=cursor/part
            if cursor.is_symlink() or (cursor.exists() and bool(getattr(cursor.lstat(),'st_file_attributes',0)&0x400)):
                raise ValueError('Office data root cannot traverse a link')
        if not instance_root.resolve().is_relative_to(allowed.resolve()):
            raise ValueError('Office data root escapes its project boundary')
        instance_root.mkdir(parents=True,exist_ok=True)
        if instance_root.is_symlink():raise ValueError('Office data root cannot be linked')
        runtime=instance_root
        logs=[runtime/'office.stdout.log',runtime/'office.stderr.log']
        for log in logs:
            runtime_update._safe_path(self.project_root,log.relative_to(self.project_root).as_posix())
            if not log.resolve().is_relative_to(runtime.resolve(strict=True)):
                raise ValueError('Office log escapes its owned runtime boundary')
        bootstrap=('import runpy,sys; root=sys.argv[1]; args=sys.argv[2:]; '
                   'sys.path.insert(0,root); sys.argv=["scripts.team_room",*args]; '
                   'runpy.run_module("scripts.team_room",run_name="__main__")')
        command=[str(self.python),'-I','-B','-c',bootstrap,str(slot),
                 '--codex-path',str(self.codex_path),'--model',str(self.model),'--port',str(self.port),
                 '--runtime-root',str(runtime),'--desktop-managed','--runtime-install-root',str(self.install_root),
                 '--runtime-slot',relative,'--runtime-release-id',identity['runtime_release_id'],
                 '--runtime-slot-sha256',identity['runtime_slot_sha256'],
                 '--runtime-component-id',identity['runtime_component_id'],'--project',str(self.project_root)]
        if self.enable_work:command.append('--enable-work')
        if self.desktop_relay:command.append('--desktop-relay')
        env={key:value for key,value in os.environ.items() if not key.upper().startswith('PYTHON')}
        self._close_logs()
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        try:
            self._stdout=logs[0].open('ab');self._stderr=logs[1].open('ab')
            self.process=subprocess.Popen(command,cwd=slot,env=env,stdin=subprocess.DEVNULL,
                stdout=self._stdout,stderr=self._stderr,creationflags=flags,shell=False,close_fds=True)
        except BaseException:
            self._close_logs();raise
        self.identity={**identity,'pid':self.process.pid};self.instance_id=None;self._stopped=False
        deadline=time.monotonic()+self.startup_timeout
        while time.monotonic()<deadline:
            if self.process.poll() is not None:return False
            descriptor=self._descriptor()
            if descriptor and self._matches(descriptor):
                return self.health_check(slot,identity['runtime_release_id'],identity['runtime_slot_sha256']).get('healthy') is True
            time.sleep(.05)
        return False

    def _descriptor(self):
        if self.runtime_root is None:return None
        allowed=(self.project_root/'.bot-mode/team-room-v1').resolve()
        try:
            if not self.runtime_root.resolve(strict=True).is_relative_to(allowed):return None
        except OSError:return None
        cursor=self.runtime_root
        while cursor!=self.project_root:
            if cursor.is_symlink() or (cursor.exists() and bool(getattr(cursor.lstat(),'st_file_attributes',0)&0x400)):
                return None
            if cursor==cursor.parent:return None
            cursor=cursor.parent
        path=self.runtime_root/'desktop.json'
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size>65536:return None
            value=json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value,dict) else None
        except (OSError,ValueError):return None

    def _matches(self,value):
        if (self.process is None or self.identity is None or self.process.poll() is not None
                or value.get('protocol')!=1 or value.get('service')!='codex-office'
                or value.get('state')!='running' or value.get('pid')!=self.process.pid
                or value.get('parent_pid')!=os.getpid() or not isinstance(value.get('instance_id'),str)
                or not value.get('instance_id')):
            return False
        if self.instance_id is not None and value['instance_id']!=self.instance_id:return False
        matches=(value.get('port')==self.port
            and all(value.get(key)==self.identity[key] for key in ('runtime_release_id','runtime_slot_sha256',
                'runtime_slot','runtime_component_id','runtime_version','runtime_entrypoint','runtime_owner')))
        if matches and self.instance_id is None:self.instance_id=value['instance_id']
        return matches

    def _request(self,path,method='GET',timeout=4):
        descriptor=self._descriptor()
        if (not descriptor or not self._matches(descriptor)
                or not isinstance(descriptor.get('token'),str) or not descriptor['token']):return None,None
        parsed=urlsplit('http://127.0.0.1:'+str(self.port)+path)
        connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=timeout)
        try:
            connection.request(method,parsed.path,headers={'X-Office-Controller-Token':descriptor['token']})
            response=connection.getresponse();raw=response.read(1024*1024)
            try:body=json.loads(raw) if raw else {}
            except ValueError:return response.status,None
            return response.status,body if isinstance(body,dict) else None
        except (OSError,http.client.HTTPException):return None,None
        finally:connection.close()

    def status(self):
        if self.process is None or self.identity is None:
            return {'ownership':'unknown','state':'unknown','paused':None,'pending_writes':None}
        if self.process.poll() is not None:
            return {'ownership':'owned' if self._stopped else 'unknown',
                    'state':'idle' if self._stopped else 'unknown',
                    'paused':False if self._stopped else None,'pending_writes':False if self._stopped else None}
        status,body=self._request('/desktop/status')
        if status!=200 or not body:return {'ownership':'unknown','state':'unknown','paused':None,'pending_writes':None}
        if not self._matches(body):return {'ownership':'unknown','state':'unknown','paused':None,'pending_writes':None}
        api_status,api=self._request('/api/state')
        if api_status!=200 or not api or type(api.get('hold')) not in (bool,int) or api['hold'] not in (0,1):
            return {'ownership':'owned','state':'unknown','paused':None,'pending_writes':None}
        safety=body.get('safety')
        state='idle' if safety=='safe' and body.get('state')=='running' else 'busy' if safety=='busy' else 'unknown'
        return {'ownership':'owned','state':state,'paused':bool(api['hold']),
            'pending_writes':False if safety=='safe' else None,
            'runtime_release_id':self.identity['runtime_release_id'],
            'runtime_slot_sha256':self.identity['runtime_slot_sha256'],'instance_id':body['instance_id']}

    def stop_owned(self):
        if self._stopped:return self.process is not None and self.process.poll() is not None
        state=self.status()
        if state.get('ownership')!='owned' or state.get('state')!='idle' or state.get('paused') is not False:
            return False
        status,body=self._request('/desktop/shutdown','POST',timeout=self.shutdown_timeout+5)
        if (status==200 and body and body.get('state')=='stopped' and body.get('safety')=='safe'):
            pass
        elif not (status==202 and body and body.get('stop_pending') is True):
            return False
        try:self.process.wait(timeout=self.shutdown_timeout)
        except subprocess.TimeoutExpired:return False
        descriptor=self._descriptor()
        self._stopped=(self.process.poll() is not None and self.process.returncode==0
                       and descriptor is not None and descriptor.get('state')=='stopped'
                       and self._matches_stopped(descriptor))
        if self._stopped:self._close_logs()
        return self._stopped

    def _matches_stopped(self,value):
        return (self.process is not None and self.identity is not None and value.get('pid')==self.process.pid
                and value.get('parent_pid')==os.getpid() and value.get('service')=='codex-office'
                and value.get('state')=='stopped'
                and value.get('protocol')==1 and value.get('instance_id')==self.instance_id
                and all(value.get(key)==self.identity[key] for key in ('runtime_release_id','runtime_slot_sha256',
                    'runtime_slot','runtime_component_id','runtime_version','runtime_entrypoint','runtime_owner')))

    def health_check(self,slot_path,release_id,slot_sha256):
        if self.process is None or self.process.poll() is not None or self.identity is None:return {'healthy':False}
        try:relative,slot,identity=self._slot_identity(slot_path)
        except (OSError,ValueError):return {'healthy':False}
        if (identity['runtime_release_id']!=release_id or identity['runtime_slot_sha256']!=slot_sha256
                or any(value!=self.identity.get(key) for key,value in identity.items())):return {'healthy':False}
        status,health=self._request('/desktop/health')
        if status!=200 or not health or not self._matches(health):return {'healthy':False}
        api_status,api=self._request('/api/state')
        healthy=(api_status==200 and isinstance(api,dict) and isinstance(api.get('participants'),list)
            and isinstance(api.get('journal'),list) and isinstance(api.get('limits'),dict))
        return {'healthy':healthy,'release_id':release_id if healthy else None,
                'slot_sha256':slot_sha256 if healthy else None}

    def _close_logs(self):
        for stream in (self._stdout,self._stderr):
            if stream:
                try:stream.close()
                except OSError:pass
        self._stdout=self._stderr=None

    def close(self):
        """Request safe shutdown of only this adapter's child; never kill it."""
        if self.process is not None and self.process.poll() is None:
            self.stop_owned()
        self._close_logs()
