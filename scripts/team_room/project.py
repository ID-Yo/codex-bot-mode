"""The project Codex Office serves: roster participants, profiles, rooms and repository.

Office runs against one project (--project, default this repository). When the
project has a roster, participants, their chats, profile locations and team rooms
come from it; otherwise the original Manager/Developer/Verifier layout applies.
"""
from pathlib import Path
import re
import subprocess

from . import roster, store

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT
LEGACY = {'manager': ('agents/project-manager', 'project-manager'),
          'developer': ('team/Development/developer', 'developer'),
          'verifier': ('team/Development/verifier', 'verifier')}


def configure(project=None):
    """Select the project and load its active participants into store.BOTS."""
    global PROJECT
    PROJECT = Path(project or ROOT).resolve(strict=True)
    bots = [p['bot'] for p in participants()]
    if bots:
        store.BOTS[:] = bots
        from . import work
        work.MANAGER, work.WORKERS = work._appointed(PROJECT)
    return PROJECT


def _roster(project=None):
    try:
        return roster.load(project or PROJECT)
    except (OSError, ValueError):
        return roster.empty(project or PROJECT)


def participants(project=None):
    """Active participants, first team's Manager first, then roster order."""
    data = _roster(project)
    active = [p for p in roster.active(data)]
    first = data['teams'][0]['name'] if data['teams'] else None
    lead = [p for p in active if p['role'] == 'Manager' and p.get('team') == first][:1]
    return lead + [p for p in active if p not in lead]


def participant(bot, project=None):
    return next((p for p in participants(project) if p['bot'] == bot), None)


def label(bot):
    p = participant(bot)
    if p and p.get('workspace'):
        meta = Path(PROJECT, p['workspace'], 'agent', 'agent.yaml')
        if meta.exists():
            from . import manifest
            try:
                name = (manifest.load(meta).get('parameters') or {}).get('display_name', {}).get('value')
                if name:
                    return name
            except ValueError:
                pass
    return p['role'] if p else bot.replace('-', ' ').title()


def profile_location(bot, project=None):
    """(profile dir, helper path, memory project, helper bot ID, memory location label)."""
    root = Path(project or PROJECT).resolve()
    p = participant(bot, root)
    workspace, bot_id = (p.get('workspace'), p.get('bot_id') or bot) if p else LEGACY.get(bot, (None, bot))
    if not workspace:
        raise ValueError('Unknown agent.')
    folder = root / workspace
    if (folder / 'agent').is_dir() and ((folder / 'agent' / 'agent.yaml').is_file() or (folder / 'agent' / 'PROFILE.md').is_file()):
        return (folder / 'agent', folder / 'scripts' / 'bot_mode.py', folder, bot_id,
                f'{workspace}/agent/memory')
    if folder.is_dir():
        return (folder, root / 'scripts' / 'bot_mode.py', root, bot_id,
                f'.bot-mode/agents/{bot_id}')
    raise ValueError('Agent profile not found.')


def teams():
    """Rooms from the roster: one per team, plus Leadership when a Director exists."""
    data = _roster()
    active = roster.active(data)
    rooms = []
    for team in data['teams']:
        members = [p['bot'] for p in active if p.get('team') == team['name']]
        managers = [p['bot'] for p in active if p.get('team') == team['name'] and p['role'] == 'Manager']
        if members:
            rooms.append({'name': team.get('room') or team['name'], 'team': team['name'],
                          'bots': managers + [b for b in members if b not in managers]})
    director = [p['bot'] for p in active if p['role'] == 'Project Director']
    if director and len(rooms) > 1:
        managers = [p['bot'] for p in active if p['role'] == 'Manager']
        rooms.append({'name': 'Leadership', 'team': None, 'bots': director + managers})
    return rooms


def thread(bot):
    p = participant(bot)
    return p and p.get('thread_id')


def threads():
    current = {p['bot']: p['thread_id'] for p in participants() if p.get('thread_id')}
    if current:
        return current
    from . import work
    legacy = {'manager': work.MANAGER, **work.WORKERS}
    return {bot: tid for bot, tid in legacy.items() if tid}


def repository():
    try:
        url = subprocess.run(['git', '-C', str(PROJECT), 'remote', 'get-url', 'origin'], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$', url)
    return match.group(1) if match else None
