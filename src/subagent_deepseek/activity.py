"""Per-call activity summaries and bounded live logs; no model/tool payloads."""
import asyncio
import fcntl
from datetime import datetime
from zoneinfo import ZoneInfo
import os
from pathlib import Path
import time
from uuid import uuid4


class Activity:
    """Track one invocation, independent of whether the client renders progress."""
    def __init__(self, directory: Path):
        self.run_id = uuid4().hex
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / (self.run_id + '.log')
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(fd, 'a')
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.status = 'queued'
        self.started = time.monotonic()
        self.sequence = 0
        self.last_message = ''
        self.events: list[dict] = []
        self.progress_unavailable = False

    def record(self, message: str) -> None:
        """Append an internally authored summary, keeping bounded in-memory history."""
        self.sequence += 1
        self.last_message = message
        event = {'sequence': self.sequence, 'elapsed_seconds': round(time.monotonic() - self.started, 2),
                 'message': message}
        self.events.append(event)
        self.events = self.events[-50:]
        # Cap disk growth for unusually long tasks. Status snapshots still update.
        if self.sequence <= 2000:
            stamp = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
            self.stream.write(f'{stamp} [{self.sequence}] +{event["elapsed_seconds"]}s {message}\n')
            self.stream.flush()

    def finish(self) -> None:
        """Release the log lock so retention can remove ended tasks across servers."""
        self.stream.close()

    def snapshot(self) -> dict:
        """Return safe summaries, never the task, commands, stdout or reasoning."""
        return {'run_id': self.run_id, 'status': self.status, 'activity_log': str(self.path),
                'elapsed_seconds': round(time.monotonic() - self.started, 2),
                'progress_delivery_unavailable': self.progress_unavailable,
                'events': list(self.events)}

    async def report(self, context, message: str) -> None:
        """Keep local evidence even when client notification delivery fails."""
        self.record(message)
        try:
            await asyncio.wait_for(context.report_progress(self.sequence, message=message), timeout=1)
        except Exception:
            # Observability is auxiliary; task execution/teardown stays authoritative.
            self.progress_unavailable = True


def prune_logs(directory: Path, active: set[Path], keep: int = 20) -> None:
    """Retain recent logs and skip active file locks, including other MCP processes."""
    candidates = []
    for path in directory.glob('*.log'):
        try:
            candidates.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue  # Another MCP process may already have pruned this log.
    retained = 0
    for _, file in sorted(candidates, reverse=True):
        if file in active:
            continue
        try:
            with file.open('a') as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # A task in another server still owns this log.
                if retained >= keep:
                    file.unlink(missing_ok=True)
                else:
                    retained += 1
        except FileNotFoundError:
            continue
