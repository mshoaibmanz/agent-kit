#!/usr/bin/env python3
"""Round-one host regressions; disposable homes and catalogs, no real providers or credentials."""

from __future__ import annotations
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

SOURCE = Path(os.environ.get("AGENT_KIT_SOURCE", Path(__file__).resolve().parents[2]))


def load(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


class ReviewTests(unittest.TestCase):
    def setUp(self):
        # Repo-dependent guards deliberately ignore literal scratch paths. This owned fixture is
        # outside those paths, and TemporaryDirectory removes only the directory it created.
        self.temp = tempfile.TemporaryDirectory(
            prefix=".hosts-review-",
            dir=Path(os.environ.get("AGENT_TEST_FIXTURE_ROOT", Path.home())),
        )
        self.root = Path(self.temp.name)
        self.kit = self.root / ".agents"
        for name in ("bin", "hooks", "rules", "agents", "git-hooks", "commands"):
            if (SOURCE / name).exists():
                shutil.copytree(SOURCE / name, self.kit / name)
        shutil.copyfile(SOURCE / "roles.toml", self.kit / "roles.toml")
        (self.kit / "mcp").mkdir()
        self.catalog = {"mcpServers": {"demo": {"command": "true", "args": []}}}
        self.catalog_write()
        (self.kit / "hosts/claude").mkdir(parents=True)
        (self.kit / "hosts/claude/host.json").write_text(
            json.dumps(
                {
                    "settings": "~/.claude/settings.json",
                    "mcp": "~/.claude/mcp.json",
                    "agents": "~/.claude/agents",
                    "uiOwned": [],
                }
            )
        )
        (self.kit / "hosts/claude/settings.base.json").write_text("{}")
        self.env = dict(
            os.environ,
            HOME=str(self.root),
            KIT_ENV="/dev/null",
            AGENT_KIT_DIR=str(self.kit),
            PYTHONDONTWRITEBYTECODE="1",
            TMPDIR=str(self.root / "state-tmp"),
        )
        (self.root / "state-tmp").mkdir()
        for key in ("AGENT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "AGENT_HOST"):
            self.env.pop(key, None)

    def tearDown(self):
        self.temp.cleanup()

    def catalog_write(self):
        (self.kit / "mcp/servers.json").write_text(json.dumps(self.catalog))

    def render(self, host="codex", extra=(), env=None):
        return subprocess.run(
            [str(self.kit / "bin/agent-kit"), "render", "--host", host, *extra],
            env=env or {**self.env, "AGENT_KIT_HOST_ROOT": str(self.root / f".{host}")},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def repo(self, name):
        path = self.root / name
        path.mkdir()
        for args in (
            ("init", "-q"),
            ("config", "user.name", "fixture"),
            ("config", "user.email", "fixture@example.invalid"),
            ("config", "commit.gpgsign", "false"),
        ):
            subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
        (path / "a.py").write_text("x=0\n")
        subprocess.run(["git", "-C", str(path), "add", "a.py"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)
        return path

    def hook(self, name, payload):
        result = subprocess.run(
            [str(self.kit / "hooks/host-adapter"), "codex", name],
            input=json.dumps(payload),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_CX1_push_gate_native_and_inherited_sessions(self):
        for extra, expected in (
            ({"CODEX_THREAD_ID": "native-one"}, "native-one"),
            (
                {
                    "CODEX_THREAD_ID": "native-two",
                    "CLAUDE_CODE_SESSION_ID": "inherited",
                    "AGENT_HOST": "codex",
                },
                "native-two",
            ),
            ({"CODEX_THREAD_ID": "native-two", "AGENT_SESSION_ID": "explicit"}, "explicit"),
        ):
            result = subprocess.run(
                ["bash", "-c", '. "$1/hooks/lib/push-gate"; pg_sid', "probe", str(self.kit)],
                env={**self.env, **extra},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.stdout.strip(), expected, result.stderr)

    def test_CX1_cursor_session_export(self):
        result = subprocess.run(
            [str(self.kit / "hooks/host-adapter"), "cursor", "host-session-context"],
            input=json.dumps(
                {
                    "hook_event_name": "sessionStart",
                    "session_id": "cursor-one",
                    "conversation_id": "cursor-one",
                    "workspace_roots": [str(self.root)],
                }
            ),
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["env"].get("AGENT_SESSION_ID"), "cursor-one")

    def test_CX2_cursor_working_directory(self):
        host = load(self.kit / "hooks/lib/host.py", f"host_{id(self)}")
        for directory, expected in (
            ("/checkout-B", "/checkout-B"),
            ("../checkout-B", "/checkout-B"),
        ):
            payload = {
                "hook_event_name": "preToolUse",
                "tool_name": "Shell",
                "cwd": "/checkout-A",
                "tool_input": {"command": "git commit -m x", "working_directory": directory},
            }
            self.assertEqual(host.normalize(payload, "cursor")[0]["cwd"], expected)

    def test_CX3_unavailable_correctness_roles_do_not_issue_round(self):
        repo = self.repo("review-repo")
        (repo / "a.py").write_text("x=[\n" + "0,\n" * 60 + "]\n")
        script = '. "$1/hooks/lib/review-state"; rv_snapshot "$2"; rv_push_decide native-session "$2" "$(git -C "$2" branch --show-current)" ""; printf "%s\n%s\n" "$RV_KIND" "$RV_TEXT"; rv_pending native-session "$2" && printf "PENDING\n"'
        result = subprocess.run(
            ["bash", "-c", script, "probe", str(self.kit), str(repo)],
            env={
                **self.env,
                "AGENT_HOST": "codex",
                "CODEX_BIN": "/missing/codex",
                "CLAUDE_BIN": "/missing/claude",
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertIn("unavailable", result.stdout, result.stderr)
        self.assertNotIn("PENDING", result.stdout)
        self.assertNotIn("select the named native", result.stdout)

    def test_CX4_unknown_failed_commit_does_not_mark(self):
        repo = self.repo("commit-repo")
        sid = "commit-native"
        common = {
            "session_id": sid,
            "cwd": str(repo),
            "turn_id": "turn",
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m next"},
        }
        self.hook("commit-cohesion", {**common, "hook_event_name": "UserPromptSubmit"})
        self.hook("commit-cohesion", {**common, "hook_event_name": "PreToolUse"})
        self.hook(
            "commit-cohesion",
            {
                **common,
                "hook_event_name": "PostToolUse",
                "tool_response": "nothing to commit; failed",
            },
        )
        marker = self.root / ".claude/tmp/claude-commit" / f"last-{sid}"
        self.assertFalse(
            marker.exists(), "Unknown status with unchanged HEAD must not consume commit allowance"
        )
        self.hook("commit-cohesion", {**common, "hook_event_name": "PreToolUse"})
        (repo / "a.py").write_text("x=1\n")
        subprocess.run(["git", "-C", str(repo), "commit", "-am", "actual", "-q"], check=True)
        self.hook(
            "commit-cohesion",
            {
                **common,
                "hook_event_name": "PostToolUse",
                "tool_response": "string response without exit status",
            },
        )
        self.assertTrue(marker.exists(), "Changed HEAD proves the attempted commit landed")

    def test_CX5_retired_cursor_server_and_user_edits(self):
        self.assertEqual(self.render("cursor").returncode, 0)
        path = self.root / ".cursor/mcp.json"
        data = json.loads(path.read_text())
        data["mcpServers"]["user"] = {"command": "true"}
        path.write_text(json.dumps(data))
        self.catalog = {"mcpServers": {}}
        self.catalog_write()
        self.assertEqual(self.render("cursor").returncode, 0)
        self.assertEqual(set(json.loads(path.read_text())["mcpServers"]), {"user"})
        self.catalog = {"mcpServers": {"demo": {"command": "true", "args": []}}}
        self.catalog_write()
        self.assertEqual(self.render("cursor").returncode, 0)
        data = json.loads(path.read_text())
        data["mcpServers"]["demo"]["command"] = "user-edited"
        path.write_text(json.dumps(data))
        before = path.read_text()
        self.catalog = {"mcpServers": {}}
        self.catalog_write()
        self.assertNotEqual(self.render("cursor").returncode, 0)
        self.assertEqual(path.read_text(), before)

    def test_CX6_interrupted_render_remembers_hook_ownership(self):
        if sys.platform != "darwin":
            self.skipTest("Real immutable-file fault requires macOS chflags")
        root = self.root / ".codex"
        root.mkdir()
        config = root / "config.toml"
        config.write_text("")
        subprocess.run(["chflags", "uchg", str(config)], check=True)
        try:
            self.assertNotEqual(self.render().returncode, 0)
            self.assertTrue((root / "hooks.json").exists())
        finally:
            subprocess.run(["chflags", "nouchg", str(config)], check=True)
        registry = json.loads((self.kit / "hooks/registry.json").read_text())
        for entry in registry:
            if "codex" in entry["hosts"]:
                entry["timeout"] = entry.get("timeout", 60) + 1
        (self.kit / "hooks/registry.json").write_text(json.dumps(registry))
        self.assertEqual(self.render().returncode, 0)
        hooks = json.loads((root / "hooks.json").read_text())["hooks"]
        commands = [
            (event, group.get("matcher"), hook["command"])
            for event, groups in hooks.items()
            for group in groups
            for hook in group["hooks"]
        ]
        self.assertEqual(
            len(commands),
            len(set(commands)),
            "Interrupted ownership cannot turn kit hooks into unmanaged duplicates",
        )

    def test_CX7_sentry_preserves_safe_options_and_env(self):
        self.catalog = {
            "mcpServers": {
                "sentry": {
                    "command": "npx",
                    "args": [
                        "-y",
                        "@sentry/mcp-server@0.37.0",
                        "--host",
                        "sentry.fixture.invalid",
                        "--organization-slug",
                        "fixture-org",
                        "--access-token",
                        "fixture-secret",
                    ],
                    "env": {
                        "SENTRY_HOST": "sentry.fixture.invalid",
                        "SENTRY_ACCESS_TOKEN": "fixture-secret",
                    },
                }
            }
        }
        self.catalog_write()
        self.assertEqual(self.render().returncode, 0)
        server = tomllib.loads((self.root / ".codex/config.toml").read_text())["mcp_servers"][
            "sentry"
        ]
        self.assertEqual(
            server["args"],
            ["--host", "sentry.fixture.invalid", "--organization-slug", "fixture-org"],
        )
        self.assertEqual(server.get("env"), {"SENTRY_HOST": "sentry.fixture.invalid"})
        self.assertNotIn("fixture-secret", json.dumps(server))

    def test_B1_foreign_rules_target_isolation(self):
        foreign = self.root / "foreign"
        self.kit.rename(foreign)
        self.kit = foreign
        live = self.root / ".claude/CLAUDE.md"
        live.parent.mkdir()
        live.symlink_to(foreign / "rules/AGENTS.md")
        before = live.read_text()
        env = {
            **self.env,
            "AGENT_KIT_DIR": str(foreign),
            "AGENT_KIT_SETTINGS": str(self.root / "out/settings.json"),
            "AGENT_KIT_MCP": str(self.root / "out/mcp.json"),
            "AGENT_KIT_AGENTS": str(self.root / "out/agents"),
        }
        (self.root / "out").mkdir()
        result = self.render("claude", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(live.is_symlink())
        self.assertEqual(live.read_text(), before)
        self.assertFalse((self.root / "out/settings.json").exists())

    def test_B4_native_outputs_and_source_alias(self):
        self.assertEqual(self.render().returncode, 0)
        self.assertEqual(self.render("cursor").returncode, 0)
        for path in (
            self.root / ".codex/config.toml",
            self.root / ".codex/agents/review-cross.toml",
            self.root / ".cursor/rules/agent-kit.mdc",
        ):
            out = self.hook(
                "edit-guard",
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "command": f"*** Begin Patch\n*** Update File: {path}\n@@\n-a\n+b\n*** End Patch"
                    },
                    "cwd": str(self.root),
                    "session_id": "native",
                },
            )
            self.assertEqual(
                out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny", str(path)
            )
        alias = self.root / ".codex/AGENTS.md"
        alias.parent.mkdir(exist_ok=True)
        alias.unlink()
        alias.symlink_to(self.kit / "rules/AGENTS.md")
        out = self.hook(
            "edit-guard",
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": f"*** Begin Patch\n*** Update File: {self.kit / 'rules/AGENTS.md'}\n@@\n-a\n+b\n*** End Patch"
                },
                "cwd": str(self.root),
                "session_id": "native",
            },
        )
        self.assertNotEqual(out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_B5_handwritten_claude_rules_are_preserved(self):
        path = self.root / ".claude/CLAUDE.md"
        path.parent.mkdir()
        path.write_text("User rule stays.\n")
        result = self.render("claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(path.read_text().startswith("User rule stays.\n"))
        self.assertIn("rules/hosts/claude.md", path.read_text())

    def test_B6_multifile_edit_is_one_guard_invocation(self):
        self.assertEqual(self.render().returncode, 0)
        hook = self.kit / "hooks/edit-guard"
        original = self.kit / "hooks/edit-guard-real"
        hook.rename(original)
        calls = self.root / "batch-calls.jsonl"
        hook.write_text(
            "#!/usr/bin/env python3\nimport json,sys,subprocess\nfrom pathlib import Path\n"
            "data=sys.stdin.read()\n"
            f"with Path({str(calls)!r}).open('a') as stream: stream.write(str(len(json.loads(data)['tool_input'].get('file_paths',[])))+'\\n')\n"
            f"result=subprocess.run([{str(original)!r}],input=data,text=True,capture_output=True)\n"
            "sys.stdout.write(result.stdout)\nsys.stderr.write(result.stderr)\nraise SystemExit(result.returncode)\n"
        )
        hook.chmod(0o700)
        patch_text = (
            "*** Begin Patch\n"
            + "".join(f"*** Update File: f{i}.py\n@@\n-a\n+b\n" for i in range(200))
            + f"*** Update File: {self.root}/.codex/config.toml\n@@\n-a\n+b\n*** End Patch"
        )
        result = subprocess.run(
            [str(self.kit / "hooks/host-adapter"), "codex", "edit-guard"],
            input=json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "apply_patch",
                    "tool_input": {"command": patch_text},
                    "cwd": str(self.root),
                    "session_id": "native",
                }
            ),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout).get("hookSpecificOutput", {}).get("permissionDecision"),
            "deny",
        )
        self.assertEqual(calls.read_text().splitlines(), ["201"])

    def test_B11_shell_filters_cannot_strip_dispatcher(self):
        root = self.root / ".codex"
        root.mkdir()
        config = root / "config.toml"
        config.write_text('[shell_environment_policy]\ninclude_only=["PATH","HOME"]\n')
        self.assertNotEqual(self.render(extra=("--components", "hooks")).returncode, 0)
        self.assertFalse((root / "hooks.json").exists())

    def test_B11_keyed_filters_cannot_strip_dispatcher(self):
        root = self.root / ".codex"
        root.mkdir()
        config = root / "config.toml"
        config.write_text(
            '[shell_environment_policy.filters]\n"PATH"="include"\n"HOME"="include"\n'
        )
        self.assertNotEqual(self.render(extra=("--components", "hooks")).returncode, 0)
        self.assertFalse((root / "hooks.json").exists())
        config.write_text('[shell_environment_policy.filters]\n"*"="include"\n"TOKEN"="exclude"\n')
        self.assertEqual(self.render(extra=("--components", "hooks")).returncode, 0)

    def test_B5_edited_managed_imports_fail_before_host_writes(self):
        path = self.root / ".claude/CLAUDE.md"
        path.parent.mkdir()
        path.write_text("User rule stays.\n")
        self.assertEqual(self.render("claude").returncode, 0)
        path.write_text(path.read_text().replace("rules/hosts/claude.md", "rules/hosts/other.md"))
        settings = self.root / ".claude/settings.json"
        before = settings.read_text()
        base = self.kit / "hosts/claude/settings.base.json"
        base.write_text('{"env":{"EXTRA":"fixture"}}')
        self.assertNotEqual(self.render("claude").returncode, 0)
        self.assertEqual(settings.read_text(), before)

    def test_B6_batch_checks_last_protected_file(self):
        self.assertEqual(self.render().returncode, 0)
        patch_text = "*** Begin Patch\n" + "".join(
            f"*** Update File: f{i}.py\n@@\n-a\n+b\n" for i in range(200)
        )
        patch_text += f"*** Update File: {self.root}/.codex/config.toml\n@@\n-a\n+b\n*** End Patch"
        out = self.hook(
            "edit-guard",
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "apply_patch",
                "tool_input": {"command": patch_text},
                "cwd": str(self.root),
                "session_id": "native",
            },
        )
        self.assertEqual(out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_B11_unsupported_codex_event_is_rejected(self):
        registry = json.loads((self.kit / "hooks/registry.json").read_text())
        registry.append(
            {"event": "Notification", "hosts": ["codex"], "command": "~/.agents/hooks/edit-guard"}
        )
        (self.kit / "hooks/registry.json").write_text(json.dumps(registry))
        self.assertNotEqual(self.render(extra=("--components", "hooks")).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
