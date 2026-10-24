"""Private synthetic browser fixtures generated through the actual projection."""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.team_room.agent_visibility import project_cards

NOW = '2026-10-20T10:00:00Z'


def correction_cards():
    cards=[]
    with tempfile.TemporaryDirectory() as temp:
        for i, bid in enumerate(['stage-completed','state-paused','stage-paused','owner-paused','active-failed','resolved','cancelled','aging-wait','canary','malformed']):
            root=Path(temp)/bid
            (root/'work/tasks').mkdir(parents=True)
            (root/'.bot-mode/local').mkdir(parents=True)
            bot=dict(bot_key='alpha:'+bid,bot_id=bid,name='Fixture '+bid,role='Developer',team_name='Development',
                     project_id='alpha',project_name='Fixture Alpha',thread_id=f'00000000-0000-0000-0000-{i+11:012}',link=None,is_current=False)
            state='paused' if bid=='state-paused' else 'failed' if bid=='active-failed' else 'in_progress'
            extra='Current action: HISTORICAL_ACTION\n'
            if bid.startswith('stage-'):extra+='Stage: '+bid.removeprefix('stage-')+'\n'
            if bid=='owner-paused':(root/'.bot-mode/PAUSED').touch()
            if bid in ('resolved','cancelled','aging-wait','canary','malformed'):
                extra+='Waiting on: owner\nWaiting request: R\nLocal request id: R\n'
                req=dict(id='R',state='open' if bid in ('aging-wait','malformed') else 'cancelled' if bid=='cancelled' else 'answered',
                         **{'from':bid,'to':'owner'},opened=NOW,answered=NOW,outcome='done')
                if bid=='canary':req['outcome']={'private_body':'OUTCOME_CANARY_NEVER_PROJECT'}
                if bid=='malformed':req['id']=['MALFORMED_CANARY']
                (root/'.bot-mode/local/requests.json').write_text(json.dumps(dict(schema=1,requests=[req])),encoding='utf-8')
            (root/'work/tasks/T.md').write_text(f'Task ID: {bid}\nAssigned executor: {bid}\nState: {state}\nUpdated at: {NOW}\n'+extra,encoding='utf-8')
            activity=dict(fetched_at=NOW,source='desktop_relay',bots=[dict(bot=bid,status='active' if bid in ('stage-completed','active-failed') else 'idle')])
            cards.extend(project_cards(root,[bot],activity,now=NOW))
    return cards


def fixture():
    cards = []
    with tempfile.TemporaryDirectory() as temp:
        for i, bid in enumerate(['working', 'wait-owner', 'wait-reviewer', 'paused', 'failed', 'unfinished', 'stale', 'unbound', 'reviewer']):
            root = Path(temp) / bid
            (root / 'work/tasks').mkdir(parents=True)
            (root / '.bot-mode/local').mkdir(parents=True)
            project = 'beta' if bid == 'unbound' else 'alpha'
            name = 'Fixture ' + bid.title()
            if bid == 'working': name = 'Fixture Developer ' + 'LongName' * 14
            bot = dict(bot_key=project + ':' + bid, bot_id=bid, name=name,
                       role='Developer' if bid == 'working' else 'Verifier',
                       team_name='QA' if bid in ('unbound', 'reviewer') else 'Development',
                       project_id=project, project_name='Fixture ' + project.title(),
                       thread_id=None if bid == 'unbound' else f'00000000-0000-0000-0000-{i+1:012}',
                       link=None if bid == 'unbound' else f'codex://threads/00000000-0000-0000-0000-{i+1:012}', is_current=False)
            state = 'failed' if bid == 'failed' else 'in_progress'
            extra = ''
            if bid == 'working': extra = 'Current action: Inspect deterministic evidence <script>private()</script>\nLast result: Candidate ready ' + 'long-result-' * 50 + '\n'
            if bid.startswith('wait-'):
                target = 'owner' if bid == 'wait-owner' else 'reviewer'
                extra = f'Waiting on: {target}\nWaiting since: 2026-10-20T09:52:00Z\nWaiting request: R-1\n'
                req = dict(schema=1, requests=[dict(id='R-1', state='open', **{'from':bid, 'to':target},
                                                   summary='PRIVATE SUMMARY NEVER SHARED', opened='2026-10-20T09:50:00Z',
                                                   link='https://github.com/example/project/pull/7')])
                (root / '.bot-mode/local/requests.json').write_text(json.dumps(req), encoding='utf-8')
            if bid == 'failed': extra = 'Last result: Fixture check failed\n'
            if bid == 'paused': (root / '.bot-mode/PAUSED').touch()
            (root / 'work/tasks/T.md').write_text(f'Task ID: T-{bid}\nTitle: Fixture {bid} task\nAssigned executor: {bid}\n'
                                                f'State: {state}\nUpdated at: {NOW}\nState since: 2026-10-20T09:45:00Z\n'
                                                'Tracking: https://github.com/example/project/issues/7\n'+extra, encoding='utf-8')
            status = 'active' if bid in ('working', 'paused', 'stale') else 'completed' if bid == 'unfinished' else 'idle'
            act = dict(fetched_at='2026-10-20T09:00:00Z' if bid == 'stale' else NOW, source='desktop_relay', bots=[dict(bot=bid, status=status)])
            cards.extend(project_cards(root, [bot], act, now=NOW))
    return dict(schema=1, observed_at=NOW, status='ok', agents=cards, correction_agents=correction_cards(),
                coverage='Synthetic fixtures only. No real agent activity or private transcripts.', source_warnings=[])


if __name__ == '__main__':
    Path(sys.argv[1]).write_text(json.dumps(fixture(), ensure_ascii=False, indent=2), encoding='utf-8')
