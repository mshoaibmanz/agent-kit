"""The per-user overlay for Python hooks, parsed exactly as hook-io's kit_env parses it."""

from __future__ import annotations

import os
import re

_KEY = re.compile(r"[A-Z][A-Z0-9_]*")


def kit_env() -> dict[str, str]:
    """KEY=value pairs from $KIT_ENV, else <config dir>/local/kit.env; empty when there is none."""
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    path = os.environ.get("KIT_ENV") or os.path.join(config, "local", "kit.env")
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if not sep or not _KEY.fullmatch(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out
