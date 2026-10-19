"""Activity history, retention and notification failure behavior."""
from pathlib import Path
import tempfile
import unittest
from subagent_deepseek.activity import Activity, prune_logs


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_failure_keeps_local_history(self):
        class Context:
            async def report_progress(self, *args, **kwargs):
                raise ConnectionError('client disconnected')
        with tempfile.TemporaryDirectory() as name:
            activity = Activity(Path(name))
            await activity.report(Context(), '任务已开始')
            self.assertTrue(activity.progress_unavailable)
            self.assertIn('任务已开始', activity.path.read_text())
            self.assertEqual(activity.path.stat().st_mode & 0o777, 0o600)
            activity.finish()

    async def test_bounded_history_and_active_log_retention(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            first = Activity(directory)
            second = Activity(directory)
            for i in range(80):
                first.record(f'事件 {i}')
            self.assertEqual(len(first.snapshot()['events']), 50)
            second.finish()
            prune_logs(directory, set(), keep=0)
            self.assertTrue(first.path.exists())
            self.assertFalse(second.path.exists())
            first.finish()
