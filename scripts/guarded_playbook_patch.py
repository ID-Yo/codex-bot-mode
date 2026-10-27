"""One-shot guarded patch of six installed PLAYBOOK.md files (#343).

Applies reviewed, insert-only changes to a fixed allowlist of six profile paths.
Nothing is written unless every target passes the preflight: current file hash
equals the planned before hash, the proposed file equals the planned after hash,
the change only inserts lines, and every inserted block occurs in the signed
public release. Each run keeps a backup and a journal, replaces files atomically
and can be rolled back. Only the six PLAYBOOK files and the journal directory
.bot-mode/local/playbook-patch are ever written.

  python -B scripts/guarded_playbook_patch.py check    --project ROOT --plan plan.json --proposed DIR --manifest M --bundle B --trust T
  python -B scripts/guarded_playbook_patch.py apply    ... --confirm
  python -B scripts/guarded_playbook_patch.py rollback --project ROOT --transaction ID
"""
import argparse
import difflib
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import uuid
import zipfile

STATE_DIR = '.bot-mode/local/playbook-patch'
MAX_INSERTED_LINES = 20
STALE_LOCK_SECONDS = 600  # a lock older than this belongs to a run that died
ALLOWED = {
    'manager': ('agents/project-manager/PLAYBOOK.md', 'manager-profile'),
    'developer': ('team/Development/developer/agent/PLAYBOOK.md', 'developer-profile'),
    'development-developer': ('team/Development/development-developer/agent/PLAYBOOK.md', 'development-developer-profile'),
    'code-reviewer': ('team/Development/code-reviewer/agent/PLAYBOOK.md', 'code-reviewer-profile'),
    'development-code-reviewer': ('team/Development/development-code-reviewer/agent/PLAYBOOK.md', 'development-code-reviewer-profile'),
    'verifier': ('team/Development/verifier/agent/PLAYBOOK.md', 'verifier-profile'),
}
REPLACE = os.replace  # target replacement only; tests can inject a failure here
HASH = re.compile(r'[0-9a-f]{64}')


