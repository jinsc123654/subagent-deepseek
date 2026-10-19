# Harness 网页桥接

配置与接口说明见 README.md。只操作由本桥接创建且登记的会话，认证经 Host 官方 /connect；
不会抓取网页 DOM，也不会创建伪造的原生 subagent 关系。最终结果仅返回文本，不返回推理或原始工具记录。
结果等待最多 60 秒；状态和最终输出按需查询；网络提交结果不确定时先查询已有会话，避免重复执行。
同一 MCP 进程中的追加消息按会话串行提交；用户也可在网页输入，因此最终答案不保证只对应某条追加消息。
foreground 权限审批和 open 问题留在网页；continued 问题可通过 harness_answer 按用户明确答案提交。

## v0.9.1 跟随 Codex 项目信任

默认示例采用 `workspace_policy: "codex-trust"`，读取 `$CODEX_HOME/config.toml`（未设置时 `~/.codex/config.toml`）中的 `[projects."/绝对项目路径"] trust_level="trusted"`。此为 Codex 官方项目信任字段，参考 https://learn.chatgpt.com/docs/config-file/config-reference 。可用 `codex_config_file` 指定用户级配置绝对路径；留空在加载设置时确定默认来源。不会读取项目自己的 `.codex/config.toml` 作为授权来源，也不修改 Codex 信任文件。

每次调用重新核查：已信任项目及其子目录可用；最具体的子项目记录优先；明确 `untrusted` 拒绝，即使它位于手动 allowed_roots 内；删除信任后不保留授权副本。已有 allowed_roots 仍作为显式兼容授权，所以若同一路径另有手动授权，删除信任记录本身不会撤销那条独立授权；明确 untrusted 会拒绝。未信任项目、邻接目录、符号链接逃逸和整机/整个home信任项不授予访问。

信任配置须为当前用户拥有的普通文件，不允许组/其他用户写入或文件符号链接；格式错误无法验证时拒绝。目录信任仅是起始目录资格，**不等于全盘读写、不改变沙箱、不替代当前任务授权**；Harness仍按当前codex_permission同步执行权限。全局/云端/系统配置、临时CLI覆盖或仅UI状态不在本版解析范围，未落入此用户级信任来源时不会猜测放行。

无需启用工作区Hook；安装器仅在显式选择 `codex-hook` 时安装该Hook。`allowlist`、`codex-hook` 保留原语义，旧设置不会自动迁移，需明确选择codex-trust。现有安装明确选择codex-trust后，重载MCP生效。新版本将目录授权错误单独报告；不存在项目记录时read返回空任务，不创建或修改项目文件。
