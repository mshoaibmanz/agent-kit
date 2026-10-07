#!/usr/bin/env python3
"""Tests for the verification checks (hooks/verify-edit, lib/verify.py, the Stop and push wiring,
git-hooks/agent/post-checkout) with the real tools in scratch repos.

    python3 hooks/tests/verify.test.py

HOOKS_DIR=<dir> tests another copy of the hooks. A case whose tool is missing is skipped, except in
CI (CI=true), where ruff and basedpyright are installed and a missing one fails.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("verify.test")

HOOKS = Path(os.environ.get("HOOKS_DIR") or Path(__file__).resolve().parents[1])
KIT = HOOKS.parent
VERIFY = HOOKS / "lib/verify.py"
CI = os.environ.get("CI") == "true"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    logger.info("%s %s", "ok  " if ok else "FAIL", label)
    if not ok:
        failures.append(label)
        if detail:
            logger.info("     %s", detail.replace("\n", "\n     "))


def need(tool: str) -> bool:
    if shutil.which(tool):
        return True
    if CI:
        failures.append(f"{tool} is not installed in CI")
    logger.info("SKIP cases needing %s (not installed)", tool)
    return False


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def new_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def call(argv: list[str], payload: str = "", **kwargs: object) -> tuple[int, str]:
    """(exit code, stdout); 127 when the program is missing, so each case fails on its own."""
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True, check=False, **kwargs)  # type: ignore[call-overload]
    except OSError as error:
        return 127, str(error)
    return proc.returncode, proc.stdout


def edit(path: Path, new: str, env: dict[str, str], hook: str = "verify-edit") -> tuple[int, str]:
    """Writes new over path and runs the PostToolUse hook with the Edit tool's payload."""
    original = path.read_text()
    path.write_text(new)
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "t",
               "tool_input": {"file_path": str(path)}, "tool_response": {"filePath": str(path), "originalFile": original}}
    return call([str(HOOKS / hook)], json.dumps(payload), env=env)


def context(out: str) -> str:
    try:
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
    except (ValueError, KeyError):
        return ""


def gate(repo: Path, env: dict[str, str], *extra: str) -> tuple[int, str]:
    return call([sys.executable, str(VERIFY), "gate", str(repo), *extra], env=env)


def edit_cases(tmp: Path, env: dict[str, str]) -> None:
    if not need("ruff"):
        return
    repo = new_repo(tmp, "plain", {"m.py": "def f():\n    return pre_existing\n"})
    m = repo / "m.py"
    rc, out = edit(m, "import os\n\n\ndef f():\n    return pre_existing\n\n\ndef g():\n    return introduced\n", env)
    text = context(out)
    check("edit: an introduced F821 is reported", rc == 0 and "F821" in text and "introduced" in text, out)
    check("edit: the pre-existing one is not (its line moved)", "pre_existing" not in text, text)
    check("edit: an unused import is not reported per edit", "F401" not in text, text)
    check("edit: no ruff config, so the file is not reformatted", m.read_text().startswith("import os\n\n\ndef"))

    rc, out = edit(m, m.read_text() + "\n", env)
    check("edit: an edit that adds no error is silent", rc == 0 and out.strip() == "", out)

    rc, out = edit(m, "".join(f"x{i} = undefined_{i}\n" for i in range(15)), env)
    lines = context(out).splitlines()
    check("edit: output capped at 10 lines plus a count", len(lines) == 12 and lines[-1].strip() == "... 5 more",
          "\n".join(lines))

    rc, out = edit(m, "def broken(:\n", env, hook="python-format")
    check("edit: the retired python-format slot forwards (syntax error reported)",
          "invalid-syntax" in context(out) or "syntax" in context(out), out)

    cfg = new_repo(tmp, "configured", {
        "ruff.toml": '[lint]\nper-file-ignores = {"skip.py" = ["F821"]}\n[format]\nquote-style = "single"\n',
        "skip.py": "a = 1\n", "other.py": "a = 1\n"})
    rc, out = edit(cfg / "skip.py", 'a = "x"\nb = undefined_c\n', env)
    check("edit: the repo config wins (its per-file ignore holds)", rc == 0 and out.strip() == "", out)
    check("edit: the repo config wins (formatted with its quote style)", (cfg / "skip.py").read_text().startswith("a = 'x'"),
          (cfg / "skip.py").read_text())
    rc, out = edit(cfg / "other.py", "b = undefined_c\n", env)
    check("edit: the repo config still reports a new F821 elsewhere", "F821" in context(out), out)

    tool_bin = tmp / "bin-no-ruff"
    tool_bin.mkdir()
    for tool in ("bash", "python3", "git"):
        (tool_bin / tool).symlink_to(shutil.which(tool) or tool)
    rc, out = edit(m, "y = undefined_z\n", {**env, "PATH": str(tool_bin)})
    check("edit: a missing tool is skipped silently", rc == 0 and out.strip() == "", out)


