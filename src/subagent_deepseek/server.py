"""MCP stdio entry point with progress, live activity logs and status queries."""
import argparse
import asyncio
from pathlib import Path

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .activity import Activity, prune_logs
from .config import Settings
from .runtime import delegate
from .wire import Failure
from .harness_tools import register_harness_tools
from .workflow_tools import INSTRUCTIONS, register_workflow_tools


def create_server(settings: Settings, path: Path) -> FastMCP:
    """Expose bounded tasks and safe live status with deployment-owned permissions."""
    server = FastMCP('subagent-deepseek', instructions=INSTRUCTIONS + '\nInstalled setup guide: ' + str(path.parent / 'QUICKSTART.md'))
    slots = asyncio.Semaphore(settings.max_concurrency)
    runs: dict[str, Activity] = {}
    directory = path.parent / 'activity'

    @server.tool(structured_output=True)
    async def subagent_deepseek(task: str, cwd: str, ctx: Context) -> dict[str, object]:
        """Delegate a self-contained task to DeepSeek Flash with high reasoning.

        Supply all context and an allowed absolute directory. No parent history
        is inherited. Fixed deployment permissions apply. Reports live MCP progress
        and tool activity summaries; returns final answer, run_id and activity_log.
        Use subagent_deepseek_status to inspect running tasks if the client hides
        progress. Cancellation/timeout terminates the process group, without rollback.
        """
        settings.directory(cwd)
        if not task.strip() or len(task) > 100000:
            raise ToolError('task must contain 1–100000 characters')
        activity = Activity(directory)
        runs[activity.run_id] = activity
        heartbeat = None
        async def report(message: str) -> None:
            await activity.report(ctx, message)
        async def pulse() -> None:
            while True:
                await asyncio.sleep(5)
                await report(f'仍在运行：{activity.status}')
        try:
            await report('任务已接收，等待执行名额')
            heartbeat = asyncio.create_task(pulse())
            async with slots:
                activity.status = 'running'
                result = await delegate(settings, path, task, cwd, progress=report)
            activity.status = 'completed'
            await report('任务完成')
            return {**result, 'run_id': activity.run_id, 'activity_log': str(activity.path)}
        except asyncio.CancelledError:
            activity.status = 'cancelled'
            activity.record('任务已取消，子进程清理已执行')
            raise
        except Failure as error:
            activity.status = 'failed'
            await report(f'任务失败：{error.stage} / {error.category}')
            raise ToolError(str(error)) from None
        except ValueError as error:
            activity.status = 'failed'
            await report('任务失败：配置或凭据检查未通过')
            raise ToolError(str(error)) from None
        except Exception:
            activity.status = 'failed'
            await report('任务失败：请检查运行时、凭据与网络')
            raise ToolError('DeepSeek subagent failed; check runtime, credentials, and network') from None
        finally:
            with anyio.CancelScope(shield=True):
                if heartbeat:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                activity.finish()
                # Retain a bounded recent history without removing other active calls.
                inactive = [key for key, value in runs.items() if value.status not in ('queued', 'running')]
                for key in inactive[:-20]:
                    del runs[key]
                prune_logs(directory, {v.path for v in runs.values() if v.status in ('queued', 'running')})

    @server.tool(structured_output=True)
    async def subagent_deepseek_status(run_id: str = '') -> dict[str, object]:
        """Read running/recent task progress without waiting for the task to finish.

        Omit run_id to list this server process's tasks, or provide a returned id.
        Includes live log paths and the latest 50 safe activity summaries.
        History resets on MCP restart; saved activity logs remain available.
        """
        if run_id:
            if run_id not in runs:
                raise ToolError('Unknown run_id in this MCP session')
            return runs[run_id].snapshot()
        return {'runs': [run.snapshot() for run in runs.values()]}

    register_workflow_tools(server, settings, path, subagent_deepseek)
    if settings.harness_url:
        register_harness_tools(server, settings, path)
    return server


def main() -> None:
    """Load deployment settings and serve MCP over stdio."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True, type=Path)
    args = parser.parse_args()
    path = args.settings.expanduser().resolve()
    settings = Settings.load(path)
    create_server(settings, path).run(transport='stdio')


if __name__ == '__main__':
    main()
