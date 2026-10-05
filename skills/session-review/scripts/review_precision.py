"""Review-agent yield and precision, per agent, from Claude Code transcripts.

Precision comes only from recorded outcomes, never from a heuristic: a number inferred from what
the main session said about a finding would be a guess wearing a percentage sign. Two sources:
- the fixer's `TALLY <id>=FIXED|NOT_REPRODUCED|OPTION|SKIPPED:<reason> ...` line (main session or
  an `engineer`), attributed to the role by id prefix (`prefix` in $AGENT_KIT_DIR/roles.toml: `CX-`
  cross-reviewer, `B-` bug-reviewer, `Q-` quality-reviewer, `R-` task-reviewer); precision is FIXED / (FIXED +
  NOT_REPRODUCED). Outcomes are deduplicated within a review round only: ids restart at 1 in every
  report, so the same `CX-1=FIXED` in two rounds is two outcomes, while an engineer's line restated by
  the main session in the same round is one;
- the retired critic's verdicts (to 2026-10-03), kept for history; a verdict whose finding carries
  a `CX-<n>` id is attributed to cross-reviewer (labelled `codex` before the roles rename).

Usage: review_precision.py [--since DATE] [--until DATE] [--project SUBSTR] [--json PATH]
"""

import argparse
import json
import os
import re
import statistics
import sys
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from transcripts import (
    RecordStream,
    Transcript,
    add_window_args,
    blocks,
    find_transcripts,
    text_of,
    window_from_args,
)

REVIEW_AGENTS = {
    "task-reviewer": "task-reviewer",
    "bug-reviewer": "bug-reviewer",
    "quality-reviewer": "quality-reviewer",
    "cross-reviewer": "cross-reviewer",
    # The role names before 2026-10-05. Permanent, unlike the hooks' aliases: old transcripts
    # persist, and they attribute to today's roles.
    "reviewer": "task-reviewer",
    "thermo-bugs": "bug-reviewer",
    "thermo-quality": "quality-reviewer",
    "review-cross": "cross-reviewer",
    "critic": "critic",
    "thermos:thermo-nuclear-review-subagent": "thermos:bugs",
    "thermos:thermo-nuclear-code-quality-review-subagent": "thermos:quality",
}
GRADED = ("CONFIRMED", "FALSE_POSITIVE", "UNPROVEN")
VERDICT = re.compile(r"^\s*VERDICT:\s*\**\s*(CONFIRMED|FALSE_POSITIVE|UNPROVEN|OPINION)\b", re.M)
TALLY = re.compile(r"^\s*TALLY:", re.M)
# Thermos-style reports number each finding under a severity heading (`**M1. …**`, `### B2.`);
# `task-reviewer` emits bullets citing path:line. Prefer the id form so a numbered finding that also
# cites paths is counted once.
FINDING_ID = re.compile(r"^\s*(?:#{2,4}\s*)?\**\s*\[?[A-Z]{0,3}\d+[a-z]?\]?[.:)]\s", re.M)
FINDING_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+.*?[\w./-]+\.\w+:\d+", re.M)
LOOKS_GOOD = re.compile(r"^\s*(?:#+\s*)?\**\s*(?:Looks good|Verified[- ]good)", re.M | re.I)
LAUNCH_FAILURE = re.compile(r"temporarily unavailable|cannot determine the safety|^Error:", re.M)
AGENT_ID = re.compile(r"agentId: (a[0-9a-f]+)")
AGENT_MESSAGE = re.compile(
    r'<agent-message from="(?P<aid>[^"]+)">(?P<body>.*?)(?:</agent-message>|\Z)', re.S
)
HANDBACK_PREAMBLE_END = "The report follows:"
NOTIFICATION = re.compile(
    r"<task-id>(?P<aid>[^<]+)</task-id>\s*<tool-use-id>(?P<tuid>[^<]+)</tool-use-id>"
    r".*?<result>(?P<body>.*?)(?:</result>|\Z)",
    re.S,
)
CRITIC_FIELD = re.compile(
    r"^\s*(EVIDENCE|TRIGGER|NEEDED|PROPOSES|ACTUAL MECHANISM|VERDICT|TALLY):", re.I
)
# Ids the critic and the main session prefix to a finding: `[9] F9.`, `[A-H1]`, `[B7] Low.`
CRITIC_INDEX = re.compile(r"^\s*\[[^\]]{1,8}\]\s*")
CODEX_ID = re.compile(r"(?<![\w-])CX-\d+\b")
TALLY_LINE = re.compile(r"^[\s>*_`-]*TALLY\s+(?=[A-Za-z][\w-]*=)(?P<items>.*)$", re.M)
TALLY_ITEM = re.compile(
    r"(?<![\w-])(?P<prefix>[A-Z]{1,3})-(?P<num>\d+)=(?P<outcome>FIXED|NOT_REPRODUCED|OPTION|SKIPPED)\b"
)
TALLY_OUTCOMES = ("FIXED", "NOT_REPRODUCED", "OPTION", "SKIPPED")
ROLES_TOML = Path(os.environ.get("AGENT_KIT_DIR") or Path(__file__).resolve().parents[3]) / "roles.toml"
LEGACY_PREFIX = {"CX": "cross-reviewer", "B": "bug-reviewer", "Q": "quality-reviewer", "R": "task-reviewer"}


