#!/usr/bin/env python3
"""Tests for bin/agent-task, hooks/project-bind, hooks/retro-extract and the session-start slice,
against a synthetic work root. HOOKS_DIR=<dir> tests another copy of the hooks (its ../bin too)."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOOKS = Path(os.environ.get("HOOKS_DIR") or Path(__file__).resolve().parent.parent)
BIN = HOOKS.parent / "bin"
T = Path(tempfile.mkdtemp(prefix="agent-task-test."))
ROOT = T / "work"
ENV = dict(
    os.environ,
    CLAUDE_OUT_ROOT=str(ROOT),
    CLAUDE_STATE_DIR=str(T / "state"),
    TAB_LABEL_DIR=str(T / "labels"),
    KIT_ENV="/dev/null",
    ITERM_SESSION_ID="w0t0p0:TAB-ONE",
)
for session_env in ("AGENT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
    ENV.pop(session_env, None)
ENV.pop("CI_WATCH_ACTIVE", None)
fails = 0
passes = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global fails, passes
    if cond:
        passes += 1
        print(f"PASS {name}")
    else:
        fails += 1
        print(f"FAIL {name}" + (f"\n     {detail[:600]}" if detail else ""))


def run(args: list[str], stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, env=env or ENV, cwd=str(T), timeout=30)


def task(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return run([str(BIN / "agent-task"), *args], env=env)


def hook(name: str, payload: dict, env: dict | None = None) -> subprocess.CompletedProcess:
    return run([str(HOOKS / name)], json.dumps(payload), env=env)


def binding(sid: str) -> str:
    f = T / "state" / "task-bindings" / sid[:8]
    return f.read_text().split("\n")[0].strip() if f.exists() else ""


def project(name: str, meta: str) -> Path:
    p = ROOT / "projects" / name
    (p / "knowledge").mkdir(parents=True, exist_ok=True)
    (p / "scripts").mkdir(exist_ok=True)
    (p / "PROJECT.md").write_text(f"# {name}\n\n{meta}\nstatus: active\n\n## Tickets and PRs\n| item | ticket | PR | status |\n|---|---|---|---|\n")
    return p


project("invoices", "scope: invoice fees\nterms: invoicing, line item\nrepos: sample-app\npaths: src/libinvoice/**\ntickets: DEMO-1500, CORE-2048")
project("web-tracking", "scope: web tracking\nterms: tracking, browser\nrepos: sample-app")
project("web-search", "scope: web search\nterms: tracking, search\nrepos: sample-app")
(ROOT / "tasks" / "DEMO-42-widget-fix").mkdir(parents=True)
(ROOT / "tasks" / "DEMO-42-widget-fix" / "HANDOFF.md").write_text("# Handoff DEMO-42\nState: half done.\n")


def resolve(text: str, *extra: str) -> dict:
    r = task("resolve", text, *extra)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"outcome": f"ERR {r.stderr}"}


print("--- native session defaults ---")
for label, variables, explicit, expected in (
    ("Codex thread fallback", {"CODEX_THREAD_ID": "sid-codex"}, "", "sid-codex"),
    ("Claude session wins over Codex", {"CODEX_THREAD_ID": "sid-codex", "CLAUDE_CODE_SESSION_ID": "sid-claude"}, "", "sid-claude"),
    ("Codex child prefers native thread over inherited Claude", {"AGENT_HOST": "codex", "CODEX_THREAD_ID": "sid-native", "CLAUDE_CODE_SESSION_ID": "sid-inherited"}, "", "sid-native"),
    ("generic session wins over hosts", {"CODEX_THREAD_ID": "sid-codex", "CLAUDE_CODE_SESSION_ID": "sid-claude", "AGENT_SESSION_ID": "sid-generic"}, "", "sid-generic"),
    ("explicit session wins", {"CODEX_THREAD_ID": "sid-codex", "CLAUDE_CODE_SESSION_ID": "sid-claude", "AGENT_SESSION_ID": "sid-generic"}, "sid-explicit", "sid-explicit"),
):
    args = ["bind", "invoices", *(["--session", explicit] if explicit else [])]
    result = task(*args, env=dict(ENV, **variables))
    check(label, result.returncode == 0 and f"Bound session {expected[:8]}" in result.stdout, result.stdout + result.stderr)
    result = task("where", "--path", env=dict(ENV, **variables), *(["--session", explicit] if explicit else []))
    check(label + " resolves the binding", result.returncode == 0 and result.stdout.strip() == str(ROOT / "projects/invoices"), result.stdout + result.stderr)

print("--- resolver ---")
r = resolve("pick up DEMO-1500 today")
check("resolver: a ticket key in PROJECT.md is an exact match", r["outcome"] == "exact" and r["candidates"][0]["project"] == "invoices", str(r))
r = resolve("the invoicing line item screen", "--paths", "src/libinvoice/fees.py", "src/libinvoice/x.py", "--repo", "sample-app")
check("resolver: terms plus paths are one strong match", r["outcome"] == "strong" and r["candidates"][0]["project"] == "invoices", str(r))
r = resolve("tracking is wrong for this browser search")
check("resolver: two close projects are ambiguous", r["outcome"] == "ambiguous" and {c["project"] for c in r["candidates"]} >= {"web-tracking", "web-search"}, str(r))
r = resolve("QQ-77 something unrelated")
check("resolver: no match is none, with the ticket kept", r["outcome"] == "none" and r["ticket"] == "QQ-77", str(r))
r = resolve("work on DEMO-42")
check("resolver: a legacy task folder matches its ticket exactly", r["outcome"] == "exact" and r["candidates"][0]["legacy"], str(r))

print("--- project-bind: UserPromptSubmit ---")
S1 = "11111111-aaaa-4000-8000-000000000000"
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S1, "cwd": str(T), "prompt": "Fix DEMO-1500 rounding please"})
ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""
check("a ticket key in the prompt binds to its project and item", binding(S1) == "project:invoices/DEMO-1500", binding(S1) + out.stderr)
check("the bind injects the slice", "PROJECT: invoices · item DEMO-1500" in ctx and "exact match" in ctx, ctx)
check("the item folder gets task.json", (ROOT / "projects/invoices/items/DEMO-1500/task.json").is_file())
check("the tab label is <project> · <item>", (T / "labels" / S1).read_text() == "invoices · DEMO-1500")
S2 = "22222222-aaaa-4000-8000-000000000000"
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S2, "cwd": str(T), "prompt": "what does the cache layer do? also convert to UTF-8"})
check("a Q&A prompt never binds (UTF-8 is no ticket)", out.stdout.strip() == "" and binding(S2) == "", out.stdout + out.stderr)
S3 = "33333333-aaaa-4000-8000-000000000000"
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S3, "cwd": str(T), "prompt": "start ABC-12 please"})
check("no match creates a project of one named after the ticket", binding(S3) == "project:ABC-12/ABC-12" and (ROOT / "projects/ABC-12/PROJECT.md").is_file(), binding(S3))
inbox = "".join(f.read_text() for f in (ROOT / "retro").glob("*.md")) if (ROOT / "retro").is_dir() else ""
check("...and records it in the retro inbox", "[auto]" in inbox and "project created project of one ABC-12" in inbox, inbox)
S4 = "44444444-aaaa-4000-8000-000000000000"
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S4, "cwd": str(T), "prompt": "the tracking for this browser search is off, see WEB-1"})
ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""
check("ambiguous: one line with the candidates and the bind command, no bind", binding(S4) == "" and "web-search" in ctx and "agent-task bind" in ctx and ctx.count("\n") == 0, ctx)
S5 = "55555555-aaaa-4000-8000-000000000000"
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S5, "cwd": str(T), "prompt": "DEMO-42 next step"})
ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""
check("a legacy folder binds as a project of one with its handoff", binding(S5) == "DEMO-42-widget-fix" and "TASK DIR:" in ctx and "Handoff DEMO-42" in ctx, ctx)
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S1, "cwd": str(T), "prompt": "now ABC-99 too"})
check("a bound session is not rebound by a later ticket", binding(S1) == "project:invoices/DEMO-1500" and out.stdout.strip() == "")
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S2, "cwd": str(T), "prompt": "DEMO-1500"}, env=dict(ENV, CI_WATCH_ACTIVE="1"))
check("CI_WATCH_ACTIVE: never binds", binding(S2) == "")

print("--- session-start: lazy OUT DIR, /clear inheritance ---")
TAB = T / "state" / "task-bindings" / "tab-iterm-session-id-w0t0p0-tab-one"
check("a prompt bind points the tab at that session", TAB.is_file() and TAB.read_text().startswith("DEMO-42-widget-fix\t" + S5), TAB.read_text() if TAB.is_file() else "no tab file")
S6 = "66666666-aaaa-4000-8000-000000000000"
out = task("session-start", "--session", S6, "--source", "startup", "--repo", "gate-repo")
check("unbound startup: OUT DIR path text, folder not created", out.stdout.startswith(f"OUT DIR: {ROOT}/gate-repo/") and not (ROOT / "gate-repo").exists(), out.stdout + out.stderr)
check("...and blanks the tab's binding (its session has gone)", not TAB.exists())
out = task("session-start", "--session", S6, "--source", "clear", "--repo", "gate-repo", env=dict(ENV, ITERM_SESSION_ID="w0t0p0:TAB-NONE"))
check("clear in a tab with no binding stays unbound", binding(S6) == "" and out.stdout.startswith("OUT DIR:"))
out = task("session-start", "--session", S6, "--source", "clear", "--repo", "gate-repo")
check("clear after an unbound startup does not inherit an earlier session's binding", binding(S6) == "" and out.stdout.startswith("OUT DIR:"), out.stdout + binding(S6))
S6B = "6666666b-aaaa-4000-8000-000000000000"
task("bind", "DEMO-42", "--session", S6B)
S6C = "6666666c-aaaa-4000-8000-000000000000"
out = task("session-start", "--session", S6C, "--source", "clear", "--repo", "gate-repo")
check("clear inherits the binding of the session that was in the tab", binding(S6C) == "DEMO-42-widget-fix" and "across /clear" in out.stdout and "TASK DIR:" in out.stdout, out.stdout + binding(S6C))
check("...marked inherited", (T / "state" / "task-bindings" / S6C[:8]).read_text() == "DEMO-42-widget-fix\ninherited\n")
task("session-start", "--session", S1, "--source", "resume", "--repo", "gate-repo")
S6D = "6666666d-aaaa-4000-8000-000000000000"
task("session-start", "--session", S6D, "--source", "clear", "--repo", "gate-repo")
check("resuming a bound session points the tab at it; clear then inherits that", binding(S6D) == "project:invoices/DEMO-1500", binding(S6D))

print("--- project-bind: a binding inherited across /clear ---")
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S6D, "cwd": str(T), "prompt": "what next?"})
check("a prompt with no ticket keeps the inherited binding and its mark", out.stdout.strip() == "" and binding(S6D) == "project:invoices/DEMO-1500" and "inherited" in (T / "state/task-bindings" / S6D[:8]).read_text(), out.stdout)
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S6D, "cwd": str(T), "prompt": "carry on with DEMO-1500"})
check("naming the inherited item keeps it, silently, and makes it sticky", out.stdout.strip() == "" and (T / "state/task-bindings" / S6D[:8]).read_text() == "project:invoices/DEMO-1500\n", out.stdout)
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S6D, "cwd": str(T), "prompt": "now DEMO-42"})
check("...after which another ticket does not rebind", binding(S6D) == "project:invoices/DEMO-1500" and out.stdout.strip() == "")
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S6C, "cwd": str(T), "prompt": "pick up DEMO-1500 now"})
ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""
check("an inherited binding re-resolves on a ticket with an exact project hit, saying so", binding(S6C) == "project:invoices/DEMO-1500" and ctx.startswith("Bound to invoices (exact match on DEMO-1500) (was DEMO-42-widget-fix, kept from before /clear)") and "PROJECT: invoices" in ctx, ctx)
task("bind", "DEMO-78", "--session", S6B)
S6E = "6666666e-aaaa-4000-8000-000000000000"
task("session-start", "--session", S6E, "--source", "clear", "--repo", "gate-repo")
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S6E, "cwd": str(T), "prompt": "start on DEMO-300"})
ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""
check("an inherited binding re-resolves on a different ticket (no project: a project of one)", binding(S6E) == "project:DEMO-300/DEMO-300" and "(was DEMO-78/DEMO-78, kept from before /clear)" in ctx, binding(S6E) + ctx)

print("--- project-bind: the branch's ticket against a prompt's mention ---")
REPO = T / "repo"
REPO.mkdir()
subprocess.run(["git", "init", "-q", "-b", "DEMO-200-widget", str(REPO)], check=True)
subprocess.run(["git", "-C", str(REPO), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
S12 = "12121212-aaaa-4000-8000-000000000000"
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S12, "cwd": str(REPO), "prompt": "implement this like DEMO-100 did"})
check("a prompt's ticket with no project loses to the branch's ticket", binding(S12) == "project:DEMO-200/DEMO-200" and not (ROOT / "projects/DEMO-100").exists(), binding(S12) + out.stdout)
S13 = "13131313-aaaa-4000-8000-000000000000"
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S13, "cwd": str(REPO), "prompt": "port the fix from DEMO-1500 here"})
check("a prompt's ticket with an exact project hit beats the branch's", binding(S13) == "project:invoices/DEMO-1500", binding(S13) + out.stdout)
r = resolve("like DEMO-100 did", "--branch", "DEMO-201-widget")
check("resolver: no exact hit keeps the branch's ticket", r["outcome"] == "none" and r["ticket"] == "DEMO-201", str(r))
for text in ("start on DEMO-310 now", "pick up ticket DEMO-310", "switch to DEMO-310", "DEMO-310", "let's move on to DEMO-310 like DEMO-1500", "fix DEMO-310", "ok. please implement DEMO-310"):
    r = resolve(text, "--branch", "DEMO-201-widget")
    check(f"resolver: a request for another ticket beats the branch ({text!r})", r["outcome"] == "none" and r["ticket"] == "DEMO-310", str(r))
for text in ("implement the DEMO-100 approach here", "this started in DEMO-100", "see DEMO-100 for context, then continue", "reuse the fix DEMO-100 shipped", "how did we handle DEMO-100?", "what do DEMO-100 and DEMO-101 share", "the work DEMO-100 did"):
    r = resolve(text, "--branch", "DEMO-201-widget")
    check(f"resolver: a mention still loses to the branch ({text!r})", r["ticket"] == "DEMO-201", str(r))
S20 = "25252525-bbbb-4000-8000-000000000000"
hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S20, "cwd": str(REPO), "prompt": "start on DEMO-311 now"})
check("unbound on a ticket branch: an explicit request binds the new ticket, not the branch's", binding(S20) == "project:DEMO-311/DEMO-311", binding(S20))
for sid, prompt, want in (("26262626-bbbb-4000-8000-000000000000", "start on DEMO-301", "project:DEMO-301/DEMO-301"), ("27272727-bbbb-4000-8000-000000000000", "do it like DEMO-101 did", "project:DEMO-200/DEMO-200")):
    task("bind", "DEMO-200", "--session", S6B)
    task("session-start", "--session", sid, "--source", "clear", "--repo", "gate-repo")
    hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": str(REPO), "prompt": prompt})
    check(f"inherited DEMO-200 on its branch: {prompt!r} binds {want}", binding(sid) == want and not (ROOT / "projects/DEMO-101").exists(), binding(sid))

print("--- ticket keys: look-alikes and known Jira prefixes ---")
S14 = "14141414-aaaa-4000-8000-000000000000"
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S14, "cwd": str(T), "prompt": "is CVE-2024-3094 relevant to us?"})
check("a CVE id never binds", out.stdout.strip() == "" and binding(S14) == "" and not (ROOT / "projects/CVE-2024").exists(), out.stdout)
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S14, "cwd": str(T), "prompt": "GHSA-1234 or CWE-79, ARM64-8 and X86-64 builds, MD5-5"})
check("GHSA, CWE, ARM64, X86 and MD5 keys never bind", out.stdout.strip() == "" and binding(S14) == "", out.stdout)
OV = T / "overlay"
OV.mkdir()
(OV / "kit.env").write_text("BQRO_PROJECT=x\n")
(OV / "jira-prefs.md").write_text("site: x\nproject_key: DEMO\n\n## CORE notes\n")
KENV = dict(ENV, KIT_ENV=str(OV / "kit.env"))
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S14, "cwd": str(T), "prompt": "start ABC-500 please"}, env=KENV)
check("with jira-prefs.md beside kit.env, an unknown prefix never binds", out.stdout.strip() == "" and binding(S14) == "", out.stdout)
for n, (key, why) in enumerate((("CORE-9", "a `## <KEY> notes` heading"), ("DEMO-501", "project_key"))):
    sid = f"1515151{n}-aaaa-4000-8000-000000000000"
    hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": str(T), "prompt": f"start {key}"}, env=KENV)
    check(f"...{why} is a known prefix", binding(sid) == f"project:{key}/{key}", binding(sid))
r = task("resolve", "x", "--branch", "ABC-9-thing", env=KENV)
check("...and an unknown branch prefix names no ticket", json.loads(r.stdout)["ticket"] == "", r.stdout)

out = task("session-start", "--session", S1, "--source", "compact")
check("a bound session gets the slice on compact", out.stdout.startswith("PROJECT: invoices · item DEMO-1500"), out.stdout)

print("--- context budget: compact re-injection, the nudge text ---")
item = ROOT / "projects/invoices/items/DEMO-1500"
(item / "HANDOFF.md").write_text("# Handoff DEMO-1500\n" + "".join(f"- step {i}: " + "detail " * 20 + "\n" for i in range(60)))
(item / "HANDOFF.auto.md").write_text("# HANDOFF.auto\n- branch: DEMO-1500-x\n")
out = task("session-start", "--session", S1, "--source", "compact", "--cap", "6000")
o = out.stdout
check("compact: the slice, then HANDOFF.md and HANDOFF.auto.md", o.startswith("PROJECT: invoices · item DEMO-1500") and f"HANDOFF.md ({item}/HANDOFF.md):" in o and f"HANDOFF.auto.md ({item}/HANDOFF.auto.md):" in o, o)
check("...HANDOFF.md cut to its share of the cap, naming the file to Read", "... (cut at 2100 characters; Read" in o and "- step 59:" not in o, o[-800:])
check("...the slice's 8-line HANDOFF opening is not repeated", "Read it before starting" not in o, o)
check("...within the cap", len(o.rstrip("\n")) <= 6000, str(len(o)))
(item / "HANDOFF.auto.md").unlink()
o = task("session-start", "--session", S1, "--source", "compact").stdout
check("compact: no HANDOFF.auto.md, no section for it", "HANDOFF.auto.md" not in o and "HANDOFF.md (" in o, o[-400:])
o = task("session-start", "--session", S5, "--source", "compact").stdout
check("compact: a legacy folder gets its TASK DIR and HANDOFF.md", o.startswith("TASK DIR:") and "HANDOFF.md (" in o and "Handoff DEMO-42" in o, o)
o = task("nudge", "--session", S1, "--ctx", "612345", "--at", "600000").stdout
check("nudge: a bound item names its HANDOFF.md, the project INDEX and agent-task retro", o.startswith("CONTEXT 612K: past the 600K handoff point") and f"update {item}/HANDOFF.md" in o and "projects/invoices/INDEX.md (it regenerates" in o and f"agent-task retro" in o and f"--session {S1}" in o, o)
o = task("nudge", "--session", S5, "--ctx", "700000", "--at", "600000").stdout
check("nudge: a legacy folder's INDEX.md is kept by hand", "INDEX.md (a line per file you leave)" in o and "DEMO-42-widget-fix/HANDOFF.md" in o, o)
(item / "HANDOFF.auto.md").write_text("# HANDOFF.auto\n- branch: OLD-branch\n")
os.utime(item / "HANDOFF.auto.md", (1_700_000_000, 1_700_000_000))
o = task("session-start", "--session", S1, "--source", "compact").stdout
check("compact: a HANDOFF.auto.md older than HANDOFF.md is left out", "HANDOFF.auto.md" not in o and "OLD-branch" not in o and "HANDOFF.md (" in o, o[-400:])
(item / "HANDOFF.auto.md").unlink()

print("--- pre-compact: a second compaction without a handoff rewrite; failed task updates ---")
S28 = "28282828-aaaa-4000-8000-000000000000"
task("bind", "cbtest/CB-1", "--session", S28)
cb = ROOT / "projects/cbtest/items/CB-1"
now = time.time()
(cb / "HANDOFF.md").write_text("# Handoff CB-1\n")
os.utime(cb / "HANDOFF.md", (now - 600, now - 600))
marker = T / "state/context-watch" / S28
marker.parent.mkdir(parents=True, exist_ok=True)
marker.write_text("600\n")
os.utime(marker, (now - 300, now - 300))
pcp = {"hook_event_name": "PreCompact", "session_id": S28, "cwd": str(T), "trigger": "auto"}
first = hook("pre-compact", pcp)
marker.unlink()  # context-watch drops it once usage falls below the threshold
os.utime(cb / "HANDOFF.auto.md", (now - 60, now - 60))
second = hook("pre-compact", pcp)
o = task("session-start", "--session", S28, "--source", "compact").stdout
check("two compactions, HANDOFF.md not rewritten: the newer snapshot is refreshed, not archived", (cb / "HANDOFF.auto.md").is_file() and not (cb / "HANDOFF.auto.prev.md").exists() and "previous compaction's snapshot" in second.stdout and "HANDOFF.auto.md (" in o, first.stdout + second.stdout + o[-300:])
(cb / "HANDOFF.auto.md").unlink(missing_ok=True)

def tline(kind: str, tid: str, body: dict, tur: dict | None = None, err: bool = False) -> str:
    if kind == "use":
        return json.dumps({"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, **body}]}}, separators=(",", ":"))
    res = {"type": "tool_result", "tool_use_id": tid, "content": body.get("content", "")}
    if err:
        res["is_error"] = True
    return json.dumps({"type": "user", "isSidechain": False, "message": {"role": "user", "content": [res]}, **({"toolUseResult": tur} if tur else {})}, separators=(",", ":"))

# Compact JSON, as Claude Code writes transcripts: pre-compact's line prefilter matches '"type":"user"'.
rows = [json.dumps({"type": "user", "message": {"role": "user", "content": "track three things"}}, separators=(",", ":"))]
for n, s in ((1, "fails to complete"), (2, "fails to delete"), (3, "never answered")):
    rows += [tline("use", f"tc{n}", {"name": "TaskCreate", "input": {"subject": s, "description": s}}), tline("res", f"tc{n}", {"content": f"Task #{n} created"}, {"task": {"id": str(n), "subject": s}})]
rows += [
    tline("use", "tu1", {"name": "TaskUpdate", "input": {"taskId": "1", "status": "completed"}}),
    tline("res", "tu1", {"content": "<tool_use_error>Task not found</tool_use_error>"}, err=True),
    tline("use", "tu2", {"name": "TaskUpdate", "input": {"taskId": "2", "status": "deleted"}}),
    tline("res", "tu2", {"content": "Update failed"}, {"success": False, "taskId": "2"}),
    tline("use", "tu3", {"name": "TaskUpdate", "input": {"taskId": "3", "status": "completed"}}),
]
(T / "tasks.jsonl").write_text("\n".join(rows) + "\n")
S29 = "29292929-aaaa-4000-8000-000000000000"
task("bind", "cbtest/CB-2", "--session", S29)
hook("pre-compact", {"hook_event_name": "PreCompact", "session_id": S29, "cwd": str(T), "trigger": "auto", "transcript_path": str(T / "tasks.jsonl")})
snap = (ROOT / "projects/cbtest/items/CB-2/HANDOFF.auto.md").read_text() if (ROOT / "projects/cbtest/items/CB-2/HANDOFF.auto.md").is_file() else ""
check("pre-compact: a failed, refused or unanswered TaskUpdate leaves the task open", all(f"- [pending] {s}" in snap for s in ("fails to complete", "fails to delete", "never answered")), snap[-500:])

print("--- context budget: one repo name for the handoff folder; the unbound compact line ---")
PA, PB = T / "proj-a", T / "proj-b"
for r in (PA, PB):
    subprocess.run(["git", "init", "-q", str(r)], check=True)
S16 = "16161616-aaaa-4000-8000-000000000000"
PENV = dict(ENV, CLAUDE_PROJECT_DIR=str(PA))
o = task("session-start", "--session", S16, "--source", "startup", "--branch", "DEMO-123-foo", env=PENV).stdout
check("startup with no --repo: the OUT DIR is CLAUDE_PROJECT_DIR's repo", o.startswith(f"OUT DIR: {ROOT}/proj-a/"), o)
o = task("nudge", "--session", S16, "--ctx", "612000", "--at", "600000", "--cwd", str(PB), env=PENV).stdout
check("nudge: CLAUDE_PROJECT_DIR names the folder, not a cwd that moved to another repo", f"in {ROOT}/proj-a/" in o and "proj-b" not in o, o)
pc = hook("pre-compact", {"hook_event_name": "PreCompact", "session_id": S16, "cwd": str(PB), "trigger": "auto"}, env=PENV)
autos = sorted(ROOT.glob(f"*/*-{S16[:8]}/HANDOFF.auto.md"))
check("pre-compact: HANDOFF.auto.md goes to that same folder", [a.parent.parent.name for a in autos] == ["proj-a"], pc.stdout + str(autos))
o = task("session-start", "--session", S16, "--source", "compact", "--branch", "DEMO-123-foo", env=PENV).stdout
check("compact: re-injects it from that folder", f"HANDOFF.auto.md ({ROOT}/proj-a/" in o, o)
check("compact, unbound: keeps the startup line's branch hint and keep-it-there rule", "the branch names DEMO-123, so the first prompt binds it." in o and "Keep what is worth keeping there, throwaway logs in the scratchpad." in o and "Compacted. Read these before continuing:" in o, o)
o = task("nudge", "--session", S2, "--ctx", "600000", "--at", "600000", "--cwd", str(T)).stdout
check("nudge: unbound names agent-task bind and the OUT DIR", "No project is bound" in o and f"bind <project>[/<item>] --session {S2}" in o and f"in {ROOT}/" in o, o)

print("--- injection cap ---")
big = project("big", "scope: many scripts\nterms: bulk")
for i in range(400):
    (big / "scripts" / f"s{i:03}.py").write_text(f'"""Script {i}: ' + "word " * 30 + '"""\n')
task("index", "big")
S7 = "77777777-aaaa-4000-8000-000000000000"
out = task("bind", "big/BIG-1", "--session", S7)
sl = task("slice", "--session", S7).stdout
check("the slice stays under 9000 characters (about 2.5K tokens)", 0 < len(sl) <= 9000 and "more lines" in sl, str(len(sl)))

print("--- auto-index keeps hand lines ---")
rp = ROOT / "projects" / "invoices"
(rp / "scripts" / "a.py").write_text('"""Fee replay for one order."""\n')
task("index", "invoices")
idx = (rp / "INDEX.md").read_text().replace("- `scripts/a.py` (python): Fee replay for one order.", "- `scripts/a.py` (python): Fee replay for one order.\n  - use: replay one order's fees\n  - proved: rounding is per line")
(rp / "INDEX.md").write_text(idx + "\n## Notes\nHand note kept.\n")
(rp / "scripts" / "b.py").write_text('"""Second script."""\n')
out = hook("project-bind", {"hook_event_name": "PostToolUse", "tool_name": "Write", "session_id": S1, "tool_input": {"file_path": str(rp / "scripts" / "b.py")}})
idx = (rp / "INDEX.md").read_text()
check("PostToolUse(Write) re-indexes the project", "`scripts/b.py` (python): Second script." in idx, idx)
check("...keeping the use/proved lines under their entry", "- `scripts/a.py` (python): Fee replay for one order.\n  - use: replay one order's fees\n  - proved: rounding is per line" in idx, idx)
check("...and the ## Notes section", "## Notes\nHand note kept." in idx)
check("...and lists the items with status", "- `items/DEMO-1500/` (open)" in idx, idx)

print("--- project-bind: PreToolUse(Write) on the first durable write ---")
S8 = "88888888-aaaa-4000-8000-000000000000"
target = str(rp / "data" / "q.csv")
pre = hook("project-bind", {"hook_event_name": "PreToolUse", "tool_name": "Write", "session_id": S8, "tool_input": {"file_path": target}})
check("the first write under a project binds silently (no decision)", binding(S8) == "project:invoices" and pre.stdout.strip() == "", pre.stdout + binding(S8))
post = hook("project-bind", {"hook_event_name": "PostToolUse", "tool_name": "Write", "session_id": S8, "tool_input": {"file_path": target}})
check("...and the next PostToolUse injects the slice once", "PROJECT: invoices" in post.stdout and "by this write" in post.stdout, post.stdout)
post = hook("project-bind", {"hook_event_name": "PostToolUse", "tool_name": "Write", "session_id": S8, "tool_input": {"file_path": target}})
check("...only once", "PROJECT:" not in post.stdout)
S9 = "99999999-aaaa-4000-8000-000000000000"
pre = hook("project-bind", {"hook_event_name": "PreToolUse", "tool_name": "Write", "session_id": S9, "tool_input": {"file_path": str(T / "elsewhere.py")}})
check("a write outside the work root does not bind", binding(S9) == "")

print("--- retro writers ---")
tr = T / "t.jsonl"
rows = [
    {"type": "user", "toolDenialKind": "permission-rule", "message": {"content": [{"type": "tool_result", "tool_use_id": "d1", "is_error": True, "content": "Permission to use Bash with command rm -rf x has been denied."}]}},
    {"type": "user", "message": {"content": "no, use the wrapper instead"}},
    {"type": "user", "message": {"content": "carry on with the plan"}},
    {"type": "attachment", "attachment": {"type": "hook_blocking_error", "hookName": "Stop", "blockingError": {"blockingError": "Review agents already completed 3 rounds this session, the budget (churn brake)."}}},
    {"type": "attachment", "attachment": {"type": "hook_non_blocking_error", "command": "~/.claude/hooks/test-exec-gate", "exitCode": 1}},
    {"type": "system", "subtype": "compact_boundary"},
    {"type": "user", "message": {"content": "next task after the Stop review"}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "p1", "is_error": True, "content": "PreToolUse:Bash hook error: bare pytest is blocked"}]}},
    {"type": "user", "message": {"content": "that was the test gate again"}},
    {"type": "user", "message": {"content": "/compact"}},
]
for i in range(3):
    rows.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}]}})
    rows.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "is_error": True, "content": "Exit code 1\nboom"}]}})
tr.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
before = sum(1 for _ in [1])
out = hook("retro-extract", {"hook_event_name": "SessionEnd", "session_id": S1, "transcript_path": str(tr)})
inbox = "".join(f.read_text() for f in (ROOT / "retro").glob("*.md"))
for tag, needle in (("denial", "denial 1x permission-rule: Permission to use Bash"), ("correction", "correction no, use the wrapper instead"), ("block", "block 1x Stop: Review agents"), ("hook-error", "hook-error 1x ~/.claude/hooks/test-exec-gate rc=1"), ("compaction", "compaction 1 compaction(s)"), ("tool-error", "tool-error 3x Bash: Exit code #"), ("review", "review review rounds past budget")):
    check(f"retro-extract writes a {tag} line", needle in inbox, inbox[-1500:])
check("retro-extract lines carry the session's project/item", f"[auto] " in inbox and " 11111111 invoices/DEMO-1500 denial" in inbox)
check("a plain follow-up prompt is not a correction", "correction carry on" not in inbox)
check("a prompt right after a PreToolUse block is a correction", "correction that was the test gate again" in inbox, inbox[-800:])
check("the prompt after a Stop block and slash commands are not corrections", "correction next task" not in inbox and "correction /compact" not in inbox)
check("a PreToolUse block is counted", "block 1x PreToolUse:Bash hook error: bare pytest is blocked" in inbox)
out = hook("retro-extract", {"hook_event_name": "SessionEnd", "session_id": S1, "transcript_path": str(tr)}, env=dict(ENV, CI_WATCH_ACTIVE="1"))
check("retro-extract skips under CI_WATCH_ACTIVE", "".join(f.read_text() for f in (ROOT / "retro").glob("*.md")) == inbox)
out = task("retro", "fact: fees round per line", "--session", S1)
check("agent-task retro writes a model line with tag and item", "- [model] " in out.stdout and " 11111111 invoices/DEMO-1500 fact fees round per line" in out.stdout, out.stdout + out.stderr)
out = task("retro", "the /retro command line", "--source", "user")
check("/retro (source user) writes a user line", out.stdout.startswith("- [user] ") and " - - note the /retro command line" in out.stdout, out.stdout)
pend = task("retro", "--pending").stdout
check("--pending lists unprocessed lines", "fees round per line" in pend and "[auto]" in pend)
ids = [x.split(" ", 1)[0] for x in pend.splitlines()]
check("...each after its <week>:<line>-<hash> id", all(re.fullmatch(r"\d{4}-W\d\d:\d+-[0-9a-f]{8}", i) for i in ids) and all(x.split(" ", 1)[1].startswith("- [") for x in pend.splitlines()), pend[:300])
bare = task("retro", "--mark")
check("--mark with no ids refuses and marks nothing", bare.returncode == 2 and task("retro", "--pending").stdout == pend, bare.stderr)
task("retro", "fact: written after the --pending read")
out = task("retro", "--mark", *ids)
left = task("retro", "--pending").stdout
check("--mark <ids> marks exactly the lines --pending showed", f"marked {len(ids)} lines" in out.stdout and left.count("\n") == 1 and "written after the --pending read" in left, out.stdout + left)
check("...processed lines carry the stamp", "(processed " in "".join(f.read_text() for f in (ROOT / "retro").glob("*.md")))
DENV = dict(ENV, CLAUDE_OUT_ROOT=str(T / "retro-dup"))
task("retro", "fact: the same note twice", "--session", S1, env=DENV)
ids = [x.split(" ", 1)[0] for x in task("retro", "--pending", env=DENV).stdout.splitlines()]
task("retro", "fact: the same note twice", "--session", S1, env=DENV)
both = task("retro", "--pending", env=DENV).stdout.splitlines()
out = task("retro", "--mark", *ids, env=DENV)
left = task("retro", "--pending", env=DENV).stdout
check("an identical note appended after --pending gets its own id and stays pending after --mark", len(both) == 2 and both[0].split(" ", 1)[0] != both[1].split(" ", 1)[0] and "marked 1 lines" in out.stdout and left.count("\n") == 1 and "the same note twice" in left, "\n".join(both) + out.stdout + left)
note = "fact: it's \"quoted\" $(touch " + str(T / "pwned") + ") `id` and\nsecond line"
out = run([str(BIN / "agent-task"), "retro", "--source", "user", "--stdin"], stdin=note)
check("retro --stdin records the note verbatim, never evaluated", out.stdout.startswith("- [user] ") and "fact it's \"quoted\" $(touch" in out.stdout and "`id` and second line" in out.stdout and not (T / "pwned").exists(), out.stdout + out.stderr)
cmd = (HOOKS.parent / "commands" / "retro.md")
check("/retro passes the note through a quoted heredoc on stdin", cmd.is_file() and "--stdin <<'NOTE'\n$ARGUMENTS\nNOTE" in cmd.read_text() and '"$ARGUMENTS"' not in cmd.read_text())

print("--- retro inbox: concurrent appends and marks lose nothing ---")
CR = T / "retro-race"
CENV = dict(ENV, CLAUDE_OUT_ROOT=str(CR))
procs = [subprocess.Popen([str(BIN / "agent-task"), "retro", f"fact: race line {i:02}"], env=CENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(24)]
marks = []
for _ in range(6):
    pend = subprocess.run([str(BIN / "agent-task"), "retro", "--pending"], env=CENV, capture_output=True, text=True).stdout
    ids = [x.split(" ", 1)[0] for x in pend.splitlines()]
    if ids:
        marks.append(subprocess.Popen([str(BIN / "agent-task"), "retro", "--mark", *ids], env=CENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
for pr in procs + marks:
    pr.wait()
text = "".join(f.read_text() for f in (CR / "retro").glob("*.md"))
check("24 parallel appends with marks in flight: every line kept, one header", all(f"race line {i:02}" in text for i in range(24)) and text.count("# Retro inbox") == 1, text[-600:])

print("--- harvest, brief, where, ls, migrate, shim ---")
hv = task("harvest", "invoices/DEMO-1500")
tj = json.loads((rp / "items/DEMO-1500/task.json").read_text())
check("harvest asks the three questions and marks the item", all(s in hv.stdout for s in ("1. Domain docs", "2. Project knowledge", "3. Scripts", "never overwrite it")) and tj["status"] == "harvested", hv.stdout + hv.stderr)
br = task("brief", "invoices/DEMO-1500", "--name", "fix")
bf = Path(br.stdout.strip())
check("brief writes a skeleton with the injected slice", bf.is_file() and "PROJECT: invoices" in bf.read_text() and "Learnings:" in bf.read_text(), br.stdout + br.stderr)
w = task("where", "--path", "--session", "deadbeef-0000")
check("where --path exits 1 when unbound", w.returncode == 1 and w.stdout == "")
w = task("where", "--path", "--session", S1)
check("where --path prints the bound folder", w.returncode == 0 and w.stdout.strip() == str(rp / "items/DEMO-1500"), w.stdout)
ls = task("ls", "--paths").stdout
check("ls --paths shows paths and legacy folders", "invoices  status=active" in ls and "paths=src/libinvoice/**" in ls and "tasks/DEMO-42-widget-fix  (legacy)" in ls, ls)
(ROOT / "tasks" / "DEMO-43-widget-cache").mkdir()
mg = task("migrate", "--plan")
check("migrate --plan proposes a grouping and moves nothing", "widget" in mg.stdout and "DEMO-42-widget-fix, DEMO-43-widget-cache" in mg.stdout and (ROOT / "tasks/DEMO-43-widget-cache").is_dir(), mg.stdout)
check("migrate without --plan refuses", task("migrate").returncode == 2)
ct = HOOKS.parent / "bin" / "claude-task"
if ct.exists():
    S10 = "aaaaaaaa-aaaa-4000-8000-000000000000"
    out = run([str(ct), "bind", "DEMO-77", "billing", "fix", "--session", S10])
    check("claude-task bind <TICKET> <desc> still works (project of one)", binding(S10) == "project:DEMO-77/DEMO-77" and "billing fix" in (ROOT / "projects/DEMO-77/PROJECT.md").read_text(), out.stdout + out.stderr)
    out = run([str(ct), "bind", "DEMO-42", "--session", S10])
    check("claude-task bind of a legacy ticket rebinds to its folder", binding(S10) == "DEMO-42-widget-fix", out.stdout + out.stderr)

print("--- rebinding a project of one keeps its item; one folder per ticket ---")
S16 = "16161616-aaaa-4000-8000-000000000000"
S17 = "17171717-aaaa-4000-8000-000000000000"
S18 = "18181818-aaaa-4000-8000-000000000000"
task("bind", "DEMO-91", "billing", "fix", "--session", S16)
w1 = task("where", "--path", "--session", S16).stdout.strip()
(ROOT / "projects/DEMO-91/items/DEMO-91/HANDOFF.md").write_text("# handoff\n")
out = task("bind", "DEMO-91", "--session", S17)
w2 = task("where", "--path", "--session", S17).stdout.strip()
item91 = str(ROOT / "projects/DEMO-91/items/DEMO-91")
check("binding an existing project of one by its ticket keeps the item", w1 == item91 and w2 == item91 and binding(S17) == "project:DEMO-91/DEMO-91", f"{w1} | {w2} | {binding(S17)}")
check("...and surfaces its handoff", "HANDOFF:" in out.stdout, out.stdout)
task("bind", "demo-91", "--session", S18)
check("a lower-case spelling resolves to the same project and item", binding(S18) == "project:DEMO-91/DEMO-91", binding(S18))
check("one folder per ticket: one project and one item for DEMO-91", [p.name for p in (ROOT / "projects").iterdir() if p.name.lower() == "demo-91"] == ["DEMO-91"] and [p.name for p in (ROOT / "projects/DEMO-91/items").iterdir()] == ["DEMO-91"])
check("one folder per ticket: DEMO-1500 has one item folder across projects", len(list((ROOT / "projects").glob("*/items/DEMO-1500"))) == 1)
task("bind", "invoices/DEMO-1500", "--session", S18)
task("bind", "DEMO-1500", "--session", S18)
check("...after binding it again by project/item and by ticket", len(list((ROOT / "projects").glob("*/items/DEMO-1500"))) == 1 and binding(S18) == "project:invoices/DEMO-1500", binding(S18))

print("--- adopting per-session folders never moves a project ---")
S19 = "19191919-aaaa-4000-8000-000000000000"
pj = ROOT / "projects" / f"old-{S19[:8]}"
(pj / "knowledge").mkdir(parents=True)
(pj / "knowledge" / "findings.md").write_text("# Findings\n")
sess = ROOT / "gate-repo" / f"2026-10-03-1200-{S19[:8]}"
sess.mkdir(parents=True)
(sess / "note.md").write_text("# note\n")
task("bind", "invoices", "--session", S19)
check("a per-session folder moves into the bound project", sess.is_symlink() and (rp / "out/sessions" / sess.name / "note.md").is_file())
check("a project whose name ends in the session id stays put", pj.is_dir() and not pj.is_symlink() and (pj / "knowledge/findings.md").is_file())

print("--- harvest skips checkouts and dependencies ---")
hv_item = ROOT / "projects/invoices/items/DEMO-1501"
for rel in ("scripts/keep.py", "worktrees/wt/src/deep.py", ".git/hooks/pre.sh", "node_modules/pkg/build.py", "out/sessions/s/x.py"):
    (hv_item / rel).parent.mkdir(parents=True, exist_ok=True)
    (hv_item / rel).write_text('"""x."""\n')
hv = task("harvest", "invoices/DEMO-1501").stdout
check("harvest lists the item's scripts only", "scripts/keep.py" in hv and not any(s in hv for s in ("deep.py", "pre.sh", "build.py", "sessions/s/x.py")), hv)

print("--- one work-root resolver ---")
H = T / "home"
(H / "claude-scratch").mkdir(parents=True)
HENV = dict(ENV, HOME=str(H), TMPDIR=str(H / "tmp"))  # T itself is under $TMPDIR, which comment-guard skips
HENV.pop("CLAUDE_OUT_ROOT")
HENV.pop("CLAUDE_CONFIG_DIR", None)
o = task("session-start", "--session", "20202020-aaaa", "--source", "startup", "--repo", "r", env=HENV).stdout
check("agent-task: ~/claude-scratch when only it exists", o.startswith(f"OUT DIR: {H}/claude-scratch/r/"), o)
cg = hook("comment-guard", {"hook_event_name": "PostToolUse", "tool_name": "Write", "session_id": "c", "tool_input": {"file_path": str(H / "claude-scratch/s.py"), "content": "# a\n" * 12 + "x = 1\n"}}, env=HENV)
check("comment-guard: the same root (no nudge for a scratch file there)", cg.returncode == 0 and cg.stdout.strip() == "", cg.stdout)
cg = hook("comment-guard", {"hook_event_name": "PostToolUse", "tool_name": "Write", "session_id": "c", "tool_input": {"file_path": str(H / "code/s.py"), "content": "# a\n" * 12 + "x = 1\n"}}, env=HENV)
check("comment-guard control: a file outside the root is nudged", "additionalContext" in cg.stdout, cg.stdout + cg.stderr)
(H / "agent-work").mkdir()
o = task("session-start", "--session", "20202020-aaaa", "--source", "startup", "--repo", "r", env=HENV).stdout
check("agent-task: ~/agent-work once it exists", o.startswith(f"OUT DIR: {H}/agent-work/r/"), o)
(OV / "kit-root.env").write_text(f"AGENT_WORK_ROOT={T}/alt\n")
o = task("session-start", "--session", "20202020-aaaa", "--source", "startup", "--repo", "r", env=dict(HENV, KIT_ENV=str(OV / "kit-root.env"))).stdout
cg = hook("comment-guard", {"hook_event_name": "PostToolUse", "tool_name": "Write", "session_id": "c", "tool_input": {"file_path": str(T / "alt/s.py"), "content": "# a\n" * 12 + "x = 1\n"}}, env=dict(HENV, KIT_ENV=str(OV / "kit-root.env")))
check("AGENT_WORK_ROOT wins in both", o.startswith(f"OUT DIR: {T}/alt/r/") and cg.stdout.strip() == "", o + cg.stdout)

print(f"=== PASS {passes} FAIL {fails} ===")
sys.exit(1 if fails else 0)
