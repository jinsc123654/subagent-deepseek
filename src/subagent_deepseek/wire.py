"""Single-thread app-server JSON-RPC transport with unattended request handling."""
import asyncio
import json
from typing import Any
from collections.abc import Awaitable, Callable

Progress = Callable[[str], Awaitable[None]]


async def silent_progress(message: str) -> None:
    """Default progress sink for non-MCP callers."""



class Failure(Exception):
    """Safe failure metadata; raw protocol errors and runtime stderr stay private."""
    def __init__(self, stage: str, category: str):
        self.stage = stage
        self.category = category
        super().__init__(f'{stage}: {category}')


def obj(value: Any, stage: str) -> dict:
    """Validate objects received from the child process."""
    if not isinstance(value, dict):
        raise Failure(stage, 'invalid-protocol')
    return value


def identifier(value: Any) -> str:
    """Validate nonempty protocol identifiers."""
    if not isinstance(value, str) or not value:
        raise Failure('protocol', 'invalid-identifier')
    return value


class Wire:
    """Own one transport, an ephemeral thread, and exactly one turn."""
    def __init__(self, process: asyncio.subprocess.Process, progress: Progress = silent_progress):
        self.progress = progress
        self.process = process
        self.pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.counter = 0
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.provisional_turn: str | None = None
        self.starting_turn = False
        self.denials: list[str] = []
        self.error: Failure | None = None
        self.reader = asyncio.create_task(self._read())

    async def send(self, message: dict) -> None:
        """Write a complete JSON-RPC frame without exposing its contents."""
        try:
            self.process.stdin.write((json.dumps(message) + '\n').encode())
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError):
            raise Failure('transport', 'closed') from None

    async def request(self, method: str, params: dict) -> dict:
        """Correlate a request with its response while the reader keeps draining."""
        if self.error:
            raise self.error
        self.counter += 1
        key = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({'jsonrpc': '2.0', 'id': key, 'method': method, 'params': params})
            return obj(await future, method)
        finally:
            self.pending.pop(key, None)
            if future.done() and not future.cancelled():
                future.exception()  # Observe errors even when sending failed first.

    def _run_ids(self, params: dict, nullable: bool = False) -> None:
        if self.thread_id is None or params.get('threadId') != self.thread_id:
            raise Failure('server-request', 'wrong-thread')
        turn = params.get('turnId')
        if nullable and turn is None:
            return
        turn = identifier(turn)
        if self.turn_id is not None:
            if turn != self.turn_id:
                raise Failure('server-request', 'wrong-turn')
        elif self.starting_turn:
            if self.provisional_turn not in (None, turn):
                raise Failure('server-request', 'wrong-turn')
            self.provisional_turn = turn
        else:
            raise Failure('server-request', 'no-active-turn')

    async def _server_request(self, message: dict) -> None:
        method = message['method']
        params = obj(message.get('params'), 'server-request')
        self._run_ids(params, method == 'mcpServer/elicitation/request')
        if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
            result = {'decision': 'decline'}
        elif method == 'item/permissions/requestApproval':
            result = {'permissions': {}, 'scope': 'turn'}
        elif method == 'item/tool/requestUserInput':
            result = {'answers': {}}
        elif method == 'mcpServer/elicitation/request':
            result = {'action': 'decline', 'content': None, '_meta': None}
        else:
            await self.send({'jsonrpc': '2.0', 'id': message['id'], 'error': {
                'code': -32601, 'message': 'Unsupported unattended request'}})
            raise Failure('server-request', 'unsupported-request')
        if method not in self.denials:
            self.denials.append(method)
        await self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})

    async def _read(self) -> None:
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise Failure('transport', 'early-eof')
                message = obj(json.loads(line), 'transport')
                if 'method' in message:
                    if 'id' in message:
                        await self._server_request(message)
                    elif message['method'] in ('item/started', 'item/completed', 'turn/completed', 'turn/started'):
                        self.events.put_nowait(message)
                elif 'id' in message:
                    future = self.pending.get(message['id'])
                    if future is None or future.done():
                        continue
                    if 'error' in message:
                        future.set_exception(Failure('rpc', 'request-rejected'))
                    else:
                        future.set_result(message.get('result'))
                else:
                    raise Failure('transport', 'invalid-protocol')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = error if isinstance(error, Failure) else Failure('transport', 'invalid-protocol')
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.error)
            # Wake the event consumer; a full queue is already ready to consume.
            if not self.events.full():
                self.events.put_nowait(None)

    async def next_event(self) -> dict:
        """Drain buffered terminal notifications before reporting a later EOF."""
        if self.error and self.events.empty():
            raise self.error
        event = await self.events.get()
        if event is None:
            raise self.error or Failure('transport', 'closed')
        return event

    async def initialize(self) -> None:
        """Perform initialize/initialized before starting a thread."""
        await self.request('initialize', {
            'clientInfo': {'name': 'subagent-deepseek', 'version': '0.9.2'},
            'capabilities': {'experimentalApi': False, 'requestAttestation': False}})
        await self.send({'jsonrpc': '2.0', 'method': 'initialized'})

    async def start_thread(self, cwd: str, model: str, effort: str, mode: str) -> None:
        """Create and verify the effective DeepSeek model route before any turn."""
        response = await self.request('thread/start', {
            'cwd': cwd, 'ephemeral': True, 'model': model,
            'modelProvider': 'deepseek', 'approvalPolicy': 'never', 'sandbox': mode,
            'config': {'model_reasoning_effort': effort}})
        thread = obj(response.get('thread'), 'thread/start')
        if thread.get('ephemeral') is not True:
            raise Failure('thread/start', 'not-ephemeral')
        if (response.get('modelProvider'), response.get('model'), response.get('reasoningEffort')) != ('deepseek', model, effort):
            raise Failure('thread/start', 'route-mismatch')
        expected = {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite',
                    'danger-full-access': 'dangerFullAccess'}[mode]
        sandbox = obj(response.get('sandbox'), 'thread/start')
        if response.get('approvalPolicy') != 'never' or sandbox.get('type') != expected:
            raise Failure('thread/start', 'permission-mismatch')
        self.thread_id = identifier(thread.get('id'))

    async def run_turn(self, task: str, effort: str) -> str:
        """Select the final answer only after this turn completes successfully."""
        self.starting_turn = True
        response = await self.request('turn/start', {
            'threadId': self.thread_id, 'effort': effort,
            'input': [{'type': 'text', 'text': task, 'text_elements': []}]})
        turn = obj(response.get('turn'), 'turn/start')
        self.turn_id = identifier(turn.get('id'))
        self.starting_turn = False
        if self.provisional_turn not in (None, self.turn_id):
            raise Failure('turn/start', 'wrong-turn')
        final = None
        unphased = None
        await self.progress('任务已开始，等待模型与工具活动')
        while True:
            event = await self.next_event()
            params = obj(event.get('params'), 'notification')
            if params.get('threadId') != self.thread_id:
                continue
            method = event['method']
            if method == 'turn/started':
                turn = obj(params.get('turn'), method)
                if turn.get('id') != self.turn_id:
                    raise Failure(method, 'wrong-turn')
                continue
            if method == 'turn/completed':
                turn = obj(params.get('turn'), method)
                if turn.get('id') != self.turn_id:
                    continue
                status = turn.get('status')
                if status != 'completed':
                    category = {'failed': 'product-error', 'interrupted': 'aborted'}.get(status, 'invalid-protocol')
                    raise Failure('turn', category)
                selected = final if final is not None else unphased
                if not selected or not selected.strip():
                    raise Failure('turn', 'empty-answer')
                return selected
            if params.get('turnId') != self.turn_id:
                continue
            item = obj(params.get('item'), method)
            if method == 'item/started':
                await self.report_item(item, False)
                continue
            await self.report_item(item, True)
            if item.get('type') in ('commandExecution', 'fileChange') and item.get('status') == 'declined':
                if item['type'] not in self.denials:
                    self.denials.append(item['type'])
            if item.get('type') != 'agentMessage':
                continue
            text = item.get('text')
            if not isinstance(text, str) or len(text) > 2_000_000:
                raise Failure('turn', 'invalid-answer')
            phase = item.get('phase')
            if phase == 'final_answer':
                final = text
            elif phase is None:
                unphased = text
            elif phase != 'commentary':
                raise Failure('turn', 'invalid-phase')

    async def report_item(self, item: dict, completed: bool) -> None:
        """Emit typed activity summaries without raw command/output or thinking text."""
        labels = {'commandExecution': '执行命令', 'fileChange': '修改文件',
                  'mcpToolCall': '调用 MCP 工具', 'dynamicToolCall': '调用工具',
                  'webSearch': '搜索网页'}
        kind = item.get('type')
        if kind not in labels:
            if kind == 'agentMessage' and completed and item.get('phase') == 'commentary':
                await self.progress('模型已更新任务进展')
            return
        state = '开始' if not completed else {
            'completed': '完成', 'failed': '失败', 'declined': '被拒绝',
            'inProgress': '进行中'}.get(item.get('status'), '结束')
        detail = ''
        if completed and kind == 'commandExecution' and type(item.get('exitCode')) is int:
            detail = f'，退出码 {item["exitCode"]}'
        await self.progress(f'{labels[kind]}：{state}{detail}')

    async def close(self) -> None:
        """Stop reading and settle pending requests during process teardown."""
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)
        for future in self.pending.values():
            if not future.done():
                future.cancel()
