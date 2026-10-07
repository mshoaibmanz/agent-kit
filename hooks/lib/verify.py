#!/usr/bin/env python3
"""Verification checks (README "Verification"): a correctness lint per edit, a type gate at Stop and
pre-push that reports only errors the branch introduced, and a new worktree's pyrightconfig.json.
Every check skips silently when its tool is missing.

    verify.py edit                       PostToolUse payload on stdin -> hookSpecificOutput JSON or nothing
    verify.py gate <root> stop           the working tree's tracked changes; exit 1 with new errors
    verify.py gate <root> push [<sha>]   the pushed commit's content (default HEAD); exit 1 with new errors
    verify.py worktree <path>            copy the main checkout's untracked pyrightconfig.json, venv pinned

The overlay key VERIFY_OFF turns checks off: space-separated <repo-glob>:<what>, what being
python.edit, python.stop, typescript.edit, typescript.stop or worktree (globs allowed on both sides).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import difflib
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

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
try:
    import kit_env
except ImportError:
    kit_env = None  # type: ignore[assignment]

LIB = Path(__file__).resolve().parent
CAP = 10
EDIT_TIMEOUT = 10
DEADLINE = {"stop": 40.0, "push": 240.0}
STALE_SECONDS = 14 * 86400
PY = (".py", ".pyi")
TS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# Correctness rules only. Unused imports and variables stay out: an edit-by-edit flow adds the import
# before the edit that uses it. B008 flags FastAPI's Depends() defaults.
RUFF_KEEP = re.compile(r"(F\d|E9\d|B\d)")
RUFF_DROP = {"F401", "F841", "B008"}
# An unresolved import says the environment is incomplete, not that the change is wrong.
PYRIGHT_DROP = {"reportMissingImports", "reportMissingModuleSource"}
ESLINT_CONFIGS = tuple(f"eslint.config.{e}" for e in ("js", "mjs", "cjs", "ts", "mts", "cts")) + tuple(
    f".eslintrc{e}" for e in ("", ".js", ".cjs", ".json", ".yml", ".yaml")
)
TSC_LINE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),\d+\): error (?P<code>TS\d+): (?P<msg>.*)$")


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


@dataclass(frozen=True)
class Phase:
    lang: str
    name: str
    exts: tuple[str, ...]
    run: Callable[..., Any]


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


def is_off(root: str | Path, what: str) -> bool:
    """VERIFY_OFF names this repo (the main checkout's folder name) and what."""
    spec = kit_env.kit_env().get("VERIFY_OFF", "") if kit_env else ""
    repo = os.path.basename(main_checkout(root) or str(root))
    for entry in spec.split():
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
        elif (d.file, d.line, d.code, d.message) not in seen:
            seen.add((d.file, d.line, d.code, d.message))
            out.append(d)
    return out


def capped(diags: list[Diag], with_file: bool = True) -> str:
    lines = [d.text(with_file) for d in diags[:CAP]]
    if len(diags) > CAP:
        lines.append(f"  ... {len(diags) - CAP} more")
    return "\n".join(lines)


def edit_python(path: str, top: str, old: str) -> list[Diag] | None:
    """ruff with the repo's own config when it has one (fixed and formatted with it too), else
    F/E9/B; either way only RUFF_KEEP codes reach the model."""
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
        return [Diag(path, r["location"]["row"], r.get("code") or "syntax", r.get("message", "")) for r in rows
                if not r.get("code") or (RUFF_KEEP.match(r["code"]) and r["code"] not in RUFF_DROP)]

    with ThreadPoolExecutor(2) as pool:
        after, before = pool.submit(check, Path(path).read_text(errors="replace")), pool.submit(check, old)
        return new_diags(after.result(), before.result())


def changed_lines(old: str, new: str) -> set[int]:
    """1-based lines of new that the edit replaced or inserted."""
    ops = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False).get_opcodes()
    return {j + 1 for tag, _, _, j1, j2 in ops if tag in ("replace", "insert") for j in range(j1, j2)}


def edit_typescript(path: str, top: str, old: str) -> list[Diag] | None:
    """The repo's eslint, errors only, once per edit (a run costs about a second): an error counts
    as introduced when it sits on a line the edit wrote."""
    config = find_up(Path(path).parent, top, ESLINT_CONFIGS)
    eslint = which_up("eslint", config.parent, top) if config else None
    if not config or not eslint:
        return None
    _, out = run([eslint, "--format", "json", path], cwd=config.parent)
    try:
        files = json.loads(out)
    except ValueError:
        return None
    lines = changed_lines(old, Path(path).read_text(errors="replace"))
    return [Diag(path, m.get("line", 0), m.get("ruleId") or "parse", m.get("message", ""))
            for f in files for m in f.get("messages", []) if m.get("severity") == 2 and m.get("line", 0) in lines]


