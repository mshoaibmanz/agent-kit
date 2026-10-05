"""The installer UX cases: each one runs agent-setup in a fake home whose PATH is a folder of real
symlinks (or the fixture host CLIs setup_test uses), then checks the summary line, the exit code and
that nothing was written outside the fake home. No provider login, network or credential is used."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

SOURCE = Path(__file__).resolve().parents[1]
# What install-hint, git and the summary need; everything else a case adds or leaves out on purpose.
BASE_TOOLS = ('git', 'sh', 'uname', 'env', 'python3', 'cat', 'tr', 'sed', 'awk', 'grep', 'dirname', 'basename', 'mkdir')


class InstallerUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='.test-installer-ux-', dir=os.environ.get('TMPDIR'))
        self.outer = Path(self.temporary.name)
        self.home = self.outer / 'home'
        self.home.mkdir()
        self.shim = self.outer / 'shim'
        self.shim.mkdir()
        self.tmp = self.outer / 'tmp'
        self.tmp.mkdir()
        self.root = self.home / '.local/share/agent-kit'
        self.link(*BASE_TOOLS)
        self.source_status = self.status()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def status(self) -> str:
        return subprocess.run(['git', '-C', str(SOURCE), 'status', '--porcelain', '--untracked-files=all'],
                              capture_output=True, text=True).stdout

    def link(self, *names: str) -> None:
        """Real tools, linked into the shim PATH."""
        for name in names:
            found = shutil.which(name)
            if found and not (self.shim / name).exists():
                (self.shim / name).symlink_to(found)

    def host_cli(self, name: str, login: bool = True) -> None:
        """A host CLI fixture: answers `features list` with hooks on, and `login status` as told."""
        script = self.shim / name
        script.write_text('#!/bin/sh\n[ "$1 $2" = "login status" ] && exit ' + ('0' if login else '1')
                          + '\nprintf "hooks experimental true\\n"\n')
        script.chmod(0o755)

    def setup(self, *flags: str, code: int = 0, env: dict | None = None) -> subprocess.CompletedProcess:
        environment = {'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp),
                       'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null', 'LANG': 'C.UTF-8', **(env or {})}
        result = subprocess.run([sys.executable, str(SOURCE / 'bin/agent-setup'), '--source', str(SOURCE), *flags],
                                capture_output=True, text=True, env=environment, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(sorted(path.name for path in self.outer.iterdir()), ['home', 'shim', 'tmp'], 'wrote beside the fake home')
        self.assertEqual(list(self.tmp.iterdir()), [], 'left files in TMPDIR')
        self.assertEqual(self.status(), self.source_status, 'changed the source checkout')
        return result

    def test_no_host_says_so_and_installs_kit_files_only(self) -> None:
        result = self.setup('--components', 'rules', 'skills', '--apply')
        self.assertIn('hosts: none detected or selected', result.stdout)
        self.assertIn('https://docs.anthropic.com/en/docs/claude-code', result.stdout)
        self.assertIn('Installed', result.stdout)
        self.assertFalse((self.home / '.claude').exists(), 'wrote a host config for a host that is not installed')
        self.assertTrue((self.root / 'rules/AGENTS.md').is_file())

    def test_one_detected_host_is_the_default(self) -> None:
        self.host_cli('codex')
        result = self.setup('--components', 'rules')
        self.assertIn('hosts codex;', result.stdout)
        self.assertIn('codex: codex, signed in', result.stdout)
        self.assertNotIn('cross-reviewer', result.stdout.split('Skipped:')[0], 'role deps only for selected hosts')

    def test_logged_out_host_needs_attention_and_doctor_checks_login(self) -> None:
        self.host_cli('claude')
        result = self.setup('--components', 'rules', '--apply')
        self.assertIn('Needs attention:\n  claude: claude, not signed in', result.stdout)
        doctor = json.loads(self.setup('doctor').stdout)
        self.assertFalse(doctor['native_auth']['claude']['logged_in'], 'doctor checked only the config dir')
        (self.home / '.claude.json').write_text(json.dumps({'oauthAccount': {'emailAddress': 'user@example.com'}}))
        self.assertIn('claude: claude, signed in', self.setup('--components', 'rules').stdout)
        self.assertTrue(json.loads(self.setup('doctor').stdout)['native_auth']['claude']['logged_in'])

    def test_missing_jq_blocks_apply_up_front_with_this_os_fix(self) -> None:
        self.host_cli('claude')
        result = self.setup('--components', 'hooks')
        hint = subprocess.run(['sh', str(SOURCE / 'hooks/lib/install-hint'), 'jq'], capture_output=True, text=True,
                              env={'PATH': str(self.shim)}, check=True).stdout.strip()
        self.assertIn(f'jq missing, required for hooks (blocks apply). Fix: {hint}\n', result.stdout)
        self.setup('--components', 'hooks', '--apply', code=2)
        self.assertFalse(self.root.exists())

    def test_soft_requirements_skip_their_feature(self) -> None:
        self.link('jq')
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps({'mcpServers': {'docs': {'command': 'npx', 'args': ['-y', 'example-docs-server']}}}))
        (self.shim / 'secret-tool').write_text('#!/bin/sh\nexit 0\n')
        (self.shim / 'secret-tool').chmod(0o755)
        result = self.setup('--components', 'skills', 'mcp', 'data-wrappers', '--skills', 'code-search',
                            '--mcp-catalog', str(catalog), '--hosts', 'claude', '--apply')
        skipped = result.stdout.split('Skipped:')[1]
        for line in ('GitHub code search off: gh missing', 'MCP server docs off: npx missing',
                     'BigQuery read wrapper off: bq missing', 'BigQuery read wrapper login off: gcloud missing'):
            self.assertIn(line, skipped)
        registered = self.home / '.claude/mcp.json'
        self.assertFalse(registered.exists() and json.loads(registered.read_text()).get('mcpServers'),
                         'a server whose runtime is missing is not registered')
        self.assertIn('credentials: ', result.stdout)
        if sys.platform == 'darwin':
            self.assertIn('wrappers read the macOS Keychain', result.stdout)
        elif not any(Path(path).exists() for path in ('/usr/bin/secret-tool', '/usr/local/bin/secret-tool')):
            self.assertIn('wrappers read env vars', result.stdout, 'a secret-tool on PATH alone is not a store')
        (self.shim / 'npx').write_text('#!/bin/sh\nexit 0\n')
        (self.shim / 'npx').chmod(0o755)
        self.setup('--components', 'skills', 'mcp', 'data-wrappers', '--skills', 'code-search',
                   '--mcp-catalog', str(catalog), '--hosts', 'claude', '--apply')
        self.assertIn('docs', json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers'],
                      'a rerun once the runtime is installed registers it')

    def test_empty_mcp_catalog_is_named_not_silent(self) -> None:
        result = self.setup('--components', 'mcp', '--hosts', 'claude')
        self.assertIn('no MCP servers configured (add them in mcp/servers.json or a preset)', result.stdout)

    def test_code_search_without_gh_is_one_line(self) -> None:
        environment = {'HOME': str(self.home), 'PATH': str(self.shim), 'AGENT_KIT_DIR': str(SOURCE), 'KIT_ENV': '/dev/null'}
        result = subprocess.run([sys.executable, str(SOURCE / 'skills/code-search/scripts/code_search.py'), 'repos', 'x'],
                                capture_output=True, text=True, env=environment, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('gh is not installed: GitHub search is off', result.stderr)

    def test_collision_is_found_in_preflight_and_skip_leaves_it(self) -> None:
        self.link('jq')
        self.host_cli('claude')
        mine = self.home / '.claude/agents/engineer.md'
        mine.parent.mkdir(parents=True)
        mine.write_text('my own engineer\n')
        result = self.setup('--components', 'roles')
        self.assertIn(f'{mine} holds your own content (blocks apply)', result.stdout, 'found in preflight')
        self.setup('--components', 'roles', '--apply', code=2)
        self.assertFalse(self.root.exists())
        result = self.setup('--components', 'roles', '--collision', 'skip', '--apply')
        self.assertIn('your own content, left in place', result.stdout)
        self.assertEqual(mine.read_text(), 'my own engineer\n')
        self.assertTrue((self.home / '.claude/agents/researcher.md').is_file())

    def test_local_preset_fills_answers_and_is_recorded(self) -> None:
        preset = self.home / 'team preset.toml'
        preset.write_text('[kit]\nCODE_SEARCH_GH_OWNER = "example-org"\nCODE_SEARCH_ZOEKT_URL = "https://zoekt.example.com"\n'
                          'BQRO_PROJECT = "example-project"\nREVIEW_BASE = "main"\n\n'
                          '[mcp.servers.docs]\nurl = "https://mcp.example.com/mcp"\n\n[skills]\ninclude = ["conventions"]\n')
        result = self.setup('--preset', str(preset), '--components', 'rules', 'skills', 'mcp', '--hosts', 'claude', '--apply')
        self.assertIn(f'preset {preset} (sha256 ', result.stdout)
        self.assertIn('GitHub org check for example-org: gh is missing', result.stdout)
        layer = (self.root / 'local/preset.env').read_text()
        for line in ('CODE_SEARCH_GH_OWNER=example-org', 'CODE_SEARCH_ZOEKT_URL=https://zoekt.example.com',
                     'BQRO_PROJECT=example-project', 'REVIEW_BASE=main'):
            self.assertIn(line, layer.splitlines())
        self.assertNotIn('CODE_SEARCH_GH_OWNER', (self.root / 'local/setup-paths.env').read_text())
        self.assertEqual(sorted(path.name for path in (self.home / '.claude/skills').iterdir()), ['conventions'])
        self.assertIn('docs', json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers'])
        doctor = json.loads(self.setup('doctor').stdout)
        self.assertEqual(doctor['preset']['current'], 'unchanged')
        self.setup('--components', 'rules', '--apply')
        self.assertEqual(self.effective()['BQRO_PROJECT'], 'example-project', 'an update without --preset keeps its values')

    def test_preset_with_an_inline_secret_is_refused_before_any_write(self) -> None:
        preset = self.home / 'bad.toml'
        preset.write_text('[kit]\nBQRO_PROJECT = "example-project?token=abc"\n')
        result = self.setup('--preset', str(preset), '--apply', code=2)
        self.assertIn('looks like an inline secret', result.stderr)
        preset.write_text('[mcp.servers.docs]\ncommand = "server"\nenv = { API_KEY = "abc" }\n')
        self.assertIn('inline secrets', self.setup('--preset', str(preset), '--apply', code=2).stderr)
        self.assertFalse(self.root.exists())

    def test_unreachable_gh_preset_falls_back_with_one_line(self) -> None:
        result = self.setup('--preset', 'gh:example-org/agent-kit-preset', '--components', 'rules')
        self.assertIn('agent-setup: preset gh:example-org/agent-kit-preset unavailable: gh is not installed', result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def test_doctor_fails_when_nothing_is_installed(self) -> None:
        result = self.setup('doctor', code=1)
        self.assertIn('not installed at', result.stderr)

    def test_agent_bodies_fill_per_host_and_follow_subagent_resume_max(self) -> None:
        self.link('jq')
        self.host_cli('claude')
        self.host_cli('codex')
        (self.root / 'local').mkdir(parents=True)
        (self.root / 'local/kit.env').write_text('SUBAGENT_RESUME_MAX=150000\n')
        codex = self.home / '.codex'
        self.setup('--hosts', 'claude', 'codex', '--components', 'rules', 'roles', '--apply')
        engineer = (self.home / '.claude/agents/engineer.md').read_text()
        self.assertNotIn('{{', engineer)
        self.assertIn(f'`{self.home}/.claude/CLAUDE.md`', engineer)
        self.assertIn(f'{self.root}/local/<repo>-', engineer)
        self.assertIn('below 150K context', engineer)
        rules = (self.root / 'state/rendered/claude-host-rules.md').read_text()
        self.assertIn('below 150K context', rules)
        self.assertIn(str(self.root / 'state/rendered/claude-host-rules.md'), (self.home / '.claude/CLAUDE.md').read_text())
        reviewer = tomllib.loads((codex / 'agents/cross-reviewer.toml').read_text())['developer_instructions']
        self.assertNotIn('{{', reviewer)
        self.assertIn(f'{self.root}/skills/review-rubric/references/correctness.md', reviewer)

    def effective(self) -> dict[str, str]:
        """The overlay as the installed hooks read it (kit_env over its layers), in the fake home."""
        code = ('import json, sys; sys.path.insert(0, sys.argv[1]); from kit_env import kit_env; '
                'print(json.dumps(kit_env(sys.argv[2])))')
        result = subprocess.run([sys.executable, '-c', code, str(SOURCE / 'hooks/lib'), str(self.root / 'local/setup-paths.env')],
                                capture_output=True, text=True, env={'HOME': str(self.home), 'PATH': str(self.shim)}, check=True)
        return json.loads(result.stdout)

    def test_a_preset_applies_over_an_earlier_install_and_a_changed_one_reapplies(self) -> None:
        self.setup('--components', 'rules', 'roles', '--hosts', 'claude', '--apply')
        preset = self.home / 'team.toml'
        preset.write_text('[kit]\nCODE_SEARCH_GH_OWNER = "a-org"\nCODE_DIRS_JSON = ["~/Code"]\n\n'
                          '[roles.cross-reviewer]\neffort = "high"\n')
        self.setup('--preset', str(preset), '--components', 'rules', 'roles', '--hosts', 'claude', '--apply')
        values = self.effective()
        self.assertEqual((values['CODE_SEARCH_GH_OWNER'], values['CODE_DIRS_JSON']), ('a-org', '["~/Code"]'))
        preset.write_text('[kit]\nCODE_SEARCH_GH_OWNER = "b-org"\n\n[roles.cross-reviewer]\neffort = "xhigh"\n')
        self.setup('--preset', str(preset), '--components', 'rules', 'roles', '--hosts', 'claude', '--apply')
        self.assertEqual(self.effective()['CODE_SEARCH_GH_OWNER'], 'b-org')
        self.assertEqual(tomllib.loads((self.root / 'roles.toml').read_text())['roles']['cross-reviewer']['effort'], 'xhigh')
        self.setup('--github-owner', 'my-org', '--components', 'rules', 'roles', '--hosts', 'claude', '--apply')
        self.setup('--preset', str(preset), '--components', 'rules', 'roles', '--hosts', 'claude', '--apply')
        self.assertEqual(self.effective()['CODE_SEARCH_GH_OWNER'], 'my-org', 'your own answer wins over the preset')

    def test_your_kit_env_wins_over_a_preset(self) -> None:
        (self.root / 'local').mkdir(parents=True)
        (self.root / 'local/kit.env').write_text('REVIEW_BASE=develop\nGIT_AUTHOR=Me <me@example.com>\n')
        preset = self.home / 'team.toml'
        preset.write_text('[kit]\nREVIEW_BASE = "main"\nGIT_AUTHOR = "Team Bot <bot@example.com>"\nBQRO_PROJECT = "team-project"\n')
        self.setup('--preset', str(preset), '--components', 'rules', '--apply')
        values = self.effective()
        self.assertEqual((values['REVIEW_BASE'], values['GIT_AUTHOR'], values['BQRO_PROJECT']),
                         ('develop', 'Me <me@example.com>', 'team-project'))

    def test_a_preset_root_with_a_tilde_does_not_stop_owner_detection(self) -> None:
        (self.home / 'Code/repo/.git').mkdir(parents=True)
        (self.home / 'Code/repo/.git/config').write_text('[remote "origin"]\n\turl = git@github.com:example-org/repo.git\n')
        preset = self.home / 'team.toml'
        preset.write_text('[kit]\nCODE_DIRS_JSON = ["~/Code", "~/missing"]\n')
        result = self.setup('--preset', str(preset), '--components', 'rules')
        self.assertIn('GitHub owner example-org (from origin remotes)', result.stdout)

    def test_preset_secret_check_matches_token_formats_not_words(self) -> None:
        preset = self.home / 'team.toml'
        for text in ('[kit]\nBQRO_PROJECT = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\n',
                     '[kit]\nREVIEW_BASE = "sk-ant-api03-abcdefghijklmnop"\n',
                     '[mcp.servers.x]\nurl = "https://mcp.example.com/mcp/ghp_abcdefghijklmnopqrstuvwxyz0123456789"\n',
                     '[mcp.servers.x]\ncommand = "npx"\nargs = ["-y", "server", "--pat", "github_pat_11ABCDEFG"]\n',
                     '[kit]\nSUBAGENT_RESUME_MAX = "abc"\n'):
            preset.write_text(text)
            self.setup('--preset', str(preset), '--components', 'rules', code=2)
        for text in ('[kit]\nCODE_DIRS_JSON = ["~/src/auth-tokens"]\n', '[kit]\nGIT_AUTHOR = "Secretariat Bot <bot@example.com>"\n'):
            preset.write_text(text)
            self.setup('--preset', str(preset), '--components', 'rules')

    def test_interactive_yes_takes_every_default_and_applies_without_a_terminal(self) -> None:
        self.setup('--interactive', '--yes', '--components', 'rules')
        self.assertTrue((self.root / '.install-state/current.json').is_file())

    def test_an_existing_server_of_the_same_name_is_a_collision_in_the_summary(self) -> None:
        self.host_cli('claude')
        (self.home / '.claude').mkdir()
        (self.home / '.claude/mcp.json').write_text(json.dumps({'mcpServers': {'docs': {'command': 'mine'}}}))
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps({'mcpServers': {'docs': {'url': 'https://mcp.example.com/mcp'}}}))
        flags = ('--hosts', 'claude', '--components', 'mcp', '--mcp-catalog', str(catalog))
        result = self.setup(*flags)
        self.assertIn(f'{self.home / ".claude/mcp.json"} holds your own content (blocks apply)', result.stdout)
        self.assertIn('apply blocked: ', self.setup(*flags, '--apply', code=2).stderr)
        self.setup(*flags, '--collision', 'skip', '--apply')
        self.assertEqual(json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers'], {'docs': {'command': 'mine'}})
        self.setup(*flags, '--collision', 'backup', '--apply')
        self.assertEqual(json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers']['docs']['type'], 'http')

    def test_a_host_installed_after_a_no_host_install_is_set_up_on_rerun(self) -> None:
        self.setup('--components', 'rules', '--apply')
        self.host_cli('claude')
        self.setup('--apply')
        self.assertTrue((self.home / '.claude/CLAUDE.md').is_file())

    def test_hosts_are_detected_by_each_cli_name_their_installers_use(self) -> None:
        self.host_cli('cursor-agent')
        self.assertEqual(json.loads(self.setup('--json').stdout)['hosts'], ['cursor'])

    def test_installed_kit_renders_codex_into_the_recorded_root(self) -> None:
        self.host_cli('codex')
        custom = self.home / 'custom-codex'
        self.setup('--hosts', 'codex', '--host-root', str(custom), '--components', 'rules', '--apply')
        (custom / 'AGENTS.md').unlink()
        environment = {'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp)}
        render = [sys.executable, str(self.root / 'bin/agent-kit'), 'render', '--host']
        subprocess.run([*render, 'codex', '--components', 'rules'], capture_output=True, text=True, env=environment, check=True)
        self.assertTrue((custom / 'AGENTS.md').is_file())
        self.assertFalse((self.home / '.codex').exists(), 'rendered into the default root instead')
        refused = subprocess.run([*render, 'cursor', '--components', 'rules'], capture_output=True, text=True, env=environment)
        self.assertIn('has no cursor target', refused.stderr)

    def test_agent_body_names_the_rules_file_under_codex_home(self) -> None:
        environment = {'HOME': str(self.home), 'PATH': str(self.shim), 'CODEX_HOME': str(self.home / 'cx'),
                       'AGENT_KIT_DIR': str(SOURCE), 'KIT_ENV': '/dev/null'}
        result = subprocess.run([sys.executable, str(SOURCE / 'bin/agent-kit'), 'agent-body', 'engineer', '--provider', 'openai'],
                                capture_output=True, text=True, env=environment, check=True)
        self.assertIn(f'{self.home}/cx/AGENTS.md', result.stdout)

    def test_a_root_named_by_the_host_variable_is_recorded_for_later_runs(self) -> None:
        self.host_cli('claude')
        self.host_cli('codex')
        roots = {'claude': self.home / 'claude-profile', 'codex': self.home / 'codex-profile'}
        flags = ('--hosts', 'claude', 'codex', '--components', 'rules', '--apply')
        self.setup(*flags, env={'CLAUDE_CONFIG_DIR': str(roots['claude']), 'CODEX_HOME': str(roots['codex'])})
        recorded = json.loads((self.root / '.install-state/current.json').read_text())['configuration']['host_roots']
        self.assertEqual(recorded, {host: str(root) for host, root in roots.items()})
        # A later shell without either variable: render and setup still target the installed roots.
        environment = {'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp)}
        for host, rules in (('claude', 'CLAUDE.md'), ('codex', 'AGENTS.md')):
            (roots[host] / rules).unlink()
            subprocess.run([sys.executable, str(self.root / 'bin/agent-kit'), 'render', '--host', host, '--components', 'rules'],
                           capture_output=True, text=True, env=environment, check=True)
            self.assertTrue((roots[host] / rules).is_file(), f'{host} rendered into another root')
        self.setup(*flags)
        for host in roots:
            self.assertFalse((self.home / f'.{host}').exists(), f'{host} set up in the default root instead')

    def test_agent_text_names_the_rules_file_under_the_installed_root(self) -> None:
        self.link('jq')
        self.host_cli('claude')
        custom = self.home / 'claude-profile'
        self.setup('--hosts', 'claude', '--host-root', str(custom), '--components', 'rules', 'roles', '--apply')
        rules = f'`{custom}/CLAUDE.md`'
        self.assertIn(rules, (custom / 'agents/engineer.md').read_text())
        shutil.rmtree(custom / 'agents')
        environment = {'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp)}
        kit = [sys.executable, str(self.root / 'bin/agent-kit')]
        subprocess.run([*kit, 'render', '--host', 'claude', '--components', 'roles'],
                       capture_output=True, text=True, env=environment, check=True)
        self.assertIn(rules, (custom / 'agents/engineer.md').read_text(), 'render filled the default root')
        body = subprocess.run([*kit, 'agent-body', 'engineer', '--host', 'claude'],
                              capture_output=True, text=True, env=environment, check=True).stdout
        self.assertIn(rules, body, 'agent-body ignored the recorded root')
        named = self.home / 'named-root'
        body = subprocess.run([*kit, 'agent-body', 'engineer', '--host', 'codex'], capture_output=True, text=True,
                              env={**environment, 'AGENT_KIT_HOST_ROOT': str(named)}, check=True).stdout
        self.assertIn(f'{named}/AGENTS.md', body, 'agent-body ignored AGENT_KIT_HOST_ROOT')

    def test_a_key_a_changed_preset_drops_is_not_kept_as_your_answer(self) -> None:
        (self.root / 'local').mkdir(parents=True)
        (self.root / 'local/kit.env').write_text('CODE_SEARCH_GH_OWNER=my-org\n')
        preset = self.home / 'team.toml'
        preset.write_text('[kit]\nCODE_SEARCH_GH_OWNER = "a-org"\nCODE_DIRS_JSON = ["~/Code"]\n\n'
                          '[skills]\ninclude = ["conventions"]\n')
        flags = ('--components', 'rules', 'skills', '--hosts', 'claude', '--apply')
        self.setup('--preset', str(preset), *flags)
        preset.write_text('[kit]\nREVIEW_BASE = "main"\n')
        self.setup('--preset', str(preset), *flags)
        paths = (self.root / 'local/setup-paths.env').read_text()
        self.assertNotIn('a-org', paths, 'the dropped preset owner became your own answer')
        self.assertNotIn('~/Code', paths, 'the dropped preset roots became your own answer')
        self.assertEqual(self.effective()['CODE_SEARCH_GH_OWNER'], 'my-org')
        self.assertNotEqual(sorted(path.name for path in (self.home / '.claude/skills').iterdir()), ['conventions'],
                            'the dropped preset skill selection was kept')

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InstallerUxTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': result.testsRun, 'successful': result.wasSuccessful(),
                                'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
