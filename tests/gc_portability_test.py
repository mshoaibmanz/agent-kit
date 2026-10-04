"""Verify canonical/generated GC entrypoints using synthetic roots and no providers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(os.environ.get("GC_PUBLIC_SOURCE", Path(__file__).resolve().parents[1]))
FIXTURES = (
    Path(os.environ.get("TMPDIR", str(SOURCE / ".test-fixtures"))) / "gc-portability"
)


class GCPortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="gc-portability-", dir=FIXTURES
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "synthetic home"
        self.home.mkdir()
        self.kit = self.root / "installed kit"
        (self.kit / "bin").mkdir(parents=True)
        (self.kit / "hooks/lib").mkdir(parents=True)
        (self.kit / "local").mkdir()
        for relative in ("bin/claude-gc", "hooks/lib/gc-lock.py", "hooks/lib/hook-io"):
            shutil.copy2(SOURCE / relative, self.kit / relative)
        self.env = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "TMPDIR": str(self.root),
            "AGENT_KIT_DIR": str(self.kit),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_COUNT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        startup = self.root / "provider-tripwire.sh"
        startup.write_text(
            'gh() { touch "$HOME/provider-called"; return 99; }\nclaude() { touch "$HOME/provider-called"; return 99; }\ncodex() { touch "$HOME/provider-called"; return 99; }\n'
        )
        self.env["BASH_ENV"] = str(startup)
        self.report = self.root / "report.txt"
        self.addCleanup(self.assert_no_provider)

    def assert_no_provider(self) -> None:
        self.assertFalse((self.home / "provider-called").exists())

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
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )

    def generate(self, entry: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                str(entry or self.kit / "bin/claude-gc"),
                "--report-file",
                str(self.report),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def legacy_hook(self) -> None:
        lib = self.home / ".claude/hooks/lib"
        lib.mkdir(parents=True)
        shutil.copyfile(self.kit / "hooks/lib/hook-io", lib / "hook-io")

    def create_worktree(
        self, directory: str = "configured repositories with spaces"
    ) -> tuple[Path, Path]:
        parent = self.root / directory
        repo = parent / "service"
        self.git("init", "-q", "-b", "main", str(repo))
        self.git("-C", str(repo), "commit", "--allow-empty", "-qm", "baseline")
        base = self.git("-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        self.git("-C", str(repo), "update-ref", "refs/remotes/origin/main", base)
        wt = repo / ".claude/worktrees/retained space"
        self.git("-C", str(repo), "worktree", "add", "--detach", "-q", str(wt))
        for path in [*wt.rglob("*"), wt]:
            os.utime(path, (946684800, 946684800))
        return parent, wt

    def configure(self, parents: list[Path], legacy: str = "") -> None:
        (self.kit / "local/setup-paths.env").write_text(
            "CODE_DIRS_JSON="
            + json.dumps([str(p) for p in parents])
            + "\nCODE_DIRS="
            + legacy
            + "\n"
        )

    def test_canonical_report_generation(self) -> None:
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.report.is_file())

    def test_generated_plugin_entrypoint_resolves_kit_helper(self) -> None:
        plugin = self.root / "workflow plugin"
        (plugin / "bin").mkdir(parents=True)
        shutil.move(str(self.kit), plugin / "kit")
        self.kit = plugin / "kit"
        self.env.pop("AGENT_KIT_DIR", None)
        generated = SOURCE / "plugins/workflow/bin/claude-gc"
        self.assertTrue(generated.is_symlink())
        entry = plugin / "bin/claude-gc"
        entry.symlink_to(generated.readlink())
        result = self.generate(entry)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.report.is_file())

    def test_host_bin_relative_symlink_chain_resolves_kit_helper(self) -> None:
        host = self.root / "custom host/bin"
        host.mkdir(parents=True)
        link = host / "gc-alias"
        link.symlink_to(os.path.relpath(self.kit / "bin/claude-gc", host))
        entry = host / "claude-gc"
        entry.symlink_to("gc-alias")
        result = self.generate(entry)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.report.is_file())

    def test_structured_roots_generate_exact_spaced_worktree(self) -> None:
        parent, wt = self.create_worktree()
        self.configure([parent])
        self.legacy_hook()  # Separates JSON enumeration from the legacy library lookup bug.
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(wt) + "\t(detached)\t", self.report.read_text())
        self.assertTrue(wt.exists())

    def test_resolved_kit_overlay_needs_no_legacy_hook_copy(self) -> None:
        parent, wt = self.create_worktree()
        self.configure([parent])
        self.env.pop("AGENT_KIT_DIR", None)
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(wt) + "\t(detached)\t", self.report.read_text())

    def test_structured_roots_take_precedence_and_keep_json_only_transcripts(
        self,
    ) -> None:
        legacy, old_wt = self.create_worktree("legacy")
        parent, wt = self.create_worktree()
        self.configure([parent], str(legacy))
        self.legacy_hook()
        encoded = str(wt).replace("/", "-").replace(".", "-")
        transcript = self.home / ".claude/projects" / encoded
        transcript.mkdir(parents=True)
        payload = transcript / "fixture-transcript.txt"
        payload.write_text("synthetic retained transcript\n")
        for path in (payload, transcript):
            os.utime(path, (946684800, 946684800))
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            payload.is_file(), "JSON-only live worktree transcript was deleted"
        )
        self.assertIn(str(wt) + "\t(detached)\t", self.report.read_text())
        self.assertNotIn(str(old_wt), self.report.read_text())

    def test_empty_structured_array_overrides_legacy_roots(self) -> None:
        legacy, wt = self.create_worktree("legacy")
        self.configure([], str(legacy))
        startup = Path(self.env["BASH_ENV"])
        with startup.open("a") as stream:
            stream.write(
                'git() { if [ "${1:-}" = -C ] && [ -z "${2:-}" ] && [ "${3:-}" = worktree ] && [ "${4:-}" = prune ]; then touch "$HOME/unconfigured-prune"; return 94; fi; command git "$@"; }\n'
            )
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(str(wt), self.report.read_text())
        self.assertFalse(
            (self.home / "unconfigured-prune").exists(),
            "empty roots invoked worktree prune in caller cwd",
        )

    def test_invalid_structured_roots_refuse_before_housekeeping(self) -> None:
        history = self.home / ".claude/file-history"
        history.mkdir(parents=True)
        old = history / "synthetic-old.txt"
        old.write_text("preserve on invalid roots\n")
        os.utime(old, (946684800, 946684800))
        for value in ("not-json", '"scalar"', "[1]", '[""]', '["line\\nroot"]'):
            with self.subTest(value=value):
                (self.kit / "local/setup-paths.env").write_text(
                    "CODE_DIRS_JSON=" + value + "\n"
                )
                result = self.generate()
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(old.is_file())
                self.assertFalse(self.report.exists())

    def test_legacy_roots_remain_supported(self) -> None:
        parent, wt = self.create_worktree("legacy")
        (self.kit / "local/setup-paths.env").write_text(
            "CODE_DIRS=" + str(parent) + "\n"
        )
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(wt) + "\t(detached)\t", self.report.read_text())

    def test_structured_root_expands_home_without_splitting_spaces(self) -> None:
        _, wt = self.create_worktree("synthetic home/repositories")
        self.configure([Path("~/repositories")])
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(wt) + "\t(detached)\t", self.report.read_text())

    def test_custom_claude_config_dir_preserves_default_root_history(self) -> None:
        history = self.home / ".claude/file-history"
        history.mkdir(parents=True)
        old = history / "synthetic-old.txt"
        old.write_text("default root is outside selected config\n")
        os.utime(old, (946684800, 946684800))
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "custom Claude config")
        custom = Path(self.env["CLAUDE_CONFIG_DIR"])
        selected_history = custom / "file-history"
        selected_history.mkdir(parents=True)
        selected_old = selected_history / "selected-old.txt"
        selected_old.write_text("synthetic expired selected-root history\n")
        os.utime(selected_old, (946684800, 946684800))
        result = subprocess.run(
            ["bash", str(self.kit / "bin/claude-gc")],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(old.is_file())
        self.assertEqual(old.read_text(), "default root is outside selected config\n")
        self.assertFalse(selected_old.exists())
        self.assertTrue((custom / "state/prunable-worktrees.txt").is_file())
        self.assertFalse((self.home / ".claude/state/prunable-worktrees.txt").exists())

    def test_spaced_live_worktree_retains_old_transcript_directory(self) -> None:
        parent, wt = self.create_worktree("repositories")
        self.legacy_hook()
        # Legacy enumeration already finds this root, exposing the independent path split.
        (self.kit / "local/setup-paths.env").write_text(
            "CODE_DIRS=" + str(parent) + "\n"
        )
        encoded = str(wt).replace("/", "-").replace(".", "-")
        transcript = self.home / ".claude/projects" / encoded
        transcript.mkdir(parents=True)
        payload = transcript / "fixture-transcript.txt"
        payload.write_text("synthetic transcript preserved\n")
        for path in (payload, transcript):
            os.utime(path, (946684800, 946684800))
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            payload.is_file(), "live spaced worktree transcript was deleted"
        )
        self.assertEqual(payload.read_text(), "synthetic transcript preserved\n")

    def old_transcript(self, name: str) -> Path:
        directory = self.home / ".claude/projects" / name
        directory.mkdir(parents=True)
        payload = directory / "fixture-transcript.txt"
        payload.write_text("synthetic transcript preserved\n")
        for path in (payload, directory):
            os.utime(path, (946684800, 946684800))
        return payload

    def test_conservative_key_preserves_spaced_live_transcript(self) -> None:
        parent, wt = self.create_worktree("repositories")
        self.configure([parent], str(parent))
        self.legacy_hook()
        encoded = re.sub(r"[^A-Za-z0-9]", "-", str(wt))
        payload = self.old_transcript(encoded)
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload.is_file())
        self.assertTrue(wt.exists())

    def assert_incomplete_discovery_preserves(self, unavailable: Path) -> None:
        parent, _ = self.create_worktree("repositories")
        self.configure([parent, unavailable], str(parent))
        self.legacy_hook()
        payload = self.old_transcript("unseen-worktrees-state")
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("transcript discovery incomplete", result.stdout + result.stderr)
        self.assertTrue(payload.is_file())

    def test_missing_root_preserves_transcripts(self) -> None:
        self.assert_incomplete_discovery_preserves(self.root / "missing root")

    def test_unreadable_root_preserves_transcripts(self) -> None:
        unavailable = self.root / "unreadable root"
        unavailable.mkdir()
        unavailable.chmod(0)
        try:
            self.assertFalse(os.access(unavailable, os.R_OK | os.X_OK))
            self.assert_incomplete_discovery_preserves(unavailable)
        finally:
            unavailable.chmod(0o700)

    def test_failed_worktree_listing_preserves_transcripts(self) -> None:
        parent, wt = self.create_worktree("repositories")
        other, _ = self.create_worktree("other-repositories")
        self.configure([parent, other], str(parent) + " " + str(other))
        self.legacy_hook()
        payload = self.old_transcript("unseen-worktrees-state")
        self.env["GC_FAIL_REPO"] = str(wt.parents[2])
        startup = Path(self.env["BASH_ENV"])
        with startup.open("a") as stream:
            stream.write(
                'git() { if [ "$1" = -C ] && [ "$2" = "$GC_FAIL_REPO" ] && [ "${3:-}" = worktree ] && [ "${4:-}" = list ]; then return 91; fi; command git "$@"; }\n'
            )
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("worktree listing failed", result.stdout + result.stderr)
        self.assertTrue(payload.is_file())

    def test_ambiguous_encoding_preserves_transcripts(self) -> None:
        parent, _ = self.create_worktree("unicode-\u00e9")
        self.configure([parent], str(parent))
        self.legacy_hook()
        payload = self.old_transcript("unseen-worktrees-state")
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ambiguous worktree path encoding", result.stdout + result.stderr)
        self.assertTrue(payload.is_file())

    def test_complete_discovery_keeps_existing_housekeeping_behavior(self) -> None:
        parent, wt = self.create_worktree()
        self.configure([parent])
        orphan = self.old_transcript("unseen-worktrees-state")
        history = self.home / ".claude/file-history"
        history.mkdir(parents=True)
        old = history / "old-fixture.txt"
        old.write_text("synthetic expired file history\n")
        os.utime(old, (946684800, 946684800))
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(orphan.exists())
        self.assertFalse(old.exists())
        self.assertTrue(wt.exists())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(GCPortabilityTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(
        json.dumps(
            {
                "cases": suite.countTestCases(),
                "successful": result.wasSuccessful(),
                "failures": len(result.failures),
                "errors": len(result.errors),
            }
        )
        + "\n"
    )
    raise SystemExit(not result.wasSuccessful())
