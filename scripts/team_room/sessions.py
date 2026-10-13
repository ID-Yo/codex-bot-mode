"""Desktop session metadata only; private messages never enter the room journal."""
from pathlib import PureWindowsPath


def office_sessions(personal):
    if personal is None: return []
    return [{'id':'office-private-'+p['bot'],'kind':'office-private','bot':p['bot'],
        'backend':p.get('backend','office'),'thread_id':p.get('thread_id'),
        'title':p['bot'].title()+' · Personal','project':'Development','status':p.get('status','not_started'),
        'updated_at':None,'pinned':False,'unread':False} for p in personal.metadata()]


def list_sessions(bridge):
    result = bridge.call('list_threads', {'limit': 50})
    sessions, seen = [], set()
    for pinned, rows in ((True, result.get('pinnedThreads', [])), (False, result.get('threads', []))):
        for row in rows:
            if row.get('kind') != 'codex' or not isinstance(row.get('id'), str) or row['id'] in seen:
                continue
            cwd = str(row.get('cwd') or '')
            if '/.bot-mode/team-room-v1/' in cwd.replace('\\', '/').lower():
                continue
            seen.add(row['id'])
            sessions.append({'id': row['id'], 'title': str(row.get('title') or 'Untitled session'),
                'project': PureWindowsPath(cwd).name if cwd else 'No project',
                'status': str(row.get('status') or 'unknown'), 'updated_at': row.get('updatedAt'),
                'pinned': pinned, 'unread': bool(row.get('isUnread'))})
    return {'sessions': sessions, 'unavailable_hosts': bool(result.get('unavailableHosts'))}


def open_session(bridge, thread_id):
    if not isinstance(thread_id, str) or thread_id not in {s['id'] for s in list_sessions(bridge)['sessions']}:
        raise ValueError('Select a currently listed Codex session')
    return bridge.call('navigate_to_codex_page', {'threadId': thread_id})