EDIT = (Phase("python", "edit", PY, edit_python), Phase("typescript", "edit", TS, edit_typescript))


def old_text(payload: dict[str, Any], path: str, top: str) -> str | None:
    """The file before this edit: the tool's own record (originalFile, None for a new file), else the
    committed copy when the file is tracked, else None (nothing to compare against)."""
    response = payload.get("tool_response")
    if isinstance(response, dict):
        if isinstance(response.get("originalFile"), str):
            return response["originalFile"]
        if response.get("type") == "create":
            return ""
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(top))
    rc, out = run(["git", "-C", top, "show", f"HEAD:{rel}"], timeout=5)
    return out if rc == 0 else None


def cmd_edit() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    phase = next((p for p in EDIT if path.endswith(p.exts)), None)
    top = git_top(path) if phase and os.path.isfile(path) else ""
    if not phase or not top or is_off(top, f"{phase.lang}.edit"):
        return 0
    old = old_text(payload, path, top)
    fresh = phase.run(path, top, old) if old is not None else None
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
        cfg = read_config(Path(top) / "pyrightconfig.json")
        candidates = [Path(top, cfg.get("venvPath", "."), cfg["venv"])] if cfg.get("venv") else []
        for env in candidates + [Path(top, ".venv"), Path(top, "venv")]:
            if (env / "bin/python").exists():
                return env.resolve()
    return None


def read_config(path: Path) -> dict[str, Any]:
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
    tracked = run(["git", "-C", main, "ls-files", "--error-unmatch", "pyrightconfig.json"], timeout=5)[0] == 0
    if src.is_file() and not tracked and not dst.exists():
        dst.write_text(pinned_config(src.read_text(), python_env(main)))
    return 0


@dataclass
class Gate:
    """One gate run: the checkout, the merge-base, the tree checked (the checkout, or the pushed
    commit's files) and the deadline every checker shares."""
    root: str
    mb: str
    tree: Path
    cache: Path
    deadline: float

    def left(self) -> float:
        return self.deadline - time.monotonic()


def fork_point(root: str, rev: str) -> str:
    """Where rev's branch left its base: review-state's _rv_fork (the nearest base branch)."""
    rc, out = run(["bash", "-c", '. "$1/review-state" >/dev/null 2>&1 && _rv_fork "$2" "$3"', "_", str(LIB), root, rev],
                  timeout=30)
    mb = out.strip() if rc == 0 else ""
    return mb if mb and git(root, "rev-parse", "-q", "--verify", f"{mb}^{{commit}}") else ""


