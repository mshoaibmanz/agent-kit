#!/usr/bin/env python3
"""Verification checks (README "Verification"): a correctness lint per edit, a type gate at Stop and
pre-push that reports only errors on the lines the branch added or changed, and a new worktree's
pyrightconfig.json. Every check skips silently when its tool is missing.

    verify.py edit                       PostToolUse payload on stdin -> hookSpecificOutput JSON or nothing
    verify.py gate <root> stop           the working tree's tracked changes; exit 1 with errors on them
    verify.py gate <root> push [<sha>]   the pushed commit (default HEAD); exit 1 with errors on its changes
    verify.py worktree <path>            copy the main checkout's untracked pyrightconfig.json, venv pinned

The overlay key VERIFY_OFF turns checks off: space-separated <repo-glob>:<what>, what being
python.edit, python.stop, typescript.edit, typescript.stop or worktree (globs allowed on both sides).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fnmatch import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, NamedTuple

from kit_env import kit_env

LIB = Path(__file__).resolve().parent
CAP = 10
EDIT_TIMEOUT = 10
DEADLINE = {"stop": 40.0, "push": 90.0}
STALE_SECONDS = 14 * 86400
PY = (".py", ".pyi")
TS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# Correctness rules only. Unused imports and variables stay out: an edit-by-edit flow adds the import
# before the edit that uses it. B008 flags FastAPI's Depends() defaults.
RUFF_KEEP = re.compile(r"F\d|E9\d|B\d|invalid-syntax$")
RUFF_DROP = {"F401", "F841", "B008"}
# An unresolved import says the environment is incomplete, not that the change is wrong.
PYRIGHT_DROP = {"reportMissingImports", "reportMissingModuleSource"}
ESLINT_CONFIGS = tuple(f"eslint.config.{e}" for e in ("js", "mjs", "cjs", "ts", "mts", "cts")) + tuple(
    f".eslintrc{e}" for e in ("", ".js", ".cjs", ".json", ".yml", ".yaml")
)
TSC_LINE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),\d+\): error (?P<code>TS\d+): (?P<msg>.*)$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class Diag(NamedTuple):
    file: str
    line: int
    code: str
    message: str

    def key(self) -> tuple[str, str, str]:
        return self.file, self.code, self.message

    def text(self, with_file: bool = True) -> str:
        where = f"{self.file}:{self.line}" if with_file else f"L{self.line}"
        return f"  {where} {self.code}: {self.message.splitlines()[0] if self.message else ''}"


class Skip(NamedTuple):
    reason: str
    once: bool = False  # an environment fact, said once per worktree; a timeout is said every time


def run(argv: list[str], cwd: str | Path | None = None, stdin: str | None = None,
        timeout: float = EDIT_TIMEOUT) -> tuple[int, str]:
    """(exit code, stdout); stderr is dropped. 127 when the tool cannot start, 124 on timeout."""
    if timeout <= 0:
        return 124, ""
    try:
        proc = subprocess.run(argv, cwd=cwd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=timeout, check=False)
    except OSError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, proc.stdout


def git(root: str | Path, *args: str) -> str:
    rc, out = run(["git", "-C", str(root), *args], timeout=60)
    return out.strip() if rc == 0 else ""


def git_top(path: str | Path) -> str:
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    return git(folder or ".", "rev-parse", "--show-toplevel")


def main_checkout(root: str | Path) -> str:
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.dirname(common) if common else ""


def tracked(root: str | Path, name: str) -> bool:
    return run(["git", "-C", str(root), "ls-files", "--error-unmatch", name], timeout=5)[0] == 0


def is_off(root: str | Path, what: str) -> bool:
    """VERIFY_OFF names this repo (the main checkout's folder name) and what."""
    repo = os.path.basename(main_checkout(root) or str(root))
    for entry in kit_env().get("VERIFY_OFF", "").split():
        glob, _, target = entry.partition(":")
        if fnmatch(repo, glob) and fnmatch(what, target or "*"):
            return True
    return False


def ancestors(start: str | Path, stop: str | Path) -> Iterator[Path]:
    """start, then each parent up to and including stop (or the filesystem root)."""
    folder, top = Path(start).resolve(), Path(stop).resolve()
    while True:
        yield folder
        if folder == top or folder.parent == folder:
            return
        folder = folder.parent


def which_up(name: str, start: str | Path, stop: str | Path) -> str | None:
    """node_modules/.bin/<name> or .venv/bin/<name> from start up to stop, else PATH."""
    for folder in ancestors(start, stop):
        for sub in ("node_modules/.bin", ".venv/bin"):
            if os.access(folder / sub / name, os.X_OK):
                return str(folder / sub / name)
    return shutil.which(name)


def find_up(start: str | Path, stop: str | Path, names: tuple[str, ...]) -> Path | None:
    return next((f / n for f in ancestors(start, stop) for n in names if (f / n).is_file()), None)


def has_ruff_config(path: str, top: str) -> bool:
    for folder in ancestors(Path(path).parent, top):
        if (folder / "ruff.toml").is_file() or (folder / ".ruff.toml").is_file():
            return True
        pyproject = folder / "pyproject.toml"
        if pyproject.is_file() and re.search(r"^\[tool\.ruff", pyproject.read_text(errors="replace"), re.M):
            return True
    return False


def new_diags(after: list[Diag], before: list[Diag]) -> list[Diag]:
    """after's diagnostics beyond before's, matched on file, code and message (line shifts ignored),
    in line order, one per line, code and message."""
    left = Counter(d.key() for d in before)
    out, seen = [], set()
    for d in sorted(after, key=lambda d: (d.file, d.line)):
        if left[d.key()] > 0:
            left[d.key()] -= 1
        elif d not in seen:
            seen.add(d)
            out.append(d)
    return out


def capped(diags: list[Diag], with_file: bool = True) -> str:
    lines = [d.text(with_file) for d in diags[:CAP]]
    if len(diags) > CAP:
        lines.append(f"  ... {len(diags) - CAP} more")
    return "\n".join(lines)


def compare(check: Callable[[str], list[Diag] | None], path: str, old: str) -> list[Diag] | None:
    """check on the file as written and on its text before the edit, side by side: the errors the
    edit added. None when either run gave no report."""
    with ThreadPoolExecutor(2) as pool:
        after, before = pool.submit(check, Path(path).read_text(errors="replace")), pool.submit(check, old)
        now, then = after.result(), before.result()
    return None if now is None or then is None else new_diags(now, then)


def edit_python(path: str, top: str, old: str) -> list[Diag] | None:
    """ruff with the repo's own config when it has one (fixed and formatted with it too), else
    F/E9/B; either way only RUFF_KEEP codes, syntax errors included, reach the model."""
    ruff = which_up("ruff", Path(path).parent, top)
    if not ruff:
        return None
    configured = has_ruff_config(path, top)
    if configured:
        run([ruff, "check", "--fix", "--quiet", path], cwd=top)
        run([ruff, "format", "--quiet", path], cwd=top)
    select = [] if configured else ["--select", "F,E9,B"]

    def check(text: str) -> list[Diag]:
        rc, out = run([ruff, "check", "--output-format", "json", "--no-fix", "--exit-zero", *select,
                       "--stdin-filename", path, "-"], cwd=top, stdin=text)
        try:
            rows = json.loads(out) if rc == 0 else []
        except ValueError:
            return []
        diags = [Diag(path, r["location"]["row"], r.get("code") or "invalid-syntax", r.get("message", "")) for r in rows]
        return [d for d in diags if RUFF_KEEP.match(d.code) and d.code not in RUFF_DROP]

    return compare(check, path, old)


def edit_typescript(path: str, top: str, old: str) -> list[Diag] | None:
    """The repo's eslint, errors only, on the file before and after the edit (stdin, named as the real
    path so its config applies): an error is new when the old text did not have it, wherever it sits."""
    config = find_up(Path(path).parent, top, ESLINT_CONFIGS)
    eslint = which_up("eslint", config.parent, top) if config else None
    if not config or not eslint:
        return None

    def check(text: str) -> list[Diag] | None:
        _, out = run([eslint, "--format", "json", "--stdin", "--stdin-filename", path], cwd=config.parent, stdin=text)
        try:
            files = json.loads(out)
        except ValueError:
            return None
        return [Diag(path, m.get("line", 0), m.get("ruleId") or "parse", m.get("message", ""))
                for f in files for m in f.get("messages", []) if m.get("severity") == 2]

    return compare(check, path, old)


EDIT: dict[str, tuple[tuple[str, ...], Callable[[str, str, str], list[Diag] | None]]] = {
    "python": (PY, edit_python), "typescript": (TS, edit_typescript)}


def old_text(payload: dict[str, Any], path: str, top: str) -> str | None:
    """The file before this edit: the tool's own record (originalFile, None for a new file), else the
    committed copy when the file is tracked, else None (nothing to compare against)."""
    response = payload.get("tool_response")
    if isinstance(response, dict):
        if isinstance(response.get("originalFile"), str):
            return response["originalFile"]
        if response.get("type") == "create":
            return ""
    rel_path = os.path.relpath(os.path.realpath(path), os.path.realpath(top))
    rc, out = run(["git", "-C", top, "show", f"HEAD:{rel_path}"], timeout=5)
    return out if rc == 0 else None


def cmd_edit() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    lang = next((name for name, (exts, _) in EDIT.items() if path.endswith(exts)), "")
    top = git_top(path) if lang and os.path.isfile(path) else ""
    if not top or is_off(top, f"{lang}.edit"):
        return 0
    old = old_text(payload, path, top)
    fresh = EDIT[lang][1](path, top, old) if old is not None else None
    if not fresh:
        return 0
    noun = "error" if len(fresh) == 1 else "errors"
    text = f"verify: this edit introduced {len(fresh)} {noun} in {os.path.relpath(path, top)}:\n{capped(fresh, False)}"
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}, sys.stdout)
    return 0


