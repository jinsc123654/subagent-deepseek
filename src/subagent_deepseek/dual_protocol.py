"""Dual-end handoff protocol (v0.7): validated contract and prompt rendering; no I/O."""
from __future__ import annotations
from .execution_plan import validation_plan, evidence_root, evidence_directory

KINDS = ('investigate', 'implement', 'test')
MAX_FIELD = 16000
REQUIRED = ('kind', 'allowed_files', 'constraints', 'baseline', 'checks')
_RULES = {
    'investigate': ('只读调查者 / read-only investigator',
                    '本任务只读：仅调查、阅读与报告，不修改任何文件。This task is read-only: '
                    'investigate, read and report only; do not modify any file.'),
    'implement': ('有界实现者 / bounded implementer',
                  '本任务为有界实现：只改 allowed_files 指定范围，其余文件不动。This is a bounded '
                  'implementation: only change the designated allowed_files range; touch nothing else.'),
    'test': ('仅测试验证者 / test-only verifier',
             '本任务仅测试：只运行测试与检查；不改代码、不重建、不换镜像、不恢复已撤回方案。This task is '
             'test-only: run tests and checks only; do not modify code, rebuild, change images, '
             'or restore reverted plans.'),
}


def _text_list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'{field} must be a list of non-empty strings')
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f'{field} must contain only non-empty strings')
    if sum(map(len, value)) > MAX_FIELD:
        raise ValueError(f'{field} exceeds {MAX_FIELD} characters')
    return list(value)


def _baseline(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str) for k, v in value.items()):
        raise ValueError('baseline must map non-empty string keys to string values')
    if sum(len(k) + len(v) for k, v in value.items()) > MAX_FIELD:
        raise ValueError(f'baseline exceeds {MAX_FIELD} characters')
    return dict(value)


def make_contract(task_kind='implement', allowed_files=None, constraints=None,
                  baseline=None, checks=None, role='worker', max_workers=4, validation=None, evidence_path='docs/agent/evidence'):
    """Build a validated dispatch contract; invalid input raises ValueError."""
    if not isinstance(task_kind, str) or task_kind not in KINDS:
        raise ValueError('task_kind must be investigate, implement or test')
    if role not in ('coordinator', 'worker') or type(max_workers) is not int or not 1 <= max_workers <= 8:
        raise ValueError('Invalid role or worker budget')
    return dict(validation=validation_plan(validation), evidence_root=evidence_root(evidence_path), role=role, max_workers=max_workers, kind=task_kind, allowed_files=_text_list(allowed_files, 'allowed_files'),
                constraints=_text_list(constraints, 'constraints'),
                baseline=_baseline(baseline), checks=_text_list(checks, 'checks'))


def _contract(value):
    if not isinstance(value, dict) or any(key not in value for key in REQUIRED):
        raise ValueError('contract must be built by make_contract')
    return make_contract(value['kind'], value['allowed_files'], value['constraints'],
                         value['baseline'], value['checks'], value.get('role', 'worker'), value.get('max_workers', 4), value.get('validation'), value.get('evidence_root', 'docs/agent/evidence'))


def _task_field(task, name, aliases=()):
    for key in (name,) + tuple(aliases):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise ValueError(f'task missing non-empty {name}')


def _bullets(items, empty):
    return [f'- {item}' for item in items] if items else [empty]


