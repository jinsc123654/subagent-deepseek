"""Private Codex auth-command helper. Never run directly for diagnostics."""
import json
import os
from pathlib import Path
import stat
import sys


def read_key(path: Path, kind: str) -> str:
    """Read a user-owned private regular file without following a final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Credential must be a private regular file owned by this user')
        text = stream.read(65537)
    if len(text) > 65536:
        raise ValueError('Credential file is too large')
    if kind == 'dsh':
        import yaml
        data = yaml.safe_load(text)
        refs = data.get('refs') if isinstance(data, dict) else None
        key = refs.get('DEEPSEEK_API_KEY') if isinstance(refs, dict) else None
    else:
        key = text.strip()
    if not isinstance(key, str) or not key.startswith('sk-') or any(c.isspace() for c in key):
        raise ValueError('No valid DeepSeek API key found')
    return key


def main() -> None:
    """Print the key only to the private runtime auth pipe; sanitize failures."""
    try:
        data = json.loads(Path(os.environ['SUBAGENT_DEEPSEEK_SETTINGS']).read_text())
        sys.stdout.write(read_key(Path(data['credential_file']), data.get('credential_type', 'file')))
    except Exception:
        sys.exit('Cannot read DeepSeek credential; check credential file permissions and format')


if __name__ == '__main__':
    main()
