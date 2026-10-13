"""Fail if a tree contains private data that must not reach the public repository.

Usage: python -B scripts/public_audit.py <folder>
"""
from pathlib import Path
import re
import sys

PATTERNS = {
    'codex thread id': re.compile(r'\b01a[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'),
    'local user path': re.compile(r'(?i)[A-Z]:[\\/]+(Users[\\/]+User|Coding)\b'),
    'owner account': re.compile(r'(?i)\bID-Yo\b|ivanyosifov|Ivan Yosifov'),
    'private project': re.compile(r'(?i)\bremsup\b|\brazgar\b|kanzen|medcare|fopos'),
    'credential': re.compile(r'\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|github_pat_|BEGIN [A-Z ]*PRIVATE KEY'),
    'cyrillic text': re.compile(r'[\u0400-\u04FF]{3,}'),
}
ALLOWED = {('LICENSE', 'owner account'),
           # Bulgarian microphone labels are matched on purpose.
           ('scripts/team_room/static/media.js', 'cyrillic text'),
           ('README.bg.md', 'cyrillic text'),
           ('README.md', 'cyrillic text'),  # language switch link
           # Credential detector pattern, not a credential.
           ('scripts/team_room/roster.py', 'credential')}
SKIP = {'scripts/public_audit.py'}


def scan(root):
    root = Path(root)
    findings = []
    for path in sorted(p for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts):
        relative = path.relative_to(root).as_posix()
        if relative in SKIP:
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for name, pattern in PATTERNS.items():
                # The catalog homepage links to this public repository only.
                checked = (re.sub(r'https://(?:github\.com/ID-Yo/codex-bot-mode|id-yo\.github\.io/codex-bot-mode)(?=[/"\s?#]|$)',
                                  'https://public.example/catalog', line, flags=re.I)
                           if relative == 'docs/index.html' and name == 'owner account' else line)
                if pattern.search(checked) and (relative, name) not in ALLOWED:
                    findings.append((relative, number, name, line.strip()[:120]))
    return findings


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    findings = scan(args[0])
    for relative, number, name, line in findings:
        print(f'{relative}:{number}: {name}: {line}')
    print(f'{len(findings)} finding(s)')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
