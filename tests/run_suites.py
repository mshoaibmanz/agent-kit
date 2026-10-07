"""Run the release suites, each in its own HOME, TMPDIR and work root; called by run-tests.sh.

The FIRST suites run one at a time before the rest, and a failure there stops the run. The rest
(SUITES) run concurrently on --jobs workers, longest (by the last run's timings) first.
`hooks/tests` expands into one suite per hook-test file, the same set run-all.sh runs.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import argparse
import fnmatch
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import suite_lock

ROOT = Path(__file__).resolve().parents[1]
# Run first, one at a time, and stop on a failure: a broken manifest or package build fails fast.
FIRST = ("tests/verify_release.py",)
SUITES = (
    "tests/setup_test.py",
    "tests/setup_review_test.py",
    "tests/installer_ux_test.py",
    "tests/team_pack_test.py",
    "tests/dev_install_test.py",
    "tests/dashboard_test.py",
    "tests/plugin_portability_test.py",
    "tests/installed_guard_test.py",
    "tests/gc_portability_test.py",
    "tests/leak_check_test.py",
    "tests/run_suites_test.py",
    "hooks/tests",
)
HOOK_SUITES = ("hooktest.sh", "review-gates.sh", "*.test.sh", "*.test.py")
# Session and host variables of the agent that started the run; CI has none of them.
DROPPED = (
    "CLAUDECODE",
    "AI_AGENT",
    "AGENT_HOST",
    "AGENT_KIT_DIR",
    "CLAUDE_OUT_ROOT",
    "CLAUDE_PROJECT_DIR",
    "AGENT_GIT_HOOKS",
    "CI_WATCH_ACTIVE",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "AGENT_WORK_ROOT",
    "KIT_ENV",
)
TAIL_LINES = 60


@dataclass
class Suite:
    name: str
    command: list[str]
    index: int = 0  # its place in the run: the fixture folder <run>/<index>/
    seconds: float = 0.0
    result: str = "not run"


def expand(entry: str) -> list[Suite]:
    if entry.rstrip("/") == "hooks/tests":
        folder = ROOT / "hooks/tests"
        names = sorted(
            {path.name for pattern in HOOK_SUITES for path in folder.glob(pattern)},
            key=lambda name: (name not in HOOK_SUITES[:2], name),
        )
        return [
            Suite(
                f"hooks/tests/{name}",
                [sys.executable, str(ROOT / "tests/run_hooks.py"), str(folder / name)],
            )
            for name in names
        ]
    interpreter = sys.executable if entry.endswith(".py") else "bash"
    return [Suite(entry, [interpreter, str(ROOT / entry)])]


def selected(suite: Suite, patterns: list[str]) -> bool:
    path = Path(suite.name)
    return not patterns or any(
        fnmatch.fnmatch(candidate, pattern)
        for pattern in patterns
        for candidate in (suite.name, path.name, path.name.split(".")[0])
    )


def environment(base: Path) -> dict[str, str]:
    # Short names: some suites encode their whole fixture path into one file name (255 bytes).
    home, tmp, work = base / "h", base / "t", base / "w"
    for folder in (home, tmp, work):
        folder.mkdir(parents=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in DROPPED and not key.startswith("GIT_CONFIG_")
    }
    env.update(
        HOME=str(home),
        TMPDIR=str(tmp),
        AGENT_WORK_ROOT=str(work),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_STATE_HOME=str(home / ".local/state"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


class Runner:
    def __init__(self, run_dir: Path, stream: bool) -> None:
        self.run_dir = run_dir
        self.stream = stream
        self.children: set[subprocess.Popen[bytes]] = set()
        # Re-entrant: the SIGTERM handler calls stop() on the main thread, which may hold it.
        self.guard = threading.RLock()
        self.stopping = False

    def log(self, suite: Suite) -> Path:
        return self.run_dir / "logs" / (suite.name.replace("/", "__") + ".log")

    def run(self, suite: Suite) -> Suite:
        if self.stopping:
            return suite
        base = self.run_dir / str(suite.index)
        log = self.log(suite)
        started = time.monotonic()
        with open(log, "wb") as output:
            process = subprocess.Popen(
                suite.command,
                cwd=ROOT,
                env=environment(base),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if self.stream else output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            with self.guard:
                self.children.add(process)
                # stop() may have run between the check above and Popen: it never saw this child.
                if self.stopping:
                    kill_group(process)
            if self.stream and process.stdout:
                for line in process.stdout:
                    output.write(line)
                    sys.stdout.buffer.write(line)
                    sys.stdout.flush()
            code = process.wait()
            with self.guard:
                self.children.discard(process)
        suite.seconds = time.monotonic() - started
        suite.result = "PASS" if code == 0 else f"FAIL rc {code}"
        if code == 0:
            shutil.rmtree(base, ignore_errors=True)
        sys.stdout.write(
            f"  {suite.result:<10} {suite.name:<44} {suite.seconds:7.1f}s\n"
        )
        sys.stdout.flush()
        return suite

    def stop(self) -> None:
        with self.guard:
            self.stopping = True
            for process in self.children:
                kill_group(process)


def kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tests/run-tests.sh",
        description=(
            "Run the release suites, each in its own HOME, TMPDIR and AGENT_WORK_ROOT. Takes a "
            "machine-wide lock first (AGENT_KIT_SUITE_LOCK, default /tmp/agent-kit-suite.lock) and "
            "waits while another run holds it."
        ),
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=os.cpu_count() or 2,
        help="concurrent suites (default: CPU count)",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="one suite at a time, output streamed, stop at the first failure",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SUITE",
        help="run only matching suites (path, file name or stem; globs; repeat or comma-separate)",
    )
    parser.add_argument(
        "--list", action="store_true", help="print the suite names and exit"
    )
    args = parser.parse_args()
    patterns = [part for value in args.only for part in value.split(",") if part]
    first = [suite for entry in FIRST for suite in expand(entry) if selected(suite, patterns)]
    rest = [suite for entry in SUITES for suite in expand(entry) if selected(suite, patterns)]
    if args.list:
        sys.stdout.write("".join(f"{suite.name}\n" for suite in first + rest))
        return 0
    if not first + rest:
        sys.stderr.write(f"no suite matches {patterns}; see --list\n")
        return 2
    if args.jobs < 1:
        sys.stderr.write("--jobs must be at least 1\n")
        return 2
    # The handle holds the lock until this process exits: keep the reference, never close it early.
    _lock = suite_lock.acquire(f"{ROOT} run-tests.sh")
    tmp = Path(os.environ["TMPDIR"])
    (tmp / "runs").mkdir(parents=True, exist_ok=True)
    run_dir = Path(
        tempfile.mkdtemp(prefix=time.strftime("%m%d-%H%M%S-"), dir=tmp / "runs")
    )
    (run_dir / "logs").mkdir()
    durations_file = tmp / "runs" / "durations.json"
    for index, suite in enumerate(first + rest):
        suite.index = index
    try:
        durations: dict[str, float] = json.loads(durations_file.read_text())
    except (OSError, ValueError):
        durations = {}
    runner = Runner(run_dir, stream=args.serial)
    signal.signal(signal.SIGTERM, lambda *_: (runner.stop(), sys.exit(143)))
    started = time.monotonic()
    try:
        for suite in first:
            if runner.run(suite).result != "PASS":
                runner.stopping = True
        if args.serial:
            for suite in rest:
                if runner.run(suite).result != "PASS":
                    runner.stopping = True
        else:
            jobs = min(args.jobs, len(rest)) or 1
            sys.stdout.write(
                f"{len(rest)} suites on {jobs} workers; logs in {run_dir / 'logs'}\n"
            )
            ordered = sorted(
                rest, key=lambda suite: -durations.get(suite.name, float("inf"))
            )
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                list(pool.map(runner.run, ordered))
    except KeyboardInterrupt:
        runner.stop()
        raise
    wall = time.monotonic() - started
    suites = first + rest
    durations.update(
        {
            suite.name: round(suite.seconds, 1)
            for suite in suites
            if suite.result != "not run"
        }
    )
    durations_file.write_text(json.dumps(durations, indent=1, sort_keys=True) + "\n")
    failed = [suite for suite in suites if suite.result.startswith("FAIL")]
    for suite in failed:
        lines = tail(runner.log(suite))
        sys.stdout.write(
            f"\n==> {suite.name} ({suite.result}), last {len(lines)} lines of "
            f"{runner.log(suite)}\n{''.join(lines)}"
        )
    sys.stdout.write(f"\n{'suite':<44} {'seconds':>8}  result\n")
    for suite in suites:
        sys.stdout.write(f"{suite.name:<44} {suite.seconds:8.1f}  {suite.result}\n")
    passed = sum(suite.result == "PASS" for suite in suites)
    sys.stdout.write(
        f"wall {wall:.1f}s, {passed} passed, {len(failed)} failed, "
        f"{len(suites) - passed - len(failed)} not run; logs in {run_dir / 'logs'}\n"
    )
    return 0 if passed == len(suites) else 1


def tail(log: Path) -> list[str]:
    return log.read_text(errors="replace").splitlines(keepends=True)[-TAIL_LINES:]


if __name__ == "__main__":
    raise SystemExit(main())
