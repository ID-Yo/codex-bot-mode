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
import shlex
import subprocess
import sys
import tempfile
import time

ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
BOT = re.compile(r'^(owner|[a-z][a-z0-9-]{0,47})$')
SHA_40 = re.compile(r'^[0-9a-f]{40}$')
STEPS = ('reminder', 'manager', 'owner')
VERDICTS = {'approve': 'done', 'request-changes': 'done', 'blocked': 'blocked'}
WORK_LINK = re.compile(r'^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)$')


def work_link(link):
    """The issue or pull request URL of a request link without fragment, query or trailing slash, or None."""
    base = re.split(r'[#?]', link or '', maxsplit=1)[0].rstrip('/')
    return base if WORK_LINK.match(base) else None


COMMENT_LINK = re.compile(r'^(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/\d+)#issuecomment-(\d+)$')


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


def open_owner(data, summary, link=None, sender='manager', due_minutes=1440, at=None):
    """One call for the Manager to record something that waits for the owner (shown in Office 'Waiting for me').

    The ID is generated. An open owner request with the same link is returned unchanged, so recording twice is safe.
    """
    link = (link or '').strip()
    if link:
        for r in data['requests']:
            if r.get('state') == 'open' and r.get('to') == 'owner' and r.get('link') == link:
                return r['id']
    at = at or now()
    key = 'own-' + at.strftime('%Y%m%d-%H%M%S') + '-' + hashlib.sha1((summary or '').encode()).hexdigest()[:6]
    open_request(data, key, sender, 'owner', summary, link or None, due_minutes, at=at)
    return key


def close_owner(data, ref, note='', at=None):
    """Record that the owner answered: ref is the request ID or its link. Closes every matching open owner request."""
    hits = [r for r in data['requests'] if r.get('state') == 'open' and r.get('to') == 'owner'
            and ref in (r['id'], r.get('link'))]
    if not hits:
        raise RequestError(f'No open owner request {ref}')
    for r in hits:
        r.update(state='answered', outcome='done', answered=(at or now()).isoformat(), reply_link=None)
        if note:
            r['note'] = note[:300]
    return [r['id'] for r in hits]


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


def _outbox_send_args(project, item, by, outcome, link=None, successor=None):
    """The outbox.py send command line for the report of a closed request, or None without a project outbox.

    Reports always go to the project Manager (#399): the ledger row and its reply link remain the record for any other
    requester."""
    outbox = Path(project).resolve() / '.bot-mode' / 'outbox.py'
    if not outbox.is_file():
        return None
    text = (f"Request {item['id']} completed by {by}: {outcome}. "
            f"Task: {item.get('summary', '').strip()}. Evidence: {link or 'none'}. "
            f"Remaining/next owner: {successor or 'none'}.")
    return [sys.executable, '-B', str(outbox), '--project', str(Path(project).resolve()), 'send', '--to', 'manager', '--by', by,
            '--dedupe-key', f'request:{item["id"]}:{by}:{outcome}', '--text', text]


def enqueue_outbox_report(project, item, by, outcome, link=None, successor=None):
    """Queue a concise completion report through the project-local outbox. Raises when the enqueue fails."""
    args = _outbox_send_args(project, item, by, outcome, link, successor)
    if args is None:
        return False
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    result = subprocess.run(args, cwd=str(Path(project).resolve()), capture_output=True, text=True, timeout=15,
                            creationflags=flags)
    if result.returncode:
        raise RequestError('Outbox enqueue failed: ' + (result.stderr.strip() or result.stdout.strip())[:200])
    return True


def enqueue_outbox_report_after_closeout(project, item, by, outcome, link=None, successor=None):
    """Queue the report of a request that is already closed, without ever failing the closeout (#399).

    Any error or timeout becomes a warning with the command to queue the report by hand; the caller still prints the
    closed item and exits 0, so the agent does not repeat a closeout that already happened."""
    try:
        return enqueue_outbox_report(project, item, by, outcome, link, successor)
    except Exception as error:  # includes subprocess.TimeoutExpired and OSError
        args = _outbox_send_args(project, item, by, outcome, link, successor) or []
        quote = (lambda part: "'" + part.replace("'", "''") + "'") if os.name == 'nt' else shlex.quote
        print(f"warning: request {item.get('id')} is closed, but its outbox report was not queued "
              f"({type(error).__name__}: {str(error)[:200]}). Do not repeat the closeout; queue the report by hand: "
              + ' '.join(quote(part) for part in args), file=sys.stderr)
        return False