def cache_dir(root: str) -> Path:
    """Per worktree: its baselines and the trees extracted for it are its own."""
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
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tempfile.TemporaryFile() as spool:
        if subprocess.run(["git", "-C", root, "archive", "--format=tar", rev], stdout=spool, check=False).returncode:
            return False
        spool.seek(0)
        with tarfile.open(fileobj=spool) as tar:
            tar.extractall(dest, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(dest)
    return True


def base_tree(g: Gate) -> Path | None:
    """The merge-base's files, extracted once per merge-base."""
    tree = g.cache / f"base-{g.mb}"
    if not (tree / ".verify-ready").is_file():
        for old in g.cache.glob("base-*"):
            shutil.rmtree(old, ignore_errors=True)
        if not extract(g.root, g.mb, tree):
            return None
        (tree / ".verify-ready").write_text(g.mb)
    return tree


def provision(tree: Path, root: str, env: Path | None) -> None:
    """Every run, since the checkout's environment can change: the checkout's pyrightconfig.json with
    its venv pinned, and its node_modules, in an extracted tree."""
    src = Path(root, "pyrightconfig.json")
    if src.is_file():
        (tree / "pyrightconfig.json").write_text(pinned_config(src.read_text(), env))
    modules = Path(root, "node_modules")
    if modules.is_dir() and not (tree / "node_modules").exists():
        (tree / "node_modules").symlink_to(modules.resolve())


def pyright_diags(g: Gate, argv: list[str], cwd: Path, files: list[str]) -> list[Diag] | Skip:
    rc, out = run([*argv, "--outputjson", "--level", "error", *files], cwd=cwd, timeout=g.left())
    if rc == 124:
        return Skip(f"{Path(argv[0]).name} did not finish within the deadline")
    try:
        report = json.loads(out[out.find("{"):])
    except ValueError:
        return Skip(f"{Path(argv[0]).name} gave no report (exit {rc})")
    return [Diag(rel(d["file"], cwd), d.get("range", {}).get("start", {}).get("line", 0) + 1, d.get("rule") or "error",
                 d.get("message", ""))
            for d in report.get("generalDiagnostics", [])
            if d.get("severity") == "error" and d.get("rule") not in PYRIGHT_DROP]


def rel(path: str, base: str | Path) -> str:
    """path relative to base, whichever of base and its resolved form contains it."""
    if not os.path.isabs(path):
        return path
    for b in (str(base), os.path.realpath(base)):
        candidate = os.path.relpath(path, b)
        if not candidate.startswith(".."):
            return candidate
    return os.path.relpath(os.path.realpath(path), os.path.realpath(base))


def gate_python(g: Gate, files: list[str]) -> tuple[str, list[Diag]] | Skip | None:
    checker = which_up("basedpyright", g.root, g.root) or which_up("pyright", g.root, g.root)
    if not checker:
        return None
    env = python_env(g.root)
    if env is None:
        return Skip("Python type gate skipped: no environment resolves (pyrightconfig.json venv, .venv or venv)", once=True)
    based = Path(checker).name == "basedpyright"
    argv = [checker, "--pythonpath", str(env / "bin/python")]
    config = Path(g.root, "pyrightconfig.json")
    tag = hashlib.sha256(f"{checker}\0{env}\0{config.read_text() if config.is_file() else ''}".encode()).hexdigest()[:12]
    store, baseline = g.cache / f"py-{g.mb}-{tag}.json", g.cache / f"py-{g.mb}-{tag}.baseline.json"
    seen: dict[str, list[list[Any]]] = json.loads(store.read_text()) if store.is_file() else {}
    todo = [f for f in files if f not in seen]
    if todo:
        tree = base_tree(g)
        if tree is None:
            return Skip("could not extract the merge-base")
        provision(tree, g.root, env)
        present = [f for f in todo if (tree / f).is_file()]
        extra = ["--writebaseline", "--baselinefile", str(baseline)] if based else []
        found = pyright_diags(g, argv + extra, tree, present) if present else []
        if isinstance(found, Skip):
            return found
        seen.update({f: [] for f in todo})
        for d in found:
            seen.setdefault(d.file, []).append([d.line, d.code, d.message])
        store.write_text(json.dumps(seen))
    if g.tree != Path(g.root):
        provision(g.tree, g.root, env)
    extra: list[str] = []
    if based and baseline.is_file():
        # basedpyright rewrites a baseline it reads when errors went away: hand it a private copy.
        fd, copy = tempfile.mkstemp(suffix=".json", dir=g.cache)
        os.close(fd)
        shutil.copy2(baseline, copy)
        extra = ["--baselinefile", copy]
    try:
        after = pyright_diags(g, argv + extra, g.tree, files)
    finally:
        for f in extra[1:]:
            os.unlink(f)
    if isinstance(after, Skip):
        return after
    before = [] if based else [Diag(f, *row) for f in files for row in seen.get(f, [])]
    return Path(checker).name, new_diags(after, before)


def tsc_diags(g: Gate, argv: list[str], tsconfig: Path, buildinfo: Path) -> list[Diag] | Skip:
    rc, out = run([*argv, "--noEmit", "--pretty", "false", "--incremental", "--tsBuildInfoFile", str(buildinfo),
                   "-p", str(tsconfig)], cwd=tsconfig.parent, timeout=g.left())
    if rc == 124:
        return Skip("tsc did not finish within the deadline")
    return [Diag(os.path.normpath(os.path.join(tsconfig.parent, m["file"])), int(m["line"]), m["code"], m["msg"])
            for m in (TSC_LINE.match(line.strip()) for line in out.splitlines()) if m]


def gate_typescript(g: Gate, files: list[str]) -> tuple[str, list[Diag]] | Skip | None:
    projects: dict[str, list[str]] = {}
    for f in files:
        tsconfig = find_up(Path(g.tree, f).parent, g.tree, ("tsconfig.json",))
        if tsconfig is not None:
            projects.setdefault(os.path.relpath(tsconfig, g.tree), []).append(f)
    fresh: list[Diag] = []
    ran = False
    for rel_cfg, mine in projects.items():
        checker = which_up("tsc", Path(g.root, rel_cfg).parent, g.root)
        if not checker:
            continue
        tag = hashlib.sha256(f"{checker}\0{rel_cfg}\0{Path(g.tree, rel_cfg).read_text()}".encode()).hexdigest()[:12]
        store = g.cache / f"ts-{g.mb}-{tag}.json"
        if not store.is_file():
            tree = base_tree(g)
            if tree is None:
                return Skip("could not extract the merge-base")
            provision(tree, g.root, None)
            rows = tsc_diags(g, [checker], tree / rel_cfg, g.cache / f"ts-base-{tag}.tsbuildinfo") \
                if (tree / rel_cfg).is_file() else []
            if isinstance(rows, Skip):
                return rows
            store.write_text(json.dumps([[rel(d.file, tree), d.line, d.code, d.message] for d in rows]))
        if g.tree != Path(g.root):
            provision(g.tree, g.root, None)
        after = tsc_diags(g, [checker], g.tree / rel_cfg, g.cache / f"ts-head-{tag}.tsbuildinfo")
        if isinstance(after, Skip):
            return after
        wanted = set(mine)
        before = [Diag(*row) for row in json.loads(store.read_text()) if row[0] in wanted]
        fresh += new_diags([d._replace(file=rel(d.file, g.tree)) for d in after if rel(d.file, g.tree) in wanted], before)
        ran = True
    return ("tsc", fresh) if ran else None


STOP = (Phase("python", "stop", PY, gate_python), Phase("typescript", "stop", TS, gate_typescript))


def cmd_gate(root: str, mode: str, rev: str) -> int:
    """Prints the new errors and exits 1, else exits 0. Skipped checks are said on stderr and never
    recorded as passed. At Stop, a tree whose errors were already shown passes: the push refuses."""
    root = git_top(root)
    head = git(root, "rev-parse", "-q", "--verify", f"{rev or 'HEAD'}^{{commit}}") if root else ""
    mb = fork_point(root, head) if head else ""
    if not mb:
        return 0
    if mode == "stop":
        names = git(root, "diff", "--name-only", "--diff-filter=d", mb).splitlines()
    else:
        names = git(root, "diff", "--name-only", "--diff-filter=d", mb, head).splitlines()
    phases = [(p, [f for f in names if f.endswith(p.exts)]) for p in STOP if not is_off(root, f"{p.lang}.stop")]
    phases = [(p, fs) for p, fs in phases if fs]
    if not phases:
        return 0
    seconds = float(os.environ.get("VERIFY_DEADLINE") or DEADLINE[mode])
    g = Gate(root, mb, Path(root), cache_dir(root), time.monotonic() + seconds)
    state_file = g.cache / "gate.json"
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    with tempfile.TemporaryDirectory(dir=g.cache) as scratch:
        clean = not git(root, "status", "--porcelain", "--untracked-files=no")
        if mode == "push" and not (head == git(root, "rev-parse", "HEAD") and clean):
            g.tree = Path(scratch, "tree")
            if not extract(root, head, g.tree):
                return 0
        digest = hashlib.sha256(json.dumps([mb, mode == "stop" or head, [p.lang for p, _ in phases]]).encode())
        for f in sorted(f for _, fs in phases for f in fs):
            digest.update(f.encode() + b"\0" + Path(g.tree, f).read_bytes())
        key = digest.hexdigest()
        if state.get("key") == key:
            return report(state, state_file, mode)
        parts, skips = [], []
        for phase, files in phases:
            result = phase.run(g, files)
            if isinstance(result, Skip):
                skips.append(result)
            elif result and result[1]:
                name, fresh = result
                noun = "error" if len(fresh) == 1 else "errors"
                parts.append(f"{name}: {len(fresh)} new {noun} in changed files (against {mb[:8]}):\n{capped(fresh)}")
    noted = set(state.get("noted", []))
    for skip in skips:
        if not skip.once or skip.reason not in noted:
            sys.stderr.write(f"verify: {skip.reason}; not checked.\n")
            noted.add(skip.reason)
    state = {"key": "" if skips else key, "text": "\n".join(parts), "shown": False, "noted": sorted(noted)}
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
