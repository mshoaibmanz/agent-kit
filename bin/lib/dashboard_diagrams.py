"""The docs view's inline SVG figures, drawn from values the docs collector read; every word, label
included, comes from the caller. Every value is escaped here. The page's CSP allows no style
attribute and no url(), so shapes take their colours from classes in dashboard.html and arrowheads
are plain polygons, not markers. A figure is drawn at its viewBox width or wider, never narrower,
so its 13px text never shrinks: a narrow page scrolls it sideways."""

from __future__ import annotations

import math
from typing import NamedTuple

from dashboard_html import esc

WIDTH = 680
WIDE = 900
FONT = 13  # px, the figures' body text; widths below are estimates from it


class Layer(NamedTuple):
    title: str
    where: str
    holds: str


class HostBox(NamedTuple):
    name: str
    root: str
    files: tuple[str, ...]
    configured: bool


class Chip(NamedTuple):
    text: str
    blocking: bool = False


class Moment(NamedTuple):
    """One stop on the lifecycle line: the event, when it fires, and its hooks."""

    event: str
    when: str
    chips: tuple[Chip, ...]


def text_width(text: str, size: float = FONT) -> float:
    return len(text) * size * 0.56


def clip(text: str, chars: int) -> str:
    """text cut to chars, the ellipsis on the left (a path's end says the most)."""
    return text if len(text) <= chars else "..." + text[-(chars - 3) :]


