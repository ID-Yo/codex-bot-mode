"""Install teams and agents from the catalog into a project.

Plans are pure: they list every file, roster change and parameter value with its
source. Installing writes only after explicit confirmation and never overwrites
an existing workspace, team or memory.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from . import installation_inventory, manifest, roster

REPO = Path(__file__).resolve().parents[2]
PLACEHOLDER = re.compile(r'\{\{([a-z_]+)\}\}')
SLUG = re.compile(r'[^a-z0-9]+')
KINDS = ('team', 'catalog-agent', 'custom-agent', 'director')
BOT_LINE = '- Bot ID: \x60{{bot}}\x60'
TRACKING_PARAMETERS = {
    'tracking_mode': {'question': 'Where must implementation work be tracked?',
                      'choices': ['github', 'local', 'both']},
    'tracking_authority': {'question': 'For both, which record owns mutable status?',
                           'choices': ['github', 'local']},
}


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
    data['parameters'] = {**(data.get('parameters') or {}), **TRACKING_PARAMETERS}
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
        if item.get('choices') and values.get(name) is not None and values[name] not in item['choices']:
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

def tracking(values):
    mode = values.get('tracking_mode')
    authority = values.get('tracking_authority')
    if mode not in (None, 'github', 'local', 'both'):
        raise TeamError('tracking_mode must be github, local or both')
    if authority not in (None, 'github', 'local'):
        raise TeamError('tracking_authority must be github or local')
    if mode != 'both' and authority and authority != mode:
        raise TeamError('tracking_authority conflicts with tracking_mode')
    return {'mode': mode, 'authority': authority if mode == 'both' else mode}


def require_tracking(choice):
    if not choice.get('mode') or not choice.get('authority'):
        raise TeamError('Choose tracking_mode (github/local/both); both also needs tracking_authority before appointment')

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
    return {'bot': bot, 'role': role, 'template': template, 'template_version': spec['version'], 'team': team, 'workspace': workspace,
            'display_name': values['display_name'], 'values': values, 'sources': sources,
            'files': files, 'helper': str(base / 'scripts' / 'bot_mode.py'),
            'helper_sha256': hashlib.sha256((base / 'scripts' / 'bot_mode.py').read_bytes()).hexdigest()}


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


def _validate_reference_index(base_dir, ref_name):
    idx_file = base_dir / 'index.yaml'
    if not idx_file.exists():
        raise TeamError(f'Reference {ref_name} is missing index.yaml')
    is_idx_link = idx_file.is_symlink() or (idx_file.exists() and bool(getattr(idx_file.lstat(), 'st_file_attributes', 0) & 0x400))
    if is_idx_link:
        raise TeamError(f'Reference {ref_name} has linked index.yaml')
    try:
        idx_data = manifest.load(idx_file)
        if not isinstance(idx_data, dict) or not idx_data.get('name') or not isinstance(idx_data.get('modules'), dict) or not isinstance(idx_data.get('templates'), dict):
            raise TeamError(f'Reference {ref_name} index lacks required name/modules/templates schema')
    except TeamError:
        raise
    except Exception as e:
        raise TeamError(f'Malformed index.yaml in reference {ref_name}: {e}')

    def _check_path(cand_path, label):
        if not isinstance(cand_path, str) or not cand_path.strip():
            raise TeamError(f'Reference {ref_name} declares invalid {label} path')
        if ':' in cand_path or chr(92) in cand_path:
            raise TeamError(f'Reference {ref_name} declares unsafe {label} path: {cand_path}')
        norm_m = PurePosixPath(cand_path)
        if norm_m.is_absolute() or '..' in norm_m.parts:
            raise TeamError(f'Reference {ref_name} declares unsafe {label} path: {cand_path}')
        target_m = base_dir / norm_m
        is_m_link = target_m.is_symlink() or (target_m.exists() and bool(getattr(target_m.lstat(), 'st_file_attributes', 0) & 0x400))
        if is_m_link:
            raise TeamError(f'Reference {ref_name} declares linked {label} path: {cand_path}')
        if not target_m.exists() or not target_m.is_file():
            raise TeamError(f'Reference {ref_name} declares missing indexed {label} file: {cand_path}')
        if target_m.suffix not in ('.md', '.yaml', '.yml'):
            raise TeamError(f'Reference {ref_name} declares unsupported file type for {label}: {cand_path}')
        try:
            target_m.resolve(strict=True).relative_to(base_dir.resolve(strict=True))
        except Exception:
            raise TeamError(f'Reference {ref_name} declares {label} path escaping bundle: {cand_path}')

    for mod in (idx_data.get('modules') or {}).values():
        mpath = mod.get('path') if isinstance(mod, dict) else mod
        if mpath:
            _check_path(mpath, 'method')
    for tpath in (idx_data.get('templates') or {}).values():
        if tpath:
            _check_path(tpath, 'template')


def _reference_files(project, catalog, references):
    if references is not None and not isinstance(references, list):
        raise TeamError('References declaration must be a list of reference names')
    out = {}
    project = Path(project).resolve()
    catalog = Path(catalog).resolve()

    # Verify catalog references directory itself is not a symlink or junction
    cat_refs_dir = catalog / 'references'
    if cat_refs_dir.exists():
        is_cr_link = cat_refs_dir.is_symlink() or bool(getattr(cat_refs_dir.lstat(), 'st_file_attributes', 0) & 0x400)
        if is_cr_link:
            raise TeamError('Catalog references directory is a symlink or junction')
        try:
            cat_refs_dir.resolve(strict=True).relative_to(catalog)
        except Exception:
            raise TeamError('Catalog references directory escapes catalog')

    for ref_name in (references or []):
        if not isinstance(ref_name, str) or not ref_name.strip():
            raise TeamError('Reference name must be a non-empty string')
        if ref_name in ('.', '..') or ref_name.startswith('.'):
            raise TeamError(f'Invalid reference name: {ref_name}')
        norm_ref = PurePosixPath(ref_name.replace(chr(92), '/'))
        if norm_ref.is_absolute() or '..' in norm_ref.parts or ':' in ref_name or len(norm_ref.parts) != 1:
            raise TeamError(f'Invalid reference path: {ref_name}')

        # Source validation (directory, links, and containment)
        ref_dir = catalog / 'references' / norm_ref
        is_ref_link = ref_dir.is_symlink() or (ref_dir.exists() and bool(getattr(ref_dir.lstat(), 'st_file_attributes', 0) & 0x400))
        if is_ref_link:
            raise TeamError(f'Unsupported link in reference directory {ref_name}')
        if not ref_dir.exists() or not ref_dir.is_dir():
            raise TeamError(f'Missing reference directory: {ref_name}')
        try:
            ref_dir.resolve(strict=True).relative_to(catalog)
        except Exception:
            raise TeamError(f'Reference containment escape: {ref_name}')

        # Validate catalog source reference index and all declared paths
        _validate_reference_index(ref_dir, ref_name)

        # Target preflight validation before any writes
        target_team = project / 'team'
        target_refs = project / 'team' / 'references'
        target_dir = project / 'team' / 'references' / norm_ref.as_posix()
        for ancestor in (target_team, target_refs, target_dir):
            if ancestor.exists():
                is_anc_link = ancestor.is_symlink() or bool(getattr(ancestor.lstat(), 'st_file_attributes', 0) & 0x400)
                if is_anc_link:
                    raise TeamError(f'Target junction or symlink not permitted in project path: {ancestor}')
                if not ancestor.is_dir():
                    raise TeamError(f'Target path conflict: {ancestor} is not a directory')
                try:
                    ancestor.resolve(strict=True).relative_to(project)
                except Exception:
                    raise TeamError(f'Target path escapes project: {ancestor}')

        # Validate existing files in target_dir if already populated
        if target_dir.exists():
            for existing in target_dir.rglob('*'):
                is_ex_link = existing.is_symlink() or bool(getattr(existing.lstat(), 'st_file_attributes', 0) & 0x400)
                if is_ex_link:
                    raise TeamError(f'Target junction or symlink not permitted inside existing reference: {existing}')

        for item in sorted(ref_dir.rglob('*')):
            is_link = item.is_symlink() or bool(getattr(item.lstat(), 'st_file_attributes', 0) & 0x400)
            if is_link:
                raise TeamError(f'Unsupported link in reference {ref_name}: {item.name}')
            if item.is_file():
                if item.suffix not in ('.md', '.yaml', '.yml'):
                    raise TeamError(f'Unsupported file type in reference {ref_name}: {item.name}')
                rel = item.relative_to(ref_dir).as_posix()
                target_rel = f'team/references/{norm_ref.as_posix()}/{rel}'
                target_file = project / target_rel
                if target_file.exists():
                    if target_file.is_dir():
                        raise TeamError(f'Existing directory in place of reference file: {target_rel}')
                    if target_file.is_symlink() or bool(getattr(target_file.lstat(), 'st_file_attributes', 0) & 0x400):
                        raise TeamError(f'Existing reference file is a symlink or junction: {target_rel}')
                    # Interface compatibility validation on existing template files
                    try:
                        ex_text = target_file.read_text(encoding='utf-8')
                    except Exception:
                        raise TeamError(f'Incompatible existing reference file cannot be read: {target_rel}')
                    if item.name == 'index.yaml':
                        _validate_reference_index(target_dir, ref_name)
                    elif item.name == 'brief.md':
                        req_fields = ['# Editorial Brief', '## Metadata', '## Objective & Audience', '## Sourced Evidence & Facts', '## Call to Action']
                        if any(f not in ex_text for f in req_fields):
                            raise TeamError(f'Incompatible existing reference template: {target_rel} lacks required brief schema')
                    elif item.name == 'draft-review.md':
                        req_fields = ['# Editorial Review', '## Metadata', '## Criteria Evaluation', '## Verdict']
                        if any(f not in ex_text for f in req_fields):
                            raise TeamError(f'Incompatible existing reference template: {target_rel} lacks required review schema')
                else:
                    try:
                        out[target_rel] = item.read_text(encoding='utf-8')
                    except (UnicodeDecodeError, OSError) as e:
                        raise TeamError(f'Cannot read reference file {rel} as valid UTF-8 text: {e}')
    return out


def _has_director(data):
    return any(p['role'] == 'Project Director' and p['status'] == 'active' for p in data['participants'])


def plan_team(project, template, answers=None, source=None, team_answers=None):
    project = Path(project).resolve(strict=True)
    catalog, label = fetch(source)
    source_revision = installation_inventory.catalog_revision(label)
    folder, spec = team_template(catalog, template)
    team_values, team_sources = resolve(spec.get('parameters'), team_answers or {})
    work_tracking = tracking(team_values)
    team = team_values.get('team_name') or spec['name']
    data = roster.load(project)
    if any(t['name'].casefold() == team.casefold() for t in data['teams']):
        raise TeamError(f'Team {team} already exists in this project')
    roles = list(spec['roles'])
    if template == 'starter':
        specialist = team_values['specialist']
        _, specialist_spec = agent_template(catalog, specialist)
        if specialist in ('manager', 'director', 'blank') or specialist_spec.get('custom'):
            raise TeamError('Starter requires an existing catalog specialist')
        roles.append(specialist)
    context = {'project': project.name, 'team': team}
    agents, taken = [], []
    for role in roles:
        _, aspec = agent_template(catalog, role)
        raw_role_refs = aspec.get('references')
        if raw_role_refs is not None and not isinstance(raw_role_refs, list):
            raise TeamError(f'References declaration in agent {role} must be a list of reference names')
        bot = _bot_id(data, aspec['role'], team, taken)
        taken.append(bot)
        agents.append(_agent_plan(project, catalog, label, role, bot, team, f'team/{team}/{bot}', '../../..',
                                  (answers or {}).get(role, {})))
        agents[-1]['references'] = list(raw_role_refs or [])
    conflicts = [a['workspace'] for a in agents if (project / a['workspace']).exists()]
    if conflicts:
        raise TeamError('Workspace already exists: ' + ', '.join(conflicts))
    director = None
    if data['teams'] and not _has_director(data):
        director = ('This will be a second team. A Project Director is proposed as the project entry point; '
                    'appoint it with add-agent --template director after owner confirmation.')
    raw_team_refs = spec.get('references')
    if raw_team_refs is not None and not isinstance(raw_team_refs, list):
        raise TeamError('References declaration in team must be a list of reference names')
    references = list(raw_team_refs or [])
    for role in roles:
        _, aspec = agent_template(catalog, role)
        raw_role_refs = aspec.get('references')
        if raw_role_refs is not None and not isinstance(raw_role_refs, list):
            raise TeamError(f'References declaration in agent {role} must be a list of reference names')
        for r in (raw_role_refs or []):
            if r not in references:
                references.append(r)
    ref_files = _reference_files(project, catalog, references)
    all_proj_files = {**_project_files(project, catalog), **ref_files}
    return {'kind': 'team', 'project': str(project), 'catalog': label, 'source_revision': source_revision, 'template': template,
            'template_version': spec['version'], 'team': team, 'team_sources': team_sources,
            'work_tracking': work_tracking,
            'section': render(spec.get('section') or '{{project}} · {{team}}', context),
            'room': render(spec.get('room') or '{{team}}', context),
            'room_file': render((folder / 'ROOM.md').read_text(encoding='utf-8'), context) if (folder / 'ROOM.md').exists() else None,
            'project_files': all_proj_files, 'references': references, 'agents': agents, 'director_proposal': director}


def plan_agent(project, team, template, answers=None, source=None, extra=None):
    project = Path(project).resolve(strict=True)
    catalog, label = fetch(source)
    source_revision = installation_inventory.catalog_revision(label)
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
    agent['references'] = list(spec.get('references') or [])
    kind = 'director' if template == 'director' else ('custom-agent' if spec.get('custom') else 'catalog-agent')
    return {'kind': kind, 'project': str(project), 'catalog': label, 'source_revision': source_revision, 'template': template, 'team': team,
            'project_files': {**_project_files(project, catalog), **_reference_files(project, catalog, spec.get('references') if spec.get('references') is None or isinstance(spec.get('references'), list) else (_ for _ in ()).throw(TeamError('References declaration in agent must be a list of reference names')))}, 'references': list(spec.get('references') or []) if isinstance(spec.get('references'), list) else [], 'agents': [agent]}


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
    if plan['kind'] == 'team':
        require_tracking(plan.get('work_tracking') or {})
    project = Path(plan['project'])
    if installation_inventory.catalog_revision(plan.get('catalog')) != plan.get('source_revision'):
        raise TeamError('Catalog changed after planning; create a new plan before installing')
    data = roster.load(project)
    expected = roster.version(data)
    for agent in plan['agents']:
        if (project / agent['workspace']).exists():
            raise TeamError(f'Workspace already exists: {agent["workspace"]}')

    # Preflight project files, target ancestry, and existing reference compatibility before any writes
    for relative in plan.get('project_files', {}):
        norm_rel = PurePosixPath(relative.replace(chr(92), '/'))
        if norm_rel.is_absolute() or '..' in norm_rel.parts or ':' in relative:
            raise TeamError(f'Unsafe target path in plan: {relative}')
        target_item = project / relative
        curr = target_item
        while curr != project and curr.parent != curr:
            is_link = curr.is_symlink() or (curr.exists() and bool(getattr(curr.lstat(), 'st_file_attributes', 0) & 0x400))
            if is_link:
                raise TeamError(f'Target junction or symlink not permitted in project path: {curr}')
            if curr.exists():
                try:
                    curr.resolve(strict=True).relative_to(project)
                except Exception:
                    raise TeamError(f'Target path escapes project: {curr}')
            curr = curr.parent

    # Preflight all required reference bundles (including preserved existing bundles)
    for ref_name in plan.get('references', []):
        norm_ref = PurePosixPath(ref_name.replace(chr(92), '/'))
        target_ref_dir = project / 'team' / 'references' / norm_ref.as_posix()
        if target_ref_dir.exists():
            is_dir_link = target_ref_dir.is_symlink() or bool(getattr(target_ref_dir.lstat(), 'st_file_attributes', 0) & 0x400)
            if is_dir_link:
                raise TeamError(f'Target junction or symlink not permitted in reference path: {target_ref_dir}')
            if not target_ref_dir.is_dir():
                raise TeamError(f'Target reference path is not a directory: {target_ref_dir}')
            try:
                target_ref_dir.resolve(strict=True).relative_to(project)
            except Exception:
                raise TeamError(f'Target reference path escapes project: {target_ref_dir}')
            if any(target_ref_dir.iterdir()):
                if not (target_ref_dir / 'index.yaml').exists():
                    raise TeamError(f'Existing reference directory {ref_name} is missing index.yaml')
                _validate_reference_index(target_ref_dir, ref_name)

    created = []
    inventory_created = []
    inventory_expected = {}
    shared_tools = ('.bot-mode/roster.py', '.bot-mode/requests.py')
    shared_tools_preexisting = {rel for rel in shared_tools if (project / rel).exists()}
    from . import requests
    shared_expected = {
        '.bot-mode/roster.py': hashlib.sha256(Path(roster.__file__).read_bytes()).hexdigest(),
        '.bot-mode/requests.py': hashlib.sha256(Path(requests.__file__).read_bytes()).hexdigest(),
    }
    inventory_before = installation_inventory.snapshot(project)
    inventory_before_digest = (hashlib.sha256(inventory_before).hexdigest()
                               if inventory_before is not None else None)
    inventory_written_digest = None
    roster_committed = False
    try:
        for relative, text in plan.get('project_files', {}).items():
            target = project / relative
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8', newline='\n')
                created.append(target)
                inventory_created.append(relative)
                inventory_expected[relative] = hashlib.sha256(text.encode('utf-8')).hexdigest()
        if plan['kind'] == 'team' and plan.get('room_file'):
            target = project / 'team' / plan['team'] / 'ROOM.md'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(plan['room_file'], encoding='utf-8', newline='\n')
            created.append(target)
            relative = target.relative_to(project).as_posix()
            inventory_created.append(relative)
            inventory_expected[relative] = hashlib.sha256(plan['room_file'].encode('utf-8')).hexdigest()
        for agent in plan['agents']:
            root = project / agent['workspace']
            root.mkdir(parents=True)
            created.append(root)
            for relative, text in agent['files'].items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8', newline='\n')
                project_relative = (root / relative).relative_to(project).as_posix()
                inventory_created.append(project_relative)
                inventory_expected[project_relative] = hashlib.sha256(text.encode('utf-8')).hexdigest()
            (root / 'scripts').mkdir()
            helper = root / 'scripts' / 'bot_mode.py'
            shutil.copyfile(agent['helper'], helper)
            project_relative = helper.relative_to(project).as_posix()
            inventory_created.append(project_relative)
            inventory_expected[project_relative] = agent['helper_sha256']
        if plan['kind'] == 'team':
            roster.add_team(data, plan['team'], plan['section'], plan['room'])
            next(t for t in data['teams'] if t['name'] == plan['team'])['work_tracking'] = plan['work_tracking']
        for agent in plan['agents']:
            roster.add_participant(data, agent['bot'], agent['role'], agent['team'], agent['workspace'])
        data['project'].setdefault('tooling', str(REPO))
        roster.install_cli(project)
        requests.install_cli(project)
        inventory_created.extend(rel for rel in shared_tools if rel not in shared_tools_preexisting)
        inventory_expected.update({rel: shared_expected[rel] for rel in shared_tools if rel not in shared_tools_preexisting})
        inventory_written_digest = installation_inventory.record_new_install(
            project, plan['agents'], plan.get('references'), plan.get('catalog'), inventory_created,
            inventory_expected, plan.get('source_revision'), inventory_before_digest)
        roster.save(project, data, expected)
        roster_committed = True
        register_project(project)
    except Exception as error:
        if roster_committed:
            raise
        rollback_error = None
        preserve_created = False
        def remove_created():
            for created_path in reversed(created):
                if created_path.is_dir():
                    shutil.rmtree(created_path, ignore_errors=True)
                elif created_path.exists():
                    created_path.unlink()
        if inventory_written_digest is not None:
            try:
                restored = installation_inventory.restore(project, inventory_before, inventory_written_digest,
                                                          cleanup=remove_created)
                preserve_created = restored is False
            except Exception as failure:
                rollback_error = failure
                preserve_created = True
        elif (isinstance(error, installation_inventory.InventoryError)
              and ('changed since it was read' in str(error) or 'Another inventory writer' in str(error))):
            preserve_created = True
        else:
            remove_created()
        if preserve_created:
            detail = ('; inventory rollback failed: ' + str(rollback_error)) if rollback_error else ''
            raise TeamError('Install failed; generated files were preserved because inventory ownership changed' + detail) from error
        if rollback_error:
            raise TeamError(f'Install failed and inventory rollback also failed: {rollback_error}') from error
        raise
    return [{'bot': a['bot'], 'role': a['role'], 'display_name': a['display_name'], 'workspace': a['workspace'],
             'skill': f'codex-{a["bot"]}', 'needs_chat': True} for a in plan['agents']]
