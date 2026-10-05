#!/usr/bin/env python3
"""Focused round-two host probes against disposable homes and synthetic catalogs."""

from __future__ import annotations
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
import hashlib
import shutil

SOURCE = Path(os.environ.get("AGENT_KIT_SOURCE", Path(__file__).resolve().parents[2]))
loader = importlib.machinery.SourceFileLoader(
    "host_review_base", str(SOURCE / "hooks/tests/hosts-review.test.py")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
base = importlib.util.module_from_spec(spec)
loader.exec_module(base)


class RoundTwo(base.ReviewTests):
    def test_CX1_sentry_capability_environment(self):
        module = base.load(self.kit / "bin/lib/sentry.py", f"sentry_{id(self)}")
        env = {
            "SENTRY_HOST": "fixture.invalid",
            "MCP_SKILLS": "inspect",
            "MCP_DISABLE_SKILLS": "triage,project-management",
            "SENTRY_ACCESS_TOKEN": "fixture-secret",
        }
        actual = module.safe_sentry(
            {"command": "npx", "args": ["-y", "@sentry/mcp-server@0.37.0"], "env": env},
            self.kit / "bin/sentry-mcp",
        )
        self.assertEqual(
            actual["env"],
            {key: value for key, value in env.items() if key != "SENTRY_ACCESS_TOKEN"},
        )
        self.assertNotIn("fixture-secret", json.dumps(actual))

    def test_CX2_patch_header_whitespace_and_line_endings(self):
        self.assertEqual(self.render().returncode, 0)
        target = self.root / ".codex/hooks.json"
        for operation in ("Add", "Update", "Delete"):
            for ending in (" \t", "\r", ""):
                with self.subTest(operation=operation, ending=repr(ending)):
                    body = (
                        "+fixture\n"
                        if operation == "Add"
                        else "@@\n-a\n+b\n"
                        if operation == "Update"
                        else ""
                    )
                    text = f"*** Begin Patch\n*** {operation} File: {target}{ending}\n{body}*** End Patch"
                    output = self.hook(
                        "edit-guard",
                        {
                            "hook_event_name": "PreToolUse",
                            "session_id": "native",
                            "cwd": str(self.root),
                            "tool_name": "apply_patch",
                            "tool_input": {"command": text},
                        },
                    )
                    self.assertEqual(
                        output.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
                    )
        for ending in (" \t", "\r", ""):
            with self.subTest(move=repr(ending)):
                text = f"*** Begin Patch\n*** Update File: source.txt\n*** Move to: {target}{ending}\n@@\n-a\n+b\n*** End Patch"
                output = self.hook(
                    "edit-guard",
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "native",
                        "cwd": str(self.root),
                        "tool_name": "apply_patch",
                        "tool_input": {"command": text},
                    },
                )
                self.assertEqual(
                    output.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
                )

    def test_CX3_generated_hooks_retain_custom_root(self):
        for host in ("cursor", "codex"):
            with self.subTest(host=host):
                root = self.root / "project" / f".{host}"
                self.assertEqual(
                    self.render(
                        host, env={**self.env, "AGENT_KIT_HOST_ROOT": str(root)}
                    ).returncode,
                    0,
                )
                hooks = json.loads((root / "hooks.json").read_text())["hooks"]
                groups = hooks["preToolUse" if host == "cursor" else "PreToolUse"]
                command = next(
                    hook["command"]
                    for group in groups
                    for hook in group.get("hooks", [group])
                    if "edit-guard" in hook["command"]
                )
                target = root / ("rules/agent-kit.mdc" if host == "cursor" else "config.toml")
                payload = {
                    "hook_event_name": "preToolUse" if host == "cursor" else "PreToolUse",
                    "session_id": "native",
                    "cwd": str(self.root),
                    "tool_name": "WriteFile" if host == "cursor" else "apply_patch",
                    "tool_input": {"path": str(target)}
                    if host == "cursor"
                    else {
                        "command": f"*** Begin Patch\n*** Update File: {target}\n@@\n-a\n+b\n*** End Patch"
                    },
                }
                env = dict(self.env)
                env.pop("AGENT_KIT_HOST_ROOT", None)
                result = subprocess.run(
                    ["bash", "-c", command],
                    input=json.dumps(payload),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                out = json.loads(result.stdout)
                self.assertEqual(
                    out.get("permission")
                    if host == "cursor"
                    else out.get("hookSpecificOutput", {}).get("permissionDecision"),
                    "deny",
                )

    def test_CX4_claude_rules_recover_failed_ownership_write(self):
        if sys.platform != "darwin":
            self.skipTest("Real immutable-file fault requires macOS chflags")
        path = self.root / ".claude/CLAUDE.md"
        path.parent.mkdir()
        path.write_text("User rule stays.\n")
        real = str(path.parent.resolve() / path.name).casefold()
        state = (
            self.kit
            / "state/rendered"
            / f"claude-rules.{hashlib.sha256(real.encode()).hexdigest()[:12]}.json"
        )
        state.parent.mkdir(parents=True)
        state.write_text('{"imports":""}')
        subprocess.run(["chflags", "uchg", str(state)], check=True)
        try:
            self.assertNotEqual(self.render("claude").returncode, 0)
            self.assertIn("BEGIN agent-kit imports", path.read_text())
        finally:
            subprocess.run(["chflags", "nouchg", str(state)], check=True)
        self.assertEqual(self.render("claude").returncode, 0)
        self.assertTrue(path.read_text().startswith("User rule stays.\n"))
        path.write_text(
            path.read_text().replace("rules/hosts/claude.md", "rules/hosts/user-edited.md")
        )
        self.assertNotEqual(self.render("claude").returncode, 0)

    def test_B1_native_trust_state_is_preserved(self):
        root = self.root / ".codex"
        root.mkdir()
        config = root / "config.toml"
        config.write_text('[hooks.state."fixture"]\ntrusted_hash="fixture-hash"\n')
        self.assertEqual(self.render().returncode, 0)
        self.assertIn('trusted_hash="fixture-hash"', config.read_text())
        result = subprocess.run(
            [str(self.kit / "bin/agent-kit"), "doctor", "--host", "codex"],
            env={**self.env, "AGENT_KIT_HOST_ROOT": str(root)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_B2_advisory_failures_and_closed_guards(self):
        path = self.kit / "hooks/fixture-exit"
        path.symlink_to("/usr/bin/false")
        for host, event, expected in (
            ("cursor", "beforeSubmitPrompt", 1),
            ("codex", "Stop", 1),
            ("codex", "PreToolUse", 2),
        ):
            for hook in ("fixture-exit", "missing-fixture-hook"):
                with self.subTest(host=host, event=event, hook=hook):
                    result = subprocess.run(
                        [str(self.kit / "hooks/host-adapter"), host, hook],
                        input=json.dumps(
                            {
                                "hook_event_name": event,
                                "session_id": "fixture",
                                "cwd": str(self.root),
                            }
                        ),
                        env=self.env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, expected)

    def test_B4_user_native_fields_and_owned_content(self):
        self.assertEqual(self.render().returncode, 0)
        root = self.root / ".codex"
        config = root / "config.toml"
        config.write_text('model="fixture-model"\n' + config.read_text())
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "native",
            "cwd": str(self.root),
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(config),
                "old_string": 'model="fixture-model"',
                "new_string": 'model="other-model"',
            },
        }
        out = self.hook("edit-guard", payload)
        self.assertNotEqual(out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        agent = root / "agents/my-own.toml"
        agent.write_text('name="mine"\n')
        payload["tool_input"] = {
            "file_path": str(agent),
            "old_string": "mine",
            "new_string": "mine2",
        }
        out = self.hook("edit-guard", payload)
        self.assertNotEqual(out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        payload["tool_input"] = {
            "file_path": str(config),
            "old_string": "# BEGIN agent-kit managed",
            "new_string": "# user-deleted managed marker",
        }
        out = self.hook("edit-guard", payload)
        self.assertEqual(out.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_public_installer_ownership_scope(self):
        root = self.root / "project/.cursor"
        rule = root / "rules/agent-kit.mdc"
        rule.parent.mkdir(parents=True)
        owned = "<!-- BEGIN agent-kit setup -->\nFixture rules.\n<!-- END agent-kit setup -->"
        rule.write_text("User header.\n" + owned + "\n")
        hooks = self.root / ".codex/hooks.json"
        hooks.parent.mkdir()
        group = {"hooks": [{"type": "command", "command": "fixture-hook"}]}
        hooks.write_text(json.dumps({"hooks": {"PreToolUse": [group]}, "user": 1}))
        state = self.kit / ".install-state/current.json"
        state.parent.mkdir()
        state.write_text(
            json.dumps(
                {
                    "managed": {
                        f"text:{rule}": {"kind": "text", "target": str(rule), "owned": owned},
                        f"hooks:{hooks}": {
                            "kind": "hooks",
                            "target": str(hooks),
                            "owned": {"PreToolUse": [group]},
                        },
                    }
                }
            )
        )
        for target, before, after, denied in (
            (rule, "User header.", "Changed user header.", False),
            (rule, "Fixture rules.", "Changed kit rules.", True),
            (hooks, '"user": 1', '"user": 2', False),
            (hooks, "fixture-hook", "removed-kit-hook", True),
        ):
            with self.subTest(target=target, denied=denied):
                result = self.hook(
                    "edit-guard",
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "native",
                        "cwd": str(self.root),
                        "tool_name": "Edit",
                        "tool_input": {
                            "file_path": str(target),
                            "old_string": before,
                            "new_string": after,
                        },
                    },
                )
                self.assertEqual(
                    result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", denied
                )

    def test_B6_prepended_claude_user_text_no_duplicate_imports(self):
        (self.root / ".claude").mkdir()
        first = self.render("claude")
        self.assertEqual(first.returncode, 0, first.stderr)
        path = self.root / ".claude/CLAUDE.md"
        path.write_text("User prepend.\n" + path.read_text())
        self.assertEqual(self.render("claude").returncode, 0)
        self.assertEqual(path.read_text().count(f"@{self.kit}/rules/AGENTS.md"), 1)
        self.assertTrue(path.read_text().startswith("User prepend.\n"))

    def test_B7_unsupported_mcp_fields_and_transports_refuse(self):
        for server in (
            {"command": "true", "args": [7]},
            {"command": "true", "env": {"FIELD": 7}},
            {"url": "https://fixture.invalid", "headers": {"safe": "value"}},
            {"command": "node", "args": ["/fixture/server.js"]},
            {"command": "npx", "args": ["-y", "@sentry/mcp-server-extra"]},
            {"command": "npx", "args": ["-y", "other-server"]},
            {
                "command": "npx",
                "args": ["-y", "@sentry/mcp-server@0.37.0"],
                "env": {"OPENAI_API_KEY": "fixture-secret"},
            },
        ):
            with self.subTest(server=server):
                name = "sentry" if server.get("command") in ("node", "npx") else "demo"
                self.catalog = {"mcpServers": {name: server}}
                self.catalog_write()
                result = self.render()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("fixture-secret", result.stdout + result.stderr)

    def test_B8_claude_provider_receives_clean_environment(self):
        shutil.copytree(SOURCE / "skills/review-rubric", self.kit / "skills/review-rubric")
        repo = self.repo("provider-repo")
        output = self.root / "provider.json"
        probe = self.root / "provider-probe"
        probe.write_text(
            "#!/usr/bin/env python3\nimport os,json\nkeys=('AGENT_HOST','AGENT_SESSION_ID','CODEX_THREAD_ID','AI_AGENT')\nprint(json.dumps({'structured_output':{'verdict':'approve','summary':json.dumps({key:os.environ.get(key,'') for key in keys}),'findings':[],'next_steps':[]}}))\n"
        )
        probe.chmod(0o700)
        result = subprocess.run(
            [
                str(self.kit / "bin/agent-run"),
                "bug-reviewer",
                str(repo),
                "--out",
                str(output),
                "--timeout",
                "10",
            ],
            env={
                **self.env,
                "AGENT_HOST": "codex",
                "AI_AGENT": "codex",
                "CODEX_THREAD_ID": "parent-codex",
                "AGENT_SESSION_ID": "foreign-parent",
                "CLAUDE_BIN": str(probe),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = json.loads(json.loads(output.read_text())["summary"])
        self.assertEqual(
            actual,
            {
                "AGENT_HOST": "claude",
                "AGENT_SESSION_ID": "",
                "CODEX_THREAD_ID": "",
                "AI_AGENT": "claude",
            },
        )


if __name__ == "__main__":
    suite = unittest.TestSuite(
        RoundTwo(name) for name in RoundTwo.__dict__ if name.startswith("test_")
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