def python_env(root: str | Path) -> Path | None:
    """The environment imports resolve from: pyrightconfig.json's venvPath/venv, else .venv or venv,
    in the checkout and then in the main checkout (a worktree has neither of its own)."""
    for top in dict.fromkeys([str(root), main_checkout(root) or str(root)]):
        cfg = read_json(Path(top) / "pyrightconfig.json")
        candidates = [Path(top, cfg.get("venvPath", "."), cfg["venv"])] if cfg.get("venv") else []
        for env in candidates + [Path(top, ".venv"), Path(top, "venv")]:
            if (env / "bin/python").exists():
                return env.resolve()
    return None


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pinned_config(text: str, env: Path | None) -> str:
    """A pyrightconfig.json text whose venvPath/venv name env by absolute path, so the copy resolves
    the same imports wherever it lands. A config that is not plain JSON is kept as it is."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict) or env is None:
        return text
    data.update(venvPath=str(env.parent), venv=env.name)
    return json.dumps(data, indent=2) + "\n"


def cmd_worktree(path: str) -> int:
    top = git_top(path)
    main = main_checkout(path) if top else ""
    if not top or not main or os.path.realpath(main) == os.path.realpath(top) or is_off(top, "worktree"):
        return 0
    src, dst = Path(main, "pyrightconfig.json"), Path(top, "pyrightconfig.json")
    if src.is_file() and not tracked(main, "pyrightconfig.json") and not dst.exists():
        dst.write_text(pinned_config(src.read_text(), python_env(main)))
    return 0


@dataclass
class Gate:
    """One gate run: the checkout, the tree checked (the checkout, or the pushed commit's files), the
    lines the branch added or changed per file, and the deadline every checker shares."""
    root: str
    tree: Path
    added: dict[str, set[int]]
    cache: Path
    deadline: float

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def on_added(self, diags: list[Diag]) -> list[Diag]:
        return sorted({d for d in diags if d.line in self.added.get(d.file, ())})


def fork_point(root: str, rev: str) -> str:
    """Where rev's branch left its base: review-state's _rv_fork (the nearest base branch)."""
    rc, out = run(["bash", "-c", '. "$1/review-state" >/dev/null 2>&1 && _rv_fork "$2" "$3"', "_", str(LIB), root, rev],
                  timeout=30)
    mb = out.strip() if rc == 0 else ""
    return mb if mb and git(root, "rev-parse", "-q", "--verify", f"{mb}^{{commit}}") else ""


def added_lines(root: str, *revs: str) -> dict[str, set[int]]:
    """The new-side line numbers each file's diff adds or rewrites (`git diff -U0`), deletions left out."""
    out = git(root, "-c", "core.quotePath=off", "diff", "-U0", "-M", "--no-prefix", "--no-color", "--no-ext-diff",
              "--diff-filter=d", *revs)
    added: dict[str, set[int]] = {}
    path, header = "", False
    for row in out.splitlines():
        if row.startswith("diff --git "):
            header = True
        elif header and row.startswith("+++ "):
            path = row[4:]
        elif hunk := HUNK.match(row):
            header = False
            start, count = int(hunk[1]), int(hunk[2] or 1)
            if count:
                added.setdefault(path, set()).update(range(start, start + count))
    return added


def cache_dir(root: str) -> Path:
    """Per worktree: its gate state, tsc build info and the pushed trees extracted for it."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "agent-kit/verify"
    folder = base / hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:16]
    if not folder.is_dir():
        for old in base.glob("*") if base.is_dir() else []:
            if time.time() - old.stat().st_mtime > STALE_SECONDS:
                shutil.rmtree(old, ignore_errors=True)
        folder.mkdir(parents=True)
    os.utime(folder)
    return folder


def extract(root: str, rev: str, dest: Path) -> bool:
    dest.mkdir(parents=True)
    with tempfile.TemporaryFile() as spool:
        if subprocess.run(["git", "-C", root, "archive", "--format=tar", rev], stdout=spool, check=False).returncode:
            return False
        spool.seek(0)
        with tarfile.open(fileobj=spool) as tar:
            tar.extractall(dest, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(dest)
    return True


def provision(tree: Path, root: str) -> None:
    """An extracted commit keeps its own pyrightconfig.json with only the environment pinned; the
    checkout's copy goes in only when it is untracked. node_modules is linked from the checkout."""
    cfg = tree / "pyrightconfig.json"
    src = cfg if cfg.is_file() else Path(root, "pyrightconfig.json")
    if src.is_file() and (src == cfg or not tracked(root, "pyrightconfig.json")):
        cfg.write_text(pinned_config(src.read_text(), python_env(root)))
    modules = Path(root, "node_modules")
    if modules.is_dir() and not (tree / "node_modules").exists():
        (tree / "node_modules").symlink_to(modules.resolve())


def rel(path: str, base: str | Path) -> str:
    """path relative to base, whichever of base and its resolved form contains it."""
    if not os.path.isabs(path):
        return path
    for b in (str(base), os.path.realpath(base)):
        candidate = os.path.relpath(path, b)
        if not candidate.startswith(".."):
            return candidate
    return os.path.relpath(os.path.realpath(path), os.path.realpath(base))


def python_tools(root: str) -> tuple[str | None, Path | None]:
    """The type checker (basedpyright, else pyright) and the environment imports resolve from."""
    checker = which_up("basedpyright", root, root) or which_up("pyright", root, root)
    return checker, python_env(root) if checker else None


def gate_python(g: Gate, files: list[str]) -> tuple[str, list[Diag]] | Skip:
    checker, env = python_tools(g.root)
    if not checker:
        return "pyright", []
    if env is None:
        return Skip("Python type gate skipped: no environment resolves (pyrightconfig.json venv, .venv or venv)", once=True)
    name = Path(checker).name
    rc, out = run([checker, "--pythonpath", str(env / "bin/python"), "--outputjson", "--level", "error", *files],
                  cwd=g.tree, timeout=g.left())
    if rc == 124:
        return Skip(f"{name} did not finish within the deadline")
    try:
        report = json.loads(out[out.find("{"):])
    except ValueError:
        return Skip(f"{name} gave no report (exit {rc})")
    return name, g.on_added([
        Diag(rel(d["file"], g.tree), d.get("range", {}).get("start", {}).get("line", 0) + 1, d.get("rule") or "error",
             d.get("message", ""))
        for d in report.get("generalDiagnostics", [])
        if d.get("severity") == "error" and d.get("rule") not in PYRIGHT_DROP])


def ts_projects(g: Gate, files: list[str]) -> dict[str, str | None]:
    """Each changed file's nearest tsconfig.json (relative to the tree) and the tsc that checks it."""
    projects: dict[str, str | None] = {}
    for f in files:
        tsconfig = find_up(Path(g.tree, f).parent, g.tree, ("tsconfig.json",))
        if tsconfig is not None:
            name = rel(str(tsconfig), g.tree)
            projects[name] = which_up("tsc", Path(g.root, name).parent, g.root)
    return projects


def gate_typescript(g: Gate, files: list[str]) -> tuple[str, list[Diag]] | Skip:
    found: list[Diag] = []
    for name, checker in ts_projects(g, files).items():
        if not checker:
            continue
        tsconfig = g.tree / name
        buildinfo = g.cache / f"tsc-{hashlib.sha256(name.encode()).hexdigest()[:12]}.tsbuildinfo"
        rc, out = run([checker, "--noEmit", "--pretty", "false", "--incremental", "--tsBuildInfoFile", str(buildinfo),
                       "-p", str(tsconfig)], cwd=tsconfig.parent, timeout=g.left())
        if rc == 124:
            return Skip("tsc did not finish within the deadline")
        found += [Diag(rel(os.path.normpath(os.path.join(tsconfig.parent, m["file"])), g.tree), int(m["line"]),
                       m["code"], m["msg"])
                  for m in (TSC_LINE.match(line.strip()) for line in out.splitlines()) if m]
    return "tsc", g.on_added(found)


GATES: dict[str, tuple[tuple[str, ...], Callable[[Gate, list[str]], tuple[str, list[Diag]] | Skip]]] = {
    "python": (PY, gate_python), "typescript": (TS, gate_typescript)}


def fingerprint(g: Gate, base: str, todo: dict[str, list[str]]) -> str:
    """What a gate result depends on: the base, the checkers and environment found, the configs, and
    the changed files' lines and content. Neither mode nor commit: a push of the tree a Stop checked
    reuses its result, and installing a checker or creating an environment changes it."""
    files = sorted(f for fs in todo.values() for f in fs)
    facts: list[Any] = [base, sorted(todo), [[f, sorted(g.added[f])] for f in files]]
    configs = list(files)
    if "python" in todo:
        facts.append([str(t) for t in python_tools(g.root)])
        configs += ["pyrightconfig.json", "pyproject.toml"]
    if "typescript" in todo:
        projects = ts_projects(g, todo["typescript"])
        facts.append(sorted(projects.items()))
        configs += sorted(projects)
    digest = hashlib.sha256(json.dumps(facts).encode())
    for name in configs:
        path = g.tree / name
        digest.update(name.encode() + b"\0" + (path.read_bytes() if path.is_file() else b"-") + b"\0")
    return digest.hexdigest()


def cmd_gate(root: str, mode: str, rev: str) -> int:
    """Prints the errors on lines the branch added or changed and exits 1, else exits 0. A check that
    did not finish is said on stderr and never recorded as passed; an environment fact (no Python
    environment) is said once, at the push. At Stop, a tree whose errors were already shown passes."""
    deadline = time.monotonic() + float(os.environ.get("VERIFY_DEADLINE") or DEADLINE[mode])
    root = git_top(root)
    head = git(root, "rev-parse", "-q", "--verify", f"{rev or 'HEAD'}^{{commit}}") if root else ""
    base = fork_point(root, head) if head else ""
    if not base:
        return 0
    added = added_lines(root, base) if mode == "stop" else added_lines(root, base, head)
    todo = {lang: [f for f in sorted(added) if f.endswith(exts)] for lang, (exts, _) in GATES.items()
            if not is_off(root, f"{lang}.stop")}
    todo = {lang: files for lang, files in todo.items() if files}
    if not todo:
        return 0
    g = Gate(root, Path(root), added, cache_dir(root), deadline)
    state_file = g.cache / "gate.json"
    state = read_json(state_file)
    with tempfile.TemporaryDirectory(dir=g.cache) as scratch:
        clean = not git(root, "status", "--porcelain", "--untracked-files=no")
        if mode == "push" and not (head == git(root, "rev-parse", "HEAD") and clean):
            g.tree = Path(scratch, "tree")
            if not extract(root, head, g.tree):
                return 0
            provision(g.tree, root)
        key = fingerprint(g, base, todo)
        if state.get("key") != key:
            parts, skips = [], []
            for lang, files in todo.items():
                found = GATES[lang][1](g, files)
                if isinstance(found, Skip):
                    skips.append(found)
                elif found[1]:
                    noun = "error" if len(found[1]) == 1 else "errors"
                    parts.append(f"{found[0]}: {len(found[1])} {noun} on lines this branch added or changed:\n"
                                 f"{capped(found[1])}")
            for skip in skips:
                if not skip.once:
                    sys.stderr.write(f"verify: {skip.reason}; not checked.\n")
            state = {"key": key if all(s.once for s in skips) else "", "text": "\n".join(parts), "shown": False,
                     "once": [s.reason for s in skips if s.once], "noted": state.get("noted", [])}
    # Stop's stderr reaches no one (review-trigger drops it), so only the push marks a fact as said.
    noted = set(state.get("noted", []))
    for reason in state.get("once", []):
        if reason not in noted:
            sys.stderr.write(f"verify: {reason}; not checked.\n")
            if mode == "push":
                noted.add(reason)
    state["noted"] = sorted(noted)
    return report(state, state_file, mode)


def report(state: dict[str, Any], state_file: Path, mode: str) -> int:
    shown = state.get("shown", False)
    state["shown"] = shown or mode == "stop"
    state_file.write_text(json.dumps(state))
    if not state.get("text") or (mode == "stop" and shown):
        return 0
    sys.stdout.write(state["text"] + "\n")
    return 1


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["edit"]:
            return cmd_edit()
        if argv[:1] == ["gate"] and len(argv) >= 3 and argv[2] in DEADLINE:
            return cmd_gate(argv[1], argv[2], argv[3] if len(argv) > 3 else "")
        if argv[:1] == ["worktree"] and len(argv) == 2:
            return cmd_worktree(argv[1])
    except OSError:
        # A check that cannot read or write its files never blocks an edit, a Stop or a push.
        return 0
    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
