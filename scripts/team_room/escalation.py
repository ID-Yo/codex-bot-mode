"""Office side of the request ledger: send overdue reminders and escalations.

A message that may have reached Desktop is never resent; the outcome is recorded
as uncertain. The owner step is shown in Office instead of being messaged.
"""
from . import project as served, requests as rq


def escalation_target(item, step):
    if step == 'reminder':
        return item['to']
    if step == 'owner':
        return 'owner'
    people = served.participants()
    recipient = next((p for p in people if p['bot'] == item['to']), None)
    team = recipient and recipient.get('team')
    manager = next((p['bot'] for p in people if p['role'] == 'Manager' and p.get('team') == team), None)
    director = next((p['bot'] for p in people if p['role'] == 'Project Director'), None)
    for candidate in (manager, director):
        if candidate and candidate not in (item['from'], item['to']):
            return candidate
    return 'owner'


def message(item, step, root):
    command = f'python -B {root}/.bot-mode/requests.py --project {root} reply {item["id"]} --by {item["to"]} --link <reply link>'
    link = f' ({item["link"]})' if item.get('link') else ''
    if step == 'reminder':
        return (f'Reminder {item["id"]}: {item["from"]} is still waiting for your reply to: {item["summary"]}{link}. '
                f'Send the reply to {item["from"]}\'s chat (an answer only in your own chat does not reach them), '
                f'then close it with: {command}')
    return (f'Request {item["id"]} from {item["from"]} to {item["to"]} is overdue: {item["summary"]}{link}. '
            f'Please follow up with {item["to"]}.')


def escalate(bridge, at=None):
    root = served.PROJECT
    data = rq.load(root)
    expected = rq.version(data)
    steps = rq.due_steps(data, at)
    if not steps:
        return []
    threads = served.threads()
    done = []
    for item, step in steps:
        target = escalation_target(item, step)
        if target == 'owner':
            outcome = 'flagged'
        elif not threads.get(target) or bridge is None:
            outcome = 'no-chat'
        else:
            try:
                bridge.call('send_message_to_thread', {'threadId': threads[target], 'hostId': 'local',
                                                       'prompt': message(item, step, root.as_posix())})
                outcome = 'sent'
            except Exception:
                outcome = 'uncertain'
        rq.record_escalation(data, item['id'], step, target, outcome, at)
        done.append((item['id'], step, target, outcome))
    rq.save(root, data, expected)
    return done
