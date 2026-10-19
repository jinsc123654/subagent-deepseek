"""Explicit compatibility probe and optional billable file-read acceptance test."""
import argparse
import asyncio
from pathlib import Path
import secrets
import tempfile

from .config import Settings
from .runtime import delegate


async def check(settings: Settings, path: Path, cwd: str, live: bool) -> None:
    """Verify app-server metadata, optionally requesting a random file read."""
    if not live:
        result = await delegate(settings, path, 'Compatibility probe', cwd, handshake_only=True)
        print('App-server handshake, ephemeral thread and route metadata: passed')
        print('No model request sent; credentials and API access remain untested.')
        return
    root = settings.directory(cwd)
    with tempfile.TemporaryDirectory(prefix='.subagent-deepseek-test-', dir=root) as name:
        token = secrets.token_hex(24)
        file = Path(name) / 'challenge.txt'
        file.write_text(token)
        result = await delegate(settings, path,
            f'Read the local file {file} using your shell tool. Return only its exact content.', cwd)
        if result['answer'].strip() != token:
            raise RuntimeError('Live file-read verification failed')
    print('Live file read and DeepSeek route metadata: passed')


def main() -> None:
    """Run diagnostics without printing credentials or raw runtime logs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True, type=Path)
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--live', action='store_true', help='Send a billable DeepSeek file-read task')
    args = parser.parse_args()
    path = args.settings.expanduser().resolve()
    asyncio.run(check(Settings.load(path), path, args.cwd, args.live))


if __name__ == '__main__':
    main()