def verdict_marker(key, by, candidate, verdict):
    """The exact first line of a verdict comment; nothing else in a comment is ever read as a verdict."""
    return f'<!-- cbm-verdict request={key} by={by} candidate={candidate} verdict={verdict} -->'


def verdict_check(data, key, by, verdict, candidate):
    """Validate a verdict against the request and return (item, outcome). Raises RequestError; writes nothing."""
    item = _find(data, key)
    if by != item['to']:
        raise RequestError(f'Only {item["to"]} can post the verdict for {key}')
    if verdict not in VERDICTS:
        raise RequestError('Verdict must be approve, request-changes or blocked')
    if not isinstance(candidate, str) or not SHA_40.match(candidate):
        raise RequestError('The verdict needs the exact 40-hex candidate commit SHA')
    if item.get('candidate') and item['candidate'] != candidate:
        raise RequestError(f'Candidate {candidate[:12]} is not the request candidate {item["candidate"][:12]}')
    if not work_link(item.get('link')):
        raise RequestError(f'{key} has no GitHub issue or pull request link to post the verdict on')
    if item.get('state') == 'cancelled':
        raise RequestError(f'{key} was cancelled; it cannot be answered')
    if item.get('state') == 'answered':
        raise RequestError(f'{key} is already answered ({item.get("outcome")}); the recorded reply is kept')
    return item, VERDICTS[verdict]


def close_with_verdict(data, key, by, verdict, candidate, comment, at=None):
    """Answer the request with a verified verdict comment: reply link, outcome and the verdict record in one change."""
    item, outcome = verdict_check(data, key, by, verdict, candidate)
    m = COMMENT_LINK.match(comment or '')
    if not m or m.group(1) != work_link(item.get('link')):
        raise RequestError('The verdict comment must be a comment of the request link')
    reply(data, key, by, comment, outcome, at=at)
    item['verdict'] = {'value': verdict, 'candidate': candidate, 'comment': comment}
    return data


def _gh(args, stdin=None, timeout=60):
    result = subprocess.run(['gh', *args], input=stdin, capture_output=True, text=True, encoding='utf-8', timeout=timeout)
    if result.returncode:
        raise RequestError('GitHub call failed: ' + (result.stderr.strip().splitlines() or ['no message'])[-1][:200])
    return result.stdout


def post_verdict(project, key, by, verdict, candidate, body, gh=None):
    """Post the verdict comment on the request's issue or pull request and close the request, in that order.

    Validation happens first and writes nothing. The comment carries the marker line with request id, recipient,
    candidate and verdict. The request is closed only after GitHub returned the comment URL. If the ledger step fails
    after a successful post, the URL is returned in the error so close_existing_verdict finishes it without a second
    comment. The ledger lock is never held across the GitHub call."""
    gh = gh or _gh
    candidate = (candidate or '').strip().lower()
    item, _ = verdict_check(load(project), key, by, verdict, candidate)
    repo, number = WORK_LINK.match(work_link(item['link'])).groups()
    text = verdict_marker(key, by, candidate, verdict) + '\n' + body.strip() + '\n'
    out = gh(['api', f'repos/{repo}/issues/{number}/comments', '-F', 'body=@-', '--jq', '.html_url'], stdin=text)
    comment = out.strip()
    if not COMMENT_LINK.match(comment):
        raise RequestError('GitHub did not return a comment URL; the request stays open')
    try:
        update(project, lambda data: close_with_verdict(data, key, by, verdict, candidate, comment))
    except (RequestError, OSError) as error:  # PermissionError and other ledger write failures included
        recovery = f'verdict {key} --by {by} --verdict {verdict} --candidate {candidate} --comment {comment}'
        saved = _save_pending(project, key, by, verdict, candidate, comment, recovery)
        raise RequestError(f'The verdict was posted ({comment}) but the request is still open: {error}. '
                           f'Finish with: {recovery}' + (f' (recovery record: {saved})' if saved else '')) from None
    return comment


def _save_pending(project, key, by, verdict, candidate, comment, recovery):
    """Best effort: keep the published comment URL and the recovery command beside the ledger. Returns the file or None."""
    try:
        folder = path(project).parent / 'verdict-pending'
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / f'{key}.json'
        file.write_text(json.dumps({'request': key, 'by': by, 'verdict': verdict, 'candidate': candidate, 'comment': comment,
                                    'recovery': recovery, 'at': now().isoformat()}, indent=2), encoding='utf-8')
        return str(file)
    except OSError:
        return None