def role_prefixes(path: Path = ROLES_TOML) -> dict[str, str]:
    """Finding-id prefix -> role, from roles.toml: a role keeps its prefix across renames and models."""
    out = dict(LEGACY_PREFIX)
    try:
        roles = tomllib.loads(path.read_text()).get("roles") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return out
    out.update({s["prefix"]: name for name, s in roles.items() if isinstance(s, dict) and s.get("prefix")})
    return out


TALLY_PREFIX = role_prefixes()
CROSS_LABEL = TALLY_PREFIX["CX"]
# A review round starts at a review agent's spawn or a cross-model run through Bash: the runner in
# command position (line start or after ; & | or `(`, past any VAR=value prefixes) with a role, not
# a dry run. Any mention of its path (`cat bin/agent-run`, a grep) opened a round, which reset the
# per-round dedupe and counted a restated TALLY twice.
ROUND_BASH = re.compile(
    r"(?:^|[;&|(\n])\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:env\s+(?:[A-Za-z_]\w*=\S*\s+)*)?"
    r"(?:[^\s;&|()]*/)?bin/(?:agent-run\s+[a-z][a-z0-9-]*|codex-review)(?=[\s;&|)]|$)"
)


def round_command(command: str) -> bool:
    return bool(ROUND_BASH.search(command)) and "--dry-run" not in command
# Subagents whose own text can carry the fixer's TALLY line (a large round's engineer; `worker`
# before 2026-10-05).
FIXER_AGENTS = frozenset({"engineer", "worker"})
TITLE_ID = re.compile(
    r"^(?:[a-z]{0,3}-?[a-z]?\d+[a-z]?[.:)]?\s+|(?:low|medium|high|nit|blocking)\W*\s)+"
)
WORD = re.compile(r"[a-z0-9_]+(?:[./-][a-z0-9_]+)*")
STOPWORDS = frozenset(
    "that this with from when into only than then they them their there which while where what"
    " have been does each same also line lines code finding should would could because".split()
)


@dataclass
class Spawn:
    tuid: str
    label: str
    session: str
    ts: str
    aid: str | None = None
    sync_result: str = ""


@dataclass
class AgentRun:
    aid: str
    label: str
    session: str
    tuid: str | None
    start: str = ""
    end: str = ""
    handback: str = ""
    final_text: str = ""
    cost: float = 0.0


