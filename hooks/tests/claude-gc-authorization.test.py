#!/usr/bin/env python3
"""Report authorization and concurrency regressions using disposable Git fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

GC = Path(
    os.environ.get("GC_BIN", Path(__file__).resolve().parents[2] / "bin/claude-gc")
)


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="gc-authorization-")
        self.addCleanup(self.scratch.cleanup)
        self.home = Path(self.scratch.name)
        self.overlay = self.home / "synthetic-kit.env"
        self.overlay.write_text('CODE_DIRS=""\n')
        self.env = {
            "HOME": str(self.home),
            "KIT_ENV": str(self.overlay),
            "PATH": os.environ["PATH"],
            "TMPDIR": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_COUNT": "0",
        }
        library = self.home / ".claude/hooks/lib"
        library.mkdir(parents=True)
        (library / "hook-io").write_text('kit_env() { CODE_DIRS=""; }\n')
        self.repo, self.wt = self.home / "repo", self.home / "candidate"
        self.git("init", "-q", str(self.repo))
        self.git("-C", str(self.repo), "commit", "--allow-empty", "-qm", "baseline")
        self.base = self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout.strip()
        self.git("-C", str(self.repo), "branch", "proof-base")
        self.git(
            "-C", str(self.repo), "worktree", "add", "-qb", "candidate", str(self.wt)
        )
        for path in [*self.wt.rglob("*"), self.wt]:
            os.utime(path, (946684800, 946684800))
        self.report = self.home / "reviewed.txt"
        self.write_report()
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        help_result = self.gc("--help")
        self.digest_supported = (
            "--report-sha256" in help_result.stdout + help_result.stderr
        )

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )

    def gc(
        self, *args: str, env: dict | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(GC), *args],
            env=env or self.env,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def write_report(self, proof: str = "ancestor", merge: str = "-") -> None:
        self.report.write_text(
            "# reviewed fixture\n"
            + "\t".join(
                [
                    str(self.wt),
                    "candidate",
                    "synthetic verified merge",
                    "1K",
                    self.base,
                    "proof-base",
                    self.base,
                    proof,
                    merge,
                ]
            )
            + "\n"
        )

    def consume(self) -> subprocess.CompletedProcess[str]:
        flags = ["--prune-worktrees", "--report-file", str(self.report)]
        if self.digest_supported:
            flags += ["--report-sha256", self.approved]
        return self.gc(*flags)

    def test_explicit_generation_does_not_replace_reviewed_report(self) -> None:
        before = self.report.read_bytes()
        result = self.gc("--report-file", str(self.report))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.report.read_bytes(), before)

    def test_changed_digest_preserves_worktree(self) -> None:
        self.report.write_text(self.report.read_text() + "# changed after approval\n")
        self.assertNotEqual(self.consume().returncode, 0)
        self.assertTrue(self.wt.exists())

    def test_empty_commit_after_authorization_preserves_worktree(self) -> None:
        self.git(
            "-C", str(self.wt), "commit", "--allow-empty", "-qm", "unmerged later work"
        )
        self.consume()
        self.assertTrue(self.wt.exists())

    def test_branch_change_preserves_worktree(self) -> None:
        self.git("-C", str(self.wt), "branch", "-m", "renamed")
        self.consume()
        self.assertTrue(self.wt.exists())

    def test_base_change_preserves_worktree(self) -> None:
        self.git(
            "-C", str(self.repo), "commit", "--allow-empty", "-qm", "base advanced"
        )
        head = self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout.strip()
        self.git("-C", str(self.repo), "update-ref", "refs/heads/proof-base", head)
        self.consume()
        self.assertTrue(self.wt.exists())

    def test_unchanged_verified_ancestor_can_be_removed(self) -> None:
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.wt.exists())

    def test_unchanged_verified_squash_can_be_removed(self) -> None:
        self.git("-C", str(self.wt), "commit", "--allow-empty", "-qm", "topic commit")
        authorized = self.git("-C", str(self.wt), "rev-parse", "HEAD").stdout.strip()
        self.git(
            "-C", str(self.repo), "commit", "--allow-empty", "-qm", "squashed merge"
        )
        merged = self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout.strip()
        self.git("-C", str(self.repo), "update-ref", "refs/heads/proof-base", merged)
        self.write_report("squash", merged)
        self.report.write_text(
            self.report.read_text().replace(
                self.base + "\tproof-base\t" + self.base,
                authorized + "\tproof-base\t" + merged,
            )
        )
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.wt.exists())

    def test_squash_merge_outside_base_preserves_worktree(self) -> None:
        self.git(
            "-C", str(self.repo), "commit", "--allow-empty", "-qm", "outside merge"
        )
        outside = self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout.strip()
        self.write_report("squash", outside)
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.consume()
        self.assertTrue(self.wt.exists())

    def test_verified_detached_head_can_be_removed(self) -> None:
        self.git("-C", str(self.wt), "checkout", "--detach", "-q", "HEAD")
        self.report.write_text(
            self.report.read_text().replace("\tcandidate\t", "\t(detached)\t")
        )
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.wt.exists())

    def test_existing_head_lock_preserves_worktree_and_lock(self) -> None:
        lock = Path(
            self.git(
                "-C",
                str(self.wt),
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "HEAD",
            ).stdout.strip()
            + ".lock"
        )
        lock.write_text("owned by another actor\n")
        self.consume()
        self.assertTrue(self.wt.exists())
        self.assertEqual(lock.read_text(), "owned by another actor\n")

    def test_missing_digest_refuses_explicit_consumption(self) -> None:
        run = self.gc("--prune-worktrees", "--report-file", str(self.report))
        self.assertNotEqual(run.returncode, 0)
        self.assertTrue(self.wt.exists())

    def branch_lock(self) -> Path:
        return Path(
            self.git(
                "-C",
                str(self.wt),
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "refs/heads/candidate",
            ).stdout.strip()
            + ".lock"
        )

    def test_foreign_branch_lock_is_preserved(self) -> None:
        lock = self.branch_lock()
        lock.write_text("foreign actor\n")
        blocked = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "update-ref",
                "refs/heads/candidate",
                self.base,
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("File exists", blocked.stderr)
        self.consume()
        self.assertTrue(self.wt.exists())
        self.assertEqual(lock.read_text(), "foreign actor\n")
        lock.unlink()
        self.git("-C", str(self.repo), "update-ref", "refs/heads/candidate", self.base)

    def test_packed_nested_branch_can_be_removed(self) -> None:
        self.git("-C", str(self.wt), "branch", "-m", "team/candidate")
        self.git("-C", str(self.repo), "pack-refs", "--all", "--prune")
        self.assertFalse((self.repo / ".git/refs/heads/team").exists())
        self.report.write_text(
            self.report.read_text().replace("\tcandidate\t", "\tteam/candidate\t")
        )
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.wt.exists())

    def head_lock(self) -> Path:
        return Path(
            self.git(
                "-C",
                str(self.wt),
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "HEAD",
            ).stdout.strip()
            + ".lock"
        )

    def test_term_after_atomic_link_cleans_owned_and_preserves_foreign(self) -> None:
        lock = self.branch_lock()
        lock.write_text("foreign actor\n")
        kit = self.home / "signal-kit"
        (kit / "bin").mkdir(parents=True)
        (kit / "hooks/lib").mkdir(parents=True)
        script = kit / "bin/claude-gc"
        shutil.copyfile(GC, script)
        helper = kit / "hooks/lib/gc-lock.py"
        source = (GC.parent.parent / "hooks/lib/gc-lock.py").read_text()
        source = source.replace("import fcntl\n", "import fcntl\nimport signal\n")
        injection = (
            "\noriginal_link = os.link\n"
            "def inject_link(*args, **kwargs):\n"
            "    result = original_link(*args, **kwargs)\n"
            '    Path(os.environ["HOME"], "signal-injected").touch()\n'
            "    os.kill(os.getppid(), signal.SIGTERM)\n"
            "    return result\n"
            "os.link = inject_link\n\n"
        )
        helper.write_text(
            source.replace("def manifest(", injection + "def manifest(", 1)
        )
        flags = [
            "--prune-worktrees",
            "--report-file",
            str(self.report),
            "--report-sha256",
            self.approved,
        ]
        run = subprocess.run(
            ["bash", str(script), *flags],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(run.returncode, 143, run.stdout + run.stderr)
        self.assertTrue(
            (self.home / "signal-injected").exists(), "injection did not fire"
        )
        self.assertTrue(self.wt.exists())
        self.assertFalse(self.head_lock().exists(), "owned HEAD lock leaked")
        self.assertTrue(lock.exists(), "signal cleanup removed another actor's lock")
        self.assertEqual(lock.read_text(), "foreign actor\n")

    def wait_for(self, path: Path, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("fixture process exited before barrier")
            time.sleep(0.02)
        self.assertTrue(path.exists(), "fixture barrier timed out")

    def interrupt_prune(self) -> None:
        commands = self.home / "barrier-bin"
        commands.mkdir()
        git = shutil.which("git", path=self.env["PATH"])
        wrapper = commands / "git"
        wrapper.write_text(
            '#!/bin/sh\ncase "$*" in *"worktree remove"*)\nif [ "${GC_KILL_BARRIER:-}" = 1 ]; then touch "$HOME/remove-ready"; while [ ! -f "$HOME/release-remove" ]; do sleep .05; done; fi;; esac\nexec '
            + str(git)
            + ' "$@"\n'
        )
        wrapper.chmod(0o700)
        flags = [
            "--prune-worktrees",
            "--report-file",
            str(self.report),
            "--report-sha256",
            self.approved,
        ]
        env = dict(self.env, PATH=f"{commands}:{self.env['PATH']}", GC_KILL_BARRIER="1")
        process = subprocess.Popen(
            ["bash", str(GC), *flags],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            self.wait_for(self.home / "remove-ready", process)
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
        self.assertTrue(self.head_lock().exists(), "HEAD barrier never held a lock")
        self.assertTrue(self.branch_lock().exists(), "branch barrier never held a lock")

    def test_sigkill_retries_owned_locks_and_preserves_foreign_lock(self) -> None:
        self.interrupt_prune()
        self.report.write_text("# empty recovery pass\n")
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.head_lock().exists())
        self.assertFalse(
            self.branch_lock().exists(), "owned branch lock was not recovered"
        )
        branch = self.branch_lock()
        branch.write_text("foreign actor installed after interruption\n")
        self.write_report()
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        retry = self.consume()
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertTrue(self.wt.exists())
        self.assertEqual(
            branch.read_text(), "foreign actor installed after interruption\n"
        )
        branch.unlink()
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.wt.exists())

    def test_killed_prune_recovers_across_report_directory_and_base_advance(
        self,
    ) -> None:
        self.interrupt_prune()
        self.git("-C", str(self.repo), "commit", "--allow-empty", "-qm", "advance base")
        advanced = self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout.strip()
        self.git("-C", str(self.repo), "update-ref", "refs/heads/proof-base", advanced)
        self.report = self.home / "new-review" / "advanced.txt"
        self.report.parent.mkdir()
        self.write_report()
        self.report.write_text(
            self.report.read_text().replace(
                "\tproof-base\t" + self.base, "\tproof-base\t" + advanced
            )
        )
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(
            self.wt.exists(), "old report's locks blocked a new reviewed report"
        )

    def test_sigkill_recovery_preserves_replaced_foreign_lock_inode(self) -> None:
        self.interrupt_prune()
        branch = self.branch_lock()
        replacement = branch.with_suffix(".foreign")
        replacement.write_text("foreign actor replaced interrupted lock\n")
        os.replace(replacement, branch)
        foreign_inode = branch.stat().st_ino
        self.assertEqual(self.consume().returncode, 0)
        self.assertTrue(self.wt.exists())
        self.assertFalse(self.head_lock().exists(), "owned HEAD was not recovered")
        self.assertEqual(branch.stat().st_ino, foreign_inode)
        self.assertEqual(
            branch.read_text(), "foreign actor replaced interrupted lock\n"
        )
        branch.unlink()
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.wt.exists())

    def test_brackets_in_report_name_recover_literal_owner(self) -> None:
        self.report = self.home / "selected[1].txt"
        self.write_report()
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.interrupt_prune()
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.wt.exists())

    def test_stale_owner_pid_reuse_does_not_block_flock_recovery(self) -> None:
        self.interrupt_prune()
        root = self.home / ".claude/state/gc-owners"
        owners = (
            list(root.iterdir())
            if root.exists()
            else list(self.report.parent.glob(f".{self.report.name}.gc-owner-*"))
        )
        self.assertEqual(len(owners), 1)
        record = owners[0] / "manifest.json"
        data = json.loads(record.read_text())
        data["pid"] = (
            os.getpid()
        )  # A reused PID is alive, but does not hold owner flock.
        record.write_text(json.dumps(data))
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.wt.exists())

    def test_reviewed_without_base_proof_cannot_prune(self) -> None:
        self.git("-C", str(self.wt), "commit", "--allow-empty", "-qm", "unmerged topic")
        topic = self.git("-C", str(self.wt), "rev-parse", "HEAD").stdout.strip()
        self.write_report("reviewed")
        self.report.write_text(
            self.report.read_text()
            .replace(self.base, topic)
            .replace("\tproof-base\t" + topic, "\t-\t-")
        )
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.consume()
        self.assertTrue(self.wt.exists(), "explicit digest does not prove a merge")

    def test_default_reviewed_without_base_proof_cannot_prune(self) -> None:
        self.git("-C", str(self.wt), "commit", "--allow-empty", "-qm", "unmerged topic")
        topic = self.git("-C", str(self.wt), "rev-parse", "HEAD").stdout.strip()
        self.write_report("reviewed")
        self.report.write_text(
            self.report.read_text()
            .replace(self.base, topic)
            .replace("\tproof-base\t" + topic, "\t-\t-")
        )
        default = self.home / ".claude/state/prunable-worktrees.txt"
        default.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.report, default)
        self.gc("--prune-worktrees")
        self.assertTrue(
            self.wt.exists(), "default legacy reviewed proof pruned unmerged work"
        )

    def generation_env(self) -> dict[str, str]:
        (self.home / ".claude/hooks/lib/hook-io").write_text(
            'kit_env() { CODE_DIRS="$HOME"; }\n'
        )
        self.overlay.write_text("CODE_DIRS=" + str(self.home) + "\n")
        self.git(
            "-C",
            str(self.repo),
            "update-ref",
            "refs/remotes/origin/proof-base",
            self.base,
        )
        self.git("-C", str(self.wt), "checkout", "--detach", "-q", "HEAD")
        return self.env

    def test_killed_owner_waits_for_live_child_before_recovery(self) -> None:
        commands = self.home / "child-barrier-bin"
        commands.mkdir()
        git = shutil.which("git", path=self.env["PATH"])
        wrapper = commands / "git"
        wrapper.write_text(
            '#!/bin/sh\ncase "$*" in *"worktree remove"*)\n'
            'if [ "${GC_CHILD_BARRIER:-}" = 1 ]; then echo $$ > "$HOME/child-pid"; '
            'while [ ! -f "$HOME/release-child" ]; do sleep .05; done; exit 72; fi;; esac\nexec '
            + str(git)
            + ' "$@"\n'
        )
        wrapper.chmod(0o700)
        flags = [
            "--prune-worktrees",
            "--report-file",
            str(self.report),
            "--report-sha256",
            self.approved,
        ]
        env = dict(
            self.env, PATH=f"{commands}:{self.env['PATH']}", GC_CHILD_BARRIER="1"
        )
        process = subprocess.Popen(
            ["bash", str(GC), *flags],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            self.wait_for(self.home / "child-pid", process)
            child = int((self.home / "child-pid").read_text())
            process.kill()
            process.wait(timeout=5)
            os.kill(child, 0)
            blocked = self.consume()
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("report is in use", blocked.stdout + blocked.stderr)
            self.assertTrue(self.wt.exists())
            prior = self.report
            self.report = self.home / "other-child-report.txt"
            self.write_report()
            self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
            self.assertEqual(self.consume().returncode, 0)
            self.assertTrue(
                self.head_lock().exists(), "live child's HEAD lock was recovered"
            )
            self.assertTrue(
                self.branch_lock().exists(), "live child's branch lock was recovered"
            )
            self.assertTrue(self.wt.exists())
            (self.home / "release-child").touch()
            process.communicate(timeout=5)
            self.assertEqual(self.consume().returncode, 0)
            self.assertFalse(self.wt.exists())
            self.assertTrue(prior.exists())
        finally:
            (self.home / "release-child").touch()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)

    def test_generator_does_not_authorize_head_changed_during_sizing(self) -> None:
        env = self.generation_env()
        commands = self.home / "size-barrier-bin"
        commands.mkdir()
        du, git = (
            shutil.which("du", path=env["PATH"]),
            shutil.which("git", path=env["PATH"]),
        )
        wrapper = commands / "du"
        wrapper.write_text(
            "#!/bin/sh\n"
            + str(git)
            + ' -C "$GC_RACE_WORKTREE" -c core.hooksPath=/dev/null -c commit.gpgsign=false -c user.name=Fixture -c user.email=fixture@example.invalid commit --allow-empty -qm "new detached work"\nexec '
            + str(du)
            + ' "$@"\n'
        )
        wrapper.chmod(0o700)
        generated = self.home / "generated.txt"
        run = self.gc(
            "--report-file",
            str(generated),
            env=dict(
                env, PATH=f"{commands}:{env['PATH']}", GC_RACE_WORKTREE=str(self.wt)
            ),
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        current = self.git("-C", str(self.wt), "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(
            current, self.base, "generation fixture did not change HEAD"
        )
        self.assertNotIn(current, generated.read_text())
        self.report = generated
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.consume()
        self.assertTrue(self.wt.exists())

    def test_generator_emits_rechecked_base_proof(self) -> None:
        self.generation_env()
        self.report = self.home / "generated.txt"
        run = self.gc("--report-file", str(self.report))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        rows = [
            line.split("\t")
            for line in self.report.read_text().splitlines()
            if not line.startswith("#")
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][7], "ancestor")
        self.git(
            "-C", str(self.repo), "commit", "--allow-empty", "-qm", "base advances"
        )
        advanced = self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout.strip()
        self.git(
            "-C",
            str(self.repo),
            "update-ref",
            "refs/remotes/origin/proof-base",
            advanced,
        )
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        self.consume()
        self.assertTrue(self.wt.exists())

    def merged_generation(self, head_matches: bool) -> list[list[str]]:
        (self.home / ".claude/hooks/lib/hook-io").write_text(
            'kit_env() { CODE_DIRS="$HOME"; }\n'
        )
        self.overlay.write_text("CODE_DIRS=" + str(self.home) + "\n")
        self.git(
            "-C", str(self.wt), "commit", "--allow-empty", "-qm", "topic for squash"
        )
        topic = self.git("-C", str(self.wt), "rev-parse", "HEAD").stdout.strip()
        self.git(
            "-C",
            str(self.repo),
            "commit",
            "--allow-empty",
            "-qm",
            "squash merge commit",
        )
        merge = self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout.strip()
        self.git(
            "-C", str(self.repo), "update-ref", "refs/remotes/origin/proof-base", merge
        )
        payload = {
            "state": "MERGED",
            "headRefOid": topic if head_matches else self.base,
            "mergeCommit": {"oid": merge},
            "baseRefName": "proof-base",
        }
        (self.home / "fake-gh-response.json").write_text(json.dumps(payload))
        startup = self.home / "fake-gh-function.sh"
        startup.write_text('gh() { cat "$HOME/fake-gh-response.json"; }\n')
        self.report = self.home / "generated.txt"
        run = self.gc(
            "--report-file", str(self.report), env=dict(self.env, BASH_ENV=str(startup))
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        rows = [
            line.split("\t")
            for line in self.report.read_text().splitlines()
            if not line.startswith("#")
        ]
        self.approved = hashlib.sha256(self.report.read_bytes()).hexdigest()
        if head_matches:
            self.assertEqual(rows[0][4], topic)
            self.assertEqual(rows[0][8], merge)
            self.assertNotEqual(topic, merge)
        return rows

    def test_generated_squash_has_distinct_exact_head_and_merge_proof(self) -> None:
        rows = self.merged_generation(True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][7], "squash")
        self.assertEqual(self.consume().returncode, 0)
        self.assertFalse(self.wt.exists())

    def test_generated_merged_pr_does_not_authorize_later_head(self) -> None:
        self.assertEqual(self.merged_generation(False), [])
        self.consume()
        self.assertTrue(self.wt.exists())

    def test_same_report_generation_is_serial_and_complete(self) -> None:
        commands = self.home / "barrier-bin"
        commands.mkdir()
        actual_mv = subprocess.run(
            ["which", "mv"], env=self.env, capture_output=True, text=True, check=True
        ).stdout.strip()
        wrapper = commands / "mv"
        wrapper.write_text(
            '#!/bin/sh\nif [ "${GC_TEST_PUBLISHER:-}" = a ]; then\n  touch "$HOME/a-ready"\n  n=0\n  while [ ! -f "$HOME/release-a" ] && [ "$n" -lt 100 ]; do sleep .05; n=$((n+1)); done\nfi\nexec '
            + actual_mv
            + ' "$@"\n'
        )
        wrapper.chmod(0o700)
        env = dict(self.env, PATH=f"{commands}:{self.env['PATH']}")
        args = ["bash", str(GC)]
        a = subprocess.Popen(
            args,
            env=dict(env, GC_TEST_PUBLISHER="a"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not (self.home / "a-ready").exists() and time.monotonic() < deadline:
                if a.poll() is not None:
                    stdout, stderr = a.communicate(timeout=5)
                    self.fail(
                        "first publisher did not reach the publication barrier: "
                        + stdout
                        + stderr
                    )
                time.sleep(0.05)
            self.assertTrue((self.home / "a-ready").exists())
            report = self.home / ".claude/state/prunable-worktrees.txt"
            before = report.read_bytes() if report.exists() else None
            b = self.gc(env=dict(env, GC_TEST_PUBLISHER="b"))
            self.assertEqual(report.read_bytes() if report.exists() else None, before)
            (self.home / "release-a").touch()
            stdout, stderr = a.communicate(timeout=10)
            self.assertEqual(a.returncode, 0, stdout + stderr)
            self.assertEqual(b.returncode, 1, b.stdout + b.stderr)
            self.assertIn("report is in use", b.stdout + b.stderr)
            self.assertNotIn("cannot stat", stdout + stderr + b.stdout + b.stderr)
            self.assertNotIn("No such file", stdout + stderr + b.stdout + b.stderr)
            self.assertEqual(
                len(
                    [
                        line
                        for line in report.read_text().splitlines()
                        if line.startswith("#")
                    ]
                ),
                2,
            )
            self.assertEqual(list(report.parent.glob("*.tmp*")), [])
            self.assertEqual(
                list((self.home / ".claude/state/gc-owners").iterdir()), []
            )
            self.assertEqual(list(report.parent.glob(".claude-gc-artifacts-*")), [])
        finally:
            if a.poll() is None:
                os.killpg(a.pid, signal.SIGKILL)
            a.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
