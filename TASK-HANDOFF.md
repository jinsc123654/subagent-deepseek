# 三层工作包、验证与交接（v0.9）

参考 single-conversation-subagent-skill 1.0.2 的验证范围、分层记忆和有界回传，改写为三层：**Codex 顶层决策/审阅 → DeepSeek 协调与阶段记忆 → 原生分析/实现/测试 worker**。只有 worker 禁止继续委派。保留真实父子关系核验，不能照搬两层协议的“主对话直接执行全部协调”或替换为普通 Agent 工具。

## 任务入口

Codex 按任务选择快速处理、标准执行、长任务、只调查；目标或验证范围不清楚时，给一个推荐选项并询问一次，同任务不重复。用户已明确范围时直接执行。纯问答无需创建 Agent。标准/长任务先明确验证范围，再下发工作包。可推荐“本轮快速冒烟 + 收尾定向回归”；不把示例中的板卡 A、2 分钟、15 分钟等当成本机事实或默认授权。全量、稳定性、烧录和擦除需要实际授权。

## 分发契约

project_dispatch 新增：

```json
{
  "validation": {"mode":"smoke","timing":"this-round","budget_minutes":2,"resources":[]},
  "evidence_path":"docs/agent/evidence"
}
```

mode 为 inherit（兼容旧 brief/checks）、smoke、targeted、full、stability、none、method-only。timing 为 this-round 或 final；final 表示本轮暂不执行，收尾时由 Codex 用新修订选择 this-round。budget_minutes=0 表示未指定预算，不表示自动无限运行；资源清单也不是授权。none/method-only 预算必须为0，二者互斥，检查只能 NOT_RUN；method-only 在获准范围记录命令、环境和预期结果，不能声称测试通过。

一个修订选择一种验证模式；“本轮冒烟 + 收尾回归”用两个修订，避免每轮重复长测。代码实现与检查结果分开：worker 可以完成实现而检查 NOT_RUN；仅有编译成功不能证明根因修复。

证据路径必须项目相对、不能 .. 或通过符号链接逃出项目。桥接生成 `<项目>/docs/agent/evidence/<任务ID>/R<修订>/`，worker 使用独立子目录。桥接不会自动建立目录、扩大 allowed_files 或授予写入权限。只读调查保持只读，用已有文件/输出作为证据；需要落盘时 Codex 显式授权。

## 回传和验收

worker 返回建议1–2KB：状态、实际动作、证据位置、首异常、剩余问题、资源释放。完整日志留在获准证据文件；不贴密钥、Cookie、私有URL或原始推理。协调层等待真实 worker 完成，汇总真实 ID 和父子关系，并在 coordination_report 增加：

```json
"checks": [{"name":"单元检查","status":"PASS","evidence":"证据路径与关键结果"}]
```

显式指定验证模式时 checks 必填；PASS 必须有证据。none/method-only 或 deferred final 检查一律 NOT_RUN，伪报 PASS 阻塞收集。FAIL/BLOCKED/SUBSTITUTED 检查进入 blocked，由 Codex 决定；明确跳过的 NOT_RUN 可入 review，但不是测试成功。所有结果只待审阅，Codex 独立检查 diff、关键日志、测试和产物后更新 done。

## 记忆与恢复

- DEEPSEEK_PROJECT.md：Codex 维护目标、决定、撤回方案、进度及最终证据。
- DEEPSEEK_COORDINATION.md：桥接维护修订、协调记忆、真实 worker 关联、验证计划和证据索引。
- docs/agent/evidence/：获准时保存完整日志/报告，与前两者隔离。

每阶段结束提供 handoff：事实、假设、排除项、决定、未决、下一步和证据索引。顶层只保留摘要，按需查看关键证据。上下文不足先保存交接，由 Codex 决定 reuse_session=false 隔离新会话，再读取项目与协调记录恢复；不自动重启或复制全部历史，不宣称精确知道上下文百分比。

长测试可在明确授权的进程管理方式下后台运行，但须记录 PID、日志、状态、停止方式和设备占用；未确认终态记 BLOCKED，不把返回 PID 当完成。当前桥接不提供进程监控器或自动后台唤醒，仍需主动收集，不能用原生 worker 已结束代替后台作业已结束。

## 与参考包的差异

保留三层、原生 Harness subagent、现有受管 Markdown 和审阅门槛。不自动安装参考技能、不替换全局 AGENTS、不强制每次问答先四选一，不引入假定硬件或固定耗时。新字段约束提示与审计记录，不是执行沙箱；角色、路径、测试结论仍需实际运行时和 Codex 证据核对。

显式 role=worker 的叶子兼容模式仍由 Codex 人工核验检查结论；上述 checks 自动核验针对默认协调模式。

## 15秒模式倒计时

新任务创建工作包后调用 project_intake(start)，向用户展示四种模式并提示15秒无操作默认标准执行。异步提问期间保持当前轮运行，到期读project_intake(read)，再按结果继续；用户明确选择用select记录。重复start不重置倒计时。模式记录在项目私有 .deepseek-intake.json，按工作包持久化，重启也不会重新倒计时。状态在下次读取时结算，工具不会后台唤醒Codex或自动执行任务。Codex原生提问框的实时数字/按钮不由本包控制；显示文字截止时间即可。

此超时默认只授予“标准执行”模式，测试范围和权限另按用户当前授权；不能把超时当烧录/推送/长测确认。用户迟到的明确选择优先，Codex先检查活动修订再调整。已明确模式直接select，不重复询问。本次用户已要求继续改造，所以不重新计时。

默认协调模式 project_collect 仅回传最多2000字符的阶段记忆、worker ID/状态和检查状态，完整证据与handoff保存在协调MD。按需 project_coordination 或 project_collect(include_details=true) 查看全文，避免默认重复传answer和报告。调查建议仅适用于其记录基线；后续Codex修改和最新用户决定优先。
