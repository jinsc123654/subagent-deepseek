# 可选 Harness 网页补丁

适用基线：DeepSeek Harness git commit `571902028a75c64c1d767f4ae5fd099485f0ddf0`。
需要 Harness 源码开发环境及其 pnpm/Node 依赖。先备份本地修改，在 Harness 仓库中运行：

```bash
git apply --check /absolute/path/to/harness-ui/codex-group.patch
git apply /absolute/path/to/harness-ui/codex-group.patch
pnpm run build
pnpm run build:check
```

若 check 失败请停止；不同版本需手动适配，不要强制覆盖。补丁只修改网页侧栏及相关测试。
构建后刷新网页。已有服务若未启用资源更新，需要先等待任务结束再正常重启。
新组跨项目聚合 [Codex] 标题会话，不迁移工作区，不修改 Host 的全局权限。
组内按最近活动排序，可折叠，暂不支持手动拖动排序。
