"""Aggregate native Codex rollouts without retaining prompts or tool payloads."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def read_session(path: Path, since: datetime, until: datetime) -> dict[str, Any] | None:
    session = path.stem
    model = None
    first = last = None
    before: dict[str, int] = {}
    current: dict[str, int] = {}
    seen = False
    with path.open() as handle:
        for line in handle:
            try:
                record = json.loads(line)
                at = timestamp(record["timestamp"])
                payload = record.get("payload") or {}
            except (ValueError, KeyError, TypeError):
                continue
            kind = record.get("type")
            if kind == "session_meta":
                session = payload.get("id", session)
            if kind == "turn_context" and at < until:
                model = payload.get("model", model)
            usage = (
                (payload.get("info") or {}).get("total_token_usage")
                if kind == "event_msg" and payload.get("type") == "token_count"
                else None
            )
            if at < since:
                if usage:
                    before = usage
                continue
            if at >= until:
                continue
            seen = True
            first = at if first is None else min(first, at)
            last = at if last is None else max(last, at)
            if usage:
                current = {
                    key: max(current.get(key, 0), value)
                    for key, value in usage.items()
                    if isinstance(value, int) and value >= 0
                }
    if not seen:
        return None
    tokens = {key: max(0, value - before.get(key, 0)) for key, value in current.items()}
    return {
        "session": session,
        "model": model,
        "first": first.isoformat(),
        "last": last.isoformat(),
        "duration_seconds": (last - first).total_seconds(),
        "tokens": tokens,
        "transcript": str(path),
    }


def sessions(root: Path, since: datetime, until: datetime) -> list[dict[str, Any]]:
    found = {}
    for path in sorted(root.glob("**/*.jsonl")):
        result = read_session(path, since, until)
        if result is not None:
            previous = found.get(result["session"])
            if previous is None or sum(result["tokens"].values()) > sum(
                previous["tokens"].values()
            ):
                found[result["session"]] = result
    return list(found.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    parser.add_argument("--root", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    since = timestamp(args.since)
    until = timestamp(args.until) if args.until else datetime.now(timezone.utc)
    if len(args.until or "") == 10:
        from datetime import timedelta

        until += timedelta(days=1)
    result = sessions(args.root, since, until)
    output = {"sessions": result, "cursor": "usage export only"}
    sys.stdout.write(f"Native Codex sessions: {len(result)}\n")
    sys.stdout.write(json.dumps(output, indent=2) + "\n")
    if args.json:
        args.json.write_text(json.dumps(output, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
