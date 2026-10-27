"""Request ledger: every request between agents waits for its reply.

One JSON file per project and computer (.bot-mode/local/requests.json): requests
travel between the chats of one Codex Desktop. Agents open a request when they ask or assign something and close it
with their reply. Overdue requests escalate: reminder to the recipient, then the
requester's Manager or Director, then the owner. Standalone: stdlib only.
"""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
BOT = re.compile(r'^(owner|[a-z][a-z0-9-]{0,47})$')
SHA_40 = re.compile(r'^[0-9a-f]{40}$')
STEPS = ('reminder', 'manager', 'owner')


class RequestError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def path(project):
    root = Path(project).resolve()
    file = root / '.bot-mode' / 'local' / 'requests.json'
    # .bot-mode/local is untracked: a linked worktree has no ledger of its own and must not start an empty one.
    if not file.exists() and not (file.parent.parent / 'requests.json').exists():
        main = _main_checkout(root)
        if main is not None and ((main / '.bot-mode' / 'local' / 'requests.json').exists()
                                 or (main / '.bot-mode' / 'requests.json').exists()):
            return main / '.bot-mode' / 'local' / 'requests.json'  # load() falls back to the main legacy file
    return file


def _main_checkout(root):
    """Root of the main checkout when root is a linked git worktree, else None (stdlib, no subprocess)."""
    marker = root / '.git'
    try:
        if not marker.is_file():
            return None
        line = marker.read_text(encoding='utf-8').strip()
        if not line.startswith('gitdir:'):
            return None
        gitdir = Path(line.split(':', 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = root / gitdir  # relative to the worktree root, not to the process working directory
        common = (gitdir / (gitdir / 'commondir').read_text(encoding='utf-8').strip()).resolve()
        return common.parent if common.name == '.git' else None
    except (OSError, ValueError, IndexError):
        return None


def load(project):
    file = path(project)
    legacy = file.parent.parent / 'requests.json'
    if not file.exists() and legacy.exists():
        file = legacy
    for attempt in range(100):
        try:
            return json.loads(file.read_text(encoding='utf-8')) if file.exists() else {'schema': 1, 'requests': []}
        except PermissionError:  # Windows: a writer is replacing the file at this instant
            if attempt == 99:
                raise
            time.sleep(0.02)


def version(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()


LOCK_SECONDS = 15.0


@contextmanager
def locked(project, timeout=LOCK_SECONDS):
    """Exclusive cross-process lock on the canonical ledger; the OS drops it if the holder dies.

    Hold it only around read, check and write of the file, never across a network call."""
    lock = path(project).parent / 'requests.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(lock, 'a+b') as handle:
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RequestError('The request ledger is busy. Try again.') from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def _write(project, data):
    file = path(project)
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=file.parent, prefix='requests-', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    for attempt in range(100):
        try:
            os.replace(temporary, file)
            return
        except PermissionError:  # Windows: a reader has the file open for a moment
            if attempt == 99:
                os.unlink(temporary)
                raise
            time.sleep(0.02)


def save(project, data, expected):
    with locked(project):
        if version(load(project)) != expected:
            raise RequestError('Requests changed since they were read. Reload and try again.')
        _write(project, data)


def update(project, change):
    """Read, change and write the ledger as one locked step; change(data) mutates and may raise RequestError."""
    with locked(project):
        data = load(project)
        change(data)
        _write(project, data)
        return data


def _find(data, key):
    found = next((r for r in data['requests'] if r['id'] == key), None)
    if found is None:
        raise RequestError(f'Unknown request {key}')
    return found


def open_request(data, key, sender, recipient, summary, link=None, due_minutes=60, at=None,
                 candidate=None, author=None):
    if not ID.match(key or ''):
        raise RequestError('Request ID: letters, digits, dot, dash, underscore')
    for value, field in ((sender, 'from'), (recipient, 'to')):
        if not BOT.match(value or ''):
            raise RequestError(f'{field} must be a bot ID or owner')
    if sender == recipient:
        raise RequestError('A request needs a different recipient')
    if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 300:
        raise RequestError('Summary must be 1-300 characters')
    if candidate is not None:
        if not isinstance(candidate, str) or not SHA_40.match(candidate.strip().lower()):
            raise RequestError('Candidate must be a 40-hex commit SHA')
        candidate = candidate.strip().lower()
    if author is not None:
        if not isinstance(author, str) or not BOT.match(author.strip()):
            raise RequestError('Author must be a valid bot ID')
        author = author.strip()
    if any(r['id'] == key for r in data['requests']):
        raise RequestError(f'Request {key} already exists')
    at = at or now()
    entry = {'id': key, 'from': sender, 'to': recipient, 'summary': summary.strip(),
             'link': link or '', 'opened': at.isoformat(),
             'due': (at + timedelta(minutes=int(due_minutes))).isoformat(),
             'state': 'open', 'escalations': []}
    if candidate is not None:
        entry['candidate'] = candidate
    if author is not None:
        entry['author'] = author
    data['requests'].append(entry)
    return data


def reply(data, key, by, link=None, outcome='done', at=None, successor=None):
    item = _find(data, key)
    if by != item['to']:
        raise RequestError(f'Only {item["to"]} can reply to {key}')
    if outcome not in ('done', 'blocked', 'failed', 'refused', 'partial'):
        raise RequestError('Outcome must be done, blocked, failed, refused or partial')
    if item.get('state') == 'answered' and item.get('outcome') == outcome:
        return data  # repeated reply: keep the recorded one
    if item.get('state') == 'cancelled':
        raise RequestError(f'{key} was cancelled; it cannot be answered')
    item.update(state='answered', outcome=outcome, answered=(at or now()).isoformat(), reply_link=link)
    if successor:
        item['successor'] = successor
    return data


def accept(data, key, by, link=None, at=None):
    """The requester (or the owner) accepts a partial outcome and records where the remainder is tracked.

    The Monitor stops signalling a partial reply only after this record exists; the follow-up is the link given here
    or the successor the recipient named at the partial reply."""
    item = _find(data, key)
    if by not in (item['from'], 'owner'):
        raise RequestError('Only the requester or the owner can accept a partial outcome')
    if item.get('state') != 'answered' or item.get('outcome') != 'partial':
        raise RequestError(f'{key} is not an answered request with outcome partial')
    follow_up = (link or item.get('successor') or '').strip()
    if not follow_up:
        raise RequestError('Accepting a partial outcome needs the follow-up link (--link)')
    item['accepted'] = {'by': by, 'at': (at or now()).isoformat(), 'link': follow_up}
    return data


def cancel(data, key, by, reason):
    item = _find(data, key)
    if by not in (item['from'], 'owner'):
        raise RequestError('Only the requester or the owner can cancel')
    if item.get('state') == 'cancelled':
        return data  # repeated cancel: keep the recorded reason
    item.update(state='cancelled', cancelled_reason=reason)
    return data


def due_steps(data, at=None):
    """Next escalation step for each overdue open request: (request, step)."""
    at = at or now()
    out = []
    for item in data['requests']:
        if item['state'] != 'open':
            continue
        due = datetime.fromisoformat(item['due'])
        done = [e['step'] for e in item['escalations']]
        if len(done) >= len(STEPS):
            continue
        # Each further step waits one more full due interval.
        interval = due - datetime.fromisoformat(item['opened'])
        if at >= due + interval * len(done):
            out.append((item, STEPS[len(done)]))
    return out


def record_escalation(data, key, step, target, outcome, at=None):
    _find(data, key)['escalations'].append({'step': step, 'target': target, 'outcome': outcome,
                                            'at': (at or now()).isoformat()})
    return data


def summary(data, at=None):
    """Open and overdue counts per recipient."""
    at = at or now()
    out = {}
    for item in data['requests']:
        if item['state'] != 'open':
            continue
        row = out.setdefault(item['to'], {'open': 0, 'overdue': 0})
        row['open'] += 1
        row['overdue'] += datetime.fromisoformat(item['due']) <= at
    return out


def main(argv=None):
    import argparse
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Requests between agents and their replies.')
    parser.add_argument('--project', default='.')
    sub = parser.add_subparsers(dest='action', required=True)
    o = sub.add_parser('open', help='Record a request you send.')
    o.add_argument('id')
    o.add_argument('--from', dest='sender', required=True)
    o.add_argument('--to', required=True)
    o.add_argument('--summary', required=True)
    o.add_argument('--link')
    o.add_argument('--due-minutes', type=int, default=60)
    o.add_argument('--candidate', help='Exact 40-hex candidate commit SHA.')
    o.add_argument('--author', help='Author bot ID for review candidate.')
    r = sub.add_parser('reply', help='Close a request addressed to you after sending the reply.')
    r.add_argument('id')
    r.add_argument('--by', required=True)
    r.add_argument('--link')
    r.add_argument('--outcome', default='done')
    r.add_argument('--successor', help='With --outcome partial: link of the follow-up that tracks the remainder.')
    a = sub.add_parser('accept', help='Requester records that a partial outcome is accepted, with its follow-up.')
    a.add_argument('id')
    a.add_argument('--by', required=True)
    a.add_argument('--link', help='Follow-up issue or pull request; defaults to the successor named in the reply.')
    c = sub.add_parser('cancel')
    c.add_argument('id')
    c.add_argument('--by', required=True)
    c.add_argument('--reason', required=True)
    lst = sub.add_parser('list', help='Open requests, optionally only those for one bot.')
    lst.add_argument('--to')
    args = parser.parse_args(argv)
    try:
        data = load(args.project)
        if args.action == 'list':
            items = [i for i in data['requests'] if i['state'] == 'open' and (not args.to or i['to'] == args.to)]
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0
        def change(data):
            if args.action == 'open':
                open_request(data, args.id, args.sender, args.to, args.summary, args.link, args.due_minutes,
                             candidate=args.candidate, author=args.author)
            elif args.action == 'reply':
                reply(data, args.id, args.by, args.link, args.outcome, successor=args.successor)
            elif args.action == 'accept':
                accept(data, args.id, args.by, args.link)
            else:
                cancel(data, args.id, args.by, args.reason)
        data = update(args.project, change)
        print(json.dumps(_find(data, args.id), ensure_ascii=False, indent=2))
        return 0
    except (RequestError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


def install_cli(project):
    target = path(project).parent.parent / 'requests.py'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(Path(__file__).read_bytes())
    return target


if __name__ == '__main__':
    import sys
    sys.exit(main())
