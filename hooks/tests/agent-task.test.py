#!/usr/bin/env python3
"""Tests for bin/agent-task, hooks/project-bind, hooks/retro-extract and the session-start slice,
against a synthetic work root. HOOKS_DIR=<dir> tests another copy of the hooks (its ../bin too)."""

import json
import os
import re
import shutil
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

def ctx_of(out: subprocess.CompletedProcess) -> str:
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""


def prompt(sid: str, text: str, cwd: Path = T, env: dict | None = None) -> subprocess.CompletedProcess:
    return hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": str(cwd), "prompt": text}, env=env)


def git(*a: str) -> None:
    subprocess.run(["git", *a], check=True, capture_output=True)


HELPER = "00000000-hhhh-4000-8000-000000000000"  # creates items by explicit bind, as /bind does

print("--- resolver: certain rules only, nothing created from a prompt ---")
r = resolve("pick up CORE-2048 today")
check("ticket-project: a ticket only one project lists binds that project alone", r["rule"] == "ticket-project" and r["target"] == "invoices", str(r))
r = resolve("QQ-77 something unrelated")
check("a ticket nothing maps is no rule", r["rule"] == "" and r["target"] == "", str(r))
r = resolve("smoke test CORE-119-REL-TEST and CORE-119-REL")
check("release-branch spellings name no ticket (tickets_in)", r["rule"] == "" and "CORE-119" not in json.dumps(r), str(r))
task("bind", "invoices/DEMO-1500", "--session", HELPER)
r = resolve("pick up DEMO-1500 today")
check("ticket: a prompt ticket mapping to one existing item", r["rule"] == "ticket" and r["target"] == "invoices/DEMO-1500", str(r))
r = resolve(f"Continue from {ROOT}/projects/invoices/items/DEMO-1500/HANDOFF.md please")
check("path: a pasted item HANDOFF.md path", r["rule"] == "path" and r["target"] == "invoices/DEMO-1500", str(r))
(ROOT / "tasks").mkdir(exist_ok=True)
(ROOT / "tasks" / "DEMO-1500-invoices").symlink_to(ROOT / "projects/invoices/items/DEMO-1500")
r = resolve(f"Takeover {ROOT}/tasks/DEMO-1500-invoices/HANDOFF.md")
check("path: a legacy tasks/ link counts as its item", r["rule"] == "path" and r["target"] == "invoices/DEMO-1500", str(r))
task("bind", "web-tracking/DEMO-310", "--session", HELPER)
for text, want in (("start on DEMO-310 like DEMO-1500", "web-tracking/DEMO-310"), ("implement the DEMO-1500 approach here", "invoices/DEMO-1500")):
    r = resolve(text)
    check(f"ticket: the asked-for or only ticket wins ({text!r})", r["rule"] == "ticket" and r["target"] == want, str(r))
r = resolve("how did DEMO-1500 and DEMO-310 differ?")
check("two tickets mapping to two items: no rule, both offered", r["rule"] == "" and set(r["candidates"][:2]) == {"invoices/DEMO-1500", "web-tracking/DEMO-310"}, str(r))

