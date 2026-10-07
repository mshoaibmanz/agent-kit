#!/usr/bin/env python3
"""The work-root tmp/ sweep (bin/lib/tmp_sweep.py through bin/claude-gc), `agent-task close`'s script
promotion, and a new work root's editor settings. Every case builds its own scratch HOME and work
root; nothing reads the real ~/agent-work or ~/.claude."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SOURCE = Path(os.environ.get("AGENT_KIT_SOURCE", Path(__file__).resolve().parents[2]))
OLD = time.time() - 30 * 86400


def age(path: Path, when: float = OLD) -> None:
    """Set path and everything under it to when."""
    for top, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(top, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                    "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.root = self.base / "work"
        self.kit_env = self.base / "kit.env"
        self.kit_env.write_text("CODE_DIRS_JSON=[]\n")
        (self.home / ".claude/state").mkdir(parents=True)
        self.env = {**os.environ, "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                    "CLAUDE_OUT_ROOT": str(self.root), "KIT_ENV": str(self.kit_env), "TMPDIR": str(self.base),
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        self.env.pop("AGENT_KIT_DIR", None)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def item(self, project: str, item: str, status: str, closed: str = "") -> Path:
        d = self.root / "projects" / project / "items" / item
        for sub in ("out", "briefs", "tmp"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        (d / "task.json").write_text(json.dumps({"item": item, "status": status, **({"closed": closed} if closed else {})}))
        (d / "HANDOFF.md").write_text("# handoff\n")
        return d

    def gc(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SOURCE / "bin/claude-gc"), *args], capture_output=True, text=True,
                              env=self.env, timeout=120)

    def report(self) -> Path:
        return self.home / ".claude/state/tmp-sweep.tsv"


class Sweep(Fixture):
    def build(self) -> Path:
        d = self.item("p", "done", "closed", "2020-01-01")
        tmp = d / "tmp"
        (tmp / "venv-run/.venv/lib").mkdir(parents=True)
        (tmp / "venv-run/.venv/lib/site.py").write_text("x = 1\n")
        (tmp / "web/node_modules/pkg").mkdir(parents=True)
        (tmp / "web/node_modules/pkg/index.js").write_text("1\n")
        (tmp / "fake-home/.claude").mkdir(parents=True)
        (tmp / "fake-home/notes.md").write_text("fixture\n")
        (tmp / "clone").mkdir()
        git("init", "-q", cwd=tmp / "clone")
        (tmp / "own-repo").mkdir()
        git("init", "-q", cwd=tmp / "own-repo")
        (tmp / "own-repo/work.py").write_text("print(1)\n")
        git("add", "work.py", cwd=tmp / "own-repo")
        git("commit", "-qm", "local only", cwd=tmp / "own-repo")
        (tmp / "big.bin").write_bytes(b"0" * (1024 * 1024 + 1))
        (tmp / "big.csv").write_bytes(b"0" * (1024 * 1024 + 1))
        (tmp / "probe/query.sql").parent.mkdir()
        (tmp / "probe/query.sql").write_text("SELECT 1\n")
        (tmp / "probe/__pycache__").mkdir()
        (tmp / "probe/__pycache__/x.pyc").write_bytes(b"\0")
        (tmp / "check.py").write_text('"""A check."""\n')
        (tmp / "rows.csv").write_text("a,b\n")
        (tmp / "run.log").write_text("log\n")
        (d / "out/salvage").mkdir()
        (d / "out/salvage/check.py").write_text("an earlier salvage\n")
        (d / "out/keep.md").write_text("evidence\n")
        age(d)
        open_item = self.item("p", "open", "active")
        (open_item / "tmp/x.py").write_text("1\n")
        age(open_item)
        recent = self.item("p", "recent", "merged")
        (recent / "tmp/y.py").write_text("1\n")
        (self.root / "projects/p/scripts").mkdir(parents=True)
        (self.root / "projects/p/scripts/tool.py").write_text("1\n")
        return d

    def lines(self) -> dict[str, tuple[str, str]]:
        out = {}
        for line in self.report().read_text().splitlines():
            if line and not line.startswith("#"):
                action, path, kind, _size, _fp = line.split("\t")
                out[path] = (action, kind)
        return out

    def test_scan_lists_closed_items_by_kind_and_apply_does_exactly_that(self) -> None:
        d = self.build()
        tmp = d / "tmp"
        proc = self.gc()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = self.lines()
        self.assertEqual({Path(p).relative_to(tmp).as_posix(): v for p, v in lines.items()}, {
            "venv-run/.venv": ("delete", ".venv"),
            "web/node_modules": ("delete", "node_modules"),
            "fake-home": ("delete", "scratch home"),
            "clone": ("delete", "git checkout"),
            "own-repo": ("keep", "git checkout with unpushed work"),
            "big.bin": ("delete", "file over 1 MB"),
            "big.csv": ("delete", "file over 1 MB"),
            "probe/__pycache__": ("delete", "__pycache__"),
            "probe/query.sql": ("salvage", "small source or data"),
            "check.py": ("salvage", "small source or data"),
            "rows.csv": ("salvage", "small source or data"),
        })
        text = self.report().read_text()
        self.assertIn("# not swept: p/items/open: status active", text.replace("projects/", ""))
        self.assertIn("p/items/recent: tmp/ changed in the last 14 days", text)
        digest = hashlib.sha256(self.report().read_bytes()).hexdigest()
        self.assertIn(f"claude-gc --sweep-tmp --report-sha256 {digest}", proc.stdout)
        self.assertTrue((tmp / "check.py").exists(), "the scan changed nothing")

        refused = self.gc("--sweep-tmp", "--report-sha256", "0" * 64)
        self.assertEqual(refused.returncode, 1)
        self.assertTrue((tmp / "big.bin").exists())

        proc = self.gc("--sweep-tmp", "--report-sha256", digest)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for gone in ("venv-run/.venv", "web/node_modules", "fake-home", "clone", "big.bin", "big.csv", "probe/__pycache__",
                     "check.py", "rows.csv", "probe/query.sql"):
            self.assertFalse((tmp / gone).exists(), gone)
        for kept in ("own-repo/work.py", "run.log"):
            self.assertTrue((tmp / kept).exists(), kept)
        salvage = d / "out/salvage"
        self.assertEqual((salvage / "check.py").read_text(), "an earlier salvage\n")
        self.assertEqual((salvage / "check.py.salvaged-1").read_text(), '"""A check."""\n')
        self.assertEqual((salvage / "probe/query.sql").read_text(), "SELECT 1\n")
        self.assertEqual((salvage / "rows.csv").read_text(), "a,b\n")
        self.assertEqual((d / "out/keep.md").read_text(), "evidence\n")
        self.assertTrue((d / "HANDOFF.md").is_file() and (self.root / "projects/p/scripts/tool.py").is_file())
        self.assertTrue((self.root / "projects/p/items/open/tmp/x.py").exists())
        self.assertTrue((self.root / "projects/p/items/recent/tmp/y.py").exists())

    def test_apply_skips_what_changed_reopened_or_lies_outside_a_tmp(self) -> None:
        d = self.build()
        tmp = d / "tmp"
        self.assertEqual(self.gc().returncode, 0)
        (tmp / "rows.csv").write_text("a,b,c\n")
        outside = d / "out/keep.md"
        text = self.report().read_text() + f"delete\t{outside}\tforged\t1\t1:1\n" + f"delete\t{d / 'tmp/../out'}\tforged\t1\t1\n"
        self.report().write_text(text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        proc = self.gc("--sweep-tmp", "--report-sha256", digest)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((tmp / "rows.csv").exists(), "a file changed since the report is skipped")
        self.assertIn("skip (changed since the report)", proc.stdout)
        self.assertTrue(outside.exists() and (d / "out").is_dir())
        self.assertEqual(proc.stdout.count("skip (outside an item's tmp/)"), 2)

        age(d)
        self.assertEqual(self.gc().returncode, 0)
        self.assertIn("rows.csv", self.report().read_text())
        (d / "task.json").write_text(json.dumps({"item": "done", "status": "active"}))
        proc = self.gc("--sweep-tmp", "--report-sha256", hashlib.sha256(self.report().read_bytes()).hexdigest())
        self.assertIn("skip (item reopened)", proc.stdout)
        self.assertTrue((tmp / "rows.csv").exists())

    def test_grace_comes_from_tmp_sweep_days(self) -> None:
        d = self.item("p", "done", "closed")
        (d / "tmp/a.py").write_text("1\n")
        age(d, time.time() - 3 * 86400)
        self.assertEqual(self.gc().returncode, 0)
        self.assertNotIn("a.py", self.report().read_text())
        self.kit_env.write_text("CODE_DIRS_JSON=[]\nTMP_SWEEP_DAYS=2\n")
        self.assertEqual(self.gc().returncode, 0)
        self.assertIn("salvage\t" + str((d / "tmp/a.py").resolve()), self.report().read_text())


class Close(Fixture):
    def agent_task(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SOURCE / "bin/agent-task"), *args], capture_output=True, text=True,
                              env=self.env, input=stdin, timeout=60)

    def test_close_promotes_a_named_script_with_its_use_line_and_lists_the_rest(self) -> None:
        self.assertEqual(self.agent_task("bind", "proj/item-1", "--session", "s1").returncode, 0)
        d = self.root / "projects/proj/items/item-1"
        (d / "tmp/sub").mkdir(parents=True)
        (d / "tmp/sub/reconcile.py").write_text('"""Reconcile rows between two exports."""\n')
        (d / "tmp/probe.sh").write_text("#!/bin/sh\n# Probe the queue depth.\n")
        (d / "tmp/home/.claude").mkdir(parents=True)
        (d / "tmp/home/fixture.py").write_text("1\n")
        (self.root / "projects/proj/scripts/known.sql").write_text("-- known\n")
        (d / "tmp/known.sql").write_text("-- a copy\n")
        proc = self.agent_task("close", "proj/item-1", "--promote", "sub/reconcile.py", "--pr", "https://example.com/pr/1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.root / "projects/proj/scripts/reconcile.py").read_text(),
                         '"""Reconcile rows between two exports."""\n')
        self.assertTrue((d / "tmp/sub/reconcile.py").exists(), "promotion copies; the sweep salvages the original")
        index = (self.root / "projects/proj/INDEX.md").read_text()
        self.assertIn("- `scripts/reconcile.py` (python): Reconcile rows between two exports.\n"
                      "  - use: `python3 scripts/reconcile.py`: Reconcile rows between two exports.", index)
        self.assertIn("tmp/probe.sh: Probe the queue depth.", proc.stdout)
        self.assertNotIn("fixture.py", proc.stdout)
        self.assertNotIn("known.sql", proc.stdout)
        task = json.loads((d / "task.json").read_text())
        self.assertEqual((task["status"], task["pr"]), ("closed", "https://example.com/pr/1"))
        self.assertIn("closed", task)

        proc = self.agent_task("close", "proj/item-1", "--no-promote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("probe.sh", proc.stdout)
        proc = self.agent_task("close", "proj/item-1", "--promote", "missing.py")
        self.assertNotEqual(proc.returncode, 0)


class WorkRoot(Fixture):
    def test_a_new_work_root_gets_editor_excludes_and_a_users_file_stays(self) -> None:
        proc = subprocess.run([sys.executable, str(SOURCE / "bin/agent-task"), "bind", "proj/item-1", "--session", "s1"],
                              capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        settings = json.loads((self.root / ".vscode/settings.json").read_text())
        for key in ("files.watcherExclude", "search.exclude"):
            self.assertEqual(set(settings[key]), {"**/tmp/**", "**/.venv/**", "**/node_modules/**", "**/__pycache__/**",
                                                  "**/worktrees/**"})
        self.assertEqual((self.root / ".cursorignore").read_text().split(),
                         ["tmp/", ".venv/", "node_modules/", "__pycache__/", "worktrees/"])

        sys.path.insert(0, str(SOURCE / "hooks/lib"))
        import agent_task

        own = self.base / "own"
        (own / ".vscode").mkdir(parents=True)
        (own / ".vscode/settings.json").write_text('{"editor.tabSize": 2}\n')
        agent_task.init_work_root(own)
        self.assertEqual((own / ".vscode/settings.json").read_text(), '{"editor.tabSize": 2}\n')
        self.assertTrue((own / ".cursorignore").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
