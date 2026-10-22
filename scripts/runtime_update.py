"""Stage verified CBM runtime releases in owned version slots.

Activation APIs accept only a host-owned service adapter; this module never
launches commands or terminates processes itself.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import component_release as release  # noqa: E402

SCHEMA = 1
OWNER_FILE = '.runtime-owner.json'
CURRENT_FILE = 'current.json'
MAX_BUNDLE = release.MAX_TOTAL + 64 * 1024 * 1024
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


class RuntimeUpdateError(ValueError):
    pass


class ApprovalRequired(RuntimeUpdateError):
    pass


class SimulatedCrash(BaseException):
    """Test-only interruption which leaves a staging directory for recover()."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _owner_id(value):
    if not isinstance(value, str) or not release.ID.fullmatch(value):
        raise RuntimeUpdateError('Runtime owner must be a stable CBM identifier')
    return value


def _linked(path):
    return path.is_symlink() or (path.exists() and bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400))


def _root(path):
    path = Path(path).absolute()
    cursor=path
    while cursor.parent!=cursor:
        if _linked(cursor):
            raise RuntimeUpdateError('Owned runtime root cannot pass through a symlink or junction')
        cursor=cursor.parent
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise RuntimeUpdateError('Owned runtime root must be a directory')
    marker = root / OWNER_FILE
    if _linked(marker) or not marker.is_file():
        raise RuntimeUpdateError('Explicit runtime ownership marker is required')
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise RuntimeUpdateError('Runtime ownership marker is invalid') from error
    if value.get('schema') != SCHEMA or not isinstance(value.get('owner'), str):
        raise RuntimeUpdateError('Runtime ownership marker is invalid')
    _owner_id(value['owner'])
    return root


def initialize(path, owner):
    owner = _owner_id(owner)
    root = Path(path).absolute()
    cursor=root
    while cursor.parent!=cursor:
        if _linked(cursor):
            raise RuntimeUpdateError('Owned runtime root cannot pass through a symlink or junction')
        cursor=cursor.parent
    root.mkdir(parents=True, exist_ok=True)
    marker = root / OWNER_FILE
    if marker.exists():
        owned = _root(root)
        data = json.loads((owned / OWNER_FILE).read_text(encoding='utf-8'))
        if data['owner'] != owner:
            raise RuntimeUpdateError('Runtime root is owned by a different application')
        return {'status':'already_initialized','owner':owner}
    if any(root.iterdir()):
        raise RuntimeUpdateError('Refusing to claim a non-empty runtime directory')
    marker.write_bytes(canonical({'schema':SCHEMA,'owner':owner}) + b'\n')
    (root/'versions').mkdir()
    (root/'.plans').mkdir()
    return {'status':'initialized','owner':owner}


def _safe_path(root, relative, missing=True):
    try:
        release.safe_path(relative)
    except release.ReleaseError as error:
        raise RuntimeUpdateError('Unsafe runtime path') from error
    parts = PurePosixPath(relative).parts
    cursor = root
    for index, part in enumerate(parts):
        cursor = cursor/part
        if _linked(cursor):
            raise RuntimeUpdateError(f'Linked runtime path is forbidden: {relative}')
        if cursor.exists():
            try:
                cursor.resolve(strict=True).relative_to(root)
            except (ValueError,OSError) as error:
                raise RuntimeUpdateError(f'Runtime path escapes owned root: {relative}') from error
            if index < len(parts)-1 and not cursor.is_dir():
                raise RuntimeUpdateError(f'Runtime ancestor is not a directory: {relative}')
        elif not missing and index == len(parts)-1:
            raise RuntimeUpdateError(f'Runtime file is missing: {relative}')
    return root.joinpath(*parts)


@contextmanager
def _exclusive_lock(root,relative):
    lock_path=_safe_path(root,relative)
    handle = lock_path.open('a+b')
    try:
        if handle.seek(0,os.SEEK_END) == 0:
            handle.write(b'0');handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            try: msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as error: raise RuntimeUpdateError('Another runtime update is active') from error
        else:
            import fcntl
            try: fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as error: raise RuntimeUpdateError('Another runtime update is active') from error
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


