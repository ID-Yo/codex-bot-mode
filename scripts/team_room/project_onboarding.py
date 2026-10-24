"""Reviewed, idempotent connection of existing appointed projects to Office.

No Codex project API is invented: new/unappointed projects return explicit setup
steps. Confirmation changes only the host's explicit Office project registry.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from . import dreamdocs_offer, registry, roster
from .store import ProcessLock

MAX_BYTES = 256 * 1024
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
_WRITE_LOCK = threading.RLock()


def _contained_file(file, root):
    resolved = file.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('Project configuration escapes its folder')
    if resolved.exists() and (not resolved.is_file() or resolved.stat().st_size > MAX_BYTES):
        raise ValueError('Project configuration is invalid or too large')
    return resolved


def _configuration(host):
    configured = os.environ.get('CODEX_OFFICE_REGISTRY')
    file = Path(configured) if configured else host / '.bot-mode/local/office-project-roots.json'
    file = Path(file).resolve()
    if not configured and not file.is_relative_to(host):
        raise ValueError('Office registry escapes the host project')
    if file.exists():
        if not file.is_file() or file.stat().st_size > MAX_BYTES:
            raise ValueError('Office registry is invalid or too large')
        data = registry.validate_config(json.loads(file.read_text(encoding='utf-8')))
    else:
        # The unconfigured Office previously served this root as "current".
        # Keep that identity when the first additional project is registered.
        data = {'schema': 1, 'projects': [{'id': 'current', 'name': host.name,
                                        'root': str(host)}], 'standalone': []}
    return file, data


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _undo_local(root, made_folder):
    """Remove only what _create_local writes; unrelated files and a pre-existing root stay."""
    root = Path(root)
    marker = Path(roster.local_path(root)).parent / 'pending-team.json'
    for file in (marker, roster.local_path(root), roster.path(root), Path(roster.path(root)).parent / '.gitignore'):
        try:
            file.unlink()
        except OSError:
            pass
    folders = [Path(roster.local_path(root)).parent, Path(roster.path(root)).parent] + ([root] if made_folder else [])
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            pass


def _create_local(config, handoff):
    """Create the folder and an empty roster. Never overwrites; undone on failure."""
    root = Path(config['folder'])
    made_folder = not root.exists()
    done = []
    try:
        root.mkdir(exist_ok=True)
        done.append('create_folder')
        data = roster.empty(str(root), config['name'])
        roster.save(str(root), data, roster.version(roster.empty(str(root))))
        done.append('init_roster')
        marker = Path(roster.local_path(root)).parent / 'pending-team.json'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({'schema': 1, 'handoff': config['handoff'], 'issue_url': None,
                                      'team': config['initial_team']}), encoding='utf-8')
        done.append('pending_team_marker')
    except Exception:
        _undo_local(root, made_folder)
        raise ValueError('The project folder could not be prepared; nothing was registered')
    return done


def prepare(host_root, payload):
    host = Path(host_root).resolve(strict=True)
    if not isinstance(payload, dict):
        raise ValueError('Project configuration must be an object')
    name, folder = payload.get('name'), payload.get('folder')
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError('Project name must be 1–100 characters')
    if not isinstance(folder, str) or not folder.strip() or len(folder) > 512 or '\x00' in folder:
        raise ValueError('An absolute project folder is required')
    path = Path(folder)
    if not path.is_absolute():
        raise ValueError('An absolute project folder is required')
    root = path.resolve()
    kind = payload.get('kind', 'existing')
    if kind not in ('existing', 'new'):
        raise ValueError('Project kind must be existing or new')
    if root.exists() and not root.is_dir():
        raise ValueError('Project folder is not a directory')
    if kind == 'existing' and not root.is_dir():
        raise ValueError('Existing project folder is unavailable')
    repo = payload.get('repository') or None
    if repo is not None and (not isinstance(repo, str) or len(repo) > 160 or not REPO.fullmatch(repo)):
        raise ValueError('Repository must be owner/repository')
    visible = payload.get('show_dashboard', True)
    if not isinstance(visible, bool):
        raise ValueError('show_dashboard must be boolean')
    team = payload.get('initial_team', 'manager')
    if team not in ('manager', 'development', 'marketing', 'development_marketing'):
        raise ValueError('Choose a supported initial team')
    answer = payload.get('dreamdocs', 'skip')
    if answer not in dreamdocs_offer.CHOICES:
        raise ValueError('Open Dreamdocs answer must be yes, no or skip')
    # An earlier Yes or No stays until the owner asks to change it; no answer counts as Skip.
    saved = dreamdocs_offer.decided(root) if root.is_dir() else None
    answer = saved or answer
    present = root.is_dir() and dreamdocs_offer.already_present(root)
    file, data = _configuration(host)
    row = next((p for p in data['projects'] if Path(p['root']).resolve() == root), None)
    pid = row['id'] if row else 'project-' + hashlib.sha256(str(root).casefold().encode()).hexdigest()[:16]
    source_hashes, managers = {}, []
    if root.is_dir():
        for candidate in (roster.path(root), roster.local_path(root)):
            resolved = _contained_file(candidate, root)
            if resolved.exists():
                source_hashes[str(candidate.relative_to(root))] = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if source_hashes and roster.path(root).exists():
            entries = roster.load(root)
            managers = [p for p in entries['participants'] if p.get('status') == 'active'
                        and p.get('role') == 'Manager' and p.get('thread_id')]
    ready = bool(managers) and kind == 'existing'
    creatable = kind == 'new' and not source_hashes and (not root.exists() or not any(root.iterdir())) and root.parent.is_dir()
    config = {'name': name.strip(), 'folder': str(root), 'repository': repo, 'kind': kind,
              'initial_team': team, 'show_dashboard': visible, 'dreamdocs': answer}
    handoff = ('Open the project in Codex and have its Manager follow the "Optional Open Dreamdocs" step of the '
               'codex-team-launch skill: inspect the project, show the exact install scope and collision plan for the '
               'latest stable Open Dreamdocs release, and copy nothing until I approve it.') if answer == 'yes' and not present else None
    setup = [{'id': 'create_folder', 'supported': True, 'done': root.is_dir()},
             {'id': 'init_roster', 'supported': True, 'done': bool(source_hashes)},
             {'id': 'register', 'supported': True, 'done': row is not None},
             {'id': 'appoint_team', 'supported': False, 'done': bool(managers),
              'handoff': 'Open the new project in Codex and have its Manager run the codex-team-launch skill for "' + team + '".'}] if kind == 'new' else []
    if handoff:
        setup.append({'id': 'adopt_dreamdocs', 'supported': False, 'done': False, 'handoff': handoff})
    return {'schema': 1, 'dreamdocs': {'choice': answer, 'decided': saved is not None, 'already_present': present, 'handoff': handoff},
            'state': 'ready_to_create' if creatable and row is None else 'ready_to_connect' if ready else 'needs_codex_setup',
            'setup_steps': setup,
            'project_id': pid, 'configuration': config, 'already_connected': row is not None,
            'manager_count': len(managers),
            'version': _digest({'configuration': config, 'registry': data, 'roster': source_hashes,
                                'dreamdocs': [saved, present]}),
            'steps': ['Review this project folder and repository.',
                      'Open or create the project in Codex and explicitly approve the appointed Manager/team.',
                      'Confirm an existing appointed project to register it in Office.'],
            'capabilities': {'create_codex_project': False, 'appoint_team': False,
                             'connect_existing': ready, 'create_local_project': creatable and row is None},
            'registry_path': str(file)}


def _offer_outcome(config, plan):
    """Save the owner's answer in the project; a failure never undoes the connection."""
    out = {'choice': config['dreamdocs'], 'recorded': False, 'handoff': plan['dreamdocs']['handoff']}
    try:
        dreamdocs_offer.record(config['folder'], config['dreamdocs'], 'office')
        out['recorded'] = True
    except (ValueError, OSError):
        pass
    return out


