"""Read-only agent projection over registry, activity and explicit ledger metadata.

No transcript/STATE parsing, dispatch, cache writes, model calls or inferred start
times. See docs/design/agent-visibility.md for supported fields and coverage limits.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time

from . import desktop_relay, project_status, registry, requests, roster

RUNTIME_SECONDS = 60
TASK_SECONDS = 48 * 3600
MAX_BYTES = 256 * 1024
LEDGER_BYTES = 2 * 1024 * 1024
TERMINAL = ('done', 'completed', 'cancelled', 'failed')
OUTCOMES = ('done', 'blocked', 'failed', 'refused', 'partial')


def stamp(value):
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return d if d.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def freshness(at, now, seconds):
    d = stamp(at)
    if not d or not 0 <= (stamp(now) - d).total_seconds():
        return 'unknown'
    return 'fresh' if (stamp(now) - d).total_seconds() <= seconds else 'stale'


def url(value):
    try:
        project_status.safe_url(value, 'link')
        return value
    except (ValueError, TypeError):
        return None


def text(value, limit=600):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def read_json(file, root, limit=MAX_BYTES):
    if not registry._is_safe_contained(file, root) or file.stat().st_size > limit:
        raise ValueError('Source unavailable')
    return json.loads(file.read_text(encoding='utf-8'))


def task_records(root):
    """Explicit header fields only; no body prose, defaults or fuzzy assignments."""
    tasks = []
    files = sorted((root / 'work/tasks').glob('*.md'))
    if len(files) > 200:
        raise ValueError('Task coverage exceeds 200 files')
    for file in files:
        if not registry._is_safe_contained(file, root) or file.stat().st_size > MAX_BYTES:
            raise ValueError('Task source unavailable')
        meta = {}
        for line in file.read_text(encoding='utf-8').splitlines():
            if line.startswith('## '):
                break
            line = line.strip().lstrip('-* ').replace('**', '')
            if ':' in line:
                k, v = line.split(':', 1)
                meta[k.strip().lower()] = v.strip()
        if meta.get('assigned executor') or meta.get('assigned bot id'):
            tasks.append({**meta, '_source': file.relative_to(root).as_posix(), '_id': meta.get('task id') or file.stem})
    return tasks


def assigned(task, bot):
    value = task.get('assigned executor') or task.get('assigned bot id')
    if value == bot['bot_id']:
        return True
    # Existing ledgers also qualify bot identity with team and current chat.
    match = re.fullmatch(r'([^/]+)/([a-z][a-z0-9-]*) \(([0-9a-f-]{36})\)', value or '')
    return bool(match and match[1] == bot.get('team_name') and match[2] == bot['bot_id'] and match[3] == bot.get('thread_id'))


def workflow_state(task):
    state, stage = task.get('state') or task.get('status'), task.get('stage')
    if 'paused' in (state, stage):
        return 'paused'
    return next((s for s in (state, stage) if s in TERMINAL), stage or state or 'unknown')


def valid_request(item):
    """Enforce the existing writer's scalar contract before matching or display."""
    return (isinstance(item, dict)
            and isinstance(item.get('id'), str) and requests.ID.fullmatch(item['id'])
            and all(isinstance(item.get(k), str) and requests.BOT.fullmatch(item[k]) for k in ('from', 'to'))
            and isinstance(item.get('state'), str) and item['state'] in ('open', 'answered', 'cancelled')
            and ('outcome' not in item or (isinstance(item['outcome'], str) and item['outcome'] in OUTCOMES)))


