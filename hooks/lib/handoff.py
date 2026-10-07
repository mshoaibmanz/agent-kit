"""Handoffs at the context budget: the per-session handoff folder, the context-watch nudge,
pre-compact's staleness check and the text session-start re-injects after a compaction."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from agent_task import (
    CONTEXT_CAP,
    SLICE_CAP,
    Binding,
    agent_task_bin,
    binding_file,
    bound,
    branch_ticket,
    session_dir,
    slice_text,
    state_dir,
)
from binding import announce


# The context budget: hooks/context-watch nudges at CONTEXT_HANDOFF_AT, hooks/pre-compact snapshots
# a stale handoff, and session-start re-injects both after the compaction.
COMPACT_NEAR = "657K"  # autoCompactWindow 680000 in hosts/claude/settings.base.json
STALE_WITHOUT_NUDGE = 1800  # seconds


def repo_name(cwd: str) -> str:
    """The name session-context gives the repo: the main checkout's folder (worktrees share it),
    else the cwd's own folder."""
    try:
        common = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        common = ""
    if common:
        return os.path.basename(os.path.dirname(common))
    return os.path.basename(os.path.realpath(cwd or ".")) or "misc"


def handoff_folder(sid: str, cwd: str = "", repo: str = "") -> tuple[Binding | None, Path]:
    """Where the session's HANDOFF.md belongs: the bound folder, else its per-session OUT DIR. The
    one place that names the repo for it (session-start, the nudge, pre-compact): <repo>, else the
    repo of CLAUDE_PROJECT_DIR, else of <cwd>. A payload's cwd follows the Bash tool's `cd` into
    another repo; the project dir stays where the session started."""
    b = bound(sid)
    if b:
        return b, b.folder
    return None, session_dir(sid, repo or repo_name(os.environ.get("CLAUDE_PROJECT_DIR") or cwd))


def nudge_marker(sid: str) -> Path:
    return state_dir() / "context-watch" / sid


def nudge_text(sid: str, ctx: int, at: int, cwd: str) -> str:
    b, folder = handoff_folder(sid, cwd)
    head = f"CONTEXT {ctx // 1000}K: past the {at // 1000}K handoff point; auto-compaction follows near {COMPACT_NEAR}."
    retro = f'record up to 3 learnings with `{agent_task_bin()} retro "<mistake|fact|doc|tooling>: <text>" --session {sid}`'
    if b:
        if b.project.legacy:
            index = f"{b.project.path}/INDEX.md (a line per file you leave)"
        else:
            index = f"{b.project.path}/INDEX.md (it regenerates on write; add `  - use:`/`  - proved:` lines under what you leave)"
        return f"{head} At the next natural break (not mid-edit): update {folder}/HANDOFF.md (state, next steps, decisions) and {index}, {retro}, then carry on with the task."
    return (
        f"{head} No project is bound. At the next natural break (not mid-edit): bind one with "
        f"`{agent_task_bin()} bind <project>[/<item>] --session {sid}` (the user's /bind) and write HANDOFF.md there, "
        f"or write a short HANDOFF.md (state, next steps, decisions) in {folder}; {retro}; then carry on with the task."
    )


def handoff_stale(folder: Path, sid: str, now: float | None = None) -> bool:
    """HANDOFF.md is missing, unmodified since the nudge (the last 30 minutes when none fired), or
    unmodified since an earlier compaction's HANDOFF.auto.md. The last case holds after context-watch
    drops the nudge marker (usage fell below the threshold), so a second compaction refreshes that
    snapshot instead of archiving it."""
    now = time.time() if now is None else now
    m = nudge_marker(sid)
    since = m.stat().st_mtime if m.is_file() else now - STALE_WITHOUT_NUDGE
    hf = folder / "HANDOFF.md"
    return not hf.is_file() or hf.stat().st_mtime < since or auto_handoff_current(folder)


def _bounded(f: Path, cap: int) -> list[str]:
    if not f.is_file() or cap <= 0:
        return []
    text = f.read_text(errors="replace").strip()
    if len(text) > cap:
        text = text[:cap].rsplit("\n", 1)[0] + f"\n... (cut at {cap} characters; Read {f} for the rest)"
    return [f"{f.name} ({f}):", *text.split("\n")]


def unbound_text(folder: Path, branch: str = "") -> str:
    """The session-start line of an unbound session (startup, resume, clear and compact)."""
    bt = branch_ticket(branch)
    hint = f"the branch names {bt}, which maps to no single item; /bind <project>/{bt} creates it." if bt else "/bind <project>[/<item>] binds it (a prompt pasting an item's HANDOFF.md path or naming its ticket also does)."
    return f"OUT DIR: {folder} (created on first write). No project is bound: {hint} Keep what is worth keeping there, throwaway logs in the scratchpad."


def auto_handoff_current(folder: Path) -> bool:
    """HANDOFF.auto.md exists and is not older than HANDOFF.md: once the model rewrites HANDOFF.md,
    an earlier compaction's snapshot (old branch, status, prompts) is history, not state."""
    auto, hf = folder / "HANDOFF.auto.md", folder / "HANDOFF.md"
    if not auto.is_file():
        return False
    return not hf.is_file() or auto.stat().st_mtime >= hf.stat().st_mtime


def compact_text(sid: str, cwd: str = "", repo: str = "", branch: str = "", cap: int = CONTEXT_CAP) -> str:
    """After a compaction: the slice (an unbound session: its startup line), then HANDOFF.md and a
    current HANDOFF.auto.md, each bounded, within <cap> characters. The handoffs get up to 60% of it."""
    b, folder = handoff_folder(sid, cwd, repo)
    hand = _bounded(folder / "HANDOFF.md", min(2500, cap * 35 // 100))
    if auto_handoff_current(folder):
        hand += _bounded(folder / "HANDOFF.auto.md", min(2000, cap * 25 // 100))
    tail = "\n".join(["Compacted. Read these before continuing:", *hand]) if hand else ""
    room = cap - len(tail) - 1
    if b:
        head = slice_text(b, announce(b, binding_file(sid).get("rule") or "session"), cap=min(room, SLICE_CAP), handoff=False)
    else:
        head = unbound_text(folder, branch)[:room]
    return (head + ("\n" + tail if tail else ""))[:cap]
