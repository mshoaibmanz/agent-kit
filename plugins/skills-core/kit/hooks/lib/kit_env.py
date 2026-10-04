"""The per-user overlay for Python hooks and bqro, under the contract hook-io's kit_env documents."""

from __future__ import annotations

import os
from pathlib import Path

_HOOK_IO = os.path.join(os.path.dirname(os.path.realpath(__file__)), "hook-io")


def _declared_keys() -> tuple[str, ...]:
    """KIT_KEYS from hook-io, the one declared list."""
    try:
        with open(_HOOK_IO) as fh:
            for line in fh:
                if line.startswith("KIT_KEYS="):
                    return tuple(line.split("=", 1)[1].strip().strip("\"'").split())
    except OSError as e:
        raise ImportError(f"cannot read {_HOOK_IO}: {e}") from e
    raise ImportError(f"{_HOOK_IO} has no KIT_KEYS line")


# Read at import, so a missing or broken hook-io fails the import the callers already guard.
KEYS = _declared_keys()


def kit_env_path() -> str:
    """$KIT_ENV, else <config dir>/local/kit.env. Its folder is the overlay (jira-prefs.md and others)."""
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    if os.environ.get("KIT_ENV"):
        return os.environ["KIT_ENV"]
    kit = Path(os.environ.get("AGENT_KIT_DIR") or Path(__file__).resolve().parents[2])
    for candidate in (kit / "local/setup-paths.env", kit / "local/kit.env"):
        if candidate.is_file():
            return str(candidate)
    return os.path.join(config, "local", "kit.env")


def work_root() -> str:
    """The one work-root resolver: CLAUDE_OUT_ROOT (tests), else the overlay's AGENT_WORK_ROOT, else
    ~/agent-work, else ~/claude-scratch (its old name) when only that exists."""
    raw = os.environ.get("CLAUDE_OUT_ROOT") or kit_env()["AGENT_WORK_ROOT"]
    if raw:
        return os.path.expanduser(raw)
    new, old = os.path.expanduser("~/agent-work"), os.path.expanduser("~/claude-scratch")
    return old if not os.path.exists(new) and os.path.isdir(old) else new


def kit_env() -> dict[str, str]:
    """Every declared key: its value in kit_env_path(), else empty."""
    out = dict.fromkeys(KEYS, "")
    path = kit_env_path()
    paths = [path]
    if Path(path).name == "setup-paths.env":
        user_layer = Path(path).with_name("kit.env")
        if not user_layer.is_file():
            config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
            user_layer = Path(config) / "local/kit.env"
        paths = [str(user_layer), path]
    # split("\n"), not splitlines(): that also breaks on \x0b, \x85 and others, which bash's read does not.
    for overlay in paths:
        try:
            text = Path(overlay).read_text()
        except OSError:
            continue
        for line in text.split("\n"):
            key, sep, value = line.removesuffix("\r").partition("=")
            if not sep or key not in out:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[key] = value
    return out