def confirm(host_root, payload, expected_version, *, confirmed=False):
    if confirmed is not True:
        raise ValueError('Explicit project connection confirmation required')
    host = Path(host_root).resolve(strict=True)
    with _WRITE_LOCK:
        checked = prepare(host, payload)
        if not isinstance(expected_version, str) or checked['version'] != expected_version:
            raise ValueError('Project or registry changed; review a fresh plan')
        if checked['state'] not in ('ready_to_connect', 'ready_to_create'):
            raise ValueError('Codex setup and an appointed Manager must be confirmed first')
        file, _ = _configuration(host)
        file.parent.mkdir(parents=True, exist_ok=True)
        lock = ProcessLock(file.with_name('project-onboarding.lock'))
        try:
            plan = prepare(host, payload)
            if not isinstance(expected_version, str) or plan['version'] != expected_version:
                raise ValueError('Project or registry changed; review a fresh plan')
            if plan['state'] not in ('ready_to_connect', 'ready_to_create'):
                raise ValueError('Codex setup and an appointed Manager must be confirmed first')
            config = plan['configuration']
            created = []
            made_folder = not Path(config['folder']).exists()
            if plan['state'] == 'ready_to_create':
                created = _create_local({**config, 'handoff': next(s['handoff'] for s in plan['setup_steps'] if s['id'] == 'appoint_team')}, None)
            try:
                current_file, data = _configuration(host)
                if current_file != file:
                    raise ValueError('Registry destination changed; review a fresh plan')
                existing = next((p for p in data['projects'] if p['id'] == plan['project_id']), None)
                if existing is not None:
                    return {'state': 'connected', 'project_id': existing['id'],
                            'already_connected': True, 'show_dashboard': config['show_dashboard'],
                            'dreamdocs': _offer_outcome(config, plan)}
                row = {'id': plan['project_id'], 'name': config['name'], 'root': config['folder']}
                if config['repository']:
                    row['repository'] = config['repository']
                data['projects'].append(row)
                registry.validate_config(data)
                body = json.dumps(data, ensure_ascii=False, indent=2)
                if len(body.encode('utf-8')) > MAX_BYTES:
                    raise ValueError('Office registry size limit reached')
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode='w', dir=file.parent, encoding='utf-8',
                                                     delete=False, prefix='onboarding-', suffix='.tmp') as stream:
                        temporary = Path(stream.name)
                        stream.write(body)
                    os.replace(temporary, file)
                    try:  # the relay allow-list must see the new project at once
                        from .desktop_relay import invalidate_targets
                        invalidate_targets()
                    except Exception:
                        pass
                finally:
                    if temporary and temporary.exists():
                        temporary.unlink()
            except Exception:
                if created:
                    _undo_local(config['folder'], made_folder)
                raise
            if plan['state'] == 'ready_to_create':
                return {'state': 'created_pending_team', 'project_id': plan['project_id'], 'already_connected': False,
                        'show_dashboard': config['show_dashboard'], 'completed': [c for c in created if c != 'pending_team_marker'] + ['register'],
                        'remaining': [s for s in plan['setup_steps'] if not s['supported']],
                        'blocker': {'issue_required': True, 'reason': 'Team appointment needs Codex; no supported Desktop operation exists'},
                        'dreamdocs': _offer_outcome(config, plan)}
            return {'state': 'connected', 'project_id': plan['project_id'],
                    'already_connected': False, 'show_dashboard': config['show_dashboard'],
                    'dreamdocs': _offer_outcome(config, plan)}
        finally:
            lock.close()


ISSUE = re.compile(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*$')


def link_pending_issue(host_root, project_id, issue_url):
    """Record the owner's tracking issue for a project whose team is still unappointed."""
    if not isinstance(issue_url, str) or not ISSUE.fullmatch(issue_url):
        raise ValueError('A GitHub issue URL is required')
    host = Path(host_root).resolve(strict=True)
    with _WRITE_LOCK:
        _, data = _configuration(host)
        row = next((p for p in data['projects'] if p['id'] == project_id), None)
        if row is None:
            raise ValueError('Unknown project')
        root = Path(row['root']).resolve()
        marker = _contained_file(Path(roster.local_path(root)).parent / 'pending-team.json', root)
        if not marker.is_file():
            raise ValueError('This project has no pending team step')
        body = json.loads(marker.read_text(encoding='utf-8'))
        body['issue_url'] = issue_url
        marker.write_text(json.dumps(body), encoding='utf-8')
        return {'project_id': project_id, 'issue_url': issue_url}
