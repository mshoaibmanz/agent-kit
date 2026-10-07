#!/usr/bin/env python3
"""Tests for the verification checks (hooks/verify-edit, lib/verify.py, the Stop wiring,
git-hooks/agent/post-checkout) with the real tools in scratch repos.

    python3 hooks/tests/verify.test.py

HOOKS_DIR=<dir> tests another copy of the hooks. A case whose tool is missing is skipped, except in
CI (CI=true), where ruff, basedpyright, tsc and eslint are installed and a missing one fails.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("verify.test")

HOOKS = Path(os.environ.get("HOOKS_DIR") or Path(__file__).resolve().parents[1])
KIT = HOOKS.parent
VERIFY = HOOKS / "lib/verify.py"
CI = os.environ.get("CI") == "true"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    logger.info("%s %s", "ok  " if ok else "FAIL", label)
    if not ok:
        failures.append(label)
        if detail:
            logger.info("     %s", detail.replace("\n", "\n     "))


def need(*tools: str) -> bool:
    missing = [t for t in tools if not shutil.which(t)]
    if not missing:
        return True
    if CI:
        failures.append(f"{', '.join(missing)} not installed in CI")
    logger.info("SKIP cases needing %s (not installed)", ", ".join(missing))
    return False


def call(argv: list[str], payload: str = "", **kwargs: object) -> tuple[int, str, str]:
    """(exit code, stdout, stderr); 127 when the program is missing, so each case fails on its own."""
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True, check=False, **kwargs)  # type: ignore[call-overload]
    except OSError as error:
        return 127, "", str(error)
    return proc.returncode, proc.stdout, proc.stderr


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str = "c") -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def new_repo(root: Path, name: str, files: dict[str, str], venv: bool = False) -> Path:
    """A clone of a fresh origin whose main holds files; on branch feature. venv: a .venv, ignored."""
    origin = root / f"{name}.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = root / name
    git(root, "clone", "-q", str(origin), str(repo))
    git(repo, "checkout", "-qb", "main")
    commit(repo, {".gitignore": ".venv\n", **files}, "base")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-qb", "feature")
    if venv:
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")], check=True)
    return repo


def edit(path: Path, new: str, env: dict[str, str]) -> tuple[int, str]:
    """Writes new over path and runs verify-edit with the Edit tool's payload."""
    original = path.read_text()
    path.write_text(new)
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "t",
               "tool_input": {"file_path": str(path)}, "tool_response": {"filePath": str(path), "originalFile": original}}
    rc, out, _ = call([str(HOOKS / "verify-edit")], json.dumps(payload), env=env)
    return rc, out


def context(out: str) -> str:
    try:
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
    except (ValueError, KeyError):
        return ""


def gate(repo: Path, env: dict[str, str], *mode: str) -> tuple[int, str, str]:
    return call([sys.executable, str(VERIFY), "gate", str(repo), *(mode or ("stop",))], env=env)


def overlay(env: dict[str, str], text: str) -> None:
    Path(env["KIT_ENV"]).write_text("REVIEW_BASE=main\n" + text)


