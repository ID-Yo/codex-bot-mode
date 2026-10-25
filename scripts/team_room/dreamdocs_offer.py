"""The owner's answer to the optional Open Dreamdocs offer, kept as one small project-local record.

The offer never installs anything. Not answering counts as Skip. Yes and No are decisions: they
stay until the owner asks to change them. A Skip may be asked once more in the other place.
"""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import tempfile

from . import roster

CHOICES = ('yes', 'no', 'skip')
PLACES = ('office', 'team-launch')
FILE = 'dreamdocs-offer.json'
MAX_BYTES = 16 * 1024
TAG = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+$')
COMMIT = re.compile(r'[0-9a-f]{40}$')


def path(root):
    return Path(roster.local_path(root)).parent / FILE


def _file(root):
    root = Path(root).resolve()
    file = path(root).resolve()
    if not file.is_relative_to(root):
        raise ValueError('The Open Dreamdocs record escapes the project folder')
    return file


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def load(root):
    """The saved record, or None when the owner was never asked. A damaged record is an error."""
    file = _file(root)
    if not file.exists():
        return None
    try:
        if not file.is_file() or file.stat().st_size > MAX_BYTES:
            raise ValueError
        data = json.loads(file.read_text(encoding='utf-8'))
        asked = data['asked_in']
        if (data['schema'] != 1 or data['choice'] not in CHOICES or not isinstance(asked, list)
                or any(place not in PLACES for place in asked) or not isinstance(data['decided_at'], str)
                or data['decided_in'] not in PLACES):
            raise ValueError
        adopted = data.get('adopted')
        if adopted is not None and not (isinstance(adopted, dict) and TAG.match(str(adopted.get('tag')))
                                        and COMMIT.match(str(adopted.get('commit')))):
            raise ValueError
    except (ValueError, KeyError, TypeError, OSError):
        raise ValueError('The Open Dreamdocs record is damaged; ask the owner before changing it') from None
    return data


def already_present(root):
    """True when the project already has the Dreamdocs documentation index and memory folder."""
    root = Path(root).resolve()
    return (root / 'docs' / '00-index.md').is_file() and (root / 'memory').is_dir()


def decided(root):
    """'yes' or 'no' once the owner decided; otherwise None."""
    record = load(root)
    return record['choice'] if record and record['choice'] in ('yes', 'no') else None


def should_offer(root, place):
    """Ask once per place, never again after Yes or No, never when Dreamdocs is already in the project."""
    if place not in PLACES:
        raise ValueError('Unknown place')
    if already_present(root):
        return False
    record = load(root)
    if record is None:
        return True
    return record['choice'] == 'skip' and place not in record['asked_in']


def _save(root, record):
    file = _file(root)
    roster.ensure_gitignore(root)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=file.parent, encoding='utf-8', delete=False,
                                         prefix='dreamdocs-', suffix='.tmp') as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(record, indent=2))
        os.replace(temporary, file)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return record


def record(root, choice, place, *, owner_change=False):
    """Save the answer. Changing Yes or No needs owner_change=True, the owner's own request."""
    if choice not in CHOICES or place not in PLACES:
        raise ValueError('Choose yes, no or skip')
    old = load(root)
    if old and old['choice'] in ('yes', 'no') and choice != old['choice'] and not owner_change:
        raise ValueError('The owner already answered ' + old['choice'] + '; change it only when the owner asks')
    asked = sorted(set((old or {}).get('asked_in', [])) | {place})
    same = old is not None and old['choice'] == choice
    new = {'schema': 1, 'choice': choice, 'decided_at': old['decided_at'] if same else now(),
           'decided_in': old['decided_in'] if same else place, 'asked_in': asked}
    if same and old.get('adopted'):
        new['adopted'] = old['adopted']
    return _save(root, new)


def mark_adopted(root, tag, commit):
    """Note which stable release was adopted after the owner approved the install scope."""
    old = load(root)
    if not old or old['choice'] != 'yes':
        raise ValueError('Adoption needs the owner\'s Yes first')
    if not TAG.match(tag) or not COMMIT.match(commit):
        raise ValueError('Give the stable release tag (vX.Y.Z) and its full commit')
    return _save(root, {**old, 'adopted': {'tag': tag, 'commit': commit, 'at': now()}})
