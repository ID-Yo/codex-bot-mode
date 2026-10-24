"""Build the Codex Bot Mode plugin and register it in the personal marketplace.

Usage:
  python -B scripts/build_plugin.py [--out <folder>] [--no-register]

Default output is ~/plugins/codex-bot-mode, registered in
~/.agents/plugins/marketplace.json (Codex's personal marketplace).
"""
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
NAME = 'codex-bot-mode'
VERSION = '0.1.0'
MODULES = ('__init__.py', 'manifest.py', 'roster.py', 'requests.py', 'teams.py', 'installation_inventory.py', 'dreamdocs_offer.py')
SKILLS = ('codex-team-launch', 'codex-replace-chat')


def build(out):
    out = Path(out).resolve()
    if out.exists():
        manifest = out / '.codex-plugin' / 'plugin.json'
        if not manifest.exists() or json.loads(manifest.read_text(encoding='utf-8')).get('name') != NAME:
            raise SystemExit(f'Refusing to replace {out}: it is not a {NAME} plugin build')
        shutil.rmtree(out)
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
    shutil.copytree(ROOT / 'catalog', out / 'catalog', ignore=ignore)
    shutil.copytree(ROOT / 'packages' / 'chief-of-staff-v1', out / 'templates' / 'chief-of-staff', ignore=ignore)
    (out / 'scripts' / 'team_room').mkdir(parents=True)
    for name in ('team.py', 'roster.py'):
        shutil.copyfile(ROOT / 'scripts' / name, out / 'scripts' / name)
    for name in MODULES:
        shutil.copyfile(ROOT / 'scripts' / 'team_room' / name, out / 'scripts' / 'team_room' / name)
    for skill in SKILLS:
        shutil.copytree(ROOT / '.agents' / 'skills' / skill, out / 'skills' / skill, ignore=ignore)
    for name in ('LICENSE',):
        if (ROOT / name).exists():
            shutil.copyfile(ROOT / name, out / name)
    (out / 'README.md').write_text(
        '# Codex Bot Mode\n\nPersistent Codex agents organized in teams. Ask any Codex chat in your project to\n'
        'start a team (for example "start a Development team"); the codex-team-launch skill plans it, asks you to\n'
        'confirm, installs the agents with empty memory and creates their chats. Templates are in catalog/;\n'
        'the Chief of Staff template is in templates/chief-of-staff/.\n\nCommands: python -B scripts/team.py --help\n',
        encoding='utf-8')
    (out / '.codex-plugin').mkdir()
    (out / '.codex-plugin' / 'plugin.json').write_text(json.dumps({
        'name': NAME, 'version': VERSION,
        'description': 'Persistent Codex agents in teams: launch teams, appoint agents, keep chat links current.',
        'author': {'name': 'Codex Bot Mode'}, 'license': 'MIT',
        'interface': {'displayName': 'Codex Bot Mode', 'shortDescription': 'Launch agent teams in Codex',
                      'longDescription': 'Launch Development, Marketing and Sales teams with a Manager per team, a Project '
                                         'Director for multi-team projects, custom agents, standing approvals, chat '
                                         'replacement and a request ledger with reminders.',
                      'developerName': 'Codex Bot Mode', 'category': 'Productivity', 'capabilities': ['Read', 'Write'],
                      'defaultPrompt': ['Start a Development team in this project.', 'Add a Marketing team to this project.',
                                        'Replace the Developer chat; it became too long.']},
        'keywords': ['agents', 'teams', 'manager', 'roster'], 'skills': './skills/'}, indent=2) + '\n', encoding='utf-8')
    return out


def register(out):
    home = Path.home()
    marketplace = home / '.agents' / 'plugins' / 'marketplace.json'
    data = (json.loads(marketplace.read_text(encoding='utf-8')) if marketplace.exists()
            else {'name': 'personal', 'interface': {'displayName': 'Personal'}, 'plugins': []})
    relative = './' + Path(out).resolve().relative_to(home).as_posix()
    entry = {'name': NAME, 'source': {'source': 'local', 'path': relative},
             'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'}, 'category': 'Productivity'}
    data['plugins'] = [p for p in data.get('plugins', []) if p.get('name') != NAME] + [entry]
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    marketplace.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    return marketplace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', default=str(Path.home() / 'plugins' / NAME))
    parser.add_argument('--no-register', action='store_true')
    args = parser.parse_args(argv)
    out = build(args.out)
    print(f'Built {out}')
    if not args.no_register:
        print(f'Registered in {register(out)}')


if __name__ == '__main__':
    main()
