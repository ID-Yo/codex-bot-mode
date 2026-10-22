"""Cross-project registry and directory overview for Codex Office (CBM-102).

Reads verified project roots and standalone bots from explicit local configuration,
aggregates rosters without copying private content or memories, and qualifies
bot keys to avoid manager/developer collisions across projects.

Configuration file locations (strictly portable and local):
1. Environment variable: CODEX_OFFICE_REGISTRY
2. Project-local state: <served.PROJECT>/.bot-mode/local/office-project-roots.json
Never scans arbitrary filesystem paths or uses hardcoded absolute task paths.
"""
import json
import os
from pathlib import Path
import re

from . import manifest, roster

SCHEMA = 1
ID = re.compile(r'^[a-z][a-z0-9-]{0,47}$')
THREAD = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


class RegistryError(ValueError):
    pass


def registry_path(project_root=None):
    """Locate configured registry file path from environment or project-local state."""
    env = os.environ.get('CODEX_OFFICE_REGISTRY')
    if env:
        p = Path(env).resolve()
        if p.exists():
            return p
    if project_root:
        local_cand = Path(project_root) / '.bot-mode' / 'local' / 'office-project-roots.json'
        if local_cand.exists():
            return local_cand.resolve()
    return None


def validate_config(data):
    if not isinstance(data, dict):
        raise RegistryError('Registry configuration must be an object')
    if data.get('schema') != SCHEMA:
        raise RegistryError(f'Unsupported registry schema: {data.get("schema")}')

    projects = data.get('projects') or []
    if not isinstance(projects, list):
        raise RegistryError('Registry projects must be a list')

    seen_ids = set()
    for i, p in enumerate(projects):
        if not isinstance(p, dict):
            raise RegistryError(f'Project at index {i} must be an object')
        pid = p.get('id')
        if not pid or not isinstance(pid, str) or not ID.match(pid):
            raise RegistryError(f'Project at index {i} has invalid or missing id: {pid}')
        if pid in seen_ids:
            raise RegistryError(f'Duplicate project id: {pid}')
        seen_ids.add(pid)
        root = p.get('root')
        if not root or not isinstance(root, str):
            raise RegistryError(f'Project {pid} missing root path')
        if not Path(root).is_absolute():
            raise RegistryError(f'Project {pid} root must be an absolute path')

    standalone = data.get('standalone') or []
    if not isinstance(standalone, list):
        raise RegistryError('Registry standalone must be a list')

    seen_standalone = set()
    for i, s in enumerate(standalone):
        if not isinstance(s, dict):
            raise RegistryError(f'Standalone bot at index {i} must be an object')
        sid = s.get('id')
        if not sid or not isinstance(sid, str) or not ID.match(sid):
            raise RegistryError(f'Standalone bot at index {i} has invalid id: {sid}')
        if sid in seen_standalone:
            raise RegistryError(f'Duplicate standalone bot id: {sid}')
        seen_standalone.add(sid)
        tid = s.get('thread_id')
        if tid is not None and (not isinstance(tid, str) or not THREAD.match(tid)):
            raise RegistryError(f'Standalone bot {sid} has invalid thread_id: {tid}')

    return data


def load_registry(config_file):
    if not config_file or not Path(config_file).exists():
        return {'status': 'not_configured', 'projects': [], 'standalone': []}
    try:
        raw = json.loads(Path(config_file).read_text(encoding='utf-8'))
        valid = validate_config(raw)
        return {
            'status': 'ok',
            'projects': valid.get('projects') or [],
            'standalone': valid.get('standalone') or []
        }
    except Exception as e:
        return {'status': 'invalid_config', 'error': str(e), 'projects': [], 'standalone': []}


def _is_safe_contained(target_path, root_dir):
    try:
        resolved_root = Path(root_dir).resolve(strict=True)
        resolved_target = Path(target_path).resolve(strict=True)
        return resolved_target.is_relative_to(resolved_root)
    except Exception:
        return False

