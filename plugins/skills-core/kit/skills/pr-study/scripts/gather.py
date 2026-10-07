#!/usr/bin/env python3
"""Gather a PR or branch diff into a pr-study state dir. Stdlib only.

Emits meta.json, diff.patch, files.json and stats.json so every figure in the
study artifact is generated from the diff rather than transcribed by hand.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")

GENERATED_PATTERNS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock",
    "Cargo.lock", "go.sum", ".pb.go", "_pb2.py", "__generated__", ".generated.",
    "schema.graphql", "openapi.json", "swagger.json",
)
TEST_PATTERNS = ("/tests/", "/test/", "test_", "_test.", ".test.", ".spec.", "conftest.py")
CONFIG_SUFFIXES = (".yml", ".yaml", ".toml", ".ini", ".cfg", ".env", ".properties")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
MIGRATION_PATTERNS = ("/migrations/", "/alembic/", "/versions/")


def run(cmd: list[str], check: bool = True) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.stdout


def classify(path: str) -> str:
    low = path.lower()
    if any(p in low for p in GENERATED_PATTERNS):
        return "generated"
    if any(p in low for p in MIGRATION_PATTERNS):
        return "migration"
    if any(p in low for p in TEST_PATTERNS):
        return "test"
    if low.endswith(DOC_SUFFIXES):
        return "doc"
    if low.endswith(CONFIG_SUFFIXES) or low.endswith(".json"):
        return "config"
    return "source"


def normalise(line: str) -> str:
    return re.sub(r"\s+", "", line[1:])


def parse_diff(diff: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None

    def close_hunk() -> None:
        if current is None or hunk is None:
            return
        added = hunk.pop("_added")
        removed = hunk.pop("_removed")
        hunk["added"] = len(added)
        hunk["removed"] = len(removed)
        hunk["whitespace_only"] = bool(added or removed) and (
            collections.Counter(normalise(x) for x in added)
            == collections.Counter(normalise(x) for x in removed)
        )
        hunk["import_only"] = bool(added or removed) and all(
            re.match(r"^[+-]\s*(import |from .+ import |#include|use )", x)
            or not x[1:].strip()
            for x in added + removed
        )
        current["hunks"].append(hunk)

    for line in diff.splitlines():
        m = DIFF_GIT_RE.match(line)
        if m:
            close_hunk()
            hunk = None
            current = {"path": m.group(2), "old_path": m.group(1), "hunks": [],
                       "kind": classify(m.group(2)), "binary": False, "status": "modified"}
            files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current["status"] = "added"
        elif line.startswith("deleted file mode"):
            current["status"] = "deleted"
        elif line.startswith("rename from"):
            current["status"] = "renamed"
        elif line.startswith("Binary files"):
            current["binary"] = True
        m = HUNK_RE.match(line)
        if m:
            close_hunk()
            hunk = {"header": m.group(5).strip(), "old_start": int(m.group(1)),
                    "new_start": int(m.group(3)), "_added": [], "_removed": []}
            continue
        if hunk is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            hunk["_added"].append(line)
        elif line.startswith("-") and not line.startswith("---"):
            hunk["_removed"].append(line)

    close_hunk()
    for f in files:
        f["added"] = sum(h["added"] for h in f["hunks"])
        f["removed"] = sum(h["removed"] for h in f["hunks"])
        f["churn"] = f["added"] + f["removed"]
        f["hunk_count"] = len(f["hunks"])
        f["mechanical"] = bool(f["hunks"]) and all(
            h["whitespace_only"] or h["import_only"] for h in f["hunks"]
        )
    return files


def build_stats(files: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, dict[str, int]] = collections.defaultdict(
        lambda: {"files": 0, "added": 0, "removed": 0, "churn": 0, "hunks": 0}
    )
    for f in files:
        b = by_kind[f["kind"]]
        b["files"] += 1
        b["added"] += f["added"]
        b["removed"] += f["removed"]
        b["churn"] += f["churn"]
        b["hunks"] += f["hunk_count"]

    total_churn = sum(f["churn"] for f in files) or 1
    substantive = [f for f in files if f["kind"] == "source" and not f["mechanical"]]
    substantive_churn = sum(f["churn"] for f in substantive)

    return {
        "files": len(files),
        "added": sum(f["added"] for f in files),
        "removed": sum(f["removed"] for f in files),
        "churn": total_churn,
        "hunks": sum(f["hunk_count"] for f in files),
        "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "mechanical_files": sum(1 for f in files if f["mechanical"]),
        "substantive_files": len(substantive),
        "substantive_churn": substantive_churn,
        "signal_pct": round(100 * substantive_churn / total_churn, 1),
        "top_files": [
            {"path": f["path"], "churn": f["churn"], "kind": f["kind"],
             "hunks": f["hunk_count"], "mechanical": f["mechanical"]}
            for f in sorted(files, key=lambda x: -x["churn"])[:15]
        ],
    }


def detect_base() -> str:
    out = run(["gh", "pr", "list", "--state", "merged", "--limit", "20",
               "--json", "baseRefName"], check=False)
    try:
        refs = [p["baseRefName"] for p in json.loads(out or "[]")]
    except json.JSONDecodeError:
        refs = []
    if refs:
        return f"origin/{collections.Counter(refs).most_common(1)[0][0]}"
    head = run(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], check=False).strip()
    return head.replace("refs/remotes/", "") if head else "origin/main"


def divergence(base: str, head: str) -> dict[str, Any]:
    """Behind/ahead counts, or a reason they could not be computed. Never raises."""
    for b, h in ((base, head), (f"origin/{base}", f"origin/{head}")):
        out = run(["git", "rev-list", "--left-right", "--count", f"{b}...{h}"], check=False).split()
        if len(out) == 2:
            behind, ahead = int(out[0]), int(out[1])
            return {"behind_base": behind, "ahead_of_base": ahead, "stale_base": behind > 20,
                    "compared": f"{b}...{h}"}
    return {"behind_base": None, "ahead_of_base": None, "stale_base": None,
            "compared": None, "divergence_error": "refs not available locally; run git fetch"}


def code_location(head_ref: str) -> dict[str, Any]:
    """Where the PR's code actually is on this machine. The wrong tree is a wrong review."""
    current = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
    if head_ref and current == head_ref:
        return {"code_at": run(["git", "rev-parse", "--show-toplevel"], check=False).strip(),
                "checkout_matches_pr": True, "checkout_branch": current}
    # a worktree on a differently named local branch that sits at the PR head is still the PR
    head_sha = run(["git", "rev-parse", f"origin/{head_ref}"], check=False).strip() if head_ref else ""
    if not (len(head_sha) == 40 and all(c in "0123456789abcdef" for c in head_sha)):
        head_sha = ""
    for line in run(["git", "worktree", "list"], check=False).splitlines():
        parts = line.split()
        by_name = bool(head_ref) and f"[{head_ref}]" in line
        by_sha = len(parts) > 1 and bool(head_sha) and head_sha.startswith(parts[1])
        if by_name or by_sha:
            out = {"code_at": parts[0], "checkout_matches_pr": False, "checkout_branch": current}
            if by_sha and not by_name:
                out["code_at_note"] = "matched by SHA; the worktree's branch name differs from the PR head"
            return out
    return {"code_at": None, "checkout_matches_pr": False, "checkout_branch": current,
            "code_location_error": f"no checkout or worktree on {head_ref!r}"}


