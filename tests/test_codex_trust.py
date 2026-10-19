from dataclasses import replace
from pathlib import Path
import json, os, tempfile, unittest
from unittest.mock import patch
from subagent_deepseek.config import Settings
from subagent_deepseek.workspaces import project_trust, WorkspaceAccessError
from subagent_deepseek.workflow_tools import register_workflow_tools
from test_coordination import Server

class TrustTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()
  self.config=self.root/'config.toml'
  self.settings=Settings('/bin/true',str(self.root/'key'),(),workspace_policy='codex-trust',codex_config_file=str(self.config))
 def write(self,entries):
  self.config.write_text(''.join('[projects.'+json.dumps(str(p))+']\ntrust_level = '+json.dumps(level)+'\n' for p,level in entries))
  self.config.chmod(0o600)
 def test_trusted_project_and_child_no_sibling(self):
  self.write([(self.project,'trusted')]);child=self.project/'src';child.mkdir();other=self.root/'project-other';other.mkdir()
  self.assertEqual(self.settings.directory(str(child)),child)
  with self.assertRaises(WorkspaceAccessError):self.settings.directory(str(other))
 def test_revocation_dynamic_and_manual_root_denied(self):
  s=replace(self.settings,allowed_roots=(str(self.project),))
  self.write([(self.project,'trusted')]);s.directory(str(self.project))
  self.write([(self.project,'untrusted')])
  with self.assertRaisesRegex(WorkspaceAccessError,'untrusted'):s.directory(str(self.project))
  self.write([])
  with self.assertRaises(WorkspaceAccessError):self.settings.directory(str(self.project))
 def test_nearest_child_trust_overrides(self):
  child=self.project/'nested';child.mkdir();deep=child/'trusted';deep.mkdir()
  self.write([(self.project,'trusted'),(child,'untrusted'),(deep,'trusted')])
  self.assertEqual(self.settings.directory(str(deep)),deep)
  with self.assertRaises(WorkspaceAccessError):self.settings.directory(str(child))
 def test_missing_malformed_and_unsafe_source_fail_closed(self):
  self.assertIsNone(project_trust(self.project,self.config))
  for content in ['[broken','projects = "bad"','bad = "\ufffd"\n[projects]:']:
   self.config.write_text(content)
   with self.assertRaises(WorkspaceAccessError):self.settings.directory(str(self.project))
  self.write([(self.project,'trusted')]);self.config.chmod(0o666)
  with self.assertRaises(WorkspaceAccessError):self.settings.directory(str(self.project))
 def test_readable_regular_source_and_bad_source_no_manual_fallback(self):
  self.write([(self.project,'trusted')]);self.config.chmod(0o644)
  self.settings.directory(str(self.project))
  self.config.write_text('[broken')
  with self.assertRaises(WorkspaceAccessError):replace(self.settings,allowed_roots=(str(self.project),)).directory(str(self.project))
 def test_symlink_config_and_project_escape(self):
  self.write([(self.project,'trusted')]);other=self.root/'outside';other.mkdir();(self.project/'escape').symlink_to(other)
  with self.assertRaises(WorkspaceAccessError):self.settings.directory(str(self.project/'escape'))
  alias=self.root/'alias';alias.symlink_to(other);self.write([(alias,'trusted')])
  with self.assertRaises(WorkspaceAccessError):self.settings.directory(str(alias))
  alt=self.root/'real.toml';self.config.rename(alt);self.config.symlink_to(alt)
  with self.assertRaises(WorkspaceAccessError):self.settings.directory(str(self.project))
 def test_home_and_root_entries_never_grant(self):
  self.write([(Path('/'),'trusted'),(Path.home(),'trusted')])
  with self.assertRaises(WorkspaceAccessError):self.settings.directory(str(self.project))
 def test_legacy_allowlist_ignores_trust_and_no_config_write(self):
  self.write([(self.project,'untrusted')]);before=self.config.read_bytes()
  replace(self.settings,workspace_policy='allowlist',allowed_roots=(str(self.project),)).directory(str(self.project))
  self.assertEqual(self.config.read_bytes(),before)
 def test_loader_pins_codex_home_and_allows_empty_manual_roots(self):
  key=self.root/'key';key.write_text('test');settings=self.root/'settings.json'
  settings.write_text(json.dumps(dict(codex_binary='/bin/true',credential_file=str(key),workspace_policy='codex-trust',allowed_roots=[])))
  with patch.dict(os.environ,{'CODEX_HOME':str(self.root)}):loaded=Settings.load(settings)
  self.assertEqual(loaded.codex_config_file,str(self.config))

class BoardTrustTests(unittest.IsolatedAsyncioTestCase):
 async def test_read_allowed_and_reason_explained_without_mutation(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);project=root/'project';project.mkdir();cfg=root/'codex.toml'
   cfg.write_text('[projects.'+json.dumps(str(project))+']\ntrust_level="trusted"\n')
   cfg.chmod(0o600)
   settings=Settings('/bin/true',str(root/'key'),(),workspace_policy='codex-trust',codex_config_file=str(cfg))
   server=Server();register_workflow_tools(server,settings,root/'settings.json',None)
   result=await server.tools['project_board'](str(project),'read')
   self.assertEqual(result['tasks'],{});self.assertFalse((project/'DEEPSEEK_PROJECT.md').exists());self.assertFalse((project/'.deepseek-project.lock').exists());self.assertFalse(result['initialized'])
   cfg.write_text('[projects.'+json.dumps(str(project))+']\ntrust_level="untrusted"\n')
   with self.assertRaisesRegex(Exception,'untrusted'):await server.tools['project_board'](str(project),'read')
