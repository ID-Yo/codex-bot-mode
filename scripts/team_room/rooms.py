"""Named rooms with durable membership and lazy, independent discussion sessions."""
import hashlib
import json
import threading
import uuid

from .controller import Controller
from .store import BOTS


class Rooms:
    def __init__(self, base, codex_path=None, controller_factory=Controller):
        self.base, self.codex_path, self.factory = base, codex_path, controller_factory
        self.root = base.store.root / 'rooms'
        self.file = self.root / 'rooms.json'
        self.rows = json.loads(self.file.read_text(encoding='utf-8')) if self.file.exists() else []
        self.controllers = {}
        self.lock = threading.RLock()
        self.closed = False
        self.draining = False
        self.default_file = self.root / 'development-settings.json'
        from . import project as served
        team_rooms = served.teams()
        first = team_rooms[0]['name'] if team_rooms else 'Development'
        self.default = json.loads(self.default_file.read_text(encoding='utf-8')) if self.default_file.exists() else {'name':first,'instructions':''}
        self.base.room_instructions = self.default.get('instructions', '')
        self._sync_roster_rooms(team_rooms[1:])

    def _sync_roster_rooms(self, team_rooms):
        """One room per additional roster team (and Leadership); membership follows the roster."""
        changed = False
        for room in team_rooms:
            key = hashlib.md5(('roster-room:' + room['name']).encode('utf-8')).hexdigest()
            row = next((r for r in self.rows if r['id'] == key), None)
            if row is None:
                self.rows.append({'id':key,'name':room['name'],'bots':room['bots'],'client_id':'roster:'+key,'roster':True})
                changed = True
            elif row['bots'] != room['bots']:
                row['bots'] = room['bots']
                changed = True
        if changed:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.file.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.rows, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.file)

    def metadata(self):
        with self.lock:
            return [{'id':'development','name':self.default['name'],'bots':list(self.base.bots)},
                    *[{k:r[k] for k in ('id','name','bots')} for r in self.rows]]

    def create(self, name, bots, client_id):
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Room name must be 1–80 characters')
        if (not isinstance(bots,list) or any(not isinstance(bot,str) for bot in bots)
                or BOTS[0] not in bots or len(set(bots)) != len(bots)
                or any(bot not in BOTS for bot in bots)):
            raise ValueError('Select the lead Manager and the bots participating in this room')
        if not isinstance(client_id,str) or not 1 <= len(client_id) <= 128:
            raise ValueError('Valid client_message_id required')
        name = name.strip()
        bots = [bot for bot in BOTS if bot in bots]
        with self.lock:
            if self.closed or self.draining: raise RuntimeError('Rooms are closing')
            previous = next((r for r in self.rows if r['client_id'] == client_id), None)
            if previous:
                if previous['name'] != name or previous['bots'] != bots:
                    raise ValueError('Creation ID already belongs to a different room')
                return {k:previous[k] for k in ('id','name','bots')}
            if any(r['name'].casefold() == name.casefold() for r in self.metadata()):
                raise ValueError('A room with this name already exists')
            row = {'id':uuid.uuid4().hex,'name':name,'bots':bots,'client_id':client_id}
            self.root.mkdir(parents=True,exist_ok=True)
            temporary = self.file.with_suffix('.tmp')
            temporary.write_text(json.dumps([*self.rows,row],ensure_ascii=False,indent=2),encoding='utf-8')
            temporary.replace(self.file)
            self.rows.append(row)
            return {k:row[k] for k in ('id','name','bots')}

    def get(self, key):
        with self.lock:
            if self.closed or self.draining: raise RuntimeError('Rooms are closing')
            if key == 'development': return self.base
            row = next((r for r in self.rows if r['id'] == key),None)
            if row is None: raise ValueError('Unknown room')
            if key not in self.controllers:
                self.controllers[key] = self.factory(self.root / key,self.codex_path,self.base.model,
                    bots=tuple(row['bots']),profile_project=self.base.profile_project)
                self.controllers[key].room_instructions = row.get('instructions', '')
            return self.controllers[key]

    def settings(self, key):
        from .settings import version
        with self.lock:
            row = self.default if key == 'development' else next((r for r in self.rows if r['id'] == key), None)
            if row is None: raise ValueError('Unknown room')
            data = {'id':key, 'name':row['name'], 'instructions':row.get('instructions',''),
                    'bots':list(self.base.bots) if key == 'development' else row['bots']}
            return {**data, 'version':version(json.dumps(data, sort_keys=True))}

    def update(self, key, name, instructions, expected):
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80: raise ValueError('Room name must be 1–80 characters')
        if not isinstance(instructions,str) or len(instructions.encode('utf-8')) > 8000: raise ValueError('Room instructions must be at most 8000 bytes')
        with self.lock:
            if self.closed or self.draining: raise RuntimeError('Rooms are closing')
            if self.settings(key)['version'] != expected: raise ValueError('Room changed since opening. Reload before saving.')
            if any(r['id'] != key and r['name'].casefold() == name.strip().casefold() for r in self.metadata()): raise ValueError('A room with this name already exists')
            updated = {'name':name.strip(), 'instructions':instructions}
            if key == 'development':
                path, data = self.default_file, updated
            else:
                path, data = self.file, [{**r, **updated} if r['id'] == key else r for r in self.rows]
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
            if key == 'development': self.default = updated; self.base.room_instructions = instructions
            else:
                self.rows = data
                if key in self.controllers: self.controllers[key].room_instructions = instructions
            return self.settings(key)

    def close(self):
        with self.lock:
            self.closed = True
            controllers = list(self.controllers.values())
        errors = []
        for controller in controllers:
            try: controller.close()
            except Exception as error: errors.append(error)
        if errors: raise RuntimeError('Room cleanup incomplete') from errors[0]
