"""Projects and work items under the work root: the one implementation behind bin/agent-task, the
project-bind hook and session-context. Layout and rules: bin/agent-task's docstring."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from kit_env import kit_dir, kit_env, kit_env_path
from kit_env import work_root as _work_root


def agent_task_bin() -> str:
    """The installed agent-task, as model-facing text names it."""
    return str(kit_dir() / "bin/agent-task")


# A release branch's spelling (DEMO-119-REL, DEMO-119-REL-TEST, DEMO-119-TEST) names no ticket, the
# same rule as branch_ticket: a smoke prompt naming it once made a project of one.
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,5}-[0-9]+)\b(?!-(?i:rel)\b|-TEST\b)")
TICKET_PREFIX_RE = re.compile(r"[A-Z][A-Z0-9]{1,5}")
# Look like ticket keys, never are one.
NOT_TICKETS = {
    "UTF", "SHA", "ISO", "GPT", "RFC", "MD", "MD5", "HTTP", "TLS", "SSL", "PEP", "ES", "AES", "RSA",
    "ECMA", "IPV", "X", "X86", "ARM64", "COVID", "CVE", "CWE", "GHSA",
}
SLICE_CAP = 2000  # characters: the bound slice (session start, subagents, briefs)
CONTEXT_CAP = 9000  # characters: a compaction's re-injection (slice plus handoffs)
KNOWLEDGE_FILES = ("findings.md", "decisions.md", "code-map.md", "queries.md")
DONE_STATUSES = {"done", "harvested", "closed", "merged"}
INDEX_FILE = ".index.json"  # under the work root: derived from items/*/task.json, never edited
SLICE_SCRIPTS = 10
STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "per", "via", "fix", "add", "new",
    "use", "not", "are", "was", "all", "one", "out", "its", "has", "have", "task", "tasks", "readme",
    "index", "handoff", "notes", "brief", "data", "scripts", "docs", "work", "folder", "shared",
    "session", "every", "read", "first", "reuse", "before", "line", "what", "how", "run", "keep",
}


def work_root() -> Path:
    """kit_env.work_root(), the resolver comment-guard shares."""
    return Path(_work_root())


def state_dir() -> Path:
    return Path(os.environ.get("CLAUDE_STATE_DIR") or Path.home() / ".claude" / "state")


def bind_dir() -> Path:
    return state_dir() / "task-bindings"


def tab_label_dir() -> Path:
    return Path(os.environ.get("TAB_LABEL_DIR") or Path.home() / ".claude" / "state" / "tab-label")


def today() -> str:
    return dt.date.today().isoformat()


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n]
    return s.rstrip("-")


@functools.cache
def known_prefixes() -> frozenset[str]:
    """The Jira project keys a ticket in a prompt or branch may start with: `project_key:` and the
    `## <KEY> notes` headings of jira-prefs.md (the /jira command's prefs) beside kit.env. Empty
    (no prefs file) allows any prefix outside NOT_TICKETS."""
    prefs = Path(kit_env_path()).parent / "jira-prefs.md"
    try:
        text = prefs.read_text(errors="replace") if prefs.is_file() else ""
    except OSError:
        text = ""
    keys = set(re.findall(r"^project_key:\s*([A-Z][A-Z0-9]+)\s*$", text, re.M))
    keys |= set(re.findall(r"^## ([A-Z][A-Z0-9]+) notes\b", text, re.M))
    return frozenset(keys)


@functools.cache
def configured_prefixes() -> frozenset[str]:
    """TICKET_PREFIXES from the overlay or preset: when set, the only prefixes that name a ticket."""
    try:
        raw = kit_env().get("TICKET_PREFIXES", "")
    except OSError:
        raw = ""
    return frozenset(x.strip().upper() for x in raw.split(",") if x.strip())


def is_ticket(key: str, *, known: bool = False) -> bool:
    """TICKET_RE's prefix rule (2-6 characters), then TICKET_PREFIXES when set, else (known=True)
    the Jira prefs' keys when there are any."""
    prefix = key.split("-")[0]
    if prefix in NOT_TICKETS or not TICKET_PREFIX_RE.fullmatch(prefix):
        return False
    if configured_prefixes():
        return prefix in configured_prefixes()
    return not known or not known_prefixes() or prefix in known_prefixes()


def tickets_in(text: str, *, known: bool = False) -> list[str]:
    """Ticket keys in <text>. known=True (prompts) also requires a known Jira project prefix."""
    out: list[str] = []
    for t in TICKET_RE.findall(text or ""):
        if is_ticket(t, known=known) and t not in out:
            out.append(t)
    return out


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


def branch_ticket(branch: str) -> str:
    """The ticket a work branch names; '' for a release branch (CORE-118-REL), an unknown prefix
    or none."""
    m = re.match(r"^([A-Za-z][A-Za-z0-9]*-[0-9]+)(-(.+))?$", (branch or "").removeprefix("worktree-"))
    if not m or re.match(r"^rel(-|$)", (m.group(3) or "").lower()):
        return ""
    t = m.group(1).upper()
    return t if is_ticket(t, known=True) else ""


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}", (text or "").lower()) if w not in STOP_WORDS}


