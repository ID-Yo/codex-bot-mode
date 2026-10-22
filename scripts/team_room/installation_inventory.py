"""Read-only discovery and explicit metadata-only adoption of CBM installations."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

from . import manifest, roster

SCHEMA = 1
MAX_FILES = 500
MAX_FILE_BYTES = 2 * 1024 * 1024
_UNSET = object()
PROTECTED = ('STATE.md', 'memory', 'bindings.json', 'requests.json', 'approvals.yaml')
PROFILE_FILES = ('agent/agent.yaml', 'agent/BASE.md', 'agent/SOUL.md', 'agent/PROFILE.md',
                 'agent/PLAYBOOK.md', 'AGENTS.md', '.gitignore')


class InventoryError(ValueError):
    pass


def path(project):
    return Path(project).resolve() / '.bot-mode' / 'local' / 'installations.json'


def _linked(item):
    return item.is_symlink() or (item.exists() and bool(getattr(item.lstat(), 'st_file_attributes', 0) & 0x400))


def _contained(root, item):
    try:
        root = Path(root).resolve(strict=True)
        item = Path(item).resolve(strict=True)
        return item.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _linked_path(root, rel):
    cursor = Path(root).resolve(strict=True).joinpath(*PurePosixPath(rel).parts)
    root = Path(root).resolve(strict=True)
    while cursor != root:
        if _linked(cursor):
            return True
        cursor = cursor.parent
    return False


def _safe_file(root, rel):
    rel = PurePosixPath(rel)
    if rel.is_absolute() or '..' in rel.parts or ':' in str(rel) or not rel.parts:
        return None
    root = Path(root).resolve(strict=True)
    item = root.joinpath(*rel.parts)
    # Reject linked ancestors as well as a linked leaf; checking only resolve()
    # would allow an in-root junction to redirect discovery elsewhere.
    if _linked_path(root, str(rel)):
        return None
    if not item.is_file() or not _contained(root, item):
        return None
    if item.stat().st_size > MAX_FILE_BYTES:
        return None
    return item


def _hash(item):
    return hashlib.sha256(item.read_bytes()).hexdigest()


def _catalog_revision(source):
    if not isinstance(source, str):
        return None
    github = re.fullmatch(r'github:[\w.-]+/[\w.-]+@([0-9a-f]{40})', source)
    if github:
        return github.group(1)
    if source == 'local:catalog':
        repo, rel = Path(__file__).resolve().parents[2], 'catalog'
    elif source.startswith('local:'):
        catalog = Path(source[6:])
        if not catalog.is_absolute():
            return None
        try:
            repo = Path(subprocess.check_output(['git', '-C', str(catalog), 'rev-parse', '--show-toplevel'],
                                                text=True, stderr=subprocess.DEVNULL).strip()).resolve()
            rel = catalog.resolve().relative_to(repo).as_posix()
        except (OSError, subprocess.CalledProcessError, ValueError):
            return None
    else:
        return None
    try:
        head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                       text=True, stderr=subprocess.DEVNULL).strip()
        changed = subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain', '--', rel],
                                          text=True, stderr=subprocess.DEVNULL)
        return head if re.fullmatch(r'[0-9a-f]{40}', head) and not changed else None
    except (OSError, subprocess.CalledProcessError):
        return None


def catalog_revision(source):
    """Return the immutable catalog revision, or None when provenance is unknown."""
    return _catalog_revision(source)


def _validate(data):
    if not isinstance(data, dict) or data.get('schema') != SCHEMA or not isinstance(data.get('components'), list):
        raise InventoryError('Unsupported installation inventory schema')
    ids = set()
    for c in data['components']:
        if not isinstance(c, dict) or c.get('kind') not in ('profile', 'shared_reference', 'tool'):
            raise InventoryError('Invalid inventory component')
        if not isinstance(c.get('id'), str) or c['id'] in ids:
            raise InventoryError('Invalid or duplicate component ID')
        ids.add(c['id'])
        if not isinstance(c.get('path'), str) or not isinstance(c.get('owned_paths'), list) or not isinstance(c.get('baseline'), dict):
            raise InventoryError('Invalid component paths or baseline')
        component_path = PurePosixPath(c['path'])
        if component_path.is_absolute() or '..' in component_path.parts or ':' in c['path']:
            raise InventoryError('Unsafe component root')
        for rel in c['owned_paths']:
            if not isinstance(rel, str) or PurePosixPath(rel).is_absolute() or '..' in PurePosixPath(rel).parts or ':' in rel:
                raise InventoryError('Unsafe component path')
            parts = PurePosixPath(rel).parts
            folded = {part.casefold() for part in parts}
            if folded & {'memory','state.md','bindings.json','requests.json','approvals.yaml'}:
                raise InventoryError('Protected paths cannot be inventoried')
            relpath = PurePosixPath(rel)
            if c['kind'] == 'shared_reference' and (len(parts) < 4 or parts[:2] != ('team','references')):
                raise InventoryError('Shared references must remain under team/references')
            if c['kind'] == 'profile':
                prefix = component_path.parts
                suffix = parts[len(prefix):] if prefix else parts
                if parts[:len(prefix)] != prefix or not (suffix in {
                    ('agent','agent.yaml'), ('agent','BASE.md'), ('agent','SOUL.md'),
                    ('agent','PROFILE.md'), ('agent','PLAYBOOK.md'), ('AGENTS.md'),
                    ('.gitignore',), ('agent.yaml',), ('BASE.md',), ('SOUL.md',),
                    ('PROFILE.md',), ('PLAYBOOK.md',), ('AGENTS.md',), ('.gitignore',)
                }):
                    raise InventoryError('Profile path is outside the managed profile surface')
        if not isinstance(c.get('source'), (str, type(None))) or not isinstance(c.get('component_schema'), int):
            raise InventoryError('Invalid component source or schema')
        if any(not isinstance(rel,str) or rel not in c['owned_paths'] or not isinstance(item,dict)
               or item.get('state') not in ('known','unknown')
               or (item.get('state')=='known' and not re.fullmatch(r'[0-9a-f]{64}',item.get('sha256','')))
               for rel,item in c['baseline'].items()):
            raise InventoryError('Invalid per-file baseline')
        if not isinstance(c.get('version'), (str, int, type(None))) or not isinstance(c.get('source_revision'), (str, type(None))):
            raise InventoryError('Invalid component version or source revision')
    if len(ids) != len(data['components']) or len(ids) > MAX_FILES:
        raise InventoryError('Inventory has duplicate or excessive components')
    return data


def load(project):
    root = Path(project).resolve(strict=True)
    file = path(root)
    for folder in (root / '.bot-mode', file.parent):
        if folder.exists() and (_linked(folder) or not _contained(root, folder)):
            raise InventoryError('Installation inventory path is unsafe or too large')
    if not file.exists():
        return {'schema': SCHEMA, 'components': []}
    if not _safe_file(root, '.bot-mode/local/installations.json'):
        raise InventoryError('Installation inventory path is unsafe or too large')
    try:
        return _validate(json.loads(file.read_text(encoding='utf-8')))
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError('Installation inventory cannot be read') from error


def snapshot(project):
    """Validate and return the exact current metadata bytes for transaction rollback."""
    data = load(project)
    file = path(project)
    return file.read_bytes() if file.exists() else None


def _inventory_digest(file):
    return hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else None


def write(project, data, expected_digest=_UNSET):
    data = _validate(data)
    root = Path(project).resolve(strict=True)
    file = path(root)
    for folder in (root / '.bot-mode', file.parent):
        if folder.exists() and (_linked(folder) or not _contained(root, folder)):
            raise InventoryError('Refusing linked inventory directory')
    if file.exists() and (_linked(file) or not _contained(project, file)):
        raise InventoryError('Refusing linked inventory path')
    file.parent.mkdir(parents=True, exist_ok=True)
    lock = file.with_suffix('.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise InventoryError('Another inventory writer is active') from None
    temporary = None
    serialized = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    try:
        os.close(fd)
        if expected_digest is not _UNSET and _inventory_digest(file) != expected_digest:
            raise InventoryError('Inventory changed since it was read; reload and retry')
        handle, temporary = tempfile.mkstemp(dir=file.parent, prefix='installations-', suffix='.tmp')
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as out:
            out.write(serialized)
        os.replace(temporary, file)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def restore(project, previous_bytes, expected_digest, cleanup=None):
    """Restore transaction metadata only while its exact committed version is current."""
    root = Path(project).resolve(strict=True)
    file = path(root)
    for folder in (root / '.bot-mode', file.parent):
        if folder.exists() and (_linked(folder) or not _contained(root, folder)):
            raise InventoryError('Refusing linked inventory directory')
    file.parent.mkdir(parents=True, exist_ok=True)
    lock = file.with_suffix('.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise InventoryError('Another inventory writer is active') from None
    temporary = None
    try:
        os.close(fd)
        if file.exists() and (_linked(file) or not _contained(root, file)):
            raise InventoryError('Refusing linked inventory path')
        if _inventory_digest(file) != expected_digest:
            return False
        if previous_bytes is None:
            file.unlink(missing_ok=True)
        else:
            _validate(json.loads(previous_bytes.decode('utf-8')))
            handle, temporary = tempfile.mkstemp(dir=file.parent, prefix='installations-', suffix='.tmp')
            with os.fdopen(handle, 'wb') as out:
                out.write(previous_bytes)
            os.replace(temporary, file)
            temporary = None
        if cleanup:
            cleanup()
        return True
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def _file_baseline(root, owned, created_paths, expected_hashes):
    baseline = {}
    unknown = []
    for rel in owned:
        item = _safe_file(root, rel)
        expected = expected_hashes.get(rel) if rel in created_paths else None
        if isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected):
            baseline[rel] = {'state': 'known', 'sha256': expected}
            if not item and ((Path(root) / rel).exists() or _linked_path(root, rel)):
                unknown.append(rel)
        elif item:
            baseline[rel] = {'state': 'unknown', 'reason': 'pre-existing-at-install'}
        else:
            unknown.append(rel)
    return baseline, unknown


def _component(root, *, cid, kind, relroot, source, source_revision, version, owned,
               created_paths=(), expected_hashes=None, schema=1):
    baseline, unsafe = _file_baseline(root, owned, set(created_paths), expected_hashes or {})
    return {'id': cid, 'kind': kind, 'path': relroot, 'source': source,
            'source_revision': source_revision, 'version': version,
            'component_schema': schema, 'owned_paths': list(owned), 'baseline': baseline,
            'unsafe_paths': unsafe}


def record_new_install(project, agents, references, source, created_paths, expected_hashes=None,
                       source_revision=_UNSET, expected_digest=_UNSET):
    """Capture rendered defaults for newly created files; never records protected state."""
    root = Path(project).resolve(strict=True)
    inventory_file=path(root)
    if expected_digest is _UNSET:
        expected_digest=_inventory_digest(inventory_file)
    if source_revision is _UNSET:
        source_revision=_catalog_revision(source)
    known = {Path(p).as_posix() for p in created_paths}
    expected_hashes = expected_hashes or {}
    records = []
    for agent in agents:
        ws, bot = agent['workspace'], agent['bot']
        profile_paths = [f'{ws}/{rel}' for rel in PROFILE_FILES
                         if not (ws == '.' and rel == 'AGENTS.md')]
        profile_paths = [rel for rel in profile_paths if rel in known or _safe_file(root, rel)]
        records.append(_component(root, cid='profile:' + ws, kind='profile', relroot=ws,
                                  source=source, source_revision=source_revision,
                                  version=agent.get('template_version'), owned=profile_paths,
                                  created_paths=known, expected_hashes=expected_hashes))
        tool_paths = [f'{ws}/scripts/bot_mode.py', f'{ws}/.agents/skills/codex-{bot}/SKILL.md',
                      f'{ws}/.agents/skills/codex-{bot}/agents/openai.yaml']
        tool_paths = [rel for rel in tool_paths if rel in known or _safe_file(root, rel)]
        if tool_paths:
            records.append(_component(root, cid='tool:agent:' + bot, kind='tool', relroot=ws,
                                      source=source, source_revision=source_revision, version=1,
                                      owned=tool_paths, created_paths=known, expected_hashes=expected_hashes))
    for ref in references or []:
        relroot = f'team/references/{ref}'
        folder = root / relroot
        owned = _safe_tree_files(root, folder)
        ref_unsafe = []
        index_rel = f'{relroot}/index.yaml'
        index_path = _safe_file(root, index_rel)
        try:
            if not index_path:
                raise InventoryError('Unsafe reference index path')
            index = manifest.load(index_path)
            version = index.get('version')
        except Exception:
            version = None
            if _linked_path(root, index_rel):
                ref_unsafe.append(index_rel)
        record = _component(root, cid='reference:' + ref, kind='shared_reference', relroot=relroot,
                            source=source, source_revision=source_revision, version=version, owned=owned,
                            created_paths=known, expected_hashes=expected_hashes)
        record['unsafe_paths'].extend(ref_unsafe)
        record['dependents'] = sorted({agent['role'] for agent in agents if ref in agent.get('references', [])})
        records.append(record)
    for rel in ('.bot-mode/roster.py', '.bot-mode/requests.py'):
        if (root / rel).is_file():
            records.append(_component(root, cid='tool:' + rel, kind='tool', relroot=rel,
                                      source=source, source_revision=source_revision, version=1, owned=[rel],
                                      created_paths=known, expected_hashes=expected_hashes))
    data = load(root)
    by_id = {c['id']: c for c in data['components']}
    for record in records:
        existing = by_id.get(record['id'])
        if existing:
            # Preserve the original baseline. Add only files proven newly created now.
            for rel in record['owned_paths']:
                if rel not in existing['owned_paths']:
                    existing['owned_paths'].append(rel)
                    existing['baseline'][rel] = record['baseline'].get(rel, {'state':'unknown','reason':'baseline-not-captured'})
            if record['kind'] == 'shared_reference':
                existing['dependents'] = sorted(set(existing.get('dependents', [])) | set(record.get('dependents', [])))
            continue
        by_id[record['id']] = record
    data['components'] = list(by_id.values())
    return write(root, data, expected_digest)


def _safe_tree_files(root, folder):
    if not folder.exists() or _linked(folder) or not _contained(root, folder):
        return []
    result = []
    for item in sorted(folder.rglob('*')):
        if _linked(item):
            continue
        if item.is_file() and _safe_file(root, item.relative_to(root).as_posix()):
            result.append(item.relative_to(root).as_posix())
            if len(result) > MAX_FILES:
                raise InventoryError('Component exceeds inventory file limit')
    return result


def _profile_roots(root):
    candidates = set()
    roster_file = root / '.bot-mode/roster.json'
    if roster_file.is_file() and not _linked(roster_file) and _contained(root, roster_file):
        try:
            if roster_file.stat().st_size>MAX_FILE_BYTES:
                raise ValueError('Roster too large')
            raw = json.loads(roster_file.read_text(encoding='utf-8'))
            for participant in raw.get('participants', []):
                ws = participant.get('workspace') if isinstance(participant, dict) else None
                if isinstance(ws, str) and ws and not Path(ws).is_absolute() and '..' not in Path(ws).parts:
                    candidates.add(Path(ws).as_posix())
        except (OSError, ValueError, AttributeError):
            pass
    candidates.update(('agents/project-manager', '.'))
    team = root / 'team'
    if _linked(team):
        return sorted(candidates),[team.relative_to(root).as_posix()]
    if team.is_dir() and _contained(root, team):
        unsafe=[]
        for group in team.iterdir():
            if group.name == 'references':
                continue
            if _linked(group):
                unsafe.append(group.relative_to(root).as_posix())
                continue
            if not group.is_dir():
                continue
            for workspace in group.iterdir():
                if _linked(workspace):
                    unsafe.append(workspace.relative_to(root).as_posix())
                elif workspace.is_dir():
                    candidates.add(workspace.relative_to(root).as_posix())
        return sorted(candidates),unsafe
    return sorted(candidates),[]


def _identity(root, relroot):
    folder = root if relroot == '.' else root / relroot
    for meta_path in (folder / 'agent/agent.yaml', folder / 'agent.yaml'):
        item = _safe_file(root, meta_path.relative_to(root).as_posix())
        if item:
            try:
                data = manifest.load(item)
                return {'bot_id': data.get('bot_id'), 'role': data.get('role'), 'template': data.get('template'),
                        'version': data.get('version'), 'source': data.get('catalog')}
            except Exception:
                return {'source': None}
    if any(_safe_file(root, f'{relroot}/agent/{name}' if relroot != '.' else f'agent/{name}') for name in ('PROFILE.md','SOUL.md')):
        return {'source': None}
    return None


def _current_components(root):
    found, unsafe = [], []
    candidates,unsafe = _profile_roots(root)
    for relroot in candidates:
        folder = root if relroot == '.' else root / relroot
        if not folder.exists():
            continue
        if _linked(folder) or not _contained(root, folder):
            unsafe.append(relroot)
            continue
        ident = _identity(root, relroot)
        if not ident:
            continue
        bot = ident.get('bot_id')
        profile_paths=[]
        legacy_flat = (folder / 'agent.yaml').is_file()
        for name in PROFILE_FILES:
            if legacy_flat:
                rel = f'{relroot}/{name.removeprefix("agent/")}'
            else:
                if relroot == '.' and name == 'AGENTS.md':
                    continue
                rel=f'{relroot}/{name}' if relroot != '.' else name
            if not legacy_flat and relroot == '.' and name == 'AGENTS.md':
                continue
            item=root/rel
            if _linked(item):
                unsafe.append(rel)
            elif item.is_file():
                profile_paths.append(rel)
        found.append({'id':'profile:'+relroot,'kind':'profile','path':relroot,
                      'source':ident.get('source'),'source_revision':None,'version':ident.get('version'),
                      'component_schema':1,'identity':{k:ident.get(k) for k in ('bot_id','role','template')},
                      'owned_paths':profile_paths,'current':_hashes(root,profile_paths)})
        skill_paths = [f'{relroot}/.agents/skills/codex-{bot}/SKILL.md',
                       f'{relroot}/.agents/skills/codex-{bot}/agents/openai.yaml'] if bot else []
        helper = f'{relroot}/scripts/bot_mode.py' if relroot != '.' else 'scripts/bot_mode.py'
        skill_paths += [helper]
        skill_paths = [p for p in skill_paths if (root / p).is_file() and not _linked(root/p)]
        if skill_paths:
            found.append({'id':'tool:agent:'+str(bot or relroot),'kind':'tool','path':relroot,
                          'source':ident.get('source'),'source_revision':None,'version':None,'component_schema':1,
                          'identity':{'bot_id':bot},'owned_paths':skill_paths,'current':_hashes(root,skill_paths)})
    refs = root/'team/references'
    if _linked_path(root, 'team/references'):
        unsafe.append('team/references')
    elif refs.is_dir() and _contained(root, refs):
        for folder in sorted(refs.iterdir()):
            if _linked(folder) or not folder.is_dir():
                unsafe.append(folder.relative_to(root).as_posix())
                continue
            relroot=folder.relative_to(root).as_posix(); paths=_safe_tree_files(root,folder)
            index_rel=f'{relroot}/index.yaml'
            index_path=_safe_file(root,index_rel)
            ref_unsafe=[]
            try:
                if not index_path:
                    raise InventoryError('Unsafe reference index path')
                idx=manifest.load(index_path); version=idx.get('version'); name=idx.get('name')
            except Exception:
                version=name=None
                if _linked_path(root,index_rel):
                    unsafe.append(index_rel)
                    ref_unsafe.append(index_rel)
            found.append({'id':'reference:'+folder.name,'kind':'shared_reference','path':relroot,
                          'source':None,'source_revision':None,'version':version,'component_schema':1,
                          'identity':{'name':name},'dependents':[],'owned_paths':paths,
                          'unsafe_paths':ref_unsafe,'current':_hashes(root,paths)})
    for rel in ('.bot-mode/roster.py','.bot-mode/requests.py'):
        if (root/rel).is_file() and not _linked(root/rel):
            found.append({'id':'tool:'+rel,'kind':'tool','path':rel,'source':None,'source_revision':None,
                          'version':None,'component_schema':1,'identity':{},'owned_paths':[rel],
                          'current':_hashes(root,[rel])})
    return found, unsafe


def _hashes(root, paths):
    return {rel:_hash(item) for rel in paths if (item:=_safe_file(root,rel))}


def _state(component, record):
    if component.get('unsafe_paths'):
        return 'manual_review'
    if record is None:
        return 'unknown_source' if not component.get('source') else 'manual_review'
    if not record.get('source'):
        return 'unknown_source'
    baseline=record.get('baseline',{})
    owned=record.get('owned_paths',[])
    if not owned or any(baseline.get(rel,{}).get('state')!='known' for rel in owned):
        return 'manual_review'
    if not record.get('source_revision'):
        return 'unknown_source'
    current=component.get('current',{})
    for rel in owned:
        if rel not in current:return 'removed_files'
        if current[rel]!=baseline[rel].get('sha256'):return 'local_changes'
    if set(current) - set(owned):
        return 'local_changes'
    return 'unchanged'


def discover(project):
    root=Path(project).resolve(strict=True)
    current,unsafe=_current_components(root)
    stored=load(root)
    by_id={c['id']:c for c in stored['components']}
    result=[]
    for c in current:
        record=by_id.get(c['id'])
        state=_state(c,record)
        item={k:v for k,v in c.items() if k!='current'}
        if record:
            item.update(source=record.get('source'),source_revision=record.get('source_revision'),version=record.get('version'))
        item['updateability']=state
        result.append(item)
    for item in result:
        record = by_id.get(item['id'])
        if record and item['kind'] == 'shared_reference':
            item['dependents'] = record.get('dependents', [])
    # Keep disappeared installed components visible so a deleted workspace or
    # shared bundle is reported as removed instead of silently forgotten.
    current_ids = {c['id'] for c in current}
    for record in stored['components']:
        if record['id'] in current_ids:
            continue
        item = {k: record[k] for k in ('id','kind','path','source','source_revision','version',
                                       'component_schema','owned_paths') if k in record}
        item['updateability'] = ('removed_files' if record.get('baseline') and
                                 all(v.get('state') == 'known' for v in record['baseline'].values())
                                 else 'manual_review' if record.get('source') else 'unknown_source')
        result.append(item)
    return {'schema':SCHEMA,'components':result,'unsafe_paths':unsafe,
            'adoptable':sum(c['id'] not in by_id for c in current)}


def adopt(project, confirm=False):
    root=Path(project).resolve(strict=True)
    inventory_file=path(root)
    expected_digest=_inventory_digest(inventory_file)
    current,unsafe=_current_components(root)
    data=load(root);by_id={c['id']:c for c in data['components']};added=[]
    for c in current:
        if c['id'] in by_id:continue
        baseline={rel:{'state':'unknown','reason':'legacy-original-not-verified'} for rel in c['owned_paths']}
        record={k:c[k] for k in ('id','kind','path','source','source_revision','version','component_schema','owned_paths')}
        record['baseline']=baseline
        by_id[c['id']]=record;added.append(c['id'])
    preview={'action':'adopted' if confirm else 'preview','added':len(added),
             'components':[{'id':cid,'baseline':'unknown; manual review'} for cid in added],
             'unsafe_paths':unsafe,'writes':[str(path(root))] if confirm and added else []}
    if confirm and added:
        data['components']=list(by_id.values())
        write(root,data,expected_digest)
    return preview


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description='Read-only CBM installation inventory and explicit metadata adoption.')
    sub=parser.add_subparsers(dest='action',required=True)
    for action in ('inventory','adopt'):
        cmd=sub.add_parser(action)
        cmd.add_argument('--project',required=True,help='Explicit project root; no filesystem scanning is performed')
        if action=='adopt':cmd.add_argument('--confirm',action='store_true',help='Write only .bot-mode/local/installations.json')
    args=parser.parse_args(argv)
    try:
        result=discover(args.project) if args.action=='inventory' else adopt(args.project, args.confirm)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (InventoryError,OSError,ValueError) as error:
        print('error: '+str(error),file=__import__('sys').stderr)
        return 2
