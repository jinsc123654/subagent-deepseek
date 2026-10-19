import unittest,tempfile
from pathlib import Path
from unittest.mock import patch
from subagent_deepseek.config import Settings
from subagent_deepseek.workflow import Board
from subagent_deepseek.intake import intake
class IntakeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.settings=Settings('/bin/true',str(self.root/'key'),(str(self.root),))
  b=Board(self.settings,str(self.root));b.change('init');self.id=b.change('add',title='mode',description='mode',acceptance='mode')['task_id']
 def call(self,action='read',mode=''):return intake(self.settings,str(self.root),self.id,action,mode)
 def test_deadline_default_and_restart(self):
  self.assertEqual(self.call()['state'],'not_started')
  with patch('subagent_deepseek.intake.time.time',return_value=100):self.assertEqual(self.call('start')['remaining_seconds'],15)
  with patch('subagent_deepseek.intake.time.time',return_value=110):self.assertEqual(self.call('start')['deadline'],115)
  with patch('subagent_deepseek.intake.time.time',return_value=115):
   r=self.call();self.assertEqual((r['mode'],r['source']),('standard','timeout'))
  self.assertEqual(Board(self.settings,str(self.root)).read()['tasks'][self.id]['state'],'pending')
 def test_explicit_and_late_choice_wins(self):
  with patch('subagent_deepseek.intake.time.time',return_value=100):self.call('start')
  with patch('subagent_deepseek.intake.time.time',return_value=120):
   self.assertEqual(self.call()['mode'],'standard');self.assertEqual(self.call('select','investigate')['mode'],'investigate')
 def test_invalid_and_symlink_preserved(self):
  with self.assertRaises(ValueError):self.call('select','oops')
  p=self.root/'.deepseek-intake.json';target=self.root/'private';target.write_text('keep');p.symlink_to(target)
  with self.assertRaises(ValueError):self.call('start')
  self.assertEqual(target.read_text(),'keep')
