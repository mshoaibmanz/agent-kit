#!/usr/bin/env python3
"""Payload tests for ~/.agents/hooks/edit-guard: case variants on a case-insensitive disk, and the
deny reasons. hooktest.sh holds the basic allow/deny cases.

    python3 ~/.agents/hooks/tests/edit-guard.test.py

HOOKS_DIR=<dir> tests another copy of the hooks. The link cases make a temp dir under
~/.claude/tmp and remove it at the end. Exits 1 on any failure.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger("edit-guard.test")

HOME = Path.home()
GUARD = Path(os.environ.get("HOOKS_DIR") or HOME / ".agents/hooks") / "edit-guard"
KIT = Path(os.environ.get("AGENT_KIT_DIR") or GUARD.resolve().parent.parent)
failures: list[str] = []


def guard(path: str, tool: str = "Edit") -> tuple[int, str, str]:
    payload = {
        "tool_name": tool,
        "hook_event_name": "PreToolUse",
        "session_id": "t",
        "cwd": "/",
        "tool_input": {"file_path": path},
    }
    proc = subprocess.run(
        [str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, check=False
    )
    out = proc.stdout.strip()
    decision = json.loads(out)["hookSpecificOutput"] if out else {}
    return proc.returncode, decision.get("permissionDecision", "allow"), decision.get(
        "permissionDecisionReason", ""
    )


def expect(label: str, path: str, want: str, reason_has: str = "") -> None:
    rc, got, reason = guard(path)
    ok = rc == 0 and got == want and reason_has in reason
    logger.info("%s %s", "ok  " if ok else "FAIL", label)
    if not ok:
        failures.append(label)
        logger.info("     rc=%s decision=%s reason=%s", rc, got, reason)


def swap_case(path: Path) -> str:
    """HOME and the host dir in another case: /Users/x/.claude/... -> /USERS/x/.Claude/..."""
    rel = path.relative_to(HOME)
    first, *rest = rel.parts
    home = str(HOME).replace("/Users/", "/USERS/", 1)
    return os.path.join(home, first[:2].upper() + first[2:], *rest)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    settings = HOME / ".claude/settings.json"
    expect("settings.json deny names --take", str(settings), "deny", "render --host claude --take <key>")
    expect("mcp.json deny", str(HOME / ".claude/mcp.json"), "deny", "servers.json")
    if (HOME / ".claude/agents").is_symlink():
        logger.info("skip: ~/.claude/agents is still a link (agent-kit render has not rendered it)")
    else:
        expect("a rendered agent: deny, names its source and roles.toml",
               str(HOME / ".claude/agents/worker.md"), "deny", f"{KIT}/agents/worker.md (model and effort")
        expect("a rendered agent through an account's link",
               str(HOME / ".claude-work/agents/scout.md"), "deny", f"{KIT}/agents/scout.md")
        expect("the agent source itself is allowed", str(HOME / ".agents/agents/worker.md"), "allow")
    if sys.platform != "darwin":
        logger.info("skip: case-variant cases need a case-insensitive disk (macOS)")
        return 1 if failures else 0
    expect("5: settings.json in another case", swap_case(settings), "deny", "rendered by agent-kit")
    expect("5: mcp.json in another case", swap_case(HOME / ".claude/mcp.json"), "deny", "rendered by agent-kit")
    expect(
        "5: an account's settings.json link in another case",
        swap_case(HOME / ".claude-work/settings.json"),
        "deny",
        "rendered by agent-kit",
    )
    (HOME / ".claude/tmp").mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="edit-guard-test.", dir=HOME / ".claude/tmp"))
    try:
        target = tmp / "target"
        target.write_text("x\n")
        (tmp / "link").symlink_to(target)
        expect("a link under ~/.claude", str(tmp / "link"), "deny", "is a link")
        expect("5: a link under ~/.Claude, another case", swap_case(tmp / "link"), "deny", "is a link")
        expect("5: its target in another case is allowed", swap_case(target), "allow")
        expect("5: a new file under ~/.Claude is allowed", swap_case(tmp / "new.txt"), "allow")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    logger.info("%d failure(s)", len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
