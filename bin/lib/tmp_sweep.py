"""claude-gc's work-root tmp/ sweep, the kit's only work-root cleaner (README "Inspect and roll back"):
`scan --report PATH` writes the report, `apply --report PATH --sha256 HASH` applies exactly that report.
Only items/*/tmp/ of a done item (agent_task.DONE_STATUSES), after TMP_SWEEP_DAYS; generated bulk and
files over 1 MB are deleted, small source and data files move to the item's out/salvage/, anything else
stays. A checkout holding a local-only commit or tag, a stash or uncommitted work is kept wherever it sits.
Checkouts and files over 5 MB elsewhere in the item are listed as report lines, never applied."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks/lib"))
from agent_task import DONE_STATUSES, generated, read_task  # noqa: E402
from checkout import git  # noqa: E402
from kit_env import kit_env, work_root as overlay_work_root  # noqa: E402

DEFAULT_DAYS = 14
MAX_SALVAGE = 1024 * 1024
BIG_FILE = 5 * 1024 * 1024
SALVAGE_SUFFIXES = frozenset((".py", ".sh", ".sql", ".ipynb", ".md", ".csv", ".json", ".txt"))
REPORT_HEADER = ("# tmp-sweep v1: action, path, kind, bytes, fingerprint. Only delete and salvage lines are applied; "
                 "report lines are for you to judge.")


class Entry(NamedTuple):
    action: str  # delete | salvage | keep | report
    path: Path
    kind: str
    size: int
    fingerprint: str


def work_root() -> Path:
    return Path(overlay_work_root())


def sweep_days() -> int:
    """TMP_SWEEP_DAYS from the overlay, else DEFAULT_DAYS."""
    try:
        raw = kit_env().get("TMP_SWEEP_DAYS", "")
    except OSError:
        raw = ""
    return int(raw) if raw.strip().isdigit() else DEFAULT_DAYS


def status_and_closed(item: Path) -> tuple[str, str]:
    """(status, the date it was closed or harvested), read as agent-task reads task.json."""
    data = read_task(item)
    return str(data.get("status", "open")), str(data.get("closed") or data.get("harvested") or "")


def is_done(item: Path) -> bool:
    return status_and_closed(item)[0] in DONE_STATUSES


def newest_mtime(path: Path) -> int:
    """The newest mtime (ns) of path and everything under it, links not followed. A checkout's .git is
    left out: git's own reads rewrite it (`status` refreshes the index, a detached auto-maintenance
    takes objects/maintenance.lock), and git_settled judges what it holds."""
    newest = path.lstat().st_mtime_ns
    if path.is_dir() and not path.is_symlink():
        for top, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in dirs + [f for f in files if f != ".git"]:
                try:
                    newest = max(newest, os.lstat(os.path.join(top, name)).st_mtime_ns)
                except OSError:
                    continue
    return newest


def tree_size(path: Path) -> int:
    if not path.is_dir() or path.is_symlink():
        return path.lstat().st_size
    total = 0
    for top, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(top, name)).st_size
            except OSError:
                continue
    return total


def git_out(folder: Path, *args: str) -> str | None:
    """git's stdout, or None when it fails or times out. Never prompts: a remote that wants a password
    or an ssh passphrase fails instead."""
    ssh = os.environ.get("GIT_SSH_COMMAND", "ssh") + " -o BatchMode=yes"
    try:
        proc = git(folder, *args, timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": ssh})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def tags_pushed(folder: Path) -> bool:
    """Every local tag exists, with the same object, on some remote. Git keeps no remote-tracking tags,
    so this asks each remote; a remote it cannot reach proves nothing."""
    local = git_out(folder, "for-each-ref", "--format=%(objectname) %(refname)", "refs/tags")
    if local is None:
        return False
    wanted = set(local.split("\n")) - {""}
    if not wanted:
        return True
    remotes = git_out(folder, "remote")
    for remote in (remotes or "").split():
        listed = git_out(folder, "ls-remote", "--tags", "--refs", remote)
        if listed is not None:
            wanted -= {line.replace("\t", " ") for line in listed.split("\n")}
        if not wanted:
            return True
    return False


def git_settled(folder: Path) -> bool:
    """Nothing uncommitted and nothing that only this checkout holds. `--all` walks HEAD (a detached
    commit), tags and refs/stash too, which `--branches` misses; a tag on a pushed commit is checked
    against the remotes."""
    status = git_out(folder, "status", "--porcelain")
    if status is None or status.strip():
        return False
    local = git_out(folder, "rev-list", "--all", "--not", "--remotes", "-n", "1")
    return local is not None and not local.strip() and tags_pushed(folder)


def unsettled_checkouts(folder: Path) -> list[Path]:
    """Every checkout at or under folder (links not followed) that git_settled refuses."""
    found = []
    for top, dirs, files in os.walk(folder):
        if ".git" in dirs or ".git" in files:
            if not git_settled(Path(top)):
                found.append(Path(top))
        dirs[:] = [d for d in dirs if d != ".git" and not os.path.islink(os.path.join(top, d))]
    return found


def bulk_kind(folder: Path) -> str | None:
    """The kind of bulk folder may go whole; None to descend into it. A checkout with unsettled work
    (its own or a nested one) is kept whole; any other bulk folder holding one is descended into, so
    the sweep keeps that checkout and judges the rest piece by piece."""
    kind = generated(folder)
    if kind is None or not unsettled_checkouts(folder):
        return kind
    return "git checkout with unpushed work" if kind == "git checkout" else None


def classify(tmp: Path) -> Iterator[Entry]:
    for top, dirs, files in os.walk(tmp):
        here = Path(top)
        for name in sorted(dirs):
            folder = here / name
            if folder.is_symlink():
                continue
            kind = bulk_kind(folder)
            if kind is None:
                continue
            dirs.remove(name)
            action = "keep" if kind.endswith("unpushed work") else "delete"
            yield Entry(action, folder, kind, tree_size(folder), str(newest_mtime(folder)))
        dirs.sort()
        for name in sorted(files):
            path = here / name
            if path.is_symlink():
                continue
            stat = path.lstat()
            fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"
            if stat.st_size > MAX_SALVAGE:
                yield Entry("delete", path, "file over 1 MB", stat.st_size, fingerprint)
            elif path.suffix.lower() in SALVAGE_SUFFIXES:
                yield Entry("salvage", path, "small source or data", stat.st_size, fingerprint)


def item_folders(root: Path) -> Iterator[tuple[Path, Path | None]]:
    """(item, item/tmp, or None without a real tmp/ folder) for every real item folder."""
    for item in sorted(root.glob("projects/*/items/*")):
        if item.is_dir() and not item.is_symlink():
            tmp = item / "tmp"
            yield item, (tmp if tmp.is_dir() and not tmp.is_symlink() else None)


def ready(item: Path, tmp: Path | None, days: int, now: float) -> str | None:
    """None when the item may be swept, else why not."""
    status, closed = status_and_closed(item)
    if status not in DONE_STATUSES:
        return f"status {status}"
    cutoff = now - days * 86400
    if closed:
        try:
            if dt.datetime.strptime(closed[:10], "%Y-%m-%d").timestamp() > cutoff:
                return f"{status} {closed[:10]}, under {days} days ago"
        except ValueError:
            pass
    if tmp is not None and newest_mtime(tmp) / 1e9 > cutoff:
        return f"tmp/ changed in the last {days} days"
    return None


def report_only(item: Path) -> Iterator[Entry]:
    """Checkouts and files over 5 MB outside tmp/ (clones and dumps under out/): listed for the user,
    never applied."""
    for top, dirs, files in os.walk(item):
        here = Path(top)
        if here == item:
            dirs[:] = [d for d in dirs if d != "tmp"]
        elif ".git" in dirs or ".git" in files:
            yield Entry("report", here, "checkout outside tmp/", tree_size(here), str(newest_mtime(here)))
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        for name in sorted(files):
            path = here / name
            stat = path.lstat()
            if not path.is_symlink() and stat.st_size > BIG_FILE:
                yield Entry("report", path, "file over 5 MB outside tmp/", stat.st_size, f"{stat.st_size}:{stat.st_mtime_ns}")


def scan(root: Path, days: int, now: float | None = None) -> tuple[list[Entry], list[str]]:
    now = time.time() if now is None else now
    entries, skipped = [], []
    for item, tmp in item_folders(root):
        reason = ready(item, tmp, days, now)
        if reason:
            if tmp is not None:
                skipped.append(f"{item.relative_to(root)}: {reason}")
            continue
        if tmp is not None:
            entries.extend(classify(tmp))
        entries.extend(report_only(item))
    return entries, skipped


def report_text(root: Path, days: int, entries: list[Entry], skipped: list[str], apply_hint: str) -> str:
    lines = [REPORT_HEADER, f"# root {root}; grace {days} days. Apply with: {apply_hint}"]
    lines += [f"# not swept: {line}" for line in skipped]
    for e in entries:
        if any(c in str(e.path) for c in "\t\n\r"):
            continue
        lines.append("\t".join((e.action, str(e.path), e.kind, str(e.size), e.fingerprint)))
    return "\n".join(lines) + "\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_report(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-sweep.")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return sha256(path)


def owning_item(root: Path, path: Path) -> tuple[Path, Path] | None:
    """(item, item/tmp) when path lies strictly inside a real items/*/tmp/ folder of root, every
    component a real folder (no link anywhere on the way)."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 6 or parts[0] != "projects" or parts[2] != "items" or parts[4] != "tmp" or ".." in parts:
        return None
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return None
    item = root.joinpath(*parts[:4])
    return item, item / "tmp"


def free_destination(dest: Path) -> Path:
    if not dest.exists() and not dest.is_symlink():
        return dest
    for n in range(1, 1000):
        candidate = dest.with_name(f"{dest.name}.salvaged-{n}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise OSError(f"no free salvage name for {dest}")


def apply(root: Path, report: Path, expected: str, log=print) -> int:
    if not report.is_file() or report.is_symlink():
        log(f"no regular report at {report}; run claude-gc first")
        return 1
    snapshot = report.read_bytes()
    if hashlib.sha256(snapshot).hexdigest() != expected:
        log(f"report digest changed; nothing swept: {report}")
        return 1
    days_checked: dict[Path, bool] = {}
    counts = {"deleted": 0, "salvaged": 0, "skipped": 0}
    for line in snapshot.decode(errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 5 or fields[0] not in ("delete", "salvage"):
            continue
        action, raw, kind, _size, fingerprint = fields
        path = Path(raw)
        owner = owning_item(root, path)
        if owner is None:
            log(f"skip (outside an item's tmp/): {path}")
            counts["skipped"] += 1
            continue
        item, tmp = owner
        if item not in days_checked:
            days_checked[item] = is_done(item)
        if not days_checked[item]:
            log(f"skip (item reopened): {path}")
            counts["skipped"] += 1
            continue
        if path.is_symlink() or not path.exists():
            log(f"skip (gone or now a link): {path}")
            counts["skipped"] += 1
            continue
        if action == "delete" and path.is_dir() and unsettled_checkouts(path):
            log(f"skip (holds a checkout with unpushed work or a stash): {path}")
            counts["skipped"] += 1
            continue
        if path.is_dir():
            current = str(newest_mtime(path))
        else:
            stat = path.lstat()
            current = f"{stat.st_size}:{stat.st_mtime_ns}"
        if current != fingerprint or (action == "salvage" and path.is_dir()):
            log(f"skip (changed since the report): {path}")
            counts["skipped"] += 1
            continue
        if action == "delete":
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            log(f"deleted ({kind}): {path}")
            counts["deleted"] += 1
        elif (item / "out").is_symlink() or (item / "out" / "salvage").is_symlink():
            log(f"skip (out/ or out/salvage is a link): {path}")
            counts["skipped"] += 1
        else:
            dest = free_destination(item / "out" / "salvage" / path.relative_to(tmp))
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)
            log(f"salvaged: {path} -> {dest}")
            counts["salvaged"] += 1
    log(f"tmp sweep: {counts['deleted']} deleted, {counts['salvaged']} salvaged, {counts['skipped']} skipped")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmp_sweep.py")
    ap.add_argument("mode", choices=("scan", "apply"))
    ap.add_argument("--report", required=True)
    ap.add_argument("--root")
    ap.add_argument("--days", type=int)
    ap.add_argument("--sha256")
    ap.add_argument("--apply-hint", default="claude-gc --sweep-tmp --report-sha256 <sha256>")
    a = ap.parse_args(argv)
    root = Path(a.root).expanduser() if a.root else work_root()
    report = Path(a.report)
    if a.mode == "apply":
        if not a.sha256 or len(a.sha256) != 64:
            print("apply needs --sha256 of the reviewed report", file=sys.stderr)
            return 2
        return apply(root.resolve() if root.exists() else root, report, a.sha256)
    if not root.is_dir():
        print(f"tmp sweep: no work root at {root}; nothing to scan")
        return 0
    days = a.days if a.days is not None else sweep_days()
    entries, skipped = scan(root.resolve(), days)
    digest = write_report(report, report_text(root.resolve(), days, entries, skipped, a.apply_hint))
    counts = {action: sum(1 for e in entries if e.action == action) for action in ("delete", "salvage", "keep", "report")}
    print(f"tmp sweep report: {counts['delete']} to delete, {counts['salvage']} to salvage, {counts['keep']} kept, "
          f"{counts['report']} outside tmp/ for you to judge "
          f"(report: {report}; sha256 {digest}; apply with: {a.apply_hint.replace('<sha256>', digest)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
