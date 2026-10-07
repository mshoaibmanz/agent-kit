"""Exercise installer review repairs with real synthetic host installations."""

from pathlib import Path
import hashlib
import json
import os
import pty
import select
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import unittest
import setup_test as support

SOURCE = Path(os.environ.get("AGENT_SETUP_TEST_SOURCE", support.SOURCE))
support.SOURCE = SOURCE


class SetupReviewTests(unittest.TestCase):
    setUp = support.SetupTests.setUp
    tearDown = support.SetupTests.tearDown
    run_setup = support.SetupTests.run_setup
    state = support.SetupTests.state

    def fresh_source(self) -> Path:
        source = self.home / "source fixture"
        source.mkdir()
        for name in (
            "bin",
            "hooks",
            "git-hooks",
            "agents",
            "commands",
            "rules",
            "hosts",
            "skills",
            "references",
        ):
            shutil.copytree(SOURCE / name, source / name, symlinks=True)
        for name in ("roles.toml", "kit.env.example"):
            shutil.copy2(SOURCE / name, source / name)
        return source

    def test_directory_collision_preserves_skill_and_bin(self) -> None:
        # A user's own skill folder is kept and that skill skipped (spec 2026-10-05: never overwrite
        # a non-link skill); a bin collision still refuses the whole install.
        skill = self.host / "skills/unslop"
        skill.mkdir(parents=True)
        (skill / "user.md").write_text("original directory\n")
        result = self.run_setup("--components", "skills", "--skills", "unslop", "--apply")
        self.assertEqual((skill / "user.md").read_text(), "original directory\n")
        self.assertFalse(skill.is_symlink())
        self.assertIn("not a kit link", result.stderr)
        shutil.rmtree(self.root)
        target = self.host / "bin"
        target.mkdir(parents=True)
        sentinel = target / "user.md"
        sentinel.write_text("original directory\n")
        self.run_setup("--components", "hooks", "--apply", success=False)
        self.assertEqual(sentinel.read_text(), "original directory\n")
        self.assertFalse(self.root.exists())

    def test_killed_apply_recovers_original_and_blocks_rerun(self) -> None:
        self.root.mkdir()
        original = b"user-owned roles file\n"
        roles = self.root / "roles.toml"
        roles.write_bytes(original)
        code = """
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys

loader = importlib.machinery.SourceFileLoader('setup', sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules['setup'] = module
loader.exec_module(module)
trigger = Path(sys.argv[2]) / 'roles.toml'
sys.argv = [sys.argv[1], '--source', sys.argv[3], '--root-dir', sys.argv[2],
            '--host-root', sys.argv[4], '--components', 'rules',
            '--collision', 'backup', '--apply']

def stop(frame, event, arg):
    # Interrupt the real write before its completion is recorded in the journal.
    if (event == 'return' and frame.f_code is module.atomic.__code__
            and frame.f_locals['path'] == trigger
            and trigger.read_bytes() != b'user-owned roles file\\n'):
        os._exit(77)

sys.setprofile(stop)
module.main()
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(SOURCE / "bin/agent-setup"),
                str(self.root),
                str(SOURCE),
                str(self.host),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 77, result.stderr)
        pending = next(
            (self.root / ".install-state").glob("*/journal.json")
        ).parent.name
        self.run_setup("--components", "rules", "--apply", success=False)
        self.run_setup("rollback", pending)
        self.assertEqual(roles.read_bytes(), original)
        self.assertFalse((self.root / "bin/agent-setup").exists())
        self.run_setup("--components", "rules", "--collision", "backup", "--apply")

    def test_host_root_inheritance_is_scoped_and_native_home_is_honored(self) -> None:
        self.run_setup("--hosts", "claude", "--components", "rules", "--apply")
        base = [
            sys.executable,
            str(SOURCE / "bin/agent-setup"),
            "--source",
            str(SOURCE),
            "--root-dir",
            str(self.root),
            "--components",
            "rules",
            "--json",
        ]
        custom = self.home / "native codex home"
        result = subprocess.run(
            [*base, "--hosts", "codex"],
            env=dict(self.env, CODEX_HOME=str(custom)),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = [row["path"] for row in json.loads(result.stdout)["paths"]]
        self.assertIn(str(custom / "AGENTS.md"), paths)
        self.assertNotIn(str(self.host / "AGENTS.md"), paths)
        result = subprocess.run(
            [*base, "--hosts", "claude", "codex"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any(
                row["path"] == str(self.host / "CLAUDE.md")
                for row in json.loads(result.stdout)["paths"]
            )
            or (self.host / "CLAUDE.md").exists()
        )

    def test_cursor_frontmatter_starts_file(self) -> None:
        project = self.home / "project"
        project.mkdir()
        self.run_setup(
            "--hosts",
            "cursor",
            "--components",
            "rules",
            "--project-root",
            str(project),
            "--apply",
        )
        rule = project / ".cursor/rules/agent-kit.mdc"
        text = rule.read_text()
        self.assertTrue(text.startswith("---\n"), text[:100])
        self.assertIn("alwaysApply: true", text.split("---", 2)[1])

    def test_secret_source_names_excluded_and_private_modes_preserved(self) -> None:
        source = self.fresh_source()
        secret = source / "hosts/claude/settings.local.json"
        secret.parent.mkdir(exist_ok=True)
        secret.write_text('{"fixture":"PRIVATE_SOURCE_CANARY"}')
        secret.chmod(0o600)
        private = source / "bin/lib/fixture-private.json"
        private.write_text('{"fixture":"private mode"}')
        private.chmod(0o600)
        self.run_setup("--source", str(source), "--components", "rules", "--apply")
        self.assertFalse((self.root / "hosts/claude/settings.local.json").exists())
        self.assertEqual(
            (self.root / "bin/lib/fixture-private.json").stat().st_mode & 0o777, 0o600
        )

    def test_skill_narrowing_retires_and_rollback_restores(self) -> None:
        self.run_setup("--components", "skills", "--skills", "unslop", "--apply")
        self.run_setup("--components", "skills", "--skills", "code-search", "--apply")
        self.assertFalse((self.host / "skills/unslop").exists())
        self.assertFalse((self.root / "skills/unslop/SKILL.md").exists())
        self.assertTrue((self.root / "skills/code-search/SKILL.md").exists())
        self.run_setup("rollback", self.state()["id"])
        self.assertTrue((self.host / "skills/unslop").is_symlink())
        self.assertTrue((self.root / "skills/unslop/SKILL.md").exists())

    def test_skill_narrowing_preserves_user_edit(self) -> None:
        self.run_setup("--components", "skills", "--skills", "unslop", "--apply")
        doc = self.root / "skills/unslop/SKILL.md"
        doc.write_text(doc.read_text() + "\nuser edit\n")
        self.run_setup(
            "--components",
            "skills",
            "--skills",
            "code-search",
            "--apply",
            success=False,
        )
        self.assertTrue((self.host / "skills/unslop").exists())
        self.assertIn("user edit", doc.read_text())

    def test_advisory_registry_cannot_reenable_blocking(self) -> None:
        self.run_setup("--components", "hooks", "--apply")
        rows = json.loads((self.root / "hooks/registry.json").read_text())
        self.assertNotIn(
            "bash-guards", {Path(shlex.split(row["command"])[0]).name for row in rows}
        )
        before = (self.root / "hooks/registry.json").read_bytes()
        self.run_setup("--components", "rules", "--apply")
        self.assertEqual((self.root / "hooks/registry.json").read_bytes(), before)

    def test_overrides_validated_on_rules_and_empty_resets(self) -> None:
        self.run_setup(
            "--components",
            "rules",
            "--role-effort",
            "task-reviewer=invalid",
            "--apply",
            success=False,
        )
        self.assertFalse(self.root.exists())
        self.run_setup(
            "--components",
            "rules",
            "--role-model",
            "cross-reviewer=openai:chosen-model",
            "--apply",
        )
        self.run_setup(
            "--components", "roles", "--role-model", "cross-reviewer=", "--apply"
        )
        self.assertNotEqual(
            tomllib.loads((self.root / "roles.toml").read_text())["roles"][
                "cross-reviewer"
            ]["model"],
            "openai:chosen-model",
        )

    def test_disabled_hook_feature_refused(self) -> None:
        (self.commands / "codex").write_text(
            '#!/bin/sh\nprintf "hooks experimental false\\n"\n'
        )
        self.run_setup(
            "--hosts", "codex", "--components", "hooks", "--apply", success=False
        )
        self.assertFalse(self.root.exists())

    def test_role_cache_stable_and_cross_host_upgrade_uses_new_model(self) -> None:
        self.run_setup("--components", "roles", "--apply")
        text = (self.root / "state/rendered/roles-codex.sh").read_text()
        self.assertNotIn("agent-kit-install-view-", text)
        self.run_setup(
            "--hosts",
            "codex",
            "--components",
            "roles",
            "--role-model",
            "cross-reviewer=openai:chosen-model",
            "--apply",
        )
        script = '. "$1/hooks/lib/review-state"; rv_roles; printf "%s" "$RV_ROLE_TABLE"'
        result = subprocess.run(
            ["bash", "-c", script, "probe", str(self.root)],
            env=dict(self.env, AGENT_HOST="codex"),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("cross-reviewer openai chosen-model", result.stdout)
        repo = self.home / "role run repository"
        repo.mkdir()
        for args in [
            ("init", "-b", "main"),
            ("config", "user.name", "Fixture"),
            ("config", "user.email", "fixture@example.com"),
        ]:
            subprocess.run(
                ["git", "-C", str(repo), *args],
                env=self.env,
                capture_output=True,
                check=True,
            )
        (repo / "code.py").write_text("value=1\n")
        for args in [("add", "."), ("commit", "-m", "Base")]:
            subprocess.run(
                ["git", "-C", str(repo), *args],
                env=self.env,
                capture_output=True,
                check=True,
            )
        result = subprocess.run(
            [
                str(self.root / "bin/agent-run"),
                "cross-reviewer",
                str(repo),
                "--base",
                "HEAD",
                "--out",
                str(self.home / "role review.json"),
                "--dry-run",
            ],
            env=dict(self.env, AGENT_HOST="codex"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        command = next(
            line.removeprefix("command: ")
            for line in result.stdout.splitlines()
            if line.startswith("command: ")
        )
        self.assertIn("chosen-model", shlex.split(command))

    def test_commands_component_uses_configured_root(self) -> None:
        self.root = self.home / 'O\'Brien "commands root"'
        self.run_setup("--components", "rules", "commands", "--apply")
        doc = self.host / "commands/bind.md"
        text = doc.read_text()
        self.assertNotIn("~/.agents/bin", text)
        self.assertIn("export AGENT_KIT_DIR=", text)
        export = next(
            line
            for line in text.splitlines()
            if line.startswith("export AGENT_KIT_DIR=")
        )
        result = subprocess.run(
            ["bash", "-c", export + '\n"$AGENT_KIT_DIR/bin/agent-task" --help'],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agent-task:", result.stdout)

    def test_user_hook_canary_never_enters_journal(self) -> None:
        self.host.mkdir()
        canary = "PUBLIC_SYNTHETIC_USER_HOOK_CANARY"
        (self.host / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {"type": "command", "command": "printf " + canary}
                                ]
                            }
                        ]
                    }
                }
            )
        )
        self.run_setup("--components", "hooks", "--apply")
        for path in (self.root / ".install-state").rglob("*.json"):
            self.assertNotIn(canary, path.read_text(), str(path))
        self.assertIn(canary, (self.host / "settings.json").read_text())
        self.run_setup("rollback", self.state()["id"])
        self.assertIn(canary, (self.host / "settings.json").read_text())

    def test_quoted_native_paths_render_and_execute(self) -> None:
        self.root = self.home / 'O\'Brien "kit"'
        self.host = self.home / 'O\'Brien "host"'
        self.run_setup("--hosts", "codex", "--components", "hooks", "--apply")
        hooks = json.loads((self.host / "hooks.json").read_text())["hooks"]
        for groups in hooks.values():
            for group in groups:
                for hook in group.get("hooks", []):
                    words = shlex.split(hook["command"])
                    self.assertIn(str(self.root / "hooks/host-adapter"), words)
        config = tomllib.loads((self.host / "config.toml").read_text())
        self.assertEqual(
            config["shell_environment_policy"]["set"]["AGENT_KIT_DIR"], str(self.root)
        )
        command = hooks["SessionStart"][0]["hooks"][0]["command"]
        result = subprocess.run(
            ["bash", "-c", command],
            input=json.dumps(
                {
                    "session_id": "quoted-fixture",
                    "cwd": str(self.home),
                    "hook_event_name": "SessionStart",
                }
            ),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fresh_roots_write_no_line_and_explicit_empty_clears_saved(self) -> None:
        self.run_setup("--components", "rules", "--apply")
        overlay = (self.root / "local/setup-paths.env").read_text()
        # Nobody gave roots: the user's legacy CODE_DIRS (kit.env) still counts.
        self.assertNotIn("CODE_DIRS_JSON", overlay)
        self.run_setup(
            "--components",
            "rules",
            "--repo-roots",
            str(self.home / "chosen roots"),
            "--apply",
        )
        self.run_setup("--components", "rules", "--repo-roots", "--apply")
        self.assertIn(
            "CODE_DIRS_JSON=[]\n", (self.root / "local/setup-paths.env").read_text()
        )

    def test_role_preview_reports_providers_and_unavailable_native_routes(self) -> None:
        (self.commands / "claude").unlink()
        result = self.run_setup(
            "--hosts",
            "codex",
            "--components",
            "roles",
            "--claude-bin",
            str(self.home / "unavailable provider"),
        )
        rows = json.loads(result.stdout)["role_routing"]["codex"]
        self.assertEqual({row["provider"] for row in rows}, {"anthropic", "openai"})
        self.assertTrue(all(row["login"] == "not checked" for row in rows))
        self.assertTrue(
            any(
                row["provider"] == "anthropic" and not row["cli_available"]
                for row in rows
            )
        )
        self.assertTrue(
            any(
                row["role"] == "engineer" and "unavailable" in row["availability"]
                for row in rows
            )
        )

    def test_guided_setup_collects_paths_with_real_terminal(self) -> None:
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [
                sys.executable,
                str(SOURCE / "bin/agent-setup"),
                "--source",
                str(SOURCE),
                "--root-dir",
                str(self.root),
                "--host-root",
                str(self.host),
                "--interactive",
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=self.env,
        )
        os.close(slave)
        roots = [str(self.home / "selected repositories with spaces")]
        answers = [
            "",
            "claude",
            "rules skills",
            "code-search",
            json.dumps(roots),
            "example-owner",
            str(self.home / "selected work root"),
            str(self.host),
            "",
            "n",
        ]
        os.write(master, ("\n".join(answers) + "\n").encode())
        chunks = []
        deadline = time.monotonic() + 60
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                if process.poll() is not None and not ready:
                    break
            # The terminal reports EOF as the child exits, which can be before it is reaped.
            try:
                process.wait(timeout=max(deadline - time.monotonic(), 0))
            except subprocess.TimeoutExpired:
                self.fail("interactive setup did not finish")
            self.assertEqual(
                process.returncode, 0, b"".join(chunks).decode(errors="replace")[-1000:]
            )
            text = b"".join(chunks).decode(errors="replace")
            self.assertIn("agent-setup preview:", text)
            self.assertIn("Ready:", text)
            self.assertIn("example-owner", text)
            self.assertFalse(self.root.exists())
            self.assertFalse(self.host.exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master)

    def test_toml_trailing_table_extension_refuses_without_changes(self) -> None:
        self.run_setup("--hosts", "codex", "--components", "hooks", "--apply")
        config = self.host / "config.toml"
        config.write_text(config.read_text() + 'USER_EXTRA="keep semantics"\n')
        before = config.read_bytes()
        self.run_setup(
            "--hosts", "codex", "--components", "hooks", "--apply", success=False
        )
        self.assertEqual(config.read_bytes(), before)

    def test_round2_no_bytecode_export_and_noop_without_env(self) -> None:
        env = dict(self.env)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        self.run_setup("--components", "rules", "--apply", env=env)
        self.assertFalse(list(self.root.rglob("*.pyc")))
        result = self.run_setup("--components", "rules", "--apply", env=env)
        self.assertIn('"installed": 0', result.stdout)

    def test_round2_previous_owned_bytecode_upgrade(self) -> None:
        self.run_setup("--components", "rules", "--apply")
        cache = self.root / "bin/__pycache__/legacy.pyc"
        cache.parent.mkdir(exist_ok=True)
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import py_compile,sys;py_compile.compile(sys.argv[1],cfile=sys.argv[2],doraise=True)",
                str(self.root / "bin/agent-kit"),
                str(cache),
            ],
            env=self.env,
            capture_output=True,
            check=True,
        )
        state = self.state()
        state["managed"]["file:" + str(cache)] = {
            "kind": "file",
            "target": str(cache),
            "hash": hashlib.sha256(cache.read_bytes()).hexdigest(),
            "mode": cache.stat().st_mode & 0o777,
            "status": "completed",
        }
        (self.root / ".install-state/current.json").write_text(json.dumps(state))
        original = cache.read_bytes()
        self.run_setup("--components", "rules", "--apply")
        self.assertFalse(list(self.root.rglob("*.pyc")))
        self.assertFalse(
            any("__pycache__" in identity for identity in self.state()["managed"])
        )
        self.run_setup("rollback", self.state()["id"])
        self.assertEqual(cache.read_bytes(), original)

    def test_round2_existing_codex_policy_preserved(self) -> None:
        self.host.mkdir()
        config = self.host / "config.toml"
        original = 'model="user-model"\n[shell_environment_policy]\ninherit="core"\n'
        config.write_text(original)
        self.run_setup("--hosts", "codex", "--components", "hooks", "--apply")
        document = tomllib.loads(config.read_text())
        self.assertEqual(document["shell_environment_policy"]["inherit"], "core")
        self.assertEqual(document["model"], "user-model")
        result = self.run_setup("--hosts", "codex", "--components", "hooks", "--apply")
        self.assertIn('"installed": 0', result.stdout)
        self.run_setup("rollback", self.state()["id"])
        self.run_setup("rollback", self.state()["id"])
        self.assertEqual(tomllib.loads(config.read_text()), tomllib.loads(original))

    def test_round2_mcp_metadata_normalized_all_hosts(self) -> None:
        catalog = self.home / "safe catalog.json"
        catalog.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "example": {
                            "command": "/usr/bin/true",
                            "type": "stdio",
                            "description": "Uses native OAuth tokens",
                            "args": ["--help"],
                        },
                        "remote": {
                            "url": "https://example.invalid/mcp",
                            "type": "http",
                            "description": "Example HTTP transport",
                        },
                    }
                }
            )
        )
        for host in ("claude", "codex", "cursor"):
            self.root = self.home / ("kit " + host)
            self.host = self.home / ("host " + host)
            self.run_setup(
                "--hosts",
                host,
                "--components",
                "mcp",
                "--mcp-catalog",
                str(catalog),
                "--apply",
            )
            # The installed catalog keeps the description for the dashboard; no host file gets it.
            installed = json.loads((self.root / "mcp/servers.json").read_text())[
                "mcpServers"
            ]
            self.assertEqual(
                installed["example"],
                {"command": "/usr/bin/true", "args": ["--help"], "description": "Uses native OAuth tokens"},
            )
            self.assertEqual(
                installed["remote"],
                {"url": "https://example.invalid/mcp", "description": "Example HTTP transport"},
            )
            leaked = [p for p in self.host.rglob("*") if p.is_file() and b"Uses native OAuth tokens" in p.read_bytes()]
            self.assertEqual(leaked, [], f"{host} got the description")

    def test_round2_mcp_invalid_metadata_refuses_before_mutation(self) -> None:
        specs = [
            {"command": "true", "type": "http"},
            {"url": "https://example.invalid/mcp", "type": "stdio"},
            {"command": "true", "url": "https://example.invalid/mcp"},
            {"command": "true", "description": {}},
            {"command": "true", "args": [3]},
            {"command": "true", "env": {"AUTH_TOKEN": "synthetic"}},
            {"command": "true", "args": ["--token", "synthetic"]},
            {"command": "true", "args": ["--password", "synthetic"]},
            {"url": "https://user:synthetic@example.invalid/mcp"},
            {"url": "https://example.invalid/mcp", "headers": {}},
        ]
        catalog = self.home / "invalid catalog.json"
        for number, spec in enumerate(specs):
            catalog.write_text(json.dumps({"mcpServers": {"example": spec}}))
            self.root = self.home / ("invalid kit " + str(number))
            self.run_setup(
                "--hosts",
                "claude",
                "--components",
                "mcp",
                "--mcp-catalog",
                str(catalog),
                "--apply",
                success=False,
            )
            self.assertFalse(self.root.exists())
            self.assertFalse(self.host.exists())

    def test_round2_edited_retired_skill_explicit_backup_and_rollback(self) -> None:
        self.run_setup(
            "--components", "skills", "--skills", "code-search", "unslop", "--apply"
        )
        path = self.root / "skills/unslop/SKILL.md"
        original = path.read_bytes() + b"\nUser addition\n"
        path.write_bytes(original)
        self.run_setup(
            "--components",
            "skills",
            "--skills",
            "code-search",
            "--apply",
            success=False,
        )
        self.assertEqual(path.read_bytes(), original)
        self.run_setup(
            "--components",
            "skills",
            "--skills",
            "code-search",
            "--collision",
            "backup",
            "--apply",
        )
        identifier = self.state()["id"]
        journal = json.loads(
            (self.root / ".install-state" / identifier / "journal.json").read_text()
        )
        record = next(row for row in journal["records"] if row["target"] == str(path))
        self.assertEqual(Path(record["backup"]).read_bytes(), original)
        self.assertFalse(path.exists())
        self.run_setup("rollback", identifier)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue((self.host / "skills/unslop").is_symlink())

    def test_round2_cursor_old_text_ownership_upgrade_and_rollback(self) -> None:
        project = self.home / "project"
        project.mkdir()
        self.run_setup(
            "--hosts",
            "cursor",
            "--components",
            "rules",
            "--project-root",
            str(project),
            "--apply",
        )
        path = project / ".cursor/rules/agent-kit.mdc"
        block = (
            "<!-- BEGIN agent-kit setup -->\n"
            + path.read_text().rstrip()
            + "\n<!-- END agent-kit setup -->"
        )
        path.write_text(block + "\n")
        original = path.read_bytes()
        state = self.state()
        old_id = state["id"]
        state["managed"].pop("file:" + str(path))
        state["managed"]["text:" + str(path)] = {
            "kind": "text",
            "target": str(path),
            "mode": 0o644,
            "owned": block,
            "previous": "",
            "existed": False,
            "status": "completed",
        }
        (self.root / ".install-state/current.json").write_text(json.dumps(state))
        self.run_setup(
            "--hosts",
            "cursor",
            "--components",
            "rules",
            "--project-root",
            str(project),
            "--apply",
        )
        state = self.state()
        self.assertNotIn("text:" + str(path), state["managed"])
        self.assertIn("file:" + str(path), state["managed"])
        self.assertTrue(path.read_text().startswith("---\n"))
        self.run_setup("doctor")
        self.run_setup("rollback", state["id"])
        self.assertEqual(self.state()["id"], old_id)
        self.assertEqual(path.read_bytes(), original)

    def test_round2_all_skills_and_empty_selection(self) -> None:
        self.run_setup("--components", "skills", "--skills", "unslop", "--apply")
        self.run_setup("--components", "skills", "--skills", "all", "--apply")
        self.assertIsNone(self.state()["configuration"]["skills"])
        self.assertTrue((self.host / "skills/code-search").is_symlink())
        self.run_setup("--components", "skills", "--skills", "--apply")
        self.assertEqual(self.state()["configuration"]["skills"], [])
        self.assertFalse((self.host / "skills/unslop").exists())
        self.assertFalse((self.host / "skills/code-search").exists())
        self.run_setup("--components", "skills", "--apply")
        self.assertEqual(self.state()["configuration"]["skills"], [])

    def test_round2_dangling_link_backup_interruption_restores(self) -> None:
        self.host.mkdir()
        target = self.host / "bin"
        original = str(self.home / "absent destination")
        target.symlink_to(original, target_is_directory=True)
        code = "import os,runpy,sys;from pathlib import Path;source,root,host=sys.argv[1:];target=Path(host)/'bin'\ndef stop(frame,event,arg):\n if frame.f_code.co_name=='apply_change' and frame.f_locals.get('target')==target:\n  backup=frame.f_locals.get('backup')\n  if backup is not None and backup.is_symlink() and not target.exists() and not target.is_symlink():os._exit(77)\n return stop\nsys.argv=[source,'--source',str(Path(source).parent.parent),'--root-dir',root,'--host-root',host,'--components','hooks','--collision','backup','--apply'];sys.settrace(stop);runpy.run_path(source,run_name='__main__')"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(SOURCE / "bin/agent-setup"),
                str(self.root),
                str(self.host),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 77, result.stderr)
        identifier = json.loads(
            (self.root / ".install-state/pending.json").read_text()
        )["id"]
        self.run_setup("--components", "hooks", "--apply", success=False)
        self.run_setup("rollback", identifier)
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), original)
        self.assertFalse((self.root / ".install-state/pending.json").exists())

    def test_round2_installed_preview_inherits_source_and_explicit_override(
        self,
    ) -> None:
        self.run_setup("--components", "rules", "--apply")
        before = self.state()["id"]
        executable = self.root / "bin/agent-setup"
        base = [sys.executable, str(executable), "--root-dir", str(self.root), "--json"]
        result = subprocess.run(
            base, env=self.env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["source_checkout"], str(SOURCE.resolve())
        )
        alternate = self.fresh_source()
        result = subprocess.run(
            [*base, "--source", str(alternate)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["source_checkout"], str(alternate.resolve())
        )
        result = subprocess.run(
            [*base, "--source", str(self.root)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("separate from the source checkout", result.stderr)
        alias = self.home / "installed root alias"
        alias.symlink_to(self.root, target_is_directory=True)
        result = subprocess.run(
            [
                sys.executable,
                str(executable),
                "--root-dir",
                str(alias),
                "--source",
                str(self.root),
                "--collision",
                "backup",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("separate from the source checkout", result.stderr)
        self.assertEqual(self.state()["id"], before)

    def test_round2_new_checkout_default_wins_saved_origin(self) -> None:
        self.run_setup("--components", "rules", "--apply")
        alternate = self.fresh_source()
        result = subprocess.run(
            [
                sys.executable,
                str(alternate / "bin/agent-setup"),
                "--root-dir",
                str(self.root),
                "--json",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["source_checkout"], str(alternate.resolve())
        )
        self.assertEqual(
            self.state()["configuration"]["source_checkout"], str(SOURCE.resolve())
        )

    def test_round2_installed_readers_work_without_original_source(self) -> None:
        alternate = self.fresh_source()
        result = subprocess.run(
            [
                sys.executable,
                str(alternate / "bin/agent-setup"),
                "--root-dir",
                str(self.root),
                "--host-root",
                str(self.host),
                "--components",
                "rules",
                "--apply",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        identifier = self.state()["id"]
        alternate.rename(alternate.with_name("source moved"))
        base = [
            sys.executable,
            str(self.root / "bin/agent-setup"),
            "--root-dir",
            str(self.root),
        ]
        result = subprocess.run(
            base, env=self.env, capture_output=True, text=True, timeout=60
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--source /path/to/checkout", result.stderr)
        result = subprocess.run(
            [*base, "doctor"], env=self.env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(
            [*base, "rollback", identifier],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_round3_native_transport_types_match_each_host(self) -> None:
        catalog = self.home / "transport catalog.json"
        catalog.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "type": "http",
                            "url": "https://example.invalid/mcp",
                        },
                        "local": {
                            "type": "stdio",
                            "command": "/usr/bin/true",
                            "args": ["--help"],
                        },
                    }
                }
            )
        )
        for host in ("claude", "cursor", "codex"):
            self.root = self.home / ("transport kit " + host)
            self.host = self.home / ("transport host " + host)
            self.run_setup(
                "--hosts",
                host,
                "--components",
                "mcp",
                "--mcp-catalog",
                str(catalog),
                "--apply",
            )
            if host == "codex":
                entries = tomllib.loads((self.host / "config.toml").read_text())[
                    "mcp_servers"
                ]
            else:
                entries = json.loads((self.host / "mcp.json").read_text())["mcpServers"]
            expected_remote = {"url": "https://example.invalid/mcp"}
            expected_local = {"command": "/usr/bin/true", "args": ["--help"]}
            if host == "claude":
                expected_remote["type"] = "http"
                expected_local["type"] = "stdio"
            self.assertEqual(entries["remote"], expected_remote)
            self.assertEqual(entries["local"], expected_local)
            self.run_setup(
                "--hosts",
                host,
                "--components",
                "mcp",
                "--mcp-catalog",
                str(catalog),
                "--apply",
            )

    def test_round3_common_inline_credential_shapes_refused(self) -> None:
        catalog = self.home / "credential-shape catalog.json"
        specs = [
            {"url": "https://example.invalid/mcp?apikey=synthetic"},
            {"url": "https://example.invalid/mcp?API_KEY=synthetic"},
            {"url": "https://example.invalid/mcp?api-key=synthetic"},
            {"url": "https://example.invalid/mcp?key=synthetic"},
            {"command": "/usr/bin/true", "args": ["--key", "synthetic"]},
            {"command": "/usr/bin/true", "args": ["--key=synthetic"]},
        ]
        for number, spec in enumerate(specs):
            catalog.write_text(json.dumps({"mcpServers": {"example": spec}}))
            self.root = self.home / ("credential-shape kit " + str(number))
            self.run_setup(
                "--components",
                "mcp",
                "--mcp-catalog",
                str(catalog),
                "--apply",
                success=False,
            )
            self.assertFalse(self.root.exists())
            self.assertFalse(self.host.exists())

    def test_round3_benign_transport_options_preserved(self) -> None:
        catalog = self.home / "benign catalog.json"
        servers = {
            "remote": {"url": "https://example.invalid/mcp?page_size=10&region=west"},
            "local": {
                "command": "/usr/bin/true",
                "args": ["--port", "8080", "--region=west"],
            },
        }
        catalog.write_text(json.dumps({"mcpServers": servers}))
        self.run_setup("--components", "mcp", "--mcp-catalog", str(catalog), "--apply")
        canonical = json.loads((self.root / "mcp/servers.json").read_text())[
            "mcpServers"
        ]
        self.assertEqual(canonical, servers)
        entries = json.loads((self.host / "mcp.json").read_text())["mcpServers"]
        self.assertEqual(entries["remote"], {**servers["remote"], "type": "http"})
        self.assertEqual(entries["local"], {**servers["local"], "type": "stdio"})

    def test_round3_blocking_choice_persisted_without_default_change(self) -> None:
        self.run_setup("--components", "hooks", "--blocking-hooks", "--apply")
        self.assertTrue(self.state()["configuration"]["blocking_hooks"])
        result = self.run_setup("--components", "hooks")
        self.assertTrue(
            json.loads(result.stdout)["blocking_hooks"],
            "4c: reselecting hooks without the flag keeps the saved choice",
        )
        result = self.run_setup("--components", "hooks", "--no-blocking-hooks")
        self.assertFalse(json.loads(result.stdout)["blocking_hooks"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SetupReviewTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(
        json.dumps(
            {
                "cases": suite.countTestCases(),
                "successful": result.wasSuccessful(),
                "failures": len(result.failures),
                "errors": len(result.errors),
            },
            indent=2,
        )
        + "\n"
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
