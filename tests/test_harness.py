"""Public Harness API bridge behavior without real accounts or model requests."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import httpx

from subagent_deepseek.config import Settings
from subagent_deepseek.harness import HarnessBridge, HarnessError


class FakeHost:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.prompts = 0
        self.auths = 0
        self.refuse_permission = False
        self.broken = False
        self.expire_once = False
        self.stop_reason = 'completed'
        self.running = False

    async def __call__(self, request):
        if request.url.path == '/connect':
            self.auths += 1
            assert request.url.params['token'] == 'private-launch-token'
            return httpx.Response(302, headers={'set-cookie': 'dsh_api=private-cookie; Path=/; HttpOnly', 'location': './api/remote.mux'})
        if self.expire_once:
            self.expire_once = False
            return httpx.Response(401)
        if self.broken:
            raise httpx.ConnectError('private details must not leak', request=request)
        data = json.loads(request.content)
        endpoint = data['method']
        assert request.url.path == '/api/' + endpoint
        args = data['payload']['args']
        self.calls.append((endpoint, args))
        r = args.get('request', {})
        sid = r.get('sessionId', args.get('agentId'))
        value = None
        if endpoint == 'session/create':
            self.rows[sid] = {'seq': 1, 'cwd': r['cwd'], 'title': '', 'permission': 'workspace-write', 'selection': {}, 'events': [], 'questions': []}
            value = {'sessionId': sid}
        elif endpoint == 'session/rename':
            self.rows[sid]['title'] = r['title']; value = {'title': r['title'], 'seq': 1}
        elif endpoint == 'session/selectModel':
            self.rows[sid]['selection'] = {k: r[k] for k in ('provider', 'model', 'reasoningEffort')}
            value = {'selected': self.rows[sid]['selection']}
        elif endpoint == 'commands/execute':
            if not self.refuse_permission:
                self.rows[sid]['permission'] = args['line'].split()[-1]
            value = {'kind': 'success'}
        elif endpoint == 'session/projections':
            row = self.rows[sid]
            value = {'kind': 'sequenced', 'asOfSeq': row['seq'], 'values': {
                'workingDirectory': row['cwd'], 'title': row['title'], 'permissions': {'currentValue': row['permission']},
                'modelSelection': {'next': row['selection']}, 'userQuestions': {'active': row['questions']},
                'inbox': {'next-turn': [], 'next-step': []}, 'tokenUsage': {'outputTokens': 5}}}
        elif endpoint == 'session/prompt':
            self.prompts += 1
            row = self.rows[sid];seq = row['seq'] + 1
            row['seq'] += 3
            row['events'] += [
                {'type': 'assistant/message', 'seq': seq, 'data': {'turn': self.prompts, 'message': {
                    'content': [{'type': 'reasoning', 'text': 'PRIVATE_REASONING'}, {'type': 'text', 'text': r['content'][0]['text']}],
                    'source': row['selection']}}},
                {'type': 'turn/end', 'seq': seq + 1, 'data': {'turn': self.prompts, 'reason': {'kind': self.stop_reason}}}]
            value = {'accepted': True}
        elif endpoint == 'session/list':
            assert args == {'_request': {}}
            value = {'items': [{'sessionId': sid, 'running': self.running} for sid in self.rows]}
        elif endpoint == 'session/page':
            sid = r['address']['sessionId']
            value = {'records': [{'type': 'event', 'event': x} for x in self.rows[sid]['events']], 'hasMore': False}
        elif endpoint == 'session/cancel':
            value = {'accepted': True}
        elif endpoint == 'userQuestions/answer':
            value = True
        else:
            raise AssertionError(endpoint)
        return httpx.Response(200, json={'type': 'server-response', 'rpcId': data['rpcId'], 'result': {'ok': True, 'value': value}})


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / 'web.log'
        self.log.write_text('Open http://127.0.0.1:3080/?token=private-launch-token\n')
        self.settings = Settings('/bin/true', str(self.log), (str(self.root),),
            harness_url='http://127.0.0.1:3080', harness_launch_log=str(self.log))
        self.host = FakeHost()
        self.bridge = HarnessBridge(self.settings, self.root / 'settings.json', httpx.MockTransport(self.host))

    async def start(self, text='hello'):
        return (await self.bridge.start(text, str(self.root), 'Codex test', 'read-only'))['session_id']

    async def test_creates_web_session_and_pins_readonly_before_prompt(self):
        sid = await self.start()
        self.assertEqual(self.host.rows[sid]['permission'], 'read-only')
        calls = [x[0] for x in self.host.calls]
        self.assertLess(calls.index('commands/execute'), calls.index('session/prompt'))
        self.assertEqual(self.host.auths, 1)
        self.assertEqual(self.bridge.cookie_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('private-launch-token', self.bridge.cookie_path.read_text())

    async def test_status_is_compact_and_result_excludes_reasoning(self):
        sid = await self.start('answer')
        status = await self.bridge.status(sid)
        self.assertEqual(status['status'], 'idle')
        self.assertNotIn('events', status)
        result = await self.bridge.result(sid)
        self.assertTrue(result['completed'])
        self.assertEqual(result['answer'], 'answer')
        self.assertNotIn('PRIVATE_REASONING', str(result))

    async def test_followup_reuses_session_and_latest_submission_baseline(self):
        sid = await self.start('first')
        await self.bridge.send(sid, 'second', steer=True, codex_permission='read-only')
        result = await self.bridge.result(sid)
        self.assertEqual(result['answer'], 'second')
        prompts = [a for e, a in self.host.calls if e == 'session/prompt']
        self.assertEqual(prompts[-1]['request']['mode'], 'steer')
        self.assertNotEqual(prompts[0]['request']['requestId'], prompts[-1]['request']['requestId'])

    async def test_failed_turn_never_returns_partial_answer_as_success(self):
        self.host.stop_reason = 'error'
        sid = await self.start('partial')
        result = await self.bridge.result(sid)
        self.assertTrue(result['finished'])
        self.assertFalse(result['completed'])
        self.assertEqual(result['stop_reason'], 'error')
        self.assertNotIn('answer', result)

    async def test_running_result_and_explicit_cancellation(self):
        sid = await self.start()
        self.host.running = True
        self.assertFalse((await self.bridge.result(sid))['finished'])
        self.assertTrue((await self.bridge.cancel(sid))['accepted'])

    async def test_answer_truncation_is_explicit(self):
        sid = await self.start('abcdef')
        result = await self.bridge.result(sid, max_chars=3)
        self.assertEqual(result['answer'], 'abc')
        self.assertTrue(result['answer_truncated'])

    async def test_rejects_unowned_session_before_network(self):
        with self.assertRaises(HarnessError):
            await self.bridge.cancel(str(uuid4()))
        self.assertEqual(self.host.calls, [])

    async def test_permission_failure_never_sends_prompt(self):
        self.host.refuse_permission = True
        with self.assertRaisesRegex(HarnessError, 'permission preset'):
            await self.start()
        self.assertEqual(self.host.prompts, 0)

    async def test_permission_is_resynchronized_to_current_parent_before_send(self):
        sid = await self.start()
        self.host.rows[sid]['permission'] = 'danger-full-access'
        await self.bridge.send(sid, 'second', codex_permission='read-only')
        self.assertEqual(self.host.prompts, 2)
        self.assertEqual(self.host.rows[sid]['permission'], 'read-only')

    async def test_401_refreshes_auth_without_duplicate_prompt(self):
        sid = await self.start()
        self.host.expire_once = True
        await self.bridge.send(sid, 'second', codex_permission='read-only')
        self.assertEqual(self.host.prompts, 2)
        self.assertEqual(self.host.auths, 2)

    async def test_transport_failure_is_not_retried(self):
        sid = await self.start()
        self.host.broken = True
        with self.assertRaisesRegex(HarnessError, 'outcome may be unknown'):
            await self.bridge.send(sid, 'second', codex_permission='read-only')
        self.assertEqual(self.host.prompts, 1)

    async def test_only_continued_questions_are_answerable(self):
        sid = await self.start()
        question = {'callId': 'q1', 'state': 'continued', 'questions': [{'id': 'one', 'question': 'Continue?'}]}
        self.host.rows[sid]['questions'] = [question]
        self.assertEqual((await self.bridge.status(sid))['status'], 'needs-input')
        self.assertTrue((await self.bridge.answer(sid, 'q1', [{'id': 'one', 'selected': ['Yes']}], 'read-only'))['accepted'])
        question['state'] = 'open'
        with self.assertRaises(HarnessError):
            await self.bridge.answer(sid, 'q1', [{'id': 'one', 'selected': ['Yes']}], 'read-only')

    async def test_enabled_mcp_registers_web_tools_without_network(self):
        from subagent_deepseek.server import create_server
        server = create_server(self.settings, self.root / 'settings.json')
        tools = await server.list_tools()
        names = [t.name for t in tools]
        self.assertEqual(len(names), 14)
        self.assertIn('harness_start', names)
        self.assertIn('harness_result', names)
        self.assertIn('harness_answer', names)
        start = next(t for t in tools if t.name == 'harness_start')
        self.assertEqual(set(start.inputSchema['properties']), {'task', 'cwd', 'codex_permission', 'title'})

    async def test_http_logging_never_prints_launch_token(self):
        import io, logging
        from subagent_deepseek.server import create_server
        create_server(self.settings, self.root / 'settings.json')
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            await self.start()
        finally:
            root.removeHandler(handler)
        self.assertNotIn('private-launch-token', output.getvalue())
        self.assertNotIn('private-cookie', output.getvalue())

    async def test_owned_receipts_survive_new_bridge_instance(self):
        sid = await self.start()
        reopened = HarnessBridge(self.settings, self.root / 'settings.json', httpx.MockTransport(self.host))
        self.assertEqual(reopened.list_sessions()['sessions'][0]['session_id'], sid)
        self.assertEqual((await reopened.status(sid))['status'], 'idle')

    async def test_auto_approval_and_full_access_map_to_full_harness_access(self):
        for permission in ('approve-for-me', 'full-access'):
            created = await self.bridge.start('hello', str(self.root), '', permission)
            self.assertEqual(created['permission'], 'danger-full-access')
            self.assertTrue(created['title'].startswith('[Codex] '))
            self.assertEqual(self.host.rows[created['session_id']]['permission'], 'danger-full-access')

    async def test_permission_downgrade_is_applied_before_next_prompt(self):
        created = await self.bridge.start('hello', str(self.root), '', 'approve-for-me')
        sid = created['session_id']
        await self.bridge.send(sid, 'next', codex_permission='workspace-write')
        self.assertEqual(self.host.rows[sid]['permission'], 'workspace-write')
        await self.bridge.sync_permissions(sid, 'read-only')
        self.assertEqual(self.host.rows[sid]['permission'], 'read-only')

    async def test_missing_permission_refuses_before_session_creation(self):
        with self.assertRaises(HarnessError):
            await self.bridge.start('hello', str(self.root))
        self.assertEqual(self.host.rows, {})

    def test_rejects_nonlocal_endpoint_and_credentials_in_url(self):
        from dataclasses import asdict
        path = self.root / 'settings.json'
        for url in ['http://example.com', 'http://user:secret@127.0.0.1:3080', 'http://127.0.0.1:3080/?token=secret']:
            data = asdict(replace(self.settings, harness_url=url))
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                Settings.load(path)
