"""GitHub issues that wait for the owner, for the owner inbox (#403).

Open issues labelled state:needs-owner are fetched with the GitHub CLI in a background thread and written to a small
cache file. The inbox request itself only reads that cache, so opening "Waiting for me" never waits for the network.
A repository that was never fetched, failed or is stale is reported as incomplete coverage, never as an empty list.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

LABEL = 'state:needs-owner'
TTL_SECONDS = 300  # a request older than this starts a background refresh
STALE_SECONDS = 1800  # cached data older than this is reported as stale
CACHE = Path('.bot-mode/local/owner-issues.json')
MAX_ISSUES = 100
REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
REMOTE = re.compile(r'github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$')
_lock = threading.Lock()
_running = set()


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_remote(url):
    match = REMOTE.search((url or '').strip())
    return f'{match.group(1)}/{match.group(2)}' if match else None


def repos_for(entry, root, runner=subprocess.run):
    """Repositories of one project: the registry entry's repositories/repository, else the git origin of its root."""
    if entry.get('repositories') == []:
        return []
    explicit = entry.get('repositories') if isinstance(entry.get('repositories'), list) else []
    explicit = [r for r in explicit + [entry.get('repository') or entry.get('repo')] if isinstance(r, str) and REPO.match(r)]
    if explicit:
        return sorted(set(explicit))
    try:
        out = runner(['git', '-C', str(root), 'remote', 'get-url', 'origin'], capture_output=True, text=True, timeout=10,
                     creationflags=_flags())
        repo = parse_remote(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        repo = None
    return [repo] if repo else []


def _flags():
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0


def fetch_repo(repo, runner=subprocess.run):
    """Open needs-owner issues of one repository. Raises RuntimeError with a short reason when gh fails."""
    cmd = ['gh', 'issue', 'list', '-R', repo, '--label', LABEL, '--state', 'open', '--limit', str(MAX_ISSUES),
           '--json', 'number,title,labels,updatedAt,url,assignees']
    try:
        out = runner(cmd, capture_output=True, text=True, timeout=45, encoding='utf-8', creationflags=_flags())
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f'gh unavailable: {type(error).__name__}')
    if out.returncode != 0:
        raise RuntimeError('gh failed: ' + (out.stderr or '').strip()[:160])
    try:
        rows = json.loads(out.stdout or '[]')
    except ValueError:
        raise RuntimeError('gh returned invalid JSON')
    issues = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get('number'), int) or not isinstance(row.get('url'), str):
            continue
        issues.append({
            'number': row['number'], 'title': str(row.get('title') or '')[:300], 'url': row['url'],
            'updated_at': row.get('updatedAt'),
            'labels': [str(l.get('name')) for l in row.get('labels', []) if isinstance(l, dict)],
            'assignees': [str(a.get('login')) for a in row.get('assignees', []) if isinstance(a, dict)]})
    return issues


def read_cache(root):
    try:
        data = json.loads((Path(root) / CACHE).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and isinstance(data.get('repos'), dict) else {'repos': {}}
    except (OSError, ValueError):
        return {'repos': {}}


def refresh(root, repos, runner=subprocess.run):
    """Fetch every repository and rewrite the cache. A failed repository keeps its previous issues and records the error."""
    cache = read_cache(root)
    for repo in sorted(set(repos)):
        entry = dict(cache['repos'].get(repo) or {})
        try:
            entry.update(issues=fetch_repo(repo, runner), fetched_at=now(), error=None)
        except RuntimeError as error:
            entry.update(error=str(error), failed_at=now())
        cache['repos'][repo] = entry
    path = Path(root) / CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)
    return cache


def age_seconds(stamp, at=None):
    try:
        value = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return ((at or datetime.now(timezone.utc)) - value).total_seconds()


def ensure_fresh(root, repos, runner=subprocess.run, background=True):
    """Start a background refresh when any repository is missing or older than the TTL. Never blocks the caller."""
    cache = read_cache(root)
    due = [r for r in set(repos)
           if (age_seconds((cache['repos'].get(r) or {}).get('fetched_at')) or TTL_SECONDS + 1) > TTL_SECONDS
           and (age_seconds((cache['repos'].get(r) or {}).get('failed_at')) or TTL_SECONDS + 1) > 60]
    if not due:
        return False
    key = str(Path(root).resolve())
    with _lock:
        if key in _running:
            return False
        _running.add(key)

    def work():
        try:
            refresh(root, due, runner)
        finally:
            with _lock:
                _running.discard(key)

    if background:
        threading.Thread(target=work, name='owner-issues-refresh', daemon=True).start()
    else:
        work()
    return True

