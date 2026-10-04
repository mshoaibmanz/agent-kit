#!/usr/bin/env python3
"""Manage the process-doc screenshot catalog.

The catalog is the reusable screenshot store at $PROCESS_DOC_CATALOG, else
<config dir>/local/process-doc/screenshot-catalog/ (company data, never shipped with the skill):
PNGs laid out as `<process>/<screen>.png` and indexed by `catalog.json`. This script is the
deterministic side of the backfill/reuse loop — it places files, dedupes by content hash, and keeps
the index honest. The judgement side (which process/app/screen/state a screenshot shows) is done by
the agent via vision and passed in as flags.

    uv run --with pillow scripts/catalog.py add --src in.png \
        --process returns_sorting --app scanner --screen lookup \
        --state "suggested bucket; the first box of a batch shows any location" \
        --frame device --drive-id <drive file id> --source captured
    uv run scripts/catalog.py find --process returns_sorting --screen lookup
    uv run scripts/catalog.py list
    uv run scripts/catalog.py validate
    uv run scripts/catalog.py reindex

Subcommands:
  add       register a PNG: hash-dedupe, copy to <process>/<screen>.png, upsert its entry
  find      print entries matching any of --process/--app/--screen/--state (substring on state)
  list      print the whole index (optionally filtered by --process)
  validate  every `ready` entry's file must exist and match its recorded hash; nonzero exit on drift
  reindex   re-hash every on-disk file and repair `sha256`/`status`/`file` fields
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("catalog")

_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
CATALOG_DIR = Path(
    os.environ.get("PROCESS_DOC_CATALOG") or _CONFIG_DIR / "local" / "process-doc" / "screenshot-catalog"
)
CATALOG_JSON = CATALOG_DIR / "catalog.json"
SCHEMA_VERSION = 2
VALID_FRAMES = ("device", "card")
VALID_STATUS = ("ready", "pending", "adapted")


def _load() -> dict:
    if not CATALOG_JSON.exists():
        return {"version": SCHEMA_VERSION, "screenshots": []}
    data = json.loads(CATALOG_JSON.read_text())
    data.setdefault("screenshots", [])
    return data


def _save(data: dict) -> None:
    data["version"] = SCHEMA_VERSION
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_JSON.write_text(json.dumps(data, indent=2) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(text: str) -> str:
    """Lowercase; keep alnum/underscore/hyphen (so snake_case processes survive), other → hyphen."""
    return "".join(c if (c.isalnum() or c in "_-") else "-" for c in text.lower()).strip("-")


def _dest_rel(process: str, screen: str, taken: set[str]) -> str:
    """Pick `<process>/<screen>.png`, suffixing -2, -3, ... if that relative path is already used."""
    base = f"{process}/{screen}"
    rel = f"{base}.png"
    n = 2
    while rel in taken:
        rel = f"{base}-{n}.png"
        n += 1
    return rel


def cmd_add(args: argparse.Namespace) -> int:
    src = Path(args.src)
    if not src.exists():
        raise SystemExit(f"source not found: {src}")
    if args.frame not in VALID_FRAMES:
        raise SystemExit(f"--frame must be one of {VALID_FRAMES}")

    data = _load()
    shots = data["screenshots"]
    digest = _sha256(src)

    existing = next((s for s in shots if s.get("sha256") == digest), None)
    if existing:
        log.info("duplicate content — already in catalog as %s (%s)", existing["id"], existing.get("file"))
        return 0

    process, screen = _slug(args.process), _slug(args.screen)
    taken = {s["file"] for s in shots if s.get("file")}
    rel = _dest_rel(process, screen, taken)
    dest = CATALOG_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)

    entry_id = args.id or _dest_rel(process, screen, taken).rsplit(".", 1)[0].replace("/", "-")
    annotations = json.loads(args.annotations) if args.annotations else []
    entry = {
        "id": entry_id,
        "file": rel,
        "status": "adapted" if (args.source or "").startswith("adapted") else "ready",
        "process": process,
        "app": args.app,
        "screen": screen,
        "state": args.state,
        "frame": args.frame,
        "source": args.source or "captured",
        "drive_id": args.drive_id,
        "sha256": digest,
        "annotations": annotations,
    }
    by_id = {s["id"]: i for i, s in enumerate(shots)}
    if entry_id in by_id:
        shots[by_id[entry_id]] = entry
    else:
        shots.append(entry)
    _save(data)
    log.info("added %s -> %s", entry_id, rel)
    return 0


def _matches(s: dict, args: argparse.Namespace) -> bool:
    if args.process and s.get("process") != _slug(args.process):
        return False
    if args.app and s.get("app") != args.app:
        return False
    if args.screen and s.get("screen") != _slug(args.screen):
        return False
    if args.state and args.state.lower() not in (s.get("state") or "").lower():
        return False
    return True


def cmd_find(args: argparse.Namespace) -> int:
    shots = [s for s in _load()["screenshots"] if _matches(s, args)]
    print(json.dumps(shots, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    shots = _load()["screenshots"]
    if args.process:
        shots = [s for s in shots if s.get("process") == _slug(args.process)]
    for s in shots:
        print(f"{s['status']:8} {s.get('file') or '(pending)':40} {s['id']}")
    print(f"\n{len(shots)} entries")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    drift = 0
    for s in _load()["screenshots"]:
        if s.get("status") == "pending":
            continue
        rel = s.get("file")
        if not rel:
            log.error("%s: status %s but no file", s["id"], s["status"])
            drift += 1
            continue
        path = CATALOG_DIR / rel
        if not path.exists():
            log.error("%s: file missing on disk: %s", s["id"], rel)
            drift += 1
        elif s.get("sha256") and _sha256(path) != s["sha256"]:
            log.error("%s: sha256 mismatch for %s", s["id"], rel)
            drift += 1
    if drift:
        log.error("%d catalog problem(s)", drift)
        return 1
    log.info("catalog OK")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    data = _load()
    for s in data["screenshots"]:
        rel = s.get("file")
        if not rel:
            s["status"] = "pending"
            continue
        path = CATALOG_DIR / rel
        if path.exists():
            s["sha256"] = _sha256(path)
            if s.get("status") == "pending":
                s["status"] = "ready"
        else:
            log.warning("%s: file missing, marking pending: %s", s["id"], rel)
            s["status"] = "pending"
    _save(data)
    log.info("reindexed %d entries", len(data["screenshots"]))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage the process-doc screenshot catalog.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="register a PNG into the catalog")
    a.add_argument("--src", required=True, help="source PNG to ingest")
    a.add_argument("--process", required=True)
    a.add_argument("--app", required=True, help="the app or portal name")
    a.add_argument("--screen", required=True, help="lookup | confirm | manifest | ...")
    a.add_argument("--state", required=True, help="what's on screen (data/condition)")
    a.add_argument("--frame", required=True, help="device | card")
    a.add_argument("--source", help="captured | 'adapted from <process> <screen>'")
    a.add_argument("--drive-id", dest="drive_id", help="originating Google Drive file id")
    a.add_argument("--annotations", help="JSON list of {pin,label} for card frames")
    a.add_argument("--id", help="override the generated entry id")
    a.set_defaults(func=cmd_add)

    for name, fn in (("find", cmd_find), ("list", cmd_list)):
        p = sub.add_parser(name)
        p.add_argument("--process")
        p.add_argument("--app")
        p.add_argument("--screen")
        p.add_argument("--state")
        p.set_defaults(func=fn)

    sub.add_parser("validate").set_defaults(func=cmd_validate)
    sub.add_parser("reindex").set_defaults(func=cmd_reindex)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
