"""Spend, context, wake-up, cache, denial and hook-error quant over Claude Code transcripts.

Usage: transcript_quant.py [--since DATE] [--until DATE] [--project SUBSTR] [--json PATH]

Skill spend is attributed from a Skill call or slash command until the next real user prompt.
Stop-hook and hook-error rows carry the UTC time the hook was last seen: a stale row means the
hook stopped running, not that it got faster.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from transcripts import (
    ApiCall,
    RecordStream,
    Transcript,
    Window,
    add_window_args,
    blocks,
    find_transcripts,
    parse_ts,
    percentile,
    text_of,
    window_from_args,
)

GROUPS = ("main", "subagent", "workflow")
BANDS = (
    (100_000, "<100K"),
    (200_000, "100-200K"),
    (400_000, "200-400K"),
    (600_000, "400-600K"),
    (800_000, "600-800K"),
    (float("inf"), ">800K"),
)
NOTIFICATION_SOURCES = (
    ("Agent", "agent"),
    ("Background command", "background command"),
    ("Monitor", "monitor"),
)
# The same denial surfaces with and without this prefix depending on the harness path.
HOOK_ERROR_PREFIX = re.compile(r"^\w+:\w+ hook error: ")
BIG_CACHE_WRITE = 50_000
BIG_RESULT = 25_000
STEER = re.compile(
    r"^(no[,.! ]|no$|don't|do not|stop|wait|why did you|why are you|why is|why does|i said"
    r"|i told you|i asked|again[,.]|wrong|that's not|thats not|not what i|you didn't"
    r"|you did not|you ignored|revert|undo|i already|didn't i|did you read|as i said"
    r"|like i said|nope|incorrect|you were supposed|you should have|please don't|you keep"
    r"|you're not|youre not|this is not|still|what happened|what's going|whats going)",
    re.I,
)
SLASH_NAME = re.compile(r"<command-name>([^<]+)</command-name>")
# Built-in slash commands. Some share a name with a plugin command (/status, /codex:status), and
# plugin entries are keyed with their prefix, so a bare built-in never matches one.
BUILTIN_SLASH = frozenset(
    "add-dir agents bashes clear compact config context cost doctor effort exit export fast help "
    "hooks ide init login logout mcp memory model output-style permissions plugin privacy-settings "
    "release-notes resume rewind status statusline terminal-setup todos upgrade usage vim".split()
)
BASH_PATTERNS = {
    "chained": re.compile(r"\s(&&|\|\||;)\s"),
    "cd_and": re.compile(r"^\s*cd\s+\S+\s*&&"),
    "git_-C": re.compile(r"\bgit -C\b"),
    "shell_read": re.compile(r"^\s*(cat|head|tail|sed -n)\s"),
    "shell_edit": re.compile(r"sed -i|cat >{1,2} ?\S+ <<|write_text\(|tee \S+ <<"),
    "sleep": re.compile(r"\bsleep \d"),
}


def band_of(context: int) -> str:
    return next(label for limit, label in BANDS if context < limit)


def gap_class(gap_s: float | None) -> str:
    if gap_s is None:
        return "first call"
    if gap_s >= 3600:
        return "idle >1h"
    return "idle 5m-1h" if gap_s >= 300 else "active <5m"


def wake_cause(record: dict, headless: bool) -> str | None:
    """What a main-session record makes the model respond to; None when it wakes nothing new."""
    kind = record.get("type")
    if kind == "attachment":
        attachment = record.get("attachment") or {}
        if attachment.get("type") != "queued_command":
            return None
        return _classify(text_of(attachment.get("prompt")), meta=False, headless=headless)
    if kind != "user" or record.get("isSidechain"):
        return None
    if record.get("isCompactSummary"):
        return "compaction"
    text = "\n".join(b.get("text", "") for b in blocks(record) if b.get("type") == "text")
    if not text.strip():
        return None
    return _classify(text, meta=bool(record.get("isMeta")), headless=headless)


def _classify(text: str, *, meta: bool, headless: bool) -> str | None:
    s = text.lstrip()
    if s.startswith("Stop hook feedback"):
        body = s.removeprefix("Stop hook feedback:").lstrip()
        if body.startswith("CI"):
            return "stop: ci-wait" if "still running" in body[:300] else "stop: ci-red"
        return "stop: review" if "review" in body[:300].lower() else "stop: other"
    if "[Subagent hand-back]" in s[:600]:
        return "subagent hand-back"
    if "<agent-message" in s[:200]:
        return "agent message"
    if s.startswith("<task-notification>"):
        summary = s[s.find("<summary>") + 9 :][:40] if "<summary>" in s else ""
        source = next((v for k, v in NOTIFICATION_SOURCES if summary.startswith(k)), "other")
        return f"task notification: {source}"
    if s.startswith(("<command-name>", "<command-message>")):
        return "slash command"
    if meta or s.startswith("<"):
        return None
    return "headless prompt" if headless else "user prompt"


def installed_slash_names(config: Path) -> frozenset[str]:
    """Slash names of the installed skills and commands: `name` for the config dir's own,
    `ns:name` for a command in a subdirectory, `plugin:name` for a plugin's."""
    names: set[str] = set()
    skills = config / "skills"
    for root, _dirs, files in os.walk(skills, followlinks=True):
        if "SKILL.md" in files and Path(root) != skills:
            names.add(Path(root).name)
    commands = config / "commands"
    for md in commands.rglob("*.md") if commands.is_dir() else ():
        names.add(":".join(md.relative_to(commands).with_suffix("").parts))
    for version_dir in (config / "plugins/cache").glob("*/*/*"):
        plugin = version_dir.parent.name
        names.update(f"{plugin}:{md.parent.name}" for md in version_dir.glob("skills/*/SKILL.md"))
        names.update(f"{plugin}:{md.stem}" for md in version_dir.glob("commands/*.md"))
    return frozenset(names)