@dataclass
class Project:
    name: str
    path: Path
    legacy: bool = False
    meta: dict[str, str] = field(default_factory=dict)
    tickets: set[str] = field(default_factory=set)

    def items(self) -> list[Path]:
        d = self.path / "items"
        return sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def listing(self, key: str) -> list[str]:
        return [x.strip() for x in re.split(r"[,\n]", self.meta.get(key, "")) if x.strip()]


def parse_project(path: Path) -> Project:
    pm = path / "PROJECT.md"
    text = pm.read_text(errors="replace") if pm.is_file() else ""
    meta: dict[str, str] = {}
    for line in text.split("\n"):
        if line.startswith("## "):
            break
        m = re.match(r"^([a-z][a-z-]*):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip()
    p = Project(path.name, path, meta=meta, tickets=set(tickets_in(text)))
    p.tickets |= {i.name.upper() for i in p.items() if TICKET_RE.fullmatch(i.name.upper())}
    return p


def legacy_project(path: Path) -> Project:
    m = re.match(r"^([A-Za-z][A-Za-z0-9]*-[0-9]+)(?:-(.*))?$", path.name)
    tickets = {m.group(1).upper()} if m else set()
    terms = (m.group(2) or "") if m else path.name
    return Project(path.name, path, legacy=True, meta={"terms": terms.replace("-", " ")}, tickets=tickets)


def projects(root: Path | None = None) -> list[Project]:
    root = root or work_root()
    out = []
    pd = root / "projects"
    if pd.is_dir():
        out += [parse_project(p) for p in sorted(pd.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    td = root / "tasks"
    if td.is_dir():
        # A tasks/<name> link into projects/<p>/items/<i> is an alias of that item, not a project.
        out += [legacy_project(p) for p in sorted(td.iterdir()) if p.is_dir() and not p.name.startswith(".") and not legacy_alias(p, root)]
    return out


def legacy_alias(path: Path, root: Path | None = None) -> tuple[str, str] | None:
    """(project, item) a legacy tasks/<name> link points at, else None (a real legacy folder)."""
    root = (root or work_root()).resolve()
    try:
        parts = path.resolve().relative_to(root).parts
    except (ValueError, OSError):
        return None
    if len(parts) >= 2 and parts[0] == "projects":
        return (parts[1], parts[3]) if len(parts) >= 4 and parts[2] == "items" else (parts[1], "")
    return None


@dataclass
class Binding:
    project: Project
    item: str = ""

    @property
    def key(self) -> str:
        if self.project.legacy:
            return self.project.name
        return f"project:{self.project.name}" + (f"/{self.item}" if self.item else "")

    @property
    def folder(self) -> Path:
        if self.project.legacy or not self.item:
            return self.project.path
        return self.project.path / "items" / self.item

    @property
    def label(self) -> str:
        if self.project.legacy:
            return self.project.name
        return f"{self.project.name} · {self.item}" if self.item else self.project.name


def find_legacy(key: str, root: Path | None = None) -> Path | None:
    td = (root or work_root()) / "tasks"
    if (td / key).is_dir():
        return td / key
    for d in sorted(td.glob(f"{key}-*")) if td.is_dir() else []:
        if d.is_dir():
            return d
    return None


def project_dir(name: str, root: Path | None = None) -> Path | None:
    """projects/<name> under its spelling on disk; a case-insensitive match counts (demo-77 is DEMO-77),
    which a case-insensitive file system would otherwise hide behind the typed spelling."""
    pd = (root or work_root()) / "projects"
    if not name or not pd.is_dir():
        return None
    dirs = sorted(p for p in pd.iterdir() if p.is_dir() and not p.name.startswith("."))
    return next((p for p in dirs if p.name == name), None) or next((p for p in dirs if p.name.lower() == name.lower()), None)


def from_key(key: str, root: Path | None = None) -> Binding | None:
    """A binding key: `project:<p>[/<i>]`. Any other form is a legacy key (a tasks/ folder name, or
    `<p>/<i>`), read only so normalize_key can migrate it."""
    root = root or work_root()
    key = key.strip()
    if not key:
        return None
    if key.startswith("project:"):
        name, _, item = key[len("project:") :].partition("/")
        p = project_dir(name, root)
        return Binding(parse_project(p), item) if p else None
    # A bare key was written when it named tasks/<key>: that folder (or the item it links to) wins over
    # a projects/<key> made since, so a session never silently changes work folder.
    exact = root / "tasks" / key
    if "/" not in key and exact.is_dir():
        return _legacy_binding(exact, root)
    name, _, item = key.partition("/")
    p = project_dir(name, root)
    if p:
        return Binding(parse_project(p), item)
    leg = find_legacy(key, root)
    return _legacy_binding(leg, root) if leg else None


def _legacy_binding(leg: Path, root: Path) -> Binding:
    """A tasks/<name> folder's binding: the project item it links to, else the legacy folder itself."""
    alias = legacy_alias(leg, root)
    pd = project_dir(alias[0], root) if alias else None
    if alias and pd:
        return Binding(parse_project(pd), alias[1])
    return Binding(legacy_project(leg))


def normalize_key(key: str, root: Path | None = None) -> str:
    """The `project:p/i` form of a binding key (a real legacy folder keeps its name); '' if gone."""
    b = from_key(key, root)
    return b.key if b else ""


def tab_key() -> str:
    """The terminal tab: iTerm's session id, tmux's pane, else the tty of the nearest ancestor."""
    for var in ("ITERM_SESSION_ID", "TMUX_PANE", "WEZTERM_PANE", "KITTY_WINDOW_ID"):
        if os.environ.get(var):
            return slug(f"{var}-{os.environ[var]}", 80)
    pid = os.getpid()
    for _ in range(10):
        try:
            out = subprocess.run(["ps", "-o", "tty=,ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return ""
        if len(out) < 2:
            return ""
        if out[0].startswith("tty"):
            return slug("tty-" + out[0], 80)
        pid = int(out[1]) if out[1].isdigit() else 0
        if pid <= 1:
            return ""
    return ""


INHERITED = "inherited"  # the binding file's second line when /clear copied the tab's binding


def binding_file(sid: str) -> dict[str, str]:
    """The session's binding file: line 1 the key, then `inherited` and `<k>:<v>` lines (rule: the
    rule that bound it; wt: the worktree the session was in at its last prompt)."""
    f = bind_dir() / sid[:8]
    try:
        lines = f.read_text().split("\n") if sid else []
    except OSError:
        return {}
    if not lines or not lines[0].strip():
        return {}
    out = {"key": lines[0].strip()}
    for line in lines[1:]:
        if line == INHERITED:
            out[INHERITED] = "1"
        elif ":" in line:
            k, _, v = line.partition(":")
            out[k] = v
    return out


def _write_binding_file(sid: str, data: dict[str, str]) -> None:
    extra = [INHERITED] if data.get(INHERITED) else []
    extra += [f"{k}:{v}" for k, v in data.items() if k not in ("key", INHERITED) and v]
    _atomic_write(bind_dir() / sid[:8], "\n".join([data["key"], *extra]) + "\n")


def bound(sid: str, root: Path | None = None) -> Binding | None:
    data = binding_file(sid)
    if not data:
        return None
    b = from_key(data["key"], root)
    if b and b.key != data["key"]:
        # A legacy key (tasks/<name>, <p>/<i>): migrate the file to `project:p/i` on read.
        _write_binding_file(sid, {**data, "key": b.key})
    return b


def bound_inherited(sid: str) -> bool:
    """The binding came from the tab across /clear and no prompt has named a ticket since."""
    return bool(binding_file(sid).get(INHERITED))


def binding_keys() -> list[str]:
    """The project or item key of every session binding on record, one per session ever bound
    (the tab files excluded)."""
    folder = bind_dir()
    keys = []
    for f in sorted(folder.iterdir()) if folder.is_dir() else []:
        if f.name.startswith(("tab-", ".")) or not f.is_file():
            continue
        try:
            key = f.read_text(errors="replace").split("\n", 1)[0].strip()
        except OSError:
            continue
        if key:
            keys.append(key)
    return keys


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_binding(sid: str, b: Binding, *, inherited: bool = False, rule: str = "", wt: str = "") -> None:
    _write_binding_file(sid, {"key": b.key, **({INHERITED: "1"} if inherited else {}), "rule": rule, "wt": wt})
    set_tab(sid, b)
    lab = tab_label_dir() / sid
    _atomic_write(lab, b.label)
    (tab_label_dir() / f"{sid}.model").touch()
    if b.item and not b.project.legacy:
        update_item(b.project, b.item, sessions=[sid[:8]])


def set_binding_wt(sid: str, wt: str) -> None:
    data = binding_file(sid)
    if data and data.get("wt", "") != wt:
        _write_binding_file(sid, {**data, "wt": wt})


def keep_binding(sid: str) -> None:
    """Drop the inherited mark: the binding is now as sticky as one a prompt made."""
    data = binding_file(sid)
    if data:
        data.pop(INHERITED, None)
        _write_binding_file(sid, data)


def migrate_bindings(apply: bool = False, root: Path | None = None) -> list[str]:
    """Each binding file (and tab file) whose key is not in `project:p/i` form, with its new key;
    apply=True rewrites them. A key whose folder has gone is reported, never deleted."""
    out = []
    d = bind_dir()
    for f in sorted(d.iterdir()) if d.is_dir() else []:
        if not f.is_file() or f.name.startswith(".") or f.suffix in (".fresh", ".tmp", ".offered"):
            continue
        text = f.read_text()
        key = re.split(r"[\t\n]", text, maxsplit=1)[0].strip()
        if not key or key.startswith("project:") or not text.startswith(key):
            continue
        new = normalize_key(key, root)
        out.append(f"{f.name}: {key} -> {new or '(folder gone; left as is)'}")
        if apply and new and new != key:
            _atomic_write(f, new + text[len(key) :])
    return out


def set_tab(sid: str, b: Binding | None) -> None:
    """The tab's binding is the binding of the session now in it; an unbound one blanks it, so a
    later /clear never inherits from a session that has gone."""
    tk = tab_key()
    if not tk:
        return
    f = bind_dir() / f"tab-{tk}"
    if b:
        _atomic_write(f, f"{b.key}\t{sid}\n")
    else:
        f.unlink(missing_ok=True)


def tab_binding(root: Path | None = None) -> Binding | None:
    tk = tab_key()
    f = bind_dir() / f"tab-{tk}"
    if not tk or not f.is_file():
        return None
    return from_key(f.read_text().split("\t")[0], root)


PROJECT_TEMPLATE = """# {name}

scope: {scope}
terms: {terms}
repos: {repos}
paths:
status: {status}

## Tickets and PRs
| item | ticket | PR | status |
|---|---|---|---|
{rows}
## Decisions
See knowledge/decisions.md.
"""

KNOWLEDGE_HEADER = """# {title}

Each entry: `- <fact> (source: <file:line, query or PR>; verified <YYYY-MM-DD>; status: project-only | pending-docs | in-docs -> <doc link>)`.
"""


# Editors watching or indexing a work root crawl every scratch checkout and virtualenv under it.
EDITOR_EXCLUDES = ("**/tmp/**", "**/.venv/**", "**/node_modules/**", "**/__pycache__/**", "**/worktrees/**")


def init_work_root(root: Path) -> None:
    """A new work root's editor settings: .vscode/settings.json and .cursorignore, each only when absent."""
    vscode = root / ".vscode" / "settings.json"
    if not vscode.exists() and not vscode.is_symlink():
        excluded = {pattern: True for pattern in EDITOR_EXCLUDES}
        _atomic_write(vscode, json.dumps({"files.watcherExclude": excluded, "search.exclude": excluded}, indent=2) + "\n")
    cursor = root / ".cursorignore"
    if not cursor.exists() and not cursor.is_symlink():
        # gitignore syntax: `tmp/` matches a tmp folder at any depth; `tmp/**` would anchor at the root.
        _atomic_write(cursor, "".join(pattern.removeprefix("**/").removesuffix("**") + "\n" for pattern in EDITOR_EXCLUDES))


def create_project(name: str, *, scope: str = "", terms: str = "", repos: str = "", status: str = "active", root: Path | None = None) -> Project:
    root = root or work_root()
    if not root.exists():
        root.mkdir(parents=True)
        init_work_root(root)
    p = root / "projects" / name
    for sub in ("knowledge", "scripts", "data", "items"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    if not (p / "PROJECT.md").exists():
        _atomic_write(p / "PROJECT.md", PROJECT_TEMPLATE.format(name=name, scope=scope, terms=terms, repos=repos, status=status, rows=""))
    for k in KNOWLEDGE_FILES:
        kf = p / "knowledge" / k
        if not kf.exists():
            _atomic_write(kf, KNOWLEDGE_HEADER.format(title=k.removesuffix(".md").replace("-", " ").capitalize()))
    index(name, root)
    return parse_project(p)


def ensure_item(p: Project, item: str, *, branch: str = "", repo: str = "", worktree: str = "") -> Path:
    """An item is thin: task.json (the single source of truth the index derives from), HANDOFF.md,
    briefs/, out/ (evidence for its PR) and tmp/ (fixtures, clones, test homes; never indexed)."""
    d = p.path / "items" / item
    for sub in ("briefs", "out", "tmp"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    tj = d / "task.json"
    if not tj.exists():
        ticket = item if TICKET_RE.fullmatch(item) else ""
        _atomic_write(tj, json.dumps({"item": item, "ticket": ticket, "branch": branch if branch_ticket(branch) else "", "repo": repo, "status": "open", "created": today()}, indent=2) + "\n")
    pm = p.path / "PROJECT.md"
    if pm.is_file() and TICKET_RE.fullmatch(item) and item not in pm.read_text():
        text = pm.read_text()
        row = f"| {item} | {item} | | open |\n"
        marker = "|---|---|---|---|\n"
        text = text.replace(marker, marker + row, 1) if marker in text else text + "\n" + row
        _atomic_write(pm, text)
    update_item(p, item, branches=[branch], worktrees=[worktree], repo=repo)
    return d


def read_task(d: Path) -> dict:
    try:
        data = json.loads((d / "task.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


LIST_FIELDS = ("branches", "worktrees", "sessions")


def update_item(p: Project, item: str, **changes: object) -> None:
    """Merge <changes> into the item's task.json (list fields gain new values, empty values are
    ignored), then rebuild the index. A branch counts only when it names a ticket: a release
    branch (DEMO-119-REL) would map every session on it to one item."""
    d = p.path / "items" / item
    if p.legacy or not d.is_dir():
        return
    data = read_task(d) or {"item": item, "status": "open", "created": today()}
    before = json.dumps(data, sort_keys=True)
    for k, v in changes.items():
        if k in LIST_FIELDS:
            vals = [x for x in v if x] if isinstance(v, list) else []  # type: ignore[union-attr]
            if k == "branches":
                vals = [x for x in vals if branch_ticket(x)]
            cur = [x for x in data.get(k, []) if x not in vals]
            new = (cur + vals)[-20:]
            if new != data.get(k, []) and (vals or k in data):
                data[k] = new
        elif v not in ("", None) and (k != "repo" or not data.get("repo")):
            data[k] = v
    if data.get("branch") and data["branch"] not in data.get("branches", []) and branch_ticket(data["branch"]):
        data["branches"] = [data["branch"], *data.get("branches", [])]
    if json.dumps(data, sort_keys=True) != before:
        _atomic_write(d / "task.json", json.dumps(data, indent=2) + "\n")
        rebuild_index(p.path.parent.parent)


def touched(d: Path) -> float:
    """When the item was last worked on: the newest mtime of its own files and folders, shallow (an
    out/ clone holds thousands of files)."""
    best = 0.0
    for f in (d, d / "task.json", d / "HANDOFF.md", d / "briefs", d / "out", d / "tmp"):
        with contextlib.suppress(OSError):
            best = max(best, f.stat().st_mtime)
    for sub in ("briefs", "out"):
        with contextlib.suppress(OSError):
            for f in (d / sub).iterdir():
                best = max(best, f.stat().st_mtime)
    return best


def rebuild_index(root: Path | None = None) -> list[dict]:
    """Write <root>/.index.json: one record per item, derived from its task.json (ticket, branches,
    repo, worktrees, bound sessions, status). Every agent-task write calls this; never edit it."""
    root = root or work_root()
    recs = []
    for p in projects(root):
        if p.legacy:
            continue
        for d in p.items():
            t = read_task(d)
            ticket = (t.get("ticket") or (d.name if TICKET_RE.fullmatch(d.name) else "")).upper()
            branches = [b for b in [t.get("branch", ""), *t.get("branches", [])] if b and branch_ticket(b)]
            recs.append({
                "project": p.name, "item": d.name, "ticket": ticket, "status": t.get("status", "open"),
                "repo": t.get("repo", ""), "branches": list(dict.fromkeys(branches)),
                "worktrees": t.get("worktrees", []), "sessions": t.get("sessions", []),
                "pr": t.get("pr", ""), "touched": dt.date.fromtimestamp(touched(d)).isoformat(),
            })
    with contextlib.suppress(OSError):
        _atomic_write(root / INDEX_FILE, json.dumps({"note": "Derived from projects/*/items/*/task.json by agent-task on every write. Never edit.", "generated": dt.datetime.now().isoformat(timespec="seconds"), "items": recs}, indent=1) + "\n")
    return recs


def load_index(root: Path | None = None) -> list[dict]:
    root = root or work_root()
    try:
        return json.loads((root / INDEX_FILE).read_text())["items"]
    except (OSError, ValueError, KeyError, TypeError):
        return rebuild_index(root)


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


def offer_text(d: Decision, sid: str, root: Path | None = None) -> str:
    """The fallback: one line per candidate, the session stays unbound."""
    if not d.candidates:
        return ""
    root = root or work_root()
    lines = ["unbound: no rule was certain. Bind with /bind <arg> (or `" + agent_task_bin() + f" bind <arg> --session {sid}`) if one of these is this session's work:"]
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
        live = [d.name for d in p.items() if _item_status(d) not in DONE_STATUSES]
        item = live[0] if len(live) == 1 else ""
    if item:
        ensure_item(p, item, branch=here.branch, repo=repo, worktree=here.top if here.linked else "")
    b = Binding(parse_project(p.path), item)
    write_binding(sid, b, rule=rule, wt=here.top)
    adopt_session_dir(sid, b, root)
    index(p.name, root)
    return b


def session_dir(sid: str, repo: str, root: Path | None = None) -> Path:
    """The per-session folder of an unbound session: an existing one, else the path it will get."""
    root = root or work_root()
    found = sorted((root / repo).glob(f"*-{sid[:8]}")) if (root / repo).is_dir() else []
    return found[0] if found else root / repo / f"{dt.datetime.now():%Y-%m-%d-%H%M}-{sid[:8]}"


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


def _desc(f: Path) -> str:
    try:
        head = f.read_text(errors="replace")[:4000]
    except OSError:
        return ""
    if f.suffix == ".py":
        m = re.search(r'^(?:#![^\n]*\n)?(?:#[^\n]*\n)*\s*(?:"""|\'\'\')\s*([^\n]+)', head)
        if m:
            return m.group(1).strip().rstrip('"').strip()
    if f.suffix in (".md", ".html"):
        m = re.search(r"^#+\s+(.+)$", head, re.M) or re.search(r"<title>([^<]+)</title>", head)
        return m.group(1).strip() if m else ""
    if f.suffix in ("", ".sh", ".bash", ".sql", ".zsh"):
        for line in head.split("\n")[:8]:
            if (line.startswith("#") and not line.startswith("#!")) or line.startswith("--"):
                return line.lstrip("#- ").strip()
    return ""


def _kind(f: Path) -> str:
    return {".py": "python", ".sh": "shell", ".sql": "sql", ".md": "doc", ".html": "page", ".json": "data", ".csv": "data", ".txt": "text", ".jsonl": "data"}.get(f.suffix, f.suffix.lstrip(".") or "file")


GEN_NOTE = "<!-- Generated by `agent-task index` on every write here. Add `  - use: …` / `  - proved: …` lines under an entry; they are kept by path. Free text goes under ## Notes. -->"


def index(name: str, root: Path | None = None) -> Path | None:
    """Regenerate projects/<name>/INDEX.md, keeping hand-written sub-lines by path and ## Notes."""
    root = root or work_root()
    pdir = root / "projects" / name
    if not pdir.is_dir():
        return None
    idx = pdir / "INDEX.md"
    hand: dict[str, list[str]] = {}
    notes = ""
    if idx.is_file():
        cur = None
        text = idx.read_text(errors="replace")
        if "\n## Notes" in text:
            text, notes = text.split("\n## Notes", 1)
            notes = "## Notes" + notes
        for line in text.split("\n"):
            m = re.match(r"^- `([^`]+)`", line)
            if m:
                cur = m.group(1)
            elif cur and re.match(r"^\s{2,}- ", line):
                hand.setdefault(cur, []).append(line.rstrip())
            elif not line.strip():
                continue
            else:
                cur = None
    p = parse_project(pdir)
    out = [f"# {name}: index", "", GEN_NOTE, ""]
    sections: dict[str, list[str]] = {}
    for f in sorted(pdir.rglob("*")):
        rel = f.relative_to(pdir).as_posix()
        if not f.is_file() or rel in ("INDEX.md",) or any(part.startswith(".") or part == "__pycache__" for part in f.relative_to(pdir).parts):
            continue
        top = rel.split("/")[0] if "/" in rel else "."
        if top in ("items", "tmp"):
            continue
        if top == "data" and rel.count("/") > 2:
            continue
        desc = _desc(f)
        sections.setdefault(top, []).append(f"- `{rel}` ({_kind(f)}){': ' + desc if desc else ''}")
        sections[top] += hand.get(rel, [])
    order = [".", "knowledge", "scripts", "data"] + sorted(k for k in sections if k not in (".", "knowledge", "scripts", "data"))
    for k in order:
        if k in sections:
            out += [f"## {'files' if k == '.' else k + '/'}", *sections[k], ""]
    items = p.items()
    if items:
        out.append("## items/")
        for d in items:
            st = _item_status(d)
            hf = " [handoff]" if (d / "HANDOFF.md").is_file() else ""
            out.append(f"- `items/{d.name}/` ({st}){hf}")
            out += hand.get(f"items/{d.name}/", [])
            # Item scripts are listed so they can be found and promoted; the slice shows project
            # scripts only (reusable ones belong in scripts/ with a use: line).
            sd = d / "scripts"
            for f in sorted(sd.rglob("*")) if sd.is_dir() else []:
                if f.is_file() and not any(x.startswith(".") or x == "__pycache__" for x in f.relative_to(sd).parts):
                    rel = f.relative_to(pdir).as_posix()
                    desc = _desc(f)
                    out.append(f"- `{rel}` ({_kind(f)}){': ' + desc if desc else ''}")
                    out += hand.get(rel, [])
        out.append("")
    if notes:
        out.append(notes.rstrip())
    _atomic_write(idx, "\n".join(out).rstrip() + "\n")
    return idx


def _item_status(d: Path) -> str:
    try:
        return json.loads((d / "task.json").read_text()).get("status", "open")
    except (OSError, ValueError):
        return "open"


def knowledge_titles(pdir: Path, n: int = 8) -> list[str]:
    """Titles of the project's knowledge: each entry's fact (up to its `(source`) in the four
    knowledge files, then the heading of any other document in knowledge/."""
    out = []
    kd = pdir / "knowledge"
    for k in KNOWLEDGE_FILES:
        f = kd / k
        with contextlib.suppress(OSError):
            for line in f.read_text(errors="replace").split("\n"):
                if line.startswith("- "):
                    fact = re.split(r"\s+\(source\b", line[2:], maxsplit=1)[0].strip()
                    out.append(f"{k.removesuffix('.md')}: {fact[:70]}")
    for f in sorted(kd.glob("*.md")) if kd.is_dir() else []:
        if f.name not in KNOWLEDGE_FILES:
            out.append(f"{f.name}: {(_desc(f) or f.stem)[:60]}")
    return out[:n]


def proven_scripts(pdir: Path, n: int = SLICE_SCRIPTS) -> list[str]:
    """Project scripts INDEX.md gives a `use:` or `proved:` line, with those lines (scripts nobody
    vouched for and data/ stay in INDEX.md, read on demand)."""
    idx = pdir / "INDEX.md"
    try:
        text = idx.read_text(errors="replace").split("\n## Notes")[0]
    except OSError:
        return []
    out: list[str] = []
    cur: list[str] = []
    for line in [*text.split("\n"), "- `END`"]:
        if line.startswith("- `"):
            if len(cur) > 1:
                out.append("\n".join(cur))
            m = re.match(r"^- `(scripts/[^`]+)`(?: \([^)]*\))?(?::\s*(.*))?$", line)
            cur = [f"- {m.group(1)}" + (f": {m.group(2)[:90]}" if m.group(2) else "")] if m else []
        elif cur and re.match(r"^\s{2,}- (use|proved):", line):
            cur.append("  " + line.strip()[:140])
    return out[:n]


def _handoff_opening(hf: Path, n: int = 6, width: int = 110) -> list[str]:
    try:
        lines = hf.read_text(errors="replace").split("\n")
    except OSError:
        return []
    return ["  " + x[:width] for x in lines[: n * 2] if x.strip()][:n]


def slice_text(b: Binding, how: str = "", cap: int = SLICE_CAP, handoff: bool = True) -> str:
    """The injected context for a bound session, at most <cap> characters (the INDEX lines go first).
    handoff=False leaves out the HANDOFF.md opening (compact_text shows more of it)."""
    if b.project.legacy:
        d = b.folder
        lines = [
            f"TASK DIR: {d}. Shared by every session on this task (a legacy project of one): read its INDEX.md and reuse its scripts and data before writing new ones. Keep durable files here with a line each in INDEX.md; throwaway logs go to the scratchpad; worktrees stay in <repo>/.claude/worktrees/.",
        ]
        if how:
            lines.insert(0, how)
        hf = d / "HANDOFF.md"
        if handoff and hf.is_file():
            lines.append(f"HANDOFF: {hf}. Read it before starting. It opens:")
            lines += ["  " + x for x in hf.read_text(errors="replace").split("\n")[:8] if x.strip()]
        return "\n".join(lines)[:cap]
    p = b.project
    open_items = [d.name for d in p.items() if _item_status(d) not in DONE_STATUSES]
    head = [
        *([how] if how else []),
        # The root is spelled out once; a long work root would otherwise push the scripts past the cap.
        f"PROJECT: {p.name}{' · item ' + b.item if b.item else ''}. Folder: {b.folder}. Project root: {p.path} (the paths below are relative to it).",
        f"Scope: {p.meta.get('scope') or '(unset: fill scope, terms and repos in PROJECT.md)'} | status: {p.meta.get('status', '?')}"
        + (f" | open items: {', '.join(open_items[:8])}" + (f" (+{len(open_items) - 8})" if len(open_items) > 8 else "") if open_items else ""),
        "Reuse before re-deriving: knowledge/ and scripts/ are project-level; a reusable script goes to scripts/ with a `  - use:` line under it in INDEX.md. "
        + ("PR evidence goes to the item's out/, disposable files to its tmp/. " if b.item else "")
        + "INDEX.md lists every file.",
    ]
    titles = knowledge_titles(p.path)
    mid = (["Knowledge: " + "; ".join(titles)] if titles else ["Knowledge: none recorded yet (knowledge/{findings,decisions,code-map,queries}.md)."])
    tail: list[str] = []
    if handoff and b.item:
        hf = b.folder / "HANDOFF.md"
        if hf.is_file():
            tail = [f"HANDOFF: {hf.relative_to(p.path)}. Read it before starting. It opens:", *_handoff_opening(hf)]
    scripts = proven_scripts(p.path)
    if scripts:
        room = cap - len("\n".join(head + mid + tail)) - 40
        shown = []
        for s in scripts:
            if len(s) + 1 > room:
                break
            shown.append(s)
            room -= len(s) + 1
        if shown:
            mid.append("Proven scripts:\n" + "\n".join(shown))
    text = "\n".join(head + mid + tail)
    if len(text) > cap:
        text = text[: cap - 4].rsplit("\n", 1)[0] + "\n..."
    return text


def durable_lines(hf: Path, n: int = 15) -> list[str]:
    """HANDOFF.md lines worth proposing for knowledge/: bullets under a heading about decisions,
    findings, facts, learnings, gotchas or causes, else bullets that state a verified fact."""
    try:
        lines = hf.read_text(errors="replace").split("\n")
    except OSError:
        return []
    out, keep = [], False
    for line in lines:
        if line.startswith("#"):
            keep = bool(re.search(r"decision|finding|fact|learn|gotcha|caveat|cause|why|knowledge|verified", line, re.I))
            continue
        s = line.strip()
        if s.startswith(("- ", "* ")) and (keep or re.search(r"\b(verified|root cause|because|always|never)\b", s, re.I)):
            out.append(s[2:].strip()[:200])
    return out[:n]


def retro_path(when: dt.date | None = None, root: Path | None = None) -> Path:
    y, w, _ = (when or dt.date.today()).isocalendar()
    return (root or work_root()) / "retro" / f"{y}-W{w:02d}.md"


def record_retro(text: str, *, source: str = "model", tag: str = "", session: str = "", where: str = "", root: Path | None = None) -> str:
    text = " ".join((text or "").split())
    if not tag:
        m = re.match(r"^(mistake|fact|doc|tooling|denial|block|hook-error|correction|tool-error|compaction|review|project|note)\s*:\s*(.+)$", text, re.I)
        tag, text = (m.group(1).lower(), m.group(2)) if m else ("note", text)
    line = f"- [{source}] {today()} {(session or '-')[:8]} {where or '-'} {tag} {text}"
    f = retro_path(root=root)
    with _retro_lock(f.parent):
        fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            head = RETRO_HEADER.format(stem=f.stem) if os.fstat(fd).st_size == 0 else ""
            os.write(fd, (head + line + "\n").encode())
        finally:
            os.close(fd)
    return line


RETRO_HEADER = "# Retro inbox {stem}\n\nOne line per note: `- [auto|model|user] <date> <session8> <project/item|-> <tag> <text>`. `session-review audit` marks a processed line by appending ` (processed <date>)`.\n\n"
RETRO_LINE = re.compile(r"^- \[(auto|model|user)\] ")


@contextlib.contextmanager
def _retro_lock(d: Path) -> Iterator[None]:
    """Every inbox writer holds this: an append never lands between a mark's read and its rename."""
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / ".lock", os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def retro_id(f: Path, n: int, line: str) -> str:
    """A pending line's id for --mark: <week>:<line number>-<hash of the line>. The inbox is
    append-only and a mark only adds a suffix, so the number is the entry's own: an identical note
    appended later gets another id. The hash makes a hand-edited file mark nothing rather than the
    wrong line."""
    return f"{f.stem}:{n}-{hashlib.sha1(line.encode()).hexdigest()[:8]}"


def retro_pending(root: Path | None = None) -> list[tuple[Path, int, str]]:
    """(file, line number, line) of each unprocessed inbox line."""
    d = (root or work_root()) / "retro"
    out = []
    for f in sorted(d.glob("*.md")) if d.is_dir() else []:
        lines = f.read_text(errors="replace").split("\n")
        out += [(f, n, line) for n, line in enumerate(lines, 1) if RETRO_LINE.match(line) and "(processed " not in line]
    return out


def retro_mark(ids: list[str], root: Path | None = None) -> int:
    """Mark the pending lines with these ids (from --pending) processed; lines added since stay
    pending. Re-reads each file under the lock."""
    want = set(ids)
    d = (root or work_root()) / "retro"
    stamp = f" (processed {today()})"
    n = 0
    with _retro_lock(d):
        for f in sorted(d.glob("*.md")):
            if not any(i.startswith(f.stem + ":") for i in want):
                continue
            lines = f.read_text(errors="replace").split("\n")
            hit = 0
            for i, line in enumerate(lines):
                if RETRO_LINE.match(line) and "(processed " not in line and retro_id(f, i + 1, line) in want:
                    lines[i] = line + stamp
                    hit += 1
            if hit:
                _atomic_write(f, "\n".join(lines))
                n += hit
    return n


def where_label(b: Binding | None) -> str:
    if not b:
        return "-"
    return b.project.name + (f"/{b.item}" if b.item else "")


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
