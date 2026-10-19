"""MCP verbs for asynchronous, web-visible Harness sessions."""
from pathlib import Path
from typing import Literal

CodexPermission = Literal['read-only', 'workspace-write', 'approve-for-me', 'full-access']

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .config import Settings
from .harness import HarnessBridge, HarnessError


async def operation(awaitable):
    """Convert failures to safe tool errors, preserving cancellation."""
    try:
        return await awaitable
    except HarnessError as error:
        raise ToolError(str(error)) from None
    except ValueError:
        raise ToolError('Invalid configuration or working directory') from None
    except Exception:
        raise ToolError('Harness bridge failed; inspect the existing web session before retrying') from None


def register_harness_tools(server: FastMCP, settings: Settings, path: Path) -> None:
    """Register web session tools only when the deployment explicitly enables them."""
    bridge = HarnessBridge(settings, path)

    @server.tool(structured_output=True)
    async def harness_start(task: str, cwd: str, codex_permission: CodexPermission, title: str = '') -> dict[str, object]:
        """Start a real DeepSeek Harness WEB conversation and return immediately.

        Prefer this tool when the user wants to watch the task in Harness web.
        REQUIRED codex_permission must be the CURRENT parent Codex permission:
        read-only, workspace-write, approve-for-me (auto approval), or full-access.
        The user's policy maps the latter two to Harness danger-full-access.
        Never choose permissions from task requirements or stale environment values.
        If the parent's mode is unavailable, do not guess or request broader access.
        Uses its existing Host, pins configured model and inherited permission before
        sending a task. Only allowed directories are accepted. Returns session_id
        and sidebar title; the user watches full live details in the web UI.
        This is a normal web conversation controlled by the bridge, not a native
        subagent child or a Codex app-server thread. Use harness_result to wait or
        retrieve its text-only final answer. Do not duplicate an uncertain request.
        """
        return await operation(bridge.start(task, cwd, title, codex_permission))

    @server.tool(structured_output=True)
    async def harness_status(session_id: str = '') -> dict[str, object]:
        """Read compact status, token usage and pending questions for a bridge session.

        Omit session_id to recover the latest 20 bridge session receipts after a
        restart or lost start response. Does not return live transcript, tool output or reasoning. Only sessions
        created by this bridge are accessible. Watch full details in the web UI.
        """
        if not session_id:
            return bridge.list_sessions()
        return await operation(bridge.status(session_id))

    @server.tool(structured_output=True)
    async def harness_result(session_id: str, wait_seconds: int = 0, max_chars: int = 12000) -> dict[str, object]:
        """Wait up to 60 seconds and return the latest completed web-session reply.

        Returns text only; never reasoning or raw tools. finished marks a terminal
        turn and completed marks success; failed/cancelled turns are not presented
        as successful answers. Results cover the latest bridge submission baseline,
        not exact causal attribution if the user also steers in the web UI.
        Increase max_chars if answer_truncated is true; maximum 100000.
        """
        return await operation(bridge.result(session_id, wait_seconds, max_chars))

    @server.tool(structured_output=True)
    async def harness_send(session_id: str, text: str, codex_permission: CodexPermission, steer: bool = False) -> dict[str, object]:
        """Send an authorized follow-up into the same bridge-created web conversation.

        Default queues a message. steer=true interrupts/steers the current activity.
        Pass the CURRENT parent Codex permission as codex_permission (same mapping
        as harness_start). Synchronizes permissions, verifies directory and model,
        then submits the message while preserving history.
        Does not approve permissions or answer foreground approval requests.
        """
        return await operation(bridge.send(session_id, text, steer, codex_permission))

    @server.tool(structured_output=True)
    async def harness_cancel(session_id: str) -> dict[str, object]:
        """Cancel the current turn of a bridge-owned web conversation.

        Already queued messages remain and may run afterward; this does not delete
        the session, clear the queue, or roll back edits. Web service stays running.
        """
        return await operation(bridge.cancel(session_id))

    @server.tool(structured_output=True)
    async def harness_answer(session_id: str, call_id: str, answers: list[dict], codex_permission: CodexPermission) -> dict[str, object]:
        """Deliver the user's explicit answers to continued Harness questions.

        Each answer has id, selected string labels, and optional custom text.
        Does not answer foreground questions or authorize permissions; those stay
        in the web UI. Only use answers supplied or authorized by the user.
        """
        return await operation(bridge.answer(session_id, call_id, answers, codex_permission))

    @server.tool(structured_output=True)
    async def harness_sync_permissions(session_id: str, codex_permission: CodexPermission) -> dict[str, object]:
        """Sync a web task when the user changes the parent's Codex permission mode.

        Supply the CURRENT parent permission, never infer it from the task.
        approve-for-me/full-access map to danger-full-access by user policy;
        read-only/workspace-write map directly. Sends no model message.
        Permission-selector changes are not pushed to MCP automatically: call this
        tool when a change is known. Only bridge-owned sessions are mutable.
        """
        return await operation(bridge.sync_permissions(session_id, codex_permission))
