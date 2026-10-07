#!/usr/bin/env python3
"""Process-held report lock and recoverable inode-owned Git locks."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4


def manifest(owner: Path) -> dict[str, Any]:
    if owner.is_symlink() or not owner.is_dir():
        raise ValueError("Invalid lock owner directory")
    record_path = owner / "manifest.json"
    if record_path.is_symlink() or not record_path.is_file():
        raise ValueError("Invalid ownership manifest")
    data = json.loads(record_path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("version") != 2
        or not isinstance(data.get("pid"), int)
        or data["pid"] <= 0
        or not isinstance(data.get("locks"), list)
    ):
        raise ValueError("Invalid lock ownership record")
    identity = owner / "identity"
    if identity.is_symlink() or not identity.is_file():
        raise ValueError("Invalid lock identity")
    if identity.read_text() != data.get("token"):
        raise ValueError("Lock identity does not match")
    for record in data["locks"]:
        lock, identity = Path(record["path"]), Path(record["identity"])
        if (
            not lock.is_absolute()
            or not str(lock).endswith(".lock")
            or identity.parent != lock.parent
            or not identity.name.startswith(".claude-gc-identity-")
            or not isinstance(record["dev"], int)
            or not isinstance(record["ino"], int)
        ):
            raise ValueError("Invalid Git lock identity record")
    artifacts = data["artifacts"]
    if (
        not Path(artifacts["path"]).is_absolute()
        or not Path(artifacts["path"]).name.startswith(".claude-gc-artifacts-")
        or not isinstance(artifacts["dev"], int)
        or not isinstance(artifacts["ino"], int)
    ):
        raise ValueError("Invalid artifact ownership record")
    return data


def write_manifest(owner: Path, data: dict[str, Any]) -> None:
    staged = owner / "manifest.new"
    staged.write_text(json.dumps(data))
    os.replace(staged, owner / "manifest.json")


def regular_fd(path: Path, *, create: bool = False) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Lock is not regular")
    return fd


def matches(path: Path, record: dict[str, Any], *, directory: bool = False) -> bool:
    try:
        actual = path.lstat()
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        return kind(actual.st_mode) and (actual.st_dev, actual.st_ino) == (
            record["dev"],
            record["ino"],
        )
    except FileNotFoundError:
        return False


def clear_git(owner: Path, data: dict[str, Any]) -> None:
    for record in data["locks"]:
        identity, lock = Path(record["identity"]), Path(record["path"])
        if matches(identity, record) and identity.read_text() == data["token"]:
            if matches(lock, record):
                lock.unlink()
            identity.unlink()
    data["locks"] = []
    write_manifest(owner, data)


def cleanup(owner: Path, data: dict[str, Any]) -> None:
    clear_git(owner, data)
    record = data["artifacts"]
    artifacts = Path(record["path"])
    marker = artifacts / "identity"
    if (
        matches(artifacts, record, directory=True)
        and not marker.is_symlink()
        and marker.read_text() == data["token"]
    ):
        shutil.rmtree(artifacts)
    shutil.rmtree(owner)


def owner_root() -> Path:
    root = Path.home() / ".claude/state/gc-owners"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise ValueError("Invalid lock registry")
    return root


def recover(root: Path) -> None:
    # Literal enumeration ignores report filename patterns, directories and base OIDs.
    for owner in root.iterdir():
        if not owner.name.startswith("owner-") or owner.is_symlink():
            continue
        fd = None
        try:
            fd = regular_fd(owner / "active.lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            actual = os.fstat(fd)
            if not matches(
                owner / "active.lock", {"dev": actual.st_dev, "ino": actual.st_ino}
            ):
                continue
            cleanup(owner, manifest(owner))
        except (OSError, ValueError, TypeError, KeyError):
            # A held flock proves an owner or inherited child remains alive.
            # Unknown, replaced or malformed ownership records remain untouched.
            continue
        finally:
            if fd is not None:
                os.close(fd)


def hold(report: Path, script: str, args: list[str]) -> None:
    report = report.absolute()
    lock = Path(str(report) + ".lock")
    try:
        fd = regular_fd(lock, create=True)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ValueError):
        sys.stderr.write(
            f"claude-gc: report is in use or has an unowned lock: {lock}\n"
        )
        raise SystemExit(1) from None
    root = owner_root()
    recover(root)
    owner = Path(tempfile.mkdtemp(prefix="owner-", dir=root))
    active_fd = regular_fd(owner / "active.lock", create=True)
    fcntl.flock(active_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    artifacts = Path(
        tempfile.mkdtemp(prefix=".claude-gc-artifacts-", dir=report.parent)
    )
    token = f"claude-gc:{os.getpid()}:{uuid4().hex}\n"
    (owner / "identity").write_text(token)
    (artifacts / "identity").write_text(token)
    info = artifacts.stat()
    write_manifest(
        owner,
        {
            "version": 2,
            "pid": os.getpid(),
            "token": token,
            "locks": [],
            "artifacts": {
                "path": str(artifacts),
                "dev": info.st_dev,
                "ino": info.st_ino,
            },
        },
    )
    os.set_inheritable(fd, True)
    os.set_inheritable(active_fd, True)
    env = dict(
        os.environ,
        _CLAUDE_GC_GUARD=str(os.getpid()),
        _CLAUDE_GC_OWNER=str(owner),
        _CLAUDE_GC_ARTIFACTS=str(artifacts),
    )
    os.execvpe("bash", ["bash", script, *args], env)


def main() -> None:
    action, *args = sys.argv[1:]
    if action == "hold":
        hold(Path(args[0]), args[1], args[2:])
        return
    owner = Path(args[0])
    data = manifest(owner)
    if data["pid"] != os.getppid():
        raise ValueError("Lock owner is not the invoking process")
    if action == "git":
        lock = Path(args[1]).absolute()
        if not str(lock).endswith(".lock"):
            raise ValueError("Invalid Git lock path")
        lock.parent.mkdir(parents=True, exist_ok=True)
        # Keep identities on the Git lock's filesystem for atomic hardlinks.
        fd, name = tempfile.mkstemp(prefix=".claude-gc-identity-", dir=lock.parent)
        with os.fdopen(fd, "w") as identity_file:
            identity_file.write(data["token"])
        identity = Path(name)
        info = identity.stat()
        data["locks"].append(
            {
                "path": str(lock),
                "identity": str(identity),
                "dev": info.st_dev,
                "ino": info.st_ino,
            }
        )
        write_manifest(owner, data)
        os.link(identity, lock)
    elif action == "clear":
        clear_git(owner, data)
    elif action == "cleanup":
        cleanup(owner, data)
    else:
        raise ValueError("Invalid lock action")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        sys.stderr.write("claude-gc: owned lock operation refused\n")
        raise SystemExit(1) from None
