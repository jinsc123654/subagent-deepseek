"""Main-agent planning/review tools with bounded DeepSeek execution."""
import asyncio
import time
from typing import Literal
from pathlib import Path
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from .workflow import Board, install_policy
from .dual_protocol import make_contract, build_handoff
from .coordination import Coordination, parse_report, verify_report
from .execution_plan import verify_checks, evidence_directory, ValidationPlan
from .intake import intake
from .workspaces import WorkspaceAccessError
from .harness import HarnessBridge, HarnessError, inherited_permission

INSTRUCTIONS = """For coding projects use THREE layers: Codex is top reviewer, a DeepSeek coordinator
retains bounded memory and delegates to native Harness worker subagents, and workers execute without
further delegation. project_dispatch defaults to role=coordinator (Harness required). Explicit worker
mode retains direct/leaf execution. The main agent owns planning and top-level decisions, constraints and acceptance.
Initialize/read project_board, give task kind, scope/baseline/checks/decision, collect results and inspect
real evidence before marking done. Reuse the same idle coordinator session; no human relay for routine
steps. Read project_coordination for persisted memory and observed native parent/child ids.
Workers cannot change plans or create a fourth layer. Coordination reports are claims; native ancestry
and turn completion are separately checked. Prompts are not a filesystem sandbox or cryptographic roles.
Pass actual CURRENT parent permissions; never expand roots or bypass hook trust. Start a confirmed local
Harness launcher only with prior user authorization. No silent model/backend fallback or ambiguous retry.
"""
INSTRUCTIONS += '\n' + Path(__file__).with_name('three_layer_workflow.md').read_text()


def _result_view(result, details):
    if details:
        return result
    return {k: v for k, v in result.items() if k != 'answer'}


def _report_view(report, details):
    if details:
        return report
    return dict(memory=report['memory'][:2000],
                children=[dict(id=c['id'], status=c['status']) for c in report['children']],
                checks=[dict(name=c['name'][:80], status=c['status']) for c in report.get('checks', [])],
                details_omitted=True, note='Read project_coordination or collect include_details=true for evidence and full handoff.')


