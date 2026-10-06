"""The dashboard's one renderer: typed section data (plain values) to escaped HTML, laid into the
dashboard.html template beside this file. Nothing else in the dashboard writes markup."""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Union
from urllib.parse import quote

TEMPLATE = Path(__file__).with_name("dashboard.html")
BADGE_KINDS = {"present": "ok", "missing": "bad", "clean": "ok", "unknown": "warn"}


@dataclass(frozen=True)
class Badge:
    text: str
    kind: str = ""  # ok, bad, warn, off or plain

    @classmethod
    def state(cls, state: str) -> Badge:
        return cls("not checked" if state == "unknown" else state, BADGE_KINDS.get(state, "warn"))


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


@dataclass
class Table:
    headers: tuple[str, ...]
    rows: list[tuple[Cell, ...]]
    title: str = ""
    empty: str = "none"
    note: str = ""


@dataclass(frozen=True)
class Para:
    items: tuple[Cell, ...]


@dataclass(frozen=True)
class Pre:
    text: str


Cell = Union[str, int, Path, Badge, Strong, Muted, Code, Command, Fold, Lines, Table, tuple]
Block = Union[Table, Para, Pre]


@dataclass(frozen=True)
class Tile:
    label: str
    value: str
    detail: str
    level: str = ""  # warn or bad when something needs a look


@dataclass(frozen=True)
class Action:
    label: str
    command: str = ""


@dataclass
class Section:
    key: str
    title: str
    sources: list[Path] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    tile: Tile | None = None
    actions: list[Action] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    attention: list[Action] = field(default_factory=list)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def copy_button(text: str) -> str:
    return f'<button class="cp" type="button" data-c="{esc(text)}">copy</button>'


def path_html(path: Path | str) -> str:
    """An editor link, the path (truncated by the page, whole in its title) and a copy button."""
    text = str(path)
    return (
        f'<span class="path"><a href="vscode://file{esc(quote(text))}" title="open in editor">open</a>'
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


def table(value: Table) -> str:
    title = f"<h3>{esc(value.title)}</h3>" if value.title else ""
    note = f"<p>{esc(value.note)}</p>" if value.note else ""
    if not value.rows:
        return f'{title}{note}<p class="muted">{esc(value.empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in value.headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell(c)}</td>" for c in row) + "</tr>" for row in value.rows
    )
    # A wide table scrolls inside its card, not the page.
    return (
        f'{title}{note}<div class="tw"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def block(value: Block) -> str:
    if isinstance(value, Table):
        return table(value)
    if isinstance(value, Pre):
        return f"<pre>{esc(value.text)}</pre>"
    return "<p>" + " ".join(cell(v) for v in value.items) + "</p>"


def summary(value: Section) -> str:
    return f"{value.tile.value} · {value.tile.detail}" if value.tile else ""


def level(value: Section) -> str:
    """bad when the section could not read something, else its tile's level."""
    return "bad" if value.alerts else value.tile.level if value.tile else ""


def section(value: Section) -> str:
    sources = "".join(path_html(p) for p in value.sources)
    alerts = "".join(f'<p class="alert">{esc(a)}</p>' for a in value.alerts)
    body = "".join(block(b) for b in value.blocks)
    return (
        f'<section id="{esc(value.key)}" class="card"><div class="head"><div><h2>{esc(value.title)}</h2>'
        f'<div class="sum">{esc(summary(value))}</div></div><div class="src">{sources}</div></div>'
        f"{alerts}{body}</section>"
    )


def attention(items: list[Action]) -> str:
    if not items:
        body = '<p class="muted">Nothing needs attention.</p>'
    else:
        body = (
            "<ul>"
            + "".join(
                f"<li>{esc(a.label)}{cell(Command(a.command)) if a.command else ''}</li>"
                for a in items
            )
            + "</ul>"
        )
    kind = "warn" if items else "ok"
    return (
        f'<section id="attention" class="card attention {kind}"><div class="head"><div>'
        f'<h2>Needs attention</h2><div class="sum">{len(items)} item(s)</div></div></div>{body}</section>'
    )


def tile(value: Tile) -> str:
    return (
        f'<div class="tile {esc(value.level)}"><div class="k">{esc(value.label)}</div>'
        f'<div class="v">{esc(value.value)}</div><div class="s">{esc(value.detail)}</div></div>'
    )


def nav_link(key: str, title: str, count: str, kind: str) -> str:
    dot = f'<i class="dot {esc(kind)}" title="{esc(kind)}"></i>' if kind else ""
    return (
        f'<a href="#{esc(key)}" data-k="{esc(key)}"><span class="t">{esc(title)}</span>{dot}'
        f'<span class="n">{esc(count)}</span></a>'
    )


def page(sections: list[Section], needs: list[Action], kit: Path, engine: Path) -> str:
    links = [nav_link("attention", "Needs attention", str(len(needs)), "warn" if needs else "")]
    links += [
        nav_link(s.key, s.title, s.tile.value.split(" ")[0] if s.tile else "", level(s))
        for s in sections
    ]
    return Template(TEMPLATE.read_text()).substitute(
        kit_name=esc(kit.name or str(kit)),
        generated=esc(dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        kit=path_html(kit),
        engine=path_html(engine),
        nav="".join(links),
        attention=attention(needs),
        tiles="".join(tile(s.tile) for s in sections if s.tile),
        sections="".join(section(s) for s in sections),
    )
