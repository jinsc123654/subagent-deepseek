"""Loopback Harness session bridge: public APIs, owned sessions, text-only results."""
import asyncio
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx

from .config import Settings

# The official token exchange puts a credential in the URL. MCP enables INFO
# logging globally; never let HTTP request/debug logs disclose that URL/cookies.
logging.getLogger('httpx').setLevel(logging.CRITICAL + 1)
logging.getLogger('httpcore').setLevel(logging.CRITICAL + 1)


CODEX_PERMISSION_MODES = {
    'read-only': 'read-only',
    'workspace-write': 'workspace-write',
    'approve-for-me': 'danger-full-access',
    'full-access': 'danger-full-access',
}


def inherited_permission(value: str | None) -> str:
    """Map the caller's current Codex permission mode; never infer a missing value."""
    if value not in CODEX_PERMISSION_MODES:
        raise HarnessError('Current codex_permission is required; do not infer it from the task or cached MCP environment')
    return CODEX_PERMISSION_MODES[value]


class HarnessError(Exception):
    """Safe API failures; URLs with tokens and raw errors never reach the model."""


def atomic_json(path: Path, data: dict) -> None:
    """Replace private bridge state without exposing credentials in temporary files."""
    fd, name = tempfile.mkstemp(prefix='.harness-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, ensure_ascii=False)
            out.write('\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class SessionStore:
    """Persist only bridge-owned session identities; serialize cross-process writes."""
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def locked(self):
        lock = self.path.with_suffix('.lock')
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            yield

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, OSError):
            raise HarnessError('Harness session registry is unreadable; no sessions were changed') from None

    def put(self, session_id: str, **fields) -> dict:
        with self.locked():
            data = self.read()
            data[session_id] = {**data.get(session_id, {}), **fields}
            atomic_json(self.path, data)
            return data[session_id]

    def get(self, session_id: str) -> dict:
        data = self.read()
        if session_id not in data:
            raise HarnessError('Session is not owned by this bridge; refusing access')
        return data[session_id]


class HarnessBridge:
    """Operate existing web Host sessions without launching another Harness process."""
    def __init__(self, settings: Settings, path: Path, transport=None):
        self.settings = settings
        self.origin = settings.harness_url.rstrip('/')
        self.cookie_path = path.parent / '.harness-auth.json'
        self.store = SessionStore(path.parent / 'harness-sessions.json')
        self.transport = transport
        self.auth_lock = asyncio.Lock()
        self.operations: dict[str, asyncio.Lock] = {}

    def selection(self) -> dict:
        """Pin the configured web provider and model without hidden fallback."""
        return {'provider': self.settings.harness_provider, 'model': self.settings.model,
                'reasoningEffort': self.settings.reasoning_effort}

    def cookies(self) -> dict:
        if not self.cookie_path.exists():
            return {}
        try:
            info = self.cookie_path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise HarnessError('Harness API cookie file must be private and user-owned')
            value = json.loads(self.cookie_path.read_text())
            if value.get('origin') != self.origin:
                return {}
            cookies = value.get('cookies')
            if not isinstance(cookies, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in cookies.items()):
                raise ValueError()
            return cookies
        except (ValueError, OSError):
            raise HarnessError('Harness API cookie file is unreadable') from None

    async def authenticate(self, client: httpx.AsyncClient) -> None:
        """Exchange the process launch URL through /connect; never forge cookies."""
        async with self.auth_lock:
            try:
                text = Path(self.settings.harness_launch_log).read_text(errors='replace')
                token = None
                for candidate in re.findall(r'https?://[^\s\x1b]+', text):
                    url = urlsplit(candidate.rstrip(')\"\''))
                    if f'{url.scheme}://{url.netloc}' == self.origin and url.path == '/':
                        values = parse_qs(url.query).get('token', [])
                        if len(values) == 1 and re.fullmatch(r'[A-Za-z0-9_-]+', values[0]):
                            token = values[0]
                if not token:
                    raise HarnessError('No current Harness launch URL in configured log; start the web service normally')
                client.cookies.clear()
                reply = await client.get('/connect', params={'token': token, 'interactions': 'none'})
                if reply.status_code != 302 or not client.cookies:
                    raise HarnessError('Harness authentication failed; reopen or restart its normal web launcher')
                atomic_json(self.cookie_path, {'origin': self.origin, 'cookies': dict(client.cookies)})
            except (httpx.HTTPError, OSError):
                raise HarnessError('Cannot authenticate to local Harness; check service and configured launch log') from None

    async def rpc(self, endpoint: str, args: dict) -> object:
        """Use exact Connection RPC envelopes, never retry ambiguous mutations."""
        rid = str(uuid4())
        envelope = {'type': 'client-request', 'rpcId': rid, 'method': endpoint, 'payload': {'args': args}}
        try:
            async with httpx.AsyncClient(base_url=self.origin, trust_env=False,
                    follow_redirects=False, timeout=30, cookies=self.cookies(), transport=self.transport) as client:
                if not client.cookies:
                    await self.authenticate(client)
                reply = await client.post('/api/' + endpoint, json=envelope)
                if reply.status_code == 401:
                    # HTTP 401 is rejected before dispatch, so this retry cannot duplicate a task.
                    await self.authenticate(client)
                    reply = await client.post('/api/' + endpoint, json=envelope)
                if reply.status_code != 200:
                    raise HarnessError(f'Harness HTTP {reply.status_code}; no automatic task retry')
                data = reply.json()
                if data.get('type') != 'server-response' or data.get('rpcId') != rid:
                    raise HarnessError('Harness RPC response identity mismatch')
                result = data['result']
                if result.get('ok') is not True:
                    code = result.get('error', {}).get('code', '')
                    safe = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_/-]{1,80}', code) else 'request-rejected'
                    raise HarnessError('Harness RPC failed: ' + safe)
                return result.get('value')
        except httpx.HTTPError:
            raise HarnessError('Harness transport failed; mutation outcome may be unknown. Query the existing session before retrying') from None
        except (KeyError, ValueError, TypeError):
            raise HarnessError('Harness returned an invalid response') from None

    async def request(self, method: str, request: dict) -> object:
        return await self.rpc('session/' + method, {'request': request})

    def owned(self, session_id: str) -> dict:
        record = self.store.get(session_id)
        if record.get('origin') != self.origin:
            raise HarnessError('Session belongs to a different configured Harness origin')
        self.settings.directory(record['cwd'])
        return record

    async def projection(self, session_id: str) -> dict:
        self.owned(session_id)
        value = await self.request('projections', {'sessionId': session_id})
        if not isinstance(value, dict) or value.get('kind') != 'sequenced':
            raise HarnessError('Harness session is missing or requires migration; no automatic replacement')
        return value

    def check_controls(self, projection: dict, record: dict, check_permission: bool = True) -> None:
        values = projection['values']
        cwd = values.get('workingDirectory')
        if not cwd or str(self.settings.directory(cwd)) != record['cwd']:
            raise HarnessError('Harness working directory changed; refusing to submit into another directory')
        if check_permission and values.get('permissions', {}).get('currentValue') != record.get('permission_mode', self.settings.mode):
            raise HarnessError('Harness session permission preset changed; restore it in the web UI before sending')
        if values.get('modelSelection', {}).get('next') != self.selection():
            raise HarnessError('Harness model selection changed; restore the configured route before sending')

    async def start(self, task: str, cwd: str, title: str = '', codex_permission: str | None = None) -> dict:
        mode = inherited_permission(codex_permission)
        if not task.strip() or len(task) > 100000:
            raise HarnessError('task must contain 1–100000 characters')
        if len(title) > 120:
            raise HarnessError('title must be at most 120 characters')
        directory = str(self.settings.directory(cwd))
        sid = str(uuid4())
        name = title.strip() or 'DeepSeek ' + sid[:8]
        if not name.startswith('[Codex] '):
            name = '[Codex] ' + name
        self.store.put(sid, cwd=directory, origin=self.origin, title=name, state='creating', after_seq=0, permission_mode=mode, codex_permission=codex_permission, group='Codex')
        try:
            created = await self.request('create', {'cwd': directory, 'sessionId': sid})
            if created.get('sessionId') != sid:
                raise HarnessError('Harness created a different session; task was not sent')
            await self.request('rename', {'sessionId': sid, 'title': name})
            await self.request('selectModel', {'sessionId': sid, **self.selection()})
            await self.rpc('commands/execute', {'agentId': sid,
                'line': '/permission ' + mode, 'submittedAttachments': []})
            projection = await self.projection(sid)
            self.check_controls(projection, self.owned(sid))
            await self.send(sid, task, codex_permission=codex_permission)
            return {'session_id': sid, 'title': name, 'web_url': self.origin + '/',
                    'status': 'submitted', 'permission': mode, 'codex_permission': codex_permission, 'group': 'Codex',
                    'model': self.selection(), 'note': 'Open this named conversation in the web sidebar; no full live transcript is returned to Codex.'}
        except (HarnessError, ValueError) as error:
            self.store.put(sid, state='setup-or-submission-uncertain')
            raise HarnessError(f'{error}; session_id={sid}. Inspect this session before starting another') from None

    async def sync_permissions(self, session_id: str, codex_permission: str | None) -> dict:
        """Synchronize the owned session to the parent's explicitly supplied current mode."""
        mode = inherited_permission(codex_permission)
        record = self.owned(session_id)
        projection = await self.projection(session_id)
        self.check_controls(projection, record, check_permission=False)
        if projection['values'].get('permissions', {}).get('currentValue') != mode:
            await self.rpc('commands/execute', {'agentId': session_id,
                'line': '/permission ' + mode, 'submittedAttachments': []})
            projection = await self.projection(session_id)
        if projection['values'].get('permissions', {}).get('currentValue') != mode:
            raise HarnessError('Harness permission synchronization failed; no prompt was sent')
        self.store.put(session_id, permission_mode=mode, codex_permission=codex_permission)
        return {'session_id': session_id, 'permission': mode, 'codex_permission': codex_permission}

    async def send(self, session_id: str, text: str, steer: bool = False, codex_permission: str | None = None) -> dict:
        inherited_permission(codex_permission)
        if not text.strip() or len(text) > 100000:
            raise HarnessError('text must contain 1–100000 characters')
        self.owned(session_id)
        async with self.operations.setdefault(session_id, asyncio.Lock()):
            await self.sync_permissions(session_id, codex_permission)
            record = self.owned(session_id)
            projection = await self.projection(session_id)
            self.check_controls(projection, record)
            rid = str(uuid4())
            self.store.put(session_id, after_seq=projection['asOfSeq'], request_id=rid, state='submission-uncertain')
            try:
                result = await self.request('prompt', {'sessionId': session_id, 'requestId': rid,
                    'mode': 'steer' if steer else 'queue', 'content': [{'type': 'text', 'text': text}],
                    'clientTimeZone': 'Asia/Shanghai'})
                if result.get('accepted') is not True:
                    raise HarnessError('Harness did not acknowledge prompt acceptance')
            except HarnessError as error:
                raise HarnessError(f'{error}; session_id={session_id}, request_id={rid}') from None
            self.store.put(session_id, state='submitted')
            return {'session_id': session_id, 'request_id': rid, 'accepted': True,
                    'delivery': 'steer' if steer else 'queue', 'permission': record['permission_mode']}

    def list_sessions(self) -> dict:
        """Recover owned ids after a lost receipt or MCP restart, without other chats."""
        records = self.store.read()
        sessions = [{'session_id': sid, 'title': row.get('title'),
                     'submission_state': row.get('state'), 'group': 'Codex', 'web_url': self.origin + '/'}
                    for sid, row in records.items() if row.get('origin') == self.origin]
        return {'sessions': sessions[-20:], 'note': 'Saved receipts only; supply a session_id for current live status'}

    async def worker_tree(self, session_id: str, max_nodes: int = 32) -> dict:
        """Observe only authoritative descendants of this owned coordinator; never adopt their ids."""
        self.owned(session_id)
        root = await self.projection(session_id)
        pending = [(session_id, 1, root)]
        nodes, errors, seen = [], [], {session_id}
        truncated = False
        while pending:
            parent, depth, projection = pending.pop(0)
            catalog = projection['values'].get('subagentCatalog', [])
            if not isinstance(catalog, list):
                errors.append('Invalid native worker catalog')
                continue
            for row in catalog:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in seen:
                    errors.append('Invalid or cyclic worker entry')
                    continue
                if len(nodes) >= max_nodes:
                    truncated = True
                    break
                sid = row['id']; seen.add(sid)
                node = dict(id=sid, parent_id=parent, depth=depth, mode=row.get('mode', 'unknown'), label=str(row.get('label', ''))[:200])
                nodes.append(node)
                if node['mode'] == 'external':
                    continue
                try:
                    child = await self.request('projections', {'sessionId': sid})
                    if child.get('kind') != 'sequenced':
                        raise HarnessError('Missing worker projection')
                    values = child['values']
                    if str(self.settings.directory(values.get('workingDirectory', ''))) != self.owned(session_id)['cwd']:
                        raise HarnessError('Worker changed directory outside coordinator root')
                    timing = values.get('subagentTiming', {})
                    node.update(running=bool(timing.get('active')), completed=timing.get('lastTurnCompleted'),
                                seq=child.get('asOfSeq'), model=values.get('modelSelection', {}).get('next'))
                    if depth < 2:
                        pending.append((sid, depth + 1, child))
                    elif values.get('subagentCatalog'):
                        truncated = True
                except (HarnessError, ValueError, TypeError, KeyError):
                    errors.append('Worker observation unavailable: ' + sid)
        return dict(root_id=session_id, nodes=nodes, errors=errors, truncated=truncated)

    async def status(self, session_id: str) -> dict:
        record = self.owned(session_id)
        projection, catalog = await asyncio.gather(self.projection(session_id), self.rpc('session/list', {'_request': {}}))
        row = next((x for x in catalog['items'] if x['sessionId'] == session_id), None)
        if row is None:
            raise HarnessError('Harness session is not visible in the current catalog')
        values = projection['values']
        inbox = values.get('inbox', {})
        questions = values.get('userQuestions', {}).get('active', [])
        queued = sum(len(v) for v in inbox.values() if isinstance(v, list))
        state = 'needs-input' if questions else 'running' if row['running'] else 'queued' if queued else 'idle'
        return {'session_id': session_id, 'title': values.get('title', record['title']),
                'status': state, 'submission_state': record['state'], 'queued_messages': queued,
                'as_of_seq': projection['asOfSeq'], 'permission': values.get('permissions', {}).get('currentValue'),
                'model': values.get('modelSelection', {}).get('next'),
                'token_usage': values.get('tokenUsage', {}), 'pending_questions': questions,
                'web_url': self.origin + '/'}

    async def result(self, session_id: str, wait_seconds: int = 0, max_chars: int = 12000) -> dict:
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 60:
            raise HarnessError('wait_seconds must be an integer from 0 to 60')
        if type(max_chars) is not int or not 1 <= max_chars <= 100000:
            raise HarnessError('max_chars must be from 1 to 100000')
        deadline = time.monotonic() + wait_seconds
        while True:
            status = await self.status(session_id)
            if status['status'] == 'needs-input':
                return {**status, 'finished': False, 'completed': False}
            if status['status'] == 'idle':
                result = await self.finished_result(session_id, status, max_chars)
                if result['finished']:
                    return result
            if time.monotonic() >= deadline:
                return {**status, 'finished': False, 'completed': False,
                        'note': 'No completed latest turn observed yet; do not resubmit the task'}
            await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))

    async def finished_result(self, session_id: str, status: dict, max_chars: int) -> dict:
        record = self.owned(session_id)
        page = await self.request('page', {'address': {'kind': 'session', 'sessionId': session_id},
                    'throughSeq': status['as_of_seq'], 'maxMessages': 20})
        events = [r['event'] for r in page.get('records', []) if r.get('type') == 'event'
                  and r.get('event', {}).get('seq', -1) > record['after_seq']]
        messages = [e for e in events if e['type'] == 'assistant/message']
        endings = [e for e in events if e['type'] == 'turn/end']
        if not endings:
            return {**status, 'finished': False, 'completed': False}
        last = endings[-1]
        reason = last['data'].get('reason', {}).get('kind')
        if reason != 'completed':
            safe = reason if reason in ('aborted', 'blocked', 'error', 'max-tokens', 'interrupted', 'forked') else 'unknown'
            return {**status, 'finished': True, 'completed': False, 'stop_reason': safe}
        messages = [e for e in messages if e['data'].get('turn') == last['data']['turn'] and e['seq'] < last['seq']]
        if not messages:
            return {**status, 'finished': True, 'completed': False, 'stop_reason': 'no-answer-in-history-window'}
        message = messages[-1]['data']['message']
        source = message.get('source', {})
        if source.get('provider') != self.settings.harness_provider or source.get('model') != self.settings.model:
            raise HarnessError('Completed reply used a different model route')
        text = '\n'.join(b['text'] for b in message.get('content', []) if b.get('type') == 'text' and isinstance(b.get('text'), str))
        return {**status, 'finished': True, 'completed': bool(text.strip()), 'answer': text[:max_chars],
                'answer_truncated': len(text) > max_chars, 'stop_reason': 'completed' if text.strip() else 'empty-answer'}

    async def cancel(self, session_id: str) -> dict:
        self.owned(session_id)
        value = await self.request('cancel', {'sessionId': session_id})
        return {'session_id': session_id, 'accepted': value.get('accepted') is True,
                'note': 'Cancels the current turn; queued messages are retained by Harness.'}

    async def answer(self, session_id: str, call_id: str, answers: list[dict], codex_permission: str | None = None) -> dict:
        await self.sync_permissions(session_id, codex_permission)
        projection = await self.projection(session_id)
        self.check_controls(projection, self.owned(session_id))
        pending = projection['values'].get('userQuestions', {}).get('active', [])
        question = next((q for q in pending if q.get('callId') == call_id), None)
        if not question or question.get('state') != 'continued':
            raise HarnessError('This question is not a continued question; answer foreground questions in the web UI')
        if {a.get('id') for a in answers} != {q['id'] for q in question['questions']} or len(answers) != len(question['questions']):
            raise HarnessError('Answer each pending question exactly once')
        for item in answers:
            if set(item) - {'id', 'selected', 'custom'} or not isinstance(item.get('selected'), list) or not all(isinstance(s, str) for s in item['selected']) or ('custom' in item and not isinstance(item['custom'], str)):
                raise HarnessError('Invalid structured question answer')
        value = await self.rpc('userQuestions/answer', {'agentId': session_id, 'callId': call_id, 'answer': {'answers': answers}})
        return {'session_id': session_id, 'accepted': value is True}