def gather_pr(number: str) -> tuple[dict[str, Any], str]:
    fields = ("title,body,author,baseRefName,headRefName,state,additions,deletions,"
              "changedFiles,labels,url,createdAt,commits,reviewDecision")
    meta = json.loads(run(["gh", "pr", "view", number, "--json", fields]))
    meta["scope_mode"] = "pr"
    meta["scope_ref"] = number
    meta["base"] = meta.get("baseRefName", "")
    meta["commit_count"] = len(meta.pop("commits", []) or [])
    meta.update(divergence(meta["base"], meta.get("headRefName", "")))
    meta.update(code_location(meta.get("headRefName", "")))
    return meta, run(["gh", "pr", "diff", number])


def gather_branch(ref: str | None, base: str | None) -> tuple[dict[str, Any], str]:
    head = ref or run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
    base = base or detect_base()
    div = divergence(base, head)
    log = run(["git", "log", "--no-decorate", "--pretty=%h %s", f"{base}..{head}"]).strip()
    meta = {
        "scope_mode": "branch",
        "scope_ref": head,
        "base": base,
        "headRefName": head,
        "baseRefName": base,
        "commit_count": div["ahead_of_base"],
        "commit_subjects": log.splitlines(),
        **div,
        **code_location(head),
    }
    return meta, run(["git", "diff", f"{base}...{head}"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", nargs="?", help="PR number, or branch name; omit for current branch")
    ap.add_argument("--base", help="override base ref (branch mode)")
    ap.add_argument("--out", required=True, help="state directory to write into")
    args = ap.parse_args()

    target = (args.target or "").strip().lstrip("#")
    url = re.match(r"^https?://[^/]+/[^/]+/[^/]+/pull/(\d+)", target)
    if url:
        target = url.group(1)
    if target.isdigit():
        meta, diff = gather_pr(target)
    else:
        meta, diff = gather_branch(target or None, args.base)

    files = parse_diff(diff)
    stats = build_stats(files)
    meta["repo"] = run(["git", "rev-parse", "--show-toplevel"], check=False).strip()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "diff.patch").write_text(diff)
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    (out / "files.json").write_text(json.dumps(files, indent=2))
    (out / "stats.json").write_text(json.dumps(stats, indent=2))

    print(json.dumps({"out": str(out), "meta": {k: meta[k] for k in
                      ("scope_mode", "scope_ref", "base", "behind_base", "ahead_of_base",
                       "stale_base", "divergence_error", "code_at", "checkout_matches_pr",
                       "code_location_error") if k in meta},
                      "stats": {k: stats[k] for k in
                                ("files", "added", "removed", "hunks", "signal_pct",
                                 "substantive_files", "mechanical_files")}}, indent=2))
    if not meta.get("checkout_matches_pr", True):
        loc = meta.get("code_at") or "NOWHERE — git fetch the branch, or add a worktree for it"
        print(f"WARNING: this checkout is on {meta.get('checkout_branch')!r}, not the PR's "
              f"{meta.get('headRefName')!r}. Read the PR's code from: {loc}\n"
              f"         Reading the current checkout returns PRE-change code with no error, "
              f"and every conclusion from it is wrong. Pass this path to subagents too.",
              file=sys.stderr)
    if meta.get("stale_base"):
        print(f"WARNING: base is {meta['behind_base']} commits ahead of this branch "
              f"({meta.get('compared')}). The diff and any CI verdict are against a stale tree.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
