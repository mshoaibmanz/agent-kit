"""Run all hooks with explicit source paths and a disposable, non-scratch home."""

from pathlib import Path
import os
import subprocess
import tempfile

source = Path(__file__).resolve().parents[1]
fixture_root = Path(os.environ.get('TMPDIR', str(source / '.test-fixtures')))
fixture_root.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='.test-home-', dir=fixture_root) as temporary:
    home = Path(temporary)
    (home / '.claude').mkdir()
    for name in ('hooks', 'bin', 'agents'):
        (home / '.claude' / name).symlink_to(source / name)
    (home / '.claude/settings.json').write_text('{"enabledPlugins": {"thermos@fixture": true}}\n')
    (home / '.claude-work').mkdir()
    (home / '.claude-work/settings.json').symlink_to(home / '.claude/settings.json')
    (home / '.claude-work/agents').symlink_to(home / '.claude/agents')
    (home / '.agents').symlink_to(source)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('GIT_CONFIG_') and key not in ('CLAUDECODE', 'AI_AGENT', 'AGENT_HOST',
              'AGENT_KIT_DIR', 'CLAUDE_OUT_ROOT', 'CLAUDE_PROJECT_DIR', 'AGENT_GIT_HOOKS', 'CI_WATCH_ACTIVE', 'CLAUDE_CONFIG_DIR')}
    env.update(HOME=str(home), KIT_ENV='/dev/null', HOOKS_DIR=str(source / 'hooks'),
               BQRO=str(source / 'bin/bqro'), RO_MYSQL=str(source / 'bin/ro-mysql'),
               LAUNCHER=str(source / 'bin/claude-launcher.zsh'), AGENT_KIT_SOURCE=str(source),
               GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run(['bash', str(source / 'hooks/tests/run-all.sh')], env=env, cwd=source)
    raise SystemExit(result.returncode)
