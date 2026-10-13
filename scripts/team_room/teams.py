"""Install teams and agents from the catalog into a project.

Plans are pure: they list every file, roster change and parameter value with its
source. Installing writes only after explicit confirmation and never overwrites
an existing workspace, team or memory.
"""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from . import manifest, roster

REPO = Path(__file__).resolve().parents[2]
PLACEHOLDER = re.compile(r'\{\{([a-z_]+)\}\}')
SLUG = re.compile(r'[^a-z0-9]+')
KINDS = ('team', 'catalog-agent', 'custom-agent', 'director')
BOT_LINE = '- Bot ID: \x60{{bot}}\x60'


class TeamError(ValueError):
    pass


def slug(text):
    value = SLUG.sub('-', str(text).lower()).strip('-')
    if not value or not value[0].isalpha():
        raise TeamError(f'Cannot make an identifier from {text!r}')
    return value[:40]


def today():
    return datetime.now(timezone.utc).date().isoformat()


# Chief of Staff project list --------------------------------------------------

def config_path():
    """User-level config, outside every project: {"chief_of_staff": "<path>"}."""
    return Path(os.environ.get('CODEX_BOT_MODE_CONFIG') or Path.home() / '.codex-bot-mode' / 'config.json')


def register_project(project):
    """Add the project to the Chief of Staff list once. Returns the list file or None."""
    config = config_path()
    if not config.exists():
        return None
    root = json.loads(config.read_text(encoding='utf-8-sig')).get('chief_of_staff')
    projects = Path(root) / 'knowledge' / 'projects.md' if root else None
    if not projects or not projects.exists():
        return None
    project = Path(project).resolve()
    text = projects.read_text(encoding='utf-8')
    if str(project).casefold() in text.casefold():
        return projects
    line = f'| {project.name} | {project} | Added by team launch on {today()}; purpose to confirm with the owner |'
    projects.write_text(text.rstrip('\n') + '\n' + line + '\n', encoding='utf-8', newline='\n')
    return projects


# Catalog ---------------------------------------------------------------------

def fetch(source):
    """Return (catalog directory, source label). source: path or github:owner/repo@ref."""
    if source is None:
        return REPO / 'catalog', 'local:catalog'
    if not source.startswith('github:'):
        path = Path(source).resolve()
        if not (path / 'index.yaml').exists():
            raise TeamError(f'No catalog index at {path}')
        return path, f'local:{path}'
    match = re.fullmatch(r'github:([\w.-]+/[\w.-]+)@([\w./-]+)', source)
    if not match:
        raise TeamError('GitHub catalog must look like github:owner/repo@ref')
    repo, ref = match.groups()
    data = subprocess.run(['gh', 'api', f'repos/{repo}/tarball/{ref}'], capture_output=True, check=False)
    if data.returncode:
        raise TeamError(f'Could not fetch {repo}@{ref}: {data.stderr.decode(errors="replace").strip()[:200]}')
    target = Path(tempfile.mkdtemp(prefix='cbm-catalog-'))
    with tarfile.open(fileobj=io.BytesIO(data.stdout), mode='r:gz') as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            if len(parts) < 3 or parts[1] != 'catalog' or not (member.isfile() or member.isdir()):
                continue
            relative = PurePosixPath(*parts[2:])
            if relative.is_absolute() or '..' in relative.parts:
                raise TeamError('Unsafe path in catalog archive')
            destination = target.joinpath(*relative.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(member).read())
    if not (target / 'index.yaml').exists():
        raise TeamError(f'{repo}@{ref} has no catalog')
    return target, f'github:{repo}@{ref}'


def index(catalog):
    data = manifest.load(catalog / 'index.yaml')
    for kind in ('agents', 'teams'):
        for item in data.get(kind) or []:
            if not isinstance(item.get('name'), str) or not isinstance(item.get('version'), int):
                raise TeamError(f'Invalid catalog entry in {kind}')
    return data


def validate_parameters(spec, where):
    for name, item in (spec or {}).items():
        if not isinstance(item, dict) or not item.get('question'):
            raise TeamError(f'{where}: parameter {name} needs a question')
        if 'default' not in item and not item.get('required'):
            raise TeamError(f'{where}: parameter {name} needs a default or required: true')


