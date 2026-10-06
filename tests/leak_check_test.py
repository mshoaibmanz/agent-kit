"""The org denylist matcher (scripts/leak-check.sh) and the pre-push hook, in throwaway repos."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
FIXTURES = Path(os.environ.get("TMPDIR", str(SOURCE / ".test-fixtures"))) / "leak-check"
GITLEAKS = os.environ.get("GITLEAKS") or shutil.which("gitleaks")
# The hooks target macOS bash 3.2; CI runners put a newer bash first on PATH.
BASH = "/bin/bash"
KIT_FILES = (
    "scripts/leak-check.sh",
    "scripts/scan.sh",
    "scripts/pre-push",
    ".githooks/pre-push",
    ".gitleaks.toml",
)
# Line 2 is a short plain term (word start), line 3 a long one (substring), line 4 an ERE.
TERMS = "# fixture denylist\nhooli\nglobex-corp\n(^|[^a-z])initech\n"
IDENTITY = "kit-test@users.noreply.github.com"


class LeakCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="leak-check-", dir=FIXTURES)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.terms = self.root / "outside the repo" / "leak-terms.txt"
        self.terms.parent.mkdir()
        self.terms.write_text(TERMS)
        self.env = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "TMPDIR": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_COUNT": "0",
            "GIT_AUTHOR_NAME": "Kit Test",
            "GIT_AUTHOR_EMAIL": IDENTITY,
            "GIT_COMMITTER_NAME": "Kit Test",
            "GIT_COMMITTER_EMAIL": IDENTITY,
            "AGENT_KIT_LEAK_TERMS": str(self.terms),
        }
        if GITLEAKS:
            self.env["GITLEAKS"] = GITLEAKS
        self.repo = self.root / "clone"
        for relative in KIT_FILES:
            (self.repo / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / relative, self.repo / relative)
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")
        self.commit("kit scripts", {"README.md": "A neutral readme.\n"})

    def git(self, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            env=env or self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=120,
        )

    def commit(
        self, message: str, files: dict[str, str | bytes | None], env: dict[str, str] | None = None
    ) -> str:
        """Write each file (None deletes it), commit everything, return the new sha."""
        for name, content in files.items():
            path = self.repo / name
            if content is None:
                path.unlink()
            elif isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-qm", message, env=env)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def leak_check(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(self.repo / "scripts/leak-check.sh"), *args],
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def hits(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        return sorted(line for line in result.stdout.splitlines() if line.startswith("term #"))

    def bare_remote(self, name: str) -> Path:
        remote = self.root / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], env=self.env, check=True)
        self.git("remote", "add", name, str(remote))
        return remote

    def remote_sha(self, remote: Path, ref: str) -> str:
        return subprocess.run(
            ["git", "-C", str(remote), "rev-parse", ref], env=self.env, capture_output=True, text=True, check=True
        ).stdout.strip()

    def test_short_terms_match_at_a_word_start_long_terms_and_eres_as_written(self) -> None:
        tree = self.root / "tree"
        tree.mkdir()
        files = {
            "inside-a-word.txt": "the xhooli, my_hooli and xinitech stay quiet\n",
            "joined.txt": "call hooliApi now\n",
            "short.txt": "see hooli.example for details\n",
            "long.txt": "a xglobex-corpy substring\n",
            "ere.txt": "call initech today\n",
        }
        for name, text in files.items():
            (tree / name).write_text(text)
        paths = self.root / "paths.txt"
        paths.write_text("".join(f"{name}\n" for name in files))
        result = self.leak_check("--tree", str(tree), "--paths", str(paths))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(
            self.hits(result),
            ["term #2 in file #2", "term #2 in file #3", "term #3 in file #4", "term #4 in file #5"],
            result.stdout,
        )

    def test_a_hit_never_prints_the_term(self) -> None:
        sha = self.commit("bump the hooli client", {"notes.txt": "globex-corp\n"})
        tree = self.root / "tree"
        (tree / "hooli-src").mkdir(parents=True)
        (tree / "hooli-src/x.txt").write_text("hooli\n")
        (tree / "README.md").write_text("neutral\n")
        paths = self.root / "paths.txt"
        paths.write_text("README.md\nhooli-src/x.txt\n")
        result = self.leak_check("--tree", str(tree), "--paths", str(paths), "--", "--no-walk", sha)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(
            self.hits(result),
            [
                f"term #2 in commit {sha[:12]}",
                "term #2 in file #2",
                "term #2 in file name #2",
                f"term #3 in commit {sha[:12]}",
            ],
            result.stdout,
        )
        for word in ("hooli", "globex", "initech"):
            self.assertNotIn(word, (result.stdout + result.stderr).lower())
        self.assertEqual(self.leak_check("--tree", str(tree)).returncode, 2)

    def test_an_invalid_ere_stops_the_check_and_names_its_line(self) -> None:
        self.terms.write_text("hooli\n# a comment\nfoo(bar\n")
        result = self.leak_check("--", "--all")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("line 3 of the denylist is not a valid ERE", result.stderr)
        self.assertNotIn("foo", result.stdout + result.stderr)

    def test_commit_identities_and_message_bullets_are_checked(self) -> None:
        env = dict(self.env, GIT_AUTHOR_EMAIL="hooli-dev@example.com")
        identity = self.commit("a neutral message", {"a.txt": "neutral\n"}, env=env)
        bullet = self.commit("tidy\n\n- the globex-corp part", {"a.txt": "neutral, edited\n"})
        result = self.leak_check("--", identity, "--not", f"{identity}~1")
        self.assertEqual(self.hits(result), [f"term #2 in commit {identity[:12]}"], result.stdout + result.stderr)
        result = self.leak_check("--", "--no-walk", bullet)
        self.assertEqual(self.hits(result), [f"term #3 in commit {bullet[:12]}"], result.stdout + result.stderr)

    def test_a_term_early_in_a_large_commit_is_found(self) -> None:
        # A `git show | grep -q` pipeline under pipefail reports SIGPIPE, not the hit, once the
        # diff outgrows the pipe buffer.
        sha = self.commit("a large file", {"big.txt": "globex-corp\n" + "filler line\n" * 50000})
        result = self.leak_check("--", "--no-walk", sha)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"term #3 in commit {sha[:12]}", result.stdout)

    def test_deleted_lines_do_not_count_and_binary_content_does(self) -> None:
        added = self.commit("add a note", {"note.txt": "keep\nglobex-corp\n"})
        removed = self.commit("drop a line", {"note.txt": "keep\n"})
        binary = self.commit("add a blob", {"blob.bin": b"\x00\x01globex-corp\x00\xff"})
        result = self.leak_check("--", f"{added}~1..{binary}")
        self.assertEqual(
            self.hits(result),
            sorted([f"term #3 in commit {added[:12]}", f"term #3 in commit {binary[:12]}"]),
            result.stdout + result.stderr,
        )
        self.assertEqual(self.leak_check("--", "--no-walk", removed).returncode, 0)

    def test_annotated_tags_are_checked(self) -> None:
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("tag", "-a", "v1", "-m", "release for hooli")
        self.git("tag", "light")
        tag = self.git("rev-parse", "v1").stdout.strip()
        named = self.leak_check("--", "v1", "--not", head)
        self.assertEqual(self.hits(named), [f"term #2 in tag {tag[:12]}"], named.stdout + named.stderr)
        everything = self.leak_check("--", "--all")
        self.assertEqual(self.hits(everything), [f"term #2 in tag {tag[:12]}"], everything.stdout)
        self.assertIn("1 tag(s)", everything.stdout)

    def test_the_allowlist_covers_only_public_history(self) -> None:
        self.bare_remote("origin")
        public = self.commit("published", {"a.txt": "globex-corp\n"})
        self.git("push", "-q", "origin", "main")
        unpushed = self.commit("not yet published", {"b.txt": "globex-corp\n"})
        allow = self.root / "allow.txt"
        allow.write_text(f"# accepted\n{public}\n{unpushed}\n")
        result = self.leak_check("--allow", str(allow), "--", "--all")
        self.assertEqual(self.hits(result), [f"term #3 in commit {unpushed[:12]}"], result.stdout + result.stderr)
        self.assertIn("1 public commit(s) skipped", result.stdout)

    def test_list_sources_in_order(self) -> None:
        sha = self.commit("mention initech", {"a.txt": "neutral\n"})
        commits = ("--", "--no-walk", sha)
        quiet = "nothing-here\n"
        kit = self.root / "kit"
        (kit / "local").mkdir(parents=True)
        (kit / "local/leak-terms.txt").write_text(quiet)
        fallback = self.home / ".local/share/agent-kit/local/leak-terms.txt"
        fallback.parent.mkdir(parents=True)
        fallback.write_text(TERMS)
        base = {k: v for k, v in self.env.items() if k != "AGENT_KIT_LEAK_TERMS"}
        cases = [
            ("LEAK_TERMS over AGENT_KIT_LEAK_TERMS", dict(self.env, LEAK_TERMS=quiet), 0),
            ("AGENT_KIT_LEAK_TERMS over AGENT_KIT_DIR", dict(self.env, AGENT_KIT_DIR=str(kit)), 1),
            ("AGENT_KIT_DIR over the HOME default", dict(base, AGENT_KIT_DIR=str(kit)), 0),
            ("the HOME default", base, 1),
        ]
        for label, env, code in cases:
            with self.subTest(label):
                result = self.leak_check(*commits, env=env)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        fallback.unlink()
        # LEAK_TERMS_FILE was the older name; it is not read.
        missing = dict(base, LEAK_TERMS_FILE=str(self.terms))
        result = self.leak_check(*commits, env=missing)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("WARNING", result.stderr)

    def test_pre_push_reports_a_scan_that_could_not_run(self) -> None:
        self.bare_remote("origin")
        self.git("config", "core.hooksPath", ".githooks")
        env = dict(self.env, GITLEAKS=str(self.root / "no-gitleaks"))
        result = self.git("push", "-q", "origin", "main", check=False, env=env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("pre-push: the scan could not run (exit 2", result.stderr)
        self.assertNotIn("found something", result.stderr)

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_scan_fails_without_a_list_when_required(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "AGENT_KIT_LEAK_TERMS"}
        result = subprocess.run(
            [BASH, str(self.repo / "scripts/scan.sh")],
            env=dict(env, REQUIRE_LEAK_TERMS="1"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FINDING no denylist", result.stdout)

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_pre_push_refuses_a_term_in_a_message_and_passes_a_clean_push(self) -> None:
        remote = self.bare_remote("origin")
        self.git("config", "core.hooksPath", ".githooks")
        clean = self.git("push", "-q", "origin", "main", check=False)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        published = self.git("rev-parse", "HEAD").stdout.strip()
        # The tree stays clean: only the message carries the term.
        sha = self.commit("tidy the readme for the hooli team", {"README.md": "A neutral readme, edited.\n"})
        refused = self.git("push", "-q", "origin", "main", check=False)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn(f"term #2 in commit {sha[:12]}", refused.stdout + refused.stderr)
        self.assertNotIn("in file", refused.stdout + refused.stderr)
        self.assertIn("pre-push: the leak scan found something", refused.stderr)
        self.assertEqual(self.remote_sha(remote, "main"), published)
        self.git("commit", "--amend", "-qm", "tidy the readme")
        without_list = dict(self.env, AGENT_KIT_LEAK_TERMS=str(self.root / "missing.txt"))
        passed = self.git("push", "-q", "origin", "main", check=False, env=without_list)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn("WARNING: no denylist", passed.stdout + passed.stderr)
        self.assertEqual(self.remote_sha(remote, "main"), self.git("rev-parse", "HEAD").stdout.strip())

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_pre_push_checks_commits_another_remote_already_has(self) -> None:
        self.bare_remote("origin")
        self.bare_remote("fork")
        self.git("config", "core.hooksPath", ".githooks")
        self.assertEqual(self.git("push", "-q", "origin", "main", check=False).returncode, 0)
        sha = self.commit("wire the hooli client", {"a.txt": "neutral\n"})
        self.git("push", "-q", "--no-verify", "fork", "main")
        for ref in ("main", "main:feature"):
            with self.subTest(ref):
                refused = self.git("push", "-q", "origin", ref, check=False)
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn(f"term #2 in commit {sha[:12]}", refused.stdout + refused.stderr)

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_pre_push_checks_a_pushed_annotated_tag(self) -> None:
        self.bare_remote("origin")
        self.git("config", "core.hooksPath", ".githooks")
        self.assertEqual(self.git("push", "-q", "origin", "main", check=False).returncode, 0)
        self.git("tag", "-a", "v1", "-m", "release for hooli")
        refused = self.git("push", "-q", "origin", "v1", check=False)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("term #2 in tag", refused.stdout + refused.stderr)

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_the_old_symlink_install_still_refuses(self) -> None:
        self.bare_remote("origin")
        hook = self.repo / ".git/hooks/pre-push"
        hook.parent.mkdir(exist_ok=True)
        hook.symlink_to("../../scripts/pre-push")
        self.commit("wire the hooli client", {"a.txt": "neutral\n"})
        refused = self.git("push", "-q", "origin", "main", check=False)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("pre-push: the leak scan found something", refused.stderr)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LeakCheckTests)
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
