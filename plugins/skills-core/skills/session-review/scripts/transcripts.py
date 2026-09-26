"""Shared transcript discovery, windowing, dedupe and pricing for the session-review scripts."""

import argparse
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"

# $/MTok: input, output, cache read, cache write 5m, cache write 1h.
PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-opus-5": (5, 25, 0.5, 6.25, 10),
    "claude-opus-5-5": (5, 25, 0.5, 6.25, 10),  # unpublished; priced as claude-opus-5
    "claude-fable-5": (10, 50, 1.0, 12.5, 20),
    "claude-fable-5-1": (10, 50, 0.25, 12.5, 20),
    "claude-sonnet-5": (2, 10, 0.2, 2.5, 4),
    "claude-haiku-4-5-20251001": (1, 5, 0.1, 1.25, 2),
}
UNBILLED_MODELS = {"<synthetic>"}


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime

    @property
    def since_iso(self) -> str:
        return _iso(self.since)

    @property
    def until_iso(self) -> str:
        return _iso(self.until)

    def contains(self, ts: str | None) -> bool:
        """Records without a timestamp are metadata and always pass."""
        return ts is None or self.since_iso <= ts < self.until_iso

    def label(self) -> str:
        return f"{self.since_iso[:16]}Z to {self.until_iso[:16]}Z"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_when(value: str, *, end_of_day: bool) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if end_of_day and len(value) == 10:
        dt += timedelta(days=1)
    return dt


def add_window_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since", help="start date or ISO datetime, UTC (default: 14 days before today)"
    )
    parser.add_argument(
        "--until", help="end date (inclusive) or ISO datetime (exclusive), UTC (default: now)"
    )
    parser.add_argument("--project", help="substring of the project dir to restrict to")


def window_from_args(args: argparse.Namespace) -> Window:
    now = datetime.now(UTC)
    since = (
        _parse_when(args.since, end_of_day=False)
        if args.since
        else datetime.combine(now.date() - timedelta(days=14), time(), UTC)
    )
    until = _parse_when(args.until, end_of_day=True) if args.until else now
    return Window(since, until)


@dataclass(frozen=True)
class Transcript:
    path: Path
    group: str  # main | subagent | workflow
    project: str
    session: str  # the main session id; a subagent's is its parent's
    agent_id: str | None = None
    meta: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def agent_type(self) -> str:
        return self.meta.get("agentType", "?")


def find_transcripts(window: Window, project: str | None = None) -> list[Transcript]:
    """Main sessions, subagents and workflow agents touched since the window opened, oldest first.

    Oldest first so an original transcript claims its records before a /resume copy does.
    """
    since = window.since.timestamp()
    found: list[Transcript] = []
    for path in PROJECTS.glob("*/*.jsonl"):
        found.append(Transcript(path, "main", path.parent.name, path.stem))
    for path in PROJECTS.glob("*/*/subagents/**/*.jsonl"):
        rel = path.relative_to(PROJECTS).parts
        meta_path = path.with_name(path.stem + ".meta.json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                meta = {}
        group = "workflow" if "workflows" in rel else "subagent"
        found.append(
            Transcript(path, group, rel[0], rel[1], path.stem.removeprefix("agent-"), meta)
        )
    found = [
        t
        for t in found
        if t.path.stat().st_mtime >= since and (not project or project in t.project)
    ]
    return sorted(found, key=lambda t: t.path.stat().st_mtime)


class RecordStream:
    """Yields in-window records once each; /resume copies history into a new file under the same uuids."""

    def __init__(self, window: Window) -> None:
        self.window = window
        self._uuids: set[str] = set()
        self._message_ids: set[str] = set()

    def records(self, transcript: Transcript, needle: str | None = None) -> Iterator[dict]:
        with transcript.path.open(errors="replace") as fh:
            for line in fh:
                if needle and needle not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or not self.window.contains(
                    record.get("timestamp")
                ):
                    continue
                uuid = record.get("uuid")
                if uuid:
                    if uuid in self._uuids:
                        continue
                    self._uuids.add(uuid)
                yield record

    def api_call(self, record: dict) -> "ApiCall | None":
        """One ApiCall per message id: streaming splits a response into several records."""
        if record.get("type") != "assistant":
            return None
        message = record.get("message") or {}
        usage = message.get("usage") or {}
        mid = message.get("id")
        if not mid or mid in self._message_ids or usage.get("input_tokens") is None:
            return None
        self._message_ids.add(mid)
        return ApiCall.from_message(message, record.get("timestamp") or "")


@dataclass
class ApiCall:
    model: str
    ts: str
    input: int
    output: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    thinking: int

    @classmethod
    def from_message(cls, message: dict, ts: str) -> "ApiCall":
        usage = message.get("usage") or {}
        breakdown = usage.get("cache_creation") or {}
        c5 = breakdown.get("ephemeral_5m_input_tokens") or 0
        c1 = breakdown.get("ephemeral_1h_input_tokens") or 0
        if c5 + c1 == 0:
            # Older records carry only the total: price it at the 1h rate, the upper bound.
            c1 = usage.get("cache_creation_input_tokens") or 0
        details = usage.get("output_tokens_details") or {}
        return cls(
            model=message.get("model") or "?",
            ts=ts,
            input=usage.get("input_tokens") or 0,
            output=usage.get("output_tokens") or 0,
            cache_read=usage.get("cache_read_input_tokens") or 0,
            cache_write_5m=c5,
            cache_write_1h=c1,
            thinking=details.get("thinking_tokens") or 0,
        )

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def context(self) -> int:
        return self.input + self.cache_read + self.cache_write

    @property
    def priced(self) -> bool:
        return self.model in PRICES or self.model in UNBILLED_MODELS

    def cost_parts(self) -> dict[str, float]:
        p = PRICES.get(self.model, (0, 0, 0, 0, 0))
        return {
            "input": self.input * p[0] / 1e6,
            "output": self.output * p[1] / 1e6,
            "cache_read": self.cache_read * p[2] / 1e6,
            "cache_write": (self.cache_write_5m * p[3] + self.cache_write_1h * p[4]) / 1e6,
        }

    def cost(self) -> float:
        return sum(self.cost_parts().values())


def text_of(content: object) -> str:
    """Flatten a message or tool_result content (string or block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            text_of(b.get("text", b.get("content", ""))) for b in content if isinstance(b, dict)
        )
    return ""


def blocks(record: dict) -> list[dict]:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * p)]
