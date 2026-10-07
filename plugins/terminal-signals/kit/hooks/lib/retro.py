"""The weekly retro inbox (<work root>/retro/<YYYY-Www>.md): `agent-task retro` and the hooks append
one line per note; `session-review audit` marks the lines it processed."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path

from agent_task import atomic_write, today, work_root


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
                atomic_write(f, "\n".join(lines))
                n += hit
    return n
