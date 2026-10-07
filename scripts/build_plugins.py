"""Generate standalone Claude plugin packages from canonical kit sources."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin/lib'))
from hosts import SUBAGENT_RESUME_MAX, install_text  # noqa: E402
MATRIX = json.loads((ROOT / '.claude-plugin/components.json').read_text())
HOOKS = {name: set(hooks) for name, hooks in MATRIX['hook_plugins'].items()}
SKILLS = MATRIX['skill_plugins']
MARKETPLACE = ROOT / '.claude-plugin/marketplace.json'
# Every marketplace entry installs plugins/<name> from the dist branch of this repository, which the
# dist workflow publishes (scripts/publish_dist.sh). --marketplace writes these into marketplace.json.
REPO_URL = 'https://github.com/mshoaibmanz/agent-kit.git'
DIST_REF = 'dist'


def plugin_source(name: str) -> dict[str, str]:
    return {'source': 'git-subdir', 'url': REPO_URL, 'path': f'plugins/{name}', 'ref': DIST_REF}


def marketplace_text() -> str:
    """marketplace.json with each plugin's source as plugin_source gives it."""
    market = json.loads(MARKETPLACE.read_text())
    market['plugins'] = [{'name': row['name'], 'source': plugin_source(row['name']),
                          **{key: value for key, value in row.items() if key not in ('name', 'source')}}
                         for row in market['plugins']]
    return json.dumps(market, indent=2, ensure_ascii=False) + '\n'


def files(root: Path) -> dict[str, bytes | str]:
    return {str(path.relative_to(root)): path.readlink().as_posix() if path.is_symlink() else path.read_bytes()
            for path in root.rglob('*') if path.is_file() or path.is_symlink()}


def copy_source_code(source: str | Path, target: str | Path) -> str:
    path = Path(source)
    mode = path.stat().st_mode & 0o777
    shutil.copyfile(path, target)
    Path(target).chmod((0o755 if mode & 0o111 else 0o644) & (mode | 0o600))
    return str(target)


def copy_licenses(destination: Path) -> None:
    for name in ('LICENSE', 'NOTICE'):
        copy_source_code(ROOT / name, destination / name)
    shutil.copytree(ROOT / 'LICENSES', destination / 'LICENSES', copy_function=copy_source_code)


