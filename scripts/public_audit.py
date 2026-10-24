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



CANONICAL_COS_PACKAGE_HASHES ={
    '.agents/skills/codex-chief-of-staff/SKILL.md': 'e98722fd31c2e27798fa2807e18be5a5eabc281380fc115524d3be67dccb12fc',
    '.agents/skills/codex-chief-of-staff/agents/openai.yaml': 'c6da5f3f982bb08073c8a3768c679d5675c52e033bfb14b832b28ca2ce1aaaf3',
    '.gitignore': 'de807abdbf05bc3368639533929cc4309641e39896fcae936e3f5174e0b1df21',
    'AGENTS.md': '4449e1cfbae9951aab2a741b76b44c17c1ac1b1605fef655b00be285eaeaf5fc',
    'README.md': '6edef3e6164b5465b29df03bdc42b2d4207114d68dc1be67799007f0d32401f3',
    'agent/PLAYBOOK.md': '5946494a8696a0dc6242faf5da19fec2101d9b2b1389a7cd5df29cd6766ef295',
    'agent/PROFILE.md': '585ab2f44efec8e3dce6caa3b63a66c960112ffcc55672213ea6ea209629bf39',
    'agent/SOUL.md': '75e038ee7ad7b3d71221e8a4314bb60e0ff3ab83a5ff9e9b19b457de3400b528',
    'agent/STATE.md': '7d70d25b58aec83548706966d019d4705766cfe63006f257f1b11e75f7516df6',
    'agent/agent.yaml': '0c51d0c7bf2328033cbdd9cadef632136b45c2a3c5f134db613d2009c2515199',
    'agent/memory/README.md': 'c41cf3e7dd5ab5adaf78372490392d37e4a013fae56986a3861de648751a7cff',
    'knowledge/accounts.md': 'b0794c1354ed81f256766fcbe3cec60499afaee73480284311f5db2ff205f585',
    'knowledge/owner.md': '448b1d3c3b54dd140677a00cda0754ace9007214f66756214468bc05f369d305',
    'knowledge/people.md': '8cfbe88495eafe85a380e9799e1e1c074a7814d13e2a05a94c0eb4b97c35322b',
    'knowledge/personal.md': 'd21443cfaf093b573039a076efa2f5778b519604868cade758500cb56d33428c',
    'knowledge/preferences.md': '09f6dea35606cee2412632159100befe01c0cad6fad8be8832c05781407f89c2',
    'knowledge/projects.md': '82391c2af474b812b5ad8923b4d2efdd802bd7b586d113c320973f3ce83e6de0',
    'knowledge/README.md': '91954c2ef6f355f472af2f11d2e01e3e94875d0ad27f259b249db7f932dbbf40',
    'knowledge/routines.md': '0f9be6da60d1dea59737203e72312b0731b26b641269c70fee9a93410c4607d7',
    'scripts/backup.ps1': '54f0a67f4dd3799d50e48be709f3afe87c23ef46f28877687a7fa8c2370e6864',
    'scripts/bot_mode.py': '19c890d4dd7407596897c07a37b51b1d72d12348723724aa958aa84901622302',
}

ALLOWED_COS_PACKAGE_FILES = set(CANONICAL_COS_PACKAGE_HASHES.keys())

ALLOWED_COS_PACKAGE_DIRS = {
    '.agents',
    '.agents/skills',
    '.agents/skills/codex-chief-of-staff',
    '.agents/skills/codex-chief-of-staff/agents',
    'agent',
    'agent/memory',
    'knowledge',
    'scripts',
}

def normalize_scaffold_text(text):
    return chr(10).join(text.splitlines()).strip()


def is_canonical_scaffold_file(name, text):
    normalized = normalize_scaffold_text(text)
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

    # Mandatory Chief of Staff package inventory audit when package is exported
    cos_root = root / 'packages' / 'chief-of-staff-v1'
    if cos_root.exists():
        is_cos_link = cos_root.is_symlink() or (cos_root.exists() and bool(getattr(cos_root.lstat(), 'st_file_attributes', 0) & 0x400))
        if is_cos_link or not cos_root.is_dir():
            findings.append(('packages/chief-of-staff-v1', 1, 'private knowledge leak', 'Unsupported symlink or non-directory for Chief of Staff package'))
        else:
            cos_know = cos_root / 'knowledge'
            is_know_link = cos_know.is_symlink() or (cos_know.exists() and bool(getattr(cos_know.lstat(), 'st_file_attributes', 0) & 0x400))
            if not cos_know.exists() or is_know_link or not cos_know.is_dir():
                findings.append(('packages/chief-of-staff-v1', 1, 'private knowledge leak', 'Missing mandatory Chief of Staff knowledge directory'))
            else:
                for req_f in ALLOWED_COS_PACKAGE_FILES:
                    f_target = cos_root / req_f
                    if not f_target.exists() or not f_target.is_file():
                        findings.append(('packages/chief-of-staff-v1', 1, 'private knowledge leak', 'Missing mandatory Chief of Staff template files'))
                        break

    for path in sorted(p for p in root.rglob('*') if '.git' not in p.relative_to(root).parts):
        relative = path.relative_to(root).as_posix()
        if relative in SKIP:
            continue

        # Path-based binary-safe guard for packages/chief-of-staff-v1 before text decoding
        cos_pkg_rel = 'packages/chief-of-staff-v1'
        if relative == cos_pkg_rel or relative.startswith(cos_pkg_rel + '/'):
            is_link = path.is_symlink() or (path.exists() and bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400))
            if relative == cos_pkg_rel:
                continue
            rel_in_pkg = path.relative_to(root / cos_pkg_rel).as_posix()
            if path.is_dir():
                if is_link or rel_in_pkg not in ALLOWED_COS_PACKAGE_DIRS:
                    findings.append(('packages/chief-of-staff-v1', 1, 'private knowledge leak', 'Extra directory or symlink in Chief of Staff package'))
                continue
            if rel_in_pkg not in ALLOWED_COS_PACKAGE_FILES or is_link:
                findings.append(('packages/chief-of-staff-v1', 1, 'private knowledge leak', 'Extra file or symlink in Chief of Staff package'))
                continue
            if rel_in_pkg in CANONICAL_COS_PACKAGE_HASHES:
                try:
                    f_text = path.read_text(encoding='utf-8')
                except (UnicodeDecodeError, OSError):
                    findings.append(('packages/chief-of-staff-v1', 1, 'private knowledge leak', 'Chief of Staff template content cannot be decoded as valid text'))
                    continue
                norm_f = normalize_scaffold_text(f_text)
                if hashlib.sha256(norm_f.encode('utf-8')).hexdigest() != CANONICAL_COS_PACKAGE_HASHES[rel_in_pkg]:
                    findings.append(('packages/chief-of-staff-v1', 1, 'private knowledge leak', 'Chief of Staff template content does not match canonical empty template'))
                # Do NOT continue; global pattern detectors scan content as well

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
                    if relative == 'packages/chief-of-staff-v1' or relative.startswith('packages/chief-of-staff-v1/'):
                        findings.append(('packages/chief-of-staff-v1', number, name, f'Chief of Staff package contains {name}'))
                    else:
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
