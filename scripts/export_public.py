"""Assemble the curated public tree from an allowlist, then audit it.

Usage: python -B scripts/export_public.py <empty or new output folder>
"""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = [
    'LICENSE', 'catalog', 'packages/chief-of-staff-v1',
    '.agents/skills/codex-team-launch', '.agents/skills/codex-replace-chat',
    'scripts/team_room', 'scripts/team.py', 'scripts/roster.py', 'scripts/bot_mode.py',
    'scripts/build_plugin.py', 'scripts/public_audit.py', 'scripts/export_public.py',
    'scripts/start_codex_office.ps1', 'Codex Office.cmd',
    'docs/operations/teams.md', 'docs/design/project-roster.md',
    'tests/test_roster.py', 'tests/test_manifest.py', 'tests/test_teams.py', 'tests/test_requests.py',
    'tests/test_escalation.py', 'tests/test_launch.py', 'tests/test_office_project.py', 'tests/test_build_plugin.py',
]
PUBLIC_FILES = ROOT / 'public'


def export(out):
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'{out} must be empty or new')
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
    for item in INCLUDE:
        source = ROOT / item
        target = out / item
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=ignore)
        else:
            shutil.copyfile(source, target)
    if PUBLIC_FILES.exists():
        shutil.copytree(PUBLIC_FILES, out, dirs_exist_ok=True, ignore=ignore)
    return out


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    out = export(args[0])
    print(f'Exported {out}')
    return subprocess.run([sys.executable, '-B', str(ROOT / 'scripts' / 'public_audit.py'), str(out)]).returncode


if __name__ == '__main__':
    sys.exit(main())
