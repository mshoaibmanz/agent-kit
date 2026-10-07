"""The binding rules: where a session's work lives (Place), the deterministic rules that pick a
project or item for it (decide), the fallback offer and bind() itself. Rules: bin/agent-task's
docstring."""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agent_task import (
    DONE_STATUSES,
    TICKET_RE,
    Binding,
    agent_task_bin,
    branch_ticket,
    create_project,
    ensure_item,
    find_legacy,
    index,
    legacy_alias,
    legacy_project,
    load_index,
    parse_project,
    project_dir,
    projects,
    read_task,
    slug,
    tickets_in,
    touched,
    where_label,
    work_root,
    write_binding,
)
from retro import record_retro


# The line between asking for a ticket and mentioning one: the key is the object of a start-work verb
# ("start on DEMO-300", "pick up ticket DEMO-300", "switch to DEMO-300"), of an imperative fix/implement/do
# that opens a clause ("fix DEMO-300", "please implement DEMO-300"), or the prompt is nothing but keys.
# Anything else mentions it: "like DEMO-100 did", "the fix DEMO-100 shipped", "how did we handle DEMO-100".
_TICKET_WORD = r"(?:\s+(?:the\s+)?(?:jira\s+)?ticket)?\s+$"
REQUEST_VERB = re.compile(
    r"\b(?:start(?:ing)?(?:\s+(?:on|with|work\s+on))?|begin(?:ning)?(?:\s+(?:on|with))?|work(?:ing)?\s+on|"
    r"switch(?:ing)?(?:\s+over)?\s+to|mov(?:e|ing)\s+(?:on\s+)?(?:on)?to|pick(?:ing)?\s+up|tak(?:e|ing)\s+(?:on|over)|"
    r"tackl(?:e|ing)|kick(?:ing)?\s+off|resum(?:e|ing)|continu(?:e|ing)(?:\s+(?:on|with))?|carry(?:ing)?\s+on(?:\s+with)?)"
    + _TICKET_WORD
    + r"|(?:^|[.!?;:,\n]\s*|\b(?:please|let'?s|now|then|and|you|go)\s+)(?:fix|implement|do)" + _TICKET_WORD,
    re.I,
)


def requested_ticket(text: str, keys: list[str] | None = None) -> str:
    """The ticket <text> asks to work on ('' when it only mentions keys); see REQUEST_VERB."""
    keys = tickets_in(text, known=True) if keys is None else keys
    if keys and not TICKET_RE.sub("", text).strip(" \t\n.,;:!?&/+"):
        return keys[0]
    for k in keys:
        for m in re.finditer(rf"\b{re.escape(k)}\b", text):
            if REQUEST_VERB.search(text, max(0, m.start() - 80), m.start()):
                return k
    return ""


@dataclass
class Place:
    """Where a session is: its checkout's top level, branch and repo, and whether the checkout is a
    linked worktree (git-dir differs from the common dir)."""

    top: str = ""
    branch: str = ""
    repo: str = ""
    linked: bool = False


