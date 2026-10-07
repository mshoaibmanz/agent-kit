#!/usr/bin/env python3
"""Verification checks (README "Verification"): a correctness lint per edit, a type gate at Stop and
pre-push that reports only errors the branch introduced, and the untracked type-check config a new
worktree needs. Every check skips silently when its tool is missing; `doctor` says which are there.

    verify.py edit                 PostToolUse payload on stdin -> hookSpecificOutput JSON, or nothing
    verify.py gate <root> [stop]   new errors in the branch's changed files; exit 1 when there are any
    verify.py worktree <path>      copy pyrightconfig.json and link .venv from the main checkout
    verify.py doctor [<repo>...]   tools found and phases on, per repo (a path, or a name under CODE_DIRS)

Config: verify.toml in the overlay folder (beside kit.env) over <kit>/pack/verify.toml. Tables
[default] and [repo.<name>] (the main checkout's folder name), each holding <lang>.<phase> =
{enabled, cmd, select} and worktree = {enabled}; a later layer and a repo table win per key.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, List, NamedTuple, Optional, Tuple

try:
    import tomllib
except ImportError:  # Python < 3.11: no config file, the defaults apply
    tomllib = None  # type: ignore[assignment]

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
try:
    import kit_env
except ImportError:
    kit_env = None  # type: ignore[assignment]

CAP = 10
EDIT_TIMEOUT = 10
GATE_TIMEOUT = float(os.environ.get("VERIFY_TIMEOUT") or 300)
STALE_SECONDS = 3 * 86400
LANGS = {
    "python": (".py", ".pyi"),
    "typescript": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    "sql": (".sql",),
    "rust": (".rs",),
}
# Phases each language has; rust has no per-edit check (a crate compiles as a whole).
PHASES = {"python": ("edit", "stop"), "typescript": ("edit", "stop"), "sql": ("edit",), "rust": ("stop",)}
# Unused imports and variables stay out of the per-edit report: an edit-by-edit flow adds the import
# before the line that uses it, and the editor's "not accessed" hints were a third of all diagnostics.
RUFF_SELECT = ["--select", "F,E9,B", "--ignore", "F401,F841"]
ESLINT_CONFIGS = tuple(f"eslint.config.{e}" for e in ("js", "mjs", "cjs", "ts", "mts", "cts")) + tuple(
    f".eslintrc{e}" for e in ("", ".js", ".cjs", ".json", ".yml", ".yaml")
)
WORKTREE_COPY = ("pyrightconfig.json",)
WORKTREE_LINK = (".venv",)
TSC_LINE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),\d+\): error (?P<code>TS\d+): (?P<msg>.*)$")
CLIPPY_LINE = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+):\d+: error(?:\[(?P<code>[^\]]+)\])?: (?P<msg>.*)$")
SQLFLUFF_LINE = re.compile(r"^L:\s*(?P<line>\d+) \| P:\s*\d+ \|\s*(?P<code>\w+) \| (?:Line \d+, Position \d+: )?(?P<msg>.*)$")


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


def run(argv: list[str], cwd: str | Path | None = None, stdin: str | None = None,
        timeout: float = EDIT_TIMEOUT) -> tuple[int, str]:
    """(exit code, stdout + stderr); 127 when the tool cannot start, 124 on timeout."""
    try:
        proc = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except (OSError, ValueError):
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, proc.stdout + proc.stderr if proc.returncode else proc.stdout


def git(root: str | Path, *args: str) -> str:
    rc, out = run(["git", "-C", str(root), *args], timeout=60)
    return out.strip() if rc == 0 else ""


def git_top(path: str | Path) -> str:
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    return git(folder or ".", "rev-parse", "--show-toplevel")


def main_checkout(root: str | Path) -> str:
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.dirname(common) if common else ""


def lang_of(path: str) -> str | None:
    return next((lang for lang, exts in LANGS.items() if path.endswith(exts)), None)


def overlay_files() -> list[Path]:
    """The config layers, lowest first: the team pack's, then the user's overlay."""
    if kit_env is None:
        return []
    return [kit_env.kit_dir() / "pack/verify.toml", Path(kit_env.kit_env_path()).parent / "verify.toml"]


def load_config(repo: str) -> dict[str, Any]:
    """lang -> phase -> {enabled, cmd, select}, plus worktree -> {enabled}, for one repo name."""
    tables: list[dict[str, Any]] = []
    for layer in overlay_files():
        if tomllib is None or not layer.is_file():
            continue
        try:
            tables.append(tomllib.loads(layer.read_text()))
        except (OSError, ValueError):
            continue
    scopes = [t.get("default", {}) for t in tables] + [t.get("repo", {}).get(repo, {}) for t in tables]
    out: dict[str, Any] = {lang: {p: {"enabled": True} for p in phases} for lang, phases in PHASES.items()}
    out["worktree"] = {"enabled": True}
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        for lang, phases in scope.items():
            if lang == "worktree" and isinstance(phases, dict):
                out["worktree"].update(phases)
            elif lang in PHASES and isinstance(phases, dict):
                for phase, keys in phases.items():
                    if phase in out[lang] and isinstance(keys, dict):
                        out[lang][phase].update(keys)
    return out


def repo_name(root: str | Path) -> str:
    return os.path.basename(main_checkout(root) or str(root))


def phase(cfg: dict[str, Any], lang: str, name: str) -> dict[str, Any] | None:
    """The phase's settings when it is on, else None."""
    entry = cfg.get(lang, {}).get(name)
    return entry if entry and entry.get("enabled", True) is not False else None


