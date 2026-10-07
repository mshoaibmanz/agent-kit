"""The How the kit works view's inline SVG figures, drawn from values the docs collector read. Every
value is escaped here. The page's CSP allows no style attribute and no url(), so shapes take their
colours from classes in dashboard.html and arrowheads are plain polygons, not markers."""

from __future__ import annotations

import math
from typing import NamedTuple

from dashboard_html import esc

WIDTH = 680
FONT = 12.5  # px, the figures' body text; widths below are estimates from it
MONO = 7.4  # px per character of 12px monospace


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


def svg(label: str, height: float, body: str) -> str:
    return (
        f'<svg class="dg" viewBox="0 0 {WIDTH} {math.ceil(height)}" role="img" '
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


def layers(stack: list[Layer], hosts: list[str]) -> str:
    """The layers, lowest first in stack, drawn with the most personal on top, and the hosts they
    render into."""
    band, gap, top, left, right = 76, 12, 16, 48, 480
    out: list[str] = []
    for i, layer in enumerate(reversed(stack)):
        y = top + i * (band + gap)
        out.append(box(left, y, right - left, band, f"dg-band dg-band{len(stack) - 1 - i}"))
        out.append(txt(left + 16, y + 24, layer.title, "dg-h"))
        out.append(txt(left + 16, y + 44, clip(layer.where, 54), "dg-m"))
        holds = layer.holds if len(layer.holds) <= 66 else layer.holds[:63].rstrip(", ") + "..."
        out.append(txt(left + 16, y + 63, holds, "dg-s"))
    bottom = top + len(stack) * (band + gap) - gap
    if len(stack) > 1:
        out.append(arrow([(24, bottom - band / 2), (24, top + band / 2)]))
        out.append(
            f'<text x="14" y="{(top + bottom) / 2:.0f}" class="dg-s" text-anchor="middle" '
            f'transform="rotate(-90 14 {(top + bottom) / 2:.0f})">a higher layer wins</text>'
        )
    middle = (top + bottom) / 2
    host_h = 40 + 20 * len(hosts)
    out.append(arrow([(right, middle), (right + 40, middle)]))
    out.append(box(right + 44, middle - host_h / 2, WIDTH - right - 60, host_h, "dg-box dg-accent"))
    out.append(txt(right + 60, middle - host_h / 2 + 24, "Rendered into", "dg-h"))
    for i, host in enumerate(hosts):
        out.append(txt(right + 60, middle - host_h / 2 + 46 + 20 * i, clip(host, 18), "dg-m"))
    return svg("The kit's layers and the hosts they render into", bottom + 16, "".join(out))


def wiring(sources: list[str], hosts: list[HostBox]) -> str:
    """Kit sources into the render step, and from it each host's files under its root."""
    out: list[str] = []
    src_h = 36 + 19 * len(sources)
    host_hs = [54 + 18 * len(h.files) for h in hosts]
    total = max(src_h, sum(host_hs) + 14 * (len(hosts) - 1))
    top = 16
    middle = top + total / 2
    out.append(box(16, middle - src_h / 2, 196, src_h))
    out.append(txt(32, middle - src_h / 2 + 24, "Kit sources", "dg-h"))
    for i, source in enumerate(sources):
        out.append(txt(32, middle - src_h / 2 + 46 + 19 * i, clip(source, 22), "dg-m"))
    out.append(arrow([(212, middle), (248, middle)]))
    out.append(box(252, middle - 34, 150, 68, "dg-box dg-accent"))
    out.append(txt(327, middle - 6, "agent-setup", "dg-h", "middle"))
    out.append(txt(327, middle + 14, "agent-kit render", "dg-h", "middle"))
    y = top
    for host, h in zip(hosts, host_hs):
        cls = "dg-box" if host.configured else "dg-box dg-off"
        out.append(arrow([(402, middle), (418, middle), (418, y + h / 2), (442, y + h / 2)]))
        out.append(box(446, y, WIDTH - 462, h, cls))
        state = "" if host.configured else " (not configured)"
        out.append(txt(462, y + 22, host.name + state, "dg-h"))
        out.append(txt(462, y + 39, clip(host.root, 28), "dg-s"))
        for i, name in enumerate(host.files):
            out.append(txt(474, y + 58 + 18 * i, clip(name, 26), "dg-m"))
        y += h + 14
    return svg("How the kit's sources reach each host", top + total + 16, "".join(out))


def lifecycle(moments: list[Moment]) -> str:
    """A vertical timeline: each event with when it fires on the left, its hooks as chips."""
    line_x, chip_x, row_gap = 196, 216, 18
    out: list[str] = []
    y = 46
    out.append(box(16, 10, 12, 12, "dg-chip dg-blk"))
    out.append(txt(34, 21, "can refuse (blocking)", "dg-s"))
    out.append(box(196, 10, 12, 12, "dg-chip"))
    out.append(txt(214, 21, "advisory", "dg-s"))
    rows: list[tuple[float, float, str]] = []
    for moment in moments:
        x, line = chip_x, 0
        for chip in moment.chips or (Chip("no hook"),):
            w = text_width(chip.text, 12) + 18
            if x + w > WIDTH - 12 and x > chip_x:
                x, line = chip_x, line + 1
            cy = y + line * 26
            cls = "dg-chip dg-blk" if chip.blocking else "dg-chip"
            cls += " dg-none" if not moment.chips else ""
            out.append(
                f'<rect x="{x:.0f}" y="{cy - 14:.0f}" width="{w:.0f}" height="20" rx="10" class="{cls}"/>'
            )
            out.append(txt(x + 9, cy + 0.5, chip.text, "dg-c"))
            x += w + 6
        height = (line + 1) * 26
        rows.append((y, height, moment.event))
        out.append(txt(line_x - 16, y + 1, moment.event, "dg-h", "end"))
        for i, part in enumerate(wrap(moment.when, 24)[:2]):
            out.append(txt(line_x - 16, y + 18 + 15 * i, part, "dg-s", "end"))
        y += max(height, 44) + row_gap
    if rows:
        first, last = rows[0][0] - 4, rows[-1][0] - 4
        out.insert(
            4, f'<line x1="{line_x}" y1="{first:.0f}" x2="{line_x}" y2="{last:.0f}" class="dg-ln"/>'
        )
        for ry, _, _ in rows:
            out.append(f'<circle cx="{line_x}" cy="{ry - 4:.0f}" r="5" class="dg-dot"/>')
    return svg("Which hooks fire at each point of a session", y, "".join(out))


def review_loop(stages: list[tuple[str, str]], notes: list[str]) -> str:
    """Up to eight (title, detail) stages in a snake: the first four left to right, the rest right
    to left under them; the stage before last loops back to the one before it until clean."""
    per_row, gap, top, h = 4, 28, 16, 84
    w = (WIDTH - 32 - (per_row - 1) * gap) / per_row
    out: list[str] = []
    spots: list[tuple[float, float]] = []
    for i, (title, detail) in enumerate(stages[: per_row * 2]):
        row, col = divmod(i, per_row)
        col = col if row == 0 else per_row - 1 - col
        x, y = 16 + col * (w + gap), top + row * (h + 56)
        spots.append((x, y))
        last = i == len(stages) - 1
        out.append(box(x, y, w, h, "dg-box dg-accent" if last else "dg-box"))
        out.append(txt(x + 12, y + 22, title, "dg-h"))
        for j, part in enumerate(wrap(detail, int(w / 7.0))[:3]):
            out.append(txt(x + 12, y + 42 + 16 * j, part, "dg-s"))
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
        out.append(
            txt((lx + fx + w) / 2, under + 15, "until a round finds nothing new", "dg-s", "middle")
        )
    y = (spots[-1][1] if spots else top) + h + 52
    for note in notes:
        out.append(txt(16, y, note, "dg-s"))
        y += 18
    return svg("The verification and review loop before a push and after it", y, "".join(out))
