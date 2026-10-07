"""Projects and work items under the work root: the one implementation behind bin/agent-task, the
project-bind hook and session-context (the binding rules are binding.py, the retro inbox retro.py).
Layout and rules: bin/agent-task's docstring."""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import json
import os
import re
import shutil
import subprocess
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
    # bind(pick_item=True) on a project with no sole open item: the open items to offer.
    open_items: list[str] = field(default_factory=list, compare=False)

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
    atomic_write(bind_dir() / sid[:8], "\n".join([data["key"], *extra]) + "\n")


def bound(sid: str, root: Path | None = None) -> Binding | None:
    data = binding_file(sid)
    if not data:
        return None
    b = from_key(data["key"], root)
    if b and b.key != data["key"]:
        # A legacy key (tasks/<name>, <p>/<i>): migrate the file to `project:p/i` on read.
        _write_binding_file(sid, {**data, "key": b.key})
    return b


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


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_binding(sid: str, b: Binding, *, inherited: bool = False, rule: str = "", wt: str = "") -> None:
    _write_binding_file(sid, {"key": b.key, **({INHERITED: "1"} if inherited else {}), "rule": rule, "wt": wt})
    set_tab(sid, b)
    lab = tab_label_dir() / sid
    atomic_write(lab, b.label)
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
            atomic_write(f, new + text[len(key) :])
    return out


def set_tab(sid: str, b: Binding | None) -> None:
    """The tab's binding is the binding of the session now in it; an unbound one blanks it, so a
    later /clear never inherits from a session that has gone."""
    tk = tab_key()
    if not tk:
        return
    f = bind_dir() / f"tab-{tk}"
    if b:
        atomic_write(f, f"{b.key}\t{sid}\n")
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
        atomic_write(vscode, json.dumps({"files.watcherExclude": excluded, "search.exclude": excluded}, indent=2) + "\n")
    cursor = root / ".cursorignore"
    if not cursor.exists() and not cursor.is_symlink():
        # gitignore syntax: `tmp/` matches a tmp folder at any depth; `tmp/**` would anchor at the root.
        atomic_write(cursor, "".join(pattern.removeprefix("**/").removesuffix("**") + "\n" for pattern in EDITOR_EXCLUDES))


def create_project(name: str, *, scope: str = "", terms: str = "", repos: str = "", status: str = "active", root: Path | None = None) -> Project:
    root = root or work_root()
    if not root.exists():
        root.mkdir(parents=True)
        init_work_root(root)
    p = root / "projects" / name
    for sub in ("knowledge", "scripts", "data", "items"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    if not (p / "PROJECT.md").exists():
        atomic_write(p / "PROJECT.md", PROJECT_TEMPLATE.format(name=name, scope=scope, terms=terms, repos=repos, status=status, rows=""))
    for k in KNOWLEDGE_FILES:
        kf = p / "knowledge" / k
        if not kf.exists():
            atomic_write(kf, KNOWLEDGE_HEADER.format(title=k.removesuffix(".md").replace("-", " ").capitalize()))
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
        atomic_write(tj, json.dumps({"item": item, "ticket": ticket, "branch": branch if branch_ticket(branch) else "", "repo": repo, "status": "open", "created": today()}, indent=2) + "\n")
    pm = p.path / "PROJECT.md"
    if pm.is_file() and TICKET_RE.fullmatch(item) and item not in pm.read_text():
        text = pm.read_text()
        row = f"| {item} | {item} | | open |\n"
        marker = "|---|---|---|---|\n"
        text = text.replace(marker, marker + row, 1) if marker in text else text + "\n" + row
        atomic_write(pm, text)
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
        atomic_write(d / "task.json", json.dumps(data, indent=2) + "\n")
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
        atomic_write(root / INDEX_FILE, json.dumps({"note": "Derived from projects/*/items/*/task.json by agent-task on every write. Never edit.", "generated": dt.datetime.now().isoformat(timespec="seconds"), "items": recs}, indent=1) + "\n")
    return recs


def load_index(root: Path | None = None) -> list[dict]:
    root = root or work_root()
    try:
        return json.loads((root / INDEX_FILE).read_text())["items"]
    except (OSError, ValueError, KeyError, TypeError):
        return rebuild_index(root)


def session_dir(sid: str, repo: str, root: Path | None = None) -> Path:
    """The per-session folder of an unbound session: an existing one, else the path it will get."""
    root = root or work_root()
    found = sorted((root / repo).glob(f"*-{sid[:8]}")) if (root / repo).is_dir() else []
    return found[0] if found else root / repo / f"{dt.datetime.now():%Y-%m-%d-%H%M}-{sid[:8]}"



def describe(f: Path) -> str:
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
        desc = describe(f)
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
            st = item_status(d)
            hf = " [handoff]" if (d / "HANDOFF.md").is_file() else ""
            out.append(f"- `items/{d.name}/` ({st}){hf}")
            out += hand.get(f"items/{d.name}/", [])
            # Item scripts are listed so they can be found and promoted; the slice shows project
            # scripts only (reusable ones belong in scripts/ with a use: line).
            sd = d / "scripts"
            for f in sorted(sd.rglob("*")) if sd.is_dir() else []:
                if f.is_file() and not any(x.startswith(".") or x == "__pycache__" for x in f.relative_to(sd).parts):
                    rel = f.relative_to(pdir).as_posix()
                    desc = describe(f)
                    out.append(f"- `{rel}` ({_kind(f)}){': ' + desc if desc else ''}")
                    out += hand.get(rel, [])
        out.append("")
    if notes:
        out.append(notes.rstrip())
    atomic_write(idx, "\n".join(out).rstrip() + "\n")
    return idx


def item_status(d: Path) -> str:
    try:
        return json.loads((d / "task.json").read_text()).get("status", "open")
    except (OSError, ValueError):
        return "open"


HARVEST_SKIP = {"worktrees", ".git", "node_modules", "sessions", "__pycache__", ".venv"}
PROMOTE_SUFFIXES = {".py": "python3", ".sh": "bash", ".sql": "run"}


def _item_files(d: Path) -> list[Path]:
    """The item's own files: checkouts, dependencies, per-session folders and the bulk the tmp sweep
    calls generated (scratch homes, checkouts) are pruned unread."""
    # bin/lib/tmp_sweep imports this module, so it is imported on use (bin/agent-task puts bin/lib on the path).
    from tmp_sweep import generated

    out = []
    for top, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in HARVEST_SKIP and not generated(Path(top) / x)]
        out += [Path(top) / n for n in names]
    return sorted(out)


