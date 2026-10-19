"""Behavior coverage across process, protocol, credential, and MCP boundaries."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subagent_deepseek.config import Settings
from subagent_deepseek.credential import read_key
from subagent_deepseek.runtime import child_environment, delegate, native_config
from subagent_deepseek.wire import Failure

FIXTURE = Path(__file__).parent / 'fake_app_server.py'


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / 'private-key'
        self.key.write_text('sk-test-placeholder')
        self.key.chmod(0o600)
        self.settings_path = self.root / 'settings.json'
        self.settings = Settings(str(FIXTURE.resolve()), str(self.key), (str(self.root),),
                                 timeout_seconds=2, shutdown_grace_seconds=0.02)
        self.save_settings()

    def save_settings(self):
        from dataclasses import asdict
        self.settings_path.write_text(json.dumps(asdict(self.settings)))

    async def run_scenario(self, scenario):
        with patch.dict(os.environ, {'TEST_SCENARIO': scenario}):
            return await delegate(self.settings, self.settings_path, 'Task', str(self.root))

    async def test_final_answer_and_foreign_event_filtering(self):
        result = await self.run_scenario('success')
        self.assertEqual(result['answer'], 'final answer')
        self.assertTrue(result['route_metadata_verified'])
        self.assertNotIn('commentary', str(result))

    async def test_notifications_before_turn_start_response(self):
        self.assertEqual((await self.run_scenario('early-events'))['answer'], 'early final')

    async def test_nullable_phase_compatibility(self):
        self.assertEqual((await self.run_scenario('unphased'))['answer'], 'legacy final')

    async def test_unattended_approval_is_declined(self):
        result = await self.run_scenario('approval')
        self.assertEqual(result['answer'], 'permission declined')
        self.assertIn('item/commandExecution/requestApproval', result['unattended_denials'])

    async def test_protocol_failures_do_not_return_partial_answers(self):
        cases = {'route-mismatch': 'route-mismatch', 'permission-mismatch': 'permission-mismatch',
                 'not-ephemeral': 'not-ephemeral', 'failed': 'product-error',
                 'empty': 'empty-answer', 'eof': 'early-eof', 'malformed': 'invalid-protocol',
                 'unknown-request': 'unsupported-request', 'wrong-request-thread': 'wrong-thread'}
        for scenario, category in cases.items():
            with self.subTest(scenario=scenario):
                with self.assertRaises(Failure) as caught:
                    await self.run_scenario(scenario)
                self.assertEqual(caught.exception.category, category)

    async def test_timeout_cleans_temporary_home_and_process_group(self):
        self.settings = replace(self.settings, timeout_seconds=0.15)
        trace = self.root / 'trace.json'
        with patch.dict(os.environ, {'TEST_TRACE': str(trace)}):
            with self.assertRaises(Failure) as caught:
                await self.run_scenario('hang')
        self.assertEqual(caught.exception.category, 'timeout')
        self.assert_cleaned(trace)

    def assert_cleaned(self, trace):
        data = json.loads(trace.read_text())
        self.assertFalse(Path(data['home']).exists())
        for key in ('pid', 'child_pid'):
            pid = data[key]
            stat = Path(f'/proc/{pid}/stat')
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z')

    async def test_cancellation_cleans_process_group(self):
        trace = self.root / 'trace.json'
        with patch.dict(os.environ, {'TEST_TRACE': str(trace), 'TEST_SCENARIO': 'cancel'}):
            task = asyncio.create_task(delegate(self.settings, self.settings_path, 'Task', str(self.root)))
            for _ in range(100):
                if trace.exists() and 'child_pid' in trace.read_text():
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail('Fixture did not start')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assert_cleaned(trace)

    async def test_keyless_handshake_never_sends_model_request(self):
        self.key.write_text('invalid key')
        result = await delegate(self.settings, self.settings_path, 'Probe', str(self.root), handshake_only=True)
        self.assertFalse(result['model_request_sent'])

    async def test_concurrent_calls_have_separate_homes(self):
        results = await asyncio.gather(*(self.run_scenario('success') for _ in range(2)))
        self.assertEqual([r['answer'] for r in results], ['final answer'] * 2)

    async def test_progress_summaries_exclude_payloads(self):
        messages = []
        async def progress(message):
            messages.append(message)
        with patch.dict(os.environ, {'TEST_SCENARIO': 'progress'}):
            result = await delegate(self.settings, self.settings_path, 'Task', str(self.root), progress=progress)
        self.assertEqual(result['answer'], 'final answer')
        self.assertEqual(messages.count('执行命令：开始'), 1)
        self.assertIn('执行命令：完成，退出码 0', messages)
        self.assertNotIn('sk-fixture-secret', str(messages))

    def test_settings_reject_unknown_fields_and_unsafe_paths(self):
        self.assertEqual(Settings.load(self.settings_path), self.settings)
        with self.assertRaises(ValueError):
            self.settings.directory('/tmp')
        link = self.root / 'escape'
        link.symlink_to('/tmp')
        with self.assertRaises(ValueError):
            self.settings.directory(str(link))
        raw = json.loads(self.settings_path.read_text())
        raw['unexpected'] = True
        self.settings_path.write_text(json.dumps(raw))
        with self.assertRaises(TypeError):
            Settings.load(self.settings_path)

    def test_credentials_private_plain_yaml_and_symlink(self):
        self.assertEqual(read_key(self.key, 'file'), 'sk-test-placeholder')
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            read_key(self.key, 'file')
        self.key.chmod(0o600)
        link = self.root / 'key-link'
        link.symlink_to(self.key)
        with self.assertRaises(OSError):
            read_key(link, 'file')
        self.key.write_text('refs:\n  DEEPSEEK_API_KEY: sk-test-yaml\n')
        self.assertEqual(read_key(self.key, 'dsh'), 'sk-test-yaml')

    def test_credential_environment_scrubbing(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'private', 'SOME_TOKEN': 'private',
                                     'CODEX_THREAD_ID': 'parent', 'NORMAL_SETTING': 'preserve'}):
            env = child_environment(self.root, self.settings_path)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('SOME_TOKEN', env)
        self.assertNotIn('CODEX_THREAD_ID', env)
        self.assertEqual(env['NORMAL_SETTING'], 'preserve')
        self.assertNotIn('sk-test', native_config(self.settings))


if __name__ == '__main__':
    unittest.main()
