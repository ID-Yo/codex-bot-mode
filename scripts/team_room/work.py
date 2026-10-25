"""Host-mediated documentation tasks through the existing Desktop MCP client."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import subprocess
import threading
import time
import uuid

from . import roster
from .transport import safe_error


def _appointed(project=Path(__file__).resolve().parents[2]):
    """Current Manager and worker chats from the project roster.

    Without a roster, placeholder IDs keep the work host inert; real chats
    always come from the roster.
    """
    own = Path(project).resolve() == Path(__file__).resolve().parents[2]
    manager = '00000000-0000-7000-8000-000000000001' if own else None
    workers = ({'developer':'00000000-0000-7000-8000-000000000002',
                'verifier':'00000000-0000-7000-8000-000000000003'} if own else {})
    try:
        active = roster.active(roster.load(project))
    except (OSError, ValueError):
        return manager, workers
    current = {p['bot']: p.get('thread_id') for p in active}
    lead = next((p for p in active if p['role'] == 'Manager'), None)
    if lead and lead.get('thread_id'):
        manager = lead['thread_id']
    for bot in ('developer', 'verifier'):
        if current.get(bot):
            workers[bot] = current[bot]
    return manager, workers


MANAGER, WORKERS = _appointed()
REPOSITORY = None  # taken from the served project's git remote
DONE = ('completed', 'failed', 'review_required')
MAX_SECONDS = 900
MAX_BYTES = 65536


class WorkConflict(ValueError):
    pass


class Uncertain(RuntimeError):
    pass




class TooLarge(Uncertain):
    """Desktop refused the read because its answer would be too large; asking for less is safe."""

def digest(data):
    return hashlib.sha256(data).hexdigest()


class DesktopBridge:
    """Use the unchanged packaged MCP server with naturally inherited caller context."""
    def __init__(self, node, server):
        self.origin = os.environ.get('CODEX_THREAD_ID')
        if self.origin != MANAGER or not os.environ.get('CODEX_APP_TOOLS_PIPE_PATH'):
            raise ValueError('Run task host must be launched by the appointed Manager executor')
        self.lock = threading.RLock()
        self.command = [str(node), str(server)]
        self._start()

    def _start(self):
        self.responses = queue.Queue()
        self.next_id = 0
        self.process = subprocess.Popen(self.command,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.pid = self.process.pid
        self.reader = threading.Thread(target=self._read,daemon=True)
        self.reader.start()
        try:
            self._request('initialize',{'protocolVersion':'2024-11-05','capabilities':{},
                'clientInfo':{'name':'cbm_work_host','version':'0.1.0'}})
            self._write({'jsonrpc':'2.0','method':'notifications/initialized'})
            tools = self._request('tools/list',{})['tools']
            if not {'send_message_to_thread','wait_threads','read_thread'} <= {t['name'] for t in tools}:
                raise ValueError('Desktop coordination tools are unavailable')
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            try: self.responses.put(json.loads(line))
            except ValueError: pass
        self.responses.put(None)

    def alive(self):
        return self.process.poll() is None

    def reconnect(self):
        """Start a fresh MCP server process with the inherited Desktop context and initialise it.

        Requests that were in flight on the old process stay unconfirmed (callers get Uncertain and never resend).
        Raises when Desktop no longer accepts this process's context, e.g. after an app-server restart."""
        with self.lock:
            try: self.close()
            except Exception: pass
            self._start()

    def _write(self, message):
        self.process.stdin.write(json.dumps(message,ensure_ascii=False)+'\n')
        self.process.stdin.flush()

    def _request(self, method, params, timeout=35):
        with self.lock:
            self.next_id += 1
            current = self.next_id
            try:
                self._write({'jsonrpc':'2.0','id':current,'method':method,'params':params})
                deadline = time.monotonic()+timeout
                while time.monotonic()<deadline:
                    response = self.responses.get(timeout=max(.01,deadline-time.monotonic()))
                    if response is None: raise Uncertain('Desktop bridge disconnected; do not resend')
                    if response.get('id') == current: break
                else: raise queue.Empty()
            except (OSError,queue.Empty):
                raise Uncertain('Desktop request outcome unconfirmed; do not resend') from None
            if 'error' in response:
                raise RuntimeError(safe_error(response['error'].get('message','Desktop tool rejected')))
            return response['result']

    def call(self, name, arguments):
        if name not in ('send_message_to_thread','wait_threads','read_thread','list_threads','navigate_to_codex_page'):
            raise ValueError('Unsupported work-host tool')
        result = self._request('tools/call',{'name':name,'arguments':arguments,
            '_meta':{'x-codex-turn-metadata':{'thread_id':self.origin}}})
        text = '\n'.join(c.get('text','') for c in result.get('content',[]) if c['type']=='text')
        if result.get('isError'): raise RuntimeError(safe_error(text))
        return json.loads(text)

    def snapshot(self, thread_id):
        if thread_id == self.origin or thread_id not in WORKERS.values():
            raise ValueError('Self or unappointed work recipient refused')
        result = self.call('wait_threads',{'targets':[{'threadId':thread_id,'hostId':'local'}],'timeoutMs':0})
        polls = result.get('polls',[])
        if len(polls)!=1 or polls[0].get('thread',{}).get('id')!=thread_id:
            raise RuntimeError('Worker snapshot unavailable or target identity mismatch')
        return polls[0]

    def send(self, thread_id, prompt):
        if thread_id == self.origin or thread_id not in WORKERS.values():
            raise ValueError('Self or unappointed work recipient refused')
        return self.call('send_message_to_thread',{'threadId':thread_id,'hostId':'local','prompt':prompt})

    def find_final(self, thread_id, marker, baseline, turn_id=None):
        snapshot = self.snapshot(thread_id)
        message = snapshot.get('latestAssistantMessage') or {}
        turn = snapshot.get('latestTurn') or {}
        if (turn.get('id')!=baseline and message.get('phase')=='final_answer'
                and marker in message.get('text','')):
            return snapshot, [dict(message,status=turn.get('status'))]
        if turn.get('id')==baseline or snapshot['thread']['status']['type']=='active':
            return snapshot,[]
        # After an unknown send or restart, read only bounded summaries. Never
        # store/publish surrounding private messages. Exact public marker only.
        history = self.call('read_thread',{'threadId':thread_id,'hostId':'local',
            'turnLimit':3,'includeOutputs':False,'maxOutputCharsPerItem':20000})
        matches = []
        def visit(value, parent_turn=None, status=None):
            if isinstance(value,list):
                for child in value: visit(child,parent_turn,status)
            elif isinstance(value,dict):
                parent_turn = value.get('turnId',parent_turn)
                if value.get('status') in ('completed','failed','interrupted'):
                    status = value['status']
                    parent_turn = value.get('id',parent_turn)
                text = value.get('text')
                if isinstance(text,str) and marker in text and value.get('phase')=='final_answer':
                    matches.append(dict(value,turnId=parent_turn,status=status))
                for key,child in value.items():
                    if key not in ('text','arguments','outputs'): visit(child,parent_turn,status)
        visit(history)
        return snapshot,matches

    def close(self):
        if not self.process.stdin.closed: self.process.stdin.close()
        try: self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate();self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()


