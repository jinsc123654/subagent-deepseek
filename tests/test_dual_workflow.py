from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from subagent_deepseek.config import Settings
from subagent_deepseek.workflow import Board
from subagent_deepseek.workflow_tools import register_workflow_tools, INSTRUCTIONS
from subagent_deepseek.dual_protocol import make_contract, build_handoff

class Server:
    def __init__(self):self.tools={}
    def tool(self,**kwargs):
        def add(fn):self.tools[fn.__name__]=fn;return fn
        return add

class Bridge:
    def __init__(self):self.started=[];self.sent=[];self.current='idle';self.pause=None
    async def start(self,prompt,cwd,title,permission):
        self.started.append(prompt);return {'session_id':'s'+str(len(self.started))}
    async def send(self,sid,prompt,**kwargs):self.sent.append((sid,prompt));return {'accepted':True}
    async def status(self,sid):return {'status':self.current}
    async def result(self,sid,**kwargs):
        if self.pause:await self.pause()
        return {'finished':True,'completed':True,'answer':'PASS: files checked; evidence fixture'}

class DualWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.settings=Settings('/bin/true',str(self.root/'key'),(str(self.root),),harness_url='http://127.0.0.1:3080')
        self.board=Board(self.settings,str(self.root));self.board.change('init')
        self.id=self.board.change('add',title='Bounded test',description='Read current output',acceptance='Checks and evidence')['task_id']
        self.bridge=Bridge();self.server=Server()
        with patch('subagent_deepseek.workflow_tools.HarnessBridge',return_value=self.bridge):register_workflow_tools(self.server,self.settings,self.root/'settings.json',None)
    async def dispatch(self,**kwargs):
        return await self.server.tools['project_dispatch'](str(self.root),self.id,'Only inspect named files; report checks',None,codex_permission='read-only',role='worker',**kwargs)
    def test_contract_validation_and_readonly_prompt(self):
        for kwargs in [dict(task_kind='evil'),dict(allowed_files='file.py'),dict(checks=[False]),dict(baseline={'sha':12})]:
            with self.assertRaises(ValueError):make_contract(**kwargs)
        contract=make_contract('test',['src/a.py'],['No code changes'],{'build':'0835'},['Check output'])
        prompt=build_handoff(self.board.read()['tasks'][self.id],str(self.root),'Read only',contract,2)
        for phrase in ['0835','src/a.py','Do not edit DEEPSEEK_PROJECT.md']:
            self.assertIn(phrase,prompt)
        self.assertIn('test-only: run tests',prompt)
        self.assertNotIn('Implement only task',prompt)
        self.assertIn('THREE layers',INSTRUCTIONS)
    async def test_same_session_next_revision_keeps_history_and_clears_evidence(self):
        first=await self.dispatch(task_kind='test',baseline={'build':'0835'},decision='Keep withdrawn UI change removed')
        self.assertEqual(first['revision'],1)
        await self.server.tools['project_collect'](str(self.root),self.id)
        self.board.change('update',self.id,evidence='First review needs another check',note='More checks')
        second=await self.dispatch(task_kind='test',baseline={'build':'0835'},checks=['Additional check'])
        self.assertEqual(second['revision'],2);self.assertTrue(second['result']['reused_session'])
        self.assertEqual(len(self.bridge.started),1);self.assertEqual(len(self.bridge.sent),1)
        self.assertIn('Keep withdrawn UI change removed',self.bridge.sent[0][1])
        task=self.board.read()['tasks'][self.id]
        self.assertEqual(task['evidence'],'');self.assertEqual(len(task['rounds']),2)
        self.assertIn('First review',task['rounds'][0]['evidence'])
        self.assertEqual(task['rounds'][1]['contract']['kind'],'test')
        self.assertIn('修订：2',self.board.path.read_text())
    async def test_busy_session_rejected_before_new_reservation(self):
        await self.dispatch();await self.server.tools['project_collect'](str(self.root),self.id)
        self.bridge.current='running'
        with self.assertRaises(Exception):await self.dispatch()
        task=self.board.read()['tasks'][self.id]
        self.assertEqual(task['revision'],1);self.assertFalse(task['uncertain']);self.assertEqual(task['state'],'review')
    async def test_isolated_round_when_requested(self):
        await self.dispatch();await self.server.tools['project_collect'](str(self.root),self.id)
        result=await self.dispatch(reuse_session=False)
        self.assertFalse(result['result']['reused_session']);self.assertEqual(len(self.bridge.started),2)
    async def test_stale_result_same_session_cannot_overwrite_new_revision(self):
        await self.dispatch()
        async def advance():
            self.board.execution(self.id,state='review')
            self.board.reserve(self.id,'harness',make_contract('test'))
            self.board.execution(self.id,state='in_progress',uncertain=False)
        self.bridge.pause=advance
        await self.server.tools['project_collect'](str(self.root),self.id)
        task=self.board.read()['tasks'][self.id]
        self.assertEqual(task['revision'],2);self.assertEqual(task['state'],'in_progress');self.assertEqual(task['result_summary'],'')
    async def test_invalid_contract_never_changes_board_or_submits(self):
        with self.assertRaises(Exception):await self.dispatch(checks=[False])
        self.assertEqual(self.board.read()['tasks'][self.id]['state'],'pending');self.assertEqual(self.bridge.started,[])
