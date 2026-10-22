"""Track installed component revisions separately from evidenced agent loading.

Pure state transitions: callers persist through their transaction/lock. This
module neither wakes agents nor changes files, holds, models or chat bindings.
"""
from datetime import datetime, timezone
import hashlib
import json
import re


class ActivationError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _revision(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ActivationError('An exact installed content digest is required')
    return value


def pending(component, revision, consumers):
    """Called only after an accepted transaction actually committed its files."""
    _revision(revision)
    if not isinstance(component, str) or not component or not isinstance(consumers, list) or not consumers:
        raise ActivationError('Component identity and explicit consumers are required')
    identities = set()
    result = []
    for consumer in consumers:
        if not isinstance(consumer, dict) or set(consumer) != {'bot', 'thread_id'}:
            raise ActivationError('Consumer must name its current bot and thread')
        if not all(isinstance(v, str) and v for v in consumer.values()):
            raise ActivationError('Missing consumer binding')
        identity = (consumer['bot'], consumer['thread_id'])
        if identity in identities:
            raise ActivationError('Duplicate consumer binding')
        identities.add(identity)
        result.append({**consumer, 'state': 'pending_activation', 'revision': revision,
                       'receipt': None})
    return {'schema': 1, 'component': component, 'installed_revision': revision,
            'consumers': result, 'state': 'pending_activation'}


def eligibility(activity, expected_bot, expected_thread, now=None, max_age_seconds=120):
    """Require fresh explicit idle evidence; unknown/stale/paused never grants permission."""
    current = now or datetime.now(timezone.utc)
    if (not isinstance(activity, dict) or not isinstance(expected_bot, str) or not expected_bot
            or not isinstance(expected_thread, str) or not expected_thread):
        return {'eligible': False, 'reason': 'activity_unknown'}
    required = {'bot', 'thread_id', 'as_of', 'state', 'paused', 'pending_writes', 'ownership'}
    if not required.issubset(activity):
        return {'eligible': False, 'reason': 'activity_unknown'}
    if activity['bot'] != expected_bot or activity['thread_id'] != expected_thread:
        return {'eligible': False, 'reason': 'binding_changed'}
    try:
        stamp = datetime.fromisoformat(activity['as_of'].replace('Z', '+00:00'))
        age = (current - stamp).total_seconds()
        if stamp.tzinfo is None or not 0 <= age <= max_age_seconds:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        return {'eligible': False, 'reason': 'activity_stale_or_unknown'}
    if activity['paused'] is not False:
        return {'eligible': False, 'reason': 'paused_or_unknown'}
    if activity['pending_writes'] is not False or activity['state'] != 'idle':
        return {'eligible': False, 'reason': 'busy_or_unknown'}
    if activity['ownership'] not in ('native_desktop', 'office'):
        return {'eligible': False, 'reason': 'ownership_unknown'}
    return {'eligible': True, 'reason': 'next_authorized_turn_only',
            'dispatch_authorized': False, 'route': activity['ownership']}


def acknowledge(record, receipt, installed_revision, current_bindings, activity, now=None):
    """Accept host-authenticated readback, never a free-form agent assertion.

    Receipt comes from the trusted delivery adapter after a normal authorized
    turn. It names the adapter's observed loaded digest and completed turn ID.
    This pure function cannot authenticate an arbitrary caller; the API boundary
    must supply verified provenance and installed files freshly hashed by host.
    """
    _revision(installed_revision)
    if (not isinstance(record, dict) or record.get('schema') != 1
            or not isinstance(record.get('component'), str) or not record['component']
            or record.get('installed_revision') != installed_revision):
        raise ActivationError('Installed revision changed; new activation required')
    fields = {'bot', 'thread_id', 'turn_id', 'component', 'revision', 'completed_at', 'host_verified'}
    if not isinstance(receipt, dict) or set(receipt) != fields or receipt['host_verified'] is not True:
        raise ActivationError('Host-verified completed-turn readback required')
    if (not all(isinstance(receipt[k], str) and receipt[k] for k in fields - {'host_verified'})
            or not isinstance(current_bindings, dict)
            or not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in current_bindings.items())):
        raise ActivationError('Invalid receipt or current bindings')
    if (receipt['component'] != record['component'] or receipt['revision'] != installed_revision
            or not isinstance(receipt['turn_id'], str) or not receipt['turn_id']):
        raise ActivationError('Receipt does not identify this exact revision and turn')
    bot, thread = receipt['bot'], receipt['thread_id']
    if current_bindings.get(bot) != thread:
        raise ActivationError('Receipt belongs to a superseded binding')
    updated = reconcile(record, current_bindings)
    consumer = next((c for c in updated['consumers'] if c['bot'] == bot and c['thread_id'] == thread), None)
    if consumer is None:
        raise ActivationError('Receipt is not an affected consumer')
    receipt_id = _digest(receipt)
    if consumer['receipt'] == receipt_id and consumer['state'] == 'verified_active':
        return updated
    safe = eligibility(activity, bot, thread, now)
    if not safe['eligible']:
        raise ActivationError(safe['reason'])
    try:
        completed = datetime.fromisoformat(receipt['completed_at'].replace('Z', '+00:00'))
        observed = datetime.fromisoformat(activity['as_of'].replace('Z', '+00:00'))
        if completed.tzinfo is None or completed > observed or (observed - completed).total_seconds() > 120:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise ActivationError('Stale or invalid completion evidence') from error
    if consumer['receipt'] is not None and consumer['receipt'] != receipt_id:
        raise ActivationError('Consumer already has different activation evidence')
    consumer.update(state='verified_active', receipt=receipt_id)
    updated['state'] = 'verified_active' if all(c['state'] == 'verified_active' for c in updated['consumers']) else 'partial_activation'
    return updated


def reconcile(record, current_bindings):
    """Invalidate stale consumer evidence on every binding refresh, even without a receipt."""
    if (not isinstance(record, dict) or record.get('schema') != 1
            or not isinstance(record.get('component'), str) or not record['component']
            or not isinstance(record.get('consumers'), list) or not record['consumers']
            or not isinstance(current_bindings, dict)):
        raise ActivationError('Invalid activation record or current bindings')
    _revision(record.get('installed_revision'))
    updated = json.loads(json.dumps(record))
    seen = set()
    for consumer in updated['consumers']:
        if (not isinstance(consumer, dict) or not isinstance(consumer.get('bot'), str)
                or not consumer['bot'] or consumer['bot'] in seen
                or not isinstance(consumer.get('thread_id'), str) or not consumer['thread_id']
                or not isinstance(consumer.get('state'), str)
                or consumer.get('state') not in {'pending_activation', 'verified_active'}
                or consumer.get('revision') != record['installed_revision']
                or (consumer.get('state') == 'verified_active' and
                    (not isinstance(consumer.get('receipt'), str) or not re.fullmatch(r'[a-f0-9]{64}', consumer['receipt'])))):
            raise ActivationError('Invalid consumer activation evidence')
        seen.add(consumer['bot'])
        current = current_bindings.get(consumer['bot'])
        if current != consumer['thread_id']:
            if isinstance(current, str) and current:
                consumer['thread_id'] = current
            consumer.update(state='pending_activation', receipt=None)
    verified = sum(c['state'] == 'verified_active' for c in updated['consumers'])
    updated['state'] = ('verified_active' if verified == len(updated['consumers']) else
                        'partial_activation' if verified else 'pending_activation')
    return updated
