"""Verify setup against disposable homes without provider logins or network calls."""

from __future__ import annotations

import json
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import re
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

SOURCE = Path(__file__).resolve().parents[1]
# The last release before the role renames: upgrade tests install it first, then this checkout.
BASE = '2b07c10017f87e354d8dc1aa8b578a7b041b385b'
# The last release whose Codex sandbox roots sat in the mcp block.
SANDBOX_IN_MCP_BLOCK = 'c7738b427f876470281cc642a89ee4ee67933c5d'


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='.test-setup-', dir=os.environ.get('TMPDIR', str(SOURCE.parent)))
        self.home = Path(self.temporary.name) / 'home with spaces'
        self.home.mkdir()
        self.root = self.home / 'kit source with spaces'
        self.host = self.home / 'host with spaces'
        self.commands = self.home / 'provider commands'
        self.commands.mkdir()
        for command in ('claude', 'codex', 'cursor'):
            path = self.commands / command
            path.write_text('#!/bin/sh\nprintf "hooks experimental true\\n"\n')
            path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.commands) + ':' + os.environ['PATH'],
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null')
        for key in list(self.env):
            if key.startswith('GIT_CONFIG_') and key not in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM'):
                self.env.pop(key)
        for key in ('AGENT_KIT_DIR', 'KIT_ENV', 'CLAUDE_CONFIG_DIR', 'CLAUDE_OUT_ROOT', 'AI_AGENT', 'AGENT_HOST', 'CLAUDECODE'):
            self.env.pop(key, None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_setup(self, *flags: str, success: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        # --json: these tests read the plan and the apply result as data, not the summary.
        result = subprocess.run([sys.executable, str(SOURCE / 'bin/agent-setup'), '--source', str(SOURCE),
                                 '--root-dir', str(self.root), '--host-root', str(self.host), '--json', *flags],
                                capture_output=True, text=True, env=env or self.env, timeout=60)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def state(self) -> dict:
        return json.loads((self.root / '.install-state/current.json').read_text())

    def test_skills_honor_hosts_and_keep_user_folders(self) -> None:
        user = self.host / 'skills/debug'
        user.mkdir(parents=True)
        (user / 'SKILL.md').write_text('user-owned fixture\n')
        result = self.run_setup('--hosts', 'cursor', '--components', 'skills', '--apply')
        self.assertFalse((self.host / 'skills/pr-study').exists(), 'a hosts: [claude] skill reached cursor')
        self.assertTrue((self.host / 'skills/review-rubric').is_symlink())
        self.assertTrue((self.host / 'skills/grilling').is_symlink())
        self.assertFalse(user.is_symlink())
        self.assertEqual((user / 'SKILL.md').read_text(), 'user-owned fixture\n')
        self.assertIn('not a kit link', result.stderr)
        installed = (self.root / 'skills/process-doc/SKILL.md').read_text()
        self.assertNotIn('${CLAUDE_SKILL_DIR}', installed)
        command = re.search(r'`(uv run [^`]*catalog\.py[^`]*)`', installed).group(1)
        self.assertEqual(shlex.split(command)[2], str(self.root / 'skills/process-doc/scripts/catalog.py'),
                         'CX-3: the installed path is one shell word even with spaces')

    def run_base_setup(self, *flags: str, commit: str = BASE) -> None:
        """Install with the base release, so an upgrade test starts from the state it really leaves."""
        if subprocess.run(['git', '-C', str(SOURCE), 'cat-file', '-e', commit + '^{commit}'], capture_output=True).returncode:
            self.skipTest(f'base commit {commit[:7]} is not in this clone (CI fetches full history)')
        base = Path(self.temporary.name) / 'base source'
        base.mkdir()
        archive = subprocess.run(['git', '-C', str(SOURCE), 'archive', commit], capture_output=True, check=True)
        subprocess.run(['tar', '-x', '-C', str(base)], input=archive.stdout, check=True)
        result = subprocess.run([sys.executable, str(base / 'bin/agent-setup'), '--source', str(base), '--root-dir', str(self.root),
                                 '--host-root', str(self.host), *flags], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_upgrade_canonicalizes_saved_role_overrides(self) -> None:
        self.run_base_setup('--components', 'roles', '--role-model', 'review-cross=openai:chosen-model',
                            '--role-effort', 'worker=low', '--apply')
        self.run_setup('--components', 'rules', '--apply')
        roles = tomllib.loads((self.root / 'roles.toml').read_text())['roles']
        self.assertEqual(roles['cross-reviewer']['model'], 'openai:chosen-model')
        self.assertEqual(roles['engineer']['effort'], 'low')
        saved = self.state()['configuration']
        self.assertEqual(saved['role_model'], ['cross-reviewer=openai:chosen-model'])
        self.assertEqual(saved['role_effort'], ['engineer=low'])

    def test_upgrade_retires_codex_skills_and_agents_no_longer_produced(self) -> None:
        self.run_base_setup('--hosts', 'codex', '--components', 'skills', 'roles', '--apply')
        self.assertTrue((self.host / 'skills/pr-study').is_symlink())
        self.assertTrue((self.host / 'agents/review-cross.toml').is_file())
        (self.host / 'agents/mine.toml').write_text('name = "mine"\n')
        self.run_setup('--hosts', 'codex', '--components', 'skills', 'roles', '--apply')
        self.assertFalse((self.host / 'skills/pr-study').exists(), 'a hosts: [claude] skill stayed linked for codex')
        self.assertFalse((self.host / 'skills/session-review').exists())
        self.assertTrue((self.host / 'skills/grilling').is_symlink())
        self.assertFalse((self.host / 'agents/review-cross.toml').exists())
        self.assertTrue((self.host / 'agents/cross-reviewer.toml').is_file())
        self.assertEqual((self.host / 'agents/mine.toml').read_text(), 'name = "mine"\n')
        self.assertFalse((self.root / 'agents/thermo-bugs.md').exists())
        managed = self.state()['managed']
        self.assertFalse([key for key in managed if 'pr-study' in key and key.startswith('link:')])
        self.run_setup('doctor')

    def test_upgrade_retires_claude_agents_for_renamed_roles(self) -> None:
        self.run_base_setup('--hosts', 'claude', '--components', 'roles', '--apply')
        self.assertTrue((self.host / 'agents/thermo-bugs.md').is_file())
        self.run_setup('--hosts', 'claude', '--components', 'roles', '--apply')
        agents = sorted(path.name for path in (self.host / 'agents').iterdir())
        for old in ('thermo-bugs.md', 'thermo-quality.md', 'reviewer.md', 'worker.md', 'scout.md', 'adversary.md'):
            self.assertNotIn(old, agents)
        self.assertIn('bug-reviewer.md', agents)

    def test_codex_sandbox_names_a_fresh_work_root_and_gcloud_only_on_opt_in(self) -> None:
        work = self.home / 'fresh work root'
        gcloud = self.home / '.config/gcloud'
        gcloud.mkdir(parents=True)
        env = dict(self.env, CLAUDE_OUT_ROOT=str(self.home / 'not the work root'))
        self.run_setup('--hosts', 'codex', '--components', 'mcp', '--work-root', str(work), '--apply', env=env)
        config = tomllib.loads((self.host / 'config.toml').read_text())
        self.assertEqual(config['sandbox_workspace_write']['writable_roots'], [str(work)], 'CX-4 / Q-10 / B-10')
        self.assertTrue(work.is_dir(), 'CX-4: the work root the sandbox names exists after the install')
        self.run_setup('--hosts', 'codex', '--components', 'mcp', '--codex-gcloud', 'on', '--apply')
        config = tomllib.loads((self.host / 'config.toml').read_text())
        self.assertEqual(config['sandbox_workspace_write']['writable_roots'], [str(work), str(gcloud)])

    def test_codex_sandbox_has_its_own_block_gated_on_hooks_too(self) -> None:
        """Hooks alone name the work root; the sandbox block is its own, and an update without mcp
        keeps it without a second [sandbox_workspace_write] from an install before the split."""
        work = self.home / 'work'
        self.run_setup('--hosts', 'codex', '--components', 'hooks', '--work-root', str(work), '--apply')
        text = (self.host / 'config.toml').read_text()
        self.assertEqual(tomllib.loads(text)['sandbox_workspace_write']['writable_roots'], [str(work)], 'hooks only')
        self.assertIn('# BEGIN agent-kit sandbox', text)
        self.assertTrue(work.is_dir())
        shutil.rmtree(self.root)
        shutil.rmtree(self.host)
        self.run_base_setup('--hosts', 'codex', '--components', 'mcp', '--work-root', str(work), '--apply')
        self.run_setup('--hosts', 'codex', '--components', 'hooks', '--work-root', str(work), '--apply')
        config = tomllib.loads((self.host / 'config.toml').read_text())
        self.assertEqual(config['sandbox_workspace_write']['writable_roots'], [str(work)])
        self.run_setup('--hosts', 'codex', '--components', 'rules', '--apply')
        self.assertEqual(tomllib.loads((self.host / 'config.toml').read_text()), config, 'rules alone leave config.toml')
        self.run_setup('doctor')

    def test_rules_only_update_moves_a_legacy_install_s_sandbox_roots(self) -> None:
        work = self.home / 'work'
        self.run_base_setup('--hosts', 'codex', '--components', 'rules', 'mcp', '--work-root', str(work), '--apply',
                            commit=SANDBOX_IN_MCP_BLOCK)
        self.run_setup('--hosts', 'codex', '--components', 'rules', '--apply')
        text = (self.host / 'config.toml').read_text()
        self.assertEqual(tomllib.loads(text)['sandbox_workspace_write']['writable_roots'], [str(work)])
        self.assertIn('# BEGIN agent-kit sandbox', text)

    def test_user_sandbox_table_is_kept_not_duplicated(self) -> None:
        self.host.mkdir()
        (self.host / 'config.toml').write_text('model = "x"\n\n[sandbox_workspace_write]\nnetwork_access = true\n')
        result = self.run_setup('--hosts', 'codex', '--components', 'mcp', '--apply')
        config = tomllib.loads((self.host / 'config.toml').read_text())
        self.assertEqual(config['sandbox_workspace_write'], {'network_access': True})
        self.assertIn('agent-kit: note: your [sandbox_workspace_write] is kept', result.stderr)

    def test_codex_tables_appended_inside_the_setup_block_survive_a_rerun(self) -> None:
        self.run_setup('--hosts', 'codex', '--components', 'hooks', 'mcp', '--apply')
        path = self.host / 'config.toml'
        text = path.read_text()
        end = text.rindex('# END agent-kit setup')
        path.write_text(text[:end] + '[hooks.state."abc"]\ntrusted_hash = "sha256:x"\n\n[projects."/p"]\ntrust_level = "trusted"\n\n' + text[end:])
        self.run_setup('--hosts', 'codex', '--components', 'hooks', 'mcp', '--apply')
        config = tomllib.loads(path.read_text())
        self.assertEqual(config['hooks']['state']['abc']['trusted_hash'], 'sha256:x')
        self.assertEqual(config['projects']['/p']['trust_level'], 'trusted')
        self.run_setup('doctor')

    def test_cursor_hand_added_server_spelling_is_not_registered_twice(self) -> None:
        self.host.mkdir()
        config = self.host / 'mcp.json'
        config.write_text(json.dumps({'mcpServers': {'Sentry': {'command': 'user-choice'}}}))
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps({'mcpServers': {'sentry': {'command': 'true'}}}))
        result = self.run_setup('--hosts', 'cursor', '--components', 'mcp', '--mcp-catalog', str(catalog), '--apply', success=False)
        self.assertIn('Sentry', result.stderr)
        self.assertEqual(json.loads(config.read_text())['mcpServers'], {'Sentry': {'command': 'user-choice'}})

    def test_skill_hosts_rejects_what_would_install_nowhere(self) -> None:
        module = self.module()
        skill = self.home / 'SKILL.md'
        for front, expected in (('hosts: ["claude"]', {'claude'}), ('hosts: [claude, cursor]', {'claude', 'cursor'}),
                                ('name: x', {'claude', 'codex', 'cursor'})):
            skill.write_text(f'---\n{front}\n---\nbody\n')
            self.assertEqual(module.skill_hosts(skill), expected, front)
        for front in ('hosts: [claud]', 'hosts:\n  - claude', 'hosts: []'):
            skill.write_text(f'---\n{front}\n---\nbody\n')
            with self.assertRaises(ValueError, msg=front):
                module.skill_hosts(skill)

    def test_default_is_preview(self) -> None:
        result = self.run_setup()
        self.assertEqual(json.loads(result.stdout)['mode'], 'preview')
        self.assertFalse(self.root.exists())
        self.assertFalse(self.host.exists())

    def test_install_rerun_doctor_and_rollback(self) -> None:
        self.run_setup('--apply')
        self.assertTrue((self.host / 'CLAUDE.md').exists())
        first = self.state()['id']
        result = self.run_setup('--apply')
        self.assertIn('"installed": 0', result.stdout)
        self.run_setup('doctor')
        latest = self.state()['id']
        self.run_setup('rollback', latest)
        self.assertEqual(self.state()['id'], first)
        self.run_setup('rollback', first)
        self.assertFalse((self.host / 'CLAUDE.md').exists())

    def test_codex_hooks_only_writes_shell_configuration(self) -> None:
        self.host.mkdir()
        (self.host / 'config.toml').write_text('model = "existing"\n[mcp_servers.user]\ncommand = "user-server"\n')
        self.run_setup('--hosts', 'codex', '--components', 'hooks', '--blocking-hooks', '--apply')
        config = tomllib.loads((self.host / 'config.toml').read_text())
        self.assertEqual(config['model'], 'existing')
        self.assertEqual(config['mcp_servers']['user']['command'], 'user-server')
        self.assertEqual(config['shell_environment_policy']['set']['GIT_CONFIG_VALUE_0'], str(self.root / 'git-hooks'))

    def test_advisory_hooks_do_not_enable_git_dispatcher(self) -> None:
        self.run_setup('--hosts', 'codex', '--components', 'hooks', '--apply')
        config = tomllib.loads((self.host / 'config.toml').read_text())
        self.assertNotIn('GIT_CONFIG_VALUE_0', config['shell_environment_policy']['set'])
        self.assertEqual(config['shell_environment_policy']['set']['AGENT_GIT_HOOKS'], 'off')

    def test_partial_upgrade_preserves_model_paths_and_registry(self) -> None:
        repo = str(self.home / 'repository roots with spaces')
        work = str(self.home / 'work root with spaces')
        self.run_setup('--components', 'roles', '--role-model', 'task-reviewer=openai:chosen-model',
                       '--repo-roots', repo, '--work-root', work, '--apply')
        registry = (self.root / 'hooks/registry.json').read_bytes()
        self.run_setup('--components', 'rules', '--apply')
        roles = tomllib.loads((self.root / 'roles.toml').read_text())
        self.assertEqual(roles['roles']['task-reviewer']['model'], 'openai:chosen-model')
        self.assertEqual((self.root / 'hooks/registry.json').read_bytes(), registry)
        overlay = (self.root / 'local/setup-paths.env').read_text()
        self.assertIn('CODE_DIRS_JSON=' + json.dumps([repo]), overlay)
        self.assertIn('AGENT_WORK_ROOT=' + work, overlay)

    def test_existing_npx_skills_and_auth_preserved(self) -> None:
        self.root.mkdir()
        skill = self.root / 'skills/user-skill/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('User skill\n')
        auth = self.home / '.codex/auth.json'
        auth.parent.mkdir()
        auth.write_bytes(b'opaque credential fixture')
        auth.chmod(0)
        self.run_setup('--apply')
        self.assertEqual(skill.read_text(), 'User skill\n')
        self.assertEqual(auth.stat().st_mode & 0o777, 0)
        auth.chmod(0o600)
        self.assertEqual(auth.read_bytes(), b'opaque credential fixture')

    def test_collision_refuses_before_mutation_and_backup_restores(self) -> None:
        self.root.mkdir()
        collision = self.root / 'roles.toml'
        collision.write_text('user config\n')
        self.run_setup('--apply', success=False)
        self.assertFalse((self.host / 'CLAUDE.md').exists())
        self.assertEqual(collision.read_text(), 'user config\n')
        self.run_setup('--collision', 'backup', '--apply')
        self.run_setup('rollback', self.state()['id'])
        self.assertEqual(collision.read_text(), 'user config\n')

    def test_missing_cli_and_unsupported_hooks_refuse_before_mutation(self) -> None:
        self.run_setup('--components', 'hooks', '--claude-bin', 'missing-fixture-cli', '--apply', success=False)
        self.assertFalse(self.root.exists())
        self.run_setup('--components', 'hooks', '--hook-runtime', 'cloud', '--apply', success=False)
        self.assertFalse(self.root.exists())
        (self.commands / 'codex').write_text('#!/bin/sh\nprintf "other stable true\\n"\n')
        self.run_setup('--hosts', 'codex', '--components', 'hooks', '--apply', success=False)
        self.assertFalse(self.root.exists())

    def test_missing_jq_refuses_before_mutation(self) -> None:
        minimal = self.home / 'minimal-bin'
        minimal.mkdir()
        for name in ('git', 'claude'):
            path = minimal / name
            path.symlink_to(shutil.which(name, path=self.env['PATH']))
        env = dict(self.env, PATH=str(minimal))
        self.run_setup('--components', 'hooks', '--apply', success=False, env=env)
        self.assertFalse(self.root.exists())

    def test_blank_config_and_no_workroot_author(self) -> None:
        self.host.mkdir()
        (self.host / 'settings.json').write_text('')
        self.run_setup('--components', 'hooks', '--work-root', '', '--apply')
        self.assertNotIn('AGENT_WORK_ROOT=', (self.root / 'local/setup-paths.env').read_text())
        self.assertNotIn('GIT_AUTHOR=', (self.root / 'local/setup-paths.env').read_text())

    def test_user_mcp_hooks_and_config_preserved_and_retired_server_removed(self) -> None:
        self.host.mkdir()
        user = {'mcpServers': {'user': {'command': 'user-server'}}, 'userSetting': True}
        (self.host / 'mcp.json').write_text(json.dumps(user))
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps({'mcpServers': {'selected': {'command': 'selected-server'}}}))
        self.run_setup('--components', 'mcp', '--mcp-catalog', str(catalog), '--apply')
        catalog.write_text('{"mcpServers": {}}')
        self.run_setup('--components', 'mcp', '--mcp-catalog', str(catalog), '--apply')
        document = json.loads((self.host / 'mcp.json').read_text())
        self.assertEqual(document, user)
        self.run_setup('rollback', self.state()['id'])
        self.assertIn('selected', json.loads((self.host / 'mcp.json').read_text())['mcpServers'])

    def test_cursor_project_rules_separate_from_global_hook_config(self) -> None:
        project = self.home / 'project with spaces'
        self.run_setup('--hosts', 'cursor', '--components', 'rules', 'hooks', '--project-root', str(project),
                       '--confirm-hook-support', 'cursor', '--apply')
        self.assertTrue((project / '.cursor/rules/agent-kit.mdc').exists())
        self.assertTrue((self.host / 'hooks.json').exists())
        self.assertFalse((project / '.cursor/hooks.json').exists())

    def test_secret_catalog_refused(self) -> None:
        catalog = self.home / 'catalog.json'
        catalog.write_text('{"mcpServers": {"fixture": {"command": "server", "env": {"API_KEY": "fixture"}}}}')
        self.run_setup('--components', 'mcp', '--mcp-catalog', str(catalog), '--apply', success=False)
        self.assertFalse(self.root.exists())

    def test_claude_blocking_choice_sets_model_shell_environment(self) -> None:
        self.host.mkdir()
        (self.host / 'settings.json').write_text('{"env": {"USER_CHOICE": "preserved"}, "model": "user-model"}')
        self.run_setup('--components', 'hooks', '--blocking-hooks', '--apply')
        config = json.loads((self.host / 'settings.json').read_text())
        self.assertEqual(config['env']['USER_CHOICE'], 'preserved')
        self.assertEqual(config['model'], 'user-model')
        self.assertEqual(config['env']['GIT_CONFIG_VALUE_0'], str(self.root / 'git-hooks'))
        self.run_setup('--components', 'hooks', '--apply')
        config = json.loads((self.host / 'settings.json').read_text())
        self.assertEqual(config['env']['GIT_CONFIG_VALUE_0'], str(self.root / 'git-hooks'),
                         '4c: reselecting hooks without the flag keeps the saved blocking choice')
        self.run_setup('--components', 'hooks', '--no-blocking-hooks', '--apply')
        config = json.loads((self.host / 'settings.json').read_text())
        self.assertNotIn('GIT_CONFIG_VALUE_0', config['env'])
        self.assertEqual(config['env']['AGENT_GIT_HOOKS'], 'off')

    def test_codex_mcp_upgrade_preserves_previous_hook_shell(self) -> None:
        self.run_setup('--hosts', 'codex', '--components', 'hooks', '--blocking-hooks', '--apply')
        self.run_setup('--hosts', 'codex', '--components', 'mcp', '--apply')
        config = tomllib.loads((self.host / 'config.toml').read_text())
        self.assertEqual(config['shell_environment_policy']['set']['GIT_CONFIG_VALUE_0'], str(self.root / 'git-hooks'))

    def test_overlay_behavior_preserves_user_values_with_generated_paths(self) -> None:
        local = self.root / 'local'
        local.mkdir(parents=True)
        overlay = local / 'kit.env'
        content = 'TEST_RUNNER=fixture-runner\nGIT_AUTHOR=Fixture Author\nBQRO_PROJECT=fixture-project\nAGENT_WORK_ROOT=old-work-root\n'
        overlay.write_text(content)
        work = str(self.home / 'chosen work root')
        self.run_setup('--components', 'rules', '--work-root', work, '--apply')
        env = dict(self.env, KIT_ENV=str(local / 'setup-paths.env'))
        result = subprocess.run(['bash', '-c', '. "$1"; kit_env; printf "%s\\n" "$TEST_RUNNER" "$GIT_AUTHOR" "$BQRO_PROJECT" "$AGENT_WORK_ROOT"',
                                 '_', str(self.root / 'hooks/lib/hook-io')], capture_output=True, text=True, env=env, check=True)
        self.assertEqual(result.stdout.splitlines(), ['fixture-runner', 'Fixture Author', 'fixture-project', work])
        result = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); from kit_env import kit_env; e=kit_env(); print(e["TEST_RUNNER"]); print(e["AGENT_WORK_ROOT"])',
                                 str(self.root / 'hooks/lib')], capture_output=True, text=True, env=env, check=True)
        self.assertEqual(result.stdout.splitlines(), ['fixture-runner', work])
        self.assertEqual(overlay.read_text(), content)
        manifest = (self.root / '.install-state/current.json').read_text()
        self.assertNotIn('Fixture Author', manifest)
        self.assertNotIn('fixture-project', manifest)

    def test_retired_edited_mcp_server_is_preserved(self) -> None:
        catalog = self.home / 'catalog.json'
        catalog.write_text('{"mcpServers": {"selected": {"command": "selected-server"}}}')
        self.run_setup('--components', 'mcp', '--mcp-catalog', str(catalog), '--apply')
        document = json.loads((self.host / 'mcp.json').read_text())
        document['mcpServers']['selected']['command'] = 'user-edited-server'
        (self.host / 'mcp.json').write_text(json.dumps(document))
        catalog.write_text('{"mcpServers": {}}')
        self.run_setup('--components', 'mcp', '--mcp-catalog', str(catalog), '--apply', success=False)
        self.assertEqual(json.loads((self.host / 'mcp.json').read_text())['mcpServers']['selected']['command'], 'user-edited-server')

    def test_rules_only_exposes_no_skills_and_selected_one_exposes_only_one(self) -> None:
        self.run_setup('--components', 'rules', '--apply')
        self.assertEqual(list((self.root / 'skills').glob('*/SKILL.md')), [])
        self.assertFalse((self.host / 'skills').exists())
        self.run_setup('--components', 'skills', '--skills', 'unslop', '--apply')
        self.assertEqual([path.parent.name for path in (self.root / 'skills').glob('*/SKILL.md')], ['unslop'])
        self.assertEqual([path.name for path in (self.host / 'skills').iterdir()], ['unslop'])

    def test_partial_failure_rolls_back_before_pending_config_write(self) -> None:
        self.host.mkdir()
        settings = self.host / 'settings.json'
        original = {'env': {'USER_CHOICE': 'preserve'}, 'model': 'user-choice'}
        settings.write_text(json.dumps(original))
        self.host.chmod(0o555)
        try:
            result = self.run_setup('--components', 'hooks', '--blocking-hooks', '--apply', success=False)
            self.assertIn('Permission denied', result.stderr)
        finally:
            self.host.chmod(0o755)
        self.assertEqual(json.loads(settings.read_text()), original)
        self.assertFalse((self.root / 'bin/agent-setup').exists())
        self.assertFalse((self.root / '.install-state/current.json').exists())

    def test_roles_only_runner_dry_run_uses_internal_references_without_provider(self) -> None:
        custom = self.commands / 'configured codex with spaces'
        custom.write_text('#!/bin/sh\nexit 97\n')
        custom.chmod(0o755)
        self.run_setup('--components', 'roles', '--codex-bin', str(custom), '--apply')
        repo = self.home / 'synthetic repository'
        repo.mkdir()
        subprocess.run(['git', '-C', str(repo), 'init', '-b', 'main'], capture_output=True, env=self.env, check=True)
        (repo / 'code.py').write_text('value = 1\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'code.py'], env=self.env, check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com',
                        'commit', '-m', 'Fixture'], capture_output=True, env=self.env, check=True)
        marker = self.home / 'provider-tripwire'
        for command in ('codex', 'claude'):
            (self.commands / command).write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 97\n')
        out = self.home / 'review output.json'
        result = subprocess.run([str(self.root / 'bin/agent-run'), 'cross-reviewer', str(repo),
                                 '--base', 'HEAD', '--out', str(out), '--dry-run'], env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        command = next(line.removeprefix('command: ') for line in result.stdout.splitlines() if line.startswith('command: '))
        self.assertIn(str(custom), shlex.split(command))
        self.assertTrue(out.with_suffix('.prompt.md').exists())
        text = out.with_suffix('.prompt.md').read_text()
        self.assertIn('correctness_rubric', text)
        self.assertNotIn('{{AGENT_KIT_DIR}}', text)
        self.assertEqual(list((self.root / 'skills').glob('*/SKILL.md')), [])
        self.assertTrue((self.root / 'skills/review-rubric/references/fix-policy.md').exists())

    def test_unselected_mcp_catalog_is_never_opened_or_copied(self) -> None:
        source = self.home / 'private-source fixture'
        source.mkdir()
        for path in SOURCE.iterdir():
            if path.name not in ('mcp', '.git'):
                (source / path.name).symlink_to(path, target_is_directory=path.is_dir())
        (source / 'mcp').mkdir()
        catalog = source / 'mcp/servers.json'
        catalog.write_text('unselected opaque fixture\n')
        catalog.chmod(0)
        try:
            self.run_setup('--source', str(source), '--components', 'rules', '--apply')
            self.assertFalse((self.root / 'mcp/servers.json').exists())
            self.assertNotIn('unselected opaque fixture', json.dumps(self.state()))
        finally:
            catalog.chmod(0o600)

    def test_selected_code_search_shell_loads_root_and_overlay(self) -> None:
        repo_parent = self.home / 'repo roots with spaces'
        self.run_setup('--components', 'skills', '--skills', 'code-search', '--repo-roots', str(repo_parent), '--apply')
        skill = (self.host / 'skills/code-search/SKILL.md').read_text()
        block = re.search(r'```sh\n(.*?)\n```', skill, re.S).group(1)
        prefix = block.split("printf '%s", 1)[0]
        result = subprocess.run(['bash', '-c', prefix + '\nprintf "%s" "$CODE_DIRS_JSON"'],
                                env=self.env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [str(repo_parent)])
        helper = self.root / 'skills/code-search/scripts/code_search.py'
        subprocess.run([str(helper), '--help'], env=self.env, capture_output=True, check=True)

    def test_blocking_hooks_also_render_required_native_agents(self) -> None:
        self.run_setup('--components', 'hooks', '--blocking-hooks', '--apply')
        self.assertTrue((self.host / 'agents/engineer.md').exists())
        self.assertTrue((self.host / 'agents/task-reviewer.md').exists())
        self.assertTrue((self.root / 'references/conventions.md').exists())
        for name in ('engineer.md', 'task-reviewer.md', 'quality-reviewer.md'):
            text = (self.host / 'agents' / name).read_text()
            self.assertNotIn('/skills/conventions/SKILL.md', text)
            self.assertNotIn('/skills/debug/SKILL.md', text)
            self.assertIn(str(self.root / 'references/conventions.md'), text)

    def test_session_context_reads_selected_claude_host(self) -> None:
        self.run_setup('--components', 'hooks', '--apply')
        repo = self.home / 'context-fixture'
        repo.mkdir()
        subprocess.run(['git', '-C', str(repo), 'init', '-b', 'main'], env=self.env, capture_output=True, check=True)
        (self.host / 'local').mkdir()
        (self.host / 'local/context-fixture-rules.md').write_text('selected host guidance\n')
        legacy = self.home / '.claude/local'
        legacy.mkdir(parents=True)
        (legacy / 'context-fixture-testing.md').write_text('legacy guidance fixture\n')
        settings = json.loads((self.host / 'settings.json').read_text())
        command = next(hook['command'] for group in settings['hooks']['SessionStart'] for hook in group['hooks']
                       if 'session-context' in hook.get('command', ''))
        result = subprocess.run(['bash', '-c', command], input='{}', env=dict(self.env, CLAUDE_PROJECT_DIR=str(repo)),
                                capture_output=True, text=True, timeout=20, check=True)
        self.assertIn(str(self.host / 'local'), result.stdout)
        self.assertIn('context-fixture-rules.md', result.stdout)
        self.assertNotIn('context-fixture-testing.md', result.stdout)

    def module(self):
        loader = importlib.machinery.SourceFileLoader('setup_real_file_probe', str(SOURCE / 'bin/agent-setup'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        loader.exec_module(module)
        return module

    def test_preflight_user_edit_is_refused_and_unrelated_fields_preserved(self) -> None:
        module = self.module()
        options = argparse.Namespace(root_dir=str(self.root), collision='refuse')
        self.host.mkdir()
        target = self.host / 'mcp.json'
        target.write_text('{}')
        wanted = {'fixture': {'command': 'true'}}
        change = module.Change('servers', target, wanted)
        record = module.prepare_change(change, options)
        target.write_text(json.dumps({'mcpServers': {'fixture': {'command': 'user-choice'}}}))
        with self.assertRaisesRegex(ValueError, 'after preflight'):
            module.apply_change(change, record, self.home / 'journal', 0)
        self.assertEqual(json.loads(target.read_text())['mcpServers']['fixture']['command'], 'user-choice')
        target.write_text('{}')
        record = module.prepare_change(change, options)
        target.write_text(json.dumps({'user-choice': 'preserved'}))
        module.apply_change(change, record, self.home / 'journal', 0)
        self.assertEqual(json.loads(target.read_text())['user-choice'], 'preserved')

    def test_rollback_drift_preflight_preserves_all_later_records(self) -> None:
        self.run_setup('--components', 'rules', '--apply')
        state = self.state()
        journal = json.loads((self.root / '.install-state' / state['id'] / 'journal.json').read_text())
        first = Path(journal['records'][0]['target'])
        original = first.read_bytes()
        before_rule = (self.host / 'CLAUDE.md').read_bytes()
        first.write_bytes(original + b'\nuser edit\n')
        self.run_setup('rollback', state['id'], success=False)
        self.assertEqual((self.host / 'CLAUDE.md').read_bytes(), before_rule)
        first.write_bytes(original)
        self.run_setup('rollback', state['id'])
        self.assertFalse((self.host / 'CLAUDE.md').exists())

    def test_interrupted_rollback_journal_resumes_real_permission_failure(self) -> None:
        self.run_setup('--components', 'rules', '--apply')
        state = self.state()
        journal_dir = self.root / '.install-state' / state['id']
        journal = json.loads((journal_dir / 'journal.json').read_text())
        first = Path(journal['records'][0]['target'])
        first.parent.chmod(0o555)
        try:
            self.run_setup('rollback', state['id'], success=False)
            progress = json.loads((journal_dir / 'rollback-state.json').read_text())
            self.assertTrue(progress['completed'])
            self.assertFalse((self.host / 'CLAUDE.md').exists())
        finally:
            first.parent.chmod(0o755)
        self.run_setup('rollback', state['id'])
        self.assertFalse((self.root / 'bin/agent-setup').exists())

    def test_roles_direct_runner_honors_provider_path_and_explicit_override(self) -> None:
        custom = self.commands / 'custom codex'
        custom.write_text('#!/bin/sh\nexit 97\n')
        custom.chmod(0o755)
        self.run_setup('--components', 'roles', '--codex-bin', str(custom), '--apply')
        script = '. "$1/hooks/lib/review-state"; _rv_cli openai'
        result = subprocess.run(['bash', '-c', script, 'probe', str(self.root)], env=self.env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, str(custom))
        result = subprocess.run(['bash', '-c', script, 'probe', str(self.root)], env=dict(self.env, CODEX_BIN='/explicit/override'),
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, '/explicit/override')

    def test_plugin_root_bin_forwards_wrappers_without_auth_calls(self) -> None:
        marker = self.home / 'auth-tripwire'
        for command in ('security', 'ssh', 'mysql', 'bq'):
            fixture = self.commands / command
            fixture.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 97\n')
            fixture.chmod(0o755)
        plugin = SOURCE / 'plugins/prod-data'
        env = dict(self.env, PATH=str(plugin / 'bin') + ':' + self.env['PATH'])
        result = subprocess.run(['bash', '-c', 'command -v ro-mysql; ro-mysql'], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertIn(str(plugin / 'bin/ro-mysql'), result.stdout)
        self.assertIn('usage: ro-mysql', result.stderr)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_plugin_scoped_review_agent_is_recognized(self) -> None:
        helper = SOURCE / 'plugins/auto-review/kit/hooks/lib/review-state'
        script = '. "$1"; rv_review_agent auto-review:bug-reviewer'
        result = subprocess.run(['bash', '-c', script, 'probe', str(helper)], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_duplicate_plugin_native_hooks_refused_without_user_config_changes(self) -> None:
        self.host.mkdir()
        settings = self.host / 'settings.json'
        original = json.dumps({'enabledPlugins': {'auto-review@fixture': True}, 'model': 'user-choice'})
        settings.write_text(original)
        preview = self.run_setup('--components', 'hooks', '--blocking-hooks')
        self.assertIn('auto-review@fixture', json.loads(preview.stdout)['conflicting_plugins'])
        self.run_setup('--components', 'hooks', '--blocking-hooks', '--apply', success=False)
        self.assertFalse(self.root.exists())
        self.assertEqual(settings.read_text(), original)

    def test_cursor_empty_catalog_retires_owned_servers_and_preserves_user(self) -> None:
        self.host.mkdir()
        config = self.host / 'mcp.json'
        config.write_text(json.dumps({'mcpServers': {'user': {'command': 'user-choice'}}}))
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps({'mcpServers': {'owned': {'command': 'true'}}}))
        self.run_setup('--hosts', 'cursor', '--components', 'mcp', '--mcp-catalog', str(catalog), '--apply')
        catalog.write_text('{"mcpServers": {}}')
        self.run_setup('--hosts', 'cursor', '--components', 'mcp', '--mcp-catalog', str(catalog), '--apply')
        self.assertEqual(json.loads(config.read_text())['mcpServers'], {'user': {'command': 'user-choice'}})
        self.run_setup('rollback', self.state()['id'])
        current = json.loads(config.read_text())
        current['mcpServers']['owned'] = {'command': 'edited-choice'}
        config.write_text(json.dumps(current))
        self.run_setup('--hosts', 'cursor', '--components', 'mcp', '--mcp-catalog', str(catalog), '--apply', success=False)
        self.assertEqual(json.loads(config.read_text())['mcpServers']['owned']['command'], 'edited-choice')

    def test_codex_trust_state_preserved_and_inline_event_definitions_refused(self) -> None:
        self.host.mkdir()
        config = self.host / 'config.toml'
        config.write_text('[hooks.state.fixture]\ntrusted = true\n')
        self.run_setup('--hosts', 'codex', '--components', 'hooks', '--apply')
        self.assertTrue(tomllib.loads(config.read_text())['hooks']['state']['fixture']['trusted'])
        self.run_setup('rollback', self.state()['id'])
        config.write_text('[hooks.SessionStart]\ncommand = "user-choice"\n')
        before = config.read_text()
        self.run_setup('--hosts', 'codex', '--components', 'hooks', '--apply', success=False)
        self.assertEqual(config.read_text(), before)

    def test_hooks_only_keeps_reference_dependencies_and_no_task_skills(self) -> None:
        self.run_setup('--components', 'hooks', '--apply')
        for name in ('review-output.schema.json', 'references/correctness.md', 'references/fix-policy.md'):
            self.assertTrue((self.root / 'skills/review-rubric' / name).exists())
        self.assertEqual(list((self.root / 'skills').glob('*/SKILL.md')), [])

    def snapshot(self, *hosts: Path) -> dict[str, str]:
        """Every file and link under the kit root (install state aside) and the host roots, by content."""
        files = {}
        for base in (self.root, *(hosts or [self.host])):
            for path in sorted(base.rglob('*')):
                if '.install-state' in path.relative_to(base).parts:
                    continue
                if path.is_symlink():
                    files[str(path)] = 'link:' + os.readlink(path)
                elif path.is_file():
                    files[str(path)] = path.read_text(errors='replace')
        return files

    def test_cursor_rules_or_hooks_update_keeps_installed_mcp(self) -> None:
        self.host.mkdir()
        config = self.host / 'mcp.json'
        config.write_text(json.dumps({'mcpServers': {'user': {'command': 'user-choice'}}}))
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps({'mcpServers': {'owned': {'command': 'true'}}}))
        self.run_setup('--hosts', 'cursor', '--components', 'rules', 'hooks', 'mcp', '--mcp-catalog', str(catalog),
                       '--confirm-hook-support', 'cursor', '--apply')
        servers = json.loads(config.read_text())['mcpServers']
        self.assertEqual(set(servers), {'user', 'owned'})
        record = self.state()['managed']['servers:' + str(config)]
        for components in (['rules'], ['hooks']):
            self.run_setup('--hosts', 'cursor', '--components', *components, '--confirm-hook-support', 'cursor', '--apply')
            self.assertEqual(json.loads(config.read_text())['mcpServers'], servers, f'R2-CX-1: {components} only')
            self.assertEqual(self.state()['managed']['servers:' + str(config)], record, f'R2-CX-1: {components} only')
        self.run_setup('doctor')

    def test_explicit_gcloud_off_overrides_the_user_overlay(self) -> None:
        gcloud = self.home / '.config/gcloud'
        gcloud.mkdir(parents=True)
        work = self.home / 'work'
        self.run_setup('--hosts', 'codex', '--components', 'rules', 'hooks', 'mcp', '--work-root', str(work),
                       '--codex-gcloud', 'on', '--apply')
        config = self.host / 'config.toml'
        self.assertEqual(tomllib.loads(config.read_text())['sandbox_workspace_write']['writable_roots'], [str(work), str(gcloud)])
        (self.root / 'local/kit.env').write_text('CODEX_SANDBOX_GCLOUD=1\n')
        self.run_setup('--hosts', 'codex', '--components', 'mcp', '--codex-gcloud', 'off', '--apply')
        self.assertEqual(tomllib.loads(config.read_text())['sandbox_workspace_write']['writable_roots'], [str(work)],
                         'R2-CX-2: an explicit off loses to kit.env')
        self.assertEqual(self.state()['configuration']['codex_gcloud'], 'off')
        env = dict(self.env, KIT_ENV=str(self.root / 'local/setup-paths.env'))
        result = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); from kit_env import kit_env; print(kit_env()["CODEX_SANDBOX_GCLOUD"])',
                                 str(self.root / 'hooks/lib')], capture_output=True, text=True, env=env, check=True)
        self.assertEqual(result.stdout.strip(), '0', 'R2-CX-2: hooks resolve the setup choice, not the user layer')

    def test_partial_update_keeps_the_installed_custom_mcp_catalog(self) -> None:
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps({'mcpServers': {'custom': {'command': 'true'}}}))
        self.run_setup('--components', 'rules', 'skills', 'mcp', '--mcp-catalog', str(catalog), '--apply')
        installed = self.root / 'mcp/servers.json'
        content = installed.read_text()
        self.assertIn('custom', content)
        for components in (['rules'], ['skills']):
            self.run_setup('--components', *components, '--apply')
            self.assertEqual(installed.read_text() if installed.is_file() else None, content, f'R2-CX-3: {components} only')
            self.assertIn('file:' + str(installed), self.state()['managed'])
        self.run_setup('doctor')

    def test_single_component_updates_leave_every_other_component_alone(self) -> None:
        """Install every component, then select one at a time: nothing installed may change or lose its record."""
        everything = ['rules', 'hooks', 'roles', 'skills', 'mcp', 'data-wrappers', 'commands']
        for host in ('claude', 'codex', 'cursor'):
            with self.subTest(host=host):
                shutil.rmtree(self.root, ignore_errors=True)
                shutil.rmtree(self.host, ignore_errors=True)
                self.host.mkdir()
                (self.host / 'mcp.json' if host != 'codex' else self.host / 'config.toml').write_text(
                    json.dumps({'mcpServers': {'user': {'command': 'user-choice'}}}) if host != 'codex' else 'model = "user"\n')
                flags = ['--hosts', host, '--confirm-hook-support', 'cursor']
                # 4c: blocking hooks and a custom catalog, given only here, must survive every reselection.
                catalog = self.home / 'custom catalog.json'
                catalog.write_text(json.dumps({'mcpServers': {'custom': {'command': 'true'}}}))
                self.run_setup(*flags, '--components', *everything, '--blocking-hooks', '--mcp-catalog', str(catalog), '--apply')
                before = self.snapshot()
                managed = self.state()['managed']
                for component in everything:
                    self.run_setup(*flags, '--components', component, '--apply')
                    after = self.snapshot()
                    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
                    self.assertEqual(changed, [], f'{host}: selecting only {component}')
                    current = self.state()['managed']
                    self.assertEqual(sorted(managed.keys() - current.keys()), [], f'{host}: selecting only {component}')
                self.run_setup('doctor')

    def test_single_host_updates_leave_the_other_hosts_alone(self) -> None:
        hosts = {'claude': self.home / 'claude config', 'codex': self.home / 'codex home', 'cursor': self.home / '.cursor'}
        env = dict(self.env, CLAUDE_CONFIG_DIR=str(hosts['claude']), CODEX_HOME=str(hosts['codex']))
        everything = ['rules', 'hooks', 'roles', 'skills', 'mcp', 'data-wrappers', 'commands']

        def setup(*flags: str) -> None:
            result = subprocess.run([sys.executable, str(SOURCE / 'bin/agent-setup'), '--source', str(SOURCE), '--root-dir', str(self.root),
                                     *flags, '--confirm-hook-support', 'cursor'], capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)

        setup('--hosts', *hosts, '--components', *everything, '--apply')
        before = self.snapshot(*hosts.values())
        managed = self.state()['managed']
        for host in hosts:
            for components in (everything, ['rules']):
                setup('--hosts', host, '--components', *components, '--apply')
                after = self.snapshot(*hosts.values())
                changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
                self.assertEqual(changed, [], f'selecting only {host} with {components}')
                self.assertEqual(sorted(managed.keys() - self.state()['managed'].keys()), [], f'selecting only {host}')
        setup('doctor')


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SetupTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': result.testsRun, 'successful': result.wasSuccessful(),
                                'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
