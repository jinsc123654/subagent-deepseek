# subagent-deepseek 0.9.2：先读我

## 直接对Codex说

> 帮我识别本机配置，不显示密钥；你负责目标和最终审查，将工作包交给一个DeepSeek协调Agent，由它拆分给下层Agent分析、实现、测试并承接记忆，持续维护项目进度。

无需强制填写HTML。先按README完成安装、自己的密钥文件路径、MCP注册和Harness连接。
默认project_dispatch进入协调模式；没有Harness不会偷偷退化为独立执行。显式role=worker才是无下层的叶子模式。

## 一轮工作

1. Codex确定目标、输入版本、允许文件、禁止操作、task_kind和验收标准，调用project_board记录。
2. project_dispatch(role=coordinator)下发；DeepSeek使用原生subagent创建worker，给每个worker完整范围和不再委派的约束。
3. DeepSeek等待真实完成通知并汇总，返回实际worker ID与逐项证据、记忆、风险和下一步。
4. project_collect从原生父子目录独立观察子任务，写入DEEPSEEK_COORDINATION.md。project_coordination可读取。
5. Codex独立复核，成功才标done；未完成或需变更时给同一协调会话下一修订，记忆自动带入。

全套规则见THREE-LAYER-WORKFLOW.md。仅测试不能修改源码、重建/换镜像或恢复撤回方案。设备资源不能并发争抢。

## 首次信任与配置影响

install.sh默认备份合并全局AGENTS；仅显式codex-hook模式安装Hook且不代批信任。新示例用codex-trust，旧设置不自动迁移。
不想改全局文件时用DEEPSEEK_SKIP_CODEX_POLICY=1。跳过后仍按所选目录策略检查；codex-trust可读取已有Codex信任，codex-hook需可信登记，allowlist需显式目录。
配置占位路径/absolute/path/...不是实际文件；请使用自己机器的路径，密钥文件权限600、格式file或dsh。
即使仅使用网页，当前加载器仍需有效的codex_binary和credential_file。Codex/Harness/Node/pnpm需自行安装。

## Harness未启动

主Agent只有在用户已授权自动启动且启动器已确认时才启动。现成仪表盘可能用bash '/绝对路径/网页启动.sh' start；
配置页生成的脚本则直接bash '/绝对路径/网页启动-配置生成.sh'并保持终端打开。不要混用参数。
核对日志与harness_launch_log一致，启动后认证并查状态。401仅证明有HTTP服务响应，不证明服务身份。
端口占用不杀进程，超时不反复拉起，含糊提交先查询不重复发任务。不输出令牌URL、Cookie或完整私有日志。

## 限制与排查

- 三层是真实外部MCP/Harness协作，不是Codex原生子Agent，也不是强制角色安全隔离。
- worker不再委派主要靠提示和Host深度设置；桥接发现第四层后拒绝验收，不能声称提前阻止了创建。
- max_workers限制新建worker数量；原生已有worker可继续。返回started不是完成，发出中断也不是已释放资源。
- 协调记忆只在collect/任务回执时持久化，无后台连续写入；会话中途原生历史仍由Harness保存。
- 默认仅处理本工作包的有界原生子树；树观测超限、未知/外部子任务会记受阻，不能按部分结果验收。
- 新接口未显示：重载MCP，必要时重启Codex。角色切换需reuse_session=false，避免带入旧worker约束。
- 项目未授权：codex-trust检查Codex用户级projects是否trusted；codex-hook检查Hook登记；allowlist检查显式目录。不要靠扩大根目录绕过。
- direct默认只读；Harness父模式approve-for-me/full-access会映射完全访问，必须符合用户授权。
- 协调报告格式错误、子任务ID不匹配或未完成时进入blocked；先查原生任务再继续，不伪造记录。
- 子任务返回FAIL/BLOCKED/NOT_RUN/SUBSTITUTED时，不将整个工作包当作已验收；Codex决定补测或修订。
- worker、协调Agent与真实调用可能产生明显模型用量；任务要有界、限制输出，不承诺免费。
- 安全扫描不完整、部分测试、替代条件与根因修复分别记录，不能互相替代。

私有settings、凭据、认证文件、运行日志、项目规划和协调记忆不要放入分享包。以README和实际验证记录为准。

## 已验证的工具兼容限制

某些Harness实例的send_message/list_agents属于Team系统，不能把原生subagent ID当Team成员名。协调会话可由桥接稳定续接；worker是否能续接取决于实际工具。不要混用ID。可由Codex在下一修订允许新建worker，协调记忆仍保留。


## v0.9 验证范围与证据交接

参见 [TASK-HANDOFF.md](TASK-HANDOFF.md)：结构化验证计划、证据目录、有界摘要和会话恢复。保留三层；不测试/仅记录方法为 NOT_RUN，最终仍由 Codex 验收。

## v0.9.1 跟随 Codex 项目信任

默认示例采用 `workspace_policy: "codex-trust"`，读取 `$CODEX_HOME/config.toml`（未设置时 `~/.codex/config.toml`）中的 `[projects."/绝对项目路径"] trust_level="trusted"`。此为 Codex 官方项目信任字段，参考 https://learn.chatgpt.com/docs/config-file/config-reference 。可用 `codex_config_file` 指定用户级配置绝对路径；留空在加载设置时确定默认来源。不会读取项目自己的 `.codex/config.toml` 作为授权来源，也不修改 Codex 信任文件。

每次调用重新核查：已信任项目及其子目录可用；最具体的子项目记录优先；明确 `untrusted` 拒绝，即使它位于手动 allowed_roots 内；删除信任后不保留授权副本。已有 allowed_roots 仍作为显式兼容授权，所以若同一路径另有手动授权，删除信任记录本身不会撤销那条独立授权；明确 untrusted 会拒绝。未信任项目、邻接目录、符号链接逃逸和整机/整个home信任项不授予访问。

信任配置须为当前用户拥有的普通文件，不允许组/其他用户写入或文件符号链接；格式错误无法验证时拒绝。目录信任仅是起始目录资格，**不等于全盘读写、不改变沙箱、不替代当前任务授权**；Harness仍按当前codex_permission同步执行权限。全局/云端/系统配置、临时CLI覆盖或仅UI状态不在本版解析范围，未落入此用户级信任来源时不会猜测放行。

无需启用工作区Hook；安装器仅在显式选择 `codex-hook` 时安装该Hook。`allowlist`、`codex-hook` 保留原语义，旧设置不会自动迁移，需明确选择codex-trust。现有安装明确选择codex-trust后，重载MCP生效。新版本将目录授权错误单独报告；不存在项目记录时read返回空任务，不创建或修改项目文件。
