# Sources and attribution

- Lifecycle/protocol design reference: DeepSeek Harness
  `packages/subagent/subagent-codex` (`src/wire.ts`, `src/run.ts`).
  The bridge is an independent Python implementation, not a renamed DSH plugin.
- `src/subagent_deepseek/models.json` is copied unchanged from
  `deepseek-mcp-linux.zip` supplied by the user. Its package carries the included
  MIT license, copyright (c) 2026 oil-oil. The model catalog is a snapshot; it is
  not dynamically discovered or independently authenticated by this bridge.
- MCP SDK (`mcp`, MIT), PyYAML (MIT), and their transitive dependencies are
  installed separately; their own licenses apply.
- Codex is an external runtime, not redistributed in this package.
