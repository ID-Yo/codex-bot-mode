"""One owned App Server child; JSONL RPC with explicit uncertain outcomes."""
from collections import deque
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time


class TransportError(RuntimeError):
    pass


class UnknownOutcome(TransportError):
    """A sent request may have been accepted. Never replay it automatically."""

    def __init__(self, method, request_id):
        self.method, self.request_id = method, request_id
        super().__init__(f'{method} request {request_id}: outcome unknown; reconcile before retry')


def safe_error(text):
    text = re.sub(r'(?i)(bearer\s+|(?:token|api[_-]?key|secret)\s*[=:]\s*)\S+',
                  r'\1[REDACTED]', str(text))
    text = re.sub(r'\b(?:sk-|eyJ)[A-Za-z0-9_.-]+', '[REDACTED]', text)
    return text[:500]


def public_event(message):
    """Allowlist lifecycle/public text only. Never retain reasoning/tool arguments."""
    method, params = message.get('method'), message.get('params', {})
    payload = {k: params[k] for k in ('threadId', 'turnId', 'itemId') if k in params}
    if method in ('turn/started', 'turn/completed'):
        turn = params.get('turn', {})
        payload['turnId'] = turn.get('id')
        payload['status'] = turn.get('status')
        if turn.get('error'):
            payload['error'] = safe_error(turn['error'].get('message', 'Turn failed'))
    elif method == 'item/agentMessage/delta':
        payload['delta'] = params.get('delta', '')
    elif method in ('item/started', 'item/completed'):
        item = params.get('item', {})
        if item.get('type') == 'reasoning':
            return None
        payload.update(itemId=item.get('id'), itemType=item.get('type'), status=item.get('status'))
        if item.get('type') == 'agentMessage':
            payload['text'] = item.get('text', '')
    else:
        return None
    return {'method': method, 'params': payload}