def item_scripts(p: Project, d: Path) -> tuple[list[Path], list[Path]]:
    """Scripts in item folder d whose name the project's scripts/ lacks: (those under its tmp/, which
    close offers for promotion, the rest)."""
    sd = p.path / "scripts"
    have = {f.name for f in sd.iterdir()} if not p.legacy and sd.is_dir() else set()
    tmp: list[Path] = []
    other: list[Path] = []
    for f in _item_files(d):
        if f.suffix not in PROMOTE_SUFFIXES or f.name in have:
            continue
        if d / "tmp" not in f.parents:
            other.append(f)
        elif not f.is_symlink():
            tmp.append(f)
    return tmp, other


def harvest(p: Project, item: str, *, closing: bool, pr: str) -> str:
    """The harvest prompts of an item (or a legacy folder), then its status: harvested, or closed
    when <closing> (close has already offered, or been told to skip, the tmp/ scripts)."""
    b = Binding(p, item)
    d = b.folder
    tmp, other = item_scripts(p, d)
    scripts = other if closing else sorted(tmp + other)
    docs = [f for f in _item_files(d) if f.suffix == ".md" and f.name != "HANDOFF.md"]
    kroot = p.path / "knowledge" if not p.legacy else d
    out = [
        f"HARVEST {where_label(b)} ({d}). Answer three questions, act on each, then append the summary.",
        "1. Domain docs: which facts are true beyond this ticket, about business meaning, flows or decisions, and verified in code? Add them through the docs-rollup worktree (session-review skill, \"Domain-doc learnings\"), then record each in knowledge/ as a one-line `status: in-docs -> <doc link>` entry.",
        f"2. Project knowledge: which other findings, decisions, code-map notes and query recipes go to {kroot}/{{findings,decisions,code-map,queries}}.md, each with a source, a last-verified date and a status (project-only or pending-docs)?",
        f"3. Scripts: which of these move to {p.path / 'scripts'}/ with a docstring (git mv is not needed; mv, then fix callers):",
    ]
    out += [f"   - {f.relative_to(d)}: {describe(f) or '(no docstring)'}" for f in scripts[:30]]
    if not scripts:
        out.append("   (no scripts in the item)")
    if docs:
        out.append("   Item docs to mine for 1 and 2: " + ", ".join(str(f.relative_to(d)) for f in docs[:15]))
    proposed = durable_lines(d / "HANDOFF.md")
    if proposed:
        out.append(f"Proposed for {kroot}/ (lines from HANDOFF.md that read as durable; keep, reword or drop each):")
        out += [f"   - {x}" for x in proposed]
    out.append(f"Then append to {d / 'HANDOFF.md'} (never overwrite it): `## Summary (harvested {today()})` with the PR, what shipped, and where each harvested fact and script went.")
    out.append("Last, record at most 3 learnings: agent-task retro \"<mistake|fact|doc|tooling>: <text>\".")
    if not p.legacy and item:
        update_item(p, item, status="closed" if closing else "harvested", harvested=today(), **({"pr": pr} if pr else {}), **({"closed": today()} if closing else {}))
        index(p.name)
    return "\n".join(out)


def add_use_line(p: Project, rel: str, text: str) -> None:
    """A `  - use:` line under the INDEX.md entry of rel (index keeps hand lines by path)."""
    idx = index(p.name, p.path.parent.parent)
    if idx is None:
        return
    lines = idx.read_text().split("\n")
    for n, line in enumerate(lines):
        if line.startswith(f"- `{rel}`"):
            if not (n + 1 < len(lines) and lines[n + 1].startswith("  - use:")):
                lines.insert(n + 1, f"  - use: {text}")
                atomic_write(idx, "\n".join(lines))
            return


def promote(p: Project, d: Path, f: Path) -> str:
    """Copy item script f (under d/tmp/) to the project's scripts/ with an INDEX.md use line."""
    dest = p.path / "scripts" / f.name
    if dest.exists():
        return f"kept {f.relative_to(d)}: scripts/{f.name} exists"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(f, dest)
    desc = describe(f) or f"promoted from items/{d.name}/{f.relative_to(d)}; say when to reach for it"
    add_use_line(p, f"scripts/{f.name}", f"`{PROMOTE_SUFFIXES[f.suffix]} scripts/{f.name}`: {desc}")
    return f"promoted {f.relative_to(d)} -> scripts/{f.name} (INDEX.md has its use line)"


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
            out.append(f"{f.name}: {(describe(f) or f.stem)[:60]}")
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
    open_items = [d.name for d in p.items() if item_status(d) not in DONE_STATUSES]
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


def where_label(b: Binding | None) -> str:
    if not b:
        return "-"
    return b.project.name + (f"/{b.item}" if b.item else "")