def edit_cases(tmp: Path, env: dict[str, str]) -> None:
    if not need("ruff"):
        return
    repo = new_repo(tmp, "plain", {"m.py": "def f():\n    return pre_existing\n"})
    m = repo / "m.py"
    rc, out = edit(m, "import os\n\n\ndef f():\n    return pre_existing\n\n\ndef g():\n    return introduced\n", env)
    text = context(out)
    check("edit: an introduced F821 is reported", rc == 0 and "F821" in text and "introduced" in text, out)
    check("edit: the pre-existing one is not (its line moved)", "pre_existing" not in text, text)
    check("edit: an unused import is not reported per edit", "F401" not in text, text)
    check("edit: no ruff config, so the file is not reformatted", m.read_text().startswith("import os\n\n\ndef"))

    rc, out = edit(m, m.read_text() + "\n", env)
    check("edit: an edit that adds no error is silent", rc == 0 and out.strip() == "", out)

    rc, out = edit(m, "".join(f"x{i} = undefined_{i}\n" for i in range(15)), env)
    lines = context(out).splitlines()
    check("edit: output capped at 10 lines plus a count", len(lines) == 12 and lines[-1].strip() == "... 5 more",
          "\n".join(lines))

    rc, out = edit(m, "from fastapi import Depends\n\n\ndef route(db=Depends(object)):\n    return db\n", env)
    check("edit: B008 (a FastAPI Depends default) is not reported", "B008" not in context(out), out)

    rc, out = edit(m, "def f(:\n    return 1\n", env)
    check("edit: a syntax error the edit made is reported", "invalid-syntax" in context(out), out)

    cfg = new_repo(tmp, "configured", {
        "ruff.toml": '[lint]\nextend-select = ["B"]\nignore = ["B006"]\nper-file-ignores = {"skip.py" = ["F821"]}\n'
                     '[format]\nquote-style = "single"\n',
        "skip.py": "a = 1\n", "other.py": "a = 1\n"})
    rc, out = edit(cfg / "skip.py", 'a = "x"\nb = undefined_c\n', env)
    check("edit: the repo config wins (its per-file ignore holds)", rc == 0 and out.strip() == "", out)
    check("edit: the repo config wins (formatted with its quote style)", (cfg / "skip.py").read_text().startswith("a = 'x'"),
          (cfg / "skip.py").read_text())
    rc, out = edit(cfg / "other.py", "def f(x=[]):\n    return x\n\n\nb = undefined_c\n", env)
    text = context(out)
    check("edit: the repo's rule selection wins (its ignored B006 is not reported)", "B006" not in text, text)
    check("edit: the repo config still reports a new F821", "F821" in text, out)

    tool_bin = tmp / "bin-no-ruff"
    tool_bin.mkdir()
    for tool in ("bash", "python3", "git"):
        (tool_bin / tool).symlink_to(shutil.which(tool) or tool)
    rc, out = edit(m, "y = undefined_z\n", {**env, "PATH": str(tool_bin)})
    check("edit: a missing tool is skipped silently", rc == 0 and out.strip() == "", out)


def eslint_cases(tmp: Path, env: dict[str, str]) -> None:
    if not need("eslint"):
        return
    # The config writes to stderr and eslint exits 1 on an error: the JSON is stdout's alone.
    repo = new_repo(tmp, "lintjs", {
        "eslint.config.js": 'console.error("config loaded");\nmodule.exports = [{ files: ["**/*.js"], '
                            'rules: { "no-undef": "error" }, languageOptions: { sourceType: "commonjs", globals: {} } }];\n',
        "c.js": "foo_pre();\n"})
    rc, out = edit(repo / "c.js", "foo_pre();\nbar_new();\n", env)
    text = context(out)
    check("eslint: an introduced no-undef is reported (stderr noise, exit 1)", "bar_new" in text and "no-undef" in text, out)
    check("eslint: the pre-existing one is not", "foo_pre" not in text, text)
    rc, out = edit(repo / "c.js", "foo_pre(1);\nbar_new();\n", env)
    check("eslint: rewriting a line that already had the error reports nothing", out.strip() == "", out)
    (repo / "d.js").write_text("const foo = () => {};\nfoo();\n")
    rc, out = edit(repo / "d.js", "foo();\n", env)
    check("eslint: deleting a declaration reports the no-undef left at its caller", "foo" in context(out)
          and "no-undef" in context(out), out)