@dataclass
class LabelStats:
    spawns: int = 0
    reported: int = 0
    findings: list[int] = field(default_factory=list)
    empty_report: int = 0
    never_started: int = 0
    launch_failed: int = 0
    cost: float = 0.0


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*`_#\[\]()>]", "", text)).strip().lower()


def count_findings(body: str) -> int:
    looks_good = LOOKS_GOOD.search(body)
    body = body[: looks_good.start()] if looks_good else body
    return len(FINDING_ID.findall(body)) or len(FINDING_BULLET.findall(body))


class Collector:
    def __init__(self, stream: RecordStream) -> None:
        self.stream = stream
        self.spawns: dict[str, Spawn] = {}
        self.runs: dict[str, AgentRun] = {}
        self.by_aid: dict[str, str] = {}  # parent-side reports, aid -> body
        self.by_tuid: dict[str, str] = {}
        # session -> (timestamp, kind, TALLY items): kind "round" opens a review round, "tally" is
        # one fixer line; tally_attribution orders them and dedupes within a round.
        self.tally_lines: dict[str, list[tuple[str, str, str]]] = defaultdict(list)

    def _tallies(self, session: str, text: str, ts: str = "") -> None:
        for m in TALLY_LINE.finditer(text):
            self.tally_lines[session].append((ts, "tally", m.group("items").strip()))

    def _round(self, session: str, ts: str) -> None:
        self.tally_lines[session].append((ts, "round", ""))

    def main_session(self, t: Transcript) -> None:
        for rec in self.stream.records(t):
            kind = rec.get("type")
            if kind == "assistant":
                for b in blocks(rec):
                    self._spawn(t, rec, b)
                    if b.get("type") == "text" and "TALLY" in b.get("text", ""):
                        self._tallies(t.session, b["text"], rec.get("timestamp") or "")
                    elif (b.get("type") == "tool_use" and b.get("name") == "Bash"
                          and round_command(str((b.get("input") or {}).get("command") or ""))):
                        self._round(t.session, rec.get("timestamp") or "")
            elif kind == "user":
                for b in blocks(rec):
                    if b.get("type") == "tool_result":
                        self._tool_result(b)
                    elif b.get("type") == "text":
                        self._parent_reports(b.get("text", ""))
            elif kind == "attachment":
                attachment = rec.get("attachment") or {}
                if attachment.get("type") == "queued_command":
                    self._parent_reports(text_of(attachment.get("prompt")))
            elif kind == "queue-operation":
                self._parent_reports(text_of(rec.get("content")))

    def _spawn(self, t: Transcript, rec: dict, b: dict) -> None:
        if b.get("type") != "tool_use" or b.get("name") not in ("Agent", "Task"):
            return
        label = REVIEW_AGENTS.get((b.get("input") or {}).get("subagent_type") or "")
        if label and b.get("id") not in self.spawns:
            self.spawns[b["id"]] = Spawn(b["id"], label, t.session, rec.get("timestamp") or "")
            self._round(t.session, rec.get("timestamp") or "")

    def _tool_result(self, b: dict) -> None:
        spawn = self.spawns.get(b.get("tool_use_id") or "")
        if spawn is None:
            return
        text = text_of(b.get("content"))
        match = AGENT_ID.search(text)
        if match:
            spawn.aid = match.group(1)
        if "Async agent launched" not in text:
            spawn.sync_result = text

    def _parent_reports(self, text: str) -> None:
        if "agent-message" in text:
            for m in AGENT_MESSAGE.finditer(text):
                body = m.group("body")
                cut = body.find(HANDBACK_PREAMBLE_END)
                body = body[cut + len(HANDBACK_PREAMBLE_END) :] if cut >= 0 else body
                self.by_aid.setdefault(m.group("aid"), body)
        if "task-notification" in text:
            for m in NOTIFICATION.finditer(text):
                self.by_aid.setdefault(m.group("aid"), m.group("body"))
                self.by_tuid.setdefault(m.group("tuid"), m.group("body"))

    def agent(self, t: Transcript) -> None:
        if t.agent_type in FIXER_AGENTS:
            self._fixer(t)
            return
        label = REVIEW_AGENTS.get(t.agent_type)
        if not label or not t.agent_id:
            return
        run = AgentRun(t.agent_id, label, t.session, t.meta.get("toolUseId"))
        for rec in self.stream.records(t):
            ts = rec.get("timestamp")
            if ts:
                run.start = run.start or ts
                run.end = ts
            call = self.stream.api_call(rec)
            if call:
                run.cost += call.cost()
            if rec.get("type") != "assistant":
                continue
            for b in blocks(rec):
                if b.get("type") == "tool_use" and b.get("name") == "SubagentHandback":
                    run.handback = str((b.get("input") or {}).get("message") or "")
                elif b.get("type") == "text" and b.get("text", "").strip():
                    run.final_text = b["text"]
        self.runs[run.aid] = run

    def _fixer(self, t: Transcript) -> None:
        for rec in self.stream.records(t):
            if rec.get("type") != "assistant":
                continue
            for b in blocks(rec):
                if b.get("type") == "text":
                    text = b.get("text", "")
                elif b.get("type") == "tool_use" and b.get("name") == "SubagentHandback":
                    text = str((b.get("input") or {}).get("message") or "")
                else:
                    continue
                if "TALLY" in text:
                    self._tallies(t.session, text, rec.get("timestamp") or "")


@dataclass
class Resolved:
    spawn: Spawn
    run: AgentRun | None
    body: str
    status: str  # reported | empty | never_started | launch_failed


def resolve(c: Collector) -> list[Resolved]:
    runs_by_tuid = {r.tuid: r for r in c.runs.values() if r.tuid}
    out = []
    for spawn in c.spawns.values():
        run = runs_by_tuid.get(spawn.tuid) or c.runs.get(spawn.aid or "")
        aid = run.aid if run else spawn.aid
        body = next(
            (
                b
                for b in (
                    run.handback if run else "",
                    c.by_aid.get(aid or "", ""),
                    c.by_tuid.get(spawn.tuid, ""),
                    spawn.sync_result,
                    run.final_text if run else "",
                )
                if b.strip()
            ),
            "",
        )
        if run is None and LAUNCH_FAILURE.search(spawn.sync_result[:400]):
            status = "launch_failed"
        elif body.strip():
            status = "reported"
        else:
            status = "empty" if run else "never_started"
        out.append(Resolved(spawn, run, body, status))
    return out


def critic_items(body: str) -> list[tuple[str, str]]:
    """(verdict, finding title) per critic block; the title is the line above the VERDICT."""
    items = []
    lines = body.splitlines()
    for i, line in enumerate(lines):
        m = VERDICT.match(line)
        if not m:
            continue
        title = next(
            (
                lines[j]
                for j in range(i - 1, -1, -1)
                if lines[j].strip() and not CRITIC_FIELD.match(lines[j])
            ),
            "",
        )
        items.append((m.group(1), title))
    return items


def words(text: str) -> set[str]:
    return {w for w in WORD.findall(text) if len(w) >= 4 and w not in STOPWORDS}


def attribute(title: str, sources: dict[str, tuple[str, set[str]]]) -> list[str]:
    """Labels whose report holds this finding: verbatim title first, then distinctive-word overlap.

    The main session relabels and trims findings before handing them to the critic, so an exact
    match misses about half; the overlap fallback only accepts a clear single winner.
    """
    full = TITLE_ID.sub("", norm(CRITIC_INDEX.sub("", title)))
    key = full[:40]
    if len(key) >= 12:
        hits = [label for label, (body, _) in sources.items() if key in body]
        if hits:
            return hits
    title_words = words(full)
    if len(title_words) < 3:
        return []
    scores = sorted(
        ((len(title_words & ws) / len(title_words), label) for label, (_, ws) in sources.items()),
        reverse=True,
    )
    if not scores or scores[0][0] < 0.75:
        return []
    if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.15:
        return []
    return [scores[0][1]]


def critic_attribution(resolved: list[Resolved]) -> tuple[dict, int, int]:
    """Verdicts by the agent that raised each finding, from reviews that ended before the critic."""
    by_label: dict[str, Counter] = defaultdict(Counter)
    traced = total = 0
    by_session = defaultdict(list)
    for r in resolved:
        if r.status == "reported":
            by_session[r.spawn.session].append(r)
    for r in resolved:
        if r.spawn.label != "critic" or r.status != "reported":
            continue
        started = r.run.start if r.run else r.spawn.ts
        sources: dict[str, list[str]] = defaultdict(list)
        for other in by_session[r.spawn.session]:
            ended = other.run.end if other.run else other.spawn.ts
            if other.spawn.label != "critic" and ended <= started:
                sources[other.spawn.label].append(norm(other.body))
        joined = {}
        for label, bodies in sources.items():
            body = " ".join(bodies)
            joined[label] = (body, words(body))
        for verdict, title in critic_items(r.body):
            total += 1
            labels = [CROSS_LABEL] if CODEX_ID.search(title) else attribute(title, joined)
            traced += bool(labels)
            for label in labels or ["unattributed"]:
                by_label[label][verdict] += 1
    return by_label, traced, total


def tally_attribution(tally_lines: dict[str, list[tuple[str, str, str]]]) -> tuple[dict, int]:
    """Fixer outcomes by raising role. A line repeated within one review round (an engineer's TALLY
    restated by the main session to close it) counts once; the same line in another round is
    another round's outcomes, since every report numbers its findings from 1. An event with no
    timestamp cannot be placed in a round (it sorted before all of them) and is skipped."""
    by_label: dict[str, Counter] = defaultdict(Counter)
    lines = 0
    for events in tally_lines.values():
        seen: set[str] = set()
        for _ts, kind, line in sorted((e for e in events if e[0]), key=lambda e: e[0]):
            if kind == "round":
                seen.clear()
                continue
            if line in seen:
                continue
            seen.add(line)
            lines += 1
            for m in TALLY_ITEM.finditer(line):
                label = TALLY_PREFIX.get(m.group("prefix"), "unattributed")
                by_label[label][m.group("outcome")] += 1
    return by_label, lines


def build(
    resolved: list[Resolved], runs: dict[str, AgentRun], tally_lines: dict[str, list[tuple[str, str, str]]]
) -> dict:
    stats: dict[str, LabelStats] = defaultdict(LabelStats)
    verdicts: Counter = Counter()
    tallied = 0
    for r in resolved:
        s = stats[r.spawn.label]
        s.spawns += 1
        if r.status == "reported":
            s.reported += 1
            if r.spawn.label == "critic":
                found = VERDICT.findall(r.body)
                verdicts.update(found)
                s.findings.append(len(found))
                tallied += bool(TALLY.search(r.body))
            else:
                s.findings.append(count_findings(r.body))
        elif r.status == "empty":
            s.empty_report += 1
        elif r.status == "never_started":
            s.never_started += 1
        else:
            s.launch_failed += 1
    for run in runs.values():
        stats[run.label].cost += run.cost
    by_label, traced, total = critic_attribution(resolved)
    tally_by_label, tally_count = tally_attribution(tally_lines)
    return {
        "agents": {
            label: {
                "spawns": s.spawns,
                "reported": s.reported,
                "median_findings": statistics.median(s.findings) if s.findings else None,
                "total_findings": sum(s.findings),
                "empty_report": s.empty_report,
                "never_started": s.never_started,
                "launch_failed": s.launch_failed,
                "cost": round(s.cost, 2),
            }
            for label, s in sorted(stats.items(), key=lambda kv: -kv[1].spawns)
        },
        "verdicts": dict(verdicts),
        "critic_runs_with_tally": tallied,
        "verdicts_by_raising_agent": {k: dict(v) for k, v in by_label.items()},
        "attribution": {"traced": traced, "total": total},
        "tally_lines": tally_count,
        "tally_by_raising_agent": {k: dict(v) for k, v in tally_by_label.items()},
    }


def precision(v: dict) -> str:
    graded = sum(v.get(k, 0) for k in GRADED)
    return f"{v.get('CONFIRMED', 0) / graded:.0%}" if graded else "-"


def tally_precision(v: dict) -> str:
    graded = v.get("FIXED", 0) + v.get("NOT_REPRODUCED", 0)
    return f"{v.get('FIXED', 0) / graded:.0%}" if graded else "-"


def tally_summary(r: dict) -> list[str]:
    if not r["tally_lines"]:
        return ["No fixer TALLY lines in the window."]
    lines = [
        f"fixer TALLY outcomes ({r['tally_lines']} lines; precision fixed/(fixed+not reproduced)):"
    ]
    for label, tv in sorted(r["tally_by_raising_agent"].items()):
        lines.append(
            f"  {label:16} "
            + " ".join(f"{k} {tv.get(k, 0)}" for k in TALLY_OUTCOMES)
            + f"  precision {tally_precision(tv)}"
        )
    return lines


def summary(r: dict, window_label: str) -> str:
    lines = [
        f"review agents, {window_label}",
        f"{'agent':16} {'spawns':>6} {'reported':>8} {'median':>6} {'findings':>8} "
        f"{'empty':>5} {'unstarted':>9} {'failed':>6} {'cost':>7}",
    ]
    for label, a in r["agents"].items():
        med = "-" if a["median_findings"] is None else f"{a['median_findings']:.0f}"
        lines.append(
            f"{label:16} {a['spawns']:>6} {a['reported']:>8} {med:>6} {a['total_findings']:>8} "
            f"{a['empty_report']:>5} {a['never_started']:>9} {a['launch_failed']:>6} "
            f"${a['cost']:>6,.0f}"
        )
    v = r["verdicts"]
    if not any(v.get(k) for k in GRADED):
        lines.append("No critic verdicts in the window (the critic retired 2026-10-03).")
        return "\n".join([*lines, *tally_summary(r)]) + "\n"
    graded = sum(v.get(k, 0) for k in GRADED)
    lines.append(
        f"critic verdicts: {graded} graded, precision (confirmed/graded) {precision(v)}; "
        + ", ".join(f"{k} {v.get(k, 0)}" for k in (*GRADED, "OPINION"))
        + f"; {r['critic_runs_with_tally']} runs closed with TALLY"
    )
    at = r["attribution"]
    lines.append(
        f"by raising agent ({at['traced']}/{at['total']} verdicts traced; a finding raised by"
        " two agents counts for both):"
    )
    for label, bv in sorted(r["verdicts_by_raising_agent"].items()):
        lines.append(
            f"  {label:16} "
            + " ".join(f"{k} {bv.get(k, 0)}" for k in (*GRADED, "OPINION"))
            + f"  precision {precision(bv)}"
        )
    return "\n".join([*lines, *tally_summary(r)]) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_window_args(parser)
    parser.add_argument("--json", type=Path, help="write the full result here")
    args = parser.parse_args()
    window = window_from_args(args)
    transcripts = find_transcripts(window, args.project)
    if not transcripts:
        sys.stderr.write(f"No transcripts touched in {window.label()}\n")
        return 1
    collector = Collector(RecordStream(window))
    for t in transcripts:
        if t.group == "main":
            collector.main_session(t)
        elif t.group == "subagent":
            collector.agent(t)
    result = build(resolve(collector), collector.runs, collector.tally_lines)
    result["window"] = {"since": window.since_iso, "until": window.until_iso}
    if args.json:
        args.json.write_text(json.dumps(result, indent=1))
    sys.stdout.write(summary(result, window.label()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