_installed: frozenset[str] | None = None


def slash_name(record: dict) -> str | None:
    """The skill or command a slash-command record runs; None for a built-in or an unknown name."""
    global _installed
    text = "\n".join(b.get("text", "") for b in blocks(record) if b.get("type") == "text")
    match = SLASH_NAME.search(text)
    if not match:
        return None
    name = match.group(1).lstrip("/")
    if name in BUILTIN_SLASH:
        return None
    if _installed is None:
        _installed = installed_slash_names(Path.home() / ".claude")
    return name if name in _installed else None


def skill_invoked(record: dict) -> str | None:
    """The skill a record's Skill tool call loads. The harness's attributionSkill tag is not used:
    it persists across later prompts and charged unrelated calls to the last skill."""
    if record.get("type") != "assistant":
        return None
    for b in blocks(record):
        if b.get("type") == "tool_use" and b.get("name") == "Skill":
            return (b.get("input") or {}).get("skill") or "?"
    return None


def _money(value: float) -> float:
    return round(value, 2)


@dataclass
class Tally:
    n: int = 0
    calls: int = 0
    cost: float = 0.0

    def as_dict(self) -> dict:
        return {"n": self.n, "calls": self.calls, "cost": _money(self.cost)}


@dataclass
class Quant:
    window: Window
    stream: RecordStream
    files: Counter = field(default_factory=Counter)
    spend: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(Tally)))
    parts: dict = field(default_factory=lambda: defaultdict(Counter))
    daily: dict = field(default_factory=lambda: defaultdict(Counter))
    entrypoint: dict = field(default_factory=lambda: defaultdict(Tally))
    effort: dict = field(default_factory=lambda: defaultdict(Counter))
    by_skill: dict = field(default_factory=lambda: defaultdict(Tally))
    stop_hook_ms: dict = field(default_factory=lambda: defaultdict(list))
    contexts: dict = field(default_factory=lambda: defaultdict(list))
    bands: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(Tally)))
    wakes: dict = field(default_factory=lambda: defaultdict(Tally))
    rewrites: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(Tally)))
    denials: Counter = field(default_factory=Counter)
    denial_kinds: Counter = field(default_factory=Counter)
    denials_by_group: Counter = field(default_factory=Counter)
    tool_errors: Counter = field(default_factory=Counter)
    hook_errors: Counter = field(default_factory=Counter)
    hook_error_sample: dict = field(default_factory=dict)
    hook_error_last: dict = field(default_factory=dict)
    stop_hook_last: dict = field(default_factory=dict)
    stop_hook_errors: Counter = field(default_factory=Counter)
    agent_types: dict = field(default_factory=lambda: defaultdict(Tally))
    spawns: Counter = field(default_factory=Counter)
    skills: Counter = field(default_factory=Counter)
    bash: dict = field(default_factory=lambda: defaultdict(Counter))
    reads: dict = field(default_factory=lambda: defaultdict(Counter))
    compactions: Counter = field(default_factory=Counter)
    big_results: list = field(default_factory=list)
    steering: list = field(default_factory=list)
    unpriced: Counter = field(default_factory=Counter)
    sessions: dict = field(default_factory=dict)

    def scan(self, t: Transcript) -> None:
        g = t.group
        sess = self.sessions.setdefault(
            t.session,
            {
                "project": t.project[-40:],
                "main": 0.0,
                "agents": 0.0,
                "calls": 0,
                "peak": 0,
                "prompts": 0,
                "denials": 0,
            },
        )
        agent_tally = self.agent_types[t.agent_type] if g != "main" else None
        billed = False
        prev_ts: str | None = None
        cause, fresh_cause = "session start", True
        headless = False
        tool_inputs: dict[str, tuple[str, dict]] = {}
        skill: str | None = None
        for rec in self.stream.records(t):
            headless = headless or rec.get("entrypoint") == "sdk-cli"
            if g == "main":
                new_cause = wake_cause(rec, headless)
                if new_cause:
                    cause, fresh_cause = new_cause, True
                    if new_cause in ("user prompt", "headless prompt"):
                        sess["prompts"] += 1
                        self._steering(t, rec)
                        skill = None
                    elif new_cause == "slash command":
                        skill = slash_name(rec)
            skill = skill_invoked(rec) or skill
            call = self.stream.api_call(rec)
            if call:
                if not billed:
                    billed = True
                    self.files[g] += 1
                    if agent_tally is not None:
                        agent_tally.n += 1
                cost = self._call(t, rec, call, sess, agent_tally, skill)
                gap = (parse_ts(call.ts) - parse_ts(prev_ts)).total_seconds() if prev_ts else None
                prev_ts = call.ts or prev_ts
                if call.cache_write > BIG_CACHE_WRITE:
                    r = self.rewrites[g][gap_class(gap)]
                    r.n += 1
                    r.cost += call.cost_parts()["cache_write"]
                if g == "main":
                    w = self.wakes[cause]
                    w.n += fresh_cause
                    w.calls += 1
                    w.cost += cost
                    fresh_cause = False
            kind = rec.get("type")
            if kind == "assistant":
                self._tool_uses(g, rec, tool_inputs)
            elif kind == "user":
                self._tool_results(t, rec, tool_inputs, sess)
            elif kind == "attachment":
                self._attachment(rec.get("attachment") or {}, rec.get("timestamp") or "")
            elif kind == "system":
                self._system(g, rec)

    def _call(
        self,
        t: Transcript,
        rec: dict,
        call: ApiCall,
        sess: dict,
        agent_tally: Tally | None,
        skill: str | None,
    ) -> float:
        g = t.group
        cost = call.cost()
        if not call.priced:
            self.unpriced[call.model] += 1
        s = self.spend[g][call.model]
        s.calls += 1
        s.cost += cost
        self.parts[g].update(call.cost_parts())
        self.daily[call.ts[:10]][g] += cost
        eff = self.effort[f"{g} {call.model} {rec.get('effort') or '?'}"]
        eff.update(calls=1, output=call.output, thinking=call.thinking)
        eff["cost_cents"] += round(cost * 100)
        by_skill = self.by_skill[skill or "(none)"]
        by_skill.calls += 1
        by_skill.cost += cost
        self.contexts[g].append(call.context)
        b = self.bands[g][band_of(call.context)]
        b.calls += 1
        b.cost += cost
        if g == "main":
            ep = self.entrypoint[rec.get("entrypoint") or "?"]
            ep.calls += 1
            ep.cost += cost
            sess["main"] += cost
            sess["calls"] += 1
            sess["peak"] = max(sess["peak"], call.context)
        else:
            sess["agents"] += cost
            agent_tally.calls += 1
            agent_tally.cost += cost
        return cost

    def _tool_uses(self, g: str, rec: dict, tool_inputs: dict) -> None:
        for b in blocks(rec):
            if b.get("type") != "tool_use":
                continue
            name, inp = b.get("name") or "?", b.get("input") or {}
            tool_inputs[b.get("id")] = (name, inp)
            if name in ("Agent", "Task"):
                self.spawns[inp.get("subagent_type") or "default"] += 1
            elif name == "Skill":
                self.skills[inp.get("skill") or "?"] += 1
            elif name == "Bash":
                cmd = inp.get("command") or ""
                self.bash[g]["calls"] += 1
                self.bash[g].update(k for k, rx in BASH_PATTERNS.items() if rx.search(cmd))
            elif name == "Read":
                ranged = inp.get("offset") or inp.get("limit")
                self.reads[g]["ranged" if ranged else "full"] += 1

    def _tool_results(self, t: Transcript, rec: dict, tool_inputs: dict, sess: dict) -> None:
        denial = rec.get("toolDenialKind")
        for b in blocks(rec):
            if b.get("type") != "tool_result":
                continue
            text = text_of(b.get("content"))
            name, inp = tool_inputs.get(b.get("tool_use_id"), ("?", {}))
            first = HOOK_ERROR_PREFIX.sub("", text.strip().split("\n")[0])[:140]
            if denial:
                self.denials[(name, first)] += 1
                self.denial_kinds[denial] += 1
                self.denials_by_group[t.group] += 1
                sess["denials"] += 1
            elif b.get("is_error"):
                self.tool_errors[(name, first)] += 1
            if len(text) > BIG_RESULT:
                what = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                self.big_results.append((len(text), name, t.session[:8], str(what)[:110]))

    def _attachment(self, a: dict, ts: str) -> None:
        if a.get("type") != "hook_non_blocking_error":
            return
        hook = Path((a.get("command") or "?").split()[0]).name
        key = (hook, a.get("hookName") or a.get("hookEvent") or "?", a.get("exitCode"))
        self.hook_errors[key] += 1
        self.hook_error_last[key] = max(self.hook_error_last.get(key, ""), ts)
        stderr = (a.get("stderr") or "").strip().split("\n")[0][:160]
        self.hook_error_sample.setdefault(key, stderr)

    def _system(self, g: str, rec: dict) -> None:
        subtype = rec.get("subtype")
        if subtype == "compact_boundary":
            self.compactions[g] += 1
        elif subtype == "stop_hook_summary":
            for err in rec.get("hookErrors") or []:
                self.stop_hook_errors[str(err).strip().split("\n")[0][:140]] += 1
            for info in rec.get("hookInfos") or []:
                if "durationMs" in info:
                    hook = Path((info.get("command") or "?").split()[0]).name
                    self.stop_hook_ms[hook].append(info["durationMs"])
                    ts = rec.get("timestamp") or ""
                    self.stop_hook_last[hook] = max(self.stop_hook_last.get(hook, ""), ts)

    def _steering(self, t: Transcript, rec: dict) -> None:
        text = "\n".join(b.get("text", "") for b in blocks(rec) if b.get("type") == "text")
        text = text.strip()
        if text and len(text) < 700 and STEER.search(text):
            self.steering.append((t.session[:8], (rec.get("timestamp") or "")[:16], text[:300]))

    def result(self) -> dict:
        spend = {
            g: {m: s.as_dict() for m, s in sorted(ms.items(), key=lambda kv: -kv[1].cost)}
            for g, ms in self.spend.items()
        }
        totals = {g: _money(sum(s.cost for s in self.spend[g].values())) for g in GROUPS}
        context = {}
        for g, xs in self.contexts.items():
            context[g] = {
                "calls": len(xs),
                **{f"p{int(p * 100)}": percentile(xs, p) for p in (0.5, 0.9, 0.99)},
                "max": max(xs, default=0),
                "bands": {lbl: self.bands[g][lbl].as_dict() for _, lbl in BANDS},
            }
        sessions = sorted(self.sessions.items(), key=lambda kv: -(kv[1]["main"] + kv[1]["agents"]))
        return {
            "window": {"since": self.window.since_iso, "until": self.window.until_iso},
            "files": dict(self.files),
            "unpriced_models": dict(self.unpriced),
            "spend": {
                "total": _money(sum(totals.values())),
                "by_group": totals,
                "by_model": spend,
                "parts": {g: {k: _money(v) for k, v in p.items()} for g, p in self.parts.items()},
                "main_by_entrypoint": {k: v.as_dict() for k, v in self.entrypoint.items()},
                "by_group_model_effort": {k: dict(v) for k, v in sorted(self.effort.items())},
                "daily": {
                    d: {g: _money(v) for g, v in gs.items()} for d, gs in sorted(self.daily.items())
                },
            },
            "context": context,
            "wakes_main": {
                k: v.as_dict() for k, v in sorted(self.wakes.items(), key=lambda kv: -kv[1].cost)
            },
            "cache_rewrites_over_50k": {
                g: {k: v.as_dict() for k, v in cs.items()} for g, cs in self.rewrites.items()
            },
            "denials": {
                "total": sum(self.denials.values()),
                "by_group": dict(self.denials_by_group),
                "by_kind": dict(self.denial_kinds),
                "by_first_line": [[*k, n] for k, n in self.denials.most_common(40)],
            },
            "tool_errors_by_first_line": [[*k, n] for k, n in self.tool_errors.most_common(40)],
            "hook_errors": [
                {
                    "hook": k[0],
                    "event": k[1],
                    "exit": k[2],
                    "n": n,
                    "last_seen": self.hook_error_last[k][:16] + "Z",
                    "stderr": self.hook_error_sample[k],
                }
                for k, n in self.hook_errors.most_common()
            ],
            "stop_hook_errors": self.stop_hook_errors.most_common(20),
            "stop_hook_ms": {
                hook: {
                    "n": len(ms),
                    "p50": percentile(ms, 0.5),
                    "p95": percentile(ms, 0.95),
                    "last_seen": self.stop_hook_last[hook][:16] + "Z",
                }
                for hook, ms in self.stop_hook_ms.items()
            },
            "spend_by_invoked_skill": {
                k: v.as_dict() for k, v in sorted(self.by_skill.items(), key=lambda kv: -kv[1].cost)
            },
            "agents_by_type": {
                k: v.as_dict()
                for k, v in sorted(self.agent_types.items(), key=lambda kv: -kv[1].cost)
                if v.n
            },
            "spawns_by_subagent_type": dict(self.spawns.most_common()),
            "skills": dict(self.skills.most_common()),
            "bash": {g: dict(c) for g, c in self.bash.items()},
            "reads": {g: dict(c) for g, c in self.reads.items()},
            "compactions": dict(self.compactions),
            "big_results": sorted(self.big_results, reverse=True)[:20],
            "steering": self.steering,
            "top_sessions": [
                {
                    "session": sid[:8],
                    **{k: _money(v) if isinstance(v, float) else v for k, v in s.items()},
                }
                for sid, s in sessions[:15]
            ],
        }