print("--- resolver: worktree, branch and a new <TICKET>-* worktree ---")
REPO = T / "repo"
git("init", "-q", "-b", "DEMO-1500-invoices-x", str(REPO))
git("-C", str(REPO), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
r = resolve("", "--cwd", str(REPO))
check("branch: a ticket branch mapping to one item", r["rule"] == "branch" and r["target"] == "invoices/DEMO-1500", str(r))
WT555 = REPO / ".claude/worktrees/DEMO-555-foo"
git("-C", str(REPO), "worktree", "add", "-q", "-b", "DEMO-555-foo", str(WT555))
r = resolve("", "--cwd", str(WT555))
check("worktree-new: a DEMO-555-* worktree with no item creates a project of one", r["rule"] == "worktree-new" and r["target"] == "DEMO-555", str(r))
project("wtproj", "scope: worktree tests\ntickets: DEMO-556")
WT556 = REPO / ".claude/worktrees/DEMO-556-bar"
git("-C", str(REPO), "worktree", "add", "-q", "-b", "DEMO-556-bar", str(WT556))
r = resolve("", "--cwd", str(WT556))
check("worktree-new: ...under the one project that lists the ticket", r["rule"] == "worktree-new" and r["target"] == "wtproj/DEMO-556", str(r))
WTLONG = REPO / ".claude/worktrees/abcdefg-12-x"
git("-C", str(REPO), "worktree", "add", "-q", "-b", "abcdefg-12-x", str(WTLONG))
r = resolve("", "--cwd", str(WTLONG))
check("a branch prefix over 6 characters names no ticket (TICKET_RE's rule)", r["rule"] == "" and "ABCDEFG" not in json.dumps(r), str(r))
WTPORT = REPO / ".claude/worktrees/port-02-guards"
git("-C", str(REPO), "worktree", "add", "-q", "-b", "port-02-guards", str(WTPORT))
OVP = T / "ov-prefixes"
OVP.mkdir()
(OVP / "kit.env").write_text("TICKET_PREFIXES=DEMO,CORE\n")
r = json.loads(task("resolve", "", "--cwd", str(WTPORT), env=dict(ENV, KIT_ENV=str(OVP / "kit.env"))).stdout or "{}")
check("TICKET_PREFIXES set: port-02-guards names no ticket", r.get("rule") == "" and "PORT" not in json.dumps(r), str(r))
r = json.loads(task("resolve", "", "--cwd", str(WT556), env=dict(ENV, KIT_ENV=str(OVP / "kit.env"))).stdout or "{}")
check("...and a listed prefix still does", r.get("rule") == "worktree-new" and r.get("target") == "wtproj/DEMO-556", str(r))
project("wtproj2", "scope: second owner\ntickets: DEMO-558")
project("wtproj3", "scope: third owner\ntickets: DEMO-558")
SAMB = "5a5a5a5a-aaaa-4000-8000-000000000000"
out = task("bind", "DEMO-558", "--session", SAMB)
check("bind <TICKET> two projects list refuses and names both, creating nothing", out.returncode != 0 and "wtproj2" in out.stderr and "wtproj3" in out.stderr and binding(SAMB) == "" and not (ROOT / "projects/DEMO-558").exists() and not (ROOT / "projects/wtproj2/items/DEMO-558").exists(), out.stdout + out.stderr)
WT558 = REPO / ".claude/worktrees/DEMO-558-amb"
git("-C", str(REPO), "worktree", "add", "-q", "-b", "DEMO-558-amb", str(WT558))
r = resolve("", "--cwd", str(WT558))
check("...and a DEMO-558-* worktree is no rule, offering both (one ticket_target for bind and decide)", r["rule"] == "" and set(r["candidates"][:2]) == {"wtproj2", "wtproj3"}, str(r))
WTH = REPO / ".claude/worktrees/hosts"
git("-C", str(REPO), "worktree", "add", "-q", "-b", "hosts", str(WTH))
r = resolve("", "--cwd", str(WTH))
check("a worktree named after no ticket, unmapped: no rule", r["rule"] == "", str(r))
task("bind", "web-tracking/DEMO-310", "--session", HELPER, "--cwd", str(WTH))
tj = json.loads((ROOT / "projects/web-tracking/items/DEMO-310/task.json").read_text())
check("binding from inside a worktree records it in task.json", os.path.realpath(WTH) in [os.path.realpath(w) for w in tj.get("worktrees", [])], str(tj))
idx = json.loads((ROOT / ".index.json").read_text())
rec = next((x for x in idx["items"] if x["item"] == "DEMO-310"), {})
check("...and the derived .index.json carries it", rec.get("project") == "web-tracking" and rec.get("worktrees") == tj.get("worktrees") and HELPER[:8] in rec.get("sessions", []), str(rec))
r = resolve("", "--cwd", str(WTH))
check("worktree: a worktree the index maps", r["rule"] == "worktree" and r["target"] == "web-tracking/DEMO-310", str(r))
REL = T / "relrepo"
git("init", "-q", "-b", "CORE-119-REL", str(REL))
git("-C", str(REL), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
r = resolve("give me the DML", "--cwd", str(REL))
check("a release branch binds nothing and offers nothing without this repo's items", r["rule"] == "" and r["candidates"] == [], str(r))

print("--- project-bind: UserPromptSubmit ---")
S1 = "11111111-aaaa-4000-8000-000000000000"
out = prompt(S1, "Fix DEMO-1500 rounding please")
ctx = ctx_of(out)
check("a prompt ticket of one existing item binds it", binding(S1) == "project:invoices/DEMO-1500", binding(S1) + out.stderr)
check("the bind announces its rule, then the slice", ctx.startswith("bound: invoices/DEMO-1500 (rule: ticket)") and "PROJECT: invoices · item DEMO-1500" in ctx, ctx)
check("the item folder has task.json and tmp/", (ROOT / "projects/invoices/items/DEMO-1500/task.json").is_file() and (ROOT / "projects/invoices/items/DEMO-1500/tmp").is_dir())
check("the tab label is <project> · <item>", (T / "labels" / S1).read_text() == "invoices · DEMO-1500")
S2 = "22222222-aaaa-4000-8000-000000000000"
out = prompt(S2, "what does the cache layer do? also convert to UTF-8")
check("a Q&A prompt never binds (UTF-8 is no ticket)", out.stdout.strip() == "" and binding(S2) == "", out.stdout + out.stderr)
S3 = "33333333-aaaa-4000-8000-000000000000"
out = prompt(S3, "start ABC-12 please")
check("a ticket nothing maps creates nothing and binds nothing", binding(S3) == "" and not (ROOT / "projects/ABC-12").exists(), binding(S3) + out.stdout)
S3B = "3333333b-aaaa-4000-8000-000000000000"
out = prompt(S3B, "CORE-2048 follow-up: tweak the copy")
check("ticket-project: binds the project only (no item made)", binding(S3B) == "project:invoices" and not (ROOT / "projects/invoices/items/CORE-2048").exists() and "rule: ticket-project" in ctx_of(out), binding(S3B) + out.stdout)
S3C = "3333333c-aaaa-4000-8000-000000000000"
out = prompt(S3C, "smoke: deploy CORE-119-REL-TEST to staging")
check("a release-branch name in a prompt never makes a project", binding(S3C) == "" and not (ROOT / "projects/CORE-119").exists(), out.stdout)
task("bind", "web-tracking/DEMO-900", "--session", HELPER)
task("bind", "web-search/DEMO-900", "--session", HELPER)
S4 = "44444444-aaaa-4000-8000-000000000000"
out = prompt(S4, "look at DEMO-900 again")
ctx = ctx_of(out)
check("ambiguous: one line per candidate with its /bind argument, no bind", binding(S4) == "" and "/bind web-search/DEMO-900" in ctx and "/bind web-tracking/DEMO-900" in ctx and ctx.startswith("unbound:"), ctx)
out = prompt(S4, "and DEMO-900 once more")
check("...offered once per distinct offer", out.stdout.strip() == "", out.stdout)
out = prompt(S4, "/bind web-search/DEMO-900")
check("a /bind prompt is left to the command", out.stdout.strip() == "" and binding(S4) == "", out.stdout)
S5 = "55555555-aaaa-4000-8000-000000000000"
out = prompt(S5, "DEMO-42 next step")
check("a real legacy folder never binds from a prompt", binding(S5) == "", out.stdout)
out = task("bind", "DEMO-42", "--session", S5)
check("an explicit bind of a legacy folder still works, with its handoff", binding(S5) == "DEMO-42-widget-fix" and "TASK DIR:" in out.stdout and "Handoff DEMO-42" in out.stdout, out.stdout)
out = prompt(S1, "now ABC-99 too")
check("a bound session is not rebound by a later ticket", binding(S1) == "project:invoices/DEMO-1500" and out.stdout.strip() == "")
out = prompt(S2, "DEMO-1500", env=dict(ENV, CI_WATCH_ACTIVE="1"))
check("CI_WATCH_ACTIVE: never binds", binding(S2) == "")

print("--- binding: worktrees, entering one mid-session, session start ---")
S30 = "30303030-aaaa-4000-8000-000000000000"
out = prompt(S30, "go", cwd=WT555)
ctx = ctx_of(out)
check("a prompt in a new DEMO-555-* worktree creates and binds its item", binding(S30) == "project:DEMO-555/DEMO-555" and ctx.startswith("bound: DEMO-555/DEMO-555 (rule: worktree-new)"), binding(S30) + ctx)
tj = json.loads((ROOT / "projects/DEMO-555/items/DEMO-555/task.json").read_text())
check("...recording the worktree and branch", [os.path.realpath(w) for w in tj.get("worktrees", [])] == [os.path.realpath(WT555)] and tj.get("branches") == ["DEMO-555-foo"], str(tj))
inbox = "".join(f.read_text() for f in (ROOT / "retro").glob("*.md")) if (ROOT / "retro").is_dir() else ""
check("...and the project of one is in the retro inbox", "project created project of one DEMO-555" in inbox, inbox[-300:])
S31 = "31313131-aaaa-4000-8000-000000000000"
prompt(S31, "Fix DEMO-1500 rounding please", cwd=REPO)
check("bound in the main checkout by its branch", binding(S31) == "project:invoices/DEMO-1500", binding(S31))
out = prompt(S31, "next", cwd=REPO)
check("...the same checkout on the next prompt: silent, sticky", out.stdout.strip() == "" and binding(S31) == "project:invoices/DEMO-1500", out.stdout)
out = prompt(S31, "next", cwd=WT555)
ctx = ctx_of(out)
check("entering a mapped worktree mid-session rebinds, announcing the change", binding(S31) == "project:DEMO-555/DEMO-555" and ctx.startswith("bound: DEMO-555/DEMO-555 (rule: worktree; was invoices/DEMO-1500)"), binding(S31) + ctx)
out = hook("project-bind", {"hook_event_name": "PostToolUse", "tool_name": "EnterWorktree", "session_id": S31, "cwd": str(REPO), "tool_input": {"name": "DEMO-556-bar"}, "tool_response": {"worktreePath": str(WT556), "message": "ok"}})
check("PostToolUse(EnterWorktree) never creates an item for a bound session", binding(S31) == "project:DEMO-555/DEMO-555" and ctx_of(out) == "" and not (ROOT / "projects/wtproj/items/DEMO-556").exists(), binding(S31) + out.stdout + out.stderr)
out = prompt(S31, "next", cwd=WTPORT)
check("...nor a project of one: a prompt in port-02-guards (checkout changed) stays bound", binding(S31) == "project:DEMO-555/DEMO-555" and not (ROOT / "projects/PORT-02").exists(), binding(S31) + out.stdout)
out = hook("project-bind", {"hook_event_name": "PostToolUse", "tool_name": "EnterWorktree", "session_id": S31, "cwd": str(REPO), "tool_input": {"name": "port-02-guards"}, "tool_response": {"worktreePath": str(WTPORT), "message": "ok"}})
check("...nor does EnterWorktree into port-02-guards", binding(S31) == "project:DEMO-555/DEMO-555" and ctx_of(out) == "" and not (ROOT / "projects/PORT-02").exists(), binding(S31) + out.stdout + out.stderr)
S34 = "34343434-aaaa-4000-8000-000000000000"
out = hook("project-bind", {"hook_event_name": "PostToolUse", "tool_name": "EnterWorktree", "session_id": S34, "agent_id": "sub-1", "cwd": str(REPO), "tool_input": {"name": "DEMO-556-bar"}, "tool_response": {"worktreePath": str(WT556), "message": "ok"}})
check("a subagent's EnterWorktree (agent_id) binds nothing", binding(S34) == "" and not (ROOT / "projects/wtproj/items/DEMO-556").exists(), binding(S34) + out.stdout)
out = hook("project-bind", {"hook_event_name": "PostToolUse", "tool_name": "EnterWorktree", "session_id": S34, "cwd": str(REPO), "tool_input": {"name": "DEMO-556-bar"}, "tool_response": {"worktreePath": str(WT556), "message": "ok"}})
ctx = ctx_of(out)
check("PostToolUse(EnterWorktree) binds an unbound session's new worktree item at once", binding(S34) == "project:wtproj/DEMO-556" and "rule: worktree-new" in ctx, binding(S34) + ctx + out.stderr)
out = prompt(S31, "next", cwd=WT556)
check("a bound session entering a worktree of an EXISTING item moves to it", binding(S31) == "project:wtproj/DEMO-556" and "rule: worktree; was DEMO-555/DEMO-555" in ctx_of(out), binding(S31) + out.stdout)
S32 = "32323232-aaaa-4000-8000-000000000000"
o = task("session-start", "--session", S32, "--source", "startup", env=dict(ENV, CLAUDE_PROJECT_DIR=str(WT555))).stdout
check("session start inside a mapped worktree binds before the first prompt", binding(S32) == "project:DEMO-555/DEMO-555" and o.startswith("bound: DEMO-555/DEMO-555 (rule: worktree)"), o)
o = task("session-start", "--session", S32, "--source", "resume", env=dict(ENV, CLAUDE_PROJECT_DIR=str(REL))).stdout
check("resume keeps the session's own binding (rule: session)", binding(S32) == "project:DEMO-555/DEMO-555" and o.startswith("bound: DEMO-555/DEMO-555 (rule: session)"), o)
task("bind", "web-search/DEMO-777", "--session", HELPER, "--repo", "relrepo")
S33 = "33333330-aaaa-4000-8000-000000000000"
o = task("session-start", "--session", S33, "--source", "startup", env=dict(ENV, CLAUDE_PROJECT_DIR=str(REL))).stdout
check("unbound on a release branch: OUT DIR plus this repo's recent open items, still unbound", o.startswith("OUT DIR:") and "Recent open items for relrepo" in o and "/bind web-search/DEMO-777" in o and binding(S33) == "", o)

print("--- session-start: lazy OUT DIR, /clear inheritance ---")
TAB = T / "state" / "task-bindings" / "tab-iterm-session-id-w0t0p0-tab-one"
task("bind", "DEMO-42", "--session", S5)
check("a bind points the tab at that session", TAB.is_file() and TAB.read_text().startswith("DEMO-42-widget-fix\t" + S5), TAB.read_text() if TAB.is_file() else "no tab file")
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
check("...marked inherited", (T / "state" / "task-bindings" / S6C[:8]).read_text() == "DEMO-42-widget-fix\ninherited\nrule:tab\n", (T / "state" / "task-bindings" / S6C[:8]).read_text())
task("session-start", "--session", S1, "--source", "resume", "--repo", "gate-repo")
S6D = "6666666d-aaaa-4000-8000-000000000000"
task("session-start", "--session", S6D, "--source", "clear", "--repo", "gate-repo")
check("resuming a bound session points the tab at it; clear then inherits that", binding(S6D) == "project:invoices/DEMO-1500", binding(S6D))

print("--- project-bind: a binding inherited across /clear ---")
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S6D, "cwd": str(T), "prompt": "what next?"})
check("a prompt with no ticket keeps the inherited binding and its mark", out.stdout.strip() == "" and binding(S6D) == "project:invoices/DEMO-1500" and "inherited" in (T / "state/task-bindings" / S6D[:8]).read_text(), out.stdout)
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S6D, "cwd": str(T), "prompt": "carry on with DEMO-1500"})
check("naming the inherited item keeps it, silently, and makes it sticky", out.stdout.strip() == "" and binding(S6D) == "project:invoices/DEMO-1500" and "inherited" not in (T / "state/task-bindings" / S6D[:8]).read_text(), out.stdout)
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S6D, "cwd": str(T), "prompt": "now DEMO-42"})
check("...after which another ticket does not rebind", binding(S6D) == "project:invoices/DEMO-1500" and out.stdout.strip() == "")
out = hook("project-bind", {"hook_event_name": "UserPromptSubmit", "session_id": S6C, "cwd": str(T), "prompt": "pick up DEMO-1500 now"})
ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""
check("an inherited binding moves on a certain rule, announcing it", binding(S6C) == "project:invoices/DEMO-1500" and ctx.startswith("bound: invoices/DEMO-1500 (rule: ticket; was DEMO-42-widget-fix)") and "kept from before /clear" in ctx and "PROJECT: invoices" in ctx, ctx)
task("bind", "DEMO-78", "--session", S6B)
S6E = "6666666e-aaaa-4000-8000-000000000000"
task("session-start", "--session", S6E, "--source", "clear", "--repo", "gate-repo")
out = prompt(S6E, "start on DEMO-300")
check("an inherited binding stays on a ticket no rule maps (nothing created)", binding(S6E) == "project:DEMO-78/DEMO-78" and out.stdout.strip() == "" and not (ROOT / "projects/DEMO-300").exists(), binding(S6E) + out.stdout)

