"""The installer UX matrix: each case runs agent-setup in a fake home whose PATH is a folder of real
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
        self.assertFalse((self.home / '.claude').exists(), 'matrix 1: wrote a host config for a host that is not installed')
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
        self.assertFalse(doctor['native_auth']['claude']['logged_in'], 'matrix 3: doctor checked only the config dir')
        (self.home / '.claude.json').write_text(json.dumps({'oauthAccount': {'emailAddress': 'user@example.com'}}))
        self.assertIn('claude: claude, signed in', self.setup('--components', 'rules').stdout)
        self.assertTrue(json.loads(self.setup('doctor').stdout)['native_auth']['claude']['logged_in'])

    def test_missing_jq_blocks_apply_up_front_with_this_os_fix(self) -> None:
        self.host_cli('claude')
        result = self.setup('--components', 'hooks')
        hint = 'brew install jq' if sys.platform == 'darwin' else 'install'
        self.assertIn('jq missing, required for hooks (blocks apply). Fix: ', result.stdout)
        self.assertIn(hint, result.stdout.split('jq missing')[1].splitlines()[0])
        self.setup('--components', 'hooks', '--apply', code=2)
        self.assertFalse(self.root.exists())

    def test_soft_requirements_skip_their_feature(self) -> None:
        self.link('jq')
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps({'mcpServers': {'docs': {'command': 'npx', 'args': ['-y', 'example-docs-server']}}}))
        result = self.setup('--components', 'skills', 'mcp', 'data-wrappers', '--skills', 'code-search',
                            '--mcp-catalog', str(catalog), '--hosts', 'claude', '--apply')
        skipped = result.stdout.split('Skipped:')[1]
        for line in ('GitHub code search off: gh missing', 'MCP server docs off: npx missing',
                     'BigQuery read wrapper off: bq missing', 'BigQuery read wrapper login off: gcloud missing'):
            self.assertIn(line, skipped)
        store = 'macOS Keychain' if Path('/usr/bin/security').exists() else 'secret-tool' if shutil.which('secret-tool') else 'env vars'
        self.assertIn(store, result.stdout, 'matrix 4f: the summary says where wrappers read credentials')

    def test_empty_mcp_catalog_is_named_not_silent(self) -> None:
        result = self.setup('--components', 'mcp', '--hosts', 'claude')
        self.assertIn('no MCP servers configured (add them in mcp/servers.json or a preset)', result.stdout)

    def test_code_search_without_gh_is_one_line(self) -> None:
        environment = {'HOME': str(self.home), 'PATH': str(self.shim), 'AGENT_KIT_DIR': str(SOURCE), 'KIT_ENV': '/dev/null'}
        result = subprocess.run([sys.executable, str(SOURCE / 'skills/code-search/scripts/code_search.py'), 'repos', 'x'],
                                capture_output=True, text=True, env=environment, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr, 'matrix 4a')
        self.assertIn('gh is not installed: GitHub search is off', result.stderr)

    def test_collision_is_found_in_preflight_and_skip_leaves_it(self) -> None:
        self.link('jq')
        self.host_cli('claude')
        mine = self.home / '.claude/agents/engineer.md'
        mine.parent.mkdir(parents=True)
        mine.write_text('my own engineer\n')
        result = self.setup('--components', 'roles')
        self.assertIn(f'{mine} holds your own content (blocks apply)', result.stdout, 'matrix 6b: found in preflight')
        self.setup('--components', 'roles', '--apply', code=2)
        self.assertFalse(self.root.exists())
        result = self.setup('--components', 'roles', '--collision', 'skip', '--apply')
        self.assertIn('your own content, left in place', result.stdout)
        self.assertEqual(mine.read_text(), 'my own engineer\n')
        self.assertTrue((self.home / '.claude/agents/researcher.md').is_file())

    def test_local_preset_fills_answers_and_is_recorded(self) -> None:
        preset = self.home / 'team preset.toml'
        preset.write_text('[kit]\nGITHUB_OWNER = "example-org"\nZOEKT_URL = "https://zoekt.example.com"\n'
                          'BQRO_PROJECT = "example-project"\nREVIEW_BASE = "main"\n\n'
                          '[mcp.servers.docs]\nurl = "https://mcp.example.com/mcp"\n\n[skills]\ninclude = ["conventions"]\n')
        result = self.setup('--preset', str(preset), '--components', 'rules', 'skills', 'mcp', '--hosts', 'claude', '--apply')
        self.assertIn(f'preset {preset} (sha256 ', result.stdout)
        self.assertIn('GitHub org check for example-org: gh is missing', result.stdout)
        overlay = (self.root / 'local/setup-paths.env').read_text()
        for line in ('CODE_SEARCH_GH_OWNER=example-org', 'CODE_SEARCH_ZOEKT_URL=https://zoekt.example.com',
                     'BQRO_PROJECT=example-project', 'REVIEW_BASE=main'):
            self.assertIn(line, overlay.splitlines())
        self.assertEqual(sorted(path.name for path in (self.home / '.claude/skills').iterdir()), ['conventions'])
        self.assertIn('docs', json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers'])
        doctor = json.loads(self.setup('doctor').stdout)
        self.assertEqual(doctor['preset']['current'], 'unchanged')
        self.setup('--components', 'rules', '--apply')
        self.assertIn('BQRO_PROJECT=example-project', (self.root / 'local/setup-paths.env').read_text(),
                      'an update without --preset keeps its values')

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

    def test_unknown_placeholder_is_refused(self) -> None:
        sys.path.insert(0, str(SOURCE / 'bin/lib'))
        from hosts import fill

        self.assertEqual(fill('{{KIT_DIR}} {{OVERLAY_DIR}}', '/kit'), '/kit /kit/local')
        self.assertEqual(fill('{{RULES_FILE}}', '/kit', 'codex', '/codex'), '/codex/AGENTS.md')
        with self.assertRaises(SystemExit) as raised:
            fill('{{NO_SUCH_PATH}}', '/kit', 'claude')
        self.assertIn('unknown placeholder {{NO_SUCH_PATH}}', str(raised.exception))
        with self.assertRaises(SystemExit):
            fill('{{RULES_FILE}}', '/kit')  # host-neutral text cannot name a host's file


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InstallerUxTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': result.testsRun, 'successful': result.wasSuccessful(),
                                'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