def agent_template(catalog, name):
    folder = catalog / 'agents' / name
    if not (folder / 'agent.yaml').exists():
        raise TeamError(f'Unknown agent template {name}')
    data = manifest.load(folder / 'agent.yaml')
    if not isinstance(data.get('version'), int):
        raise TeamError(f'{name}: version must be an integer')
    if data.get('role') is None and not data.get('custom'):
        raise TeamError(f'{name}: role is required')
    validate_parameters(data.get('parameters'), name)
    for required in ('SOUL.md', 'PROFILE.md', 'PLAYBOOK.md'):
        if not (folder / required).exists():
            raise TeamError(f'{name}: missing {required}')
    if BOT_LINE not in (folder / 'PROFILE.md').read_text(encoding='utf-8'):
        raise TeamError(f'{name}: PROFILE.md must contain the Bot ID line')
    return folder, data


def team_template(catalog, name):
    folder = catalog / 'teams' / name
    if not (folder / 'team.yaml').exists():
        raise TeamError(f'Unknown team template {name}')
    data = manifest.load(folder / 'team.yaml')
    if not data.get('roles') or not isinstance(data['roles'], list):
        raise TeamError(f'{name}: roles must be a non-empty list')
    for role in data['roles']:
        agent_template(catalog, role)
    validate_parameters(data.get('parameters'), name)
    return folder, data


def questions(spec):
    return [{'name': k, 'question': v['question'], 'default': v.get('default'),
             'required': bool(v.get('required')), 'choices': v.get('choices')} for k, v in (spec or {}).items()]


def resolve(spec, answers, inferred=None):
    """Values and sources: owner answer, inferred value, then default."""
    values, sources, missing = {}, {}, []
    for name, item in (spec or {}).items():
        if answers.get(name) not in (None, ''):
            values[name], sources[name] = answers[name], 'owner'
        elif (inferred or {}).get(name) is not None and item.get('default') == 'inferred':
            values[name], sources[name] = inferred[name], 'inferred'
        elif 'default' in item and item['default'] != 'inferred':
            values[name], sources[name] = item['default'], 'default'
        elif item.get('required'):
            missing.append(name)
        else:
            values[name], sources[name] = (inferred or {}).get(name), 'inferred'
        if item.get('choices') and name in values and values[name] not in item['choices']:
            raise TeamError(f'{name} must be one of {item["choices"]}')
    if missing:
        raise TeamError('Missing required answers: ' + ', '.join(missing))
    return values, sources


def render(text, context):
    def replace(match):
        key = match.group(1)
        if key not in context or context[key] is None:
            raise TeamError(f'No value for placeholder {{{{{key}}}}}')
        return str(context[key])
    return PLACEHOLDER.sub(replace, text)


# Approvals -------------------------------------------------------------------

def approvals_path(project):
    return Path(project).resolve() / 'team' / 'approvals.yaml'


def check_approval(project, approval, actor, kind, team=None, template=None):
    """approval: 'once' (owner approved this case in chat) or 'standing:<id>'."""
    if kind not in KINDS:
        raise TeamError(f'Unknown appointment kind {kind}')
    if approval == 'once':
        return 'once'
    if not approval or not approval.startswith('standing:'):
        raise TeamError('Explicit owner approval required: pass --approval once after the owner approved this case, or --approval standing:<id>')
    key = approval.split(':', 1)[1]
    file = approvals_path(project)
    data = manifest.load(file) if file.exists() else {}
    record = next((r for r in data.get('approvals') or [] if r.get('id') == key), None)
    if record is None:
        raise TeamError(f'No standing approval {key}')
    if record.get('granted_by') != 'owner':
        raise TeamError(f'Standing approval {key} was not granted by the owner')
    if record.get('revoked'):
        raise TeamError(f'Standing approval {key} is revoked')
    if record.get('expires') and str(record['expires']) < today():
        raise TeamError(f'Standing approval {key} expired on {record["expires"]}')
    if actor not in (record.get('used_by') or []):
        raise TeamError(f'{actor} may not use standing approval {key}')
    if kind not in (record.get('covers') or []):
        raise TeamError(f'Standing approval {key} does not cover {kind}')
    if record.get('teams') and team not in record['teams']:
        raise TeamError(f'Standing approval {key} does not cover team {team}')
    if record.get('templates') and template not in record['templates']:
        raise TeamError(f'Standing approval {key} does not cover template {template}')
    if record.get('max_per_month') is not None:
        month = today()[:7]
        used = [u for u in record.get('uses') or [] if str(u.get('date', ''))[:7] == month]
        if len(used) >= record['max_per_month']:
            raise TeamError(f'Standing approval {key} reached its monthly limit')
    return approval