def register_workflow_tools(server, settings, path, direct):
    bridge = HarnessBridge(settings, path) if settings.harness_url else None

    @server.tool(structured_output=True)
    async def project_intake(cwd: str, task_id: str,
                             action: Literal['start', 'read', 'select'] = 'read',
                             mode: Literal['', 'quick', 'standard', 'long', 'investigate'] = '') -> dict[str, object]:
        """15-second task mode selection. Start once, show four modes via caller UI, and read after
        the deadline: no selection defaults to standard. Explicit user choices win, including late
        replies (review the active revision before changing work). Repeated start never resets the timer.
        This tool does not show buttons, wait, submit work, grant permissions or wake an idle chat.
        Caller must keep the turn active and perform a bounded wait. Select only actual user choices.
        """
        try:
            return intake(settings, cwd, task_id, action, mode)
        except (ValueError, OSError, KeyError, TypeError):
            raise ToolError('Mode selection unavailable; existing record preserved where possible') from None

    @server.tool(structured_output=True)
    async def project_board(cwd: str, action: Literal['init', 'read', 'add', 'update', 'resolve_uncertain'] = 'read',
                            task_id: str = '', title: str = '', description: str = '', acceptance: str = '',
                            kind: Literal['task', 'bug', 'plan'] = 'task', state: str = '', progress: int = -1,
                            evidence: str = '', anomaly: str = '', note: str = '') -> dict[str, object]:
        """Maintain DEEPSEEK_PROJECT.md: plans, bugs, progress, anomalies and review evidence.

        Main agent owns this board. read on a missing board returns initialized=false without creating files.
        init creates it without replacing existing data and
        installs managed workflow instructions into the project's AGENTS.md, preserving
        existing text with a backup. add requires title, description and acceptance.
        update states: pending/in_progress/review/done/blocked. done requires new review
        evidence from the main agent. Execution output is never acceptance evidence alone.
        resolve_uncertain requires note proving inspection before retrying ambiguous dispatch.
        Do not put secrets or raw reasoning in this record; no automatic background polling.
        """
        try:
            board = Board(settings, cwd)
            if action == 'resolve_uncertain':
                return board.resolve_uncertain(task_id, note)
            result = board.change(action, task_id, title, description, acceptance, kind,
                                  state, progress, evidence, anomaly, note)
            if action == 'init':
                result['instructions'] = install_policy(board.root / 'AGENTS.md')
            return result
        except WorkspaceAccessError as error:
            raise ToolError(str(error)) from None
        except (ValueError, OSError, KeyError):
            raise ToolError('Board operation refused; check allowed directory, board format, state and required fields. Existing records were preserved.') from None

    @server.tool(structured_output=True)
    async def project_dispatch(cwd: str, task_id: str, brief: str, ctx: Context,
                               backend: Literal['auto', 'harness', 'direct'] = 'auto',
                               codex_permission: Literal['', 'read-only', 'workspace-write', 'approve-for-me', 'full-access'] = '',
                               task_kind: Literal['investigate', 'implement', 'test'] = 'implement',
                               allowed_files: list[str] | None = None, constraints: list[str] | None = None,
                               baseline: dict[str, str] | None = None, checks: list[str] | None = None,
                               decision: str = '', reuse_session: bool = True,
                               role: Literal['coordinator', 'worker'] = 'coordinator', max_workers: int = 4,
                               validation: ValidationPlan | None = None,
                               evidence_path: str = 'docs/agent/evidence') -> dict[str, object]:
        """Execute an existing project task with DeepSeek; main agent retains planning/review.

        Default coordinator is the middle layer and must use native Harness workers.
        Its stage memory is persisted in DEEPSEEK_COORDINATION.md and loaded into follow-ups.
        role=worker is an explicit leaf (including direct backend); workers never delegate.
        max_workers limits newly created workers per revision (1–8); fourth-layer descendants
        cause collection to block. Native Host depth limits remain the actual runtime guard.
        validation: mode inherit/smoke/targeted/full/stability/none/method-only, timing this-round/final,
        budget_minutes and resources. Unselected/deferred tests are NOT_RUN. evidence_path is project
        relative; it never grants write permission. Full logs stay in authorized evidence files.
        Example validation={"mode":"none","timing":"this-round","resources":["source files only"]}.
        codex_permission is exactly read-only/workspace-write/approve-for-me/full-access, not prose.
        Put detailed permission restrictions in constraints. none-mode checks are NOT_RUN; static
        investigation results belong in coordinator memory and child reports, not PASS test checks.
        The main agent chooses task_kind and supplies structured allowed_files, constraints,
        baseline (commit/diff/artifact identifiers), checks and current decision. Each dispatch
        records a new revision. Defaults retain compatibility; brief must still define scope.
        Investigate is read-only; test never changes code/artifacts or restores withdrawn plans.
        These are worker instructions and audit records, not an execution sandbox.
        Reuse an existing idle Harness session by default; reuse_session=false isolates a round.
        Main agent handles normal decision/review cycles without human relay.
        brief must include permitted files, constraints and checks. auto selects configured
        Harness, otherwise direct; errors never switch backend. Harness requires actual
        current codex_permission. direct uses fixed deployment settings. User authorization
        for the delegated implementation is required. Don't delegate overlapping file writes.
        Harness returns a receipt; call project_collect. Direct waits and records review.
        Completed here always means awaiting main-agent diff review and tests, never done.
        """
        if not brief.strip() or len(brief) > 80000:
            raise ToolError('brief must contain 1–80000 characters')
        try:
            if isinstance(validation, ValidationPlan):
                validation = validation.model_dump()
            contract = make_contract(task_kind, allowed_files, constraints, baseline, checks, role, max_workers, validation, evidence_path)
            evidence_directory(cwd, contract['evidence_root'], task_id, 1)
            if not isinstance(decision, str) or len(decision) > 16000:
                raise ValueError('Invalid decision')
        except ValueError as error:
            raise ToolError('Invalid handoff contract: ' + str(error) + '; task was not submitted') from None
        backend = ('harness' if bridge else 'direct') if backend == 'auto' else backend
        if role == 'coordinator' and (backend != 'harness' or bridge is None):
            raise ToolError('Coordinator requires Harness native subagent tools; use explicit role=worker for leaf/direct execution')
        if backend == 'harness':
            if bridge is None:
                raise ToolError('Harness is not configured')
            try:
                inherited_permission(codex_permission)
            except HarnessError:
                raise ToolError('codex_permission must be exactly read-only, workspace-write, approve-for-me or full-access; do not append permission descriptions. Task was not submitted.') from None
        try:
            board = Board(settings, cwd)
            previous = board.read()['tasks'].get(task_id, {})
            ledger = Coordination(settings, cwd) if role == 'coordinator' else None
            memory = ledger.context(task_id) if ledger else ''
            existing_ids = []
            baseline_seq = {}
            if previous.get('session_id') and reuse_session and previous.get('contract', {}).get('role', 'worker') != role:
                raise ValueError('Role changed; explicitly isolate with reuse_session=false')
            if backend == 'harness' and previous.get('session_id') and reuse_session:
                status = await bridge.status(previous['session_id'])
                if role == 'coordinator':
                    before_tree = await bridge.worker_tree(previous['session_id'])
                    if before_tree['errors'] or before_tree['truncated'] or any(n.get('running') for n in before_tree['nodes']):
                        raise ValueError('Prior worker tree is unavailable or active')
                    existing_ids = [n['id'] for n in before_tree['nodes']]
                    baseline_seq = {n['id']: n.get('seq') for n in before_tree['nodes']}
                if status.get('status') != 'idle':
                    raise ValueError('Previous session is not idle; inspect or cancel and confirm terminal state')
            task = board.reserve(task_id, backend, contract, decision)
        except (ValueError, OSError, HarnessError):
            raise ToolError('Task cannot be dispatched; initialize/read the board and inspect its state first') from None
        try:
            task['coordination_context'] = memory
            prompt = build_handoff(task, str(board.root), brief, contract, task['revision'])
            if len(prompt) > 100000:
                raise ValueError('Handoff exceeds runtime limit')
        except ValueError:
            board.execution(task_id, state='blocked', uncertain=False, anomaly='交接提示生成失败，尚未提交任务')
            raise ToolError('Invalid handoff prompt; no task was submitted') from None
        if ledger:
            try:
                ledger.record(task_id, task['revision'], state='in_progress', memory=memory,
                              decision=task.get('current_decision', ''), contract=contract, existing_worker_ids=existing_ids)
            except (ValueError, OSError):
                board.execution(task_id, state='blocked', uncertain=False, anomaly='协调记忆不可写；任务未提交')
                raise ToolError('Coordination file cannot be updated; no task was submitted') from None
        try:
            if backend == 'harness':
                if task.get('session_id') and reuse_session:
                    sent = await bridge.send(task['session_id'], prompt, codex_permission=codex_permission)
                    result = dict(sent, session_id=task['session_id'], reused_session=True)
                else:
                    result = await bridge.start(prompt, cwd, task['title'][:100], codex_permission)
                    result['reused_session'] = False
                board.execution(task_id, state='in_progress', session_id=result['session_id'], uncertain=False,
                                worker_baseline_ids=existing_ids, worker_baseline_seq=baseline_seq, coordination_path=str(ledger.path) if ledger else None)
                if ledger:
                    ledger.record(task_id, task['revision'], session_id=result['session_id'])
            else:
                result = await direct(task=prompt, cwd=cwd, ctx=ctx)
                if result.get('status') != 'completed':
                    raise ToolError('Direct execution did not complete')
                board.execution(task_id, state='review', progress=100, uncertain=False,
                                run_id=result.get('run_id'), result_summary=str(result.get('answer', ''))[:12000])
            return dict(task_id=task_id, revision=task['revision'], role=role, contract=contract,
                        coordination_path=str(ledger.path) if ledger else None, board=str(board.path), backend=backend,
                        state='in_progress' if backend == 'harness' else 'review', result=result)
        except BaseException as error:
            # A process/network interruption may follow acceptance: never retry automatically.
            board.execution(task_id, state='blocked', anomaly='执行失败或提交不确定；先核查已有运行再重试。', uncertain=True)
            if ledger:
                ledger.record(task_id, task['revision'], state='blocked', anomaly='执行失败或提交不确定，先核查再重试')
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ToolError('Dispatch failed or is uncertain; task recorded blocked. Inspect existing Harness sessions/activity before resolving and retrying.') from None

    @server.tool(structured_output=True)
    async def project_collect(cwd: str, task_id: str, wait_seconds: int = 0, include_details: bool = False) -> dict[str, object]:
        """Collect a Harness task result into review/blocked; never mark done.

        Default coordinator output is a bounded summary; full report stays in coordination Markdown.
        include_details=true explicitly returns the full report and answer for investigation.
        Read-only/no model submission. Main agent must review actual files and tests,
        then use project_board(update, state=done, evidence=...) for acceptance.
        Repeated collection doesn't alter tasks already in review/done/blocked.
        """
        try:
            deadline = time.monotonic() + (wait_seconds if type(wait_seconds) is int and 0 <= wait_seconds <= 60 else 0)
            board = Board(settings, cwd)
            task = board.read()['tasks'][task_id]
            if task['state'] != 'in_progress' or task.get('dispatching'):
                return dict(task_id=task_id, state=task['state'], note='No active submitted task to collect')
            if task.get('backend') != 'harness' or bridge is None:
                return dict(task_id=task_id, state=task['state'], note='Direct task completes within project_dispatch')
            result = await bridge.result(task['session_id'], wait_seconds=wait_seconds,
                                         max_chars=100000 if task.get('contract', {}).get('role') == 'coordinator' else 12000)
            if task.get('contract', {}).get('role') == 'coordinator':
                tree = await bridge.worker_tree(task['session_id'])
                current = board.read()['tasks'][task_id]
                if current.get('revision') != task.get('revision'):
                    return dict(task_id=task_id, state=current['state'], stale_result=True)
                ledger = Coordination(settings, cwd)
                ledger.record(task_id, task['revision'], observed_children=tree['nodes'])
                while any(n.get('running') for n in tree['nodes']) and time.monotonic() < deadline:
                    await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
                    tree = await bridge.worker_tree(task['session_id'])
                if not any(n.get('running') for n in tree['nodes']):
                    result = await bridge.result(task['session_id'], wait_seconds=0, max_chars=100000)
                if any(n.get('running') for n in tree['nodes']):
                    return dict(task_id=task_id, state='in_progress', coordination_path=str(ledger.path),
                                workers=tree['nodes'], result=dict(finished=False, completed=False, note='Native workers are still running'))
                while (tree['nodes'] and result.get('completed') and '<coordination_report>' not in result.get('answer', '')
                       and time.monotonic() < deadline):
                    await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
                    result = await bridge.result(task['session_id'], wait_seconds=0, max_chars=100000)
                if not result.get('finished'):
                    return dict(task_id=task_id, state=task['state'], workers=tree['nodes'],
                                coordination_path=str(ledger.path), result=_result_view(result, include_details))
                if tree['nodes'] and result.get('completed') and '<coordination_report>' not in result.get('answer', ''):
                    since = task.get('coord_report_wait_since') or time.time()
                    if time.time() - since < 60:
                        board.execution(task_id, expected_revision=task.get('revision'), state='in_progress', coord_report_wait_since=since)
                        return dict(task_id=task_id, state='in_progress', workers=tree['nodes'],
                                    result=dict(finished=False, completed=False, note='Waiting for coordinator summary after worker completion'))
                try:
                    if not result.get('completed'):
                        raise ValueError('Coordinator turn did not complete')
                    report = parse_report(result.get('answer', ''))
                    verify_report(report, tree, task.get('worker_baseline_ids', []), task['contract'].get('max_workers', 4), task.get('worker_baseline_seq', {}))
                    check_results = verify_checks(report, task['contract'].get('validation', {'mode':'inherit', 'timing':'this-round'}))
                    outcome = 'review' if all(c['status'] == 'PASS' for c in report['children']) and not any(c['status'] in ('FAIL','BLOCKED','SUBSTITUTED') for c in check_results) else 'blocked'
                    anomaly = '' if outcome == 'review' else '部分执行子任务未通过；由Codex决定下一轮'
                    ledger.record(task_id, task['revision'], report=report, state=outcome, anomaly=anomaly)
                    board.execution(task_id, expected_session=task['session_id'], expected_revision=task.get('revision'),
                                    state=outcome, progress=100 if outcome == 'review' else task.get('progress', 0),
                                    result_summary=result.get('answer', '')[:12000], anomaly=anomaly)
                    return dict(task_id=task_id, state=outcome, workers=tree['nodes'],
                                coordination_path=str(ledger.path), report=_report_view(report, include_details), result=_result_view(result, include_details))
                except (ValueError, TypeError, KeyError):
                    anomaly = '协调报告或原生子任务核验失败；不能当作三层执行成功'
                    ledger.record(task_id, task['revision'], state='blocked', anomaly=anomaly)
                    board.execution(task_id, expected_session=task['session_id'], expected_revision=task.get('revision'),
                                    state='blocked', anomaly=anomaly)
                    return dict(task_id=task_id, state='blocked', workers=tree['nodes'],
                                coordination_path=str(ledger.path), result=_result_view(result, include_details), anomaly=anomaly)
            if result.get('finished'):
                if result.get('completed'):
                    board.execution(task_id, expected_session=task['session_id'], expected_revision=task.get('revision'), state='review', progress=100,
                                    result_summary=result.get('answer', '')[:12000])
                else:
                    board.execution(task_id, expected_session=task['session_id'], expected_revision=task.get('revision'), state='blocked', anomaly='执行结束异常：' + str(result.get('stop_reason')))
            return dict(task_id=task_id, state=board.read()['tasks'][task_id]['state'], result=result)
        except Exception:
            raise ToolError('Could not collect task; existing state preserved where possible. Inspect session before retrying.') from None

    @server.tool(structured_output=True)
    async def project_coordination(cwd: str, task_id: str = '') -> dict[str, object]:
        """Read persisted middle-layer memory, worker ids and evidence without resubmitting tasks.

        The bridge records native ancestry separately from coordinator claims. This file is
        not the top-level project plan and never substitutes for Codex review or acceptance.
        Use project_collect to refresh live worker observations. No secrets or raw reasoning.
        """
        try:
            ledger = Coordination(settings, cwd)
            data = ledger.read()
            return dict(path=str(ledger.path), tasks={task_id: data['tasks'][task_id]} if task_id else data['tasks'])
        except (ValueError, OSError, KeyError):
            raise ToolError('Coordination memory unavailable; existing records preserved') from None