def wrap(text: str, chars: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and len(lines[-1]) + 1 + len(word) <= chars:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines or [""]


def svg(label: str, height: float, body: str, width: int = WIDTH) -> str:
    wide = " dg-wide" if width > WIDTH else ""
    return (
        f'<svg class="dg{wide}" viewBox="0 0 {width} {math.ceil(height)}" role="img" '
        f'aria-label="{esc(label)}">{body}</svg>'
    )


def txt(x: float, y: float, text: str, cls: str = "dg-t", anchor: str = "start") -> str:
    end = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return f'<text x="{x:.0f}" y="{y:.0f}" class="{cls}"{end}>{esc(text)}</text>'


def box(x: float, y: float, w: float, h: float, cls: str = "dg-box") -> str:
    return f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="10" class="{cls}"/>'


def arrow(points: list[tuple[float, float]], cls: str = "dg-ln") -> str:
    """A polyline through points with a filled head at its last point."""
    path = " ".join(f"{x:.0f},{y:.0f}" for x, y in points)
    (x1, y1), (x2, y2) = points[-2], points[-1]
    angle = math.atan2(y2 - y1, x2 - x1)
    head = [
        (x2, y2),
        (x2 - 9 * math.cos(angle - 0.45), y2 - 9 * math.sin(angle - 0.45)),
        (x2 - 9 * math.cos(angle + 0.45), y2 - 9 * math.sin(angle + 0.45)),
    ]
    tip = " ".join(f"{x:.1f},{y:.1f}" for x, y in head)
    return f'<polyline points="{path}" class="{cls}"/><polygon points="{tip}" class="dg-head"/>'


def layers(
    stack: list[Layer], target: str, items: list[str], wins: str, label: str
) -> str:
    """The layers, lowest first in stack, drawn with the most personal on top; an arrow up the side
    labelled wins, and a box titled target listing items (what the layers render into)."""
    band, gap, top, left, right = 80, 12, 16, 52, 500
    out: list[str] = []
    for i, layer in enumerate(reversed(stack)):
        y = top + i * (band + gap)
        out.append(box(left, y, right - left, band, f"dg-band dg-band{len(stack) - 1 - i}"))
        out.append(txt(left + 16, y + 25, layer.title, "dg-h"))
        out.append(txt(left + 16, y + 46, clip(layer.where, 52), "dg-m"))
        holds = layer.holds if len(layer.holds) <= 60 else layer.holds[:57].rstrip(", ") + "..."
        out.append(txt(left + 16, y + 66, holds, "dg-s"))
    bottom = top + len(stack) * (band + gap) - gap
    if len(stack) > 1:
        out.append(arrow([(28, bottom - band / 2), (28, top + band / 2)]))
        out.append(
            f'<text x="16" y="{(top + bottom) / 2:.0f}" class="dg-s" text-anchor="middle" '
            f'transform="rotate(-90 16 {(top + bottom) / 2:.0f})">{esc(wins)}</text>'
        )
    middle = (top + bottom) / 2
    side_h = 44 + 22 * len(items)
    out.append(arrow([(right, middle), (right + 40, middle)]))
    out.append(box(right + 44, middle - side_h / 2, WIDTH - right - 60, side_h, "dg-box dg-accent"))
    out.append(txt(right + 60, middle - side_h / 2 + 26, target, "dg-h"))
    for i, item in enumerate(items):
        out.append(txt(right + 60, middle - side_h / 2 + 50 + 22 * i, clip(item, 14), "dg-m"))
    return svg(label, bottom + 16, "".join(out))


def wiring(
    title: str,
    sources: list[str],
    hub: tuple[str, ...],
    hosts: list[HostBox],
    reads: str,
    writes: str,
    off: str,
    label: str,
) -> str:
    """The sources (a box titled title) into the hub (its lines), and from it each host's files
    under its root; the two arrow legs are labelled reads and writes, an unconfigured host gets
    off after its name."""
    out: list[str] = []
    src_w, hub_x, hub_w, host_x = 230, 310, 180, 600
    src_h = 44 + 22 * len(sources)
    host_hs = [62 + 21 * len(h.files) for h in hosts]
    total = max(src_h, sum(host_hs) + 16 * (len(hosts) - 1))
    top = 16
    middle = top + total / 2
    out.append(box(16, middle - src_h / 2, src_w, src_h))
    out.append(txt(34, middle - src_h / 2 + 28, title, "dg-h"))
    for i, source in enumerate(sources):
        out.append(txt(34, middle - src_h / 2 + 52 + 22 * i, clip(source, 26), "dg-m"))
    out.append(arrow([(16 + src_w, middle), (hub_x - 4, middle)]))
    out.append(txt((16 + src_w + hub_x) / 2, middle - 8, reads, "dg-s", "middle"))
    hub_h = 40 + 22 * len(hub)
    out.append(box(hub_x, middle - hub_h / 2, hub_w, hub_h, "dg-box dg-accent"))
    for i, line in enumerate(hub):
        y = middle - hub_h / 2 + 30 + 22 * i
        out.append(txt(hub_x + hub_w / 2, y, line, "dg-h", "middle"))
    fork = hub_x + hub_w + 60
    out.append(txt(hub_x + hub_w + 30, middle - 8, writes, "dg-s", "middle"))
    y = top
    for host, h in zip(hosts, host_hs):
        cls = "dg-box" if host.configured else "dg-box dg-off"
        out.append(
            arrow([(hub_x + hub_w, middle), (fork, middle), (fork, y + h / 2), (host_x - 4, y + h / 2)])
        )
        out.append(box(host_x, y, WIDE - host_x - 16, h, cls))
        state = "" if host.configured else f" ({off})"
        out.append(txt(host_x + 18, y + 26, host.name + state, "dg-h"))
        out.append(txt(host_x + 18, y + 45, clip(host.root, 36), "dg-s"))
        for i, name in enumerate(host.files):
            out.append(txt(host_x + 30, y + 67 + 21 * i, clip(name, 32), "dg-m"))
        y += h + 16
    return svg(label, top + total + 16, "".join(out), WIDE)


def lifecycle(moments: list[Moment], blocking: str, advisory: str, empty: str, label: str) -> str:
    """A vertical timeline: each event with when it fires on the left, its hooks as chips; the
    legend names the two chip kinds, and an event with no hook gets one dashed chip, empty."""
    line_x, chip_x, row_gap = 210, 232, 18
    out: list[str] = []
    y = 50
    out.append(box(16, 10, 14, 14, "dg-chip dg-blk"))
    out.append(txt(38, 22, blocking, "dg-s"))
    out.append(box(232, 10, 14, 14, "dg-chip"))
    out.append(txt(254, 22, advisory, "dg-s"))
    rows: list[tuple[float, float, str]] = []
    for moment in moments:
        x, line = chip_x, 0
        for chip in moment.chips or (Chip(empty),):
            w = text_width(chip.text) + 20
            if x + w > WIDTH - 12 and x > chip_x:
                x, line = chip_x, line + 1
            cy = y + line * 28
            cls = "dg-chip dg-blk" if chip.blocking else "dg-chip"
            cls += " dg-none" if not moment.chips else ""
            out.append(
                f'<rect x="{x:.0f}" y="{cy - 15:.0f}" width="{w:.0f}" height="22" rx="11" class="{cls}"/>'
            )
            out.append(txt(x + 10, cy + 1, chip.text, "dg-c"))
            x += w + 6
        height = (line + 1) * 28
        rows.append((y, height, moment.event))
        out.append(txt(line_x - 16, y + 1, moment.event, "dg-h", "end"))
        for i, part in enumerate(wrap(moment.when, 24)[:2]):
            out.append(txt(line_x - 16, y + 19 + 16 * i, part, "dg-s", "end"))
        y += max(height, 48) + row_gap
    if rows:
        first, last = rows[0][0] - 4, rows[-1][0] - 4
        out.insert(
            4, f'<line x1="{line_x}" y1="{first:.0f}" x2="{line_x}" y2="{last:.0f}" class="dg-ln"/>'
        )
        for ry, _, _ in rows:
            out.append(f'<circle cx="{line_x}" cy="{ry - 4:.0f}" r="5" class="dg-dot"/>')
    return svg(label, y, "".join(out))


def review_loop(stages: list[tuple[str, str]], notes: list[str], loop: str, label: str) -> str:
    """Up to eight (title, detail) stages in a snake: the first four left to right, the rest right
    to left under them; the stage before last loops back to the one before it, labelled loop."""
    per_row, gap, top, h = 4, 28, 16, 112
    w = (WIDTH - 32 - (per_row - 1) * gap) / per_row
    out: list[str] = []
    spots: list[tuple[float, float]] = []
    for i, (title, detail) in enumerate(stages[: per_row * 2]):
        row, col = divmod(i, per_row)
        col = col if row == 0 else per_row - 1 - col
        x, y = 16 + col * (w + gap), top + row * (h + 60)
        spots.append((x, y))
        last = i == len(stages) - 1
        out.append(box(x, y, w, h, "dg-box dg-accent" if last else "dg-box"))
        out.append(txt(x + 12, y + 24, title, "dg-h"))
        for j, part in enumerate(wrap(detail, int(w / 7.4))[:4]):
            out.append(txt(x + 12, y + 44 + 16 * j, part, "dg-s"))
    for i in range(1, len(spots)):
        (ax, ay), (bx, by) = spots[i - 1], spots[i]
        if ay == by:
            forward = bx > ax
            sx, ex = (ax + w, bx) if forward else (ax, bx + w)
            out.append(arrow([(sx, ay + h / 2), (ex + (-4 if forward else 4), by + h / 2)]))
        else:
            out.append(arrow([(ax + w / 2, ay + h), (bx + w / 2, by - 4)]))
    if len(spots) >= 4 and spots[-2][1] == spots[-3][1] != spots[0][1]:
        (fx, fy), (lx, _) = spots[-3], spots[-2]
        under = fy + h + 18
        out.append(
            arrow(
                [
                    (lx + w / 2, fy + h),
                    (lx + w / 2, under),
                    (fx + w / 2, under),
                    (fx + w / 2, fy + h + 4),
                ]
            )
        )
        out.append(txt((lx + fx + w) / 2, under + 16, loop, "dg-s", "middle"))
    y = (spots[-1][1] if spots else top) + h + 56
    for note in notes:
        out.append(txt(16, y, note, "dg-s"))
        y += 20
    return svg(label, y, "".join(out))