def gate_cases(tmp: Path, env: dict[str, str]) -> None:
    if not need("basedpyright"):
        return
    repo = new_repo(tmp, "typed", {"a.py": 'x: int = "pre"\n', "b.py": "y = 1\n"}, venv=True)
    (repo / "a.py").write_text('# shifted\nx: int = "pre"\n')
    rc, out, _ = gate(repo, env)
    check("gate: a pre-existing error (line moved) does not block", rc == 0 and out == "", out)
    (repo / "b.py").write_text('import not_installed_mod\n\ny = 1\n\n\ndef f() -> str:\n    return 1\n')
    overlay(env, "VERIFY_OFF='typ*:python.types'\n")
    rc, out, _ = gate(repo, env)
    check("gate: VERIFY_OFF python.types turns the Python type check off for a matching repo", rc == 0 and out == "", out)
    overlay(env, "")
    rc, out, _ = gate(repo, env)
    check("gate: a NEW error in a changed file blocks", rc == 1 and "b.py:7" in out and "reportReturnType" in out, out)
    check("gate: the pre-existing error is not listed", "a.py" not in out, out)
    check("gate: an unresolved import is not a type error", "not_installed_mod" not in out, out)
    rc, out, _ = gate(repo, env)
    check("gate: at Stop the same tree is shown once", rc == 0 and out == "", out)

    head = commit(repo, {})
    (repo / "b.py").write_text("y = 1\n")
    rc, out, _ = gate(repo, env, "push", head)
    check("gate: the push checks the pushed commit, not the fixed working file", rc == 1 and "b.py:7" in out, out)
    rc, out, _ = gate(repo, env)
    check("gate: Stop checks the working tree (fixed, passes)", rc == 0 and out == "", out)

    (repo / "new_untracked.py").write_text("def g() -> str:\n    return 2\n")
    rc, out, _ = gate(repo, env)
    check("gate: Stop leaves untracked files out, like the push", rc == 0 and "new_untracked" not in out, out)
    (repo / "new_untracked.py").unlink()

    (repo / "b.py").write_text("def h() -> int:\n    return ''\n")
    rc, out, err = gate(repo, {**env, "VERIFY_DEADLINE": "0.01"})
    check("gate: past the deadline it passes and says it skipped", rc == 0 and "deadline" in err, out + err)
    rc, out, _ = gate(repo, env)
    check("gate: a skipped run is not recorded as passed", rc == 1 and "b.py" in out, out)

    # The system folders hold git, bash and what review-state's fork point needs, and no type checker.
    bare = "/usr/bin:/bin"
    (repo / "b.py").write_text("def h() -> int:\n    return b''\n")
    rc, out, _ = call([sys.executable, str(VERIFY), "gate", str(repo), "stop"], env={**env, "PATH": bare})
    alone = not any(shutil.which(t, path=bare) for t in ("basedpyright", "pyright"))
    check("gate: no checker installed passes", alone and rc == 0 and out == "", out)
    rc, out, _ = gate(repo, env)
    check("gate: once the checker is there, the same tree is checked", rc == 1 and "b.py" in out, out)

    stop_case(repo, env, "b.py", "def k() -> int:\n    return 'stop'\n" + "".join(f"v{i} = {i}\n" for i in range(25)))

    noenv = new_repo(tmp, "noenv", {"a.py": "x = 1\n"})
    commit(noenv, {"a.py": "def f() -> str:\n    return 1\n"})
    rc, out, err = gate(noenv, env)
    check("gate: no Python environment: Stop passes and says nothing", rc == 0 and out == "" and err == "", out + err)
    rc, out, err = gate(noenv, env, "push")
    check("gate: the push says the skip, though a Stop came first", rc == 0 and "no environment" in err, out + err)
    rc, out, err = gate(noenv, env, "push")
    check("gate: every push says it", rc == 0 and "no environment" in err, out + err)

    odd = new_repo(tmp, "odd", {"data.txt": "x\n", "my mod.py": "a = 1\n", '"q".py': "a = 1\n"}, venv=True)
    (odd / "data.txt").write_bytes(b"caf\xe9\n")
    (odd / "my mod.py").write_text('a = 1\ny: int = "s"\n')
    (odd / '"q".py').write_text('a = 1\nz: int = "s"\n')
    rc, out, _ = gate(odd, env)
    check("gate: a non-UTF-8 file in the diff does not pass the gate", rc == 1 and "reportAssignmentType" in out, out)
    check("gate: a file name with a space is checked", "my mod.py:2" in out, out)
    check("gate: a C-quoted file name is checked", '"q".py:2' in out, out)

    ihc = new_repo(tmp, "ihc", {"i.py": 'a = 1\np: int = "pre"\nb = 2\n'}, venv=True)
    git(ihc, "config", "diff.interHunkContext", "5")
    (ihc / "i.py").write_text('a = 10\np: int = "pre"\nb = 20\n')
    rc, out, _ = gate(ihc, env)
    check("gate: an unchanged line between two hunks is not counted (diff.interHunkContext)", rc == 0 and out == "", out)

    pushcfg = new_repo(tmp, "pushcfg", {"pyrightconfig.json": '{"venvPath": ".", "venv": ".venv"}\n', "a.py": "x = 1\n"},
                       venv=True)
    head = commit(pushcfg, {"a.py": "def f() -> int:\n    return ''\n"})
    (pushcfg / "pyrightconfig.json").write_text('{"venvPath": ".", "venv": ".venv", "reportReturnType": "none"}\n')
    rc, out, _ = gate(pushcfg, env)
    check("gate: Stop uses the working pyrightconfig.json (the rule is off)", rc == 0 and out == "", out)
    rc, out, _ = gate(pushcfg, env, "push", head)
    check("gate: the push uses the pushed commit's pyrightconfig.json", rc == 1 and "a.py:2" in out, out)

    reuse = new_repo(tmp, "reuse", {"a.py": "x = 1\n"}, venv=True)
    commit(reuse, {"a.py": "def f() -> int:\n    return ''\n"})
    gate(reuse, env)
    rc, out, _ = gate(reuse, {**env, "VERIFY_DEADLINE": "0.01"}, "push")
    check("gate: a push of the tree a Stop checked reuses its result", rc == 1 and "a.py:2" in out, out)

    late = new_repo(tmp, "late-env", {"pyrightconfig.json": '{"venvPath": ".", "venv": ".venv"}\n',
                                      "a.py": "import mylib\nx: int = mylib.f()\n"})
    (late / "a.py").write_text("# c1\nimport mylib\nx: int = mylib.f()\n")
    gate(late, env)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(late / ".venv")], check=True)
    site = next((late / ".venv/lib").glob("python*/site-packages"))
    (site / "mylib.py").write_text('def f() -> str:\n    return ""\n')
    (late / "a.py").write_text("# c2\nimport mylib\nx: int = mylib.f()\n")
    rc, out, _ = gate(late, env)
    check("gate: an environment created later does not make a base error new", rc == 0 and out == "", out)

    imports = new_repo(tmp, "imports", {"a.py": "x = 1\n"}, venv=True)
    (imports / "a.py").write_text("import not_installed_mod\n\nx = 1\n")
    rc, out, _ = gate(imports, env)
    check("gate: a new unresolved import alone does not block", rc == 0 and out == "", out)

    forked = develop_fork(tmp, env)
    rc, out, _ = gate(forked, env)
    check("gate: the base is the branch's nearest base (develop), not main", rc == 0 and "d.py" not in out, out)