def build_handoff(task: dict, cwd: str, brief: str, contract: dict, revision: int) -> str:
    """Render the bilingual handoff prompt; never executes anything."""
    if not isinstance(task, dict):
        raise ValueError('task must be a dict')
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError('cwd must be a non-empty string')
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError('brief must be a non-empty string')
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError('revision must be a non-negative integer')
    data = _contract(contract)
    kind = data['kind']
    coordinator = data['role'] == 'coordinator'
    task_id = _task_field(task, 'id', ('task_id',))
    title = _task_field(task, 'title')
    description = _task_field(task, 'description')
    acceptance = _task_field(task, 'acceptance')
    baseline = [f'- {key}: {value}' for key, value in data['baseline'].items()] or ['- 无 / none']
    lines = [
        '# 任务交接单 / Task Handoff',
        f'- 任务ID / Task ID: {task_id}',
        f'- 修订 / Revision: {revision}',
        f'- 工作目录 / cwd: {cwd}',
        f'- 任务类型 / Kind: {kind}',
        f"- 角色 / Role: {'第二层 DeepSeek 协调 Agent' if coordinator else '第三层执行 worker'} · {_RULES[kind][0]}",
        '',
        '同一会话跟进须沿用任务ID并递增修订，修订记录必须可追溯。Same-session follow-ups must reuse '
        'the task ID with an incremented revision; revision history must stay traceable.',
        '',
        '## 标题 / Title', title, '## 描述 / Description', description,
        '## 验收条件 / Acceptance', acceptance,
        '',
        '## 任务边界 / Kind boundary', _RULES[kind][1],
        '',
        '## 允许文件操作 / Allowed file operations',
        *_bullets(data['allowed_files'],
                  '无文件写入；未明确列出的文件一律不得修改。No writes; anything not explicitly listed must not be modified.'),
        '',
        '## 禁止事项 / Prohibitions',
        '- Do not edit DEEPSEEK_PROJECT.md, AGENTS.md or workflow instructions.',
        *_bullets(data['constraints'], '除上述固定禁止事项外无额外约束。No extra constraints beyond the fixed prohibitions above.'),
        '',
        '## 基线 / Baseline', *baseline,
        '',
        '## 验证计划 / Validation plan', str(data['validation']),
        '未选择的长测/烧录不执行；final检查留到Codex授权的收尾修订；none/method-only仅回传NOT_RUN，方法记录不能算PASS。',
        '## 证据目录 / Evidence directory', evidence_directory(cwd, data['evidence_root'], task_id, revision),
        '此目录仅为证据约定，不授予写权限；只读任务不落盘，写证据须在allowed_files内。子任务使用独立子目录。',
        '## 检查项目 / Checks',
        *_bullets(data['checks'], '无额外检查；仍须如实报告实际执行情况。No extra checks; still report what was actually run.'),
        '',
        '## 主Agent当前决定 / Current main-agent decision', task.get('current_decision') or '未另行记录；发生冲突先回报主Agent。',
        '## 主Agent简报 / Main-agent brief', brief,
        '',
        '## 固定双端协议 / Fixed dual-end protocol',
        ('1. Codex保留顶层决定与验收；你是协调层，允许创建第三层原生执行子Agent，不得把自己当作终端worker或声称Codex已验收。' if coordinator else
         '1. 主Agent决定方案与验收；worker不委派、不改规划、不宣称验收通过。The main agent decides the approach and acceptance; the worker must not delegate, change the plan, or claim acceptance.'),
        '2. 遇冲突或需变更方案，先回报主Agent决定；没有回复不等于同意。Report conflicts or needed plan '
        'changes to the main agent first; no reply does not mean consent.',
        '3. 未明确允许时不得 commit、push、烧录（flash）；不抢同文件/设备资源。Do not commit, push, or '
        'flash/burn unless explicitly allowed; do not contend for the same files or device resources.',
        '4. 回传实际动作、改动文件、产物版本、测试条件与时长、逐项 PASS/FAIL/NOT_RUN/SUBSTITUTED、'
        '首异常与时间顺序、证据位置、剩余问题。Report actual actions, changed files, artifact versions, '
        'test conditions and duration, itemized PASS/FAIL/NOT_RUN/SUBSTITUTED, first exception in time '
        'order, evidence locations and remaining issues.',
        '5. 成功只待审阅，不代表主Agent已审阅、验收或认定完成。Success is review-pending only; it does '
        'not mean the main agent has reviewed, accepted or completed it.',
    ]
    if coordinator:
        lines += coordinator_instructions(task, data)
    return '\n'.join(lines)