@contextmanager
def _lock(root):
    with _exclusive_lock(root,'.runtime-update.lock'):
        yield


@contextmanager
def _pointer_lock(root):
    with _exclusive_lock(root,'.current-pointer.lock'):
        yield


def _atomic(root,relative,data):
    root=_root(root)
    path=_safe_path(root,relative)
    path.parent.mkdir(parents=True,exist_ok=True)
    path=_safe_path(root,relative)
    handle, temporary = tempfile.mkstemp(dir=path.parent,prefix=path.name+'-',suffix='.tmp')
    try:
        with os.fdopen(handle,'wb') as out: out.write(data)
        os.replace(temporary,path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def current_bytes(path):
    root=_root(path)
    file=_safe_path(root,CURRENT_FILE)
    return file.read_bytes() if file.is_file() else None


def _validate_pointer(root,data):
    owner=json.loads((root/OWNER_FILE).read_text(encoding='utf-8'))['owner']
    if not isinstance(data,dict) or data.get('schema')!=SCHEMA or data.get('owner')!=owner or not isinstance(data.get('components'),dict):
        raise RuntimeUpdateError('Current runtime pointer is invalid')
    for cid,entry in data['components'].items():
        if not release.ID.fullmatch(cid) or not isinstance(entry,dict) or entry.get('kind') not in ('tool','application'):
            raise RuntimeUpdateError('Current runtime component entry is invalid')
        if not isinstance(entry.get('version'),str) or not VERSION.fullmatch(entry['version']):
            raise RuntimeUpdateError('Current runtime version is invalid')
        if not isinstance(entry.get('requires',{}),dict) or not all(
                isinstance(dep,str) and isinstance(ver,str) and VERSION.fullmatch(ver)
                for dep,ver in entry.get('requires',{}).items()):
            raise RuntimeUpdateError('Current runtime dependencies are invalid')
        slot=entry.get('slot')
        if not isinstance(slot,str) or not slot.startswith(f'versions/{cid}/'):
            raise RuntimeUpdateError('Current runtime slot is invalid')
        meta,_=_verify_slot(root,slot,entry.get('slot_sha256'))
        if (meta.get('owner')!=data['owner'] or meta.get('component')!=cid
                or meta.get('kind')!=entry.get('kind') or meta.get('version')!=entry.get('version')
                or meta.get('release_id')!=entry.get('release_id')):
            raise RuntimeUpdateError('Current pointer does not match its version slot')
    for cid,entry in data['components'].items():
        for dependency,version in entry.get('requires',{}).items():
            if dependency not in data['components'] or data['components'][dependency]['version']!=version:
                raise RuntimeUpdateError(f'{cid}: current pointer has incompatible dependency {dependency}')


def _read_current(root):
    raw=current_bytes(root)
    if raw is None:return None,None
    try:data=json.loads(raw)
    except ValueError as error:raise RuntimeUpdateError('Current runtime pointer is invalid') from error
    _validate_pointer(root,data)
    return data,digest(raw)


def current_digest(path):
    root=_root(path)
    return _read_current(root)[1]


def commit_current(path,expected_sha256,data):
    """Compare-and-swap current.json; every external pointer writer must use this protocol."""
    root=_root(path)
    if expected_sha256 is not None and not re.fullmatch(r'[a-f0-9]{64}',str(expected_sha256)):
        raise RuntimeUpdateError('Expected pointer SHA-256 is invalid')
    with _pointer_lock(root):
        current_data,current_hash=_read_current(root)
        if current_hash!=expected_sha256:
            return False
        _validate_pointer(root,data)
        _atomic(root,CURRENT_FILE,canonical(data)+b'\n')
        return True


def _snapshot_plan(plan):
    try:
        snapshot=json.loads(canonical(plan))
    except (TypeError,ValueError) as error:
        raise RuntimeUpdateError('Runtime plan is not valid immutable JSON data') from error
    if not isinstance(snapshot,dict):
        raise RuntimeUpdateError('Runtime plan must be a JSON object')
    return snapshot


def current(path):
    root=_root(path)
    data,_=_read_current(root)
    return data or {'schema':SCHEMA,'owner':json.loads((root/OWNER_FILE).read_text())['owner'],'components':{}}


def _verify_slot(root, slot, expected_hash=None):
    folder=_safe_path(root,slot,missing=False)
    meta_path=_safe_path(root,slot+'/slot.json',missing=False)
    if not folder.is_dir() or not meta_path.is_file():raise RuntimeUpdateError('Runtime version slot is incomplete')
    try:meta=json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError,ValueError) as error:raise RuntimeUpdateError('Runtime slot manifest is invalid') from error
    actual=digest(canonical(meta))
    if expected_hash is not None and actual!=expected_hash:raise RuntimeUpdateError('Runtime slot identity hash changed')
    if meta.get('schema')!=SCHEMA or meta.get('slot')!=slot or not isinstance(meta.get('files'),dict):
        raise RuntimeUpdateError('Runtime slot manifest is invalid')
    for relative,spec in meta['files'].items():
        file=_safe_path(root,slot+'/'+relative,missing=False)
        if not file.is_file() or file.stat().st_size!=spec.get('size') or _hash_file(file)!=spec.get('sha256'):
            raise RuntimeUpdateError(f'Runtime slot file failed its identity check: {relative}')
    return meta,actual


