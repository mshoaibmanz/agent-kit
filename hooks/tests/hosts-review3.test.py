#!/usr/bin/env python3
"""Last review regressions through real native patches and disposable hook processes."""

from __future__ import annotations
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

SOURCE = Path(os.environ.get("AGENT_KIT_SOURCE", Path(__file__).resolve().parents[2]))
loader = importlib.machinery.SourceFileLoader(
    "host_review_base", str(SOURCE / "hooks/tests/hosts-review.test.py")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
base = importlib.util.module_from_spec(spec)
loader.exec_module(base)
BEGIN, END = "# BEGIN agent-kit managed", "# END agent-kit managed"


def native_codex() -> Path:
    """The native Codex binary behind the npm `codex` on PATH (CODEX_NATIVE_BIN overrides)."""
    if os.environ.get("CODEX_NATIVE_BIN"):
        return Path(os.environ["CODEX_NATIVE_BIN"])
    found = shutil.which("codex")
    if not found:
        return Path("/nonexistent/codex")
    package = Path(found).resolve().parents[1]
    return next(iter(sorted((package / "node_modules/@openai").glob("codex-*/vendor/*/bin/codex"))), Path("/nonexistent/codex"))


NATIVE = native_codex()


class RoundThree(base.ReviewTests):
    def patch(self, text):
        return self.hook(
            "edit-guard",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "native",
                "cwd": str(self.root),
                "tool_name": "apply_patch",
                "tool_input": {"command": text},
            },
        )

    def test_B1_CX1_native_patch_projection(self):
        self.assertEqual(self.render().returncode, 0)
        target = self.root / ".codex/AGENTS.md"
        original = "X\nY\n<!-- BEGIN agent-kit managed -->\nX   \n<!-- END agent-kit managed -->\n"
        cases = (
            f"*** Begin Patch\n*** Update File: {target}\n@@\n-Y\n+Y2\n@@\n-X\n+EVIL\n*** End Patch",
            f"*** Begin Patch\n*** Update File: {target}\n@@ <!-- BEGIN agent-kit managed -->\n-X\n+EVIL\n*** End Patch",
            f"*** Begin Patch\n*** Update File: {target}\n@@\n-Y\n+Y2\n*** Add File: {self.root}/unrelated.txt\n+fixture\n   *** Delete File: {target}\n*** End Patch",
            f"*** Begin Patch\n*** Update File: {target}\n@@\n-X\n+EVIL\n*** End Patch",
        )
        if not NATIVE.exists():
            self.skipTest("Installed native Codex apply_patch binary unavailable")
        native = self.root / "apply_patch"
        native.symlink_to(NATIVE)
        for index, patch in enumerate(cases):
            with self.subTest(patch=patch):
                target.write_text(
                    original if index < 3 else original.replace("X\nY", "A\u2028X\nY")
                )
                result = self.patch(patch)
                applied = subprocess.run(
                    [str(native), patch],
                    cwd=self.root,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertTrue(not target.exists() or "EVIL" in target.read_text())
                self.assertEqual(
                    result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
                )

    def test_B2_CX2_case_aliases(self):
        if sys.platform != "darwin":
            self.skipTest("Requires the user's case-insensitive filesystem")
        for host in ("codex", "cursor"):
            self.assertEqual(self.render(host).returncode, 0)
        cases = (
            self.root / ".codex/CONFIG.TOML",
            self.root / ".codex/Hooks.json",
            self.root / ".CoDeX/AgEnTs/CROSS-REVIEWER.TOML",
            self.root / ".cursor/Mcp.json",
            self.root / ".cursor/rules/AGENT-KIT.MDC",
        )
        for target in cases:
            with self.subTest(target=target):
                self.assertTrue(target.exists())
                result = self.hook(
                    "edit-guard",
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "native",
                        "cwd": str(self.root),
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(target), "content": "{}"},
                    },
                )
                self.assertEqual(
                    result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
                )

    def test_cursor_mcp_reuses_live_case_spelling(self):
        self.catalog = {"mcpServers": {"demo": {"command": "true", "args": []}}}
        self.catalog_write()
        mcp = self.root / ".cursor/mcp.json"
        mcp.parent.mkdir()
        mcp.write_text(json.dumps({"mcpServers": {"Demo": {"command": "true", "args": []}}}))
        result = self.render("cursor", ("--components", "mcp"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(json.loads(mcp.read_text())["mcpServers"]), ["Demo"])

    def test_codex_mcp_render_keeps_hook_trust(self):
        config = self.root / ".codex/config.toml"
        config.parent.mkdir()
        trust = (
            '[hooks.state."/x/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:fixture"\n\n'
            '[projects."/p"]\ntrust_level = "trusted"\n'
        )
        config.write_text(
            f'model = "m"\n\n{BEGIN}\n[mcp_servers."old"]\ncommand = "true"\n\n'
            f"{trust}\n{END}\n"
        )
        for _ in range(2):
            result = self.render("codex", ("--components", "mcp"))
            self.assertEqual(result.returncode, 0, result.stderr)
        text = config.read_text()
        parsed = base.tomllib.loads(text)
        self.assertEqual(parsed["hooks"]["state"]["/x/hooks.json:stop:0:0"]["trusted_hash"], "sha256:fixture")
        self.assertEqual(parsed["projects"]["/p"]["trust_level"], "trusted", "B-8: any host table survives")
        self.assertEqual(set(parsed["mcp_servers"]), {"demo"})
        self.assertEqual(text.count(trust), 1)
        self.assertGreater(text.index(trust), text.index(END))

    def test_codex_sandbox_writes_work_root_and_gcloud_only_on_opt_in(self):
        work, gcloud = self.root / "work", self.root / "gcloud"
        gcloud.mkdir()
        overlay = self.root / "kit.env"
        env = {
            **self.env,
            "AGENT_KIT_HOST_ROOT": str(self.root / ".codex"),
            "CLAUDE_OUT_ROOT": str(work),
            "CLOUDSDK_CONFIG": str(gcloud),
            "KIT_ENV": str(overlay),
        }
        for setting, roots in (("", [str(work)]), ("CODEX_SANDBOX_GCLOUD=1\n", [str(work), str(gcloud)])):
            overlay.write_text(setting)
            result = self.render("codex", ("--components", "mcp"), env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = base.tomllib.loads((self.root / ".codex/config.toml").read_text())
            self.assertEqual(parsed["sandbox_workspace_write"]["writable_roots"], roots, setting)

    def test_codex_sandbox_keeps_the_users_table(self):
        config = self.root / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('[sandbox_workspace_write]\nwritable_roots = ["/x"]\n')
        result = self.render("codex", ("--components", "mcp"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("your [sandbox_workspace_write] is kept", result.stderr)
        parsed = base.tomllib.loads(config.read_text())
        self.assertEqual(parsed["sandbox_workspace_write"], {"writable_roots": ["/x"]})

    def test_B3_toml_owned_tables_extend_beyond_markers(self):
        self.assertEqual(self.render().returncode, 0)
        target = self.root / ".codex/config.toml"
        for end, addition, denied in (
            ("# END agent-kit managed", 'env = { NODE_OPTIONS = "fixture-injection" }', True),
            ("# END agent-kit shell", 'EXTRA_DISPATCHER = "fixture-user-value"', False),
        ):
            with self.subTest(end=end):
                result = self.hook(
                    "edit-guard",
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "native",
                        "cwd": str(self.root),
                        "tool_name": "Edit",
                        "tool_input": {
                            "file_path": str(target),
                            "old_string": end,
                            "new_string": end + "\n" + addition,
                        },
                    },
                )
                self.assertEqual(
                    result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", denied
                )
        before = target.read_text()
        target.write_text(
            before.replace(
                "# END agent-kit managed",
                '# END agent-kit managed\nenv = { NODE_OPTIONS = "fixture-injection" }',
            )
        )
        unchanged = target.read_text()
        self.assertNotEqual(self.render().returncode, 0)
        self.assertEqual(target.read_text(), unchanged)
        target.write_text(before + '\n[user_fixture]\nvalue = "editable"\n')
        result = self.hook(
            "edit-guard",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "native",
                "cwd": str(self.root),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(target),
                    "old_string": 'value = "editable"',
                    "new_string": 'value = "changed"',
                },
            },
        )
        self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_B3_public_toml_owned_tables(self):
        target = self.root / ".codex/config.toml"
        target.parent.mkdir()
        owned = '# BEGIN agent-kit setup\n[mcp_servers.demo]\ncommand="true"\n# END agent-kit setup'
        target.write_text(owned + "\n")
        state = self.kit / ".install-state/current.json"
        state.parent.mkdir()
        state.write_text(
            json.dumps(
                {
                    "managed": {
                        f"toml:{target}": {"kind": "toml", "target": str(target), "owned": owned}
                    }
                }
            )
        )
        result = self.hook(
            "edit-guard",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "native",
                "cwd": str(self.root),
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(target),
                    "old_string": "# END agent-kit setup",
                    "new_string": '# END agent-kit setup\nenv={ NODE_OPTIONS="fixture-injection" }',
                },
            },
        )
        self.assertEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_B4_missing_library_fails_closed(self):
        (self.kit / "hooks/lib/native_edits.py").unlink()
        result = subprocess.run(
            [str(self.kit / "hooks/edit-guard")],
            input=json.dumps(
                {"tool_input": {"file_path": str(self.root / ".claude/settings.json")}}
            ),
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout).get("hookSpecificOutput", {}).get("permissionDecision"),
            "deny",
        )

    def test_claude_selected_components_do_not_read_mcp(self):
        root = self.root / ".claude"
        root.mkdir()
        mcp = root / "mcp.json"
        untouched = "opaque fixture, deliberately not parsed"
        mcp.write_text(untouched)
        (self.kit / "mcp/servers.json").unlink()
        components = ("--components", "rules", "hooks", "roles")
        for extra in (components + ("--dry-run",), components):
            result = self.render("claude", extra=extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(mcp.read_text(), untouched)
        result = subprocess.run(
            [str(self.kit / "bin/agent-kit"), "doctor", "--host", "claude", *components],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(mcp.read_text(), untouched)
        self.assertNotEqual(self.render("claude").returncode, 0)

    def test_ordinary_repository_multihunk_edits_remain_allowed(self):
        self.assertEqual(self.render().returncode, 0)
        path = self.root / "ordinary.py"
        path.write_text("X\nY\n")
        result = self.patch(
            f"*** Begin Patch\n*** Update File: {path}\n@@\n-X\n+X2\n@@\n-Y\n+Y2\n*** End Patch"
        )
        self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_claude_partial_settings_preserve_other_components(self):
        (self.root / ".claude").mkdir()
        base_path = self.kit / "hosts/claude/settings.base.json"
        base_path.write_text(json.dumps({"env": {"COMPONENT_FIXTURE": "original"}}))
        self.assertEqual(self.render("claude").returncode, 0)
        settings = self.root / ".claude/settings.json"
        previous = json.loads(settings.read_text())
        (self.kit / "mcp/servers.json").unlink()
        base_path.write_text(json.dumps({"env": {"COMPONENT_FIXTURE": "changed"}}))
        for component in ("rules", "roles"):
            result = self.render("claude", extra=("--components", component))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(settings.read_text()), previous)
        result = self.render("claude", extra=("--components", "hooks"))
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(settings.read_text())
        self.assertEqual(actual["env"]["COMPONENT_FIXTURE"], "changed")
        for key in ("model", "effortLevel", "modelSettings"):
            self.assertEqual(actual.get(key), previous.get(key))

    def test_B5_real_single_batch_delivery(self):
        hook = self.kit / "hooks/edit-guard"
        original = self.kit / "hooks/edit-guard-real"
        hook.rename(original)
        count = self.root / "invocations.jsonl"
        hook.write_text(
            "#!/usr/bin/env python3\nimport json,sys,subprocess\nfrom pathlib import Path\ndata=sys.stdin.read()\nwith Path("
            + repr(str(count))
            + ').open("a") as stream:stream.write(json.dumps(len(json.loads(data)["tool_input"].get("file_paths",[])))+"\\n")\nresult=subprocess.run(['
            + repr(str(original))
            + "],input=data,text=True,capture_output=True)\nsys.stdout.write(result.stdout)\nsys.stderr.write(result.stderr)\nraise SystemExit(result.returncode)\n"
        )
        hook.chmod(0o700)
        patch = (
            "*** Begin Patch\n"
            + "".join(f"*** Update File: f{i}.py\n@@\n-a\n+b\n" for i in range(200))
            + "*** End Patch"
        )
        self.patch(patch)
        self.assertEqual(count.read_text().splitlines(), ["200"])


if __name__ == "__main__":
    suite = unittest.TestSuite(
        RoundThree(name) for name in RoundThree.__dict__ if name.startswith("test_")
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