def project_cards(root, bots, activity, now=None, project_pause=False):
    now = now or desktop_relay.now_utc()
    root = Path(root) if root is not None else None
    coverage = []
    try:
        tasks = task_records(root) if root is not None else []
    except (ValueError, OSError):
        tasks = []
        coverage.append('Task sources unavailable or incomplete')
    request_file = root / '.bot-mode/local/requests.json' if root is not None else None
    if request_file is not None and not request_file.exists():
        request_file = root / '.bot-mode/requests.json'
    try:
        raw = read_json(request_file, root, LEDGER_BYTES) if request_file is not None and request_file.exists() else {'schema': 1, 'requests': []}
        if raw.get('schema') != 1 or not isinstance(raw.get('requests'), list):
            raise ValueError('Invalid requests')
        reqs = [r for r in raw['requests'] if valid_request(r)]
        # Keep only scalar scope references for rejected entries; never their values.
        invalid = [{'id': r.get('id') if isinstance(r.get('id'), str) else None,
                    'bots': [r[k] for k in ('from', 'to') if isinstance(r.get(k), str)]}
                   if isinstance(r, dict) else {'id': None, 'bots': []}
                   for r in raw['requests'] if not valid_request(r)]
    except (OSError, ValueError, AttributeError):
        reqs = []
        invalid = []
        coverage.append('requests ledger unavailable')
    paused = project_pause or (root is not None and (root / '.bot-mode/PAUSED').exists())
    cards = []
    activity = activity if isinstance(activity, dict) else {}
    for bot in bots:
        bid = bot['bot_id']
        notes = list(coverage)
        matched = [t for t in tasks if assigned(t, bot)]
        unfinished = [t for t in matched if workflow_state(t) not in TERMINAL]
        selected = unfinished or matched
        t = selected[0] if len(selected) == 1 else None
        if len(selected) > 1:
            notes.append('Multiple assigned tasks; current task/action is unknown')
        elif not selected:
            notes.append('No exactly assigned task metadata')
        at = activity.get('fetched_at')
        fresh = freshness(at, now, RUNTIME_SECONDS)
        observations = [o for o in (activity.get('bots') or []) if isinstance(o, dict) and o.get('bot') == bid]
        observation = observations[0] if len(observations) == 1 else {}
        runtime_state = observation.get('status', 'unknown')
        # A partial-coverage error on the project describes other bots; this bot's own explicit active/idle
        # observation stays valid. Stale or foreign-project activity never does.
        runtime_failed = bool(activity.get('stale') or
                              (activity.get('project_id') is not None and activity['project_id'] != bot.get('project_id')) or
                              (activity.get('error') and not (activity.get('partial_failure') and runtime_state in ('active', 'idle'))))
        bound = bool(bot.get('thread_id'))
        observed = bound and fresh == 'fresh' and not runtime_failed
        # Fresh active -> working; fresh explicit non-running -> idle (not proof of no assigned task). Never inferred.
        execution = 'working' if observed and runtime_state == 'active' else 'idle' if observed and runtime_state == 'idle' else 'unknown'
        source = activity.get('source') or 'none'
        state_since = None  # active/idle metadata contains no execution start timestamp
        task = None
        action = blocker = last_result = None
        dependencies, transitions = [], []
        attention = False
        attention_reasons = []
        invalid_scope = any(bid in r['bots'] or not (r['id'] or r['bots']) for r in invalid)
        task_fresh = 'unknown'
        if t:
            updated = t.get('updated at')
            task_fresh = freshness(updated, now, TASK_SECONDS)
            recorded = workflow_state(t)
            workflow = recorded if recorded in ('development', 'implementation', 'in_progress', 'in-progress', 'review',
                         'qa', 'verification', 'integration', 'completed', 'done', 'ready', 'backlog', 'blocked', 'paused', 'failed', 'cancelled') else 'unknown'
            task = {'id': t['_id'], 'title': text(t.get('title')) or t['_id'],
                    'url': url(t.get('tracking') or t.get('authority')),
                    'workflow': workflow, 'recorded_state': text(recorded), 'source': t['_source'], 'updated_at': updated if stamp(updated) else None,
                    'state_since': t.get('state since') if stamp(t.get('state since')) and stamp(t['state since']) <= stamp(now) else None,
                    'freshness': task_fresh, 'recorded_action': text(t.get('current action'))}
            terminal = workflow in TERMINAL
            paused_task = workflow == 'paused'
            if task_fresh == 'fresh' and not (terminal or paused_task or paused):
                action = task['recorded_action']
                blocker = text(t.get('blocker') or t.get('blocker reason'))
            if text(t.get('last result')):
                last_result = {'text': text(t['last result']), 'at': t.get('result at') if stamp(t.get('result at')) else None,
                               'source': t['_source'], 'url': url(t.get('result url'))}
            related_ids = {t.get('local request id'), t.get('waiting request')}
            invalid_scope = invalid_scope or any(r['id'] is not None and r['id'] in related_ids for r in invalid)
            related = [r for r in reqs if r.get('id') in related_ids and bid in (r.get('from'), r.get('to'))]
            for r in related:
                for key, label in [('opened', 'Request opened (not execution start)'), ('acknowledged_at', 'Request acknowledged'),
                                   ('started_at', 'Execution start recorded'), ('answered', 'Request answered (not task completion)')]:
                    if stamp(r.get(key)) and stamp(r[key]) <= stamp(now):
                        transitions.append({'at': r[key], 'label': label, 'source': 'request:' + str(r['id']),
                                            'url': url(r.get('reply_link') if key == 'answered' else r.get('link'))})
                if r.get('state') == 'answered' and stamp(r.get('answered')) and stamp(r['answered']) <= stamp(now) and not last_result:
                    last_result = {'text': 'Request answered: ' + r.get('outcome', 'outcome unknown') + ' (not task completion)',
                                   'at': r['answered'], 'source': 'request:' + str(r['id']), 'url': url(r.get('reply_link'))}
            target = text(t.get('waiting on'), 80)
            if target and not (terminal or paused_task or paused):
                resolved = any(r.get('id') == t.get('waiting request') and r.get('state') in ('answered', 'cancelled', 'done') for r in related)
                pending = next((r for r in related if r.get('id') == t.get('waiting request') and r.get('state') == 'open'
                                and r.get('from') == bid and r.get('to') == target), None)
                dependencies.append({'target': target, 'request_id': pending['id'] if pending else None,
                                     'url': url(pending.get('link')) if pending else url(t.get('dependency url')),
                                     'bot_key': bot.get('project_id', '') + ':' + target,
                                     'source': t['_source'], 'source_at': task['updated_at'],
                                     'freshness': 'unknown' if resolved or invalid_scope else task_fresh})
                if resolved:
                    notes.append('Waiting request resolved; current waiting state unknown until task reconciled')
                if task_fresh == 'fresh' and not (resolved or invalid_scope):
                    attention = target == 'owner'
                    if attention:
                        attention_reasons.append('owner_decision')
                    if execution in ('unknown', 'idle'):
                        execution, source = 'waiting', t['_source']
                        state_since = t.get('waiting since') if stamp(t.get('waiting since')) and stamp(t['waiting since']) <= stamp(now) else None
            if task_fresh == 'fresh' and (workflow in ('failed', 'blocked') or blocker):
                attention = True
                attention_reasons.append('failure' if workflow == 'failed' else 'blocker')
                if execution in ('unknown', 'idle') and workflow == 'failed':
                    execution, source, state_since = 'failed', t['_source'], task['state_since']
        else:
            paused_task = False
        if paused or paused_task:
            execution, source = 'paused', 'owner/project pause' if paused else t['_source']
            state_since = task['state_since'] if paused_task else None
        if not bound:
            notes.append('Missing chat binding; runtime unknown')
        if not bound or fresh != 'fresh' or runtime_failed or runtime_state not in ('active', 'idle'):
            notes.append('Runtime ' + str(runtime_state) + '; not proof of available, blocked or task completion')
        elif runtime_state == 'idle':
            notes.append('Idle: the chat is not running; this is not proof that no task is assigned')
        if activity.get('partial_failure'):
            notes.append('Partial runtime coverage; only this bot observation is used')
        if runtime_failed:
            notes.append('Runtime refresh failed or stale; retained activity is historical')
        if invalid_scope:
            notes.append('Invalid request entries skipped; related request evidence unknown')
        notes.append('Action/start time/transitions unsupported unless explicitly recorded; no transcript access')
        execution_at = task['updated_at'] if task and source == task['source'] else at if source == activity.get('source') else None
        cards.append({**bot, 'execution': {'state': execution, 'state_since': state_since, 'source': source,
                     'observed_at': now, 'source_at': execution_at,
                     'freshness': task_fresh if task and source == task['source'] else fresh if execution_at else 'unknown',
                     'certainty': 'confirmed' if execution != 'unknown' else 'unknown'},
                      'runtime': {'status': runtime_state, 'source': activity.get('source') or 'none', 'source_at': at, 'freshness': 'unknown' if runtime_failed else fresh},
                      'task': task, 'action': action, 'last_result': last_result,
                      'blocker': blocker, 'dependencies': dependencies,
                      'transitions': sorted(transitions, key=lambda x: stamp(x['at']), reverse=True)[:12],
                      'attention': attention, 'attention_reasons': attention_reasons,
                      'uncertain': execution == 'unknown' or not bound or runtime_failed or fresh != 'fresh' or bool(coverage) or invalid_scope or (task is not None and task_fresh != 'fresh'),
                      'coverage': '; '.join(notes)})
    return cards