def which_up(name: str, start: str | Path, stop: str | Path) -> str | None:
    """node_modules/.bin/<name> or .venv/bin/<name> from start up to stop, else PATH."""
    folder, top = Path(start).resolve(), Path(stop).resolve()
    while True:
        for sub in ("node_modules/.bin", ".venv/bin"):
            candidate = folder / sub / name
            if os.access(candidate, os.X_OK):
                return str(candidate)
        if folder == top or folder.parent == folder:
            break
        folder = folder.parent
    return shutil.which(name)


def find_up(start: str | Path, stop: str | Path, names: tuple[str, ...]) -> Path | None:
    folder, top = Path(start).resolve(), Path(stop).resolve()
    while True:
        for name in names:
            if (folder / name).is_file():
                return folder / name
        if folder == top or folder.parent == folder:
            return None
        folder = folder.parent


def has_ruff_config(path: str, top: str) -> bool:
    folder, stop = Path(path).resolve().parent, Path(top).resolve()
    while True:
        if (folder / "ruff.toml").is_file() or (folder / ".ruff.toml").is_file():
            return True
        pyproject = folder / "pyproject.toml"
        if pyproject.is_file() and re.search(r"^\[tool\.ruff", pyproject.read_text(errors="replace"), re.M):
            return True
        if folder == stop or folder.parent == folder:
            return False
        folder = folder.parent


def tool_argv(entry: dict[str, Any], default: str | None) -> list[str] | None:
    """The phase's cmd override (an argv prefix), else the default tool path."""
    if entry.get("cmd"):
        return shlex.split(str(entry["cmd"]))
    return [default] if default else None


# Per-edit checkers return (the new text's diagnostics, the old text's), or None to skip.
Checked = Optional[Tuple[List[Diag], List[Diag]]]


def both(fn: Any, old: str, new: str) -> tuple[list[Diag], list[Diag]]:
    with ThreadPoolExecutor(2) as pool:
        after, before = pool.submit(fn, new), pool.submit(fn, old)
        return after.result(), before.result()


def edit_python(path: str, top: str, entry: dict[str, Any], old: str) -> Checked:
    ruff = tool_argv(entry, which_up("ruff", Path(path).parent, top))
    if not ruff:
        return None
    if has_ruff_config(path, top):
        run([*ruff, "check", "--fix", "--quiet", path], cwd=top)
        run([*ruff, "format", "--quiet", path], cwd=top)
    select = [] if entry.get("select") == "repo" else RUFF_SELECT

    def check(text: str) -> list[Diag]:
        rc, out = run([*ruff, "check", "--output-format", "json", "--no-fix", "--exit-zero", *select,
                       "--stdin-filename", path, "-"], cwd=top, stdin=text)
        try:
            rows = json.loads(out) if rc == 0 else []
        except ValueError:
            return []
        return [Diag(path, r["location"]["row"], r.get("code") or "syntax", r.get("message", "")) for r in rows]

    return both(check, old, Path(path).read_text(errors="replace"))