def place(cwd: str) -> Place:
    def git(*a: str) -> list[str]:
        try:
            r = subprocess.run(["git", "-C", cwd or ".", *a], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return []
        return r.stdout.strip().split("\n") if r.returncode == 0 else []

    out = git("rev-parse", "--show-toplevel", "--path-format=absolute", "--git-dir", "--git-common-dir")
    if len(out) < 3:
        return Place()
    branch = (git("rev-parse", "--abbrev-ref", "HEAD") or [""])[0]
    top, gitdir, common = out[0], out[1], out[2]
    return Place(top, "" if branch == "HEAD" else branch, os.path.basename(os.path.dirname(common)), os.path.realpath(gitdir) != os.path.realpath(common))


@dataclass
class Decision:
    """The binding rules' answer. rule is '' when nothing is certain; then candidates holds the
    /bind arguments to offer (ambiguous hits first, then this repo's recent open items)."""

    rule: str = ""  # worktree | branch | worktree-new | path | ticket | ticket-project
    target: str = ""  # bind()'s argument: <project>/<item>, <project>, or a bare ticket (new project of one)
    candidates: list[str] = field(default_factory=list)
    why: str = ""

    def __bool__(self) -> bool:
        return bool(self.rule)


CHEAP_RULES = ("worktree", "branch", "worktree-new")


def _live(recs: list[dict]) -> list[dict]:
    return [r for r in recs if r.get("status") not in DONE_STATUSES] or recs


def _targets(recs: list[dict]) -> list[str]:
    return sorted({f"{r['project']}/{r['item']}" for r in _live(recs)})


def _same_path(a: str, b: str) -> bool:
    return bool(a and b) and os.path.realpath(a) == os.path.realpath(b)


def work_paths_in(text: str, root: Path | None = None) -> tuple[list[str], list[str]]:
    """(items, projects) that work-root paths in <text> name: a pasted HANDOFF.md, brief or item
    folder. A legacy tasks/<name> path counts through its link into projects/."""
    root = root or work_root()
    home = str(Path.home())
    bases = {re.escape(str(root)), re.escape(str(root.resolve())), r"~/agent-work", r"~/claude-scratch", re.escape(home + "/agent-work"), re.escape(home + "/claude-scratch")}
    seg = r"[^/\s\"'`)\]]+"
    # Longest first: a work root nested under ~/agent-work (a test root) must win at the same position.
    rx = re.compile(rf"(?:{'|'.join(sorted(bases, key=len, reverse=True))})/(?:projects/({seg})(?:/items/({seg}))?|tasks/({seg}))")
    items: list[str] = []
    projs: list[str] = []
    for m in rx.finditer(text or ""):
        if m.group(3):
            alias = legacy_alias(root / "tasks" / m.group(3), root)
            if not alias:
                continue
            p, i = alias
        else:
            p, i = m.group(1), m.group(2) or ""
        pd = project_dir(p, root)
        if not pd:
            continue
        if i and (pd / "items" / i).is_dir():
            items.append(f"{pd.name}/{i}")
        projs.append(pd.name)
    return sorted(set(items)), sorted(set(projs))


def decide(prompt: str, pl: Place, *, cheap_only: bool = False, root: Path | None = None) -> Decision:
    """The deterministic binding rules, certain ones first. Each binds only on exactly one hit:
    1 worktree: the checkout is a worktree the index maps to an item;
    2 branch: the branch names a ticket that maps to exactly one item (a release branch names none);
    3 worktree-new: a linked worktree named <TICKET>-* (or on a ticket branch) with no item yet: the
      item is created, under the one project that lists the ticket, else as a project of one;
    4 path: the prompt pastes a work-root path (HANDOFF.md, brief, item folder) of exactly one item;
    5 ticket: the prompt asks for (or names only) one ticket that maps to exactly one existing item;
    6 ticket-project: that ticket has no item but exactly one project lists it: the project only.
    Nothing is created from a prompt. cheap_only stops after 3 (re-checked on every prompt)."""
    root = root or work_root()
    recs = load_index(root)
    if pl.linked and pl.top:
        hits = _targets([r for r in recs if any(_same_path(w, pl.top) for w in r.get("worktrees", []))])
        if len(hits) == 1:
            return Decision("worktree", hits[0], why=pl.top)
    bt = branch_ticket(pl.branch)
    if bt:
        hits = _targets([r for r in recs if pl.branch in r.get("branches", [])]) or _targets([r for r in recs if r.get("ticket") == bt])
        if len(hits) == 1:
            return Decision("branch", hits[0], why=pl.branch)
    wt_ticket = (branch_ticket(os.path.basename(pl.top)) or bt) if pl.linked else ""
    mapped = _targets([r for r in recs if r.get("ticket") == wt_ticket]) if wt_ticket else []
    if len(mapped) == 1:
        # A worktree named <TICKET>-* on a branch that names no ticket (widget-cache-v2).
        return Decision("worktree", mapped[0], why=pl.top)
    wt_ambiguous: list[str] = []
    if wt_ticket and not mapped:
        target, wt_ambiguous = ticket_target(wt_ticket, root)
        if target:
            return Decision("worktree-new", target, why=pl.top)
    if cheap_only:
        return Decision(candidates=wt_ambiguous)
    items, projs = work_paths_in(prompt, root)
    if len(items) == 1:
        return Decision("path", items[0])
    if not items and len(projs) == 1:
        return Decision("path", projs[0])
    keys = tickets_in(prompt, known=True)
    asked = requested_ticket(prompt, keys)
    keys = [asked] if asked else keys
    found = {k: _targets([r for r in recs if r.get("ticket") == k]) for k in keys}
    one = {t[0] for t in found.values() if len(t) == 1}
    if len(one) == 1 and all(len(t) <= 1 for t in found.values()):
        return Decision("ticket", one.pop(), why=next(k for k, t in found.items() if t))
    ambiguous = sorted({x for t in found.values() for x in t} | set(items))
    if not ambiguous and len(keys) == 1:
        owners = ticket_owners(keys[0], root)
        if len(owners) == 1:
            return Decision("ticket-project", owners[0], why=keys[0])
        ambiguous = owners
    ambiguous = wt_ambiguous + [c for c in ambiguous if c not in wt_ambiguous]
    return Decision(candidates=ambiguous + [c for c in recent_items(pl.repo, root=root) if c not in ambiguous])


def ticket_owners(ticket: str, root: Path | None = None) -> list[str]:
    """The projects whose PROJECT.md or item folders list <ticket>."""
    return sorted(p.name for p in projects(root) if not p.legacy and ticket in p.tickets)


class AmbiguousTicket(ValueError):
    def __init__(self, ticket: str, candidates: list[str]) -> None:
        super().__init__(f"{ticket} belongs to more than one place: {', '.join(candidates)}. Bind one: /bind <project>/{ticket}")
        self.candidates = candidates


def ticket_target(ticket: str, root: Path | None = None) -> tuple[str, list[str]]:
    """Where a ticket binds, for bind() and decide() alike: (target, []) with target the one item the
    index maps it to, else <the one project that lists it>/<ticket>, else the bare ticket (bind()
    makes a project of one); ('', candidates) when two items or two projects claim it."""
    mapped = _targets([r for r in load_index(root) if r.get("ticket") == ticket])
    if len(mapped) > 1:
        return "", mapped
    if mapped:
        return mapped[0], []
    owners = ticket_owners(ticket, root)
    if len(owners) > 1:
        return "", owners
    return (f"{owners[0]}/{ticket}" if owners else ticket), []


def may_move(d: Decision, cur: Binding | None, root: Path | None = None) -> bool:
    """Whether a re-bind the session did not ask for (a checkout change, EnterWorktree, a prompt after
    /clear) may act on <d>: always for an unbound session; a bound one moves only to a different,
    existing project or item, so nothing is ever created for it."""
    if not d:
        return False
    if cur is None:
        return True
    if d.target == where_label(cur) or d.rule == "worktree-new":
        return False
    name, _, item = d.target.partition("/")
    pd = project_dir(name, root)
    return bool(pd and (not item or (pd / "items" / item).is_dir()))


def recent_items(repo: str, n: int = 3, root: Path | None = None) -> list[str]:
    """This repo's open items, most recently touched first: the item's task.json repo, else its
    project's `repos:` line, names it."""
    root = root or work_root()
    if not repo:
        return []
    listed = {p.name for p in projects(root) if not p.legacy and repo in p.listing("repos")}
    recs = [r for r in load_index(root) if r.get("status") not in DONE_STATUSES and (r.get("repo") == repo or (not r.get("repo") and r["project"] in listed))]
    scored = []
    for r in recs:
        pd = project_dir(r["project"], root)
        scored.append((touched(pd / "items" / r["item"]) if pd else 0.0, f"{r['project']}/{r['item']}"))
    return [t for _, t in sorted(scored, reverse=True)[:n]]


def offer_text(d: Decision, sid: str, lead: str = "unbound: no rule was certain.", root: Path | None = None) -> str:
    """The fallback: <lead>, then one line per candidate; the session stays unbound."""
    if not d.candidates:
        return ""
    root = root or work_root()
    lines = [lead + " Bind with /bind <arg> (or `" + agent_task_bin() + f" bind <arg> --session {sid}`) if one of these is this session's work:"]
    for c in d.candidates[:3]:
        p, _, i = c.partition("/")
        pd = project_dir(p, root)
        t = read_task(pd / "items" / i) if pd and i else {}
        when = dt.date.fromtimestamp(touched(pd / "items" / i)).isoformat() if pd and i else ""
        br = (t.get("branches") or [t.get("branch", "")])[-1] if t else ""
        lines.append(f"  /bind {c}" + (f"  (touched {when}" + (f", branch {br}" if br else "") + ")" if when else ""))
    return "\n".join(lines)


def announce(b: Binding, rule: str, was: Binding | None = None) -> str:
    """The one line every binding gets: `bound: <project>/<item> (rule: <rule>)`."""
    prev = f"; was {where_label(was)}" if was and was.key != b.key else ""
    return f"bound: {where_label(b)} (rule: {rule}{prev}). Wrong? /bind <project>[/<item>]."


def bind(sid: str, target: str, *, desc: str = "", here: Place | None = None, repo: str = "", rule: str = "explicit", pick_item: bool = False, root: Path | None = None) -> Binding:
    """Bind <sid> to <project>[/<item>], a legacy task folder (its item), or a ticket: the one item
    the index maps it to, else <the project that lists it>/<ticket>, else a new project of one.
    An existing projects/<name> is used under its own spelling; a project named after a ticket
    means its item (DEMO-77 is DEMO-77/DEMO-77). pick_item (an explicit `bind <project>`) picks the
    project's sole open item. <here> records the session's worktree and branch on the item."""
    root = root or work_root()
    here = here or Place()
    repo = repo or here.repo
    name, _, item = target.partition("/")
    pdir = project_dir(name, root)
    if not item and not pdir:
        leg = find_legacy(name, root)
        alias = legacy_alias(leg, root) if leg else None
        if alias and project_dir(alias[0], root):
            name, item = alias
            pdir = project_dir(name, root)
        elif leg:
            b = Binding(legacy_project(leg))
            write_binding(sid, b, rule=rule, wt=here.top)
            return b
    t = name.upper()
    if not item and TICKET_RE.fullmatch(t):
        if pdir:
            item = t
        else:
            found, ambiguous = ticket_target(t, root)
            if not found:
                raise AmbiguousTicket(t, ambiguous)
            if "/" in found:
                name, item = found.split("/", 1)
            else:
                create_project(t, scope=desc.replace("-", " "), terms=desc.replace("-", " "), repos=repo, status="project-of-one", root=root)
                record_retro(f"created project of one {t}", source="auto", tag="project", session=sid, where=f"{t}/{t}", root=root)
                name, item = t, t
            pdir = project_dir(name, root)
    if not pdir:
        name = slug(name) or "misc"
        pdir = project_dir(name, root)
    p = parse_project(pdir) if pdir else create_project(name, scope=desc, terms=desc, repos=repo, root=root)
    if not item and pick_item:
        live = p.open_items()
        item = live[0] if len(live) == 1 else ""
    if item:
        ensure_item(p, item, branch=here.branch, repo=repo, worktree=here.top if here.linked else "")
    b = Binding(parse_project(p.path), item)
    write_binding(sid, b, rule=rule, wt=here.top)
    adopt_session_dir(sid, b, root)
    index(p.name, root)
    return b


def adopt_session_dir(sid: str, b: Binding, root: Path) -> list[str]:
    """Move the session's per-session folders into the bound folder's out/sessions/, linking back."""
    moved = []
    for d in root.glob(f"*/*-{sid[:8]}"):
        if d.is_symlink() or not d.is_dir() or d.parent.name in ("projects", "tasks"):
            continue
        if not any(d.iterdir()):
            d.rmdir()
            continue
        dest = b.folder / ("out/sessions" if not b.project.legacy else "sessions") / d.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), dest)
        d.symlink_to(dest)
        moved.append(f"{d} -> {dest}")
    return moved
