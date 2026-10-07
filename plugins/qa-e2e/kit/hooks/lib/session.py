"""One session identity resolver for task bindings, hooks and provider runners."""

from __future__ import annotations
import os
from collections.abc import Mapping


def resolve(explicit: str = "", env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    names = (
        ("AGENT_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")
        if env.get("AGENT_HOST") == "codex"
        else ("AGENT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
    )
    return explicit or next((env[name] for name in names if env.get(name)), "")


if __name__ == "__main__":
    print(resolve())
