"""Project scopes reported by explicitly trusted Codex lifecycle hooks, not model arguments."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import time
import stat
import tomllib
from .workflow import atomic, locked

EVENTS = ('SessionStart', 'UserPromptSubmit', 'SessionEnd')
TTL = 86400


def private_read(path):
    if path.is_symlink():
        raise ValueError('Workspace registry cannot be a symlink')
    if not path.exists():
        return {'version': 1, 'sessions': {}}
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Workspace registry must be private and user-owned')
    data = json.loads(path.read_text())
    if data.get('version') != 1 or not isinstance(data.get('sessions'), dict):
        raise ValueError('Invalid workspace registry')
    return data


def roots(path):
    """Read live local hook scopes; absent or expired registration never grants access."""
    if not path:
        return ()
    data = private_read(Path(path))
    result = []
    for record in data['sessions'].values():
        if time.time() - record['updated_at'] < TTL:
            p = Path(record['cwd'])
            # A root replaced by a different symlink after registration is not retained.
            if p.is_absolute() and p.is_dir() and str(p.resolve()) == str(p):
                result.append(p)
    return tuple(result)


class WorkspaceAccessError(ValueError):
    """Safe, specific directory refusal suitable for a tool error."""


def codex_config_path(value=None):
    if value:
        path = Path(value)
    else:
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        path = home / 'config.toml'
    if not path.is_absolute():
        raise WorkspaceAccessError('Codex trust source must be an absolute config.toml path.')
    return path


def project_trust(cwd, config_file=None):
    """Read user-level Codex projects trust on every call; never write/copy trust entries.

    Only the most specific matching entry applies. Explicit untrusted children override
    trusted parents (and manual compatibility roots). Root/home trust entries are ignored.
    Trust is a directory eligibility decision, never a filesystem sandbox permission.
    """
    path = codex_config_path(config_file)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise WorkspaceAccessError('Cannot read Codex trust configuration; directory access refused.') from None
    try:
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise WorkspaceAccessError('Codex trust configuration must be a user-owned regular file without group/world write access.')
            raw = source.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('size')
        data = tomllib.loads(raw.decode('utf-8'))
        projects = data.get('projects', {})
        if not isinstance(projects, dict):
            raise ValueError('projects')
    except (ValueError, UnicodeError, OSError):
        raise WorkspaceAccessError('Codex trust configuration is invalid or unreadable; directory access refused.') from None
    target = Path(cwd).resolve()
    matches = []
    for name, entry in projects.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        level = entry.get('trust_level')
        root = Path(name)
        if level not in ('trusted', 'untrusted') or not root.is_absolute():
            continue
        try:
            canonical = root.resolve()
            if not root.is_dir() or canonical in (Path('/'), Path.home().resolve()):
                continue
            # A newly retargeted symlink must not transfer a trusted project's grant.
            if level == 'trusted' and root != canonical:
                continue
            if target.is_relative_to(canonical):
                matches.append((len(canonical.parts), level))
        except (OSError, RuntimeError):
            continue
    if not matches:
        return None
    nearest = max(depth for depth, _ in matches)
    levels = [level for depth, level in matches if depth == nearest]
    return 'untrusted' if 'untrusted' in levels else 'trusted'


def record(path, event, actual_cwd=None):
    kind = event.get('hook_event_name')
    sid = event.get('session_id')
    if kind not in EVENTS or not isinstance(sid, str) or not 0 < len(sid) <= 200:
        raise ValueError('Invalid Codex lifecycle input')
    cwd = event.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError('Invalid workspace directory')
    directory = Path(cwd).resolve()
    if directory != Path(actual_cwd or Path.cwd()).resolve():
        raise ValueError('Hook cwd differs from process working directory')
    if directory == Path('/') or directory == Path.home().resolve():
        raise ValueError('Refusing whole-filesystem or whole-home registration')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path.with_name(path.name + '.lock')):
        data = private_read(path)
        data['sessions'] = {k:v for k,v in data['sessions'].items() if time.time()-v['updated_at'] < TTL}
        if kind == 'SessionEnd':
            data['sessions'].pop(sid, None)
        else:
            data['sessions'][sid] = {'cwd': str(directory), 'updated_at': time.time()}
        atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def install(hooks_path, settings_path, python):
    """Merge new hook handlers without trusting them or replacing existing handlers."""
    hooks_path = Path(hooks_path)
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    with locked(hooks_path.with_name('.deepseek-hooks.lock')):
        if hooks_path.is_symlink():
            raise ValueError('Hook configuration cannot be a symlink')
        previous = hooks_path.read_text() if hooks_path.exists() else None
        data = json.loads(previous) if previous else {}
        hooks = data.setdefault('hooks', {})
        command = shlex.join([str(python), '-m', 'subagent_deepseek.workspaces', '--settings', str(settings_path)])
        for kind in EVENTS:
            groups = hooks.setdefault(kind, [])
            # Update only our own handlers; leave all other hooks untouched.
            for group in groups:
                group['hooks'] = [h for h in group.get('hooks', []) if ' -m subagent_deepseek.workspaces --settings ' not in h.get('command', '')]
            groups[:] = [g for g in groups if g.get('hooks')]
            groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]})
        text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
        if text != previous:
            if previous is not None:
                from uuid import uuid4
                atomic(hooks_path.with_name(hooks_path.name + '.before-deepseek-' + uuid4().hex[:8]+'.bak'), previous)
            atomic(hooks_path, text)
    return str(hooks_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--settings', required=True, type=Path)
    parser.add_argument('--install-hooks', type=Path)
    args = parser.parse_args()
    if args.install_hooks:
        config = json.loads(args.settings.read_text())
        if 'workspace_policy' not in config:
            from uuid import uuid4
            atomic(args.settings.with_name(args.settings.name + '.before-workspace-' + uuid4().hex[:8] + '.bak'), args.settings.read_text())
            config['workspace_policy'] = 'codex-hook'
            config.setdefault('workspace_registry', None)
            atomic(args.settings, json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        print('Hook definitions installed; review and trust them in Codex:', install(args.install_hooks, args.settings.resolve(), sys.executable))
        return
    try:
        settings = json.loads(args.settings.read_text())
        path = settings.get('workspace_registry') or str(args.settings.resolve().parent/'codex-workspaces.json')
        event = json.loads(sys.stdin.read(65537))
        record(path, event)
    except Exception:
        # Hook failure does not grant a directory and does not expose input/transcripts.
        print('DeepSeek project registration failed; check setup before delegation.', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