def gate_cases(tmp: Path, env: dict[str, str]) -> None:
    if not (shutil.which("basedpyright") or shutil.which("pyright")):
        need("basedpyright")
        return
    repo = new_repo(tmp, "typed", {"a.py": 'x: int = "pre"\n', "b.py": "y = 1\n"})
    git(repo, "checkout", "-qb", "feature")
    (repo / "a.py").write_text('# shifted\nx: int = "pre"\n')
    rc, out = gate(repo, env, "stop")
    check("gate: a pre-existing error (line moved) does not block", rc == 0 and out == "", out)
    (repo / "b.py").write_text('y = 1\n\n\ndef f() -> str:\n    return 1\n')
    rc, out = gate(repo, env, "stop")
    check("gate: a NEW error in a changed file blocks", rc == 1 and "b.py:5" in out and "reportReturnType" in out, out)
    check("gate: the pre-existing error is not listed", "a.py" not in out, out)
    rc, out = gate(repo, env, "stop")
    check("gate: at Stop the same tree is shown once", rc == 0 and out == "", out)
    rc, out = gate(repo, env)
    check("gate: the push still refuses it", rc == 1 and "b.py:5" in out, out)
    (repo / "b.py").write_text("y = 1\n")
    rc, out = gate(repo, env)
    check("gate: fixed, it passes", rc == 0 and out == "", out)

    off = Path(env["KIT_ENV"]).parent / "verify.toml"
    off.write_text('[repo.typed.python.stop]\nenabled = false\n')
    (repo / "b.py").write_text('def f() -> str:\n    return 1\n')
    rc, out = gate(repo, env)
    check("gate: verify.toml turns the phase off for one repo", rc == 0 and out == "", out)
    off.unlink()

    # The Stop hook end to end: an edit marks the session, the Stop blocks with the new error.
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "vt", "cwd": str(repo),
               "tool_input": {"file_path": str(repo / "b.py")}}
    call([str(HOOKS / "review-mark-changes")], json.dumps(payload), env=env)
    stop = {"hook_event_name": "Stop", "session_id": "vt", "cwd": str(repo), "stop_hook_active": False,
            "last_assistant_message": "done"}
    _, out = call([str(HOOKS / "review-trigger")], json.dumps(stop), env=env)
    try:
        decision = json.loads(out) if out.strip() else {}
    except ValueError:
        decision = {}
    reason = decision.get("reason", "")
    check("stop: review-trigger blocks on the new type error",
          decision.get("decision") == "block" and "new type errors" in reason and "b.py" in reason, out)

    _, out = call([sys.executable, str(VERIFY), "doctor", str(repo)], env=env)
    check("doctor: a line per repo naming the checker each phase runs",
          f"verify {repo.name}: python edit " in out and "python stop " in out and "rust stop " in out, out)


def worktree_cases(tmp: Path, env: dict[str, str]) -> None:
    repo = new_repo(tmp, "wt-main", {"a.py": "x = 1\n"})
    (repo / "pyrightconfig.json").write_text('{"venvPath": "."}\n')
    (repo / ".venv/bin").mkdir(parents=True)
    with open(repo / ".git/info/exclude", "a") as fh:
        fh.write("pyrightconfig.json\n.venv\n")
    wt = tmp / "wt-one"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "one")
    call([sys.executable, str(VERIFY), "worktree", str(wt)], env=env)
    copied = wt / "pyrightconfig.json"
    check("worktree: untracked pyrightconfig.json copied", copied.is_file() and copied.read_text() == '{"venvPath": "."}\n')
    check("worktree: .venv linked to the main checkout's",
          (wt / ".venv").is_symlink() and (wt / ".venv").resolve() == (repo / ".venv").resolve())

    wt2 = tmp / "wt-two"
    git(repo, "worktree", "add", "-q", str(wt2), "-b", "two")
    head = git(wt2, "rev-parse", "HEAD")
    call([str(KIT / "git-hooks/agent/post-checkout"), "0" * 40, head, "1"], cwd=wt2, env=env)
    check("worktree: the agent post-checkout hook provisions a new worktree",
          (wt2 / "pyrightconfig.json").is_file() and (wt2 / ".venv").is_symlink())

    wt3 = tmp / "wt-three"
    git(repo, "worktree", "add", "-q", str(wt3), "-b", "three")
    Path(env["KIT_ENV"]).parent.joinpath("verify.toml").write_text("[default.worktree]\nenabled = false\n")
    call([sys.executable, str(VERIFY), "worktree", str(wt3)], env=env)
    check("worktree: opt-out via verify.toml", not (wt3 / ".venv").exists() and not (wt3 / "pyrightconfig.json").exists())
    Path(env["KIT_ENV"]).parent.joinpath("verify.toml").unlink()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="verify-test-"))
    overlay = tmp / "overlay"
    overlay.mkdir()
    (overlay / "kit.env").write_text("")
    env = {**os.environ, "KIT_ENV": str(overlay / "kit.env"), "XDG_CACHE_HOME": str(tmp / "cache"),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    try:
        edit_cases(tmp, env)
        gate_cases(tmp, env)
        worktree_cases(tmp, env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    logger.info("%d failure(s)", len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