def summary(r: dict) -> str:
    def usd(v: float) -> str:
        return f"${v:,.0f}"

    def ranked(d: dict, key: str = "cost", top: int = 8) -> str:
        return ", ".join(f"{k} {usd(v[key])}" for k, v in list(d.items())[:top])

    sp = r["spend"]
    lines = [
        f"window {r['window']['since'][:16]}Z to {r['window']['until'][:16]}Z | files "
        + ", ".join(f"{g} {r['files'].get(g, 0)}" for g in GROUPS),
        f"spend {usd(sp['total'])}: " + ", ".join(f"{g} {usd(sp['by_group'][g])}" for g in GROUPS),
    ]
    lines += [f"  {g} by model: {ranked(sp['by_model'].get(g, {}))}" for g in GROUPS]
    lines.append(
        "  main by part: "
        + ", ".join(f"{k} {usd(v)}" for k, v in sp["parts"].get("main", {}).items())
    )
    lines.append(f"  main by entrypoint: {ranked(sp['main_by_entrypoint'])}")
    for g in ("main", "subagent"):
        c = r["context"].get(g)
        if not c:
            continue
        bands = ", ".join(
            f"{k} {usd(v['cost'])} ({v['calls']})" for k, v in c["bands"].items() if v["calls"]
        )
        lines.append(
            f"context {g}: p50 {c['p50'] // 1000}K p90 {c['p90'] // 1000}K p99 {c['p99'] // 1000}K"
            f" max {c['max'] // 1000}K | spend by band: {bands}"
        )
    lines.append(
        "wakes (main): "
        + ", ".join(f"{k} {usd(v['cost'])} ({v['n']}x)" for k, v in r["wakes_main"].items())
    )
    for g, classes in r["cache_rewrites_over_50k"].items():
        lines.append(
            f"cache writes >50K ({g}): "
            + ", ".join(f"{k} {v['n']}x {usd(v['cost'])}" for k, v in classes.items())
        )
    d = r["denials"]
    lines.append(f"denials {d['total']} {d['by_group']} {d['by_kind']}; top first lines:")
    lines += [f"  {n:4} {tool}: {line}" for tool, line, n in d["by_first_line"][:8]]
    lines.append(f"hook errors {sum(h['n'] for h in r['hook_errors'])}:")
    lines += [
        f"  {h['n']:5} {h['hook']} ({h['event']}, exit {h['exit']}, last {h['last_seen']}):"
        f" {h['stderr'][:80]}"
        for h in r["hook_errors"][:8]
    ]
    lines.append(f"agents by type: {ranked(r['agents_by_type'], top=12)}")
    lines.append(f"spend by invoked skill: {ranked(r['spend_by_invoked_skill'])}")
    lines.append("stop hooks by p95 (n, p50/p95 ms, last seen UTC):")
    lines += [
        f"  {k} {v['n']}x {v['p50']}/{v['p95']} last {v['last_seen']}"
        for k, v in sorted(r["stop_hook_ms"].items(), key=lambda kv: -kv[1]["p95"])[:12]
    ]
    lines.append(f"steering prompts (main): {len(r['steering'])}; compactions {r['compactions']}")
    if r["unpriced_models"]:
        lines.append(f"UNPRICED models (counted as $0): {r['unpriced_models']}")
    return "\n".join(lines) + "\n"


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
    quant = Quant(window, RecordStream(window))
    for t in transcripts:
        quant.scan(t)
    result = quant.result()
    if args.json:
        args.json.write_text(json.dumps(result, indent=1, default=str))
    sys.stdout.write(summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
