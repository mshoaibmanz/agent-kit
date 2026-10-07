"""The per-user overlay for Python hooks and bqro, under the contract hook-io's kit_env documents."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

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


def kit_root(script: str) -> Path:
    """The kit an entry point (bin/<name>, hooks/<name>) runs from: $AGENT_KIT_DIR, else its folder's
    parent. Links are followed only until a folder holding an agent-setup install (.install-state): a
    dev install links each kit file into its checkout, whose overlay, roles and rendered state are not
    the install's. hooks/lib/kit-root.sh is the shell twin."""
    if os.environ.get("AGENT_KIT_DIR"):
        return Path(os.environ["AGENT_KIT_DIR"])
    path = Path(os.path.abspath(script))
    for _ in range(40):
        kit = Path(os.path.realpath(path.parent)).parent
        if not path.is_symlink() or (kit / ".install-state").is_dir():
            return kit
        path = Path(os.path.join(path.parent, os.readlink(path)))
    raise OSError(f"{script}: link chain too long")


def kit_dir() -> Path:
    """The kit for library code: $AGENT_KIT_DIR, which an entry point sets from kit_root, else the kit
    holding this file."""
    return Path(os.environ.get("AGENT_KIT_DIR") or Path(__file__).resolve().parents[2])


def kit_env_path() -> str:
    """$KIT_ENV, else <config dir>/local/kit.env. Its folder is the overlay (jira-prefs.md and others)."""
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    if os.environ.get("KIT_ENV"):
        return os.environ["KIT_ENV"]
    kit = kit_dir()
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


def layers(path: str | None = None) -> list[Path]:
    """The overlay files kit_env reads, lowest first (a later one wins a key): path (default
    kit_env_path()) alone, or for setup-paths.env the preset's layer and the user's kit.env under it."""
    path = path or kit_env_path()
    if Path(path).name != "setup-paths.env":
        return [Path(path)]
    user_layer = Path(path).with_name("kit.env")
    if not user_layer.is_file():
        config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        user_layer = Path(config) / "local/kit.env"
    return [Path(path).with_name("preset.env"), user_layer, Path(path)]


def parse(text: str) -> dict[str, str]:
    """KEY=value lines, one surrounding quote pair removed; a later line wins."""
    out = {}
    # split("\n"), not splitlines(): that also breaks on \x0b, \x85 and others, which bash's read does not.
    for line in text.split("\n"):
        key, sep, value = line.removesuffix("\r").partition("=")
        if not sep:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def kit_env(path: str | None = None) -> dict[str, str]:
    """Every declared key: its value in the overlay layers (layers(path)), else empty."""
    out = dict.fromkeys(KEYS, "")
    for layer in layers(path):
        try:
            text = layer.read_text()
        except OSError:
            continue
        out.update((key, value) for key, value in parse(text).items() if key in out)
    return out


def code_dirs(values: dict[str, str] | None = None) -> list[str]:
    """The repository parent folders as the overlay writes them (values, else kit_env()): the JSON
    list CODE_DIRS_JSON when it is set, [] included, else the legacy space-separated CODE_DIRS; ~ is
    not expanded. ValueError: a CODE_DIRS_JSON that is not a list of non-empty one-line strings."""
    values = kit_env() if values is None else values
    if not values.get("CODE_DIRS_JSON"):
        return (values.get("CODE_DIRS") or "").split()
    roots = json.loads(values["CODE_DIRS_JSON"])
    if not isinstance(roots, list) or not all(
        isinstance(root, str) and root and not any(c in root for c in "\0\t\n\r") for root in roots
    ):
        raise ValueError("CODE_DIRS_JSON must be a JSON list of folder paths")
    return roots


def code_dir(roots: list[str] | None = None) -> str:
    """{{CODE_DIR}}: the first repository parent folder (roots, else code_dirs(), '' when that is
    invalid), ~ expanded; '' when none is set."""
    if roots is None:
        try:
            roots = code_dirs()
        except ValueError:
            roots = []
    first = next((root for root in roots if isinstance(root, str) and root.strip()), "")
    return os.path.expanduser(first.strip()).rstrip("/") if first else ""


if __name__ == "__main__" and sys.argv[1:2] == ["code-dirs"]:
    # Shell callers (claude-gc): CODE_DIRS_JSON and CODE_DIRS as given, one root per line; exit 1
    # when CODE_DIRS_JSON is invalid.
    try:
        sys.stdout.write("".join(root + "\n" for root in code_dirs(dict(zip(("CODE_DIRS_JSON", "CODE_DIRS"), sys.argv[2:4])))))
    except (ValueError, TypeError):
        sys.exit(1)