def edit_typescript(path: str, top: str, entry: dict[str, Any], old: str) -> Checked:
    config = find_up(Path(path).parent, top, ESLINT_CONFIGS)
    if config is not None:
        eslint = tool_argv(entry, which_up("eslint", config.parent, top))
        if not eslint:
            return None

        def check(text: str) -> list[Diag]:
            rc, out = run([*eslint, "--format", "json", "--stdin", "--stdin-filename", path],
                          cwd=config.parent, stdin=text)
            try:
                files = json.loads(out)
            except ValueError:
                return []
            return [Diag(path, m.get("line", 0), m.get("ruleId") or "parse", m.get("message", ""))
                    for f in files for m in f.get("messages", []) if m.get("severity") == 2]

        return both(check, old, Path(path).read_text(errors="replace"))
    oxlint = tool_argv(entry, which_up("oxlint", Path(path).parent, top))
    if not oxlint:
        return None

    def check_ox(text: str) -> list[Diag]:
        # oxlint reads no stdin: the text goes to a same-named file in a scratch folder.
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / Path(path).name
            copy.write_text(text)
            rc, out = run([*oxlint, "-A", "all", "-D", "correctness", "--format", "unix", str(copy)], cwd=scratch)
        found = []
        for line in out.splitlines():
            m = re.match(r"^.+?:(\d+):\d+: (.*?)(?: \[Error/(.+)\])?$", line)
            if m:
                found.append(Diag(path, int(m.group(1)), m.group(3) or "oxlint", m.group(2)))
        return found

    return both(check_ox, old, Path(path).read_text(errors="replace"))


def edit_sql(path: str, top: str, entry: dict[str, Any], old: str) -> Checked:
    sqlfluff = tool_argv(entry, which_up("sqlfluff", Path(path).parent, top))
    if not sqlfluff:
        return None
    skip = False

    def check(text: str) -> list[Diag]:
        nonlocal skip
        rc, out = run([*sqlfluff, "parse", "-"], cwd=Path(path).parent, stdin=text)
        if "No dialect was specified" in out or rc in (124, 127):
            skip = True
            return []
        return [Diag(path, int(m["line"]), m["code"], m["msg"])
                for m in (SQLFLUFF_LINE.match(line.strip()) for line in out.splitlines()) if m]

    result = both(check, old, Path(path).read_text(errors="replace"))
    return None if skip else result


EDIT_CHECKS = {"python": edit_python, "typescript": edit_typescript, "sql": edit_sql}


def new_diags(after: list[Diag], before: list[Diag]) -> list[Diag]:
    """after's diagnostics beyond before's, matched on file, code and message (line shifts ignored),
    in line order, one per line, code and message."""
    left = Counter(d.key() for d in before)
    out, seen = [], set()
    for d in sorted(after, key=lambda d: d.line):
        if left[d.key()] > 0:
            left[d.key()] -= 1
        elif (d.line, d.code, d.message) not in seen:
            seen.add((d.line, d.code, d.message))
            out.append(d)
    return out


def capped(diags: list[Diag], with_file: bool = True) -> str:
    lines = [d.text(with_file) for d in diags[:CAP]]
    if len(diags) > CAP:
        lines.append(f"  ... {len(diags) - CAP} more")
    return "\n".join(lines)


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
    lang = lang_of(path)
    if not lang or lang not in EDIT_CHECKS or not os.path.isfile(path):
        return 0
    top = git_top(path)
    if not top:
        return 0
    entry = phase(load_config(repo_name(top)), lang, "edit")
    old = old_text(payload, path, top) if entry else None
    if entry is None or old is None:
        return 0
    checked = EDIT_CHECKS[lang](path, top, entry, old)
    if not checked:
        return 0
    fresh = new_diags(*checked)
    if not fresh:
        return 0
    rel = os.path.relpath(path, top)
    noun = "error" if len(fresh) == 1 else "errors"
    text = f"verify: this edit introduced {len(fresh)} {noun} in {rel}:\n{capped(fresh, with_file=False)}"
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}, sys.stdout)
    return 0


def provision(source: str | Path, dest: str | Path, cfg: dict[str, Any], extra_links: tuple[str, ...] = ()) -> list[str]:
    """Give dest the untracked type-check config source has: copy WORKTREE_COPY files git does not
    track, link WORKTREE_LINK folders (and extra_links); never replace what dest already has."""
    if cfg.get("worktree", {}).get("enabled", True) is False:
        return []
    done = []
    source, dest = Path(source), Path(dest)
    for name in WORKTREE_COPY:
        src, dst = source / name, dest / name
        tracked = run(["git", "-C", str(source), "ls-files", "--error-unmatch", name], timeout=5)[0] == 0
        if src.is_file() and not tracked and not dst.exists():
            shutil.copy2(src, dst)
            done.append(f"copied {name}")
    for name in WORKTREE_LINK + extra_links:
        src, dst = source / name, dest / name
        if src.is_dir() and not dst.exists() and not dst.is_symlink():
            dst.symlink_to(src.resolve())
            done.append(f"linked {name}")
    return done