def _hash_file(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk:=stream.read(1024*1024):value.update(chunk)
    return value.hexdigest()


def _versions(components):
    return {cid:item['version'] for cid,item in components.items()}


def _plan_hash(plan):
    value={k:v for k,v in plan.items() if k!='plan_sha256'}
    return digest(canonical(value))


def _write_release_file(stage, relative, data):
    target=stage.joinpath(*PurePosixPath(relative).parts)
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('xb') as out:out.write(data)


def _frozen_bundle(root,bundle):
    source=Path(bundle)
    if not source.is_file() or source.stat().st_size>MAX_BUNDLE:raise RuntimeUpdateError('Bundle is missing or exceeds its size limit')
    target=_safe_path(root,f'.incoming-{uuid.uuid4().hex}.zip')
    try:
        with source.open('rb') as src,target.open('xb') as dst:
            shutil.copyfileobj(src,dst,1024*1024)
        if target.stat().st_size>MAX_BUNDLE:raise RuntimeUpdateError('Bundle exceeds its size limit')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def prepare(path,envelope,bundle,trust,platform,components,now=None):
    root=_root(path)
    with _lock(root):
        marker=json.loads((root/OWNER_FILE).read_text(encoding='utf-8'))
        active,current_hash=_read_current(root)
        active_components=(active or {}).get('components',{})
        frozen=_frozen_bundle(root,bundle)
        try:
            payload=release.verify_bundle(envelope,frozen,trust,platform,_versions(active_components),now)
            proposed={item['id']:item for item in payload['components']}
            selected=list(components)
            if not selected or len(selected)!=len(set(selected)) or any(cid not in proposed for cid in selected):
                raise RuntimeUpdateError('Select unique components present in the verified release')
            chosen=[proposed[cid] for cid in selected]
            if any(c['kind'] not in ('tool','application') for c in chosen):
                raise RuntimeUpdateError('Only locally allowlisted tool/application components can use runtime slots')
            if sum(c['kind']=='application' for c in chosen)>1:
                raise RuntimeUpdateError('Select at most one application runtime per activation')
            resulting=dict(active_components)
            for component in chosen:
                old=active_components.get(component['id'])
                if old and tuple(map(int,component['version'].split('.'))) < tuple(map(int,old['version'].split('.'))):
                    raise RuntimeUpdateError('Downgrades require a separately approved rollback plan')
                for dependency,version in component['requires'].items():
                    candidate=proposed.get(dependency)
                    installed=active_components.get(dependency)
                    if dependency not in selected and (not installed or installed['version']!=version):
                        raise RuntimeUpdateError(f'{component["id"]}: compatible runtime dependency {dependency} is not active')
                    if dependency in selected and (not candidate or candidate['version']!=version):
                        raise RuntimeUpdateError(f'{component["id"]}: selected release dependency {dependency} is incompatible')
            bundle_hash=_hash_file(frozen)
            for component in chosen:
                slot_id=f"{component['version']}-{payload['release_id']}-{bundle_hash[:12]}"
                slot=f"versions/{component['id']}/{slot_id}"
                meta={'schema':SCHEMA,'owner':marker['owner'],'slot':slot,'release_id':payload['release_id'],
                      'source_revision':payload['source_revision'],'component':component['id'],'kind':component['kind'],
                      'version':component['version'],'requires':component['requires'],'files':component['files']}
                slot_sha=digest(canonical(meta))
                destination=_safe_path(root,slot)
                if destination.exists():
                    _verify_slot(root,slot,slot_sha)
                else:
                    stage=destination.parent/f'.staging-{uuid.uuid4().hex}'
                    stage.mkdir(parents=True)
                    _safe_path(stage,'.stage.json').write_bytes(canonical({'schema':SCHEMA,'owner':marker['owner']})+b'\n')
                    try:
                        with zipfile.ZipFile(frozen) as archive:
                            for relative,spec in sorted(component['files'].items()):
                                data=archive.read(f"{component['id']}/{relative}")
                                if len(data)!=spec['size'] or digest(data)!=spec['sha256']:
                                    raise RuntimeUpdateError('Verified bundle changed during staging')
                                _write_release_file(stage,relative,data)
                        _safe_path(stage,'slot.json').write_bytes(canonical(meta)+b'\n')
                        destination.parent.mkdir(parents=True,exist_ok=True)
                        os.replace(stage,destination)
                    except Exception:
                        shutil.rmtree(stage,ignore_errors=True)
                        raise
                resulting[component['id']]={'kind':component['kind'],'version':component['version'],'slot':slot,
                                            'slot_sha256':slot_sha,'files':component['files'],
                                            'requires':component['requires'],'release_id':payload['release_id']}
            for cid,entry in resulting.items():
                for dependency,version in entry.get('requires',{}).items():
                    if dependency not in resulting or resulting[dependency]['version']!=version:
                        raise RuntimeUpdateError(f'{cid}: resulting runtime set has incompatible dependency {dependency}')
            plan={'schema':SCHEMA,'action':'activate','owner':marker['owner'],
                  'owner_sha256':digest((root/OWNER_FILE).read_bytes()),'current_sha256':current_hash,
                  'release_id':payload['release_id'],'source_revision':payload['source_revision'],
                  'bundle_sha256':bundle_hash,'components':[resulting[cid] | {'id':cid} for cid in selected],
                  'application':next((resulting[cid] | {'id':cid} for cid in selected if resulting[cid]['kind']=='application'),None)}
            plan['plan_sha256']=_plan_hash(plan)
            plans=_safe_path(root,'.plans');plans.mkdir(exist_ok=True)
            _atomic(root,f'.plans/{plan["plan_sha256"]}.json',canonical(plan)+b'\n')
            return plan
        finally:
            frozen.unlink(missing_ok=True)


def _validate_plan(root,plan,approve):
    if not isinstance(plan,dict) or plan.get('schema')!=SCHEMA or plan.get('action') not in ('activate','rollback'):
        raise RuntimeUpdateError('Runtime plan is invalid')
    if not re.fullmatch(r'[a-f0-9]{64}',str(plan.get('plan_sha256',''))) or _plan_hash(plan)!=plan['plan_sha256']:
        raise RuntimeUpdateError('Runtime plan identity hash is invalid')
    if approve!=plan['plan_sha256']:
        raise ApprovalRequired(f'Approve this exact plan SHA-256: {plan["plan_sha256"]}')
    marker=(root/OWNER_FILE).read_bytes()
    if digest(marker)!=plan.get('owner_sha256'):
        raise RuntimeUpdateError('Runtime ownership marker changed after planning')
    owner=json.loads(marker.decode('utf-8'))['owner']
    for component in plan.get('components',[]):
        if (not isinstance(component,dict) or not isinstance(component.get('id'),str)
                or not release.ID.fullmatch(component['id']) or component.get('kind') not in ('tool','application')):
            raise RuntimeUpdateError('Runtime plan component is invalid')
        meta,slot_hash=_verify_slot(root,component.get('slot',''),component.get('slot_sha256'))
        if (meta.get('owner')!=owner or meta.get('component')!=component['id']
                or meta.get('version')!=component.get('version') or meta.get('kind')!=component.get('kind')
                or meta.get('release_id')!=component.get('release_id') or slot_hash!=component.get('slot_sha256')):
            raise RuntimeUpdateError('Prepared runtime slot does not match the approved plan')
    current_data,current_hash=_read_current(root)
    if current_hash==plan.get('current_sha256'):
        return current_data,current_hash
    targets=plan.get('components',[])
    if targets and all(current_data and current_data['components'].get(c['id'],{}).get('slot')==c.get('slot') for c in targets):
        return current_data,current_hash
    raise RuntimeUpdateError('Runtime changed after the approved plan; prepare or plan rollback again')


def _pointer(root,plan,old):
    components=dict((old or {}).get('components',{}))
    for component in plan['components']:
        entry={k:v for k,v in component.items() if k!='id'}
        components[component['id']]=entry
    return {'schema':SCHEMA,'owner':plan['owner'],'release_id':plan['release_id'],
            'source_revision':plan['source_revision'],'components':components}


def _adapter_ready(adapter):
    if adapter is None:return False,'host_adapter_unavailable'
    if not all(callable(getattr(adapter,name,None)) for name in ('status','stop_owned','start_owned','health_check')):
        raise RuntimeUpdateError('Host service adapter does not implement the owned-service contract')
    try:state=adapter.status()
    except Exception:return False,'service_state_unknown'
    if (not isinstance(state,dict) or state.get('ownership')!='owned' or state.get('state')!='idle' or state.get('paused') is not False
            or state.get('pending_writes') is not False):
        return False,'busy_paused_or_unknown'
    return True,None


def _stop_owned_confirmed(adapter):
    try:return adapter.stop_owned() is True
    except Exception:return False


def _resume_pointed_application(root,adapter,plan_hash):
    try:
        pointer=current(root)
        active=next((entry for entry in pointer['components'].values() if entry['kind']=='application'),None)
        if not active:return {'status':'recovery_required','reason':'changed_pointer_has_no_application_slot','plan_sha256':plan_hash}
        slot=root/active['slot']
        started=adapter.start_owned(slot)
        report=adapter.health_check(slot,active['release_id'],active['slot_sha256']) if started is True else None
        if (isinstance(report,dict) and report.get('healthy') is True
                and report.get('release_id')==active['release_id']
                and report.get('slot_sha256')==active['slot_sha256']):
            return {'status':'deferred','reason':'current_pointer_changed_during_activation',
                    'plan_sha256':plan_hash}
    except Exception:
        pass
    return {'status':'recovery_required','reason':'changed_pointed_application_health_unverified',
            'plan_sha256':plan_hash}


def _activate_locked(root,plan,approve,adapter):
    old,old_hash=_validate_plan(root,plan,approve)
    if old and all(old['components'].get(c['id'],{}).get('slot')==c.get('slot') for c in plan['components']):
        return {'status':'already_active','plan_sha256':plan['plan_sha256']}
    new_pointer=_pointer(root,plan,old)
    for cid,entry in new_pointer['components'].items():
        for dependency,version in entry.get('requires',{}).items():
            if dependency not in new_pointer['components'] or new_pointer['components'][dependency]['version']!=version:
                raise RuntimeUpdateError(f'{cid}: activation would leave incompatible dependency {dependency}')
    application=plan.get('application')
    if application is None:
        if not commit_current(root,plan['current_sha256'],new_pointer):
            return {'status':'deferred','reason':'current_pointer_changed','plan_sha256':plan['plan_sha256']}
        return {'status':'activated','plan_sha256':plan['plan_sha256'],'service_restarted':False}
    ready,reason=_adapter_ready(adapter)
    if not ready:return {'status':'deferred','reason':reason,'plan_sha256':plan['plan_sha256']}
    try:
        if adapter.stop_owned() is not True:return {'status':'deferred','reason':'safe_stop_refused','plan_sha256':plan['plan_sha256']}
    except Exception:
        return {'status':'deferred','reason':'safe_stop_failed','plan_sha256':plan['plan_sha256']}
    old_bytes=current_bytes(root)
    old_digest = digest(old_bytes) if old_bytes is not None else None
    if old_digest != plan.get('current_sha256'):
        return _resume_pointed_application(root,adapter,plan['plan_sha256'])
    candidate=root/application['slot']
    healthy=False
    try:
        started=adapter.start_owned(candidate)
        report=adapter.health_check(candidate,plan['release_id'],application['slot_sha256']) if started is True else None
        healthy=(isinstance(report,dict) and report.get('healthy') is True
                 and report.get('release_id')==plan['release_id']
                 and report.get('slot_sha256')==application['slot_sha256'])
    except Exception:
        healthy=False
    if healthy:
        try:committed=commit_current(root,plan['current_sha256'],new_pointer)
        except Exception:committed=False
        if not committed:
            if not _stop_owned_confirmed(adapter):
                return {'status':'recovery_required','reason':'candidate_stop_unconfirmed',
                        'plan_sha256':plan['plan_sha256']}
            return _resume_pointed_application(root,adapter,plan['plan_sha256'])
        return {'status':'rolled_back' if plan['action']=='rollback' else 'activated',
                'plan_sha256':plan['plan_sha256'],'service_restarted':True}
    if not _stop_owned_confirmed(adapter):
        return {'status':'recovery_required','reason':'candidate_stop_unconfirmed',
                'plan_sha256':plan['plan_sha256']}
    if current_bytes(root)!=old_bytes:
        return _resume_pointed_application(root,adapter,plan['plan_sha256'])
    previous=next((v for v in (old or {}).get('components',{}).values() if v['kind']=='application'),None)
    if previous:
        old_slot=root/previous['slot']
        try:
            started=adapter.start_owned(old_slot)
            report=adapter.health_check(old_slot,previous['release_id'],previous['slot_sha256']) if started is True else None
            restored=(isinstance(report,dict) and report.get('healthy') is True
                      and report.get('release_id')==previous['release_id']
                      and report.get('slot_sha256')==previous['slot_sha256'])
        except Exception:restored=False
        if not restored:return {'status':'recovery_required','reason':'previous_runtime_health_unverified','plan_sha256':plan['plan_sha256']}
    return {'status':'rolled_back','reason':'new_runtime_health_check_failed' if plan['action']=='activate' else 'rollback_health_check_failed',
            'plan_sha256':plan['plan_sha256']}


def activate(path,plan,approve,adapter=None):
    root=_root(path)
    plan=_snapshot_plan(plan)
    with _lock(root):return _activate_locked(root,plan,approve,adapter)


def rollback_plan(path,component,slot):
    root=_root(path)
    with _lock(root):
        old,current_hash=_read_current(root)
        if not old or component not in old['components']:
            raise RuntimeUpdateError('Rollback component is not active')
        slot_meta,slot_hash=_verify_slot(root,slot)
        if slot_meta.get('component')!=component:
            raise RuntimeUpdateError('Rollback slot belongs to another component')
        current_entry=old['components'][component]
        if slot==current_entry['slot']:raise RuntimeUpdateError('Rollback target is already active')
        entry={'id':component,'kind':slot_meta['kind'],'version':slot_meta['version'],'slot':slot,
               'slot_sha256':slot_hash,'files':slot_meta['files'],'requires':slot_meta['requires'],
               'release_id':slot_meta['release_id']}
        plan={'schema':SCHEMA,'action':'rollback','owner':json.loads((root/OWNER_FILE).read_text())['owner'],
              'owner_sha256':digest((root/OWNER_FILE).read_bytes()),'current_sha256':current_hash,
              'release_id':slot_meta['release_id'],'source_revision':slot_meta['source_revision'],
              'components':[entry],'application':entry if entry['kind']=='application' else None}
        plan['plan_sha256']=_plan_hash(plan)
        return plan


def rollback(path,plan,approve,adapter=None):
    plan=_snapshot_plan(plan)
    if plan.get('action')!='rollback':raise RuntimeUpdateError('Rollback requires a rollback plan')
    root=_root(path)
    with _lock(root):return _activate_locked(root,plan,approve,adapter)


def recover(path):
    root=_root(path)
    removed=[];unsafe=[]
    with _lock(root):
        versions=_safe_path(root,'versions')
        if versions.is_dir():
            for component in versions.iterdir():
                if _linked(component):unsafe.append(component.name);continue
                if not component.is_dir():continue
                for stage in component.glob('.staging-*'):
                    if _linked(stage):unsafe.append(stage.relative_to(root).as_posix());continue
                    marker=stage/'.stage.json'
                    if not marker.is_file() or _linked(marker):unsafe.append(stage.relative_to(root).as_posix());continue
                    try:metadata=json.loads(marker.read_text(encoding='utf-8'))
                    except (OSError,ValueError):unsafe.append(stage.relative_to(root).as_posix());continue
                    owner=json.loads((root/OWNER_FILE).read_text(encoding='utf-8'))['owner']
                    if metadata.get('schema')==SCHEMA and metadata.get('owner')==owner:
                        shutil.rmtree(stage);removed.append(stage.relative_to(root).as_posix())
                    else:unsafe.append(stage.relative_to(root).as_posix())
    return {'status':'recovered','removed_staging':len(removed),'unsafe_paths':unsafe}


def _cli_plan_path(root,plan_hash):
    if not re.fullmatch(r'[a-f0-9]{64}',plan_hash):raise RuntimeUpdateError('Invalid plan SHA-256')
    return _safe_path(root,f'.plans/{plan_hash}.json',missing=False)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    init=sub.add_parser('init');init.add_argument('--install-root',required=True);init.add_argument('--owner',default='codex-office')
    prepare_cmd=sub.add_parser('prepare')
    for arg in ('install-root','manifest','bundle','trust','platform'):prepare_cmd.add_argument('--'+arg,required=True)
    prepare_cmd.add_argument('--component',action='append',required=True)
    activate_cmd=sub.add_parser('activate');activate_cmd.add_argument('--install-root',required=True);activate_cmd.add_argument('--plan',required=True);activate_cmd.add_argument('--approve',required=True)
    recover_cmd=sub.add_parser('recover');recover_cmd.add_argument('--install-root',required=True)
    rollback_cmd=sub.add_parser('rollback');rollback_cmd.add_argument('--install-root',required=True);rollback_cmd.add_argument('--component',required=True);rollback_cmd.add_argument('--slot',required=True);rollback_cmd.add_argument('--approve')
    args=parser.parse_args(argv)
    try:
        if args.action=='init':result=initialize(args.install_root,args.owner)
        elif args.action=='prepare':
            plan=prepare(args.install_root,release.read_json(args.manifest),args.bundle,release.read_json(args.trust),args.platform,args.component)
            result={'status':'prepared','plan':plan}
        elif args.action=='activate':
            root=_root(args.install_root);plan=json.loads(_cli_plan_path(root,args.plan).read_text(encoding='utf-8'))
            result=activate(root,plan,args.approve,None)
        elif args.action=='recover':result=recover(args.install_root)
        else:
            plan=rollback_plan(args.install_root,args.component,args.slot)
            if args.approve!=plan['plan_sha256']:
                result={'status':'approval_required','plan_sha256':plan['plan_sha256'],'plan':plan}
            else:result=rollback(args.install_root,plan,args.approve,None)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (RuntimeUpdateError,release.ReleaseError,OSError,ValueError) as error:
        print('error: '+str(error),file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())