class PatchError(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def normal(data):
    return data.replace(b'\r\n', b'\n').decode('utf-8')


def eol_ok(data, eol):
    crlf = data.count(b'\r\n')
    if eol == 'CRLF':
        return data.count(b'\n') == crlf and b'\r' not in data.replace(b'\r\n', b'')
    return b'\r' not in data


def load_plan(path):
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(plan, dict) or plan.get('schema') != 1 or not isinstance(plan.get('targets'), list):
        raise PatchError('Plan must be schema 1 with a targets list')
    seen = set()
    for item in plan['targets']:
        if not isinstance(item, dict) or item.get('bot') not in ALLOWED or item['bot'] in seen:
            raise PatchError('Plan target is not one of the six allowed bots, or is repeated')
        seen.add(item['bot'])
        if item.get('path') != ALLOWED[item['bot']][0]:
            raise PatchError(f"Plan path for {item['bot']} is not the allowlisted path")
        if item.get('eol') not in ('CRLF', 'LF'):
            raise PatchError(f"Plan EOL for {item['bot']} must be CRLF or LF")
        for field in ('before_sha256', 'after_sha256'):
            if not isinstance(item.get(field), str) or not HASH.fullmatch(item[field]):
                raise PatchError(f"Plan {field} for {item['bot']} is not a sha256")
    if seen != set(ALLOWED):
        raise PatchError('Plan must contain exactly the six allowed targets')
    return plan


def signed_sources(manifest, bundle, trust, platform='windows-x86_64'):
    """Verify the signed public release and return {component id: PLAYBOOK text}."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import component_release
    envelope = component_release.read_json(manifest)
    component_release.verify_bundle(envelope, bundle, component_release.read_json(trust), platform)
    result = {}
    with zipfile.ZipFile(bundle) as archive:
        for _, (_, cid) in ALLOWED.items():
            result[cid] = normal(archive.read(cid + '/agent/PLAYBOOK.md'))
    return result


def target_path(root, rel):
    root = Path(os.path.realpath(root))
    path = root / rel
    if os.path.normcase(os.path.realpath(path)) != os.path.normcase(str(path)):
        raise PatchError(f'{rel}: path crosses a link or does not resolve inside the project')
    if not path.is_file():
        raise PatchError(f'{rel}: target file is missing')
    return path


def preflight(root, plan, proposed_dir, signed):
    """Return the checked targets or raise PatchError listing every problem. Writes nothing."""
    problems, checked = [], []
    for item in plan['targets']:
        bot, rel = item['bot'], item['path']
        try:
            path = target_path(root, rel)
            current = path.read_bytes()
            proposed = (Path(proposed_dir) / (bot + '.PLAYBOOK.proposed.md')).read_bytes()
        except (PatchError, OSError) as error:
            problems.append(f'{bot}: {error}')
            continue
        if sha(current) != item['before_sha256']:
            problems.append(f'{bot}: drift, current file is not the planned before version')
            continue
        if sha(proposed) != item['after_sha256']:
            problems.append(f'{bot}: proposed file does not match the planned after hash')
            continue
        if not eol_ok(current, item['eol']) or not eol_ok(proposed, item['eol']):
            problems.append(f"{bot}: line endings are not {item['eol']} throughout")
            continue
        try:
            old, new = normal(current).split('\n'), normal(proposed).split('\n')
        except UnicodeDecodeError:
            problems.append(f'{bot}: file is not UTF-8')
            continue
        added, bad = 0, False
        source = signed.get(ALLOWED[bot][1], '')
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag == 'equal':
                continue
            if tag != 'insert':
                problems.append(f'{bot}: the change deletes or rewrites existing lines')
                bad = True
                break
            block = '\n'.join(new[j1:j2])
            added += j2 - j1
            if block not in source:
                problems.append(f'{bot}: an inserted block is not in the signed public release')
                bad = True
                break
        if bad:
            continue
        if not 1 <= added <= MAX_INSERTED_LINES:
            problems.append(f'{bot}: inserted line count {added} is outside 1..{MAX_INSERTED_LINES}')
            continue
        checked.append({'bot': bot, 'path': path, 'rel': rel, 'before': item['before_sha256'], 'after': item['after_sha256'],
                        'added': added, 'proposed': proposed})
    if problems:
        raise PatchError('; '.join(problems))
    return checked


def atomic_write(path, data, tag, replace=os.replace):
    temp = path.with_name('.' + path.name + '.' + tag + '.tmp')
    try:
        with open(temp, 'xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def write_journal(folder, journal):
    atomic_write(folder / 'journal.json', json.dumps(journal, indent=1).encode('utf-8'), 'journal')


def state_base(root):
    """The journal directory, created only after every existing parent is proven to be a real folder inside the project."""
    real = Path(os.path.realpath(root))
    current = real
    for part in STATE_DIR.split('/'):
        current = current / part
        if os.path.lexists(current) and (os.path.islink(current) or os.path.normcase(os.path.realpath(current)) != os.path.normcase(str(current))):
            raise PatchError(f'{STATE_DIR}: {part} is a link or junction; refusing to write outside the project')
    current.mkdir(parents=True, exist_ok=True)
    if os.path.normcase(os.path.realpath(current)) != os.path.normcase(str(current)):
        raise PatchError(f'{STATE_DIR} does not resolve inside the project')
    return current


class locked:
    """Exclusive run lock. A lock older than STALE_LOCK_SECONDS is a dead run and is replaced; a fresh one blocks."""

    def __init__(self, base):
        self.path = base / '.lock'

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = datetime.now(timezone.utc).timestamp() - self.path.stat().st_mtime
                    if age > STALE_LOCK_SECONDS:
                        self.path.unlink()
                        continue
                except OSError:
                    continue
                raise PatchError('another guarded patch run holds the lock; wait for it or retry after %d seconds' % STALE_LOCK_SECONDS)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return self
        raise PatchError('could not take the run lock')

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def journal_targets(journal):
    """Only the fixed six targets, with their fixed paths and backup names, may come out of a journal."""
    items = journal.get('targets') if isinstance(journal, dict) else None
    if not isinstance(items, list):
        raise PatchError('Journal has no targets list')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get('bot') not in ALLOWED or item['bot'] in seen:
            raise PatchError('Journal target is not one of the six allowed bots')
        seen.add(item['bot'])
        if (item.get('path') != ALLOWED[item['bot']][0] or item.get('backup') != item['bot'] + '.PLAYBOOK.before.md'
                or not all(isinstance(item.get(k), str) and HASH.fullmatch(item[k]) for k in ('before', 'after'))):
            raise PatchError(f"Journal entry for {item['bot']} does not match the fixed allowlist")
    return items


def restore(root, folder, journal):
    """Put back the backup of every replaced target whose file is still the after version."""
    problems = []
    for item in journal_targets(journal):
        if item['state'] != 'replaced':
            continue
        try:
            path = target_path(root, item['path'])
            now = sha(path.read_bytes())
            backup = (folder / item['backup']).read_bytes()
        except (PatchError, OSError) as error:
            problems.append(f"{item['bot']}: {error}")
            continue
        if sha(backup) != item['before']:
            problems.append(f"{item['bot']}: backup does not match the before hash")
        elif now == item['after']:
            atomic_write(path, backup, 'restore')
            item['state'] = 'restored'
        elif now == item['before']:
            item['state'] = 'restored'
        else:
            problems.append(f"{item['bot']}: current file is neither the after nor the before version; left untouched")
    return problems


def apply(root, plan, proposed_dir, signed, replace=None):
    replace = replace or (lambda a, b: REPLACE(a, b))
    checked = preflight(root, plan, proposed_dir, signed)
    base = state_base(root)
    with locked(base):
        txn = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
        folder = base / txn
        folder.mkdir()
        journal = {'schema': 1, 'transaction': txn, 'release_id': plan.get('release_id'), 'status': 'prepared',
                   'started_at': datetime.now(timezone.utc).isoformat(), 'targets': []}
        for item in checked:
            name = item['bot'] + '.PLAYBOOK.before.md'
            data = item['path'].read_bytes()
            if sha(data) != item['before']:
                raise PatchError(item['bot'] + ': drift while preparing the backup')
            atomic_write(folder / name, data, 'backup')
            journal['targets'].append({'bot': item['bot'], 'path': item['rel'], 'before': item['before'], 'after': item['after'],
                                       'backup': name, 'state': 'pending'})
        write_journal(folder, journal)
        try:
            for item, entry in zip(checked, journal['targets']):
                if sha(item['path'].read_bytes()) != item['before']:
                    raise PatchError(item['bot'] + ': drift just before replacement')
                atomic_write(item['path'], item['proposed'], 'patch', replace)
                if sha(item['path'].read_bytes()) != item['after']:
                    entry['state'] = 'replaced'
                    raise PatchError(item['bot'] + ': written file does not match the after hash')
                entry['state'] = 'replaced'
                write_journal(folder, journal)
        except Exception as error:
            problems = restore(root, folder, journal)
            journal['status'] = 'rolled_back' if not problems else 'rollback_incomplete'
            journal['error'] = str(error)
            write_journal(folder, journal)
            raise PatchError(f'apply failed and was rolled back ({txn}): {error}' + (f' ; rollback problems: {problems}' if problems else ''))
        journal['status'] = 'applied'
        journal['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_journal(folder, journal)
        return txn


def rollback(root, txn):
    if not re.fullmatch(r'[0-9TZ]+-[0-9a-f]{6}', txn):
        raise PatchError('Unknown transaction')
    base = state_base(root)
    with locked(base):
        folder = base / txn
        if not (folder / 'journal.json').is_file() or os.path.normcase(os.path.realpath(folder)) != os.path.normcase(str(folder)):
            raise PatchError('Unknown transaction')
        journal = json.loads((folder / 'journal.json').read_text(encoding='utf-8'))
        journal_targets(journal)
        problems = restore(root, folder, journal)
        journal['status'] = 'rolled_back' if not problems else 'rollback_incomplete'
        write_journal(folder, journal)
        if problems:
            raise PatchError('; '.join(problems))
        return journal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('check', 'apply', 'rollback'))
    parser.add_argument('--project', required=True)
    parser.add_argument('--plan')
    parser.add_argument('--proposed')
    parser.add_argument('--manifest')
    parser.add_argument('--bundle')
    parser.add_argument('--trust')
    parser.add_argument('--platform', default='windows-x86_64')
    parser.add_argument('--transaction')
    parser.add_argument('--confirm', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'rollback':
            if not args.transaction:
                raise PatchError('rollback needs --transaction')
            print(json.dumps({'rolled_back': rollback(args.project, args.transaction)['transaction']}))
            return 0
        if not all((args.plan, args.proposed, args.manifest, args.bundle, args.trust)):
            raise PatchError('check and apply need --plan --proposed --manifest --bundle --trust')
        plan = load_plan(args.plan)
        signed = signed_sources(args.manifest, args.bundle, args.trust, args.platform)
        if args.action == 'check':
            for item in preflight(args.project, plan, args.proposed, signed):
                print(f"ok {item['bot']}: +{item['added']} lines, {item['before'][:10]} -> {item['after'][:10]}")
            print('preflight passed; nothing was written')
            return 0
        if not args.confirm:
            raise PatchError('apply needs --confirm after the plan was approved')
        print(json.dumps({'applied': apply(args.project, plan, args.proposed, signed)}))
        return 0
    except (PatchError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print('refused: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

