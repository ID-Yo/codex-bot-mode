"""Check a human-maintained material index without opening original files."""
import argparse
from datetime import date
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def linked(path):
    # Reject Windows reparse points (including junctions) on Python 3.11 as well.
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def check(root):
    folder = Path(root).resolve() / 'human-materials'
    index = folder / 'README.md'
    errors = []
    actual = set()
    if linked(folder):
        return ['Unsupported symlink or junction: human-materials']
    if not index.is_file() or index.is_symlink():
        return ['Missing or unsafe human-materials/README.md']
    def scan_error(error):
        errors.append(f'Cannot inventory folder: {error.filename}')

    for directory, folders, files in os.walk(folder, followlinks=False, onerror=scan_error):
        for name in folders[:]:
            path = Path(directory) / name
            if linked(path):
                errors.append(f'Unsupported symlink or junction: {path.relative_to(folder)}')
                folders.remove(name)
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(folder).as_posix()
            if path.is_symlink():
                errors.append(f'Unsupported symlink: {relative}')
            elif relative not in {'README.md', '.gitignore'} and name not in {'.DS_Store', 'Thumbs.db', 'desktop.ini'}:
                actual.add(relative)
    indexed = set()
    fence = None
    for number, line in enumerate(index.read_text(encoding='utf-8-sig').splitlines(), 1):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        if not line.startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if cells[0] == 'Material' or all(re.fullmatch(r'[-: ]+', c) for c in cells):
            continue
        if len(cells) != 6 or not all(cells):
            errors.append(f'Invalid material row at line {number}: six nonempty fields required')
            continue
        match = re.fullmatch(r'\[[^\]]+\]\(([^)]+)\)', cells[0])
        if not match:
            errors.append(f'Invalid material link at line {number}')
            continue
        try:
            url = urlsplit(match[1])
        except ValueError:
            errors.append(f'Unsafe material path at line {number}')
            continue
        relative = unquote(url.path)
        if (url.scheme or url.netloc or url.query or url.fragment or not relative
                or relative.startswith('/') or '\\' in relative or ':' in relative
                or any(p in {'.', '..', ''} for p in relative.split('/'))):
            errors.append(f'Unsafe material path at line {number}')
            continue
        if relative in indexed:
            errors.append(f'Duplicate row: {relative}')
        indexed.add(relative)
        if cells[4] not in {'unread', 'reviewed', 'partial', 'unparseable'}:
            errors.append(f'Invalid review status: {relative}')
        try:
            if cells[5] != '—' and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', cells[5]):
                raise ValueError()
            reviewed = date.fromisoformat(cells[5]) if cells[5] != '—' else None
            if cells[4] in {'reviewed', 'partial'} and reviewed is None:
                raise ValueError()
        except ValueError:
            errors.append(f'Invalid or missing review date: {relative}')
    errors.extend(f'Missing row: {name}' for name in sorted(actual - indexed))
    errors.extend(f'Stale row: {name}' for name in sorted(indexed - actual))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd(), help='Project root')
    args = parser.parse_args()
    try:
        errors = check(args.root)
    except (OSError, UnicodeError) as error:
        print(f'Cannot check index: {error}')
        return 1
    print('\n'.join(errors) if errors else 'Human Materials index matches recursive inventory.')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