def close_existing_verdict(project, key, by, verdict, candidate, comment, gh=None):
    """Close a request from an already posted verdict comment, after reading it back and matching its marker line."""
    gh = gh or _gh
    candidate = (candidate or '').strip().lower()
    item, _ = verdict_check(load(project), key, by, verdict, candidate)
    m = COMMENT_LINK.match(comment or '')
    if not m:
        raise RequestError('--comment must be a GitHub issue or pull request comment URL')
    if m.group(1) != work_link(item.get('link')):
        raise RequestError('The comment URL is not on the request link')
    repo, number = WORK_LINK.match(work_link(item['link'])).groups()
    # Read the comment from the request's repository and bind it to the request's exact issue or pull request by the
    # issue_url GitHub reports for the comment id, so a rewritten URL cannot point at a comment of another item.
    try:
        found = json.loads(gh(['api', f'repos/{repo}/issues/comments/{m.group(2)}', '--jq', '{body: .body, issue_url: .issue_url}']))
    except ValueError:
        raise RequestError('GitHub returned an unreadable comment') from None
    if not isinstance(found, dict) or found.get('issue_url') != f'https://api.github.com/repos/{repo}/issues/{number}':
        raise RequestError('The comment does not belong to the request issue or pull request')
    text = found.get('body') or ''
    if text.lstrip('\ufeff').replace('\r', '').split('\n', 1)[0] != verdict_marker(key, by, candidate, verdict):
        raise RequestError('The comment does not start with the verdict marker for this request, recipient, candidate and verdict')
    update(project, lambda data: close_with_verdict(data, key, by, verdict, candidate, comment))
    return comment


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
    v = sub.add_parser('verdict', help='Post your review or verification verdict on the request link and close the request.')
    v.add_argument('id')
    v.add_argument('--by', required=True)
    v.add_argument('--verdict', required=True, choices=sorted(VERDICTS))
    v.add_argument('--candidate', required=True, help='Exact 40-hex commit SHA that was reviewed.')
    group = v.add_mutually_exclusive_group(required=True)
    group.add_argument('--body-file', help='Verdict text; it is posted with the marker line and then the request is closed.')
    group.add_argument('--comment', help='Close from an already posted verdict comment that starts with the marker line.')
    w = sub.add_parser('owner', help='Manager: record something that waits for the owner (shown in Office Waiting for me).')
    w.add_argument('summary', help='What the owner must decide or do, up to 300 characters')
    w.add_argument('--link', help='GitHub issue that holds the question (strongly recommended)')
    w.add_argument('--from', dest='sender', default='manager')
    w.add_argument('--due-minutes', type=int, default=1440)
    d = sub.add_parser('owner-done', help='Manager: the owner answered; close the owner request by ID or link.')
    d.add_argument('ref', help='Request ID or its link')
    d.add_argument('--note', default='')
    c = sub.add_parser('cancel')
    c.add_argument('id')
    c.add_argument('--by', required=True)
    c.add_argument('--reason', required=True)
    lst = sub.add_parser('list', help='Open requests, optionally only those for one bot.')
    lst.add_argument('--to')
    args = parser.parse_args(argv)
    try:
        if args.action == 'verdict':
            if args.body_file:
                comment = post_verdict(args.project, args.id, args.by, args.verdict, args.candidate,
                                       Path(args.body_file).read_text(encoding='utf-8'))
            else:
                comment = close_existing_verdict(args.project, args.id, args.by, args.verdict, args.candidate, args.comment)
            data = load(args.project)
            item = _find(data, args.id)
            enqueue_outbox_report_after_closeout(args.project, item, args.by, item.get('outcome', 'done'), comment)
            print(json.dumps(item, ensure_ascii=False, indent=2))
            return 0
        data = load(args.project)
        if args.action == 'list':
            items = [i for i in data['requests'] if i['state'] == 'open' and (not args.to or i['to'] == args.to)]
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0
        if args.action == 'owner':
            holder = {}
            update(args.project, lambda data: holder.update(id=open_owner(data, args.summary, args.link, args.sender, args.due_minutes)))
            print(json.dumps(_find(load(args.project), holder['id']), ensure_ascii=False, indent=2))
            return 0
        if args.action == 'owner-done':
            holder = {}
            update(args.project, lambda data: holder.update(ids=close_owner(data, args.ref, args.note)))
            print('Closed owner request(s): ' + ', '.join(holder['ids']))
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
        item = _find(data, args.id)
        if args.action == 'reply':
            enqueue_outbox_report_after_closeout(args.project, item, args.by, item.get('outcome', args.outcome),
                                                 item.get('reply_link'), item.get('successor'))
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0
    except (RequestError, ValueError, OSError) as error:
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
