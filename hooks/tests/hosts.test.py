#!/usr/bin/env python3
"""Native host contracts use synthetic homes, MCP catalogs and rollout records."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(os.environ.get("AGENT_KIT_SOURCE", Path(__file__).resolve().parents[2]))


class HostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.kit = self.root / ".agents"
        for name in ("bin", "hooks", "rules", "agents"):
            shutil.copytree(SOURCE / name, self.kit / name)
        shutil.copyfile(SOURCE / "roles.toml", self.kit / "roles.toml")
        (self.kit / "mcp").mkdir()
        self.catalog = {
            "mcpServers": {
                "sentry": {
                    "command": "npx",
                    "args": ["-y", "@sentry/mcp-server@0.37.0", "--access-token=fixture-only"],
                },
                "demo": {"command": "true", "args": []},
            }
        }
        (self.kit / "mcp/servers.json").write_text(json.dumps(self.catalog))
        self.env = dict(
            os.environ,
            HOME=str(self.root),
            KIT_ENV="/dev/null",
            AGENT_KIT_DIR=str(self.kit),
            PYTHONDONTWRITEBYTECODE="1",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_kit(self, host: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.kit / "bin/agent-kit"), "render", "--host", host, *extra],
            env={**self.env, "AGENT_KIT_HOST_ROOT": str(self.root / f".{host}")},
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_codex_render_preserves_config_and_roles(self) -> None:
        root = self.root / ".codex"
        root.mkdir()
        prefix = 'model = "user-choice"\n[ui]\ntheme = "light"\n'
        (root / "config.toml").write_text(prefix)
        result = self.run_kit("codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (root / "config.toml").read_text()
        self.assertTrue(text.startswith(prefix))
        self.assertNotIn("fixture-only", text + result.stdout)
        self.assertIn("sentry-mcp", tomllib.loads(text)["mcp_servers"]["sentry"]["command"])
        shell = tomllib.loads(text)["shell_environment_policy"]["set"]
        self.assertEqual(shell["AGENT_HOST"], "codex")
        self.assertEqual(shell["GIT_CONFIG_VALUE_0"], str(self.kit / "git-hooks"))
        role = tomllib.loads((root / "agents/cross-reviewer.toml").read_text())
        self.assertEqual(role["model"], "gpt-6.1-sol")
        self.assertEqual(role["model_reasoning_effort"], "high")
        self.assertEqual(role["sandbox_mode"], "read-only")
        self.assertIn("cross-model reviewer", role["developer_instructions"])
        self.assertFalse((root / "agents/engineer.toml").exists())
        hooks = json.loads((root / "hooks.json").read_text())["hooks"]
        for event in ("PreToolUse", "PostToolUse"):
            edits = [
                group
                for group in hooks[event]
                if any(
                    "edit-guard" in hook["command"] or "review-mark-changes" in hook["command"]
                    for hook in group["hooks"]
                )
            ]
            self.assertTrue(edits)
            self.assertTrue(all("apply_patch" in group["matcher"] for group in edits))
        roles = subprocess.run(
            [str(self.kit / "bin/agent-kit"), "roles", "--host", "codex"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertIn(
            "run",
            next(line for line in roles.stdout.splitlines() if line.startswith("cross-reviewer")),
        )
        self.assertEqual(self.run_kit("codex").returncode, 0)
        self.assertEqual(text, (root / "config.toml").read_text())

    def test_cursor_native_schema_and_secret_free_mcp(self) -> None:
        result = self.run_kit("cursor")
        self.assertEqual(result.returncode, 0, result.stderr)
        root = self.root / ".cursor"
        hooks = json.loads((root / "hooks.json").read_text())
        self.assertEqual(hooks["version"], 1)
        self.assertIn("preToolUse", hooks["hooks"])
        self.assertNotIn("PreToolUse", hooks["hooks"])
        self.assertTrue(
            any("Delete" in hook.get("matcher", "") for hook in hooks["hooks"]["preToolUse"])
        )
        self.assertTrue(all(h.get("failClosed") for h in hooks["hooks"]["preToolUse"]))
        self.assertNotIn("fixture-only", (root / "mcp.json").read_text())
        self.assertIn("alwaysApply: true", (root / "rules/agent-kit.mdc").read_text())

    def test_config_collision_writes_nothing(self) -> None:
        root = self.root / ".codex"
        root.mkdir()
        text = '[mcp_servers.sentry]\ncommand = "user-wrapper"\n'
        (root / "config.toml").write_text(text)
        result = self.run_kit("codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(text, (root / "config.toml").read_text())
        self.assertFalse((root / "hooks.json").exists())

    def test_inline_hooks_refuse_duplicate_sources(self) -> None:
        root = self.root / ".codex"
        root.mkdir()
        (root / "config.toml").write_text(
            '[hooks]\nStop = [{ hooks = [{ command = "true", type = "command" }] }]\n'
        )
        result = self.run_kit("codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate", result.stderr)

    def test_preserve_user_hooks_and_rule_text(self) -> None:
        root = self.root / ".codex"
        root.mkdir()
        user = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}
        (root / "hooks.json").write_text(json.dumps(user))
        (root / "AGENTS.md").write_text("User rule stays.\n")
        self.assertEqual(self.run_kit("codex").returncode, 0)
        self.assertEqual(self.run_kit("codex").returncode, 0)
        hooks = json.loads((root / "hooks.json").read_text())
        self.assertEqual(hooks["hooks"]["Stop"].count(user["hooks"]["Stop"][0]), 1)
        self.assertTrue((root / "AGENTS.md").read_text().startswith("User rule stays.\n"))

    def test_agent_user_edit_refuses_partial_render(self) -> None:
        root = self.root / ".codex"
        (root / "agents").mkdir(parents=True)
        (root / "agents/cross-reviewer.toml").write_text('name = "user-owned"\n')
        result = self.run_kit("codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "hooks.json").exists())

    def test_dry_run_writes_nothing(self) -> None:
        result = self.run_kit("codex", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / ".codex").exists())
        self.assertFalse((self.kit / "state").exists())

    def test_native_rule_link_cannot_rewrite_the_shared_source(self) -> None:
        root = self.root / ".codex"
        root.mkdir()
        source = self.kit / "rules/AGENTS.md"
        before = source.read_text()
        (root / "AGENTS.md").symlink_to(source)
        result = self.run_kit("codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(source.read_text(), before)
        self.assertFalse((root / "hooks.json").exists())

    def test_component_selection_does_not_read_or_write_mcp(self) -> None:
        (self.kit / "mcp/servers.json").write_text("invalid and intentionally unreadable")
        result = self.run_kit("codex", "--components", "rules")
        self.assertEqual(result.returncode, 0, result.stderr)
        root = self.root / ".codex"
        self.assertTrue((root / "AGENTS.md").exists())
        self.assertFalse((root / "config.toml").exists())
        self.assertFalse((root / "hooks.json").exists())
        self.assertFalse((root / "agents").exists())

    def test_user_shell_environment_preserves_unrelated_keys(self) -> None:
        root = self.root / ".codex"
        root.mkdir()
        text = '[shell_environment_policy]\nset = { USER_SETTING = "keep" }\n'
        (root / "config.toml").write_text(text)
        result = self.run_kit("codex", "--components", "hooks")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / "hooks.json").exists())
        policy = tomllib.loads((root / "config.toml").read_text())["shell_environment_policy"]
        self.assertEqual(policy["set"]["USER_SETTING"], "keep")
        self.assertEqual(policy["set"]["AGENT_HOST"], "codex")

    def hook(self, host: str, name: str, payload: dict) -> dict:
        adapter = self.kit / "hooks/host-adapter"
        self.assertTrue(adapter.exists(), "native hooks need a host adapter")
        result = subprocess.run(
            [str(adapter), host, name],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_codex_multifile_patch_checks_protected_destination(self) -> None:
        self.assertEqual(self.run_kit("codex").returncode, 0)
        payload = {
            "hook_event_name": "PreToolUse",
            "turn_id": "t",
            "tool_name": "apply_patch",
            "cwd": str(self.root),
            "tool_input": {
                "command": "*** Begin Patch\n*** Add File: ordinary.py\n+x\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch"
            },
        }
        output = self.hook("codex", "edit-guard", payload)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_codex_move_destination_is_guarded(self) -> None:
        self.assertEqual(self.run_kit("codex").returncode, 0)
        payload = {
            "hook_event_name": "PreToolUse",
            "turn_id": "t",
            "tool_name": "apply_patch",
            "cwd": str(self.root),
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: ordinary.py\n*** Move to: .codex/hooks.json\n@@\n-x\n+y\n*** End Patch"
            },
        }
        output = self.hook("codex", "edit-guard", payload)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_native_production_write_is_denied(self) -> None:
        for host, tool in (("codex", "Bash"), ("cursor", "Shell")):
            with self.subTest(host=host):
                payload = {
                    "hook_event_name": "preToolUse" if host == "cursor" else "PreToolUse",
                    "tool_name": tool,
                    "tool_input": {"command": "mysql -e 'UPDATE orders SET x=1'"},
                    "cwd": str(self.root),
                    "session_id": "fixture",
                }
                payload["cursor_version" if host == "cursor" else "turn_id"] = "t"
                output = self.hook(host, "bash-guards", payload)
                decision = (
                    output.get("permission")
                    if host == "cursor"
                    else output["hookSpecificOutput"]["permissionDecision"]
                )
                self.assertEqual(decision, "deny")

    def test_cursor_neutral_pretool_hook_allows_valid_payload(self) -> None:
        output = self.hook(
            "cursor",
            "bash-guards",
            {
                "hook_event_name": "preToolUse",
                "cursor_version": "1",
                "tool_name": "Shell",
                "tool_input": {"command": "pwd"},
            },
        )
        self.assertEqual(output, {"permission": "allow"})

    def test_codex_destructive_confirmation_is_blocked_not_unsupported_ask(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "turn_id": "t",
            "tool_name": "Bash",
            "tool_input": {"command": "git reset --hard HEAD"},
            "cwd": str(self.root),
        }
        output = self.hook("codex", "bash-guards", payload)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_doctor_reads_only_and_does_not_resolve_credentials(self) -> None:
        self.assertEqual(self.run_kit("codex").returncode, 0)
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = subprocess.run(
            [str(self.kit / "bin/agent-kit"), "doctor", "--host", "codex"],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)[0]
        self.assertEqual(report["mcp_servers"], ["demo", "sentry"])
        self.assertFalse(report["runtime_rules_proven"])
        self.assertNotIn("fixture-only", result.stdout)
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class CodexRolloutTests(unittest.TestCase):
    def test_cumulative_counts_window_and_duplicate_records(self) -> None:
        path = SOURCE / "skills/session-review/scripts/codex_sessions.py"
        self.assertTrue(path.exists(), "native rollouts need a session reader")
        spec = importlib.util.spec_from_file_location("codex_sessions", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            rows = [
                {
                    "timestamp": "2026-10-03T23:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "native-test"},
                },
                {
                    "timestamp": "2026-10-03T23:59:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 100}},
                    },
                },
                {
                    "timestamp": "2026-10-04T00:00:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-test"},
                },
                {
                    "timestamp": "2026-10-04T00:01:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 140}},
                    },
                },
                {
                    "timestamp": "2026-10-04T00:02:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 140}},
                    },
                },
            ]
            rollout.write_text("\n".join(json.dumps(r) for r in rows))
            result = module.read_session(
                rollout,
                datetime(2026, 10, 4, tzinfo=timezone.utc),
                datetime(2026, 10, 5, tzinfo=timezone.utc),
            )
            self.assertEqual(result["tokens"]["input_tokens"], 40)
            self.assertEqual(result["model"], "gpt-test")
            self.assertEqual(result["duration_seconds"], 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
