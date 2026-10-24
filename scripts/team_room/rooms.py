"""Named rooms with durable membership and lazy, independent discussion sessions."""
import hashlib
import json
import re
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
        self.outcome_stores = {}
        self.discussion_stores = {}
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
                    *[self._public(r) for r in self.rows]]

    @staticmethod
    def _public(row):
        data = {k:row[k] for k in ('id','name','bots')}
        if row.get('kind') == 'cross_project':
            data.update(kind='cross_project', version=row['version'], participants=[dict(p) for p in row['participants']],
                        lead=dict(next(p for p in row['participants'] if p['bot_key'] == row['lead']['bot_key']),
                                  basis=row['lead']['basis']))
        return data

    def _save(self, rows):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix('.tmp')
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.file)

    def create_cross(self, name, keys, lead, lead_override, client_id, index_fn):
        """Create a cross-project room: membership, hierarchy lead and eligibility only (no session runtime)."""
        from .cross_rooms import CrossRoomError, resolve_lead, resolve_participants
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Room name must be 1–80 characters')
        if not isinstance(client_id,str) or not 1 <= len(client_id) <= 128:
            raise ValueError('Valid client_message_id required')
        name = name.strip()
        signature = [name, sorted(keys) if isinstance(keys,list) and all(isinstance(k,str) for k in keys) else keys,
                     lead, lead_override is True]
        with self.lock:
            if self.closed or self.draining: raise RuntimeError('Rooms are closing')
            previous = next((r for r in self.rows if r['client_id'] == client_id), None)
            if previous:
                if previous.get('request') != signature:
                    raise CrossRoomError(409, 'idempotency_conflict', 'Creation ID already belongs to a different room')
                return self._public(previous)
            people = resolve_participants(keys, index_fn())
            chosen = resolve_lead(people, lead, lead_override)
            if any(r['name'].casefold() == name.casefold() for r in self.metadata()):
                raise ValueError('A room with this name already exists')
            row = {'id':uuid.uuid4().hex,'name':name,'bots':[],'client_id':client_id,'kind':'cross_project',
                   'version':1,'participants':people,'lead':chosen,'request':signature}
            self._save([*self.rows,row])
            self.rows.append(row)
            return self._public(row)

    def cross_outcomes(self, key):
        """Public room data and the outcome store of an existing cross-project room."""
        from .cross_rooms import CrossRoomError
        from .room_outcomes import Outcomes
        with self.lock:
            if self.closed or self.draining: raise RuntimeError('Rooms are closing')
            row = next((r for r in self.rows if r['id'] == key and r.get('kind') == 'cross_project'), None)
            if row is None: raise CrossRoomError(404, 'unknown_room', 'Unknown cross-project room')
            store = self.outcome_stores.get(key)
            if store is None:
                store = self.outcome_stores[key] = Outcomes(self.root / key)  # one lock per room for the whole process
            return self._public(row), store

    def cross_discussion(self, key):
        """Public room data and the discussion journal of an existing cross-project room."""
        from .cross_rooms import CrossRoomError
        from .room_discussion import Discussion
        with self.lock:
            if self.closed or self.draining: raise RuntimeError('Rooms are closing')
            row = next((r for r in self.rows if r['id'] == key and r.get('kind') == 'cross_project'), None)
            if row is None: raise CrossRoomError(404, 'unknown_room', 'Unknown cross-project room')
            store = self.discussion_stores.get(key)
            if store is None:
                store = self.discussion_stores[key] = Discussion(self.root / key)
            return self._public(row), store

    def set_participants(self, key, version, keys, lead, lead_override, index_fn):
        from .cross_rooms import CrossRoomError, resolve_lead, resolve_participants
        with self.lock:
            if self.closed or self.draining: raise RuntimeError('Rooms are closing')
            row = next((r for r in self.rows if r['id'] == key and r.get('kind') == 'cross_project'), None)
            if row is None: raise CrossRoomError(404, 'unknown_room', 'Unknown cross-project room')
            if not isinstance(version,int) or isinstance(version,bool):
                raise CrossRoomError(400, 'invalid_version', 'Room version required')
            if version != row['version']:
                raise CrossRoomError(409, 'version_conflict', 'Room changed; reload before editing', version=row['version'])
            people = resolve_participants(keys, index_fn())
            chosen = resolve_lead(people, lead, lead_override)
            updated = {**row, 'participants':people, 'lead':chosen, 'version':row['version'] + 1}
            self._save([updated if r is row else r for r in self.rows])
            self.rows[self.rows.index(row)] = updated
            return self._public(updated)

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
            if row.get('kind') == 'cross_project': raise ValueError('Cross-project rooms have no local session')
            if key not in self.controllers:
                self.controllers[key] = self.factory(self.root / key,self.codex_path,self.base.model,
                    bots=tuple(row['bots']),profile_project=self.base.profile_project)
                self.controllers[key].room_instructions = row.get('instructions', '')
            return self.controllers[key]

    def queue_state(self, key):
        """Read an existing room queue without opening or initializing a room runtime."""
        with self.lock:
            if self.closed or self.draining:
                raise RuntimeError('Rooms are closing')
            if key == 'development':
                from .room_queue import RoomQueue
                return RoomQueue(self.base, initialize=False).state()
            if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{32}', key):
                raise ValueError('Unknown room')
            if not any(row['id'] == key for row in self.rows):
                raise ValueError('Unknown room')
            root = self.root.resolve()
            room_root = (self.root / key).resolve()
            if not room_root.is_relative_to(root):
                raise ValueError('Room path escapes storage')
            controller = self.controllers.get(key)
            if controller is not None:
                if controller.store.root.resolve() != room_root:
                    raise ValueError('Room path escapes storage')
                from .room_queue import RoomQueue
                return RoomQueue(controller, initialize=False).state()
            database = room_root / 'room.sqlite'
            if not database.exists():
                return []
            database = database.resolve(strict=True)
            if not database.is_relative_to(room_root) or not database.is_file():
                raise ValueError('Room database escapes storage')
            from .room_queue import RoomQueue
            return RoomQueue.read_existing(database)

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