def cmd_worktree(path: str) -> int:
    top = git_top(path)
    main = main_checkout(path) if top else ""
    if not top or not main or os.path.realpath(main) == os.path.realpath(top):
        return 0
    provision(main, top, load_config(repo_name(top)))
    return 0


def base_ref(root: str) -> str:
    """The merge-base with the branch reviews diff against (REVIEW_BASE, origin/HEAD, main, master)."""
    names = [kit_env.kit_env().get("REVIEW_BASE", "") if kit_env else ""]
    names += [git(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"), "origin/main",
              "origin/master", "main", "master"]
    for name in filter(None, names):
        for ref in (name, f"origin/{name}"):
            if git(root, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"):
                mb = git(root, "merge-base", "HEAD", ref)
                if mb:
                    return mb
    return ""


def changed_files(root: str, mb: str, untracked: bool) -> list[str]:
    """Files changed since mb that still exist; untracked ones too at Stop, not at a push that
    carries none of them."""
    found = git(root, "diff", "--name-only", "--diff-filter=d", mb).splitlines()
    if untracked:
        found += git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({f for f in found if f and os.path.isfile(os.path.join(root, f))})


def cache_dir(root: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "agent-kit/verify"
    return base / hashlib.sha256(os.path.realpath(main_checkout(root) or root).encode()).hexdigest()[:16]


def base_tree(root: str, mb: str, cfg: dict[str, Any]) -> Path | None:
    """The merge-base's files, extracted once per merge-base with this checkout's untracked config,
    so a checker sees the code the branch started from with the same imports."""
    cache = cache_dir(root)
    tree = cache / f"base-{mb}"
    if (tree / ".verify-ready").is_file():
        return tree
    # Other worktrees of the repo may be using their own merge-base's entries: only stale ones go.
    for old in cache.glob("*"):
        if mb not in old.name and time.time() - old.stat().st_mtime > STALE_SECONDS:
            shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)
    shutil.rmtree(tree, ignore_errors=True)
    tree.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as spool:
        rc = subprocess.run(["git", "-C", root, "archive", "--format=tar", mb], stdout=spool, check=False).returncode
        if rc != 0:
            return None
        spool.seek(0)
        with tarfile.open(fileobj=spool) as tar:
            tar.extractall(tree, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(tree)
    provision(root, tree, {**cfg, "worktree": {"enabled": True}}, extra_links=("node_modules",))
    (tree / ".verify-ready").write_text(mb)
    return tree


def pyright_diags(argv: list[str], cwd: str | Path, files: list[str], extra: list[str]) -> list[Diag] | None:
    rc, out = run([*argv, "--outputjson", "--level", "error", *extra, *files], cwd=cwd, timeout=GATE_TIMEOUT)
    try:
        report = json.loads(out[out.find("{"):]) if "{" in out else None
    except ValueError:
        report = None
    if report is None:
        return None
    return [Diag(rel(d["file"], cwd), d.get("range", {}).get("start", {}).get("line", 0) + 1,
                 d.get("rule") or "error", d.get("message", ""))
            for d in report.get("generalDiagnostics", []) if d.get("severity") == "error"]


def rel(path: str, base: str | Path) -> str:
    """path relative to base, whichever of base and its resolved form contains it."""
    if not os.path.isabs(path):
        return path
    for b in (str(base), os.path.realpath(base)):
        candidate = os.path.relpath(path, b)
        if not candidate.startswith(".."):
            return candidate
    return os.path.relpath(os.path.realpath(path), os.path.realpath(base))


def gate_python(root: str, mb: str, files: list[str], entry: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[Diag]] | None:
    default = which_up("basedpyright", root, root) or which_up("pyright", root, root)
    argv = tool_argv(entry, default)
    if not argv:
        return None
    based = "basedpyright" in " ".join(argv)
    name = "basedpyright" if based else "pyright"
    cache = cache_dir(root)
    store = cache / f"python-{mb}.json"
    baseline = cache / f"python-{mb}.baseline.json"
    seen: dict[str, list[list[Any]]] = json.loads(store.read_text()) if store.is_file() else {}
    todo = [f for f in files if f not in seen]
    if todo:
        tree = base_tree(root, mb, cfg)
        if tree is None:
            return None
        present = [f for f in todo if (tree / f).is_file()]
        extra = ["--writebaseline", "--baselinefile", str(baseline)] if based else []
        found = pyright_diags(argv, tree, present, extra) if present else []
        if found is None:
            return None
        for f in todo:
            seen[f] = []
        for d in found:
            seen.setdefault(d.file, []).append([d.line, d.code, d.message])
        cache.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(seen))
    extra = []
    if based and baseline.is_file():
        # basedpyright rewrites a baseline it reads when errors went away: hand it a copy.
        copy = cache / "baseline-run.json"
        shutil.copy2(baseline, copy)
        extra = ["--baselinefile", str(copy)]
    after = pyright_diags(argv, root, files, extra)
    if after is None:
        return None
    before = [] if based else [Diag(f, *row) for f in files for row in seen.get(f, [])]
    return name, new_diags(after, before)


def tsc_diags(argv: list[str], tsconfig: Path, buildinfo: Path) -> list[Diag]:
    rc, out = run([*argv, "--noEmit", "--pretty", "false", "--incremental", "--tsBuildInfoFile", str(buildinfo),
                   "-p", str(tsconfig)], cwd=tsconfig.parent, timeout=GATE_TIMEOUT)
    found = []
    for line in out.splitlines():
        m = TSC_LINE.match(line.strip())
        if m:
            path = os.path.normpath(os.path.join(tsconfig.parent, m["file"]))
            found.append(Diag(path, int(m["line"]), m["code"], m["msg"]))
    return found


def gate_typescript(root: str, mb: str, files: list[str], entry: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[Diag]] | None:
    projects: dict[Path, list[str]] = {}
    for f in files:
        tsconfig = find_up(Path(root, f).parent, root, ("tsconfig.json",))
        if tsconfig is not None:
            projects.setdefault(tsconfig, []).append(f)
    if not projects:
        return None
    cache, fresh, ran = cache_dir(root), [], False
    for tsconfig, mine in projects.items():
        argv = tool_argv(entry, which_up("tsc", tsconfig.parent, root))
        if not argv:
            continue
        rel_cfg = os.path.relpath(tsconfig, root)
        tag = hashlib.sha256(rel_cfg.encode()).hexdigest()[:12]
        cache.mkdir(parents=True, exist_ok=True)
        store = cache / f"ts-{mb}-{tag}.json"
        if store.is_file():
            before_rows = json.loads(store.read_text())
        else:
            tree = base_tree(root, mb, cfg)
            if tree is None:
                return None
            base_cfg = tree / rel_cfg
            rows = tsc_diags(argv, base_cfg, cache / f"ts-base-{tag}.tsbuildinfo") if base_cfg.is_file() else []
            before_rows = [[rel(d.file, tree), d.line, d.code, d.message] for d in rows]
            store.write_text(json.dumps(before_rows))
        wanted = set(mine)
        after = [d._replace(file=rel(d.file, root))
                 for d in tsc_diags(argv, tsconfig, cache / f"ts-head-{tag}.tsbuildinfo")]
        after = [d for d in after if d.file in wanted]
        before = [Diag(*row) for row in before_rows if row[0] in wanted]
        fresh += new_diags(after, before)
        ran = True
    return ("tsc", fresh) if ran else None


def gate_rust(root: str, mb: str, files: list[str], entry: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[Diag]] | None:
    crates = {m.parent for f in files if (m := find_up(Path(root, f).parent, root, ("Cargo.toml",)))}
    argv = tool_argv(entry, shutil.which("cargo"))
    if not crates or not argv:
        return None
    found = []
    for crate in sorted(crates):
        rc, out = run([*argv, "clippy", "--quiet", "--message-format", "short"], cwd=crate, timeout=GATE_TIMEOUT)
        for line in out.splitlines():
            m = CLIPPY_LINE.match(line.strip())
            if m:
                found.append(Diag(os.path.relpath(os.path.join(crate, m["file"]), root), int(m["line"]),
                                  m["code"] or "error", m["msg"]))
    return "clippy", new_diags(found, [])


GATES = {"python": gate_python, "typescript": gate_typescript, "rust": gate_rust}


def gate_state(root: str) -> Path:
    gitdir = git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    return Path(gitdir or root) / "agent-verify-gate.json"


def cmd_gate(root: str, at_stop: bool) -> int:
    """Prints the new errors and exits 1, else exits 0 (also when nothing could run). At Stop, a
    tree whose errors were already shown passes: the model saw them; the push still refuses."""
    root = git_top(root)
    mb = base_ref(root) if root else ""
    if not mb:
        return 0
    cfg = load_config(repo_name(root))
    files = changed_files(root, mb, untracked=at_stop)
    by_lang: dict[str, list[str]] = {}
    for f in files:
        lang = lang_of(f)
        if lang in GATES and phase(cfg, lang, "stop"):
            by_lang.setdefault(lang, []).append(f)
    if not by_lang:
        return 0
    digest = hashlib.sha256(json.dumps([mb, sorted(by_lang.items()), cfg], sort_keys=True).encode())
    for f in sorted(f for fs in by_lang.values() for f in fs):
        digest.update(f.encode() + b"\0" + Path(root, f).read_bytes())
    key = digest.hexdigest()
    state_file = gate_state(root)
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get("key") != key:
        parts = []
        for lang, mine in by_lang.items():
            result = GATES[lang](root, mb, mine, phase(cfg, lang, "stop") or {}, cfg)
            if result and result[1]:
                name, fresh = result
                noun = "error" if len(fresh) == 1 else "errors"
                parts.append(f"{name}: {len(fresh)} new {noun} in changed files (against {mb[:8]}):\n{capped(fresh)}")
        state = {"key": key, "text": "\n".join(parts), "shown": False}
    if not state["text"]:
        state_file.write_text(json.dumps(state))
        return 0
    shown = state["shown"]
    state["shown"] = shown or at_stop
    state_file.write_text(json.dumps(state))
    if at_stop and shown:
        return 0
    sys.stdout.write(state["text"] + "\n")
    return 1


def repo_paths(names: list[str]) -> list[Path]:
    if names:
        out = []
        roots = [Path(os.path.expanduser(r)) for r in (kit_env.code_dirs() if kit_env else [])]
        for name in names:
            path = Path(os.path.expanduser(name))
            out.append(path if path.is_dir() else next((r / name for r in roots if (r / name).is_dir()), path))
        return out
    configured: set[str] = set()
    for layer in overlay_files():
        if tomllib is not None and layer.is_file():
            try:
                configured |= set(tomllib.loads(layer.read_text()).get("repo", {}))
            except (OSError, ValueError):
                pass
    return repo_paths(sorted(configured)) if configured else []


DOCTOR_TOOLS = {("python", "edit"): ("ruff",), ("python", "stop"): ("basedpyright", "pyright"),
                ("typescript", "edit"): ("eslint", "oxlint"), ("typescript", "stop"): ("tsc",),
                ("sql", "edit"): ("sqlfluff",), ("rust", "stop"): ("cargo",)}


def cmd_doctor(names: list[str]) -> int:
    tools = sorted({t for ts in DOCTOR_TOOLS.values() for t in ts})
    found = [f"{t} {'ok' if shutil.which(t) else 'missing'}" for t in tools]
    sys.stdout.write(f"verify: on PATH: {', '.join(found)}\n")
    repos = repo_paths(names)
    if not names and not repos and git_top(os.getcwd()):
        repos = [Path(git_top(os.getcwd()))]
    for repo in repos:
        if not repo.is_dir():
            sys.stdout.write(f"verify {repo.name}: no checkout found\n")
            continue
        cfg = load_config(repo_name(repo) if git_top(repo) else repo.name)
        rows = []
        for (lang, name), wanted in DOCTOR_TOOLS.items():
            entry = phase(cfg, lang, name)
            if entry is None:
                rows.append(f"{lang} {name} off")
                continue
            tool = str(entry["cmd"]) if entry.get("cmd") else next((t for t in wanted if which_up(t, repo, repo)), "")
            rows.append(f"{lang} {name} {tool or 'skipped (no ' + '/'.join(wanted) + ')'}")
        worktree = "on" if cfg["worktree"].get("enabled", True) is not False else "off"
        sys.stdout.write(f"verify {repo.name}: {'; '.join(rows)}; worktree config {worktree}\n")
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["edit"]:
            return cmd_edit()
        if argv[:1] == ["gate"] and len(argv) >= 2:
            return cmd_gate(argv[1], argv[2:3] == ["stop"])
        if argv[:1] == ["worktree"] and len(argv) == 2:
            return cmd_worktree(argv[1])
        if argv[:1] == ["doctor"]:
            return cmd_doctor(argv[1:])
    except (OSError, ValueError, KeyError, TypeError):
        # A check that cannot run never blocks an edit, a Stop or a push.
        return 0
    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
