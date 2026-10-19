#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' 'Read QUICKSTART.md first: default setup merges global AGENTS with backup; codex-trust reads existing project trust without installing hooks. codex-hook explicitly installs hooks requiring trust. DEEPSEEK_SKIP_CODEX_POLICY=1 skips those changes.'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
python_bin="${PYTHON_BIN:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
"$python_bin" -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
if [[ -f dist/subagent_deepseek-0.9.2-py3-none-any.whl ]]; then
  .venv/bin/python -m pip install --no-deps dist/subagent_deepseek-0.9.2-py3-none-any.whl
else
  .venv/bin/python -m pip install --no-deps .
fi
if [[ ! -e settings.json ]]; then
  (umask 077; cp settings.example.json settings.json)
fi
printf '%s
' 'Installed. Edit settings.json, then add the MCP entry from README.md and reload Codex.'

# Install default coordination policy, keeping existing user instructions and a backup.
if [[ "${DEEPSEEK_SKIP_CODEX_POLICY:-0}" != "1" ]]; then
  codex_dir="${CODEX_HOME:-$HOME/.codex}"
  .venv/bin/python -m subagent_deepseek.workflow --agents-file "$codex_dir/AGENTS.md"
fi

if [[ "${DEEPSEEK_SKIP_CODEX_POLICY:-0}" != "1" ]] && .venv/bin/python -c 'import json,sys; sys.exit(0 if json.load(open("settings.json")).get("workspace_policy", "allowlist") == "codex-hook" else 1)'; then
  codex_dir="${CODEX_HOME:-$HOME/.codex}"
  .venv/bin/python -m subagent_deepseek.workspaces --settings "$PWD/settings.json" --install-hooks "$codex_dir/hooks.json"
fi
