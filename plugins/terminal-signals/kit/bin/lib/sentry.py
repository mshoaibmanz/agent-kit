"""Replace Sentry credential transport while preserving documented non-secret options."""

from __future__ import annotations
from pathlib import Path
from typing import Any

SAFE_ENV = frozenset(("SENTRY_HOST", "MCP_URL", "MCP_SKILLS", "MCP_DISABLE_SKILLS", "EMBEDDED_AGENT_PROVIDER"))
TOKEN_OPTIONS = frozenset(("--access-token", "--token"))


def safe_sentry(spec: dict[str, Any], wrapper: Path) -> dict[str, Any]:
    if set(spec) - {"command", "args", "env"}:
        raise ValueError("Unsupported Sentry transport fields; use an explicit runtime wrapper")
    command = spec.get("command")
    if not isinstance(command, str) or Path(command).name not in ("npx", "sentry-mcp"):
        raise ValueError("Unsupported Sentry launcher; expected npx or the Sentry runtime wrapper")
    raw = spec.get("args", [])
    if not isinstance(raw, list) or not all(isinstance(arg, str) for arg in raw):
        raise ValueError("Sentry args must be strings")
    index = 0
    if Path(command).name == "npx":
        if raw and raw[0] in ("-y", "--yes"):
            index = 1
        if index >= len(raw) or not (
            raw[index] == "@sentry/mcp-server" or raw[index].startswith("@sentry/mcp-server@")
        ):
            raise ValueError("Unsupported Sentry npx package; expected @sentry/mcp-server")
        index += 1
    args = []
    while index < len(raw):
        arg = raw[index]
        if arg in TOKEN_OPTIONS:
            if index + 1 >= len(raw) or raw[index + 1].startswith("--"):
                raise ValueError("Sentry token option has no value")
            index += 2
            continue
        if any(arg.startswith(option + "=") for option in TOKEN_OPTIONS):
            if not arg.split("=", 1)[1]:
                raise ValueError("Sentry token option has no value")
            index += 1
            continue
        args.append(arg)
        index += 1
    env = spec.get("env", {})
    if not isinstance(env, dict):
        raise ValueError("Sentry env must be an object")
    if set(env) - SAFE_ENV - {"SENTRY_ACCESS_TOKEN"}:
        raise ValueError(
            "Unsupported Sentry environment fields; use an explicit runtime credential wrapper"
        )
    safe_env = {key: value for key, value in env.items() if key in SAFE_ENV}
    if not all(isinstance(value, str) for value in safe_env.values()):
        raise ValueError("Sentry host environment must contain strings")
    return {"command": str(wrapper), "args": args, **({"env": safe_env} if safe_env else {})}
