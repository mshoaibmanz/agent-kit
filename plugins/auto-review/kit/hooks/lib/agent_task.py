"""Projects and work items under the work root: the one implementation behind bin/agent-task, the
project-bind hook and session-context. Layout and rules: bin/agent-task's docstring."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import fnmatch
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

from kit_env import kit_dir, kit_env_path
from kit_env import work_root as _work_root


def agent_task_bin() -> str:
    """The installed agent-task, as model-facing text names it."""
    return str(kit_dir() / "bin/agent-task")


TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,5}-[0-9]+)\b")
# Look like ticket keys, never are one.
NOT_TICKETS = {
    "UTF", "SHA", "ISO", "GPT", "RFC", "MD", "MD5", "HTTP", "TLS", "SSL", "PEP", "ES", "AES", "RSA",
    "ECMA", "IPV", "X", "X86", "ARM64", "COVID", "CVE", "CWE", "GHSA",
}
SLICE_CAP = 9000  # characters, about 2.5K tokens
KNOWLEDGE_FILES = ("findings.md", "decisions.md", "code-map.md", "queries.md")
DONE_STATUSES = {"done", "harvested", "closed", "merged"}
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


def is_ticket(key: str, *, known: bool = False) -> bool:
    prefix = key.split("-")[0]
    if prefix in NOT_TICKETS:
        return False
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
        out += [legacy_project(p) for p in sorted(td.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    return out


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
    root = root or work_root()
    key = key.strip()
    if not key:
        return None
    if key.startswith("project:"):
        name, _, item = key[len("project:") :].partition("/")
        p = project_dir(name, root)
        return Binding(parse_project(p), item) if p else None
    leg = find_legacy(key, root)
    if leg:
        return Binding(legacy_project(leg))
    name, _, item = key.partition("/")
    p = project_dir(name, root)
    return Binding(parse_project(p), item) if p else None


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


def bound(sid: str, root: Path | None = None) -> Binding | None:
    f = bind_dir() / sid[:8]
    if not sid or not f.is_file():
        return None
    return from_key(f.read_text().split("\n")[0], root)


def bound_inherited(sid: str) -> bool:
    """The binding came from the tab across /clear and no prompt has named a ticket since."""
    f = bind_dir() / sid[:8]
    try:
        return bool(sid) and f.read_text().split("\n")[1:2] == [INHERITED]
    except OSError:
        return False


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_binding(sid: str, b: Binding, *, inherited: bool = False) -> None:
    _atomic_write(bind_dir() / sid[:8], b.key + "\n" + (INHERITED + "\n" if inherited else ""))
    set_tab(sid, b)
    lab = tab_label_dir() / sid
    _atomic_write(lab, b.label)
    (tab_label_dir() / f"{sid}.model").touch()


def keep_binding(sid: str) -> None:
    """Drop the inherited mark: the binding is now as sticky as one a prompt made."""
    b = bound(sid)
    if b:
        _atomic_write(bind_dir() / sid[:8], b.key + "\n")


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


def create_project(name: str, *, scope: str = "", terms: str = "", repos: str = "", status: str = "active", root: Path | None = None) -> Project:
    root = root or work_root()
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


def ensure_item(p: Project, item: str, *, branch: str = "", repo: str = "") -> Path:
    d = p.path / "items" / item
    for sub in ("briefs", "out"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    tj = d / "task.json"
    if not tj.exists():
        ticket = item if TICKET_RE.fullmatch(item) else ""
        _atomic_write(tj, json.dumps({"item": item, "ticket": ticket, "branch": branch, "repo": repo, "status": "open", "created": today()}, indent=2) + "\n")
    pm = p.path / "PROJECT.md"
    if pm.is_file() and TICKET_RE.fullmatch(item) and item not in pm.read_text():
        text = pm.read_text()
        row = f"| {item} | {item} | | open |\n"
        marker = "|---|---|---|---|\n"
        text = text.replace(marker, marker + row, 1) if marker in text else text + "\n" + row
        _atomic_write(pm, text)
    return d


@dataclass
class Resolution:
    outcome: str  # exact | strong | ambiguous | none
    candidates: list[tuple[Project, float]]
    ticket: str = ""


def changed_paths(cwd: str, branch: str = "") -> list[str]:
    def git(*a: str) -> str:
        try:
            return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    base = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "origin/master"
    out = git("diff", "--name-only", f"{base}...{branch or 'HEAD'}")
    return [x for x in out.split("\n") if x][:400]


def resolve(text: str, *, branch: str = "", paths: list[str] | None = None, repo: str = "", root: Path | None = None) -> Resolution:
    keys = tickets_in(text, known=True)
    asked = requested_ticket(text, keys)
    if asked:
        # A request for other work: neither the branch (its ticket, words and changed paths) nor a
        # ticket mentioned beside it ("start on DEMO-300 like DEMO-100") says anything.
        keys, branch, paths = [asked], "", None
    bt = branch_ticket(branch)
    if bt and bt not in keys:
        keys.append(bt)
    ps = projects(root)
    for k in keys:
        hits = [p for p in ps if k in p.tickets]
        live = [p for p in hits if not p.legacy] or hits
        if len(live) == 1:
            return Resolution("exact", [(live[0], 100.0)], k)
        if len(live) > 1:
            return Resolution("ambiguous", [(p, 100.0) for p in live[:3]], k)
    tw = words(text + " " + branch.replace("-", " ").replace("/", " "))
    scored: list[tuple[Project, float]] = []
    for p in ps:
        s = 0.0
        globs = p.listing("paths")
        if paths and globs:
            hit = sum(1 for f in paths if any(fnmatch.fnmatch(f, g) for g in globs))
            if hit:
                s += 3 + 7 * hit / len(paths)
        for term in p.listing("terms") if not p.legacy else [p.meta.get("terms", "")]:
            tws = words(term)
            if tws and (tws <= tw if not p.legacy else len(tws & tw) >= max(1, min(2, len(tws)))):
                s += 2
        if s and repo and repo in p.listing("repos"):
            s += 1
        if s >= 3:
            scored.append((p, s))
    scored.sort(key=lambda x: -x[1])
    # A ticket the prompt merely mentions ("like DEMO-100 did") wins only on an exact hit, above; one
    # it asks for ("start on DEMO-300") has already replaced the branch's.
    ticket = asked or bt or (keys[0] if keys else "")
    if not scored:
        return Resolution("none", [], ticket)
    top = scored[0][1]
    second = scored[1][1] if len(scored) > 1 else 0
    if top >= 5 and top >= 2 * second:
        return Resolution("strong", scored[:1], ticket)
    return Resolution("ambiguous", scored[:3], ticket)


def bind(sid: str, target: str, *, desc: str = "", branch: str = "", repo: str = "", root: Path | None = None) -> Binding:
    """Bind <sid> to <project>[/<item>], a legacy task folder, or a ticket (its project, else a new
    project of one named after it). An existing projects/<name> is used under its own spelling;
    a ticket key with no item means <project>/<ticket> (DEMO-77 is DEMO-77/DEMO-77)."""
    root = root or work_root()
    name, _, item = target.partition("/")
    pdir = project_dir(name, root)
    if not item:
        leg = None if pdir else find_legacy(name, root)
        if leg:
            b = Binding(legacy_project(leg))
            write_binding(sid, b)
            return b
        t = name.upper()
        if TICKET_RE.fullmatch(t):
            hits = [] if pdir else [p for p in projects(root) if not p.legacy and t in p.tickets]
            if pdir:
                p = parse_project(pdir)
            elif hits:
                p = hits[0]
            else:
                p = create_project(t, scope=desc.replace("-", " "), terms=desc.replace("-", " "), repos=repo, status="project-of-one", root=root)
                record_retro(f"created project of one {t}", source="auto", tag="project", session=sid, where=f"{t}/{t}")
            ensure_item(p, t, branch=branch, repo=repo)
            b = Binding(parse_project(p.path), t)
            write_binding(sid, b)
            adopt_session_dir(sid, b, root)
            index(p.name, root)
            return b
    if not pdir:
        name = slug(name) or "misc"
        pdir = project_dir(name, root)
    p = parse_project(pdir) if pdir else create_project(name, scope=desc, terms=desc, repos=repo, root=root)
    if item:
        ensure_item(p, item, branch=branch, repo=repo)
    b = Binding(parse_project(p.path), item)
    write_binding(sid, b)
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
        if top == "items":
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


def _knowledge_counts(pdir: Path) -> str:
    parts = []
    for k in KNOWLEDGE_FILES:
        f = pdir / "knowledge" / k
        if f.is_file():
            n = sum(1 for line in f.read_text(errors="replace").split("\n") if line.startswith("- "))
            parts.append(f"{k} {n}")
    return ", ".join(parts)


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
    head = [
        *( [how] if how else [] ),
        f"PROJECT: {p.name}{' · item ' + b.item if b.item else ''}. Folder: {b.folder}. Project root: {p.path} (PROJECT.md, INDEX.md, knowledge/, scripts/, data/).",
        f"Scope: {p.meta.get('scope') or '(unset: fill scope, terms, repos and paths in PROJECT.md)'} | status: {p.meta.get('status', '?')}",
        f"Knowledge entries ({_knowledge_counts(p.path)}): read the relevant file before re-deriving a fact; add new ones with source, verified date and status. Durable files go in this project; INDEX.md regenerates on every write here (add `  - use:`/`  - proved:` lines under an entry). Worktrees stay in <repo>/.claude/worktrees/.",
    ]
    open_items = [f"{d.name} ({_item_status(d)})" for d in p.items() if _item_status(d) not in DONE_STATUSES]
    if open_items:
        head.append("Open items: " + ", ".join(open_items[:12]))
    tail: list[str] = []
    if handoff and b.item:
        hf = b.folder / "HANDOFF.md"
        if hf.is_file():
            tail.append(f"HANDOFF: {hf}. Read it before starting. It opens:")
            tail += ["  " + x for x in hf.read_text(errors="replace").split("\n")[:8] if x.strip()]
    idx = p.path / "INDEX.md"
    body = []
    if idx.is_file():
        for line in idx.read_text(errors="replace").split("\n"):
            if line.startswith(("- `", "  - ", "## ")) and not line.startswith("## Notes"):
                body.append(line)
    budget = cap - len("\n".join(head + tail)) - 120
    shown: list[str] = []
    used = 0
    for line in body:
        if used + len(line) + 1 > budget:
            shown.append(f"... INDEX.md has {len(body) - len(shown)} more lines: {idx}")
            break
        shown.append(line)
        used += len(line) + 1
    mid = ["INDEX (knowledge and scripts):", *shown] if shown else []
    return "\n".join(head + mid + tail)[:cap]


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
    hint = f"the branch names {bt}, so the first prompt binds it." if bt else "a ticket key in a prompt binds it, or /bind <project>[/<item>]."
    return f"OUT DIR: {folder} (created on first write). No project is bound: {hint} Keep what is worth keeping there, throwaway logs in the scratchpad."


def auto_handoff_current(folder: Path) -> bool:
    """HANDOFF.auto.md exists and is not older than HANDOFF.md: once the model rewrites HANDOFF.md,
    an earlier compaction's snapshot (old branch, status, prompts) is history, not state."""
    auto, hf = folder / "HANDOFF.auto.md", folder / "HANDOFF.md"
    if not auto.is_file():
        return False
    return not hf.is_file() or auto.stat().st_mtime >= hf.stat().st_mtime


def compact_text(sid: str, cwd: str = "", repo: str = "", branch: str = "", cap: int = SLICE_CAP) -> str:
    """After a compaction: the slice (an unbound session: its startup line), then HANDOFF.md and a
    current HANDOFF.auto.md, each bounded, within <cap> characters. The handoffs get up to 60% of it."""
    b, folder = handoff_folder(sid, cwd, repo)
    hand = _bounded(folder / "HANDOFF.md", min(2500, cap * 35 // 100))
    if auto_handoff_current(folder):
        hand += _bounded(folder / "HANDOFF.auto.md", min(2000, cap * 25 // 100))
    tail = "\n".join(["Compacted. Read these before continuing:", *hand]) if hand else ""
    room = cap - len(tail) - 1
    if b:
        head = slice_text(b, "", cap=room, handoff=False)
    else:
        head = unbound_text(folder, branch)[:room]
    return (head + ("\n" + tail if tail else ""))[:cap]
