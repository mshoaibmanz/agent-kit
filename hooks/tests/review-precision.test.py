#!/usr/bin/env python3
"""Tests for skills/session-review/scripts/review_precision.py: TALLY attribution by role prefix
(roles.toml) and deduplication within a review round, not across a session (RC-CX-5).

    python3 ~/.agents/hooks/tests/review-precision.test.py

PRECISION_DIR=<dir> tests another copy of the scripts directory. Exits 1 on any failure.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import tempfile
import types
from pathlib import Path

logger = logging.getLogger("review-precision.test")
SCRIPTS = Path(os.environ.get("PRECISION_DIR") or Path(__file__).resolve().parents[2] / "skills/session-review/scripts")
failures: list[str] = []


def check(label: str, cond: bool, detail: object = "") -> None:
    logger.info("%s %s", "ok  " if cond else "FAIL", label)
    if not cond:
        failures.append(label)
        if detail:
            logger.info("     %s", detail)


def outcomes(rp, events: list[tuple[str, str, str]]) -> tuple[dict, int]:
    by_label, lines = rp.tally_attribution({"s1": events})
    return {k: dict(v) for k, v in by_label.items()}, lines


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    sys.path.insert(0, str(SCRIPTS))
    rp = importlib.import_module("review_precision")

    # RC-CX-5: three rounds; ids restart at 1 in every report.
    rounds = [
        ("t1", "round", ""), ("t2", "tally", "CX-1=FIXED"),
        ("t3", "round", ""), ("t4", "tally", "CX-1=FIXED"),
        ("t5", "round", ""), ("t6", "tally", "CX-1=NOT_REPRODUCED"),
    ]
    try:
        got, lines = outcomes(rp, rounds)
    except Exception as exc:  # noqa: BLE001 - the old signature fails here; that is the finding
        got, lines = {"error": repr(exc)}, 0
    check("RC-CX-5: the same outcome in two rounds counts twice (2 FIXED, 1 NOT_REPRODUCED)",
          got.get("review-cross") == {"FIXED": 2, "NOT_REPRODUCED": 1} and lines == 3, got)

    # A worker's TALLY restated by the main session in the same round counts once.
    same_round = [("t1", "round", ""), ("t2", "tally", "B-1=FIXED Q-1=OPTION"),
                  ("t3", "tally", "B-1=FIXED Q-1=OPTION")]
    try:
        got, lines = outcomes(rp, same_round)
    except Exception as exc:  # noqa: BLE001
        got, lines = {"error": repr(exc)}, 0
    check("a line restated within one round counts once",
          got == {"thermo-bugs": {"FIXED": 1}, "thermo-quality": {"OPTION": 1}} and lines == 1, got)

    # Events arrive per transcript (main session, then worker files): order is by timestamp.
    shuffled = [("t4", "tally", "CX-2=SKIPPED"), ("t1", "round", ""), ("t3", "round", ""),
                ("t2", "tally", "CX-2=SKIPPED")]
    try:
        got, lines = outcomes(rp, shuffled)
    except Exception as exc:  # noqa: BLE001
        got, lines = {"error": repr(exc)}, 0
    check("events are ordered by time before rounds split them", got.get("review-cross") == {"SKIPPED": 2}, got)

    # CX-4 / B-6: only a real runner invocation opens a round. Reading the script, grepping it or a
    # dry run between a fixer's TALLY and its restatement used to count the outcome twice.
    def collect(recs: list[dict]) -> tuple[dict, int]:
        class Stream:
            def records(self, _t: object) -> list[dict]:
                return recs

            def api_call(self, _rec: dict) -> None:
                return None

        c = rp.Collector(Stream())
        c.main_session(types.SimpleNamespace(session="s1"))
        by_label, lines = rp.tally_attribution(c.tally_lines)
        return {k: dict(v) for k, v in by_label.items()}, lines

    def bash(ts: str, cmd: str) -> dict:
        return {"type": "assistant", "timestamp": ts,
                "message": {"content": [{"type": "tool_use", "name": "Bash", "id": ts, "input": {"command": cmd}}]}}

    def say(ts: str, text: str) -> dict:
        return {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "text", "text": text}]}}

    got, lines = collect([
        bash("t1", '/Users/u/.agents/bin/agent-run review-cross /r --objective "x"'), say("t2", "TALLY CX-1=FIXED"),
        bash("t3", "cat ~/.agents/bin/agent-run"), bash("t4", "grep -n fail /Users/u/.agents/bin/agent-run"),
        bash("t5", "~/.agents/bin/agent-run review-cross /r --dry-run"), bash("t6", "~/.agents/bin/codex-review-old /r"),
        say("t7", "TALLY CX-1=FIXED"),
    ])
    check("CX-4: inspecting or dry-running the runner opens no round: a restated TALLY counts once",
          got == {"review-cross": {"FIXED": 1}} and lines == 1, got)
    got, lines = collect([
        bash("t1", "$HOME/.agents/bin/agent-run review-cross /r"), say("t2", "TALLY CX-1=FIXED"),
        bash("t3", "cd /r && CODEX_BIN=x ~/.agents/bin/agent-run review-cross . --since abc"), say("t4", "TALLY CX-1=FIXED"),
    ])
    check("...a real run after `cd ... &&` with an env prefix opens the next round", got == {"review-cross": {"FIXED": 2}}, got)
    got, lines = outcomes(rp, [("", "tally", "B-1=FIXED"), ("t1", "round", ""), ("t2", "tally", "B-1=FIXED")])
    check("B-6: an event with no timestamp is skipped, not counted before every round", got == {"thermo-bugs": {"FIXED": 1}}, got)

    check("CX- is attributed to the review-cross role", rp.TALLY_PREFIX.get("CX") == "review-cross", rp.TALLY_PREFIX)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "roles.toml"
        p.write_text('[roles.main]\nmodel = "anthropic:opus"\neffort = "high"\n'
                     '[roles.second-opinion]\nmodel = "anthropic:opus"\neffort = "high"\nprefix = "CX"\n')
        fn = getattr(rp, "role_prefixes", None)
        mapped = fn(p) if fn else {}
        check("a renamed role keeps its prefix: CX- follows roles.toml", mapped.get("CX") == "second-opinion", mapped)

    logger.info("%d failure(s)", len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
