"""Launch a team from Codex Office through the project's lead Manager chat.

Office shows the catalog questions and a dry-run plan from the shared launcher.
After the owner confirms, Office sends the confirmed request to the lead Manager's
Codex chat, which runs the codex-team-launch skill (install, chats, sidebar).
"""
import json

from . import inflight, project as served, requests as rq, teams


def catalog():
    source, _ = teams.fetch(None)
    out = []
    for item in teams.index(source).get('teams') or []:
        _, spec = teams.team_template(source, item['name'])
        out.append({'name': item['name'], 'description': item.get('description'), 'roles': spec['roles'],
                    'team': teams.questions(spec.get('parameters')),
                    'agents': {role: teams.questions(teams.agent_template(source, role)[1].get('parameters'))
                               for role in spec['roles']}})
    return out


def _clean(answers):
    if answers is None:
        return {}
    if not isinstance(answers, dict) or any(not isinstance(v, dict) for v in answers.values()):
        raise ValueError('Answers must map each role to its values')
    return {role: {k: v for k, v in values.items() if isinstance(v, str) and v.strip()}
            for role, values in answers.items()}


def plan(template, team_name=None, answers=None, team_answers=None):
    if team_answers is not None and not isinstance(team_answers, dict):
        raise ValueError('Team answers must be an object')
    team = {'team_name': team_name} if isinstance(team_name, str) and team_name.strip() else {}
    team.update(team_answers or {})
    return teams.summary(teams.plan_team(served.PROJECT, template, _clean(answers), None, team))


def launch(bridge, template, team_name, answers, client_id, team_answers=None):
    if not isinstance(client_id, str) or not 8 <= len(client_id) <= 64:
        raise ValueError('Valid client ID required')
    key = 'OFFICE-' + ''.join(ch for ch in client_id if ch.isalnum())[:24]
    ledger = rq.load(served.PROJECT)
    previous = next((r for r in ledger['requests'] if r['id'] == key), None)
    if previous:
        return {'request': key, 'state': previous['state'], 'repeated': True}
    summary = plan(template, team_name, answers, team_answers)
    teams.require_tracking(summary['work_tracking'])
    lead = served.participants()[0] if served.participants() else None
    if bridge is None or not lead or not lead.get('thread_id'):
        raise ValueError('The lead Manager chat is not connected; launch the team from Codex instead')
    payload = {'template': template, 'team': {**(team_answers or {}), 'team_name': summary['team'], 'tracking_mode': summary['work_tracking']['mode'], 'tracking_authority': summary['work_tracking']['authority']}, 'agents': _clean(answers)}
    text = ('The owner reviewed and confirmed this team launch in Codex Office (request ' + key + '). '
            'Use the codex-team-launch skill: write the answers below to a file, run install-team '
            f'{template} --approval once --confirm with them, create and appoint every chat, place them in '
            f'the sidebar section "{summary["section"]}", record each chat in the roster, then reply to the owner '
            f'and close the request with requests.py reply {key}.\n\nAnswers:\n'
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + '\n\nPlan:\n' + json.dumps({k: summary[k] for k in ('team', 'section', 'room', 'director_proposal')}, ensure_ascii=False)
            + '\nAgents: ' + ', '.join(f'{a["display_name"]} ({a["role"]})' for a in summary['agents']))
    expected = rq.version(ledger)
    with inflight.track('launch', key) as send:  # intent row before the ledger write; unresolved until the request closes
        rq.open_request(ledger, key, 'owner', lead['bot'], f'Launch team {summary["team"]} from {template}', None, 60)
        rq.save(served.PROJECT, ledger, expected)
        try:
            send.attempting()
            bridge.call('send_message_to_thread', {'threadId': lead['thread_id'], 'hostId': 'local', 'prompt': text})
            state = 'sent'
            send.finish('sent')
        except Exception:
            state = 'uncertain'
            send.finish('uncertain')
    return {'request': key, 'state': state, 'manager': lead['bot'], 'plan': summary}
