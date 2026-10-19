import tempfile, unittest
from pathlib import Path
from subagent_deepseek.execution_plan import validation_plan, evidence_root, evidence_directory, verify_checks
from subagent_deepseek.dual_protocol import make_contract,build_handoff
import test_coordination
import json
from subagent_deepseek.coordination import parse_report

class PlanTests(unittest.TestCase):
 def test_paths_and_symlink_escape(self):
  for value in ['/tmp/log','../log','docs/../../log','a\\b','a\nlog','C:/log','']:
   with self.assertRaises(ValueError):evidence_root(value)
  with tempfile.TemporaryDirectory() as d:
   (Path(d)/'link').symlink_to('/tmp')
   with self.assertRaises(ValueError):evidence_directory(d,'link','T-1',1)
 def test_plan_invalid_and_skip_results(self):
  for value in [{'mode':'oops'},{'mode':'none','budget_minutes':2},{'budget_minutes':True},{'resources':'board'},{'unknown':1}]:
   with self.assertRaises(ValueError):validation_plan(value)
  for mode in ['none','method-only']:
   plan=validation_plan({'mode':mode})
   with self.assertRaises(ValueError):verify_checks({'checks':[dict(name='test',status='PASS',evidence='method')]},plan)
   verify_checks({'checks':[dict(name='test',status='NOT_RUN',evidence='method only')]},plan)
 def test_compact_collection_retains_ledger_details(self):
  from subagent_deepseek.workflow_tools import _report_view, _result_view
  report=dict(memory="m"*4000,children=[dict(id="real",status="PASS",evidence="e"*4000)])
  view=_report_view(report,False)
  self.assertEqual(len(view["memory"]),2000);self.assertNotIn("evidence",view["children"][0])
  self.assertNotIn("answer",_result_view(dict(answer="huge",completed=True),False))
  self.assertIs(_report_view(report,True),report)
 def test_report_unique_and_pass_evidence(self):
  r=dict(memory="summary",children=[dict(id="real",task="analysis",status="PASS",evidence="")])
  with self.assertRaises(ValueError):parse_report("<coordination_report>"+json.dumps(r)+"</coordination_report>")
  r["children"][0]["evidence"]="actual output"
  block="<coordination_report>"+json.dumps(r)+"</coordination_report>"
  with self.assertRaises(ValueError):parse_report(block+block)
 def test_prompt_contract_roundtrip(self):
  c=make_contract(validation={'mode':'targeted','timing':'final','budget_minutes':15,'resources':['board A']})
  p=build_handoff(dict(id='T-1',title='x',description='x',acceptance='x'),'/tmp','x',c,3)
  for value in ['targeted','board A','/tmp/docs/agent/evidence/T-1/R3','NOT_RUN']:self.assertIn(value,p)

class PlanIntegrationTests(unittest.IsolatedAsyncioTestCase):
 setUp=test_coordination.CoordinationTests.setUp
 dispatch=test_coordination.CoordinationTests.dispatch
 collect=test_coordination.CoordinationTests.collect
 async def test_explicit_validation_persisted_and_checked(self):
  await self.dispatch(validation={'mode':'none'})
  self.assertEqual(self.board.read()['tasks'][self.id]['contract']['validation']['mode'],'none')
  self.bridge.report['checks']=[dict(name='test',status='NOT_RUN',evidence='not selected')]
  self.assertEqual((await self.collect())['state'],'review')
 async def test_missing_explicit_check_blocks(self):
  await self.dispatch(validation={'mode':'smoke'})
  self.assertEqual((await self.collect())['state'],'blocked')
 async def test_fake_test_success_when_skipped_blocks(self):
  await self.dispatch(validation={'mode':'method-only'})
  self.bridge.report['checks']=[dict(name='test',status='PASS',evidence='method recorded')]
  self.assertEqual((await self.collect())['state'],'blocked')
