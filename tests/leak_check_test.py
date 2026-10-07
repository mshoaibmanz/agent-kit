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

    def test_removed_bullets_and_unchanged_neighbours_do_not_count(self) -> None:
        self.commit("a list", {"list.md": "- keep\n- globex-corp note\n", "near.txt": "globex-corp line\nkeep\n"})
        # A removed `- term` bullet shows as `-- term`; the edit below leaves the term on the line
        # above, which a full diff shows as context and a -U0 hunk header still repeats.
        dropped = self.commit("drop a bullet", {"list.md": "- keep\n"})
        edited = self.commit("edit beside it", {"near.txt": "globex-corp line\nkept\n"})
        for sha in (dropped, edited):
            with self.subTest(sha):
                result = self.leak_check("--", "--no-walk", sha)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_anchored_ere_sees_an_added_line_as_written(self) -> None:
        self.terms.write_text("^vandelay$\n")
        added = self.commit("add a line", {"a.txt": "vandelay\n"})
        inside = self.commit("add a longer line", {"b.txt": "xvandelay\n"})
        self.git("checkout", "-q", "-b", "side")
        self.commit("side", {"side.txt": "neutral\n"})
        self.git("checkout", "-q", "main")
        self.commit("main", {"main.txt": "neutral\n"})
        self.git("merge", "-q", "--no-ff", "--no-commit", "side")
        # The line is only in the merge commit: its combined diff shows it as `++vandelay`.
        merge = self.commit("merge side", {"evil.txt": "vandelay\n"})
        for sha, expected in ((added, 1), (inside, 0), (merge, 1)):
            with self.subTest(sha):
                result = self.leak_check("--", "--no-walk", sha)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_deleting_or_renaming_a_file_named_with_a_term_passes(self) -> None:
        self.commit("add notes", {"hooli-notes.md": "plain\n", "old-globex-corp.md": "plain two\n"})
        deleted = self.commit("drop the notes", {"hooli-notes.md": None})
        self.git("mv", "old-globex-corp.md", "renamed.md")
        self.git("commit", "-qm", "rename it")
        renamed = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("mv", "renamed.md", "initech-again.md")
        self.git("commit", "-qm", "rename it back")
        renamed_to = self.git("rev-parse", "HEAD").stdout.strip()
        # An empty new file has no +++ line: its name is only in the diff --git line.
        empty = self.commit("an empty file", {"hooli-empty.txt": ""})
        for sha, expected in ((deleted, 0), (renamed, 0), (renamed_to, 1), (empty, 1)):
            with self.subTest(sha):
                result = self.leak_check("--", "--no-walk", sha)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

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

    def test_a_tag_of_a_tag_is_checked_after_the_inner_ref_is_gone(self) -> None:
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("tag", "-a", "inner", "-m", "staging for initech")
        inner = self.git("rev-parse", "inner").stdout.strip()
        self.git("tag", "-a", "outer", "-m", "a neutral release", "inner")
        self.git("tag", "-d", "inner")
        for args in (("outer", "--not", head), ("--all",)):
            with self.subTest(args):
                result = self.leak_check("--", *args)
                self.assertEqual(self.hits(result), [f"term #4 in tag {inner[:12]}"], result.stdout + result.stderr)
                self.assertIn("2 tag(s)", result.stdout)

    def test_history_exceptions_come_from_the_base_and_its_history(self) -> None:
        published = self.commit("published", {"a.txt": "globex-corp\n"})
        base = self.commit("accept it", {"a.txt": None, ".scan-history-allow": f"# accepted\n{published}\n"})
        leak = self.commit("a new leak", {"b.txt": "globex-corp\n"})
        # The change under check lists its own leak in the checkout's copy.
        self.commit("cover it", {"b.txt": None, ".scan-history-allow": f"{published}\n{leak}\n"})
        both = sorted([f"term #3 in commit {published[:12]}", f"term #3 in commit {leak[:12]}"])
        cases = [
            ("the base's list", base, [f"term #3 in commit {leak[:12]}"]),
            # A base from before the file: the checkout's copy, still only for the base's history.
            ("a base without the file", published, [f"term #3 in commit {leak[:12]}"]),
            ("no base", "", both),
            ("an unknown base", "no-such-rev", both),
        ]
        for label, rev, expected in cases:
            with self.subTest(label):
                result = self.leak_check("--allow", rev, "--", "--all")
                self.assertEqual(self.hits(result), expected, result.stdout + result.stderr)

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
        old_default = self.home / ".config/claude-kit/leak-terms.txt"
        old_default.parent.mkdir(parents=True)
        old_default.write_text(quiet)
        base = {k: v for k, v in self.env.items() if k != "AGENT_KIT_LEAK_TERMS"}
        cases = [
            ("LEAK_TERMS over AGENT_KIT_LEAK_TERMS", dict(self.env, LEAK_TERMS=quiet), 0),
            ("AGENT_KIT_LEAK_TERMS over AGENT_KIT_DIR", dict(self.env, AGENT_KIT_DIR=str(kit)), 1),
            ("AGENT_KIT_DIR over the HOME default", dict(base, AGENT_KIT_DIR=str(kit)), 0),
            ("the HOME default over the old default", base, 1),
        ]
        for label, env, code in cases:
            with self.subTest(label):
                result = self.leak_check(*commits, env=env)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        fallback.unlink()
        old_default.write_text(TERMS)
        with self.subTest("the old default, last"):
            result = self.leak_check(*commits, env=base)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for label, env in (("off", dict(self.env, AGENT_KIT_LEAK_TERMS="none")), ("no list", base)):
            with self.subTest(label):
                old_default.unlink(missing_ok=True)
                result = self.leak_check(*commits, env=env)
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

    def scan(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(self.repo / "scripts/scan.sh"), *args],
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_usage_errors_exit_2_not_1(self) -> None:
        # 1 means a finding; a script that cannot run must not look like one.
        for args in (("--report",), ("--allow-base",), ("--bogus",)):
            with self.subTest(args):
                self.assertEqual(self.scan(*args).returncode, 2)
        for args in (("--paths",), ("--allow",), ("--tree", "x")):
            with self.subTest(args):
                self.assertEqual(self.leak_check(*args).returncode, 2)

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_scan_fails_without_a_list_when_required(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "AGENT_KIT_LEAK_TERMS"}
        result = self.scan(env=dict(env, REQUIRE_LEAK_TERMS="1"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FINDING no denylist", result.stdout)

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_a_change_that_lists_its_own_leak_is_still_refused(self) -> None:
        # CI's view of a push to main: the previous main is the base, the checkout is the push.
        published = self.commit("published", {"a.txt": "globex-corp\n"})
        base = self.commit("accept it", {"a.txt": None, ".scan-history-allow": f"{published}\n"})
        leak = self.commit("a new leak", {"b.txt": "globex-corp\n"})
        self.commit("cover it", {"b.txt": None, ".scan-history-allow": f"{published}\n{leak}\n"})
        result = self.scan("--allow-base", base)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"term #3 in commit {leak[:12]}", result.stdout)
        self.assertNotIn(f"commit {published[:12]}", result.stdout)
        self.assertIn("1 public commit(s) skipped", result.stdout)

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
        refused = self.git("push", "-q", "origin", "main", check=False, env=without_list)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("FINDING no denylist", refused.stdout + refused.stderr)
        self.assertEqual(self.remote_sha(remote, "main"), published)
        opted_out = dict(self.env, AGENT_KIT_LEAK_TERMS="none")
        passed = self.git("push", "-q", "origin", "main", check=False, env=opted_out)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn("WARNING: the denylist check is off", passed.stdout + passed.stderr)
        self.assertEqual(self.remote_sha(remote, "main"), self.git("rev-parse", "HEAD").stdout.strip())

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_pre_push_checks_only_what_the_push_adds(self) -> None:
        self.bare_remote("origin")
        self.commit("published before the hook", {"a.txt": "globex-corp\n"})
        self.git("push", "-q", "--no-verify", "origin", "main")
        self.git("config", "core.hooksPath", ".githooks")
        self.commit("a clean change", {"b.txt": "neutral\n"})
        (self.repo / "scratch.txt").write_text("hooli\n")
        passed = self.git("push", "-q", "origin", "main", check=False)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn("1 commit(s)", passed.stdout + passed.stderr)
        self.assertNotIn("rule self-test", passed.stdout + passed.stderr)

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_pre_push_trusts_tracking_refs_only_from_the_push_url(self) -> None:
        self.bare_remote("origin")
        self.git("config", "core.hooksPath", ".githooks")
        sha = self.commit("wire the hooli client", {"a.txt": "neutral\n"})
        self.git("push", "-q", "--no-verify", "origin", "main")
        # Same URL: origin/main records what the destination has, so a new ref adds nothing.
        same = self.git("push", "-q", "origin", "main:feature", check=False)
        self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
        public = self.root / "public.git"
        subprocess.run(["git", "init", "-q", "--bare", str(public)], env=self.env, check=True)
        self.git("remote", "set-url", "--push", "origin", str(public))
        refused = self.git("push", "-q", "origin", "main", check=False)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn(f"term #2 in commit {sha[:12]}", refused.stdout + refused.stderr)

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_pre_push_trusts_a_tracking_ref_only_while_the_destination_advertises_it(self) -> None:
        self.bare_remote("origin")
        self.git("config", "core.hooksPath", ".githooks")
        sha = self.commit("wire the hooli client", {"a.txt": "neutral\n"})
        self.git("push", "-q", "--no-verify", "origin", "main")
        # origin/main came from the private URL; after set-url it says nothing about the public one.
        public = self.root / "public.git"
        subprocess.run(["git", "init", "-q", "--bare", str(public)], env=self.env, check=True)
        self.git("remote", "set-url", "origin", str(public))
        refused = self.git("push", "-q", "origin", "main", check=False)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn(f"term #2 in commit {sha[:12]}", refused.stdout + refused.stderr)
        self.assertEqual(self.git("ls-remote", str(public)).stdout, "")
        # An ls-remote that fails trusts no tracking ref.
        missing = str(self.root / "missing.git")
        self.git("remote", "set-url", "origin", missing)
        direct = subprocess.run(
            [BASH, str(self.repo / ".githooks/pre-push"), "origin", missing],
            input=f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n",
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(direct.returncode, 1, direct.stdout + direct.stderr)
        self.assertIn(f"term #2 in commit {sha[:12]}", direct.stdout + direct.stderr)
        # Once the destination has the commit, the fetched ref counts again.
        self.git("remote", "set-url", "origin", str(public))
        self.git("push", "-q", "--no-verify", "origin", "main")
        self.git("fetch", "-q", "origin")
        passed = self.git("push", "-q", "origin", "main:feature", check=False)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)

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
        self.git("tag", "-a", "inner", "-m", "staging for initech")
        inner = self.git("rev-parse", "inner").stdout.strip()
        self.git("tag", "-a", "outer", "-m", "a neutral release", "inner")
        self.git("tag", "-d", "inner")
        refused = self.git("push", "-q", "origin", "outer", check=False)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn(f"term #4 in tag {inner[:12]}", refused.stdout + refused.stderr)

    def old_install(self) -> None:
        hook = self.repo / ".git/hooks/pre-push"
        hook.parent.mkdir(exist_ok=True)
        hook.symlink_to("../../scripts/pre-push")

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_the_old_symlink_install_keeps_its_list(self) -> None:
        self.bare_remote("origin")
        self.old_install()
        sha = self.commit("wire the hooli client", {"a.txt": "neutral\n"})
        old_env = {k: v for k, v in self.env.items() if k != "AGENT_KIT_LEAK_TERMS"}
        old_default = self.home / ".config/claude-kit/leak-terms.txt"
        cases = [
            ("LEAK_TERMS_FILE", dict(old_env, LEAK_TERMS_FILE=str(self.terms))),
            ("the old default list", old_env),
        ]
        for label, env in cases:
            with self.subTest(label):
                if label == "the old default list":
                    old_default.parent.mkdir(parents=True)
                    old_default.write_text(TERMS)
                refused = self.git("push", "-q", "origin", "main", check=False, env=env)
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn(f"term #2 in commit {sha[:12]}", refused.stdout + refused.stderr)
                self.assertIn("pre-push: the leak scan found something", refused.stderr)

    def test_the_old_symlink_runs_the_pushing_worktrees_own_hook(self) -> None:
        self.bare_remote("origin")
        self.old_install()
        worktree = self.root / "older branch"
        self.git("worktree", "add", "-q", "-b", "older", str(worktree))

        def in_worktree(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(worktree), *args], env=self.env, capture_output=True, text=True, timeout=120
            )

        # A branch from before the hook moved: no .githooks, and no hook of its own either.
        in_worktree("rm", "-q", ".githooks/pre-push", "scripts/pre-push")
        in_worktree("commit", "-qm", "a branch from before the hook")
        passed = in_worktree("push", "-q", "origin", "older")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        own = worktree / "scripts/pre-push"
        own.write_text("#!/bin/bash\necho 'its own pre-push ran' >&2\nexit 1\n")
        in_worktree("add", "scripts/pre-push")
        in_worktree("commit", "-qm", "its own hook")
        refused = in_worktree("push", "-q", "origin", "older")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("its own pre-push ran", refused.stderr)


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
