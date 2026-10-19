from pathlib import Path
import json, tempfile, time, unittest
from unittest.mock import patch
from subagent_deepseek.config import Settings
from subagent_deepseek.workspaces import record, roots, install

class WorkspacesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()
        self.registry=self.root/'scopes.json'
        self.settings=Settings('/bin/true',str(self.root/'key'),(),workspace_policy='codex-hook',workspace_registry=str(self.registry))
        self.event=dict(hook_event_name='SessionStart',session_id='session-a',cwd=str(self.project))
    def test_model_path_does_not_grant_access(self):
        with self.assertRaises(ValueError):self.settings.directory(str(self.project))
        record(self.registry,self.event,self.project)
        self.assertEqual(self.settings.directory(str(self.project)),self.project)
        unrelated=self.root/'other';unrelated.mkdir()
        with self.assertRaises(ValueError):self.settings.directory(str(unrelated))
    def test_lifecycle_and_multiple_sessions(self):
        record(self.registry,self.event,self.project)
        other=dict(self.event,session_id='b');record(self.registry,other,self.project)
        record(self.registry,dict(self.event,hook_event_name='SessionEnd'),self.project)
        self.assertTrue(roots(self.registry))
        record(self.registry,dict(other,hook_event_name='SessionEnd'),self.project)
        self.assertEqual(roots(self.registry),())
    def test_expiry_and_actual_cwd(self):
        with self.assertRaises(ValueError):record(self.registry,self.event,self.root)
        record(self.registry,self.event,self.project)
        with patch('subagent_deepseek.workspaces.time.time',return_value=time.time()+86401):self.assertEqual(roots(self.registry),())
    def test_registry_permissions_symlinks_and_directory_replacement(self):
        record(self.registry,self.event,self.project)
        self.registry.chmod(0o644)
        with self.assertRaises(ValueError):roots(self.registry)
        self.registry.chmod(0o600)
        self.project.rmdir();other=self.root/'other';other.mkdir();self.project.symlink_to(other)
        self.assertEqual(roots(self.registry),())
    def test_hooks_merge_idempotent_and_backup(self):
        hooks=self.root/'hooks.json';hooks.write_text(json.dumps({'hooks':{'SessionStart':[{'hooks':[{'type':'command','command':'echo existing'}]}]}}))
        install(hooks,self.root/'settings.json',Path('/usr/bin/python3'));first=hooks.read_text()
        install(hooks,self.root/'settings.json',Path('/usr/bin/python3'))
        self.assertEqual(first,hooks.read_text());self.assertIn('echo existing',first)
        self.assertEqual(len(list(self.root.glob('hooks.json.before-*.bak'))),1)
    def test_empty_roots_only_for_hook_policy(self):
        key=self.root/'key';key.write_text('test')
        path=self.root/'settings.json';config=dict(codex_binary='/bin/true',credential_file=str(key),allowed_roots=[],workspace_policy='codex-hook')
        path.write_text(json.dumps(config));settings=Settings.load(path)
        self.assertEqual(settings.workspace_registry,str(self.root/'codex-workspaces.json'))
        config['workspace_policy']='allowlist';path.write_text(json.dumps(config))
        with self.assertRaises(ValueError):Settings.load(path)
