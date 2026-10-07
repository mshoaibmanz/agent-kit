#!/usr/bin/env python3
"""tests/run_suites.py: a stop that lands while a suite is starting still stops that suite."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_suites  # noqa: E402


class StopsWhileStarting(run_suites.Runner):
    """stop() runs inside run(), after its `stopping` check and before the child starts: the window a
    SIGTERM or a failed first suite can hit."""

    def log(self, suite: run_suites.Suite) -> Path:
        self.stop()
        return super().log(suite)


class RunnerTests(unittest.TestCase):
    def test_a_child_started_after_stop_is_killed(self) -> None:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            run_dir = Path(temp)
            (run_dir / "logs").mkdir()
            runner = StopsWhileStarting(run_dir, stream=False)
            suite = run_suites.Suite("sleeper", ["sleep", "30"])
            started = time.monotonic()
            runner.run(suite)
            self.assertLess(time.monotonic() - started, 10, "the child outlived the stop")
            self.assertNotEqual(suite.result, "PASS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