def build(destination: Path) -> None:
    os.environ['AGENT_KIT_DIR'] = str(ROOT)
    loader = importlib.machinery.SourceFileLoader('public_plugin_kit', str(ROOT / 'bin/agent-kit'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    api = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = api
    loader.exec_module(api)
    market = json.loads((ROOT / '.claude-plugin/marketplace.json').read_text())
    registry = json.loads((ROOT / 'hooks/registry.json').read_text())
    core_skills = sorted(path.name for path in (ROOT / 'skills').iterdir()
        if path.is_dir() and path.name not in {name for names in SKILLS.values() for name in names})
    for descriptor in market['plugins']:
        name = descriptor['name']
        plugin = destination / name
        (plugin / '.claude-plugin').mkdir(parents=True)
        copy_licenses(plugin)
        # No version: a fixed number would pin installs; without one an update follows the dist commit.
        manifest = {'name': name, 'description': descriptor['description'],
                    'license': 'MIT', 'author': market['owner']}
        (plugin / '.claude-plugin/plugin.json').write_text(json.dumps(manifest, indent=2) + '\n')
        chosen = core_skills if name == 'skills-core' else SKILLS.get(name, [])
        if name in HOOKS or chosen or name == 'workflow':
            kit = plugin / 'kit'
            kit.mkdir()
            copy_licenses(kit)
            for folder in ('bin', 'hooks', 'git-hooks', 'agents', 'rules', 'hosts', 'mcp', 'references', 'commands'):
                shutil.copytree(ROOT / folder, kit / folder, copy_function=copy_source_code, symlinks=True,
                    ignore=shutil.ignore_patterns('tests', '__pycache__', '*.pyc', 'local', 'state'))
            copy_source_code(ROOT / 'kit.env.example', kit / 'kit.env.example')
            (kit / '.claude-plugin').mkdir()
            copy_source_code(ROOT / '.claude-plugin/components.json', kit / '.claude-plugin/components.json')
            (plugin / 'bin').mkdir()
            for entry in (ROOT / 'bin').iterdir():
                if entry.is_file() and os.access(entry, os.X_OK):
                    (plugin / 'bin' / entry.name).symlink_to('../kit/bin/' + entry.name)
            role_text = (ROOT / 'roles.toml').read_text().replace('[hosts.claude]\n', '[hosts.claude]\nnative_agents = false\n')
            (kit / 'roles.toml').write_text(role_text)
            for skill in chosen:
                shutil.copytree(ROOT / 'skills' / skill, kit / 'skills' / skill, copy_function=copy_source_code, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            copy_source_code(ROOT / 'hooks/lib/state', kit / 'hooks/lib/state')
            if not (kit / 'skills/review-rubric').exists():
                shutil.copytree(ROOT / 'skills/review-rubric', kit / 'skills/review-rubric', copy_function=copy_source_code)
            (kit / 'state/rendered').mkdir(parents=True)
            plugin_roles = api.load_roles()
            plugin_roles.hosts['claude']['native_agents'] = False
            (kit / 'state/rendered/roles-claude.sh').write_text(api.roles_sh(plugin_roles, 'claude').replace(str(ROOT / 'roles.toml'), 'roles.toml'))
        if name in HOOKS:
            selected = []
            for entry in registry:
                words = entry['command'].split()
                hook = Path(words[0]).name
                if 'claude' not in entry['hosts'] or hook not in HOOKS[name]:
                    continue
                row = dict(entry)
                arguments = ' ' + ' '.join(words[1:]) if len(words) > 1 else ''
                if hook == 'bash-guards':
                    arguments = ' db' if name == 'prod-data' else ' git'
                row['command'] = 'env AGENT_KIT_DIR="${CLAUDE_PLUGIN_ROOT}/kit" "${CLAUDE_PLUGIN_ROOT}/kit/hooks/' + hook + '"' + arguments
                selected.append(row)
            (plugin / 'hooks').mkdir()
            (plugin / 'hooks/hooks.json').write_text(json.dumps({'hooks': api.render_hooks(selected, 'claude')}, indent=2) + '\n')
        for skill in chosen:
            shutil.copytree(ROOT / 'skills' / skill, plugin / 'skills' / skill, copy_function=copy_source_code,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            doc = plugin / 'skills' / skill / 'SKILL.md'
            body = install_text(doc.read_text(), '${CLAUDE_PLUGIN_ROOT}/kit')
            doc.write_text(body)
        if name == 'auto-review':
            # Filled for the plugin's own kit; the default resume limit, never this machine's kit.env.
            agents = api.expected_agents(api.load_roles(), 'claude', kit='${CLAUDE_PLUGIN_ROOT}/kit',
                                         host_root='~/.claude', resume=SUBAGENT_RESUME_MAX)
            for filename, text in agents.items():
                path = plugin / 'agents' / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(install_text(text, '${CLAUDE_PLUGIN_ROOT}/kit', export=False))
        if name == 'workflow':
            shutil.copytree(ROOT / 'commands', plugin / 'commands', copy_function=copy_source_code)
            for command in (plugin / 'commands').glob('*.md'):
                command.write_text(command.read_text().replace('${AGENT_KIT_DIR}', '${CLAUDE_PLUGIN_ROOT}/kit'))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true',
                        help='compare a fresh build with --out, and marketplace.json with its sources; write nothing')
    parser.add_argument('--marketplace', action='store_true',
                        help="write each marketplace.json entry's source, then exit")
    # main does not track plugins/: CI builds it and commits it to the dist branch (scripts/publish_dist.sh).
    parser.add_argument('--out', type=Path, default=ROOT / 'plugins', help='the packages folder (default: plugins/)')
    args = parser.parse_args()
    if args.marketplace:
        MARKETPLACE.write_text(marketplace_text())
        return 0
    out = args.out.resolve()
    with tempfile.TemporaryDirectory(prefix='public-plugin-build-') as temporary:
        desired = Path(temporary) / 'plugins'
        build(desired)
        if args.check:
            expected, actual = files(desired), files(out) if out.is_dir() else {}
            drift = sorted(key for key in expected.keys() | actual.keys() if expected.get(key) != actual.get(key))
            if MARKETPLACE.read_text() != marketplace_text():
                drift.append(str(MARKETPLACE.relative_to(ROOT)) + ' (run scripts/build_plugins.py --marketplace)')
            sys.stdout.write(json.dumps({'generated_files': len(expected), 'drift': drift}, indent=2) + '\n')
            return bool(drift)
        out.mkdir(parents=True, exist_ok=True)
        for plugin in out.iterdir():
            if plugin.is_dir():
                shutil.rmtree(plugin)
        shutil.copytree(desired, out, dirs_exist_ok=True, symlinks=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
