"""Project roster: teams, participants and their current Codex chats.

The shared part (.bot-mode/roster.json: teams, participants, roles, workspaces)
is committed with the project, so every computer and every person sees the same
team. Chat bindings (.bot-mode/local/bindings.json: thread IDs and their history)
belong to one Codex Desktop and stay local. Writes are validated, atomic and
refuse stale versions.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

SCHEMA = 1
STATUSES = ('active', 'retired')
ID = re.compile(r'^[a-z][a-z0-9-]{0,47}$')
THREAD = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
SECRET = re.compile(r'(\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|github_pat_|BEGIN [A-Z ]*PRIVATE KEY)')


BINDING = ('thread_id', 'host_id', 'since', 'history')
GITIGNORE = ('# Local to this computer and its Codex Desktop. Everything else in .bot-mode\n'
             '# (roster, commands, agent memory) is shared through Git.\n'
             '/local/\n/team-room-v1/\n/desktop-relay/\n*.lock\n*.tmp\n'
             '# Derived from memory episodes; rebuilt on save.\nsummary.md\npending/\n'
             '# Chat binding of the legacy Manager layout.\ndesktop-binding.json\n')


class RosterError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def path(project):
    return Path(project).resolve() / '.bot-mode' / 'roster.json'


def local_path(project):
    return Path(project).resolve() / '.bot-mode' / 'local' / 'bindings.json'


def ensure_gitignore(project):
    """Keep chat bindings and runtime data out of Git; share the rest."""
    file = path(project).parent / '.gitignore'
    if not file.exists():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(GITIGNORE, encoding='utf-8', newline='\n')
    return file


def link(thread_id):
    return f'codex://threads/{thread_id}' if thread_id else None


def empty(project, name=None):
    project = Path(project).resolve()
    return {'schema': SCHEMA, 'project': {'name': name or project.name}, 'teams': [],
            'participants': [], 'updated_at': None}


def version(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def load(project):
    file = path(project)
    if not file.exists():
        return empty(project)
    data = json.loads(file.read_text(encoding='utf-8'))
    local = local_path(project)
    bindings = json.loads(local.read_text(encoding='utf-8')) if local.exists() else {}
    for p in data.get('participants', []):
        bound = bindings.get(p.get('bot'))
        if bound is not None:
            p.update({k: bound.get(k) for k in BINDING})
        p.setdefault('thread_id', None)
        p.setdefault('host_id', 'local')
        p.setdefault('history', [])
    validate(data)
    return data


def _text(value, field, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RosterError(f'{field} must be 1-{limit} characters')
    if SECRET.search(value):
        raise RosterError(f'{field} looks like a credential')


def validate(data):
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise RosterError('Unsupported roster schema')
    _text(data.get('project', {}).get('name'), 'project.name')
    if data['project'].get('tooling') is not None:
        _text(data['project']['tooling'], 'project.tooling', 260)
    teams = data.get('teams')
    if not isinstance(teams, list):
        raise RosterError('teams must be a list')
    names = set()
    for team in teams:
        _text(team.get('name'), 'team.name', 60)
        if team['name'].casefold() in names:
            raise RosterError(f'Duplicate team {team["name"]}')
        names.add(team['name'].casefold())
        for key in ('section', 'room'):
            if team.get(key) is not None:
                _text(team[key], f'team.{key}')
    bots, threads = set(), set()
    for p in data.get('participants', []):
        if not isinstance(p.get('bot'), str) or not ID.match(p['bot']):
            raise RosterError('bot must be a lowercase identifier')
        if p['bot'] in bots:
            raise RosterError(f'Duplicate participant {p["bot"]}')
        bots.add(p['bot'])
        _text(p.get('role'), 'role', 60)
        if p.get('team') is not None and p['team'].casefold() not in names:
            raise RosterError(f'{p["bot"]}: unknown team {p["team"]}')
        if p.get('status') not in STATUSES:
            raise RosterError(f'{p["bot"]}: status must be one of {STATUSES}')
        if p.get('workspace') is not None:
            _text(p['workspace'], 'workspace', 260)
            if Path(p['workspace']).is_absolute() or '..' in Path(p['workspace']).parts:
                raise RosterError(f'{p["bot"]}: workspace must be relative to the project')
        tid = p.get('thread_id')
        if tid is not None:
            if not THREAD.match(tid):
                raise RosterError(f'{p["bot"]}: invalid thread_id')
            if tid in threads:
                raise RosterError(f'Thread {tid} is assigned twice')
            threads.add(tid)
        for old in p.get('history', []):
            if not THREAD.match(old.get('thread_id', '')) or old.get('status') not in ('replaced', 'retired'):
                raise RosterError(f'{p["bot"]}: invalid history entry')


def _write(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=file.parent, prefix=file.stem + '-', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.replace(temporary, file)


def save(project, data, expected):
    """Write data if the stored roster still has version expected.

    Shared fields go to roster.json; chat bindings go to local/bindings.json.
    """
    current = load(project)
    if version(current) != expected:
        raise RosterError('Roster changed since it was read. Reload and try again.')
    data = json.loads(json.dumps(data))
    data['updated_at'] = now()
    validate(data)
    shared = json.loads(json.dumps(data))
    bindings = {}
    for p in shared['participants']:
        bindings[p['bot']] = {k: p.pop(k, None) for k in BINDING}
    _write(local_path(project), bindings)
    _write(path(project), shared)
    ensure_gitignore(project)
    return data


def participant(data, bot):
    found = next((p for p in data['participants'] if p['bot'] == bot), None)
    if found is None:
        raise RosterError(f'Unknown participant {bot}')
    return found


def active(data):
    return [p for p in data['participants'] if p['status'] == 'active']


def thread_of(project, bot):
    data = load(project)
    p = next((p for p in active(data) if p['bot'] == bot), None)
    return p and p.get('thread_id')


def add_team(data, name, section=None, room=None):
    if any(t['name'].casefold() == name.casefold() for t in data['teams']):
        raise RosterError(f'Team {name} already exists')
    data['teams'].append({'name': name, 'section': section, 'room': room})
    return data


def add_participant(data, bot, role, team=None, workspace=None, thread_id=None, bot_id=None):
    if any(p['bot'] == bot for p in data['participants']):
        raise RosterError(f'Participant {bot} already exists')
    data['participants'].append({'bot': bot, 'bot_id': bot_id or bot, 'role': role, 'team': team,
                                 'workspace': workspace, 'thread_id': thread_id, 'host_id': 'local',
                                 'status': 'active', 'since': now(), 'history': []})
    return data


def set_thread(data, bot, thread_id, reason):
    """Point a participant to a new chat; the previous chat goes to history."""
    p = participant(data, bot)
    if p['status'] != 'active':
        raise RosterError(f'{bot} is retired')
    _text(reason, 'reason', 200)
    if p.get('thread_id') == thread_id:
        return data
    if p.get('thread_id'):
        p['history'].append({'thread_id': p['thread_id'], 'status': 'replaced', 'until': now(), 'reason': reason})
    p['thread_id'], p['since'] = thread_id, now()
    return data


def retire(data, bot, reason):
    p = participant(data, bot)
    _text(reason, 'reason', 200)
    if p.get('thread_id'):
        p['history'].append({'thread_id': p['thread_id'], 'status': 'retired', 'until': now(), 'reason': reason})
    p['thread_id'], p['status'] = None, 'retired'
    return data


def links(data):
    """Current participants with deep links, for agents and Office."""
    return [{'bot': p['bot'], 'role': p['role'], 'team': p.get('team'), 'link': link(p.get('thread_id')),
             'thread_id': p.get('thread_id')} for p in active(data)]


def main(argv=None):
    """Command line: links, show, add-team, add, set-thread, retire."""
    import argparse
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Project roster: teams, participants and current chat links.')
    parser.add_argument('--project', default='.', help='Project root (default: current folder)')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('show', help='Print the full roster with its version.')
    sub.add_parser('links', help='Print active participants with their Codex deep links.')
    team = sub.add_parser('add-team', help='Add a team.')
    team.add_argument('name')
    team.add_argument('--section')
    team.add_argument('--room')
    add = sub.add_parser('add', help='Add a participant.')
    add.add_argument('bot')
    add.add_argument('--role', required=True)
    add.add_argument('--team')
    add.add_argument('--workspace')
    add.add_argument('--thread')
    add.add_argument('--bot-id')
    st = sub.add_parser('set-thread', help='Point a participant to a new chat; the old one goes to history.')
    st.add_argument('bot')
    st.add_argument('thread')
    st.add_argument('--reason', required=True)
    rt = sub.add_parser('retire', help='Retire a participant; history is kept.')
    rt.add_argument('bot')
    rt.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    try:
        data = load(args.project)
        if args.action == 'show':
            print(json.dumps({**data, 'version': version(data)}, ensure_ascii=False, indent=2))
            return 0
        if args.action == 'links':
            print(json.dumps(links(data), ensure_ascii=False, indent=2))
            return 0
        expected = version(data)
        if args.action == 'add-team':
            add_team(data, args.name, args.section, args.room)
        elif args.action == 'add':
            add_participant(data, args.bot, args.role, args.team, args.workspace, args.thread, args.bot_id)
        elif args.action == 'set-thread':
            set_thread(data, args.bot, args.thread, args.reason)
        elif args.action == 'retire':
            retire(data, args.bot, args.reason)
        save(args.project, data, expected)
        print(json.dumps(links(load(args.project)), ensure_ascii=False, indent=2))
        return 0
    except (RosterError, json.JSONDecodeError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


def install_cli(project):
    """Copy this module into <project>/.bot-mode/roster.py as a standalone command."""
    target = path(project).parent / 'roster.py'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(Path(__file__).read_bytes())
    ensure_gitignore(project)
    return target


if __name__ == '__main__':
    import sys
    sys.exit(main())