def record_use(project, approval, actor, what):
    if not approval.startswith('standing:'):
        return
    key = approval.split(':', 1)[1]
    file = approvals_path(project)
    data = manifest.load(file)
    for record in data['approvals']:
        if record['id'] == key:
            record['uses'] = (record.get('uses') or []) + [{'date': today(), 'by': actor, 'what': what}]
    manifest.dump(file, data, '# Standing approvals. Only the owner creates, changes or revokes records.')


# Planning --------------------------------------------------------------------

def _agent_plan(project, catalog, source, template, bot, team, workspace, host, answers, extra=None):
    folder, spec = agent_template(catalog, template)
    values, sources = resolve(spec.get('parameters'), answers, {})
    role = values.get('role') or spec['role']
    if values.get('display_name') in (None, 'inferred'):
        values['display_name'], sources['display_name'] = role, 'inferred'
    context = {**{k: v for k, v in values.items() if v is not None}, 'role': role, 'bot': bot,
               'team': team or project.name, 'project': project.name, 'host': host,
               'skill': f'codex-{bot}', 'date': today()}
    resolved = {'bot_id': bot, 'role': role, 'version': spec['version'], 'template': template,
                'catalog': source, 'installed_at': today(), 'team': team,
                'description': spec.get('description'),
                'reports_to': values.get('reports_to') or spec.get('reports_to'),
                'permissions': spec.get('permissions') or {},
                'parameters': {k: {**v, 'value': values.get(k), 'source': sources.get(k)}
                               for k, v in (spec.get('parameters') or {}).items()}}
    if spec.get('custom'):
        resolved['custom'] = True
    resolved.update(extra or {})
    base = catalog / 'base'
    read = lambda path: path.read_text(encoding='utf-8')
    files = {
        'AGENTS.md': render(read(base / 'AGENTS.md.tmpl'), context),
        '.gitignore': read(base / 'gitignore.tmpl'),
        'agent/BASE.md': render(read(base / 'BASE.md'), context),
        'agent/SOUL.md': render(read(folder / 'SOUL.md'), context),
        'agent/PROFILE.md': render(read(folder / 'PROFILE.md'), context),
        'agent/PLAYBOOK.md': render(read(folder / 'PLAYBOOK.md'), context),
        'agent/STATE.md': render(read(base / 'STATE.md.tmpl'), context),
        'agent/agent.yaml': '# Installed agent manifest.\n' + manifest.dumps(resolved) + '\n',
        'agent/memory/README.md': read(base / 'memory' / 'README.md'),
        f'.agents/skills/codex-{bot}/SKILL.md': render(read(base / 'SKILL.md.tmpl'), context),
        f'.agents/skills/codex-{bot}/agents/openai.yaml': render(read(base / 'openai.yaml.tmpl'), context),
    }
    return {'bot': bot, 'role': role, 'template': template, 'team': team, 'workspace': workspace,
            'display_name': values['display_name'], 'values': values, 'sources': sources,
            'files': files, 'helper': str(base / 'scripts' / 'bot_mode.py')}


def _bot_id(data, role, team, taken=()):
    base = slug(role)
    used = {p['bot'] for p in data['participants']} | set(taken)
    if base not in used:
        return base
    candidate = f'{slug(team)}-{base}' if team else base
    if candidate in used:
        raise TeamError(f'Participant {candidate} already exists')
    return candidate


def _project_files(project, catalog):
    out = {}
    for name in ('PROJECT.md', 'OWNER.md', 'DECISIONS.md'):
        if not (project / 'team' / name).exists():
            out[f'team/{name}'] = render((catalog / 'base' / 'project' / name).read_text(encoding='utf-8'),
                                         {'project': project.name})
    return out


def _has_director(data):
    return any(p['role'] == 'Project Director' and p['status'] == 'active' for p in data['participants'])