def _get_display_name(proot, workspace, default_role):
    try:
        if not workspace or not isinstance(workspace, str):
            return default_role
        agent_yaml = Path(proot) / workspace / 'agent' / 'agent.yaml'
        if not agent_yaml.exists():
            return default_role
        if not _is_safe_contained(agent_yaml, proot):
            return default_role
        data = manifest.load(agent_yaml)
        param = data.get('parameters', {}).get('display_name')
        if isinstance(param, dict) and param.get('value'):
            return param['value']
        elif isinstance(param, str) and param:
            return param
    except Exception:
        pass
    return default_role


def build_overview(config_file, current_project_id=None, current_project_root=None):
    reg = load_registry(config_file)
    if reg['status'] != 'ok':
        return {
            'status': reg['status'],
            'error': reg.get('error'),
            'bots': [],
            'teams': []
        }

    all_bots = []
    all_teams = []

    for p in reg['projects']:
        pid = p.get('id')
        pname = p.get('name') or pid
        proot = p.get('root')

        roster_file = roster.path(proot)
        local_bindings = roster.local_path(proot)
        if not Path(proot).exists() or not roster_file.exists():
            all_teams.append({
                'project_id': pid,
                'project_name': pname,
                'status': 'source_unavailable',
                'team': None,
                'members': []
            })
            continue
        if not _is_safe_contained(roster_file, proot) or (local_bindings.exists() and not _is_safe_contained(local_bindings, proot)):
            all_teams.append({
                'project_id': pid,
                'project_name': pname,
                'status': 'roster_error',
                'team': None,
                'members': []
            })
            continue

        is_curr = (pid == current_project_id) or (
            current_project_root and Path(proot).resolve() == Path(current_project_root).resolve()
        )

        try:
            rdata = roster.load(proot)
        except Exception:
            all_teams.append({
                'project_id': pid,
                'project_name': pname,
                'status': 'roster_error',
                'team': None,
                'members': []
            })
            continue

        # Build bots
        for part in rdata.get('participants', []):
            if part.get('status') != 'active':
                continue
            bot_id = part.get('bot')
            role = part.get('role') or bot_id.title()
            display_name = _get_display_name(proot, part.get('workspace') or '', role)
            team_name = part.get('team')
            tid = part.get('thread_id')
            all_bots.append({
                'bot_key': f'{pid}:{bot_id}',
                'bot_id': bot_id,
                'name': display_name,
                'role': role,
                'project_id': pid,
                'project_name': pname,
                'team_name': team_name,
                'thread_id': tid,
                'link': roster.link(tid),
                'is_current': bool(is_curr)
            })

        # Build teams
        if not rdata.get('teams'):
            all_teams.append({
                'project_id': pid,
                'project_name': pname,
                'status': 'no_teams',
                'team': None,
                'members': []
            })
        else:
            for t in rdata.get('teams', []):
                tname = t['name']
                members = [
                    {
                        'bot_key': f'{pid}:{part.get("bot")}',
                        'bot_id': part.get('bot'),
                        'role': part.get('role'),
                        'display_name': _get_display_name(proot, part.get('workspace') or '', part.get('role') or part.get('bot').title()),
                        'thread_id': part.get('thread_id'),
                        'link': roster.link(part.get('thread_id'))
                    }
                    for part in rdata.get('participants', [])
                    if part.get('team') == tname and part.get('status') == 'active'
                ]
                all_teams.append({
                    'project_id': pid,
                    'project_name': pname,
                    'status': 'ok',
                    'team': tname,
                    'section': t.get('section'),
                    'room': t.get('room'),
                    'members': members
                })

    for s in reg['standalone']:
        sid = s.get('id')
        sname = s.get('name') or sid
        tid = s.get('thread_id')
        all_bots.append({
            'bot_key': f'standalone:{sid}',
            'bot_id': sid,
            'name': sname,
            'role': s.get('role') or 'Chief of Staff',
            'project_id': None,
            'project_name': 'Standalone',
            'team_name': None,
            'thread_id': tid,
            'link': roster.link(tid),
            'is_current': False
        })

    return {
        'status': 'ok',
        'bots': all_bots,
        'teams': all_teams
    }


