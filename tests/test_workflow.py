import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from subagent_deepseek.config import Settings
from subagent_deepseek.workflow import Board, install_policy, BEGIN
from subagent_deepseek.workflow_tools import register_workflow_tools


class FakeServer:
    def __init__(self): self.tools = {}
    def tool(self, **kwargs):
        def register(fn): self.tools[fn.__name__] = fn; return fn
        return register


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings('/bin/true', str(self.root/'key'), (str(self.root),))
        self.board = Board(self.settings, str(self.root)); self.board.change('init')
        self.task_id = self.board.change('add', title='Fix', description='A bounded bug', acceptance='Test passes', kind='bug')['task_id']

    def test_review_gate_and_progress(self):
        with self.assertRaises(ValueError): self.board.change('update', self.task_id, state='done')
        self.board.change('update', self.task_id, state='review', progress=100)
        self.assertEqual(self.board.change('read')['completed'], 0)
        self.board.change('update', self.task_id, state='done', evidence='Main reviewed diff and ran regression')
        self.assertEqual(self.board.change('read')['completed'], 1)
        self.assertIn('验收完成度：1/1（100%）',self.board.path.read_text())

    def test_idempotent_init_and_instruction_preservation(self):
        agents=self.root/'AGENTS.md'; agents.write_text('Keep my rules\n')
        install_policy(agents); first=agents.read_text(); install_policy(agents)
        self.assertEqual(first, agents.read_text()); self.assertEqual(first.count(BEGIN),1)
        self.assertTrue(first.startswith('Keep my rules'))
        self.board.change('init'); self.assertIn(self.task_id,self.board.read()['tasks'])
        self.assertEqual(len(list(self.root.glob('AGENTS.md.before-*.bak'))),1)

    def test_no_symlink_escape_and_corrupt_board_preserved(self):
        other=self.root/'other'; other.mkdir()
        out=other/'board'; out.write_text('original')
        self.board.path.unlink(); self.board.path.symlink_to(out)
        with self.assertRaises(ValueError):self.board.change('read')
        self.assertEqual(out.read_text(),'original')
        self.board.path.unlink();self.board.path.write_text('handwritten plan')
        with self.assertRaises(ValueError):self.board.change('init')
        self.assertEqual(self.board.path.read_text(),'handwritten plan')

    def test_allowlist_and_duplicate_dispatch(self):
        with self.assertRaises(ValueError): Board(self.settings,'/')
        self.board.reserve(self.task_id,'direct')
        with self.assertRaises(ValueError):self.board.reserve(self.task_id,'direct')
        self.board.resolve_uncertain(self.task_id,'Verified no live submission after interruption')
        self.board.reserve(self.task_id,'direct')

    async def test_direct_dispatch_awaits_review(self):
        server=FakeServer()
        async def direct(**kwargs):
            self.assertIn('Do not edit DEEPSEEK_PROJECT.md', kwargs['task'])
            return {'status':'completed','answer':'Tests passed','run_id':'test'}
        register_workflow_tools(server,self.settings,self.root/'settings.json',direct)
        receipt=await server.tools['project_dispatch'](str(self.root),self.task_id,'Only edit src/fix.py; run regression',None,role='worker')
        self.assertEqual(receipt['state'],'review')
        self.assertEqual(self.board.read()['tasks'][self.task_id]['state'],'review')
        self.assertEqual(self.board.change('read')['completed'],0)

    async def test_failure_blocks_retry_without_inspection(self):
        server=FakeServer()
        async def direct(**kwargs):raise RuntimeError('private failure')
        register_workflow_tools(server,self.settings,self.root/'settings.json',direct)
        with self.assertRaises(Exception) as error:
            await server.tools['project_dispatch'](str(self.root),self.task_id,'Implement fix',None,role='worker')
        self.assertNotIn('private failure',str(error.exception))
        task=self.board.read()['tasks'][self.task_id]
        self.assertEqual(task['state'],'blocked');self.assertTrue(task['uncertain'])
        with self.assertRaises(ValueError):self.board.reserve(self.task_id,'direct')

    async def test_harness_submission_collect_and_review(self):
        class FakeBridge:
            async def start(self,*args): return {'session_id':'session1'}
            async def result(self,*args,**kwargs):return {'finished':True,'completed':True,'answer':'Implemented'}
        settings=replace(self.settings,harness_url='http://127.0.0.1:3080')
        server=FakeServer()
        with patch('subagent_deepseek.workflow_tools.HarnessBridge',return_value=FakeBridge()):
            register_workflow_tools(server,settings,self.root/'settings.json',None)
        with self.assertRaises(Exception):
            await server.tools['project_dispatch'](str(self.root),self.task_id,'Fix',None,role='worker')
        self.assertEqual(self.board.read()['tasks'][self.task_id]['state'],'pending')
        result=await server.tools['project_dispatch'](str(self.root),self.task_id,'Fix',None,role='worker',codex_permission='read-only')
        self.assertEqual(result['state'],'in_progress')
        collected=await server.tools['project_collect'](str(self.root),self.task_id)
        self.assertEqual(collected['state'],'review')
        self.board.change('update',self.task_id,state='done',evidence='reviewed')
        await server.tools['project_collect'](str(self.root),self.task_id)
        self.assertEqual(self.board.change('read')['completed'],1)

    def test_late_execution_does_not_overwrite_accepted_task(self):
        self.board.change('update',self.task_id,state='done',evidence='Reviewed')
        self.board.execution(self.task_id,state='review')
        self.assertEqual(self.board.read()['tasks'][self.task_id]['state'],'done')

    async def test_cancellation_records_uncertainty(self):
        server=FakeServer()
        async def direct(**kwargs):raise asyncio.CancelledError()
        register_workflow_tools(server,self.settings,self.root/'settings.json',direct)
        with self.assertRaises(asyncio.CancelledError):
            await server.tools['project_dispatch'](str(self.root),self.task_id,'Fix',None,role='worker')
        self.assertTrue(self.board.read()['tasks'][self.task_id]['uncertain'])