def plan_team(project, template, answers=None, source=None, team_answers=None):
    project = Path(project).resolve(strict=True)
    catalog, label = fetch(source)
    folder, spec = team_template(catalog, template)
    team_values, team_sources = resolve(spec.get('parameters'), team_answers or {})
    team = team_values.get('team_name') or spec['name']
    data = roster.load(project)
    if any(t['name'].casefold() == team.casefold() for t in data['teams']):
        raise TeamError(f'Team {team} already exists in this project')
    context = {'project': project.name, 'team': team}
    agents, taken = [], []
    for role in spec['roles']:
        _, aspec = agent_template(catalog, role)
        bot = _bot_id(data, aspec['role'], team, taken)
        taken.append(bot)
        agents.append(_agent_plan(project, catalog, label, role, bot, team, f'team/{team}/{bot}', '../../..',
                                  (answers or {}).get(role, {})))
    conflicts = [a['workspace'] for a in agents if (project / a['workspace']).exists()]
    if conflicts:
        raise TeamError('Workspace already exists: ' + ', '.join(conflicts))
    director = None
    if data['teams'] and not _has_director(data):
        director = ('This will be a second team. A Project Director is proposed as the project entry point; '
                    'appoint it with add-agent --template director after owner confirmation.')
    return {'kind': 'team', 'project': str(project), 'catalog': label, 'template': template,
            'template_version': spec['version'], 'team': team, 'team_sources': team_sources,
            'section': render(spec.get('section') or '{{project}} · {{team}}', context),
            'room': render(spec.get('room') or '{{team}}', context),
            'room_file': render((folder / 'ROOM.md').read_text(encoding='utf-8'), context) if (folder / 'ROOM.md').exists() else None,
            'project_files': _project_files(project, catalog), 'agents': agents, 'director_proposal': director}


def plan_agent(project, team, template, answers=None, source=None, extra=None):
    project = Path(project).resolve(strict=True)
    catalog, label = fetch(source)
    data = roster.load(project)
    _, spec = agent_template(catalog, template)
    if template == 'director':
        if _has_director(data):
            raise TeamError('This project already has a Project Director')
        team, workspace_root, host = None, 'team', '../..'
    else:
        if not any(t['name'] == team for t in data['teams']):
            raise TeamError(f'Unknown team {team}')
        workspace_root, host = f'team/{team}', '../../..'
    role = (answers or {}).get('role') or spec.get('role')
    if not role:
        raise TeamError('Missing required answers: role')
    bot = _bot_id(data, role, team)
    workspace = f'{workspace_root}/{bot}'
    if (project / workspace).exists():
        raise TeamError(f'Workspace already exists: {workspace}')
    agent = _agent_plan(project, catalog, label, template, bot, team, workspace, host, answers or {}, extra)
    kind = 'director' if template == 'director' else ('custom-agent' if spec.get('custom') else 'catalog-agent')
    return {'kind': kind, 'project': str(project), 'catalog': label, 'template': template, 'team': team,
            'project_files': _project_files(project, catalog), 'agents': [agent]}


def summary(plan):
    """Plan without file bodies, for review before confirmation."""
    out = {k: v for k, v in plan.items() if k not in ('agents', 'project_files', 'room_file')}
    out['project_files'] = sorted(plan.get('project_files', {}))
    out['agents'] = [{'bot': a['bot'], 'role': a['role'], 'display_name': a['display_name'],
                      'workspace': a['workspace'], 'template': a['template'],
                      'values': {k: {'value': v, 'source': a['sources'].get(k)} for k, v in a['values'].items()},
                      'files': sorted(a['files'])} for a in plan['agents']]
    return out


# Installing ------------------------------------------------------------------

def install(plan):
    project = Path(plan['project'])
    data = roster.load(project)
    expected = roster.version(data)
    for agent in plan['agents']:
        if (project / agent['workspace']).exists():
            raise TeamError(f'Workspace already exists: {agent["workspace"]}')
    created = []
    try:
        for relative, text in plan.get('project_files', {}).items():
            target = project / relative
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8', newline='\n')
                created.append(target)
        if plan['kind'] == 'team' and plan.get('room_file'):
            target = project / 'team' / plan['team'] / 'ROOM.md'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(plan['room_file'], encoding='utf-8', newline='\n')
            created.append(target)
        for agent in plan['agents']:
            root = project / agent['workspace']
            root.mkdir(parents=True)
            created.append(root)
            for relative, text in agent['files'].items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8', newline='\n')
            (root / 'scripts').mkdir()
            shutil.copyfile(agent['helper'], root / 'scripts' / 'bot_mode.py')
        if plan['kind'] == 'team':
            roster.add_team(data, plan['team'], plan['section'], plan['room'])
        for agent in plan['agents']:
            roster.add_participant(data, agent['bot'], agent['role'], agent['team'], agent['workspace'])
        data['project'].setdefault('tooling', str(REPO))
        roster.save(project, data, expected)
        roster.install_cli(project)
        from . import requests
        requests.install_cli(project)
        register_project(project)
    except Exception:
        for path in reversed(created):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        raise
    return [{'bot': a['bot'], 'role': a['role'], 'display_name': a['display_name'], 'workspace': a['workspace'],
             'skill': f'codex-{a["bot"]}', 'needs_chat': True} for a in plan['agents']]