def pause_map(root):
    """Read existing readmodel cache and owner snapshot, never trigger assembly."""
    pauses = {}
    for file in (project_status.derived_cache_path(root), project_status.path(root)):
        if not file.exists():
            continue
        try:
            raw = read_json(file, root)
            snap = project_status.validate(raw.get('snapshot', raw))
            for p in snap['projects']:
                if p['status'] != 'unknown' and p['source']['available']:
                    # The explicit owner snapshot is read last and can clear
                    # an old derived pause; this never clears a PAUSED marker.
                    pauses[p['id']] = p['status'] == 'paused'
        except (ValueError, OSError, AttributeError):
            pass
    return pauses


def build(project_root, bridge=None, now=None):
    root = Path(project_root)
    fixed_now = now
    config = registry.registry_path(root)
    overview = registry.build_overview(config, current_project_root=root)
    roots = {p['id']: Path(p['root']) for p in registry.load_registry(config).get('projects', [])}
    if overview['status'] == 'not_configured':
        # Same served roster, not a guessed second directory or chat identity.
        overview['bots'] = [{'bot_key': 'current:' + p['bot'], 'bot_id': p['bot'],
                             'name': p.get('role') or p['bot'], 'role': p.get('role'), 'team_name': p.get('team'),
                             'project_id': 'current', 'project_name': root.name, 'thread_id': p.get('thread_id'),
                             'link': roster.link(p.get('thread_id')), 'is_current': True} for p in roster.active(roster.load(root))]
        roots = {'current': root}
    pauses = pause_map(root)
    agents = []
    for pid in dict.fromkeys(b.get('project_id') for b in overview.get('bots', [])):
        bots = [b for b in overview['bots'] if b.get('project_id') == pid]
        activity = {}
        if bridge and pid in roots and config:
            try:
                activity = bridge.activity(pid) if hasattr(bridge, 'activity') else desktop_relay.get_project_activity(bridge, pid, current_project_root=root)
            except Exception:
                activity = {}
        # Runtime timestamps are compared with the observation after that
        # project's serialized fetch, not a build-start timestamp.
        observed_at = fixed_now if fixed_now is not None else desktop_relay.now_utc()
        # Standalone bots have no registry project task scope/runtime producer.
        agents.extend(project_cards(roots.get(pid), bots, activity, observed_at, pauses.get(pid, False)))
    snapshot_at = fixed_now if fixed_now is not None else desktop_relay.now_utc()
    return {'schema': 1, 'observed_at': snapshot_at, 'status': overview['status'], 'agents': agents,
            'source_warnings': [t['project_name'] + ': ' + t['status'] for t in overview.get('teams', [])
                                if t['status'] in ('source_unavailable', 'roster_error')],
            'coverage': 'Registry identities, explicit task headers and request lifecycle only. Runtime metadata may be partial; standalone runtime unsupported.'}


