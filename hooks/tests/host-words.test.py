#!/usr/bin/env python3
"""Host-specific hook text: trailers, confirmations, read/search nouns and background test runs.

Real payloads piped into the real hooks with AGENT_HOST set as the host adapter and the Codex shell
environment set it. HOOKS_DIR=<dir> tests another copy of the hooks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(os.environ.get("HOOKS_DIR") or Path(__file__).resolve().parents[1])
KIT = HOOKS.parent
failures = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global failures
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failures += 1
        print("     " + str(detail)[:600])


def env_for(host: str | None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_HOST", "AI_AGENT", "CLAUDECODE")}
    env.update({"KIT_ENV": "/dev/null", **extra})
    if host:
        env["AGENT_HOST"] = host
    return env


def bash_hook(hook: str, command: str, cwd: Path, host: str | None, **extra: str) -> dict:
    payload = {
        "tool_name": "Bash",
        "hook_event_name": "PreToolUse",
        "session_id": "host-words",
        "cwd": str(cwd),
        "tool_input": {"command": command},
    }
    out = subprocess.run(
        [str(HOOKS / hook)], input=json.dumps(payload), capture_output=True, text=True,
        env=env_for(host, **extra), cwd=cwd, timeout=60,
    ).stdout
    return json.loads(out).get("hookSpecificOutput", {}) if out.strip() else {}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})


with tempfile.TemporaryDirectory(prefix="host-words-", dir=os.environ.get("TMPDIR")) as tmp:
    root = Path(tmp)
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", "git@github.com:example/repo.git")

    for host, trailer in ((None, "Co-authored-by: Claude <noreply@anthropic.com>"),
                          ("codex", "Co-authored-by: Codex <noreply@openai.com>"),
                          ("cursor", "Co-authored-by: Cursor <cursoragent@cursor.com>")):
        out = bash_hook("bash-guards", "git commit -m 'Fix the thing'", repo, host)
        check(f"commit trailer names the {host or 'claude'} agent",
              out.get("permissionDecision") == "deny" and trailer in out.get("permissionDecisionReason", ""), out)

    out = bash_hook("bash-guards", "git push --force origin topic", repo, None)
    check("claude: a force push asks for confirmation",
          out.get("permissionDecision") == "ask" and "Confirm only if" in out.get("permissionDecisionReason", ""), out)
    out = bash_hook("bash-guards", "git push --force origin topic", repo, "codex")
    reason = out.get("permissionDecisionReason", "")
    check("codex: a force push is refused with who runs it, not an ask",
          out.get("permissionDecision") == "deny" and "cannot pause for approval" in reason
          and "Confirm" not in reason and "ask first" not in reason, out)

    big = root / "big.txt"
    big.write_text("".join(f"line {i} " + "x" * 80 + "\n" for i in range(600)))
    out = bash_hook("bash-guards", f"cat {big}", repo, "codex")
    reason = out.get("permissionDecisionReason", "")
    check("codex: the dump refusal names shell reads, not Read/Grep tools",
          out.get("permissionDecision") == "deny" and "sed -n" in reason and "Read(" not in reason, out)
    out = bash_hook("bash-guards", f"cat {big}", repo, None)
    check("claude: the dump refusal names the Read tool",
          "Read(file_path" in out.get("permissionDecisionReason", ""), out)

    msg = root / "msg"
    msg.write_text("Fix the thing\n")
    hook = subprocess.run([str(KIT / "git-hooks/agent/commit-msg"), str(msg)], cwd=repo, capture_output=True,
                          text=True, env=env_for("codex", HOME=str(root)), timeout=60)
    check("git commit-msg: the refusal names the Codex trailer on codex",
          hook.returncode == 1 and "Co-authored-by: Codex <noreply@openai.com>" in hook.stderr, hook.stderr)

    (repo / ".test-gate").write_text("")
    overlay = root / "kit.env"
    overlay.write_text("TEST_GATE_FILE=.test-gate\nTEST_RUNNER=run-tests\n")
    for command in ("run-tests tests/a_test.py &", "nohup run-tests tests/a_test.py"):
        out = bash_hook("test-exec-gate", command, repo, "codex", KIT_ENV=str(overlay))
        reason = out.get("permissionDecisionReason", "")
        check(f"codex: a backgrounded test run is refused ({command})",
              out.get("permissionDecision") == "deny" and "in the background" in reason
              and "system prompt" not in reason, out)
    out = bash_hook("test-exec-gate", "run-tests tests/a_test.py > log.txt 2>&1 &", repo, "codex",
                    KIT_ENV=str(overlay))
    check("codex: a redirected background run is allowed", out.get("permissionDecision") != "deny", out)

print(f"{failures} failure(s)")
sys.exit(1 if failures else 0)
