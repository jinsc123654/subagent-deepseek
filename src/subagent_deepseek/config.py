"""Deployment settings and validation; credentials never enter tool arguments."""
from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlsplit

MODES = ('read-only', 'workspace-write', 'danger-full-access')


@dataclass(frozen=True)
class Settings:
    """Settings fixed by the deployment, never selected by the child model."""
    codex_binary: str
    credential_file: str
    allowed_roots: tuple[str, ...] = ()
    credential_type: str = 'file'
    model: str = 'deepseek-flash'
    reasoning_effort: str = 'high'
    mode: str = 'read-only'
    timeout_seconds: float = 300
    shutdown_grace_seconds: float = 3
    max_concurrency: int = 2
    harness_url: str | None = None
    harness_launch_log: str | None = None
    harness_provider: str = 'deepseek-account' 

    workspace_policy: str = 'allowlist'
    workspace_registry: str | None = None
    codex_config_file: str | None = None

    @classmethod
    def load(cls, path: Path) -> 'Settings':
        """Load a JSON deployment file and reject invalid or unknown options."""
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError('Settings must be a JSON object')
        policy = raw.get('workspace_policy', 'allowlist')
        if policy not in ('allowlist', 'codex-hook', 'codex-trust'):
            raise ValueError('Invalid workspace_policy')
        roots = raw.get('allowed_roots', [])
        if not isinstance(roots, list) or (not roots and policy == 'allowlist') or not all(isinstance(r, str) for r in roots):
            raise ValueError('allowed_roots must be a nonempty list of absolute directories')
        raw['allowed_roots'] = tuple(roots)
        if policy == 'codex-hook' and not raw.get('workspace_registry'):
            raw['workspace_registry'] = str(path.resolve().parent / 'codex-workspaces.json')
        if raw.get('workspace_registry') and not Path(raw['workspace_registry']).is_absolute():
            raise ValueError('workspace_registry must be absolute')
        if raw.get('codex_config_file') and not Path(raw['codex_config_file']).is_absolute():
            raise ValueError('codex_config_file must be absolute')
        if policy == 'codex-trust':
            from .workspaces import codex_config_path
            raw['codex_config_file'] = str(codex_config_path(raw.get('codex_config_file')))
        result = cls(**raw)
        for name in ('codex_binary', 'credential_file'):
            p = Path(getattr(result, name))
            if not p.is_absolute() or not p.is_file():
                raise ValueError(f'{name} must name an existing absolute file')
        for root in result.allowed_roots:
            if not Path(root).is_absolute() or not Path(root).is_dir():
                raise ValueError('allowed_roots must contain existing absolute directories')
        if result.credential_type not in ('file', 'dsh') or result.mode not in MODES:
            raise ValueError('Invalid credential_type or mode')
        if result.model != 'deepseek-flash' or result.reasoning_effort != 'high':
            raise ValueError('This release supports deepseek-flash with high reasoning only')
        for name in ('timeout_seconds', 'shutdown_grace_seconds'):
            value = getattr(result, name)
            if type(value) not in (float, int) or not 0 < value <= 3600:
                raise ValueError(f'{name} must be positive and no greater than 3600')
        if type(result.max_concurrency) is not int or not 1 <= result.max_concurrency <= 16:
            raise ValueError('max_concurrency must be an integer from 1 to 16')
        if result.harness_url is not None:
            url = urlsplit(result.harness_url)
            if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
                raise ValueError('harness_url must be a plain loopback HTTP origin')
            if not result.harness_launch_log or not Path(result.harness_launch_log).is_absolute():
                raise ValueError('harness_launch_log must be an absolute launch-log path')
            if result.harness_provider not in ('deepseek-account', 'deepseek-official'):
                raise ValueError('Unsupported Harness provider')
        return result

    def directory(self, cwd: str) -> Path:
        """Resolve symlinks before checking whether a task directory is allowed."""
        p = Path(cwd)
        if not p.is_absolute() or not p.is_dir():
            raise ValueError('cwd must be an existing absolute directory')
        p = p.resolve()
        permitted = [Path(root).resolve() for root in self.allowed_roots]
        if self.workspace_policy == 'codex-hook':
            from .workspaces import roots
            permitted.extend(roots(self.workspace_registry))
        if self.workspace_policy == 'codex-trust':
            from .workspaces import project_trust, WorkspaceAccessError
            trust = project_trust(p, self.codex_config_file)
            if trust == 'untrusted':
                raise WorkspaceAccessError('Codex explicitly marks this project untrusted; DeepSeek access refused.')
            if trust == 'trusted':
                return p
        if not any(p.is_relative_to(root) for root in permitted):
            from .workspaces import WorkspaceAccessError
            raise WorkspaceAccessError('Project directory is not authorized. In codex-trust mode, trust the project in Codex; otherwise register the trusted workspace hook or configure an explicit allowed root.')
        return p
