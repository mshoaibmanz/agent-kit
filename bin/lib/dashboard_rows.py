"""The row builders both views share: the Setup view's cards (dashboard_sections) and the docs view
(dashboard_docs) build their hook, role, review-round, MCP server and skill rows here, so the two
cannot disagree."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, NamedTuple

from credentials import show_args, show_url
from dashboard_html import Badge, Cell, Code, Lines, Muted, Row, Strong, Table, anchor
from hosts import HOSTS, frontmatter, skill_hosts

if TYPE_CHECKING:
    from dashboard_sections import Setup

TableRows = list[Row | tuple[Cell, ...]]

# How a shared row builder shows a file: the Setup view's full path cell, or the docs view's link.
Linker = Callable[[Path], Cell]


class HookRow(NamedTuple):
    """One registry entry as agent-kit's validate_registry accepts it, each field a plain value."""

    event: str
    matcher: str  # "" when the entry has none: every tool
    name: str
    command: str
    hosts: tuple[str, ...]
    description: str
    timeout: str
    blocking: bool


class SkillEntry(NamedTuple):
    """One skill the kit installs, once: layer is kit or pack (the layer setup took it from), md its
    SKILL.md, and replaces whether it stands in for an engine skill of that name with other text."""

    name: str
    layer: str
    md: Path
    replaces: bool


def load_script(path: Path, name: str) -> ModuleType | None:
    """A kit bin script as a module, without writing bytecode beside it or keeping the import
    paths it adds."""
    if not path.is_file():
        return None
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        return None
    module = importlib.util.module_from_spec(spec)
    dont_write, paths = sys.dont_write_bytecode, list(sys.path)
    sys.dont_write_bytecode = True
    sys.modules[name] = module  # a dataclass in the script looks its module up there
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
        sys.path[:] = paths
    return module


def script_module(path: Path) -> ModuleType | None:
    """A Python bin script that builds its own argparse parser() as a module, else None. Only a
    script that defines one and guards its main is loaded: anything else might act on import."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    if not re.search(r"^def parser\(", text, re.M) or "__name__ ==" not in text:
        return None
    return load_script(path, "dashboard_bin_" + re.sub(r"\W", "_", path.name))


def server_line(setup: Setup, name: str) -> tuple[str, str, str]:
    """(transport, its command or URL with any credential masked, the catalog's description) of one
    catalog server, as both views show it."""
    filled, raw = setup.filled.get(name), setup.catalog.get(name)
    spec = filled if isinstance(filled, dict) else {}
    description = str(raw.get("description") or "") if isinstance(raw, dict) else ""
    if "url" in spec:
        return "http", show_url(str(spec["url"])).text, description
    target = show_args([str(spec.get("command", "")), *map(str, spec.get("args") or [])]).text
    return "stdio", target, description


def skill_text(md: Path) -> tuple[str, set[str], str]:
    """(SKILL.md's text, the hosts its hosts: line allows, that list as shown)."""
    text = md.read_text(errors="replace")
    try:
        allowed = skill_hosts(md, text)
    except ValueError:
        return text, set(), "invalid hosts: line"
    return text, allowed, ", ".join(sorted(allowed))


def layer_cell(entry: SkillEntry) -> Cell:
    badge = Badge(entry.layer)
    return (badge, Muted("replaces the engine's skill of this name")) if entry.replaces else badge


def event_cell(row: HookRow) -> str:
    """The event with its matcher folded in: PreToolUse · Bash, Read."""
    return f"{row.event} · {', '.join(row.matcher.split('|'))}" if row.matcher else row.event


def hook_table(
    rows: list[HookRow],
    hooks: Path,
    prefix: str,
    link: Linker,
    installed: set[tuple[str, str]] | None = None,
    timeout: bool = False,
    **table: Any,
) -> Table:
    """The hooks table both views show, one row per registry entry: a row installed does not hold
    is marked not installed, and timeout adds the Setup view's column."""
    out: TableRows = []
    ids: Counter[str] = Counter()
    for hook in rows:
        row = anchor(prefix, f"{hook.event}-{hook.name}")
        ids[row] += 1
        row += f"-{ids[row]}" if ids[row] > 1 else ""
        kind: Cell = Badge("blocking", "warn") if hook.blocking else Badge("advisory")
        if installed is not None and (hook.event, hook.name) not in installed:
            kind = (kind, Badge("not installed"))
        script = hooks / hook.name
        cells: tuple[Cell, ...] = (
            Strong(hook.name),
            event_cell(hook),
            kind,
            tuple(Badge(h) for h in hook.hosts),
            *((hook.timeout,) if timeout else ()),
            link(script) if script.is_file() else Code(hook.command),
        )
        out.append(Row(cells, row, hook.description))
    headers = ("Hook", "Event", "Kind", "Hosts", *(("Timeout",) if timeout else ()), "Source")
    return Table(headers, out, **table)


def role_table(setup: Setup, prefix: str, link: Linker, live: bool = False, **table: Any) -> Table:
    """The roles table both views show; live adds how each runs per host and whether Claude has
    its rendered agent file."""
    roles = setup.roles
    assert roles is not None
    hosts = [h for h in roles.hosts if h in HOSTS]
    rows: TableRows = []
    for name, role in roles.roles.items():
        src = setup.kit / "agents" / f"{name}.md"
        description = ""
        if src.is_file():
            description = str(frontmatter(src.read_text(errors="replace")).get("description", ""))
        cells: list[Cell] = [
            Strong(name),
            Code(f"{role.provider}:{role.model}"),
            role.effort,
            Badge(role.prefix, "warn") if role.prefix else Muted("-"),
            role.fallback or Muted("-"),
            link(src) if src.is_file() else Muted("None (the main session)"),
        ]
        if live:
            runs: list[Cell] = []
            for host in hosts:
                try:
                    runs.append(f"{host}: {roles.invoke(role, host)}")
                except SystemExit:
                    runs.append(f"{host}: ?")
            rendered = setup.roots["claude"] / "agents" / f"{name}.md"
            cells += [Lines(tuple(runs)), Badge("rendered", "ok") if rendered.is_file() else Muted("-")]
        rows.append(Row(tuple(cells), anchor(prefix, name), description))
    headers = ("Role", "Model", "Effort", "Review prefix", "Fallback", "Agent file")
    headers += ("Runs as", "Claude agent") if live else ()
    return Table(headers, rows, **table)


def round_table(setup: Setup, **table: Any) -> Table:
    """roles.toml [review] as written: each list's key and its roles."""
    review = setup.roles.review if setup.roles is not None else {}
    rows: TableRows = [(Code(k), ", ".join(v)) for k, v in review.items()]
    return Table(("[review] key", "Roles"), rows, **table)