_BUILDS = {}
_BUILDS_LOCK = threading.Lock()
BUILD_WAIT = 1.0
BUILD_STALE_SECONDS = 60.0


def _as_stale(value):
    """Retained records must not claim a live state: execution becomes Unknown (paused stays, it is owner-set)
    and runtime and execution freshness read stale. Task and identity records are kept as they were."""
    out = json.loads(json.dumps(value))
    for card in out.get('agents', []):
        execution = card.get('execution')
        if isinstance(execution, dict):
            if execution.get('state') != 'paused':
                execution.update(state='unknown', state_since=None, certainty='unknown')
            execution['freshness'] = 'stale'
        if isinstance(card.get('runtime'), dict):
            card['runtime']['freshness'] = 'stale'
        card['uncertain'] = True
    return out


def build_nonblocking(project_root, bridge=None, wait=BUILD_WAIT, stale_after=BUILD_STALE_SECONDS):
    """build() for HTTP polling (#329). Per-project Desktop reads can take tens of seconds, so a request never
    waits for them: one background build per project runs at a time and a request waits at most wait seconds.

    The answer says what it is in 'enrichment': 'fresh' (a finished build younger than stale_after), 'stale' (the
    last finished build while a refresh runs), 'pending' (no build finished yet: roster identities only, no Desktop
    evidence, so execution stays Unknown) or 'failed' (the last build raised; the reason is in source_warnings)."""
    key = str(project_root)
    now = time.monotonic()
    with _BUILDS_LOCK:
        entry = _BUILDS.setdefault(key, {'value': None, 'at': 0.0, 'worker': None, 'error': None, 'bridge': id(bridge)})
        if entry['bridge'] != id(bridge):
            # A replacement bridge is a different Desktop connection: nothing observed through the old one is reused.
            entry.update(value=None, at=0.0, error=None, bridge=id(bridge))
        worker = entry['worker']
        if (worker is None or not worker.is_alive()) and (entry['value'] is None or now - entry['at'] >= stale_after / 2
                                                          or entry['error']):
            def run():
                try:
                    value, error = build(project_root, bridge), None
                except Exception as exc:
                    value, error = None, type(exc).__name__  # never echo exception text to the UI
                with _BUILDS_LOCK:
                    if value is not None:
                        entry['value'], entry['at'] = value, time.monotonic()
                    entry['error'] = error
            worker = threading.Thread(target=run, daemon=True, name='agent-visibility')
            entry['worker'] = worker
            worker.start()
    worker.join(wait if entry['value'] is None else 0)
    with _BUILDS_LOCK:
        value, at, error = entry['value'], entry['at'], entry['error']
    if value is not None:
        age = time.monotonic() - at
        if age < stale_after and not error:
            return dict(value, enrichment='fresh')
        out = dict(_as_stale(value), enrichment='stale')
        if error:
            out['source_warnings'] = list(value.get('source_warnings', [])) + ['Agent evidence refresh failed (' + error + '); showing the last known records.']
        return out
    # Nothing finished yet: valid roster cards without Desktop evidence, labelled as such.
    try:
        out = dict(build(project_root, None), enrichment='failed' if error else 'pending')
    except Exception as exc:
        return {'status': 'unknown', 'agents': [], 'enrichment': 'failed',
                'source_warnings': ['Agent evidence unavailable (' + type(exc).__name__ + ').']}
    note = ('Agent evidence refresh failed (' + error + ').') if error else 'Desktop evidence is still loading; execution is Unknown until it arrives.'
    out['source_warnings'] = list(out.get('source_warnings', [])) + [note]
    return out
