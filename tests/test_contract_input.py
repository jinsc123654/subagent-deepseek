import unittest
from pydantic import ValidationError
from subagent_deepseek.execution_plan import ValidationPlan
import test_coordination

class ContractInputTests(unittest.TestCase):
 def test_resources_and_unknown_fields_rejected_before_submission(self):
  for data in [{'resources':'source files only'},{'resources':[1]},{'budget_minutes':True},{'unknown':True}]:
   with self.assertRaises(ValidationError):ValidationPlan.model_validate(data)
  self.assertEqual(ValidationPlan(resources=['source files only']).resources,['source files only'])

class ContractRoundtripTests(unittest.IsolatedAsyncioTestCase):
 setUp=test_coordination.CoordinationTests.setUp
 dispatch=test_coordination.CoordinationTests.dispatch
 collect=test_coordination.CoordinationTests.collect
 async def test_typed_model_reaches_contract_and_collects(self):
  r=await self.dispatch(validation=ValidationPlan(mode='none',resources=['source files only']))
  self.assertEqual(r['contract']['validation']['resources'],['source files only'])
  self.bridge.report['checks']=[dict(name='runtime tests',status='NOT_RUN',evidence='not selected')]
  self.assertEqual((await self.collect())['state'],'review')
 async def test_direct_error_identifies_field_and_does_not_reserve(self):
  with self.assertRaisesRegex(Exception,'validation.resources'):
   await self.dispatch(validation={'resources':'source files only'})
  task=self.board.read()['tasks'][self.id]
  self.assertEqual(task['state'],'pending');self.assertNotIn('revision',task);self.assertEqual(self.bridge.prompts,[])
