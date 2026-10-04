"""Exercise generated plugins with synthetic homes and no provider calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(os.environ.get('PLUGIN_TEST_SOURCE', Path(__file__).resolve().parents[1]))

class PluginPortabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixtures = Path(os.environ.get('TMPDIR', str(Path(__file__).resolve().parents[1] / '.test-fixtures')))
        fixtures.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='plugin-portability-', dir=fixtures)
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / 'source'
        cls.source.mkdir()
        for name in ('bin', 'hooks', 'git-hooks', 'agents', 'rules', 'hosts', 'mcp', 'references', 'skills', 'commands', '.claude-plugin', 'scripts', 'LICENSES'):
            shutil.copytree(SOURCE / name, cls.source / name, symlinks=True)
        for name in ('roles.toml', 'kit.env.example', 'LICENSE', 'NOTICE'):
            shutil.copy2(SOURCE / name, cls.source / name)
        for path in cls.source.rglob('*'):
            if path.is_symlink():
                continue
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        (cls.source / 'plugins').mkdir()
        cls.env = {**os.environ, 'HOME': str(cls.root / 'home'), 'TMPDIR': str(cls.root),
                   'PYTHONDONTWRITEBYTECODE': '1', 'AGENT_KIT_DIR': str(cls.source),
                   'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null'}
        cls.env.pop('KIT_ENV', None)
        cls.env.pop('CLAUDE_CONFIG_DIR', None)
        cls.env.pop('AGENT_HOST', None)
        result = subprocess.run([sys.executable, str(cls.source / 'scripts/build_plugins.py')],
                                env=cls.env, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def run_command(self, command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, env=env or self.env, capture_output=True, text=True, timeout=60)

    def test_relocated_standalone_packages_preserve_license_artifacts(self) -> None:
        expected = {name: (self.source / name).read_bytes() for name in ('LICENSE', 'NOTICE')}
        expected.update({str(path.relative_to(self.source)): path.read_bytes()
                         for path in (self.source / 'LICENSES').rglob('*') if path.is_file()})
        relocated = self.root / 'relocated packages'
        relocated.mkdir()
        plugins = sorted((self.source / 'plugins').iterdir())
        names = {entry['name'] for entry in json.loads((self.source / '.claude-plugin/marketplace.json').read_text())['plugins']}
        self.assertEqual({path.name for path in plugins}, names)
        for plugin in plugins:
            package = relocated / plugin.name
            shutil.copytree(plugin, package, symlinks=True)
            for layer in (Path(), Path('kit')):
                for name, data in expected.items():
                    with self.subTest(plugin=plugin.name, layer=str(layer), artifact=name):
                        target = package / layer / name
                        self.assertTrue(target.is_file(), str(target))
                        self.assertFalse(target.is_symlink(), str(target))
                        self.assertEqual(target.read_bytes(), data)

    def test_packaged_setup_preview_and_roles_resolve_kit(self) -> None:
        for name in ('workflow', 'terminal-signals', 'prod-data'):
            with self.subTest(plugin=name):
                plugin = self.source / 'plugins' / name
                result = self.run_command([str(plugin / 'bin/agent-setup'), '--hosts', 'claude',
                    '--components', 'rules', '--root-dir', str(self.root / name / 'installed kit'),
                    '--host-root', str(self.root / name / 'host')])
                self.assertEqual(result.returncode, 0, result.stderr)
                result = self.run_command([str(plugin / 'bin/agent-kit'), 'roles'])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((plugin / 'kit/commands/bind.md').is_file())
                self.assertTrue((plugin / 'kit/.claude-plugin/components.json').is_file())
                self.assertTrue((plugin / 'kit/state/rendered/roles-claude.sh').is_file())

    def test_bundled_setup_template_dependencies(self) -> None:
        plugin = self.source / 'plugins/workflow'
        result = self.run_command([str(plugin / 'kit/bin/agent-setup'), '--hosts', 'claude',
            '--components', 'rules', '--root-dir', str(self.root / 'bundled setup root'),
            '--host-root', str(self.root / 'bundled setup host')])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workflow_bind_executes_without_legacy_install(self) -> None:
        plugin = self.source / 'plugins/workflow'
        body = (plugin / 'commands/bind.md').read_text().replace('${CLAUDE_PLUGIN_ROOT}', str(plugin))
        command = re.search(r'With no arguments: run `([^`]+)`', body).group(1)
        env = dict(self.env)
        env.pop('AGENT_KIT_DIR', None)
        env.pop('CLAUDE_PLUGIN_ROOT', None)
        result = self.run_command(['bash', '-c', command], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (plugin / 'commands').glob('*.md'):
            content = path.read_text()
            self.assertNotIn('~/.agents/', content)
            self.assertNotIn('~/.claude/commands/', content)

    def overlay_values(self, kit: Path, env: dict[str, str]) -> tuple[list[str], list[str]]:
        script = '. "$1/hooks/lib/hook-io"; kit_env; printf "%s\\n" "$GIT_AUTHOR" "$TEST_RUNNER" "$AGENT_WORK_ROOT"'
        bash = self.run_command(['bash', '-c', script, 'overlay-test', str(kit)], env)
        self.assertEqual(bash.returncode, 0, bash.stderr)
        script = 'import json,sys; sys.path.insert(0,sys.argv[1]); from kit_env import kit_env; x=kit_env(); print(json.dumps([x[k] for k in ("GIT_AUTHOR","TEST_RUNNER","AGENT_WORK_ROOT")]))'
        python = self.run_command([sys.executable, '-c', script, str(kit / 'hooks/lib')], env)
        self.assertEqual(python.returncode, 0, python.stderr)
        return bash.stdout.splitlines(), json.loads(python.stdout)

    def test_plugin_bash_and_python_reuse_existing_overlay(self) -> None:
        kit = self.source / 'plugins/terminal-signals/kit'
        config = self.root / 'custom Claude configuration'
        (config / 'local').mkdir(parents=True, exist_ok=True)
        (config / 'local/kit.env').write_text('GIT_AUTHOR=Fixture Author\nTEST_RUNNER=fixture-runner\nAGENT_WORK_ROOT=legacy-work\n')
        env = {**self.env, 'AGENT_KIT_DIR': str(kit), 'CLAUDE_CONFIG_DIR': str(config)}
        self.assertEqual(self.overlay_values(kit, env), (['Fixture Author','fixture-runner','legacy-work'],) * 2)
        env['KIT_ENV'] = '/dev/null'
        self.assertEqual(self.overlay_values(kit, env), (['','',''],) * 2)

    def test_generated_paths_layer_preserves_legacy_then_prefers_local_user_layer(self) -> None:
        kit = self.root / 'layered kit'
        shutil.copytree(self.source / 'hooks', kit / 'hooks')
        (kit / 'local').mkdir()
        (kit / 'local/setup-paths.env').write_text('AGENT_WORK_ROOT=chosen-work\n')
        config = self.root / 'layered Claude'
        (config / 'local').mkdir(parents=True)
        (config / 'local/kit.env').write_text('GIT_AUTHOR=Legacy Author\nTEST_RUNNER=legacy-runner\nAGENT_WORK_ROOT=legacy-work\n')
        env = {**self.env, 'AGENT_KIT_DIR': str(kit), 'CLAUDE_CONFIG_DIR': str(config),
               'KIT_ENV': str(kit / 'local/setup-paths.env')}
        self.assertEqual(self.overlay_values(kit, env), (['Legacy Author','legacy-runner','chosen-work'],) * 2)
        (kit / 'local/kit.env').write_text('GIT_AUTHOR=Local Author\nTEST_RUNNER=local-runner\n')
        self.assertEqual(self.overlay_values(kit, env), (['Local Author','local-runner','chosen-work'],) * 2)

    def test_generator_check_supports_read_only_source(self) -> None:
        for path in self.source.rglob('*'):
            if path.is_file() and not path.is_symlink() and 'plugins' not in path.relative_to(self.source).parts:
                path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
        result = self.run_command([sys.executable, str(self.source / 'scripts/build_plugins.py'), '--check'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['drift'], [])

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PluginPortabilityTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': suite.countTestCases(), 'successful': result.wasSuccessful(),
        'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(not result.wasSuccessful())
