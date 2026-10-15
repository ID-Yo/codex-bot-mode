"""Fail if a tree contains private data that must not reach the public repository.

Usage: python -B scripts/public_audit.py <folder>
"""
import hashlib
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
CANONICAL_README_HASH = '5716e4dcf5a9dbe34bfd80b70cc3bf3febda1a04c2c68cde1d3bdb103a62e88e'
CANONICAL_GITIGNORE_HASH = '68ffd288875fe615126af629eb1e50b79bb9267a1cdad796819b463df7766700'


def is_canonical_scaffold_file(name, text):
    normalized = text.replace('\r\n', '\n').strip()
    h = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    if name == 'README.md':
        return h == CANONICAL_README_HASH
    if name == '.gitignore':
        return h == CANONICAL_GITIGNORE_HASH
    return False


def scan(root):
    root = Path(root)
    findings = []

    # Mandatory human-materials root directory and exact files audit
    hm_root = root / 'human-materials'
    if not hm_root.exists():
        findings.append(('human-materials', 1, 'private material leak', 'Missing mandatory human-materials root directory'))
    else:
        is_hm_link = hm_root.is_symlink() or bool(getattr(hm_root.lstat(), 'st_file_attributes', 0) & 0x400)
        if is_hm_link:
            findings.append(('human-materials', 1, 'private material leak', 'Unsupported directory symlink or junction for human-materials root'))
        elif not hm_root.is_dir():
            findings.append(('human-materials', 1, 'private material leak', 'human-materials root must be a directory'))
        else:
            entries = list(hm_root.iterdir())
            entry_names = {e.name for e in entries}
            allowed = {'.gitignore', 'README.md'}
            if entry_names != allowed:
                findings.append(('human-materials', 1, 'private material leak', 'human-materials must contain exactly .gitignore and README.md'))

    for path in sorted(p for p in root.rglob('*') if '.git' not in p.relative_to(root).parts):
        relative = path.relative_to(root).as_posix()
        if relative in SKIP:
            continue

        # Path-based binary-safe guard for human-materials before text decoding
        if relative == 'human-materials' or relative.startswith('human-materials/'):
            is_link = path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
            if relative == 'human-materials':
                if is_link or not path.is_dir():
                    findings.append((relative, 1, 'private material leak', 'Unsupported symlink or non-directory for human-materials root'))
                continue
            if path.is_dir():
                if is_link:
                    findings.append((relative, 1, 'private material leak', 'Unsupported directory symlink or junction in exported human-materials'))
                else:
                    findings.append((relative, 1, 'private material leak', 'Extra directory in exported human-materials'))
                continue
            if relative not in ('human-materials/README.md', 'human-materials/.gitignore') or is_link:
                findings.append((relative, 1, 'private material leak', 'Extra file or symlink in exported human-materials'))
                continue
            if relative in ('human-materials/README.md', 'human-materials/.gitignore'):
                try:
                    text = path.read_text(encoding='utf-8')
                except (UnicodeDecodeError, OSError):
                    findings.append((relative, 1, 'private material leak', f'Cannot read exported {relative}'))
                    continue
                if not is_canonical_scaffold_file(path.name, text):
                    findings.append((relative, 1, 'modified material scaffold', f'Exported {relative} does not match canonical empty template'))

        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for name, pattern in PATTERNS.items():
                checked = (re.sub('https://(?:github\\.com/ID-Yo/codex-bot-mode|id-yo\\.github\\.io/codex-bot-mode)(?=[/`"\\\'\\s?#\\)]|$)',
                                  'https://public.example/catalog', line, flags=re.I)
                           if name == 'owner account' else line)
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