def develop_fork(tmp: Path, env: dict[str, str]) -> Path:
    """A feature branch off origin/develop, whose d.py (added on develop) has an error."""
    repo = new_repo(tmp, "forked", {"a.py": "x = 1\n"}, venv=True)
    git(repo, "checkout", "-qb", "develop", "main")
    commit(repo, {"d.py": 'z: int = "develop"\n'})
    git(repo, "push", "-q", "origin", "develop")
    git(repo, "checkout", "-qb", "topic", "origin/develop")
    (repo / "a.py").write_text("x = 2\n")
    return repo


def stop_case(repo: Path, env: dict[str, str], name: str, text: str) -> None:
    """review-trigger end to end: the type errors join the self-check block (25 lines is over its floor)."""
    (repo / name).write_text(text)
    sid = f"vt-{repo.name}"
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": sid, "cwd": str(repo),
               "tool_input": {"file_path": str(repo / name)}}
    call([str(HOOKS / "review-mark-changes")], json.dumps(payload), env=env)
    stop = {"hook_event_name": "Stop", "session_id": sid, "cwd": str(repo), "stop_hook_active": False,
            "last_assistant_message": "done"}
    _, out, err = call([str(HOOKS / "review-trigger")], json.dumps(stop), env=env)
    try:
        reason = json.loads(out).get("reason", "") if out.strip() else ""
    except ValueError:
        reason = ""
    check(f"stop: review-trigger blocks on the new type error ({name})", "type errors" in reason and name in reason,
          out + err)
    check(f"stop: the same block carries the self-check review ({name})", "SELF-CHECK" in reason.upper(), reason)