class GitHubIssues:
    def __init__(self, runtime, repository=REPOSITORY):
        self.runtime = Path(runtime)
        self.repository = repository

    def run(self, arguments, timeout=30):
        try:
            result = subprocess.run(['gh',*arguments],capture_output=True,text=True,
                encoding='utf-8',timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        except subprocess.TimeoutExpired:
            raise Uncertain('GitHub outcome unknown; reconcile marker before any retry') from None
        if result.returncode:
            raise RuntimeError(safe_error(result.stderr))
        return result.stdout.strip()

    def find(self, marker):
        # REST read avoids GitHub search-index lag. More than 100 recent issues
        # without a match stays ambiguous rather than re-creating an issue.
        rows = json.loads(self.run(['api',f'repos/{self.repository}/issues?state=all&per_page=100']))
        return [r['html_url'] for r in rows if marker in (r.get('body') or '') and 'pull_request' not in r]

    def create(self, task, specification):
        marker = '<!-- CBM-WORK:'+task+' -->'
        body = ('Owner submitted an explicit documentation task through Team Room.\n\n'
            +specification['text']+'\n\nAuthorized artifact: `'+specification['path']+'`\n\n'
            'Existing Developer proposes; host validates/publishes; existing Verifier reviews the actual artifact.\n'
            'No automatic commit, merge, deployment or owner acceptance.\n\n'+marker+'\n')
        path = self.runtime/(task+'-issue-body.md')
        path.write_text(body,encoding='utf-8')
        url = self.run(['issue','create','--repo',self.repository,'--title',specification['title'],
                        '--body-file',str(path)])
        if not re.fullmatch(r'https://github\.com/'+re.escape(self.repository)+r'/issues/\d+',url):
            raise Uncertain('GitHub create URL unconfirmed; reconcile task marker')
        actual = json.loads(self.run(['issue','view',url,'--repo',self.repository,'--json','body,url']))
        if marker not in actual['body']: raise Uncertain('Created issue marker readback unconfirmed')
        return actual['url']


class Work:
    """One active host writer. Desktop workers keep their native permissions."""
    def __init__(self, controller, bridge, *, project, github=None):
        self.c,self.store,self.bridge = controller,controller.store,bridge
        self.project = Path(project).resolve(strict=True)
        if github is None:
            from . import project as served
            github = GitHubIssues(self.store.root, served.repository() or REPOSITORY)
        self.github = github
        if bridge.origin != MANAGER or bridge.origin in WORKERS.values():
            raise ValueError('Manager must originate the work host, never target itself')
        self.preserving = ('.git','.bot-mode','node_modules','__pycache__','.codex','agent','agents','secrets')
        with self.store.transaction() as db:
            db.execute("UPDATE work_tasks SET hold=1,error='Restart: reconcile then Resume work explicitly' WHERE state NOT IN ('completed','failed','review_required')")

    def tasks(self):
        return self.store.rows('SELECT * FROM work_tasks ORDER BY seq')

    def current(self):
        return next((t for t in self.tasks() if t['state'] not in DONE),None)

    def status(self):
        return {'enabled':True,'origin':'Manager','origin_thread_id':self.bridge.origin,
            'workers':WORKERS,'max_seconds':MAX_SECONDS,'max_worker_turns':2,
            'stop_mode':'Stop scheduling; accepted Desktop turn continues',
            'tasks':[{k:t[k] for k in ('id','state','hold','issue_phase','issue_url','artifact_sha',
                    'verification','error','created','deadline')} | {'specification':json.loads(t['specification']),
                    'steps':[{k:s[k] for k in ('role','state','thread_id','turn_id','message_id')}
                        for s in self.steps(t['id'])]} for t in self.tasks()]}

    def steps(self, task):
        return self.store.rows('SELECT * FROM work_steps WHERE task_id=? ORDER BY rowid',(task,))

    def path(self, name):
        if (not isinstance(name,str) or '\\' in name or ':' in name or
            not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.md',name)):
            raise ValueError('Use a project-relative Markdown path without traversal')
        parts = PurePosixPath(name).parts
        if '..' in parts or '.' in parts or parts[-1].lower() in ('agents.md','skill.md'):
            raise ValueError('Policy files and traversal are outside task scope')
        if name!='README.md' and (not name.startswith('docs/') or name.lower().startswith('docs/context/')):
            raise ValueError('Supported scope: README.md or docs/ Markdown, excluding private/historical context')
        candidate = self.project/name
        for ancestor in (candidate,*candidate.parents):
            if ancestor==self.project: break
            if ancestor.is_symlink() or getattr(ancestor,'is_junction',lambda:False)():
                raise ValueError('Symlink/junction task paths refused')
        if not candidate.resolve().is_relative_to(self.project): raise ValueError('Artifact escapes project')
        if candidate.exists() and not candidate.is_file(): raise ValueError('Artifact must be a regular file')
        return candidate

    def read(self, name):
        path = self.path(name)
        if not path.exists(): return None
        data = path.read_bytes()
        if len(data)>MAX_BYTES: raise ValueError('Documentation input exceeds 64 KiB')
        text = data.decode('utf-8')
        if re.search(r'\b(?:sk-|eyJ)[A-Za-z0-9_.-]+|(?i:bearer)\s+\S+',text):
            raise ValueError('Potential credential material refused as task context')
        return {'text':text,'sha256':digest(data)}

    def inventory(self):
        result = {}
        for directory,folders,files in os.walk(self.project,followlinks=False):
            folders[:] = [f for f in folders if f not in self.preserving
                and not (Path(directory)/f).is_symlink()
                and not getattr(Path(directory)/f,'is_junction',lambda:False)()]
            for name in files:
                path = Path(directory)/name
                if path.is_symlink() or name.startswith('.env') or path.suffix in ('.pyc','.log'): continue
                result[path.relative_to(self.project).as_posix()] = digest(path.read_bytes())
        return result

    def preserved(self, task, *, artifact=False):
        context = json.loads(task['context'])
        actual = self.inventory()
        expected = dict(context['inventory'])
        path = json.loads(task['specification'])['path']
        if artifact: expected[path] = task['artifact_sha']
        if actual!=expected:
            raise WorkConflict('Project baseline changed; work held for owner review, no overwrite or rollback')

    def submit(self, data, *, intent_id=None, held=False):
        if not isinstance(data,dict) or set(data)!={'title','text','path','context_paths','client_task_id'}:
            raise ValueError('Run task requires title, text, path, context_paths and client_task_id')
        for key,limit in (('title',160),('text',6000),('client_task_id',128)):
            if not isinstance(data[key],str) or not data[key].strip() or len(data[key])>limit:
                raise ValueError('Invalid '+key)
        if '\n' in data['title']: raise ValueError('Title must be one line')
        paths = data['context_paths']
        if (not isinstance(paths,list) or len(paths)>4 or not all(isinstance(p,str) for p in paths)
                or len(set(paths))!=len(paths)):
            raise ValueError('Declare at most four distinct context paths')
        spec = {k:data[k] for k in ('title','text','path','context_paths')}
        self.path(spec['path'])
        encoded = json.dumps(spec,sort_keys=True)
        with self.c.mutex:
            previous = self.store.rows('SELECT * FROM work_tasks WHERE client_id=?',(data['client_task_id'],))
            if previous:
                if previous[0]['specification']!=encoded: raise WorkConflict('Task retry ID belongs to another request')
                return {'task_id':previous[0]['id'],'duplicate':True}
            if self.store.rows("SELECT id FROM message_intents WHERE state='ready' AND id!=?",(intent_id or '',)):
                raise WorkConflict('Manager decision awaits processing or explicit Resume')
            if self.current() or self.c.active or any(d['state'] not in ('completed','failed','interrupted','cancelled') for d in self.store.deliveries()):
                raise WorkConflict('Run task requires idle work and discussion')
            sources = {name:self.read(name) for name in paths}
            if any(value is None for value in sources.values()): raise ValueError('Declared context path does not exist')
            baseline = self.read(spec['path'])
            context = {'sources':sources,'baseline':baseline,'inventory':self.inventory()}
            if len(json.dumps(sources))+len((baseline or {}).get('text',''))>80000:
                raise ValueError('Combined task context exceeds 80 KiB')
            task = uuid.uuid4().hex
            with self.store.transaction() as db:
                db.execute('INSERT INTO work_tasks(id,client_id,specification,context,created,deadline,hold) VALUES(?,?,?,?,?,?,?)',
                    (task,data['client_task_id'],encoded,json.dumps(context),time.time(),time.time()+MAX_SECONDS,int(held)))
                if intent_id:
                    updated = db.execute("UPDATE message_intents SET task_id=?,state='task',error='' WHERE id=? AND state=? AND task_id IS NULL",(task,intent_id,'clarification' if held else 'ready'))
                    if updated.rowcount!=1: raise WorkConflict('Original intent already resolved')
                    self.publish(db,task,'manager',('Recovered task held for explicit Resume: ' if held else 'Starting task: ')+spec['title']+'\nArtifact: '+spec['path'])
                else:
                    self.publish(db,task,'owner','Run task: '+spec['title']+'\n'+spec['text']+'\nArtifact: '+spec['path'])
            self.c.wake.set()
            return {'task_id':task,'duplicate':False}

    def publish(self, db, task, sender, text):
        public_id = 'work-'+task+'-'+sender
        inserted = db.execute('INSERT OR IGNORE INTO messages(id,sender,recipient,text,created) VALUES(?,?,?,?,?)',
            (public_id,sender,'work',text,time.time()))
        if inserted.rowcount: self.store.emit(db,'work',{'task_id':task,'sender':sender,'text':text})

    def update(self, task, **values):
        with self.store.transaction() as db:
            db.execute('UPDATE work_tasks SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),task))
            self.store.emit(db,'work',{'task_id':task,'status':values.get('state'),'hold':values.get('hold'),
                'error':values.get('error'),'issue_url':values.get('issue_url')})

    def hold(self):
        with self.c.mutex:
            task = self.current()
            if task: self.update(task['id'],hold=1,error='Scheduling held; accepted Desktop turn continues')
        return {'held':True}

    def resume(self):
        with self.c.mutex:
            task = self.current()
            if not task: raise WorkConflict('No unfinished work task')
            if time.time()>=task['deadline']: raise WorkConflict('Work budget expired; review current result')
            if task['state']=='ambiguous': raise WorkConflict('Ambiguous work requires reconciliation; no replay')
            self.update(task['id'],hold=0,error='')
            self.c.wake.set()
        return {'resumed':True}

    def envelope(self, text, marker):
        text = text.strip()
        if text.startswith('```') and text.endswith('```'):
            text = '\n'.join(text.splitlines()[1:-1])
        data = json.loads(text)
        if not isinstance(data,dict) or data.get('marker')!=marker:
            raise ValueError('Worker final response does not match exact task marker')
        return data

    def new_step(self, task, role):
        spec,context = json.loads(task['specification']),json.loads(task['context'])
        marker = 'CBM-WORK:'+task['id']+':'+role
        common = ('The owner explicitly authorized this narrowly scoped work in the correlated request. Keep your appointed role. '
            'Write documentation and artifact content in English, regardless of the owner request language. '
            'Write the public summary or review findings in the owner request language, honoring any explicit '
            'conversation-language request. Preserve JSON keys, markers, paths, hashes and verdict values exactly. '
            'No delegation, messages to other chats, GitHub actions, commits, merge, deployment, memory writes '
            'or file edits. Only this task context and result are public; do not quote private history. '
            'Return one final JSON object, no commentary or report files. marker must be exactly '+marker+'.\n'
            'Issue: '+task['issue_url']+'\nOwner request: '+spec['text']+'\n')
        if role=='developer':
            prompt = (common+'Generate useful documentation for '+spec['path']+'. Native files must NOT be edited: '
                'the host applies your generated proposal after baseline validation. '
                'Return {"marker":"'+marker+'","path":"'+spec['path']+'","content":"full Markdown content",'
                '"summary":"brief change summary"}. Maximum UTF-8 content 64 KiB. '
                'Use verified supplied source facts; explicitly describe limitations.\nPublic sources:\n'
                +json.dumps(context['sources'],ensure_ascii=False)+'\nOriginal artifact:\n'
                +json.dumps(context['baseline'],ensure_ascii=False))
        else:
            prompt = (common+'Independently READ the actual host-published artifact at '+str(self.path(spec['path']))
                +'. Read-only tools are allowed for inspection; no file/issue/memory changes. '
                'Inspect the actual file, compute its SHA-256 and assess against the owner request. '
                'Do not infer success from the supplied proposal. Expected SHA-256 '+task['artifact_sha']+'. '
                'Return {"marker":"'+marker+'","path":"'+spec['path']+'","sha256":"actual SHA-256",'
                '"verdict":"PASS or FAIL","findings":"concrete assessment and limitations"}.\n'
                'Public sources:\n'+json.dumps(context['sources'],ensure_ascii=False))
        with self.store.transaction() as db:
            db.execute('INSERT INTO work_steps(id,task_id,role,thread_id,prompt) VALUES(?,?,?,?,?)',
                (marker,task['id'],role,WORKERS[role],prompt))

    def poll_step(self, task, step):
        if step['thread_id']==self.bridge.origin or step['thread_id']!=WORKERS[step['role']]:
            raise ValueError('Stored worker identity is not the appointed non-origin target')
        if step['state']=='pending':
            if task['hold']: return
            snap = self.bridge.snapshot(step['thread_id'])
            if snap['thread']['status']['type']!='idle':
                self.update(task['id'],hold=1,error=step['role']+' Desktop chat is busy; Resume work when idle')
                return
            self.preserved(task,artifact=step['role']=='verifier')
            with self.c.mutex:
                if self.c.draining or self.current()['hold']: return
                with self.store.transaction() as db:
                    db.execute("UPDATE work_steps SET state='sending',baseline_turn=?,cursor=?,sent_at=? WHERE id=?",
                        ((snap.get('latestTurn') or {}).get('id'),snap.get('cursor'),time.time(),step['id']))
            try:
                accepted = self.bridge.send(step['thread_id'],step['prompt'])
                if accepted.get('threadId')!=step['thread_id']: raise Uncertain('Send returned a different target; do not replay')
            except Exception as error:
                # Includes rejected/unconfirmed send: conservative single intent,
                # never risk a second accepted native work turn.
                self.update(task['id'],state='ambiguous',hold=1,error=safe_error(error))
                return
            with self.store.transaction() as db:
                db.execute("UPDATE work_steps SET state='accepted' WHERE id=?",(step['id'],))
            return
        if step['state'] in ('sending','accepted'):
            snap,matches = self.bridge.find_final(step['thread_id'],step['id'],step['baseline_turn'],step['turn_id'])
            latest = snap.get('latestTurn') or {}
            unique = {(m.get('turnId'),m.get('id')):m for m in matches
                if m.get('turnId')!=step['baseline_turn'] and m.get('status')=='completed'}
            if len(unique)==1:
                message = next(iter(unique.values()))
                result = self.envelope(message['text'],step['id'])
                with self.store.transaction() as db:
                    db.execute("UPDATE work_steps SET state='completed',turn_id=?,message_id=?,result=? WHERE id=?",
                        (message['turnId'],message.get('id'),json.dumps(result,ensure_ascii=False),step['id']))
                # Recover matching original outcome but retain restart/Stop hold.
                if task['state']=='ambiguous': self.update(task['id'],state=step['role'],error='Original result reconciled; Resume work explicitly')
            elif len(unique)>1:
                self.update(task['id'],state='ambiguous',hold=1,error='Multiple matching native results; owner review required')
            elif latest.get('id')!=step['baseline_turn'] and latest.get('status') in ('failed','interrupted'):
                self.update(task['id'],state='ambiguous',hold=1,error='Native worker '+latest['status']+'; exact task result unconfirmed, no replay')
            elif latest.get('id')!=step['baseline_turn'] and latest.get('status')=='completed':
                self.update(task['id'],state='ambiguous',hold=1,error='Completed native turn has no matching final marker; no replay')

    def apply(self, task, result):
        spec = json.loads(task['specification'])
        if set(result)!={'marker','path','content','summary'} or result['path']!=spec['path']:
            raise ValueError('Developer proposal exceeds declared artifact contract')
        if not isinstance(result['content'],str) or not result['content'].strip() or not isinstance(result['summary'],str):
            raise ValueError('Invalid documentation proposal')
        data = result['content'].encode('utf-8')
        if len(data)>MAX_BYTES or len(result['summary'])>1000: raise ValueError('Proposal exceeds supported size')
        if re.search(r'\b(?:sk-|eyJ)[A-Za-z0-9_.-]+|(?i:bearer)\s+\S+',result['content']):
            raise ValueError('Potential credential material refused in generated artifact')
        path = self.path(spec['path'])
        planned = digest(data)
        with self.c.mutex:
            # Stop and publication share this lock. Read the hold only after
            # acquiring it; a pre-lock snapshot can miss a completed Stop.
            current = self.current()
            if not current or current['id']!=task['id'] or current['hold'] or self.c.draining: return
            task = current
            # Persist the plan before writing so recovery recognizes an exact
            # publication without replaying the model turn.
            if task['state']!='applying':
                self.preserved(task)
                self.update(task['id'],state='applying',proposal=json.dumps(result,ensure_ascii=False),artifact_sha=planned)
                task = self.current()
            if path.exists() and digest(path.read_bytes())==planned:
                self.preserved(task,artifact=True)
            else:
                self.preserved(task)
                path.parent.mkdir(parents=True,exist_ok=True)
                baseline = json.loads(task['context'])['baseline']
                if baseline is None:
                    with path.open('xb') as stream: stream.write(data)
                else:
                    staging = self.store.root/(task['id']+'-artifact.md')
                    staging.write_bytes(data)
                    if digest(path.read_bytes())!=baseline['sha256']: raise WorkConflict('Target baseline changed before publication')
                    os.replace(staging,path)
                self.preserved(task,artifact=True)
            with self.store.transaction() as db:
                db.execute("UPDATE work_tasks SET state='verifier' WHERE id=?",(task['id'],))
                self.publish(db,task['id'],'developer','Artifact published: '+spec['path']+'\nSHA-256: '+planned+'\n'+result['summary'])

    def tick(self):
        task = self.current()
        if not task: return False
        try:
            steps = self.steps(task['id'])
            pending = next((s for s in steps if s['state']!='completed'),None)
            # Continue observing already accepted native turns even while held.
            if pending and pending['state'] in ('sending','accepted'):
                if time.time()>=task['deadline'] and not task['hold']:
                    self.update(task['id'],hold=1,error='900-second work budget expired; accepted turn may continue')
                    task = self.current()
                self.poll_step(task,pending)
                return True
            if self.c.draining:
                return False
            if task['hold']: return True
            if time.time()>=task['deadline']:
                self.update(task['id'],hold=1,error='900-second work budget expired; accepted turn may continue')
                return True
            if task['issue_phase']=='pending':
                with self.c.mutex:
                    if self.c.draining or self.current()['hold']: return False
                    self.update(task['id'],issue_phase='creating',state='creating_issue')
                try: url = self.github.create(task['id'],json.loads(task['specification']))
                except Exception as error:
                    self.update(task['id'],state='ambiguous',hold=1,error=safe_error(error))
                    return True
                self.update(task['id'],issue_phase='created',issue_url=url,state='developer')
                return True
            if task['issue_phase']=='creating':
                found = self.github.find('<!-- CBM-WORK:'+task['id']+' -->')
                if len(found)==1:
                    self.update(task['id'],issue_phase='created',issue_url=found[0],state='developer',hold=1,
                        error='Original issue reconciled; Resume work explicitly')
                else: self.update(task['id'],state='ambiguous',hold=1,error='Issue outcome unconfirmed; no create replay')
                return True
            if not steps:
                self.new_step(task,'developer');return True
            if pending:
                self.poll_step(task,pending);return True
            developer = next(s for s in steps if s['role']=='developer')
            if task['state'] in ('developer','applying'):
                self.apply(task,json.loads(developer['result']));return True
            verifier = next((s for s in steps if s['role']=='verifier'),None)
            if not verifier:
                self.new_step(task,'verifier');return True
            result = json.loads(verifier['result'])
            spec = json.loads(task['specification'])
            if (set(result)!={'marker','path','sha256','verdict','findings'} or result['path']!=spec['path']
                or result['sha256']!=task['artifact_sha'] or result['verdict'] not in ('PASS','FAIL')
                or not isinstance(result['findings'],str) or len(result['findings'])>6000):
                raise ValueError('Verifier did not attest the actual artifact and expected hash')
            self.preserved(task,artifact=True)
            with self.c.mutex:
                if self.c.draining or self.current()['hold']: return False
                with self.store.transaction() as db:
                    verdict = 'completed' if result['verdict']=='PASS' else 'review_required'
                    db.execute('UPDATE work_tasks SET state=?,verification=? WHERE id=?',
                        (verdict,json.dumps(result,ensure_ascii=False),task['id']))
                    self.publish(db,task['id'],'verifier',result['verdict']+' artifact review: '+spec['path']+'\n'
                        +result['findings']+'\nIssue: '+task['issue_url']+'\nOwner acceptance remains separate.')
            return True
        except Exception as error:
            uncertain = any(s['state'] in ('sending','accepted') for s in self.steps(task['id']))
            self.update(task['id'],state='ambiguous' if uncertain else 'review_required',hold=1,error=safe_error(error))
            return True

    def reconcile(self):
        task = self.current()
        if not task: return {'reconciled':False}
        if task['issue_phase']=='creating':
            found = self.github.find('<!-- CBM-WORK:'+task['id']+' -->')
            if len(found)==1:
                self.update(task['id'],issue_phase='created',issue_url=found[0],state='developer',hold=1,error='Original issue reconciled; Resume work')
            else: raise WorkConflict('Issue creation remains ambiguous; never replay create')
        else:
            for step in self.steps(task['id']):
                if step['state'] in ('sending','accepted'): self.poll_step(task,step)
        return {'reconciled':True}

    def close(self):
        self.hold()
        self.bridge.close()