def resolve_bot_thread(config_file, bot_key, current_project_root=None):
    if not bot_key or not isinstance(bot_key, str):
        return None
    overview = build_overview(config_file, current_project_root=current_project_root)
    for b in overview.get('bots', []):
        if b.get('bot_key') == bot_key:
            return b.get('thread_id')
    return None

def resolve_project_manager(config_file, project_id, current_project_root=None):
    """Resolve exactly one appointed Manager bot details and thread_id for a given project_id.

    Reuses build_overview safety, containment, and manifest display name checks.
    Fails closed if there is none, multiple managers, symlink escape, or missing project.
    Never returns private filesystem roots to caller.
    """
    if not project_id or not isinstance(project_id, str):
        return {'status': 'invalid_id', 'error': 'Valid project_id required'}
    overview = build_overview(config_file, current_project_root=current_project_root)
    if overview.get('status') != 'ok':
        return {'status': overview.get('status') or 'error', 'error': overview.get('error') or 'Registry error'}

    # Check if project exists in overview teams
    project_teams = [t for t in overview.get('teams', []) if t.get('project_id') == project_id]
    if not project_teams:
        return {'status': 'not_configured', 'error': f'Project {project_id} not configured in registry'}

    first_team = project_teams[0]
    pname = first_team.get('project_name') or project_id
    if first_team.get('status') in ('source_unavailable', 'roster_error'):
        return {'status': first_team['status'], 'error': f'Project roster is unavailable or failed containment check'}

    # Collect active Manager bots for this project from overview.bots
    # Strict criteria: role must be exactly 'Manager'
    mgrs = [b for b in overview.get('bots', []) if b.get('project_id') == project_id and b.get('role') == 'Manager']
    if not mgrs:
        return {'status': 'no_manager', 'error': f'No active Manager appointed for {pname}'}
    if len(mgrs) > 1:
        return {'status': 'multiple_managers', 'error': f'Multiple Managers found for {pname}; manual review required'}

    mgr = mgrs[0]
    tid = mgr.get('thread_id')
    return {
        'status': 'ok',
        'project_id': project_id,
        'project_name': pname,
        'bot_key': mgr.get('bot_key'),
        'bot': mgr.get('bot_id'),
        'role': 'Manager',
        'name': mgr.get('name'),
        'thread_id': tid,
        'link': mgr.get('link'),
        'is_current': mgr.get('is_current', False)
    }

def resolve_project_bots(config_file, project_id, current_project_root=None):
    """Resolve all appointed active bots for a given project_id.

    Reuses build_overview safety, containment, and manifest display name checks.
    Returns {'status': 'ok', 'project_id': pid, 'project_name': pname, 'bots': [{'bot_id', 'name', 'role', 'thread_id'}]}
    or error status ('invalid_id', 'not_configured', 'source_unavailable', 'roster_error', etc.).
    """
    if not project_id or not isinstance(project_id, str):
        return {'status': 'invalid_id', 'error': 'Valid project_id required', 'bots': []}
    overview = build_overview(config_file, current_project_root=current_project_root)
    if overview.get('status') != 'ok':
        return {'status': overview.get('status') or 'error', 'error': overview.get('error') or 'Registry error', 'bots': []}

    project_teams = [t for t in overview.get('teams', []) if t.get('project_id') == project_id]
    if not project_teams:
        return {'status': 'not_configured', 'error': f'Project {project_id} not configured in registry', 'bots': []}

    first_team = project_teams[0]
    pname = first_team.get('project_name') or project_id
    if first_team.get('status') in ('source_unavailable', 'roster_error'):
        return {'status': first_team['status'], 'error': 'Project roster is unavailable or failed containment check', 'bots': []}

    project_bots = [
        {
            'bot_id': b['bot_id'],
            'name': b['name'],
            'role': b['role'],
            'thread_id': b.get('thread_id')
        }
        for b in overview.get('bots', [])
        if b.get('project_id') == project_id
    ]
    return {
        'status': 'ok',
        'project_id': project_id,
        'project_name': pname,
        'bots': project_bots
    }

