"""Real MCP SDK client/server round trip through subprocess stdio."""
import asyncio
from dataclasses import asdict
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from subagent_deepseek.config import Settings


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_discovery_call_and_validation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            key = root / 'key'
            key.write_text('sk-fake-test-key')
            key.chmod(0o600)
            fixture = Path(__file__).parent / 'fake_app_server.py'
            settings = Settings(str(fixture.resolve()), str(key), (str(root),),
                                timeout_seconds=2, shutdown_grace_seconds=0.01)
            path = root / 'settings.json'
            path.write_text(json.dumps(asdict(settings)))
            parameters = StdioServerParameters(command=sys.executable,
                args=['-m', 'subagent_deepseek.server', '--settings', str(path)],
                env={**os.environ, 'TEST_SCENARIO': 'progress'})
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as client:
                    initialized = await client.initialize()
                    self.assertIn('main agent owns planning', initialized.instructions)
                    tools = await client.list_tools()
                    self.assertEqual([t.name for t in tools.tools], ['subagent_deepseek', 'subagent_deepseek_status', 'project_intake', 'project_board', 'project_dispatch', 'project_collect', 'project_coordination'])
                    initialized_board = await client.call_tool('project_board', {'cwd': str(root), 'action': 'init'})
                    self.assertFalse(initialized_board.isError)
                    added = await client.call_tool('project_board', {'cwd': str(root), 'action': 'add',
                        'title': 'Bounded fix', 'description': 'Implement fixture task', 'acceptance': 'Fixture check'})
                    task_id = added.structuredContent['task_id']
                    dispatch_schema = next(t.inputSchema for t in tools.tools if t.name == 'project_dispatch')
                    self.assertEqual(dispatch_schema['$defs']['ValidationPlan']['properties']['resources']['type'], 'array')
                    self.assertIn('workspace-write', dispatch_schema['properties']['codex_permission']['enum'])
                    for bad, field in [({'validation': {'mode': 'none', 'resources': 'source files only'}}, 'resources'),
                                       ({'codex_permission': 'workspace-write; read filesystem excluding credentials'}, 'codex_permission')]:
                        rejected = await client.call_tool('project_dispatch', dict(cwd=str(root), task_id=task_id,
                            brief='Only fixture', backend='direct', role='worker', **bad))
                        self.assertTrue(rejected.isError)
                        self.assertIn(field, str(rejected.content))
                    unchanged = await client.call_tool('project_board', {'cwd':str(root), 'action':'read'})
                    self.assertEqual(unchanged.structuredContent['tasks'][task_id]['state'],'pending')
                    dispatched = await client.call_tool('project_dispatch', {'cwd': str(root), 'task_id': task_id,
                        'brief': 'Modify only allowed fixture; run checks', 'backend': 'direct', 'role': 'worker'})
                    self.assertFalse(dispatched.isError)
                    self.assertEqual(dispatched.structuredContent['state'], 'review')
                    rejected = await client.call_tool('project_board', {'cwd': str(root), 'action': 'update',
                        'task_id': task_id, 'state': 'done'})
                    self.assertTrue(rejected.isError)
                    accepted = await client.call_tool('project_board', {'cwd': str(root), 'action': 'update',
                        'task_id': task_id, 'state': 'done', 'evidence': 'Main-agent fixture review passed'})
                    self.assertEqual(accepted.structuredContent['completed'], 1)
                    schema = tools.tools[0].inputSchema
                    self.assertEqual(set(schema['properties']), {'task', 'cwd'})
                    messages = []
                    command_started = asyncio.Event()
                    async def progress(value, total, message):
                        messages.append((value, total, message))
                        if message == '执行命令：开始':
                            command_started.set()
                    running = asyncio.create_task(client.call_tool('subagent_deepseek',
                        {'task': 'A task', 'cwd': str(root)}, progress_callback=progress))
                    await asyncio.wait_for(command_started.wait(), 3)
                    self.assertFalse(running.done(), 'Progress must arrive before final result')
                    status = await client.call_tool('subagent_deepseek_status', {})
                    current = next(run for run in status.structuredContent['runs'] if run['status'] == 'running')
                    self.assertEqual(current['status'], 'running')
                    live = Path(current['activity_log']).read_text()
                    self.assertIn('执行命令：开始', live)
                    self.assertNotIn('sk-fixture-secret', live)
                    result = await running
                    self.assertEqual([m[0] for m in messages], sorted(set(m[0] for m in messages)))
                    self.assertTrue(all(m[1] is None for m in messages))
                    self.assertIn('执行命令：完成，退出码 0', [m[2] for m in messages])
                    self.assertNotIn('sk-fixture-secret', str(messages))
                    run_id = result.structuredContent['run_id']
                    status = await client.call_tool('subagent_deepseek_status', {'run_id': run_id})
                    self.assertEqual(status.structuredContent['status'], 'completed')
                    self.assertFalse(result.isError)
                    self.assertEqual(result.structuredContent['answer'], 'final answer')
                    invalid = await client.call_tool('subagent_deepseek', {'task': 'A task', 'cwd': '/outside'})
                    self.assertTrue(invalid.isError)
                    invalid_task = await client.call_tool('subagent_deepseek', {'task': ' ', 'cwd': str(root)})
                    self.assertTrue(invalid_task.isError)


if __name__ == '__main__':
    unittest.main()
