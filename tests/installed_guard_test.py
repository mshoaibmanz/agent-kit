"""Invoke public-installed native guards with real installer ownership and synthetic homes."""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

import setup_test as support

SOURCE = Path(os.environ.get("AGENT_SETUP_TEST_SOURCE", support.SOURCE))
support.SOURCE = SOURCE


class InstalledGuardTests(unittest.TestCase):
    tearDown = support.SetupTests.tearDown
    run_setup = support.SetupTests.run_setup
    state = support.SetupTests.state

    def setUp(self) -> None:
        support.SetupTests.setUp(self)
        (self.commands / "codex").write_text(
            '#!/bin/sh\nprintf "hooks experimental true\\n"\n'
        )
        for key in (
            "AGENT_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID",
            "CODEX_HOME",
        ):
            self.env.pop(key, None)
        self.project = self.home / "repository with spaces"
        self.project.mkdir()
        self.catalog = self.home / "synthetic catalog.json"
        self.catalog.write_text(
            json.dumps({"mcpServers": {"fixture": {"command": "true"}}})
        )

    def install(self, host: str, *, mcp: bool = False) -> Path:
        self.host = self.home / f"custom {host} host"
        components = ["rules", "hooks", "roles"] + (["mcp"] if mcp else [])
        flags = [
            "--hosts",
            host,
            "--components",
            *components,
            "--blocking-hooks",
            "--apply",
        ]
        if mcp:
            flags.extend(("--mcp-catalog", str(self.catalog)))
        if host == "cursor":
            flags.extend(
                (
                    "--project-root",
                    str(self.project),
                    "--confirm-hook-support",
                    "cursor",
                )
            )
        self.run_setup(*flags)
        return self.host

    def command(self, host: str, root: Path) -> str:
        document = json.loads(
            (root / ("settings.json" if host == "claude" else "hooks.json")).read_text()
        )
        event = "preToolUse" if host == "cursor" else "PreToolUse"
        for group in document["hooks"][event]:
            for hook in group.get("hooks", [group]):
                command = hook.get("command", "")
                if any(
                    Path(part).name == "edit-guard" for part in shlex.split(command)
                ):
                    return command
        self.fail("Installed blocking edit-guard registration is absent")

    def guard(
        self, host: str, root: Path, target: Path, inp: dict
    ) -> subprocess.CompletedProcess:
        data = {
            "hook_event_name": "preToolUse" if host == "cursor" else "PreToolUse",
            "session_id": "public-installed-guard-fixture",
            "cwd": str(self.project),
            "tool_name": "EditFile" if host == "cursor" else "Edit",
            "tool_input": {"file_path": str(target), **inp},
        }
        if "command" in inp:
            data["tool_name"] = "apply_patch"
        return subprocess.run(
            ["bash", "-c", self.command(host, root)],
            input=json.dumps(data),
            env=self.env,
            cwd=self.project,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def decision(
        self, result: subprocess.CompletedProcess, host: str, denied: bool
    ) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout) if result.stdout.strip() else {}
        actual = (
            document.get("permission")
            if host == "cursor"
            else document.get("hookSpecificOutput", {}).get("permissionDecision")
        )
        self.assertEqual(actual == "deny", denied, result.stdout)

    def replace(
        self, host: str, root: Path, target: Path, old: str, new: str, *, denied: bool
    ) -> None:
        self.assertIn(old, target.read_text())
        self.decision(
            self.guard(host, root, target, {"old_string": old, "new_string": new}),
            host,
            denied,
        )

    def patch(
        self, host: str, root: Path, target: Path, text: str, *, denied: bool
    ) -> None:
        self.decision(self.guard(host, root, target, {"command": text}), host, denied)

    def claude_ownership(self, *, default_root: bool, mcp: bool) -> None:
        if not default_root:
            self.root = self.home / "kit source's directory with spaces"
        root = self.home / (".claude" if default_root else "custom claude host")
        flags = ["--hosts", "claude", "--components", "hooks", "roles"]
        if mcp:
            flags.extend(("mcp", "--mcp-catalog", str(self.catalog)))
        if not default_root:
            flags.extend(("--host-root", str(root)))
        result = subprocess.run(
            [
                sys.executable,
                str(SOURCE / "bin/agent-setup"),
                "--source",
                str(SOURCE),
                "--root-dir",
                str(self.root),
                *flags,
                "--blocking-hooks",
                "--apply",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = root / "settings.json"
        document = json.loads(settings.read_text())
        document["permissions"] = {"allow": ["Read"]}
        document["theme"] = "user-theme"
        document["hooks"]["PreToolUse"].append(
            {"hooks": [{"type": "command", "command": "user-hook-command"}]}
        )
        settings.write_text(json.dumps(document))
        for old, new in (
            ('"allow": ["Read"]', '"allow": ["Read", "Write"]'),
            ("user-theme", "changed-theme"),
            ("user-hook-command", "changed-user-hook"),
        ):
            with self.subTest(field=old):
                self.replace("claude", root, settings, old, new, denied=False)
        for owned in ("hooks", "environment"):
            record = next(
                row
                for row in self.state()["managed"].values()
                if row["target"] == str(settings) and row["kind"] == owned
            )
            changed = copy.deepcopy(document)
            if owned == "hooks":
                event, groups = next(iter(record["owned"].items()))
                changed["hooks"][event].remove(groups[0])
            else:
                key = next(iter(record["owned"]))
                changed["env"][key] = "changed-owned-value"
            denial = self.guard(
                "claude", root, settings, {"content": json.dumps(changed)}
            )
            self.decision(denial, "claude", True)
            with self.subTest(guidance=owned):
                self.assertIn("agent-setup", denial.stdout)
                self.assertNotIn("~/.agents", denial.stdout)
                reason = json.loads(denial.stdout)["hookSpecificOutput"][
                    "permissionDecisionReason"
                ]
                command = reason.split("preview ", 1)[1].split("; review", 1)[0]
                self.assertEqual(
                    shlex.split(command),
                    [
                        str(SOURCE / "bin/agent-setup"),
                        "--root-dir",
                        str(self.root),
                        "--blocking-hooks",
                    ],
                )
                if not default_root and not mcp and owned == "hooks":
                    preview = subprocess.run(
                        # --json: the plan as data; the user who runs the printed command sees the summary.
                        ["bash", "-c", command + " --json"],
                        env=self.env,
                        cwd=self.project,
                        text=True,
                        capture_output=True,
                        timeout=60,
                    )
                    self.assertEqual(preview.returncode, 0, preview.stderr)
                    plan = json.loads(preview.stdout)
                    self.assertEqual(plan["mode"], "preview")
                    self.assertEqual(plan["root"], str(self.root))
                    self.assertEqual(plan["host_targets"]["claude"], str(root))
        mcp_path = root / "mcp.json"
        servers = json.loads(mcp_path.read_text()) if mcp else {"mcpServers": {}}
        servers["mcpServers"]["user"] = {"command": "user-mcp-command"}
        mcp_path.write_text(json.dumps(servers))
        self.replace(
            "claude",
            root,
            mcp_path,
            "user-mcp-command",
            "changed-user-mcp",
            denied=False,
        )
        if mcp:
            self.replace(
                "claude", root, mcp_path, '"true"', '"changed-owned-mcp"', denied=True
            )
        role = root / "agents/task-reviewer.md"
        self.decision(
            self.guard("claude", root, role, {"content": "overwritten"}), "claude", True
        )
        user_role = root / "agents/user-defined.md"
        user_role.write_text("User role\n")
        self.replace(
            "claude", root, user_role, "User role", "Changed user role", denied=False
        )
        registry = self.root / "hooks/registry.json"
        self.assertTrue(registry.exists())
        self.assertTrue(document["hooks"]["PreToolUse"])

    def test_claude_default_root_owned_and_user_fields(self) -> None:
        self.claude_ownership(default_root=True, mcp=True)

    def test_claude_custom_root_owned_and_user_fields(self) -> None:
        self.claude_ownership(default_root=False, mcp=True)

    def test_claude_default_root_unmanaged_mcp_and_role(self) -> None:
        self.claude_ownership(default_root=True, mcp=False)

    def test_claude_custom_root_unmanaged_mcp_and_role(self) -> None:
        self.claude_ownership(default_root=False, mcp=False)
        state = self.state()
        state["configuration"].pop("source_checkout", None)
        (self.root / ".install-state/current.json").write_text(json.dumps(state))
        root = self.home / "custom claude host"
        denial = self.guard(
            "claude", root, root / "agents/task-reviewer.md", {"content": "overwritten"}
        )
        self.decision(denial, "claude", True)
        reason = json.loads(denial.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        self.assertIn(
            "rerun agent-setup from your source checkout with --root-dir ", reason
        )
        self.assertEqual(
            shlex.split(reason.split("--root-dir ", 1)[1]),
            [str(self.root), "--blocking-hooks"],
        )

    def test_guidance_apply_preserves_blocking_and_uses_origin_logic(self) -> None:
        root = self.install("claude")
        settings = root / "settings.json"
        before = json.loads(settings.read_text())
        self.assertIs(self.state()["configuration"].get("blocking_hooks"), True)
        for older in (False, True):
            with self.subTest(older=older):
                if older:
                    state = self.state()
                    state["configuration"].pop("blocking_hooks")
                    (self.root / ".install-state/current.json").write_text(
                        json.dumps(state)
                    )
                changed = copy.deepcopy(before)
                changed["env"]["AGENT_GIT_HOOKS"] = "changed-fixture-value"
                denial = self.guard(
                    "claude", root, settings, {"content": json.dumps(changed)}
                )
                self.decision(denial, "claude", True)
                reason = json.loads(denial.stdout)["hookSpecificOutput"][
                    "permissionDecisionReason"
                ]
                command = reason.split("preview ", 1)[1].split("; review", 1)[0]
                self.assertEqual(
                    shlex.split(command),
                    [
                        str(SOURCE / "bin/agent-setup"),
                        "--root-dir",
                        str(self.root),
                        "--blocking-hooks",
                    ],
                )
                applied = subprocess.run(
                    ["bash", "-c", command + " --apply"],
                    env=self.env,
                    cwd=self.project,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(applied.returncode, 0, applied.stderr)
                after = json.loads(settings.read_text())
                self.assertEqual(after["hooks"], before["hooks"])
                for key in (
                    "AGENT_GIT_HOOKS",
                    "GIT_CONFIG_COUNT",
                    "GIT_CONFIG_KEY_0",
                    "GIT_CONFIG_VALUE_0",
                ):
                    self.assertEqual(after["env"][key], before["env"][key])

    def test_ambiguous_legacy_guidance_requires_explicit_previous_choice(self) -> None:
        root = self.install("claude")
        state = self.state()
        state["configuration"].pop("blocking_hooks", None)
        (self.root / ".install-state/current.json").write_text(json.dumps(state))
        registry = self.root / "hooks/registry.json"
        registry.write_text(registry.read_text() + "\n")
        denial = self.guard(
            "claude", root, root / "agents/task-reviewer.md", {"content": "overwritten"}
        )
        self.decision(denial, "claude", True)
        reason = json.loads(denial.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        self.assertIn("explicit previous blocking-hook choice", reason)
        self.assertNotIn("preview ", reason)

    def test_generated_plugin_guard_scopes_ownership(self) -> None:
        import plugin_portability_test as packages

        packages.SOURCE = SOURCE
        fixture = packages.PluginPortabilityTests
        fixture.setUpClass()
        try:
            plugin = fixture.source / "plugins/guard-rails"
            document = json.loads((plugin / "hooks/hooks.json").read_text())
            command = next(
                hook["command"]
                for groups in document["hooks"].values()
                for group in groups
                for hook in group["hooks"]
                if "edit-guard" in hook["command"]
            )
            env = dict(self.env, CLAUDE_PLUGIN_ROOT=str(plugin))
            targets = (
                self.home / ".claude/settings.json",
                self.home / ".claude/mcp.json",
                self.home / ".claude/agents/new-user-agent.md",
                self.home / ".codex/config.toml",
                self.home / ".cursor/mcp.json",
            )
            for target in (*targets, plugin / "kit/roles.toml"):
                target.parent.mkdir(parents=True, exist_ok=True)
                if target in targets and target.name != "new-user-agent.md":
                    target.write_text(
                        "{}" if target.suffix == ".json" else 'model="user"\n'
                    )
                with self.subTest(target=target):
                    result = subprocess.run(
                        ["bash", "-c", command],
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=30,
                        input=json.dumps(
                            {
                                "tool_name": "Write",
                                "cwd": str(self.project),
                                "tool_input": {
                                    "file_path": str(target),
                                    "content": "user change",
                                },
                            }
                        ),
                    )
                    self.decision(result, "claude", target not in targets)
        finally:
            fixture.tearDownClass()

    def test_codex_installed_ownership_and_user_fields(self) -> None:
        root = self.install("codex", mcp=True)
        config = root / "config.toml"
        config.write_text('model = "user-model"\n' + config.read_text())
        self.replace(
            "codex",
            root,
            config,
            'model = "user-model"',
            'model = "changed-user-model"',
            denied=False,
        )
        self.replace(
            "codex",
            root,
            config,
            '"AI_AGENT" = "codex"',
            '"AI_AGENT" = "changed"',
            denied=True,
        )
        self.replace(
            "codex",
            root,
            config,
            '"command" = "true"',
            '"command" = "changed"',
            denied=True,
        )
        hooks = root / "hooks.json"
        previous = json.loads(hooks.read_text())
        previous["userPreference"] = "user-value"
        previous["hooks"]["PreToolUse"].append(
            {"hooks": [{"type": "command", "command": "user-owned-hook-command"}]}
        )
        hooks.write_text(json.dumps(previous))
        self.replace(
            "codex", root, hooks, "user-value", "changed-user-value", denied=False
        )
        self.replace(
            "codex",
            root,
            hooks,
            "user-owned-hook-command",
            "changed-user-owned-hook-command",
            denied=False,
        )
        changed = copy.deepcopy(previous)
        changed["hooks"]["PreToolUse"] = []
        self.decision(
            self.guard("codex", root, hooks, {"content": json.dumps(changed)}),
            "codex",
            True,
        )
        # native_agents = false: the kit owns no agents/*.toml (hosts.test.py covers the render).
        self.assertFalse((root / "agents/cross-reviewer.toml").exists())
        user_role = root / "agents/user-defined.toml"
        user_role.parent.mkdir(exist_ok=True)
        user_role.write_text('name="user-role"\n')
        self.replace(
            "codex", root, user_role, "user-role", "changed-user-role", denied=False
        )
        self.assertTrue(
            any(
                row["target"] == str(config) and row["kind"] == "toml"
                for row in self.state()["managed"].values()
            )
        )
        self.assertFalse(
            list((self.root / "state/rendered").glob("codex-hooks.*.json"))
        )

    def test_codex_user_patch_is_allowed_and_owned_patch_forms_are_denied(self) -> None:
        root = self.install("codex")
        config = root / "config.toml"
        config.write_text('model = "user-model"\n' + config.read_text())
        self.patch(
            "codex",
            root,
            config,
            f'*** Begin Patch\n*** Update File: {config}\n@@\n-model = "user-model"\n+model = "changed-user-model"\n*** End Patch',
            denied=False,
        )
        patches = (
            f'*** Begin Patch\n*** Update File: {config}\n@@ # BEGIN agent-kit setup\n-"AI_AGENT" = "codex"\n+"AI_AGENT" = "changed"\n*** End Patch',
            f'*** Begin Patch\n*** Update File: {config}\n@@\n-"AI_AGENT" = "codex"\n+"AI_AGENT" = "changed"\n*** Update File: {config}\n@@\n-"AGENT_HOST" = "codex"\n+"AGENT_HOST" = "changed"\n*** End Patch',
            f"*** Begin Patch\n*** Delete File: {config}\n*** End Patch",
        )
        for text in patches:
            with self.subTest(patch=text):
                self.patch("codex", root, config, text, denied=True)
        ordinary = self.project / "ordinary.py"
        ordinary.write_text("X\nY\n")
        self.patch(
            "codex",
            root,
            ordinary,
            f"*** Begin Patch\n*** Update File: {ordinary}\n@@\n-X\n+X2\n@@\n-Y\n+Y2\n*** End Patch",
            denied=False,
        )

    def test_installed_toml_owned_tables_extend_beyond_marker_comments(self) -> None:
        root = self.install("codex", mcp=True)
        config = root / "config.toml"
        self.replace(
            "codex",
            root,
            config,
            "# END agent-kit setup",
            '# END agent-kit setup\nenv = { NODE_OPTIONS="fixture-injection" }',
            denied=True,
        )
        original = config.read_text()
        config.write_text(original + '\n[user_table]\nvalue="user-value"\n')
        self.replace(
            "codex",
            root,
            config,
            'value="user-value"',
            'value="changed-user-value"',
            denied=False,
        )
        self.assertEqual(
            tomllib.loads(config.read_text())["mcp_servers"]["fixture"]["command"],
            "true",
        )

    def test_cursor_project_rules_are_owned_outside_global_hook_root(self) -> None:
        root = self.install("cursor", mcp=True)
        rule = self.project / ".cursor/rules/agent-kit.mdc"
        self.assertFalse(rule.is_relative_to(root))
        self.assertTrue(
            any(
                row["target"] == str(rule) and row["kind"] == "file"
                for row in self.state()["managed"].values()
            )
        )
        self.replace(
            "cursor", root, rule, "alwaysApply: true", "alwaysApply: false", denied=True
        )
        user_rule = rule.parent / "user-defined.mdc"
        user_rule.write_text("User note remains.\n")
        self.replace(
            "cursor",
            root,
            user_rule,
            "User note remains.",
            "Changed user note.",
            denied=False,
        )
        mcp = root / "mcp.json"
        document = json.loads(mcp.read_text())
        document["mcpServers"]["user"] = {"command": "user-command"}
        mcp.write_text(json.dumps(document))
        self.replace(
            "cursor", root, mcp, "user-command", "changed-user-command", denied=False
        )
        changed = copy.deepcopy(document)
        changed["mcpServers"]["fixture"]["command"] = "changed-owned-command"
        self.decision(
            self.guard("cursor", root, mcp, {"content": json.dumps(changed)}),
            "cursor",
            True,
        )

    def test_macos_case_aliases_use_installed_ownership(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("Case-alias check requires the macOS fixture filesystem")
        root = self.install("codex")
        for target in (
            root / "CONFIG.TOML",
            root / "Hooks.json",
        ):
            with self.subTest(target=target):
                self.assertTrue(target.exists())
                self.decision(
                    self.guard("codex", root, target, {"content": "{}"}), "codex", True
                )
        root = self.install("cursor")
        rule = self.project / ".cursor/rules/AGENT-KIT.MDC"
        self.assertTrue(rule.exists())
        self.decision(
            self.guard("cursor", root, rule, {"content": "overwritten"}), "cursor", True
        )

    def test_missing_installed_guard_library_fails_closed(self) -> None:
        root = self.install("codex")
        (self.root / "hooks/lib/native_edits.py").unlink()
        target = root / "config.toml"
        result = self.guard("codex", root, target, {"content": "overwritten"})
        self.assertEqual(result.returncode, 2, result.stderr)
        direct = subprocess.run(
            [str(self.root / "hooks/edit-guard")],
            input=json.dumps({"tool_input": {"file_path": str(target)}}),
            env=dict(self.env, AGENT_KIT_DIR=str(self.root)),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(direct.returncode, 2, direct.stderr)
        self.assertEqual(
            json.loads(direct.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InstalledGuardTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(
        json.dumps(
            {
                "cases": suite.countTestCases(),
                "successful": result.wasSuccessful(),
                "failures": len(result.failures),
                "errors": len(result.errors),
            }
        )
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