print("--- project-bind: a ticket branch with no item, a mention on it ---")
BR = T / "brrepo"
git("init", "-q", "-b", "DEMO-200-widget", str(BR))
git("-C", str(BR), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
S12 = "12121212-aaaa-4000-8000-000000000000"
out = prompt(S12, "implement this like DEMO-100 did", cwd=BR)
check("a main-checkout ticket branch with no item creates nothing (only a worktree or /bind does)", binding(S12) == "" and not (ROOT / "projects/DEMO-100").exists() and not (ROOT / "projects/DEMO-200").exists(), binding(S12) + out.stdout)
S13 = "13131313-aaaa-4000-8000-000000000000"
out = prompt(S13, "port the fix from DEMO-1500 here", cwd=BR)
check("...a prompt ticket of one existing item still binds it there", binding(S13) == "project:invoices/DEMO-1500", binding(S13) + out.stdout)

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
for key in ("ABC-500", "CORE-9", "DEMO-501"):
    task("bind", f"invoices/{key}", "--session", HELPER)
out = prompt(S14, "start ABC-500 please", env=KENV)
check("with jira-prefs.md beside kit.env, an unknown prefix never binds", out.stdout.strip() == "" and binding(S14) == "", out.stdout)
for n, (key, why) in enumerate((("CORE-9", "a `## <KEY> notes` heading"), ("DEMO-501", "project_key"))):
    sid = f"1515151{n}-aaaa-4000-8000-000000000000"
    prompt(sid, f"start {key}", env=KENV)
    check(f"...{why} is a known prefix", binding(sid) == f"project:invoices/{key}", binding(sid))
r = task("resolve", "x", "--branch", "ABC-9-thing", env=KENV)
check("...and an unknown branch prefix names no ticket", json.loads(r.stdout)["rule"] == "", r.stdout)

out = task("session-start", "--session", S1, "--source", "compact")
check("a bound session gets the announce line and the slice on compact", out.stdout.startswith("bound: invoices/DEMO-1500 (rule: ticket)") and "PROJECT: invoices · item DEMO-1500" in out.stdout, out.stdout)

print("--- context budget: compact re-injection, the nudge text ---")
item = ROOT / "projects/invoices/items/DEMO-1500"
(item / "HANDOFF.md").write_text("# Handoff DEMO-1500\n" + "".join(f"- step {i}: " + "detail " * 20 + "\n" for i in range(60)))
(item / "HANDOFF.auto.md").write_text("# HANDOFF.auto\n- branch: DEMO-1500-x\n")
out = task("session-start", "--session", S1, "--source", "compact", "--cap", "6000")
o = out.stdout
check("compact: the slice, then HANDOFF.md and HANDOFF.auto.md", o.startswith("bound: invoices/DEMO-1500") and "PROJECT: invoices · item DEMO-1500" in o and f"HANDOFF.md ({item}/HANDOFF.md):" in o and f"HANDOFF.auto.md ({item}/HANDOFF.auto.md):" in o, o)
check("...HANDOFF.md cut to its share of the cap, naming the file to Read", "... (cut at 2100 characters; Read" in o and "- step 59:" not in o, o[-800:])
check("...the slice's 8-line HANDOFF opening is not repeated", "Read it before starting" not in o, o)
check("...within the cap", len(o.rstrip("\n")) <= 6000, str(len(o)))
(item / "HANDOFF.auto.md").unlink()
o = task("session-start", "--session", S1, "--source", "compact").stdout
check("compact: no HANDOFF.auto.md, no section for it", "HANDOFF.auto.md" not in o and "HANDOFF.md (" in o, o[-400:])
o = task("session-start", "--session", S5, "--source", "compact").stdout
check("compact: a legacy folder gets its TASK DIR and HANDOFF.md", "TASK DIR:" in o and "HANDOFF.md (" in o and "Handoff DEMO-42" in o, o)
o = task("nudge", "--session", S1, "--ctx", "612345", "--at", "600000").stdout
check("nudge: a bound item names its HANDOFF.md, the project INDEX and agent-task retro", o.startswith("CONTEXT 612K: past the 600K handoff point") and f"update {item}/HANDOFF.md" in o and "projects/invoices/INDEX.md (it regenerates" in o and f"agent-task retro" in o and f"--session {S1}" in o, o)
(T / "cfg").mkdir()
(T / "cfg/settings.json").write_text('{"autoCompactWindow": 623000}\n')
o = task("nudge", "--session", S1, "--ctx", "575000", env=dict(ENV, CLAUDE_CONFIG_DIR=str(T / "cfg"))).stdout
check("nudge: with no --at, the point sits 30K before the compaction autoCompactWindow places", o.startswith("CONTEXT 575K: past the 570K handoff point; auto-compaction follows near 600K."), o)
o = task("context-points", env=dict(ENV, CLAUDE_CONFIG_DIR=str(T / "cfg"))).stdout
check("context-points: hook-io's handoff default and compaction point", o == "570000 600000\n", o)
o = task("context-points", env=dict(ENV, CLAUDE_CONFIG_DIR=str(T / "nocfg"))).stdout
check("context-points: no autoCompactWindow: the 600K default, compaction unknown (0)", o == "600000 0\n", o)
o = task("nudge", "--session", S1, "--ctx", "612345", "--at", "600000", env=dict(ENV, CLAUDE_CONFIG_DIR=str(T / "nocfg"))).stdout
check("nudge: no autoCompactWindow names no compaction point", o.startswith("CONTEXT 612K: past the 600K handoff point. ") and "near" not in o, o)
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
check("compact, unbound: keeps the startup line's branch hint and keep-it-there rule", "the branch names DEMO-123, which maps to no single item; /bind <project>/DEMO-123 creates it." in o and "Keep what is worth keeping there, throwaway logs in the scratchpad." in o and "Compacted. Read these before continuing:" in o, o)
o = task("nudge", "--session", S2, "--ctx", "600000", "--at", "600000", "--cwd", str(T)).stdout
check("nudge: unbound names agent-task bind and the OUT DIR", "No project is bound" in o and f"bind <project>[/<item>] --session {S2}" in o and f"in {ROOT}/" in o, o)

print("--- injection cap ---")
big = project("big", "scope: many scripts\nterms: bulk")
for i in range(400):
    (big / "scripts" / f"s{i:03}.py").write_text(f'"""Script {i}: ' + "word " * 30 + '"""\n')
(big / "data").mkdir()
(big / "data" / "rows.csv").write_text("a,b\n")
task("index", "big")
text = (big / "INDEX.md").read_text()
for i in range(0, 30, 2):
    entry = next(line for line in text.split("\n") if line.startswith(f"- `scripts/s{i:03}.py`"))
    text = text.replace(entry, entry + f"\n  - use: replay case {i} " + "x" * 40 + f"\n  - proved: case {i} holds")
(big / "INDEX.md").write_text(text)
task("index", "big")
(big / "knowledge" / "findings.md").write_text("# Findings\n\n- Fees round per line, not per order (source: fees.py:10; verified 2026-10-01; status: project-only)\n")
(big / "knowledge" / "prd-bulk.md").write_text("# Bulk PRD\n")
S7 = "77777777-aaaa-4000-8000-000000000000"
out = task("bind", "big/BIG-1", "--session", S7)
(big / "items/BIG-1/HANDOFF.md").write_text("# Handoff BIG-1\n" + "".join(f"- state line {i} " + "y" * 200 + "\n" for i in range(20)))
sl = task("slice", "--session", S7).stdout.rstrip("\n")
check("the slice stays under 2KB", 0 < len(sl) <= 2000, str(len(sl)))
check("...lists knowledge entry titles, not counts", "findings: Fees round per line, not per order" in sl and "prd-bulk.md: Bulk PRD" in sl, sl)
shown = re.findall(r"^- scripts/s(\d+)\.py", sl, re.M)
check("...at most 10 scripts, only ones with a use:/proved: line", 0 < len(shown) <= 10 and all(int(n) % 2 == 0 for n in shown) and "  - use: replay case" in sl, sl)
check("...no data/ and no unproven script", "rows.csv" not in sl and "s001.py" not in sl, sl)
check("...says where a reusable script goes, relative to the project root it names once", f"Project root: {big} (the paths below are relative to it)" in sl and "goes to scripts/ with a `  - use:` line" in sl and sl.count(str(big)) == 2, sl)
LONGROOT = T / "w"
LONGROOT = LONGROOT.with_name("w" + "x" * max(0, 120 - len(str(LONGROOT))))
shutil.copytree(big, LONGROOT / "projects/big")
lsl = task("slice", "--session", "78787878-aaaa", env=dict(ENV, CLAUDE_OUT_ROOT=str(LONGROOT))) if task("bind", "big/BIG-1", "--session", "78787878-aaaa", env=dict(ENV, CLAUDE_OUT_ROOT=str(LONGROOT))).returncode == 0 else None
lsl_text = lsl.stdout if lsl else ""
check(f"...a {len(str(LONGROOT))}-character work root still leaves room for the proven scripts", len(str(LONGROOT)) >= 120 and "Proven scripts:" in lsl_text and len(lsl_text.rstrip()) <= 2000, lsl_text)
check("...ends with the item's HANDOFF opening", "HANDOFF: " in sl and "Handoff BIG-1" in sl, sl[-400:])
bf = Path(task("brief", "big/BIG-1", "--name", "cap").stdout.strip())
check("brief embeds the slice, not the INDEX", bf.is_file() and "Proven scripts:" in bf.read_text() and "s001.py" not in bf.read_text(), bf.read_text()[-600:] if bf.is_file() else "")

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
for rel in ("items/DEMO-1500/scripts/probe.py", "items/DEMO-1500/tmp/fixture.py", "tmp/scratch.py"):
    (rp / rel).parent.mkdir(parents=True, exist_ok=True)
    (rp / rel).write_text('"""Probe one order."""\n')
task("index", "invoices")
idx = (rp / "INDEX.md").read_text()
check("INDEX lists item scripts for on-demand reading, never tmp/", "- `items/DEMO-1500/scripts/probe.py` (python): Probe one order." in idx and "fixture.py" not in idx and "scratch.py" not in idx, idx)

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
check("ls shows each item's name, status and last-touched date", re.search(r"^  invoices/DEMO-1500  harvested  \d{4}-\d\d-\d\d$", ls, re.M) is not None and re.search(r"^  web-tracking/DEMO-310  open  \d{4}-", ls, re.M) is not None, ls)

print("--- subagent-context: SubagentStart (payload as Claude Code 2.1.291 sends it) ---")
sa = hook("subagent-context", {"session_id": S1, "transcript_path": "/x.jsonl", "cwd": str(T), "prompt_id": "p", "agent_id": "a961e272483c31737", "agent_type": "researcher", "hook_event_name": "SubagentStart"})
ctx = ctx_of(sa)
check("a bound parent's subagent gets the work folder, the HANDOFF path and the slice, within 2KB", sa.returncode == 0 and json.loads(sa.stdout)["hookSpecificOutput"]["hookEventName"] == "SubagentStart" and ctx.startswith(f"WORK FOLDER (your parent session's binding): {rp}/items/DEMO-1500.") and f"Item HANDOFF: {rp}/items/DEMO-1500/HANDOFF.md" in ctx and "PROJECT: invoices · item DEMO-1500" in ctx and len(ctx) <= 2000, ctx)
sa = hook("subagent-context", {"session_id": "deadbeef-0000", "agent_type": "researcher", "hook_event_name": "SubagentStart"})
check("...an unbound parent's subagent gets nothing", sa.returncode == 0 and sa.stdout.strip() == "", sa.stdout)

print("--- bind <project>: the sole open item, else the list ---")
solo = project("solo", "scope: one item")
task("bind", "solo/only-one", "--session", HELPER)
S40 = "40404040-aaaa-4000-8000-000000000000"
out = task("bind", "solo", "--session", S40)
check("bind <project> with one open item binds that item", binding(S40) == "project:solo/only-one" and "bound: solo/only-one (rule: explicit" in out.stdout, out.stdout)
out = task("bind", "web-tracking", "--session", S40)
check("bind <project> with several open items binds the project and lists them", binding(S40) == "project:web-tracking" and "Bound to the project only" in out.stdout and "DEMO-310" in out.stdout and "DEMO-900" in out.stdout, out.stdout)

print("--- binding keys: legacy forms migrate to project:p/i ---")
BD = T / "state" / "task-bindings"
S41, S42 = "41414141-aaaa-4000-8000-000000000000", "42424242-aaaa-4000-8000-000000000000"
(BD / S41[:8]).write_text("DEMO-1500-invoices\n")
w = task("where", "--path", "--session", S41)
check("a legacy tasks/ key resolves through its link to the item, and the file migrates on read", w.stdout.strip() == str(rp / "items/DEMO-1500") and binding(S41) == "project:invoices/DEMO-1500", w.stdout + binding(S41))
S43 = "43434343-aaaa-4000-8000-000000000000"
(ROOT / "tasks/samename").mkdir()
(ROOT / "tasks/samename/HANDOFF.md").write_text("# legacy samename\n")
project("samename", "scope: a project made after the legacy folder")
(BD / S43[:8]).write_text("samename\n")
w = task("where", "--path", "--session", S43)
check("a bare legacy key keeps its tasks/ folder when projects/<same name> appears", w.stdout.strip() == str(ROOT / "tasks/samename") and binding(S43) == "samename", w.stdout + binding(S43))
(BD / S42[:8]).write_text("invoices/DEMO-1500\n")
(BD / "tab-tty-ttys099").write_text("DEMO-1500-invoices\t" + S42 + "\n")
dry = task("migrate", "--bindings").stdout
check("migrate --bindings is a dry run listing old keys", "42424242: invoices/DEMO-1500 -> project:invoices/DEMO-1500" in dry and "tab-tty-ttys099: DEMO-1500-invoices -> project:invoices/DEMO-1500" in dry and binding(S42) == "invoices/DEMO-1500", dry)
task("migrate", "--bindings", "--apply")
check("...--apply rewrites them, the tab file keeping its session", binding(S42) == "project:invoices/DEMO-1500" and (BD / "tab-tty-ttys099").read_text() == "project:invoices/DEMO-1500\t" + S42 + "\n", (BD / "tab-tty-ttys099").read_text())

print("--- close: harvest prompts, proposed knowledge lines, a closed ledger entry ---")
it501 = rp / "items/DEMO-501"
(it501 / "HANDOFF.md").write_text("# Handoff DEMO-501\n\n## State\n- PR open\n\n## Decisions\n- Fees round per line because finance reconciles per line\n- Keep the cap at 3\n")
cl = task("close", "invoices/DEMO-501", "--pr", "https://example.invalid/pr/1")
tj = json.loads((it501 / "task.json").read_text())
check("close proposes the HANDOFF's durable lines for knowledge/", "Proposed for" in cl.stdout and "- Fees round per line because" in cl.stdout and "- Keep the cap at 3" in cl.stdout and "- PR open" not in cl.stdout, cl.stdout)
check("...and leaves a closed ledger entry with its PR (never blocked)", cl.returncode == 0 and tj["status"] == "closed" and tj.get("pr") == "https://example.invalid/pr/1" and tj.get("closed") and tj.get("harvested"), str(tj))
idx = json.loads((ROOT / ".index.json").read_text())
check("...which the index reflects", any(x["item"] == "DEMO-501" and x["status"] == "closed" for x in idx["items"]))

print("--- no second cleaner: the tmp sweep (claude-gc) is the only one ---")
old = rp / "items/OLD-1"
task("bind", "invoices/OLD-1", "--session", HELPER)
(old / "tmp" / "home").mkdir(parents=True)
(old / "tmp" / "home" / "unfinished.py").write_text("x = 1\n")
(old / "out" / "clone" / ".git").mkdir(parents=True)
past = time.time() - 30 * 86400
for f in [old, *old.rglob("*")]:
    os.utime(f, (past, past))
pa = task("prune", "--apply")
check("agent-task prune is gone, and an idle open item keeps its tmp/ and out/ clone", pa.returncode != 0 and (old / "tmp/home/unfinished.py").is_file() and (old / "out/clone/.git").is_dir(), pa.stdout + pa.stderr)
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
alone = run([sys.executable, "-c", "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import agent_task as at; "
             "p = at.parse_project(Path(sys.argv[2])); print(*(f.name for f in sum(at.item_scripts(p, Path(sys.argv[3])), [])))",
             str(HOOKS / "lib"), str(ROOT / "projects/invoices"), str(hv_item)])
check("hooks/lib harvests on its own (no bin/lib on the path)", alone.returncode == 0 and "keep.py" in alone.stdout, alone.stdout + alone.stderr)
(ROOT / "projects/invoices/items/DEMO-1502").mkdir()
(ROOT / "projects/invoices/items/DEMO-1502/task.json").write_text("[]\n")
ls = task("ls")
check("a task.json that is not an object reads as open", ls.returncode == 0 and re.search(r"^  invoices/DEMO-1502  open  ", ls.stdout, re.M) is not None, ls.stdout + ls.stderr)

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