def tsc_cases(tmp: Path, env: dict[str, str]) -> None:
    if not need("tsc"):
        return
    repo = new_repo(tmp, "typedts", {
        "tsconfig.json": '{"compilerOptions": {"strict": true, "noEmit": true}, "include": ["*.ts"]}\n',
        "a.ts": 'export const x: number = "pre";\n', "b.ts": "export const y = 1;\n"})
    (repo / "a.ts").write_text('// moved\nexport const x: number = "pre";\n')
    rc, out, _ = gate(repo, env)
    check("tsc: a pre-existing error does not block", rc == 0 and out == "", out)
    (repo / "b.ts").write_text("export const y: string = 1;\n")
    rc, out, _ = gate(repo, env)
    check("tsc: a new error in a changed file blocks", rc == 1 and "b.ts:1" in out and "TS2322" in out, out)
    check("tsc: the pre-existing one is not listed", "a.ts" not in out, out)

    mixed = new_repo(tmp, "mixedts", {
        "tsconfig.json": '{"compilerOptions": {"strict": true, "noEmit": true}, "include": ["*.ts", "*.mts"]}\n',
        "a.ts": "export const x = 1;\n", "m.py": "a = 1\n"})
    (mixed / "c.mts").write_text("export const z: number = 'mts';\n")
    git(mixed, "add", "c.mts")
    (mixed / "m.py").write_text("a = 2\n")
    rc, out, _ = gate(mixed, env)
    check("tsc: an .mts file is checked", rc == 1 and "c.mts:1" in out, out)
    rc, out, _ = gate(mixed, env)
    check("tsc: with a once-only skip beside it (no Python environment), the tree is shown once", rc == 0 and out == "",
          out)
    inherit = new_repo(tmp, "inherit", {
        "tsconfig.base.json": '{"compilerOptions": {"strict": false, "noEmit": true}}\n',
        "tsconfig.json": '{"extends": "./tsconfig.base.json", "include": ["*.ts"]}\n', "a.ts": "export const x = 1;\n"})
    (inherit / "a.ts").write_text("export const x = 1;\nexport const s: string = null;\n")
    rc, out, _ = gate(inherit, env)
    check("tsc: the change passes under the loose inherited config", rc == 0 and out == "", out)
    (inherit / "tsconfig.base.json").write_text('{"compilerOptions": {"strict": true, "noEmit": true}}\n')
    rc, out, _ = gate(inherit, env)
    check("tsc: tightening only the inherited config is checked again", rc == 1 and "a.ts:2" in out, out)

    solution = new_repo(tmp, "solution", {
        "tsconfig.json": '{\n  // solution\n  "files": [],\n  "references": [{"path": "./tsconfig.app.json"}],\n}\n',
        "tsconfig.app.json": '{"compilerOptions": {"strict": true, "composite": true}, "include": ["src"]}\n',
        "src/app.ts": "export const a = 1;\n"})
    (solution / "src/app.ts").write_text("export const a = 1;\nexport const b: string = 2;\n")
    rc, out, _ = gate(solution, env)
    check("tsc: a solution-style tsconfig.json checks the referenced project", rc == 1 and "src/app.ts:2" in out, out)
    check("tsc: no build info is written into the repo", not list(solution.glob("*.tsbuildinfo")),
          str(list(solution.glob("*"))))

    stop_case(mixed, env, "c.mts", "export const k: number = 'stop';\n" + "".join(f"export const v{i} = {i};\n"
                                                                               for i in range(25)))


def worktree_cases(tmp: Path, env: dict[str, str]) -> None:
    repo = new_repo(tmp, "wt-main", {"a.py": "x = 1\n"}, venv=True)
    (repo / "pyrightconfig.json").write_text('{"venvPath": ".", "venv": ".venv", "extraPaths": ["src"]}\n')
    with open(repo / ".git/info/exclude", "a") as fh:
        fh.write("pyrightconfig.json\n")
    wt = tmp / "wt-one"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "one")
    call([sys.executable, str(VERIFY), "worktree", str(wt)], env=env)
    try:
        cfg = json.loads((wt / "pyrightconfig.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    venv = Path(cfg.get("venvPath", ""), cfg.get("venv", ""))
    check("worktree: untracked pyrightconfig.json copied, other keys kept", cfg.get("extraPaths") == ["src"], str(cfg))
    check("worktree: its venv points at the main checkout's", venv.resolve() == (repo / ".venv").resolve(), str(cfg))
    check("worktree: no .venv link is made", not (wt / ".venv").exists() and not (wt / ".venv").is_symlink())

    wt2 = tmp / "wt-two"
    git(repo, "worktree", "add", "-q", str(wt2), "-b", "two")
    call([str(KIT / "git-hooks/agent/post-checkout"), "0" * 40, git(wt2, "rev-parse", "HEAD"), "1"], cwd=wt2, env=env)
    check("worktree: the agent post-checkout hook provisions a new worktree", (wt2 / "pyrightconfig.json").is_file())

    wt3 = tmp / "wt-three"
    git(repo, "worktree", "add", "-q", str(wt3), "-b", "three")
    overlay(env, "VERIFY_OFF=wt-*:worktree\n")
    call([sys.executable, str(VERIFY), "worktree", str(wt3)], env=env)
    check("worktree: VERIFY_OFF opts a repo out", not (wt3 / "pyrightconfig.json").exists())
    overlay(env, "")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="verify-test-"))
    (tmp / "overlay").mkdir()
    env = {**os.environ, "KIT_ENV": str(tmp / "overlay/kit.env"), "XDG_CACHE_HOME": str(tmp / "cache"),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    os.environ.update({k: v for k, v in env.items() if k.startswith("GIT_")})
    overlay(env, "")
    try:
        edit_cases(tmp, env)
        eslint_cases(tmp, env)
        gate_cases(tmp, env)
        tsc_cases(tmp, env)
        worktree_cases(tmp, env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    logger.info("%d failure(s)", len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
