import threading
import time
"""Private, bounded project-status snapshot for Office project cards (CBM-095).

One local writer (Fos through scripts/project_status.py) stores approved per-project
summaries in <project>/.bot-mode/local/project-status.json. Office reads it through the
token-protected local API. Nothing here parses agent STATE, knowledge or transcripts,
and snapshot content is never written to logs.
"""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

SCHEMA = 1
MAX_BYTES = 64 * 1024
CACHE_MAX_BYTES = 256 * 1024  # derived cache envelope: snapshot plus per project watermarks
WORK_MAX_AGE = 120  # seconds; worst case staleness = this + worker interval (60) + one read (~25 s) = ~205 s < 300 s (#125)
WORK_FETCH = None  # tests inject a GitHub fetcher; None uses gh
WORK_COUNTS = ('open', 'ready', 'in_progress', 'review', 'blocked', 'backlog', 'done', 'total')
STATUSES = ('working', 'needs_decision', 'blocked', 'completed', 'paused', 'unknown', 'unconfigured')
TASK_STATES = ('open', 'in_progress', 'blocked', 'done')
DECISION_STATES = ('pending', 'answered', 'cancelled')
AUTHORITIES = ('github', 'local', 'google_tasks', 'none')
SOURCE_KINDS = ('fos_relay', 'request_ledger', 'github', 'google_tasks', 'local')
HTTPS_HOSTS = ('github.com', 'tasks.google.com')  # exact hosts; no wildcards
ID = re.compile(r'^[a-z][a-z0-9-]{0,47}$')
REPO_ID = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
ITEM_ID = re.compile(r'^[A-Za-z0-9#][A-Za-z0-9._:#/-]{0,79}$')
THREAD = re.compile(r'^codex://threads/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
LIMITS = {'projects': 20, 'reports': 10, 'tasks': 20, 'decisions': 10, 'unassigned': 20}


class StatusError(ValueError):
    pass


def path(project):
    return Path(project).resolve() / '.bot-mode' / 'local' / 'project-status.json'


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _fields(value, where, required, optional=()):
    if not isinstance(value, dict):
        raise StatusError(f'{where} must be an object')
    unknown = set(value) - set(required) - set(optional)
    missing = [k for k in required if k not in value]
    if unknown:
        raise StatusError(f'{where}: unexpected key')
    if missing:
        raise StatusError(f'{where}: required key missing')


def _text(value, where, limit, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise StatusError(f'{where} must be text of 1-{limit} characters')


def _time(value, where, optional=False):
    if value is None and optional:
        return
    try:
        if not isinstance(value, str) or not value.endswith('Z'):
            raise ValueError
        datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError:
        raise StatusError(f'{where} must be an ISO UTC time ending in Z') from None


def _choice(value, where, choices):
    if value not in choices:
        raise StatusError(f'{where} must be one of {list(choices)}')


def safe_url(value, where, optional=True):
    if value is None and optional:
        return
    if not isinstance(value, str) or len(value) > 300:
        raise StatusError(f'{where} must be a URL of at most 300 characters')
    if THREAD.match(value):
        return
    parts = urlsplit(value)
    if parts.scheme != 'https' or parts.hostname not in HTTPS_HOSTS or parts.username or parts.password or parts.port:
        raise StatusError(f'{where} must be codex://threads/<id> or https on {list(HTTPS_HOSTS)}')


def _items(value, where, kind, check):
    if not isinstance(value, list) or len(value) > LIMITS[kind]:
        raise StatusError(f'{where} must be a list of at most {LIMITS[kind]}')
    seen = set()
    for i, item in enumerate(value):
        check(item, f'{where}[{i}]')
        if item['id'] in seen:
            raise StatusError(f'{where}: duplicate id')
        seen.add(item['id'])


def _item_id(value, where):
    if not isinstance(value, str) or not ITEM_ID.match(value):
        raise StatusError(f'{where}.id must be a stable identifier')


def _report(item, where):
    _fields(item, where, ('id', 'at', 'from', 'text'), ('source_url',))
    _item_id(item['id'], where); _time(item['at'], where + '.at')
    _text(item['from'], where + '.from', 60); _text(item['text'], where + '.text', 600)
    safe_url(item.get('source_url'), where + '.source_url')


def _task(item, where):
    _fields(item, where, ('id', 'title', 'state'), ('url',))
    _item_id(item['id'], where); _text(item['title'], where + '.title', 120)
    _choice(item['state'], where + '.state', TASK_STATES); safe_url(item.get('url'), where + '.url')


def _suggestion(item, where):
    _fields(item, where, ('options', 'by', 'at', 'source_url'), ('recommended', 'context'))
    options = item.get('options')
    if not isinstance(options, list) or not 1 <= len(options) <= 4:
        raise StatusError(f'{where}.options must be a list of 1 to 4 items')
    seen_opts = set()
    for i, opt in enumerate(options):
        _text(opt, f'{where}.options[{i}]', 80)
        if opt in seen_opts:
            raise StatusError(f'{where}.options has duplicate items')
        seen_opts.add(opt)
    rec = item.get('recommended')
    if rec is not None:
        _text(rec, f'{where}.recommended', 80)
        if rec not in seen_opts:
            raise StatusError(f'{where}.recommended must be one of the provided options')
    _text(item.get('context'), f'{where}.context', 280, True)
    _text(item['by'], f'{where}.by', 60)
    _time(item['at'], f'{where}.at')
    safe_url(item['source_url'], f'{where}.source_url', False)


def _decision(item, where):
    _fields(item, where, ('id', 'question', 'requested_by', 'requested_at', 'state', 'source_url'),
            ('context', 'due', 'urgency', 'answer', 'suggestion'))
    _item_id(item['id'], where); _text(item['question'], where + '.question', 280)
    _text(item.get('context'), where + '.context', 600, True)
    _text(item['requested_by'], where + '.requested_by', 60); _time(item['requested_at'], where + '.requested_at')
    _time(item.get('due'), where + '.due', True); _choice(item.get('urgency', 'normal'), where + '.urgency', ('normal', 'urgent'))
    _choice(item['state'], where + '.state', DECISION_STATES); safe_url(item['source_url'], where + '.source_url', False)
    answer = item.get('answer')
    if item['state'] == 'answered':
        # A receipt or guess never resolves a decision: require recorded authoritative evidence.
        _fields(answer, where + '.answer', ('text', 'by', 'at', 'source_url'))
        _text(answer['text'], where + '.answer.text', 280); _text(answer['by'], where + '.answer.by', 60)
        _time(answer['at'], where + '.answer.at'); safe_url(answer['source_url'], where + '.answer.source_url', False)
    elif answer is not None:
        raise StatusError(f'{where}: answer is only valid for answered decisions')
    if 'suggestion' in item:
        _suggestion(item['suggestion'], where + '.suggestion')


def _source(item, where):
    _fields(item, where, ('kind', 'label', 'available'), ('url', 'as_of', 'error'))
    _choice(item['kind'], where + '.kind', SOURCE_KINDS); _text(item['label'], where + '.label', 60)
    if not isinstance(item['available'], bool):
        raise StatusError(f'{where}.available must be true or false')
    safe_url(item.get('url'), where + '.url'); _time(item.get('as_of'), where + '.as_of', True)
    _text(item.get('error'), where + '.error', 120, True)


def _work(item, where):
    _fields(item, where, ('as_of', 'state'), WORK_COUNTS + ('sources', 'error'))
    _time(item['as_of'], where + '.as_of'); _choice(item['state'], where + '.state', ('known', 'unknown'))
    for key in WORK_COUNTS:
        if key in item and (type(item[key]) is not int or not 0 <= item[key] <= 99999):
            raise StatusError(f'{where}.{key} must be 0-99999')
    if item['state'] == 'known' and any(key not in item for key in WORK_COUNTS):
        raise StatusError(f'{where} needs every counter when known')
    if not isinstance(item.get('sources', []), list) or len(item.get('sources', [])) > 6:
        raise StatusError(f'{where}.sources must be a short list')
    for source in item.get('sources', []):
        _text(source, where + '.sources', 80)
    _text(item.get('error'), where + '.error', 160, True)


def _project(item, where):
    _fields(item, where, ('id', 'name', 'status', 'summary', 'source'),
            ('manager', 'open_tasks', 'tracking', 'stale_after_hours', 'reports', 'tasks', 'decisions', 'work'))
    if not isinstance(item['id'], str) or not ID.match(item['id']):
        raise StatusError(f'{where}.id must match {ID.pattern}')
    _text(item['name'], where + '.name', 60); _choice(item['status'], where + '.status', STATUSES)
    _text(item['summary'], where + '.summary', 280); _text(item.get('manager'), where + '.manager', 60, True)
    if item.get('open_tasks') is not None and (type(item['open_tasks']) is not int or not 0 <= item['open_tasks'] <= 9999):
        raise StatusError(f'{where}.open_tasks must be null or 0-9999')
    if 'tracking' in item:
        _fields(item['tracking'], where + '.tracking', ('authority',), ('url',))
        _choice(item['tracking']['authority'], where + '.tracking.authority', AUTHORITIES)
        safe_url(item['tracking'].get('url'), where + '.tracking.url')
    hours = item.get('stale_after_hours', 48)
    if type(hours) is not int or not 1 <= hours <= 720:
        raise StatusError(f'{where}.stale_after_hours must be 1-720')
    _source(item['source'], where + '.source')
    if 'work' in item:
        _work(item['work'], where + '.work')
    _items(item.get('reports', []), where + '.reports', 'reports', _report)
    _items(item.get('tasks', []), where + '.tasks', 'tasks', _task)
    _items(item.get('decisions', []), where + '.decisions', 'decisions', _decision)


def _unassigned(item, where):
    _fields(item, where, ('id', 'at', 'text'), ('source_url',))
    _item_id(item['id'], where); _time(item['at'], where + '.at'); _text(item['text'], where + '.text', 280)
    safe_url(item.get('source_url'), where + '.source_url')


def validate(data):
    _fields(data, 'snapshot', ('schema', 'generated_at', 'generated_by', 'projects'), ('unassigned',))
    if data['schema'] != SCHEMA:
        raise StatusError('Unsupported snapshot schema')
    _time(data['generated_at'], 'generated_at')
    if not isinstance(data['generated_by'], str) or not ID.match(data['generated_by']):
        raise StatusError('generated_by must be a bot id')
    _items(data['projects'], 'projects', 'projects', _project)
    _items(data.get('unassigned', []), 'unassigned', 'unassigned', _unassigned)
    # Measure what is stored: upsert and the cache write indent=2, so compact size would under-count (#98).
    if len(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')) + 1 > MAX_BYTES:
        raise StatusError(f'Snapshot exceeds {MAX_BYTES} bytes')
    return data


def read(project):
    """Return {'available': bool, ...}; errors never include snapshot content."""
    file = path(project)
    if not file.exists():
        return {'available': False, 'error': 'No project status snapshot yet'}
    try:
        if file.stat().st_size > MAX_BYTES:
            raise StatusError('Snapshot is too large')
        data = validate(json.loads(file.read_text(encoding='utf-8')))
        return {'available': True, 'snapshot': data}
    except (StatusError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, StatusError) else 'Snapshot could not be read'
        return {'available': False, 'error': message[:200]}


def _merge_list(old, new):
    merged = {item['id']: item for item in old}
    for item in new:
        merged[item['id']] = item
    return list(merged.values())


def merge(snapshot, update, writer='chief-of-staff'):
    """Pure merge of an update into a snapshot (or None); returns a validated new snapshot."""
    _fields(update, 'update', (), ('projects', 'unassigned'))
    data = copy.deepcopy(snapshot) if snapshot else {'schema': SCHEMA, 'generated_at': now(), 'generated_by': writer, 'projects': []}
    projects = {p['id']: p for p in data['projects']}
    stored, updated = [], []
    for item in update.get('projects', []):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            raise StatusError('Each project update needs an id')
        old = projects.get(item['id'])
        merged = dict(old or {})
        for key, value in item.items():
            merged[key] = _merge_list(merged.get(key, []), value) if key in ('reports', 'tasks', 'decisions') and isinstance(value, list) else value
        if 'reports' in merged:
            merged['reports'] = sorted(merged['reports'], key=lambda r: r.get('at', ''), reverse=True)[:LIMITS['reports']]
        projects[item['id']] = merged
        (updated if old else stored).append(item['id'])
    data = {**data, 'projects': list(projects.values()), 'generated_at': now(), 'generated_by': writer}
    if 'unassigned' in update:
        data['unassigned'] = _merge_list(data.get('unassigned', []), update['unassigned'])
    validate(data)  # bounds enforced before any write
    return data, stored, updated


def _current(project):
    result = read(project)
    if path(project).exists() and not result['available']:
        raise StatusError('Existing snapshot is invalid; fix or remove it first: ' + result['error'])
    return result.get('snapshot')


def dry_merge(project, update, writer='chief-of-staff'):
    """Validate an update against the current snapshot in memory; nothing is copied or written."""
    _, stored, updated = merge(_current(project), update, writer)
    return {'new_projects': stored, 'updated_projects': updated}


def upsert(project, update, writer='chief-of-staff'):
    """Take the one-writer lock first, then read, merge by stable id and write atomically."""
    file = path(project)
    file.parent.mkdir(parents=True, exist_ok=True)
    lock = file.with_suffix('.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise StatusError('Another project-status writer is active') from None
    try:
        os.close(fd)
        data, stored, updated = merge(_current(project), update, writer)
        handle, temporary = tempfile.mkstemp(dir=file.parent, prefix='project-status-', suffix='.tmp')
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write('\n')
        os.replace(temporary, file)
    finally:
        lock.unlink(missing_ok=True)
    return {'result': 'stored', 'path': str(file), 'new_projects': stored, 'updated_projects': updated,
            'projects': len(data['projects'])}

# --- Derived Read-Model Cache and Background Refresh (CBM-125) ---

DERIVED_CACHE_NAME = 'derived-read-model.json'


def derived_cache_path(project):
    return Path(project).resolve() / '.bot-mode' / 'local' / DERIVED_CACHE_NAME


def resolve_approved_source_path(project_root, project_id):
    env_map = os.environ.get('CODEX_PROJECT_STATUS_SOURCES')
    if env_map:
        try:
            m = json.loads(env_map)
            if project_id in m:
                return Path(m[project_id]).resolve()
        except Exception:
            pass

    from . import registry
    reg_file = registry.registry_path(project_root)
    if reg_file and Path(reg_file).exists():
        try:
            reg_data = json.loads(Path(reg_file).read_text(encoding='utf-8'))
            for p_entry in reg_data.get('projects', []):
                if p_entry.get('id') == project_id and p_entry.get('root'):
                    proot = Path(p_entry['root'])
                    candidates = [
                        proot / 'agents' / 'project-manager' / 'agent' / 'project-status.json',
                        proot / 'agents' / 'project-manager' / 'project-status.json',
                        proot / 'team' / 'RoutePort' / 'manager' / 'agent' / 'project-status.json',
                        proot / 'team' / 'RoutePort' / 'manager' / 'project-status.json',
                    ]
                    roster_p = proot / '.bot-mode' / 'roster.json'
                    if roster_p.exists():
                        try:
                            rdata = json.loads(roster_p.read_text(encoding='utf-8'))
                            for part in rdata.get('participants', []):
                                if part.get('role') == 'Manager' and part.get('workspace'):
                                    ws = Path(part['workspace'])
                                    candidates.append(proot / ws / 'agent' / 'project-status.json')
                                    candidates.append(proot / ws / 'project-status.json')
                        except Exception:
                            pass
                    for cand in candidates:
                        if cand.exists():
                            return cand.resolve()
        except Exception:
            pass
    return None


def report_summary(data):
    """Plain project line from one validated report: its own optional summary (<= 280 characters, cut at a word), else
    counts of its structured lists. next/active prose is never concatenated into it; unknown counts are left out."""
    text = data.get('summary')
    if text is not None:
        if not isinstance(text, str):
            raise StatusError('Manager report summary must be text')
        text = ' '.join(text.split())
        if text:
            if len(text) <= 280:
                return text
            return text[:279].rsplit(' ', 1)[0].rstrip(' ,;:.') + '…'
    parts = []
    for key, label, count in (
            ('active', 'reported work item', lambda v: len(v)),
            ('issues', 'open issue', lambda v: len([i for i in v if isinstance(i, dict) and i.get('state') == 'OPEN'])),
            ('blockers', 'blocker', lambda v: len(v)),
            ('owner_decisions', 'owner decision', lambda v: len(v))):
        value = data.get(key)
        if isinstance(value, list) and count(value):
            n = count(value)
            parts.append(f"{n} {label}{'' if n == 1 else 's'}")
    return 'Manager report: ' + ', '.join(parts) + '.' if parts else 'Manager report received; it lists no counts.'


def parse_approved_manager_report(project_id, source_path, expected_project_name=None, expected_repo=None):
    source_path = Path(source_path).resolve()
    if not source_path.exists():
        raise StatusError(f'Source file {source_path} does not exist')
    if source_path.stat().st_size > MAX_BYTES:
        raise StatusError('Manager report source file exceeds size limit')
    data = json.loads(source_path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1:
        raise StatusError('Unsupported manager report schema_version')

    # Validate mandatory report fields
    reported_at = data.get('reported_at_utc')
    if not reported_at or not isinstance(reported_at, str) or not reported_at.endswith('Z'):
        raise StatusError('Manager report missing valid reported_at_utc ISO time ending in Z')
    _time(reported_at, 'reported_at_utc')

    report_proj = data.get('project')
    if not report_proj or not isinstance(report_proj, str):
        raise StatusError('Manager report missing project name')
    if expected_project_name and report_proj.lower() != expected_project_name.lower():
        raise StatusError(f'Manager report project mismatch: expected {expected_project_name}, got {report_proj}')

    repo = data.get('repository')
    if not expected_repo:
        raise StatusError('Missing trusted canonical owner/repository contract for approved source')
    if not isinstance(expected_repo, str) or not REPO_ID.match(expected_repo):
        raise StatusError(f'Invalid trusted repository format (must be owner/repo): {expected_repo}')
    if not repo or not isinstance(repo, str) or not REPO_ID.match(repo):
        raise StatusError(f'Manager report repository must be exact owner/repo format: {repo}')
    if repo != expected_repo:
        raise StatusError(f'Manager report repository mismatch: expected {expected_repo}, got {repo}')

    # Must have active or next to claim progress
    next_step = data.get('next') or ''
    active_list = data.get('active') or []
    if not isinstance(active_list, list):
        raise StatusError('Manager report active must be a list')
    if not next_step and not active_list:
        raise StatusError('Manager report has empty next and active; cannot establish working progress')
    summary = report_summary(data)

    pname = report_proj
    status = 'blocked' if data.get('blockers') else 'working'
    _choice(status, 'status', STATUSES)

    reports = []
    for i, comp in enumerate(data.get('completed', [])):
        at = comp.get('source_at_utc') or data.get('reported_at_utc') or now()
        text = (comp.get('item', '') + ': ' + comp.get('evidence', '')).strip()[:600]
        reports.append({
            'id': f'comp-{i}',
            'at': at,
            'from': f'{pname} Manager',
            'text': text
        })

    tasks = []
    repo = data.get('repository')
    for iss in data.get('issues', []):
        t_item = {
            'id': f"#{iss.get('number')}",
            'title': f"Issue #{iss.get('number')}",
            'state': 'open' if iss.get('state') == 'OPEN' else 'done'
        }
        if repo:
            t_item['url'] = f'https://github.com/{repo}/issues/{iss.get("number")}'
        tasks.append(t_item)
    for pr in data.get('pull_requests', []):
        t_item = {
            'id': f"#{pr.get('number')}",
            'title': f"PR #{pr.get('number')}",
            'state': 'in_progress' if pr.get('state') == 'OPEN' else 'done'
        }
        if repo:
            t_item['url'] = f'https://github.com/{repo}/pull/{pr.get("number")}'
        tasks.append(t_item)

    source = {
        'kind': 'local',
        'label': f'{pname} Manager Report',
        'available': True,
        'as_of': reported_at
    }

    open_tasks = len(data.get('active', [])) + len([i for i in data.get('issues', []) if i.get('state') == 'OPEN'])
    proj_obj = {
        'id': project_id,
        'name': pname,
        'status': status,
        'summary': summary,
        'manager': f'{pname} Manager',
        'open_tasks': min(9999, max(0, open_tasks)),
        'source': source,
        'reports': reports[:LIMITS['reports']],
        'tasks': tasks[:LIMITS['tasks']],
        'decisions': []
    }
    return proj_obj


def work_counters(project_entry, at):
    """Fresh work counters for one registry project from GitHub labels, ledger and task records (#259 source)."""
    import sys
    scripts = str(Path(__file__).resolve().parents[1])
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import status_sources
    stamp = at.strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        _, summary = status_sources.project_status(project_entry, at, fetch=WORK_FETCH)
    except Exception as error:
        return {'as_of': stamp, 'state': 'unknown', 'error': (type(error).__name__ + ': project root unavailable')[:160]}
    if summary['problems'] or summary['state'] == 'unknown':
        reason = '; '.join(summary['problems']) or 'no GitHub labels, ledger or task records for this project'
        return {'as_of': stamp, 'state': 'unknown', 'sources': summary['sources'][:6], 'error': reason[:160]}
    by = summary['by_status']
    count = lambda *names: sum(by.get(n, 0) for n in names)
    work = {'as_of': stamp, 'state': 'known', 'ready': count('ready'), 'in_progress': count('in-progress'),
            'review': count('review'), 'blocked': count('blocked'), 'backlog': count('backlog'), 'done': count('done'),
            'total': summary['total'], 'sources': summary['sources'][:6]}
    work['open'] = work['ready'] + work['in_progress'] + work['review'] + work['blocked'] + work['backlog']
    return work


def apply_work(p_obj, entry, at):
    """Counters and a fresh source replace the stale Manager report numbers; the report stays as summary."""
    if not entry or not entry.get('root'):
        return
    work = work_counters(entry, at)
    p_obj['work'] = work
    if work['state'] == 'known':
        p_obj['open_tasks'] = work['open']
        p_obj['source'] = {'kind': 'github' if any(s.startswith('github:') for s in work['sources']) else 'request_ledger',
                           'label': 'Work status', 'available': True, 'as_of': work['as_of']}
    else:
        p_obj['open_tasks'] = None
        p_obj['source'] = {'kind': 'local', 'label': 'Work status', 'available': False, 'as_of': work['as_of'],
                           'error': work['error'][:120]}


def assemble_derived_read_model(project_root, sources_map=None):
    project_root = Path(project_root).resolve()
    cos_res = read(project_root)
    cos_data = cos_res.get('snapshot') if cos_res.get('available') else None
    cos_projects = {p['id']: p for p in cos_data.get('projects', [])} if cos_data else {}
    unassigned = cos_data.get('unassigned', []) if cos_data else []

    old_watermarks = {}
    c_path = derived_cache_path(project_root)
    if c_path.exists():
        try:
            raw_cache = json.loads(c_path.read_text(encoding='utf-8'))
            old_watermarks = raw_cache.get('watermarks', {})
        except Exception:
            pass

    reg_projects = {}
    from . import registry
    reg_file = registry.registry_path(project_root)
    if reg_file and Path(reg_file).exists():
        try:
            r_data = json.loads(Path(reg_file).read_text(encoding='utf-8'))
            for p_e in r_data.get('projects', []):
                if p_e.get('id'):
                    reg_projects[p_e['id']] = p_e
        except Exception:
            pass

    all_pids = list(cos_projects.keys())
    for r_pid in reg_projects:
        if r_pid not in all_pids:
            all_pids.append(r_pid)
    if sources_map:
        for s_pid in sources_map:
            if not s_pid.endswith('_repo') and s_pid not in all_pids:
                all_pids.append(s_pid)
    if 'routeport' not in all_pids:
        all_pids.append('routeport')

    new_watermarks = copy.deepcopy(old_watermarks)
    derived_projects = []
    for pid in all_pids:
        src = sources_map.get(pid) if sources_map else resolve_approved_source_path(project_root, pid)
        p_meta = reg_projects.get(pid) or cos_projects.get(pid) or {}
        expected_name = p_meta.get('name') or pid.replace('-', ' ').title()

        # Explicit approved mapping contract strictly requires full canonical owner/repo
        expected_repo = p_meta.get('repository') or p_meta.get('repo')
        if not expected_repo and isinstance(p_meta.get('tracking'), dict):
            t_url = p_meta['tracking'].get('url', '')
            if 'github.com/' in t_url:
                cand_part = t_url.split('github.com/')[-1].strip('/')
                parts = cand_part.split('/')
                if len(parts) >= 2:
                    cand_repo = f'{parts[0]}/{parts[1]}'
                    if REPO_ID.match(cand_repo):
                        expected_repo = cand_repo
        if not expected_repo:
            env_map = os.environ.get('CODEX_PROJECT_STATUS_SOURCES')
            if env_map:
                try:
                    m = json.loads(env_map)
                    if f'{pid}_repo' in m and REPO_ID.match(m[f'{pid}_repo']):
                        expected_repo = m[f'{pid}_repo']
                except Exception:
                    pass
        if not expected_repo and isinstance(sources_map, dict) and f'{pid}_repo' in sources_map and REPO_ID.match(sources_map[f'{pid}_repo']):
            expected_repo = sources_map[f'{pid}_repo']
        if not expected_repo and (p_meta.get('repository') or p_meta.get('repo')):
            cand_repo = p_meta.get('repository') or p_meta.get('repo')
            if REPO_ID.match(cand_repo):
                expected_repo = cand_repo

        p_obj = None
        last_wm = old_watermarks.get(pid, {})
        last_prod_as_of = last_wm.get('as_of')
        last_prod_data = last_wm.get('data')

        if src and Path(src).exists():
            try:
                parsed = parse_approved_manager_report(pid, Path(src), expected_project_name=expected_name, expected_repo=expected_repo)
                new_as_of = parsed.get('source', {}).get('as_of')
                if last_prod_as_of and new_as_of and new_as_of < last_prod_as_of:
                    p_obj = copy.deepcopy(last_prod_data) if last_prod_data else parsed
                    p_obj['source']['error'] = f'Ignored older report {new_as_of} (cached {last_prod_as_of})'
                else:
                    p_obj = parsed
                    new_watermarks[pid] = {'as_of': new_as_of, 'data': copy.deepcopy(parsed)}
            except Exception as e:
                if last_prod_data:
                    p_obj = copy.deepcopy(last_prod_data)
                    p_obj['source']['available'] = False
                    p_obj['source']['error'] = f'Report error: {str(e)[:60]}'
                    new_watermarks[pid] = last_wm
                elif pid in cos_projects:
                    p_obj = copy.deepcopy(cos_projects[pid])
                    p_obj['source'] = {'kind': 'local', 'label': f'{expected_name} Manager Report', 'available': False, 'error': f'Report error: {str(e)[:60]}'}
                else:
                    p_obj = {
                        'id': pid, 'name': expected_name, 'status': 'unknown',
                        'summary': f'Report error: {str(e)[:60]}',
                        'source': {'kind': 'local', 'label': f'{expected_name} Manager Report', 'available': False, 'error': f'Report error: {str(e)[:60]}'}
                    }
        elif src is not None and not Path(src).exists():
            if last_prod_data:
                p_obj = copy.deepcopy(last_prod_data)
                p_obj['source']['available'] = False
                p_obj['source']['error'] = 'Approved manager status source file missing'
                new_watermarks[pid] = last_wm
            elif pid in cos_projects:
                p_obj = copy.deepcopy(cos_projects[pid])
                p_obj['source'] = {'kind': 'local', 'label': f'{expected_name} Manager Report', 'available': False, 'error': 'Approved manager status source file missing'}
            else:
                p_obj = {
                    'id': pid, 'name': expected_name, 'status': 'unknown',
                    'summary': 'Approved manager status source file missing.',
                    'source': {'kind': 'local', 'label': f'{expected_name} Manager Report', 'available': False, 'error': 'Approved manager status source file missing'}
                }
        elif pid in cos_projects:
            p_obj = copy.deepcopy(cos_projects[pid])
        else:
            p_obj = {
                'id': pid, 'name': expected_name, 'status': 'unknown',
                'summary': 'No approved producer report yet.',
                'source': {'kind': 'local', 'label': 'Registry metadata only', 'available': False, 'error': 'No approved manager status source mapped'}
            }

        if pid in cos_projects:
            cos_p = cos_projects[pid]
            if 'decisions' in cos_p:
                p_obj['decisions'] = copy.deepcopy(cos_p['decisions'])
            if cos_p.get('status') == 'paused':
                p_obj['status'] = 'paused'
            if 'tracking' in cos_p:
                p_obj['tracking'] = copy.deepcopy(cos_p['tracking'])

        apply_work(p_obj, reg_projects.get(pid), datetime.now(timezone.utc))
        derived_projects.append(p_obj)

    snapshot = {
        'schema': SCHEMA,
        'generated_at': now(),
        'generated_by': 'read-model-worker',
        'projects': derived_projects,
        'unassigned': unassigned
    }
    validate(snapshot)

    # Write envelope containing clean valid snapshot and internal watermarks
    cache_envelope = {
        'snapshot': snapshot,
        'watermarks': new_watermarks
    }

    cache_file = derived_cache_path(project_root)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if len(json.dumps(cache_envelope, ensure_ascii=False, indent=2).encode('utf-8')) > CACHE_MAX_BYTES:
        raise StatusError('Derived read model cache exceeds its size limit')
    handle, temporary = tempfile.mkstemp(dir=cache_file.parent, prefix='derived-read-model-', suffix='.tmp')
    with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as out:
        json.dump(cache_envelope, out, ensure_ascii=False, indent=2)
        out.write('\n')
    os.replace(temporary, cache_file)
    return snapshot


def read_model(project_root, sources_map=None):
    cache_file = derived_cache_path(project_root)
    if cache_file.exists():
        try:
            if cache_file.stat().st_size <= CACHE_MAX_BYTES:
                raw = json.loads(cache_file.read_text(encoding='utf-8'))
                snap = raw.get('snapshot') if isinstance(raw, dict) and 'snapshot' in raw else raw
                data = validate(snap)
                return {'available': True, 'snapshot': data}
        except Exception:
            pass
    cos_res = read(project_root)
    if not cos_res.get('available') and not sources_map:
        return cos_res
    try:
        data = assemble_derived_read_model(project_root, sources_map=sources_map)
        return {'available': True, 'snapshot': data}
    except Exception:
        return cos_res


class ReadModelWorker(threading.Thread):
    def __init__(self, project_root, interval_seconds=60, sources_map=None):
        super().__init__(name='ReadModelWorker', daemon=True)
        self.project_root = Path(project_root).resolve()
        self.interval = min(max(1, interval_seconds), 300)
        self.sources_map = sources_map
        self._stop_event = threading.Event()
        self._last_signatures = {}

    def stop(self):
        self._stop_event.set()

    def _signature(self, p):
        try:
            st = Path(p).stat()
            return (st.st_mtime, st.st_size)
        except OSError:
            return None

    def refresh_if_changed(self):
        current_sigs = {}
        cos_file = path(self.project_root)
        current_sigs['cos'] = self._signature(cos_file)
        from . import registry
        reg_file = registry.registry_path(self.project_root)
        pids_to_check = set()
        if self.sources_map:
            for k in self.sources_map:
                if not k.endswith('_repo'):
                    pids_to_check.add(k)
        if reg_file and Path(reg_file).exists():
            try:
                reg_data = json.loads(Path(reg_file).read_text(encoding='utf-8'))
                for p_entry in reg_data.get('projects', []):
                    if p_entry.get('id'):
                        pids_to_check.add(p_entry['id'])
            except Exception:
                pass
        if not pids_to_check:
            pids_to_check.add('routeport')
        for pid in sorted(pids_to_check):
            src = self.sources_map.get(pid) if self.sources_map else resolve_approved_source_path(self.project_root, pid)
            if src:
                current_sigs[pid] = self._signature(src)

        cache = derived_cache_path(self.project_root)
        aged = not cache.exists() or time.time() - cache.stat().st_mtime > WORK_MAX_AGE
        if current_sigs != self._last_signatures or aged:
            try:
                assemble_derived_read_model(self.project_root, self.sources_map)
                self._last_signatures = current_sigs
                return True
            except Exception:
                pass
        return False

    def run(self):
        self.refresh_if_changed()
        while not self._stop_event.wait(self.interval):
            self.refresh_if_changed()
