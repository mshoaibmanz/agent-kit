"""Read only configured provider command paths; never execute configuration."""
from __future__ import annotations
import json
from pathlib import Path
import sys


def command(root: Path, provider: str) -> str:
    host = {'anthropic': 'claude', 'openai': 'codex'}.get(provider, provider)
    path = root / 'local/provider-paths.json'
    if not path.is_file():
        return host
    value = json.loads(path.read_text()).get(host, host)
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValueError('Provider path must be a nonempty single-line string')
    return value


if __name__ == '__main__':
    sys.stdout.write(command(Path(sys.argv[1]), sys.argv[2]))
