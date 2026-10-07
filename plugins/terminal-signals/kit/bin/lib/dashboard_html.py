"""The dashboard's one renderer: typed section data (plain values) to escaped HTML, laid into the
dashboard.html template beside this file. Nothing else in the dashboard writes markup."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Literal, NamedTuple, Union
from urllib.parse import quote

TEMPLATE = Path(__file__).with_name("dashboard.html")
Kind = Literal["", "ok", "bad", "warn", "off"]
Level = Literal["", "warn", "bad"]
State = Literal["present", "missing", "unknown", "clean"]
BADGE_KINDS: dict[State, Kind] = {"present": "ok", "missing": "bad", "clean": "ok", "unknown": "warn"}


@dataclass(frozen=True)
class Badge:
    text: str
    kind: Kind = ""

    @classmethod
    def state(cls, state: State) -> Badge:
        return cls("not checked" if state == "unknown" else state, BADGE_KINDS[state])


@dataclass(frozen=True)
class Strong:
    text: str


@dataclass(frozen=True)
class Muted:
    text: str


@dataclass(frozen=True)
class Code:
    text: str


@dataclass(frozen=True)
class Command:
    """A shell command to copy; every path in it is already shlex-quoted."""

    text: str


@dataclass(frozen=True)
class Fold:
    """A <details>: summary shown, body on demand."""

    summary: Cell
    body: tuple[Cell, ...]


@dataclass(frozen=True)
class Lines:
    """Cells one per line."""

    items: tuple[Cell, ...]


@dataclass(frozen=True)
class Row:
    """A table row: anchor is its element id (a Needs attention item links to it), description a
    muted line under its first cell."""

    cells: tuple[Cell, ...]
    anchor: str = ""
    description: str = ""


@dataclass
class Table:
    headers: tuple[str, ...]
    rows: list[Row | tuple[Cell, ...]]
    title: str = ""
    empty: str = "None"
    note: str = ""
    anchor: str = ""  # the title's element id


@dataclass(frozen=True)
class Para:
    items: tuple[Cell, ...]


@dataclass(frozen=True)
class Pre:
    text: str


@dataclass(frozen=True)
class AddHelper:
    """The add-connection form. The page's one script fills it in the browser and sends nothing;
    ports and aliases are the known tunnels', for its collision check, and patterns are
    (name, value) pairs of ro-mysql's own checks, written as data-p-<name> attributes."""

    ports: tuple[str, ...]
    aliases: tuple[str, ...]
    patterns: tuple[tuple[str, str], ...] = ()


Cell = Union[str, int, Path, Badge, Strong, Muted, Code, Command, Fold, Lines, Table, tuple]
Block = Union[Table, Para, Pre, AddHelper]


@dataclass(frozen=True)
class Action:
    """A line in Needs attention or Actions: the command that changes it, and the row (anchor) in
    the section (view) it is about."""

    label: str
    command: str = ""
    view: str = ""
    anchor: str = ""


@dataclass
class Section:
    """One card and view. count: the sidebar number; summary: the card head's line; level: warn or
    bad when something needs a look."""

    key: str
    title: str
    description: str = ""
    sources: list[Path] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    count: int | None = None
    summary: str = ""
    level: Level = ""
    actions: list[Action] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    attention: list[Action] = field(default_factory=list)


def anchor(view: str, name: str) -> str:
    """A stable element id for a row: the view's prefix and the row's name, e.g. mcp-sentry."""
    return f"{view}-{re.sub(r'[^A-Za-z0-9_.-]+', '-', name).strip('-')}"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def copy_button(text: str) -> str:
    return f'<button class="cp" type="button" data-c="{esc(text)}">copy</button>'


def path_html(path: Path | str) -> str:
    """An editor link, the path (truncated by the page, whole in its title) and a copy button."""
    text = str(path)
    return (
        f'<span class="path"><a href="vscode://file{esc(quote(text))}" title="Open in editor">open</a>'
        f'<code class="p" title="{esc(text)}">{esc(text)}</code>{copy_button(text)}</span>'
    )


def cell(value: Cell) -> str:
    if isinstance(value, Path):
        return path_html(value)
    if isinstance(value, Badge):
        return f'<span class="b {esc(value.kind)}">{esc(value.text)}</span>'
    if isinstance(value, Strong):
        return f"<b>{esc(value.text)}</b>"
    if isinstance(value, Muted):
        return f'<span class="muted">{esc(value.text)}</span>'
    if isinstance(value, Code):
        return f"<code>{esc(value.text)}</code>"
    if isinstance(value, Command):
        return f'<div class="cmd"><code>{esc(value.text)}</code>{copy_button(value.text)}</div>'
    if isinstance(value, Fold):
        return (
            f"<details><summary>{cell(value.summary)}</summary>"
            + "".join(cell(v) for v in value.body)
            + "</details>"
        )
    if isinstance(value, Lines):
        return "<br>".join(cell(v) for v in value.items)
    if isinstance(value, Table):
        return table(value)
    if isinstance(value, tuple):
        return " ".join(cell(v) for v in value)
    return esc(value)


DESCRIPTION_LIMIT = 220


