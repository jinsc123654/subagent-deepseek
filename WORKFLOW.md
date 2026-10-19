# 三层Agent协作协议（替代旧版两层禁止委派规则）

默认结构：Codex顶层审查 → DeepSeek协调层 → 分析/实现/测试worker。
Codex负责目标、顶层方案、授权边界和最终验收；DeepSeek协调层承接阶段记忆，拆分有界子任务，使用Harness原生subagent创建第三层，调度并汇总。worker执行具体任务，不创建第四层。

1. 开始先读取/初始化DEEPSEEK_PROJECT.md；下发project_dispatch默认role=coordinator，需Harness。明确cwd、task_kind、allowed_files、constraints、baseline、checks和decision。中间层在授权范围内自行拆分，不逐步让用户转述。
2. 协调层必须真实调用原生subagent并等待worker结果；不能用自行执行或虚构ID冒充三层。每个worker收到完整目标、父工作包/修订、类型、版本、范围和证据要求。默认最多每轮4个，可配置1–8个。
3. 只有协调层可以分发；worker禁止继续委派。调用方明确role=worker时是叶子模式，可用于direct执行。协调模式不静默降级为direct或单Agent。
4. 调查任务只读；仅测试不修改代码/重建/换镜像/恢复已撤回方案；实现仅改允许文件。中间层及worker都不改顶层规划、AGENTS、验收标准或安全策略。冲突交Codex决定，没回复不是同意。
5. 同文件写入或同板卡/串口/烧录资源串行。停止要核对终态，不重复含糊提交。提交/推送/烧录遵守用户实际授权，不因已有示例就获得权限。
6. 协调层按格式返回阶段摘要和真实worker ID、逐项PASS/FAIL/BLOCKED/NOT_RUN/SUBSTITUTED、证据、异常顺序、风险和下一步。桥接从原生父子目录独立核验，未完成、未知子任务或第四层会阻塞验收。
7. DEEPSEEK_COORDINATION.md保存各修订的协调记忆、子任务关联与报告，由桥接维护。下次同工作包分发带入相关记忆，避免复制全部历史；用户/协调层摘要不是新授权，不能覆盖Codex当前决定。用project_coordination读取，用project_collect刷新。
8. DEEPSEEK_PROJECT.md仍由Codex维护顶层规划、Bug、决定、撤回方案、进度和最终证据。协调层报告成功只进入review，Codex独立核对diff、关键原始日志或测试后才done。
9. 同一工作包优先沿用同一空闲协调会话；角色切换需显式reuse_session=false，隔离旧角色约束。程序保存修订与历史，旧结果不能覆盖新修订。
10. MCP提示不是强制沙箱或可信角色认证，实际目录/权限仍由配置和运行时控制。原生Host深度限制才是递归的运行时守卫；桥接发现第四层则记录异常，不能声称已阻止它创建。
11. 可不填HTML，由Codex按授权识别配置；Harness未运行时只在用户已有启动授权且启动器确认后拉起，不杀端口占用者，不绕过Hook信任。当前项目登记仍需受信任Hook，不自动扩大目录白名单。
12. 不记录密钥、Cookie、私有登录URL或原始推理。用户明确指定工具、模型、禁止委派或变更范围时，以用户为准。配置/服务不可用如实记录，不伪造完成。
