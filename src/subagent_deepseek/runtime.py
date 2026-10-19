"""Isolated Codex process lifecycle; configuration and logs are temporary."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

from .config import Settings
from .credential import read_key
from .wire import Failure, Wire, Progress, silent_progress


def child_environment(home: Path, settings_path: Path) -> dict[str, str]:
    """Exclude inherited credential variables and parent session identifiers."""
    excluded = ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY', 'CREDENTIAL')
    env = {k: v for k, v in os.environ.items()
           if not any(word in k.upper() for word in excluded)
           and not k.startswith('CODEX_') and k not in ('SSH_AUTH_SOCK', 'OPENAI_ORG_ID')}
    env['CODEX_HOME'] = str(home)
    env['SUBAGENT_DEEPSEEK_SETTINGS'] = str(settings_path)
    # Absolute helper path works in both an installed wheel and a source checkout.
    return env


def native_config(settings: Settings) -> str:
    """Pin the provider endpoint and load credentials through a private helper."""
    root = Path(__file__).resolve().parent
    values = {
        'model': settings.model, 'model_provider': 'deepseek',
        'model_reasoning_effort': settings.reasoning_effort,
        'approval_policy': 'never', 'sandbox_mode': settings.mode,
        'web_search': 'disabled', 'model_catalog_json': str(root / 'models.json'),
    }
    lines = [f'{key} = {json.dumps(value)}' for key, value in values.items()]
    lines += ['[features]', 'multi_agent = false',
              '[model_providers.deepseek]', 'name = "DeepSeek"',
              'base_url = "https://api.deepseek.com/"', 'wire_api = "responses"',
              '[model_providers.deepseek.auth]', f'command = {json.dumps(sys.executable)}',
              f'args = {json.dumps([str(root / "credential.py")])}',
              'timeout_ms = 5000', 'refresh_interval_ms = 0']
    return '\n'.join(lines) + '\n'


async def drain_stderr(stream: asyncio.StreamReader) -> None:
    """Drain without retaining or forwarding raw runtime logs."""
    while await stream.read(65536):
        pass


async def stop_process(process: asyncio.subprocess.Process, grace: float) -> None:
    """Terminate the process group, including descendants after parent exit."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            # Keep a bounded grace even when the parent exited before descendants.
            await asyncio.sleep(grace)
    await process.wait()


async def delegate(settings: Settings, settings_path: Path, task: str, cwd: str,
                   handshake_only: bool = False, progress: Progress = silent_progress) -> dict:
    """Run one bounded task, or perform a keyless app-server compatibility probe."""
    await progress("检查目录与凭据")
    directory = settings.directory(cwd)
    if not task.strip() or len(task) > 100000:
        raise ValueError('task must contain 1–100000 characters')
    if not handshake_only:
        read_key(Path(settings.credential_file), settings.credential_type)
    with tempfile.TemporaryDirectory(prefix='subagent-deepseek-') as name:
        home = Path(name)
        config = home / 'config.toml'
        config.write_text(native_config(settings))
        config.chmod(0o600)
        # Prevent runtime writes in the task directory before sandbox activation.
        process = await asyncio.create_subprocess_exec(
            settings.codex_binary, '--no-daemon',
            '-c', 'model_provider="deepseek"', '-c', f'model={json.dumps(settings.model)}',
            '-c', 'model_providers.deepseek.base_url="https://api.deepseek.com/"',
            '-c', 'model_providers.deepseek.wire_api="responses"',
            '-c', f'model_reasoning_effort={json.dumps(settings.reasoning_effort)}',
            'app-server', '--stdio', cwd=home,
            env=child_environment(home, settings_path), start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024)
        stderr_task = asyncio.create_task(drain_stderr(process.stderr))
        wire = Wire(process, progress)
        completed = False
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                await progress("连接子智能体运行时")
                await wire.initialize()
                await wire.start_thread(str(directory), settings.model, settings.reasoning_effort, settings.mode)
                await progress("临时线程已就绪，DeepSeek 模型与权限核验通过")
                if handshake_only:
                    return {'status': 'compatible', 'provider': 'deepseek', 'model': settings.model,
                            'reasoning_effort': settings.reasoning_effort, 'mode': settings.mode,
                            'route_metadata_verified': True, 'model_request_sent': False}
                answer = await wire.run_turn(task, settings.reasoning_effort)
                completed = True
                return {'status': 'completed', 'provider': 'deepseek', 'model': settings.model,
                        'reasoning_effort': settings.reasoning_effort, 'mode': settings.mode,
                        'route_metadata_verified': True, 'answer': answer,
                        'unattended_denials': wire.denials}
        except TimeoutError:
            raise Failure('lifecycle', 'timeout') from None
        finally:
            # MCP cancellation uses AnyIO cancel scopes; shield mandatory cleanup.
            import anyio
            with anyio.CancelScope(shield=True):
                if not completed and wire.thread_id and wire.turn_id and not wire.error:
                    try:
                        await asyncio.wait_for(wire.request('turn/interrupt', {
                            'threadId': wire.thread_id, 'turnId': wire.turn_id}), timeout=0.5)
                    except (Failure, TimeoutError):
                        pass  # Local process-group teardown remains authoritative.
                await wire.close()
                await stop_process(process, settings.shutdown_grace_seconds)
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