class AppServerTransport:
    """Trusted client. Only thread IDs created here (or checked owned bindings)."""

    def __init__(self, codex_path, runtime_root, *, owned_threads=None, command=None):
        self.runtime_root = Path(runtime_root).resolve(strict=True)
        self.owned_threads = {}
        for thread_id, cwd in (owned_threads or {}).items():
            self.owned_threads[thread_id] = self._workspace(cwd)
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._responses = {}
        self._pending = set()
        self._events = deque()
        self._next_id = 0
        self._dead = False
        self._closing = False
        self.stderr_bytes = 0
        self.denied_requests = 0
        self.pid = None
        # Overrides affect only this child, never the user's stored configuration.
        overrides = ['features.memories=false', 'features.multi_agent=false',
                     'features.multi_agent_v2=false', 'features.apps=false',
                     'features.plugins=false', 'features.browser_use=false',
                     'features.computer_use=false', 'features.hooks=false',
                     'features.code_mode=false',
                     'features.goals=false', 'features.code_mode_host=false',
                     'web_search="disabled"', 'approval_policy="never"',
                     'sandbox_mode="read-only"']
        argv = [str(Path(codex_path)), 'app-server', '--listen', 'stdio://']
        for value in overrides:
            argv.extend(['-c', value])
        self.process = subprocess.Popen(command or argv, cwd=self.runtime_root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.pid = self.process.pid
        self._readers = [threading.Thread(target=self._read_stdout, daemon=True),
                         threading.Thread(target=self._read_stderr, daemon=True)]
        for reader in self._readers:
            reader.start()

    def _workspace(self, cwd):
        path = Path(cwd).resolve(strict=True)
        if not path.is_dir() or path == self.runtime_root or not path.is_relative_to(self.runtime_root):
            raise ValueError('Room workspace must be a child of its runtime root')
        return path

    def _send(self, message):
        data = (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8')
        with self._write_lock:
            if self._dead or self._closing:
                raise TransportError('App Server disconnected')
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def _read_stderr(self):
        # Count/drain only: stderr can contain secrets and hidden model context.
        while chunk := self.process.stderr.read(4096):
            self.stderr_bytes += len(chunk)

    def _deny(self, message):
        method = message['method']
        if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
            reply = {'result': {'decision': 'decline'}}
        elif method == 'item/permissions/requestApproval':
            reply = {'result': {'permissions': {}, 'scope': 'turn'}}
        elif method == 'item/tool/call':
            reply = {'result': {'contentItems': [{'type': 'inputText', 'text': 'Disabled in discussion room'}], 'success': False}}
        else:
            reply = {'error': {'code': -32601, 'message': 'Disabled in discussion room'}}
        self.denied_requests += 1
        self._send({'id': message['id'], **reply})

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                message = json.loads(line.decode('utf-8'))
                if not isinstance(message, dict):
                    raise ValueError('Invalid JSON-RPC object')
                if 'id' in message and 'method' in message:
                    self._deny(message)
                elif 'id' in message:
                    with self._condition:
                        if message['id'] in self._pending:
                            self._responses[message['id']] = message
                        self._condition.notify_all()
                elif event := public_event(message):
                    with self._condition:
                        # Overflow is a disconnect, never silently lose a terminal event.
                        if len(self._events) >= 10000:
                            raise ValueError('Event backlog exceeded')
                        self._events.append(event)
                        self._condition.notify_all()
        except (ValueError, OSError, TransportError):
            pass
        finally:
            with self._condition:
                self._dead = True
                self._condition.notify_all()

    def _request(self, method, params, timeout=30, before_send=None):
        with self._condition:
            self._next_id += 1
            request_id = self._next_id
            self._pending.add(request_id)
        try:
            if before_send:
                before_send(request_id)
            try:
                self._send({'id': request_id, 'method': method, 'params': params})
            except (OSError, TransportError):
                raise UnknownOutcome(method, request_id) from None
            deadline = time.monotonic() + timeout
            with self._condition:
                while request_id not in self._responses:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._dead:
                        raise UnknownOutcome(method, request_id)
                    self._condition.wait(remaining)
                reply = self._responses.pop(request_id)
            if 'error' in reply:
                raise TransportError(safe_error(reply['error'].get('message', 'RPC rejected')))
            return reply['result']
        finally:
            with self._condition:
                self._pending.discard(request_id)
                self._responses.pop(request_id, None)

    def initialize(self):
        result = self._request('initialize', {'clientInfo': {
            'name': 'cbm_team_room', 'title': 'Team Room', 'version': '0.1.0'}})
        self._send({'method': 'initialized', 'params': {}})
        return result

    def models(self):
        models, cursor = [], None
        while True:
            result = self._request('model/list', {'cursor': cursor, 'includeHidden': False})
            models.extend(result['data'])
            cursor = result.get('nextCursor')
            if not cursor:
                return models

    def account_status(self):
        result = self._request('account/read', {'refreshToken': False})
        account = result.get('account')
        return {'present': account is not None, 'type': account.get('type') if account else None,
                'requiresOpenaiAuth': result.get('requiresOpenaiAuth')}

    def configuration(self):
        # Never return auth/environment/layers; the selected provider is not changed.
        config = self._request('config/read', {'includeLayers': False})['config']
        return {k: config.get(k) for k in ('model', 'model_provider')}

    @staticmethod
    def _permissions(result):
        sandbox = result.get('sandbox', {})
        if (sandbox.get('type') != 'readOnly' or sandbox.get('networkAccess', False)
                or result.get('approvalPolicy') != 'never'):
            raise TransportError('Server did not retain read-only / never permissions')

    def _discussion_config(self):
        config = self._request('config/read', {'includeLayers': False})['config']
        # Disable each configured MCP server without copying any of its credentials.
        return {f'mcp_servers.{name}.enabled': False for name in (config.get('mcp_servers') or {})}

    def start_thread(self, cwd, model, instructions):
        cwd = self._workspace(cwd)
        result = self._request('thread/start', {'cwd': str(cwd), 'model': model,
            'sandbox': 'read-only', 'approvalPolicy': 'never', 'ephemeral': False,
            'developerInstructions': instructions, 'config': self._discussion_config()})
        thread_id = result['thread']['id']
        self.owned_threads[thread_id] = cwd
        self._permissions(result)
        return result

    def _owned(self, thread_id):
        if thread_id not in self.owned_threads:
            raise ValueError('Thread is not owned by this room runtime')
        return self.owned_threads[thread_id]

    def read_thread(self, thread_id):
        cwd = self._owned(thread_id)
        result = self._request('thread/read', {'threadId': thread_id, 'includeTurns': True})
        if Path(result['thread']['cwd']).resolve() != cwd:
            raise TransportError('Stored thread workspace does not match owned binding')
        return result

    def resume_thread(self, thread_id, model, instructions=None):
        cwd = self._owned(thread_id)
        self.read_thread(thread_id)  # Validate original cwd before resuming any turn.
        result = self._request('thread/resume', {'threadId': thread_id, 'cwd': str(cwd),
            'model': model, 'sandbox': 'read-only', 'approvalPolicy': 'never',
            'config': self._discussion_config(), **({'developerInstructions': instructions} if instructions is not None else {})})
        self._permissions(result)
        return result

    def start_turn(self, thread_id, text, *, effort=None, timeout=30, before_send=None, images=()):
        self._owned(thread_id)
        params = {'threadId': thread_id, 'input': [{'type': 'text', 'text': text}],
                  'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False}}
        params['input'].extend({'type': 'localImage', 'path': str(path)} for path in images)
        if effort:
            params['effort'] = effort
        return self._request('turn/start', params, timeout, before_send)

    def interrupt(self, thread_id, turn_id):
        self._owned(thread_id)
        return self._request('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})

    def next_event(self, thread_id, turn_id=None, *, timeout=30):
        self._owned(thread_id)
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for index, event in enumerate(self._events):
                    p = event['params']
                    if p.get('threadId') == thread_id and (turn_id is None or p.get('turnId') == turn_id):
                        del self._events[index]
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('No matching event; terminal outcome unconfirmed')
                if self._dead:
                    raise TransportError('Disconnected before terminal observation')
                self._condition.wait(remaining)

    def probe_command(self, command, cwd):
        """Operator-only gate probe. Not exposed to a model or public room endpoint."""
        cwd = self._workspace(cwd)
        return self._request('command/exec', {'command': command, 'cwd': str(cwd),
            'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False}, 'timeoutMs': 10000})

    def close(self):
        if self._closing:
            return
        self._closing = True
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for reader in self._readers:
            reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
