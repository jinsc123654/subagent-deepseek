from dataclasses import replace
from pathlib import Path
import json,tempfile,unittest
from unittest.mock import patch
from subagent_deepseek.config import Settings
from subagent_deepseek.workflow import Board
from subagent_deepseek.workflow_tools import register_workflow_tools
from subagent_deepseek.dual_protocol import make_contract,build_handoff
from subagent_deepseek.coordination import Coordination,parse_report,verify_report
from subagent_deepseek.harness import HarnessBridge

class Server:
    def __init__(self):self.tools={}
    def tool(self,**kwargs):
        def add(fn):self.tools[fn.__name__]=fn;return fn
        return add

def node(id='worker1',depth=1,completed=True,running=False):
    return dict(id=id,parent_id='root',depth=depth,mode='continuable',completed=completed,running=running,label='analysis',seq=10)
def report(id='worker1',status='PASS'):
    return dict(memory='ANCHOR_memory',children=[dict(id=id,task='read-only check',status=status,evidence='fixture verified')],decisions=[],risks=[],next_steps=['review'])

class FakeBridge:
    def __init__(self):self.nodes=[node()];self.errors=[];self.report=report();self.prompts=[]
    async def start(self,prompt,*args):self.prompts.append(prompt);return {'session_id':'root'}
    async def send(self,sid,prompt,**kwargs):
        self.prompts.append(prompt)
        for n in self.nodes:n['seq']+=5
        return {'accepted':True}
    async def status(self,sid):return {'status':'idle'}
    async def result(self,*args,**kwargs):return dict(finished=True,completed=True,answer='<coordination_report>'+json.dumps(self.report)+'</coordination_report>')
    async def worker_tree(self,sid):return dict(root_id=sid,nodes=self.nodes,errors=self.errors,truncated=False)

class CoordinationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.settings=Settings('/bin/true',str(self.root/'key'),(str(self.root),),harness_url='http://127.0.0.1:3080')
        self.board=Board(self.settings,str(self.root));self.board.change('init')
        self.id=self.board.change('add',title='Three layers',description='Native worker check',acceptance='Verified child')['task_id']
        self.server=Server();self.bridge=FakeBridge()
        with patch('subagent_deepseek.workflow_tools.HarnessBridge',return_value=self.bridge):register_workflow_tools(self.server,self.settings,self.root/'settings.json',None)
    async def dispatch(self,**kwargs):return await self.server.tools['project_dispatch'](str(self.root),self.id,'Bounded read-only task',None,codex_permission='read-only',task_kind='investigate',**kwargs)
    async def collect(self):return await self.server.tools['project_collect'](str(self.root),self.id)
    async def test_native_success_and_memory_handoff(self):
        first=await self.dispatch(max_workers=1)
        self.assertEqual(first['role'],'coordinator')
        prompt=self.bridge.prompts[0];self.assertIn('必须实际调用',prompt);self.assertIn('第三层',prompt)
        result=await self.collect();self.assertEqual(result['state'],'review')
        data=await self.server.tools['project_coordination'](str(self.root),self.id)
        self.assertEqual(data['tasks'][self.id]['rounds']['1']['report']['memory'],'ANCHOR_memory')
        self.assertIn('worker1',Path(data['path']).read_text())
        await self.dispatch(max_workers=1)
        self.assertIn('ANCHOR_memory',self.bridge.prompts[-1])
        await self.collect();self.assertEqual(len(Coordination(self.settings,str(self.root)).read()['tasks'][self.id]['rounds']),2)
    async def test_prior_worker_success_without_new_activity_is_not_reused_evidence(self):
        await self.dispatch();await self.collect()
        async def unchanged_send(sid,prompt,**kwargs):return {'accepted':True}
        self.bridge.send=unchanged_send
        await self.dispatch()
        self.assertEqual((await self.collect())['state'],'blocked')
    async def test_worker_done_before_parent_summary_does_not_prematurely_block(self):
        await self.dispatch()
        original=self.bridge.result
        async def waiting(*args,**kwargs):return dict(finished=True,completed=True,answer='Waiting for child completion notice')
        self.bridge.result=waiting
        result=await self.collect();self.assertEqual(result['state'],'in_progress');self.assertFalse(result['result']['finished'])
        self.bridge.result=original
        self.assertEqual((await self.collect())['state'],'review')
    async def test_coordinator_never_silently_falls_back_to_direct(self):
        with self.assertRaises(Exception):await self.dispatch(backend='direct')
        self.assertEqual(self.board.read()['tasks'][self.id]['state'],'pending')
    async def test_prose_or_fake_id_is_not_native_evidence(self):
        await self.dispatch();self.bridge.nodes=[]
        self.assertEqual((await self.collect())['state'],'blocked')
    async def test_wrong_reported_worker_blocks(self):
        await self.dispatch();self.bridge.report=report('invented')
        self.assertEqual((await self.collect())['state'],'blocked')
    async def test_fourth_layer_detected(self):
        await self.dispatch();self.bridge.nodes.append(node('grandchild',depth=2))
        self.assertEqual((await self.collect())['state'],'blocked')
    async def test_active_worker_holds_review(self):
        await self.dispatch();self.bridge.nodes=[node(running=True,completed=False)]
        result=await self.collect();self.assertEqual(result['state'],'in_progress');self.assertFalse(result['result']['finished'])
    async def test_pass_requires_completed_worker(self):
        await self.dispatch();self.bridge.nodes=[node(completed=False)]
        self.assertEqual((await self.collect())['state'],'blocked')
    async def test_fail_report_saved_but_task_blocked(self):
        await self.dispatch();self.bridge.report=report(status='FAIL')
        self.assertEqual((await self.collect())['state'],'blocked')
        self.assertEqual(Coordination(self.settings,str(self.root)).read()['tasks'][self.id]['rounds']['1']['report']['children'][0]['status'],'FAIL')
    async def test_creation_budget_and_incomplete_observation(self):
        await self.dispatch(max_workers=1);self.bridge.nodes.append(node('worker2'))
        self.assertEqual((await self.collect())['state'],'blocked')
    async def test_existing_user_markdown_not_overwritten(self):
        path=self.root/'DEEPSEEK_COORDINATION.md';path.write_text('User notes')
        with self.assertRaises(Exception):await self.dispatch()
        self.assertEqual(path.read_text(),'User notes');self.assertEqual(self.bridge.prompts,[])
    def test_memory_symlink_rejected(self):
        target=self.root/'notes';target.write_text('original');(self.root/'DEEPSEEK_COORDINATION.md').symlink_to(target)
        with self.assertRaises(ValueError):Coordination(self.settings,str(self.root)).read()
        self.assertEqual(target.read_text(),'original')
    async def test_native_tree_observation_does_not_adopt_arbitrary_sessions(self):
        bridge=HarnessBridge(self.settings,self.root/'settings.json')
        bridge.store.put('root',cwd=str(self.root),origin=bridge.origin)
        calls=[]
        async def request(method,payload):
            self.assertEqual(method,'projections');sid=payload['sessionId'];calls.append(sid)
            values={'workingDirectory':str(self.root)}
            if sid=='root':values['subagentCatalog']=[{'id':'child','mode':'continuable','label':'w'}]
            else:values.update(subagentTiming={'lastTurnCompleted':True},subagentCatalog=[])
            return {'kind':'sequenced','asOfSeq':10,'values':values}
        bridge.request=request
        tree=await bridge.worker_tree('root')
        self.assertEqual(calls,['root','child']);self.assertEqual(tree['nodes'][0]['parent_id'],'root')
        with self.assertRaises(Exception):await bridge.worker_tree('unknown')
    def test_report_parser_rejects_duplicate_or_missing_memory(self):
        with self.assertRaises(ValueError):parse_report('plain success')
        data=report();data['children']*=2
        with self.assertRaises(ValueError):parse_report('<coordination_report>'+json.dumps(data)+'</coordination_report>')
