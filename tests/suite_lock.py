"""One machine-wide lock for the release suites, so concurrent runs from any checkout queue.

The lock lives outside every checkout and outside TMPDIR (each caller points TMPDIR at its own
fixture directory). The kernel drops a flock when its holder dies, so there is no stale lock to
clear. A runner exports SUITE_LOCK_HELD to its children so a nested run_hooks.py does not wait
on its own parent.
"""

from pathlib import Path
import fcntl
import os
import sys
import time
from typing import IO

LOCK_PATH = Path(os.environ.get("AGENT_KIT_SUITE_LOCK") or "/tmp/agent-kit-suite.lock")
HELD = "AGENT_KIT_SUITE_LOCK_HELD"


def acquire(label: str) -> IO[str] | None:
    """Block until this process holds the lock; return the handle that keeps it.

    Returns None when an enclosing runner already holds it.
    """
    if os.environ.get(HELD):
        return None
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        holder = handle.read().strip() or "unknown holder"
        sys.stderr.write(f"waiting for the suite lock {LOCK_PATH} ({holder})\n")
        sys.stderr.flush()
        started = time.monotonic()
        fcntl.flock(handle, fcntl.LOCK_EX)
        sys.stderr.write(
            f"suite lock acquired after {time.monotonic() - started:.0f}s\n"
        )
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid {os.getpid()} {label} since {time.strftime('%H:%M:%S')}\n")
    handle.flush()
    os.environ[HELD] = "1"
    return handle
