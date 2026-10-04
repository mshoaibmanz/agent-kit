#!/usr/bin/env python3
"""Real native render and guard checks for user-owned shell environment keys."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
import tomllib
from pathlib import Path

SOURCE = Path(os.environ.get("AGENT_KIT_SOURCE", Path(__file__).resolve().parents[2]))
loader = importlib.machinery.SourceFileLoader(
    "shell_host_base", str(SOURCE / "hooks/tests/hosts.test.py")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
base = importlib.util.module_from_spec(spec)
loader.exec_module(base)


class ShellEnvironmentTests(unittest.TestCase):
    setUp = base.HostTests.setUp
    tearDown = base.HostTests.tearDown
    run_kit = base.HostTests.run_kit
    hook = base.HostTests.hook

    def config(self, text: str) -> Path:
        root = self.root / ".codex"
        root.mkdir(exist_ok=True)
        path = root / "config.toml"
        path.write_text(text)
        return path

    def render(self):
        return self.run_kit("codex", "--components", "hooks")

    def guard(self, path: Path, old: str, new: str) -> dict:
        removed = "\n".join("-" + line for line in old.splitlines())
        added = "\n".join("+" + line for line in new.splitlines())
        command = f"*** Begin Patch\n*** Update File: {path}\n@@\n{removed}\n{added}\n*** End Patch"
        return self.hook(
            "codex",
            "edit-guard",
            {
                "hook_event_name": "PreToolUse",
                "turn_id": "fixture-turn",
                "session_id": "fixture-native",
                "cwd": str(self.root),
                "tool_name": "apply_patch",
                "tool_input": {"command": command},
            },
        )

    def test_table_user_keys_survive_and_rerender_is_idempotent(self) -> None:
        prefix = 'model = "fixture"\n[shell_environment_policy.set]\nUSER_SETTING = "keep" # user comment\n'
        path = self.config(prefix)
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = path.read_text()
        self.assertIn('USER_SETTING = "keep" # user comment', text)
        self.assertEqual(
            tomllib.loads(text)["shell_environment_policy"]["set"]["USER_SETTING"],
            "keep",
        )
        self.assertEqual(
            tomllib.loads(text)["shell_environment_policy"]["set"]["AGENT_HOST"],
            "codex",
        )
        self.assertEqual(self.render().returncode, 0)
        self.assertEqual(path.read_text(), text)

    def test_inline_user_keys_survive(self) -> None:
        path = self.config(
            '[shell_environment_policy]\nset = { USER_SETTING = "keep" }\ninherit = "all"\n'
        )
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = tomllib.loads(path.read_text())["shell_environment_policy"]
        self.assertEqual(policy["inherit"], "all")
        self.assertEqual(policy["set"]["USER_SETTING"], "keep")
        self.assertEqual(policy["set"]["AGENT_HOST"], "codex")
        text = path.read_text()
        self.assertEqual(self.render().returncode, 0)
        self.assertEqual(text, path.read_text())

    def test_new_user_key_outside_marker_allowed_by_guard_and_rerender(self) -> None:
        path = self.config('model = "fixture"\n')
        self.assertEqual(self.render().returncode, 0)
        text = path.read_text()
        after = text.replace(
            "# END agent-kit shell",
            '# END agent-kit shell\nNEW_USER_SETTING = "keep-new"',
        )
        output = self.guard(
            path,
            "# END agent-kit shell",
            '# END agent-kit shell\nNEW_USER_SETTING = "keep-new"',
        )
        self.assertNotEqual(
            output.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
        )
        path.write_text(after)
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            tomllib.loads(path.read_text())["shell_environment_policy"]["set"][
                "NEW_USER_SETTING"
            ],
            "keep-new",
        )

    def test_unowned_kit_key_conflict_refuses_all_writes(self) -> None:
        text = '[shell_environment_policy.set]\nAGENT_HOST = "user-choice"\nUSER_SETTING = "keep"\n'
        path = self.config(text)
        result = self.render()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), text)
        self.assertFalse((path.parent / "hooks.json").exists())
        self.assertFalse((self.kit / "state/rendered").exists())

    def test_managed_kit_key_edit_denied_and_renderer_refuses(self) -> None:
        path = self.config('model = "fixture"\n')
        self.assertEqual(self.render().returncode, 0)
        text = path.read_text()
        after = text.replace('AGENT_HOST = "codex"', 'AGENT_HOST = "user-choice"')
        output = self.guard(path, 'AGENT_HOST = "codex"', 'AGENT_HOST = "user-choice"')
        self.assertEqual(
            output.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
        )
        path.write_text(after)
        before_hooks = (path.parent / "hooks.json").read_bytes()
        result = self.render()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), after)
        self.assertEqual((path.parent / "hooks.json").read_bytes(), before_hooks)

    def test_user_key_remains_outside_owned_marker(self) -> None:
        path = self.config('[shell_environment_policy.set]\nUSER_SETTING = "keep"\n')
        self.assertEqual(self.render().returncode, 0)
        text = path.read_text()
        owned = text.split("# BEGIN agent-kit shell", 1)[1].split(
            "# END agent-kit shell", 1
        )[0]
        self.assertNotIn("USER_SETTING", owned)
        after = text.replace('USER_SETTING = "keep"', 'USER_SETTING = "updated"')
        output = self.guard(path, 'USER_SETTING = "keep"', 'USER_SETTING = "updated"')
        self.assertNotEqual(
            output.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
        )
        path.write_text(after)
        self.assertEqual(self.render().returncode, 0)
        self.assertEqual(
            tomllib.loads(path.read_text())["shell_environment_policy"]["set"][
                "USER_SETTING"
            ],
            "updated",
        )


if __name__ == "__main__":
    unittest.main()