def clamp(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    """text on one line, cut to limit characters with an ellipsis."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def row(value: Row | tuple[Cell, ...]) -> str:
    value = value if isinstance(value, Row) else Row(value)
    ident = f' id="{esc(value.anchor)}"' if value.anchor else ""
    cells = [cell(c) for c in value.cells]
    if value.description and cells:
        cells[0] += f'<div class="desc">{esc(clamp(value.description))}</div>'
    return f"<tr{ident}>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def table(value: Table) -> str:
    ident = f' id="{esc(value.anchor)}"' if value.anchor else ""
    title = f"<h3{ident}>{esc(value.title)}</h3>" if value.title else ""
    note = f"<p>{esc(value.note)}</p>" if value.note else ""
    if not value.rows:
        return f'{title}{note}<p class="muted">{esc(value.empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in value.headers)
    body = "".join(row(r) for r in value.rows)
    # A wide table scrolls inside its card, not the page.
    return (
        f'{title}{note}<div class="tw"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


class FormField(NamedTuple):
    key: str
    label: str
    placeholder: str
    value: str = ""


AH_FIELDS = (
    FormField("name", "Name", "orders"),
    FormField("user", "DB user", "reader"),
    FormField("via", "Bastion alias", "jump"),
    FormField("host", "Remote host", "db.example.com"),
    FormField("port", "Remote port", "3306", "3306"),
    FormField("local", "Local port", "15310"),
)


def add_helper(value: AddHelper) -> str:
    fields = "".join(
        f'<label>{esc(f.label)}<input data-f="{f.key}" type="text" autocomplete="off" spellcheck="false" '
        f'placeholder="{esc(f.placeholder)}"' + (f' value="{esc(f.value)}"' if f.value else "") + "></label>"
        for f in AH_FIELDS
    )
    patterns = "".join(f' data-p-{esc(name)}="{esc(pattern)}"' for name, pattern in value.patterns)
    return (
        f'<div class="ah" id="sql-add" data-ports="{esc(" ".join(value.ports))}" '
        f'data-aliases="{esc(" ".join(value.aliases))}"{patterns}><h3>Add an instance</h3>'
        '<p class="muted">Paste a connection URI or fill the fields. This form runs in the page and '
        "sends nothing; a password in the URI is removed, never shown.</p>"
        # The script sets the example placeholder: written here, the page's userinfo mask would hide it.
        '<label class="wide">Connection URI<input id="ah-uri" type="text" autocomplete="off" '
        'spellcheck="false"></label>'
        f'<div class="ahf">{fields}<label class="chk"><input data-f="staging" type="checkbox">Staging</label></div>'
        '<p class="alert" id="ah-warn" hidden></p>'
        '<div class="cmd"><code id="ah-cmd"></code><button class="cp" type="button" data-c="" id="ah-cmd-cp">'
        "copy</button></div>"
        '<details><summary>Or by hand: the checked ssh block, the Keychain prompt and the check</summary>'
        '<div class="cmd"><pre id="ah-manual"></pre><button class="cp" type="button" data-c="" id="ah-man-cp">'
        "copy</button></div></details></div>"
    )


def block(value: Block) -> str:
    if isinstance(value, Table):
        return table(value)
    if isinstance(value, Pre):
        return f"<pre>{esc(value.text)}</pre>"
    if isinstance(value, AddHelper):
        return add_helper(value)
    return "<p>" + " ".join(cell(v) for v in value.items) + "</p>"


def level(value: Section) -> Level:
    """bad when the section could not read something, else its own level."""
    return "bad" if value.alerts else value.level


def section(value: Section) -> str:
    sources = "".join(path_html(p) for p in value.sources)
    alerts = "".join(f'<p class="alert">{esc(a)}</p>' for a in value.alerts)
    body = "".join(block(b) for b in value.blocks)
    description = f'<div class="desc">{esc(value.description)}</div>' if value.description else ""
    return (
        f'<section id="{esc(value.key)}" class="card"><div class="head"><div><h2>{esc(value.title)}</h2>'
        f'{description}<div class="sum">{esc(value.summary)}</div></div><div class="src">{sources}</div></div>'
        f"{alerts}{body}</section>"
    )


def attention_item(item: Action) -> str:
    target = item.anchor or item.view
    label = f'<a href="#{esc(target)}">{esc(item.label)}</a>' if target else esc(item.label)
    return f"<li>{label}{cell(Command(item.command)) if item.command else ''}</li>"


def attention(items: list[Action]) -> str:
    if items:
        body = "<ul>" + "".join(attention_item(a) for a in items) + "</ul>"
    else:
        body = '<p class="muted">Nothing needs attention.</p>'
    kind = "warn" if items else "ok"
    return (
        f'<section id="attention" class="card attention {kind}"><div class="head"><div>'
        f'<h2>Needs attention</h2><div class="sum">{len(items)} item(s)</div></div></div>{body}</section>'
    )


def nav_link(key: str, title: str, count: int | None, kind: Level) -> str:
    dot = f'<i class="dot {esc(kind)}" title="{esc(kind)}"></i>' if kind else ""
    number = "" if count is None else str(count)
    return (
        f'<a href="#{esc(key)}" data-k="{esc(key)}"><span class="t">{esc(title)}</span>{dot}'
        f'<span class="n">{esc(number)}</span></a>'
    )


def inline_hash(text: str, tag: str) -> str:
    """The CSP source for the template's one inline <tag> block: its sha256, so no other inline
    script or style may run."""
    found = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    body = (found.group(1) if found else "").replace("$$", "$")  # as Template.substitute writes it
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


def page(sections: list[Section], needs: list[Action], kit: Path, engine: Path) -> str:
    links = [nav_link("attention", "Needs attention", len(needs), "warn" if needs else "")]
    links += [nav_link(s.key, s.title, s.count, level(s)) for s in sections]
    text = TEMPLATE.read_text()
    return Template(text).substitute(
        script_hash=inline_hash(text, "script"),
        style_hash=inline_hash(text, "style"),
        kit_name=esc(kit.name or str(kit)),
        generated=esc(dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        kit=path_html(kit),
        engine=path_html(engine),
        nav="".join(links),
        attention=attention(needs),
        sections="".join(section(s) for s in sections),
    )