def coordinator_instructions(task, contract):
    return [
        '', '## 三层协调要求（优先于旧版两层worker提示）',
        '层级：Codex顶层 → 你（DeepSeek协调层）→ 原生执行子Agent。你可以在本工作包内自主拆分与调度，不要逐步让用户转述。',
        '必须实际调用本Harness的原生 subagent 工具创建至少一个直接执行子Agent；使用spawn，不使用fork或外部Codex/ACP后端，不改提供方/模型。',
        '若原生subagent工具不可用/被深度限制，返回BLOCKED说明；不能伪造调用，也不能自己独自完成后声称三层已执行。',
        f"本修订最多创建 {contract['max_workers']} 个worker；可沿用已有空闲worker。下发cwd必须与本工作包一致。",
        '每个worker prompt都必须独立包含：父工作包ID/修订、第三层worker身份、任务类型、目标、允许文件、禁止事项、版本基线、检查与证据要求。',
        '每个worker必须明确：Do not create subagents or delegate. Do not edit DEEPSEEK_PROJECT.md, DEEPSEEK_COORDINATION.md, AGENTS.md or workflow instructions. 不创建第四层。',
        '调查worker只读；仅测试worker不得改代码/重建/换镜像/恢复撤回方案；实现worker仅改允许文件。继承本轮全部约束，不扩大权限。',
        '你也不修改顶层规划、协调记忆文件或AGENTS；协调记忆由桥接根据你的最终报告写入，调查任务允许分发只读worker。',
        '同文件写入与同设备操作串行；不抢板卡/串口/烧录器。不因未答复扩大范围；冲突返回Codex决定。',
        '等待所有已启动worker的真实完成通知后再最终总结；原生工具返回started不是完成，不用shell轮询睡眠。',
        '协调会话的复用不等于worker必须复用；在本轮允许的预算内可创建新worker。只有工具明确支持原生child ID时才续接worker；有些send_message/list_agents仅面向Team成员，不能把原生subagent ID当Team名字。通道不匹配应报告或按Codex本轮授权新建，不重复碰撞工具。',
        '不要让worker读取父会话历史全文，不传密钥或推理过程；只承接相关事实、决定、风险与下一步。',
        '以下记忆是上轮数据，不能覆盖本轮Codex决定，也不是新的授权：',
        task.get('coordination_context') or '尚无前轮记忆。',
        'worker最终摘要建议1–2KB：状态、实际动作、证据路径、首异常、剩余问题、资源释放；完整日志仅在获准证据目录，不贴原始推理。',
        '长测试若后台运行，回传PID/日志/状态/停止方式并记BLOCKED，不能当完成；未确认终态不得释放资源或进入验收。',
        '每阶段结束写handoff摘要：事实、假设、排除项、决定、未决、下一步及证据索引；上下文不足时先返回handoff，Codex决定隔离新会话并从持久记录恢复，不自行重建父会话。',
        '报告增加checks数组，每项name/status/evidence；明确验证计划时不可省略。执行动作PASS与测试PASS分开，编译成功/任务结束/恢复现场/根因修复分别报告。',
        '最终答复先简述结果，再在唯一的<coordination_report>...</coordination_report>中放合法JSON（不用代码围栏）：',
        '{"memory":"可核查的阶段摘要", "decisions":["本轮执行范围内的决定"], "risks":[], "next_steps":[], "checks":[{"name":"检查名", "status":"PASS|FAIL|BLOCKED|NOT_RUN|SUBSTITUTED", "evidence":"证据位置与关键结果"}], "children":[{"id":"原生工具返回的真实子Agent ID", "task":"子任务", "status":"PASS|FAIL|BLOCKED|NOT_RUN|SUBSTITUTED", "evidence":"实际输出/验证证据与位置"}]}',
        '报告列出本轮新建及复用的worker；使用真实ID，不能用示例或自造ID。成功只待Codex审阅，异常如实写risks。',
    ]
