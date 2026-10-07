"""Validate manifests, schemas and canonical generated packages without host configuration."""

from pathlib import Path
import ast
import json
import os
import subprocess
import sys
import tempfile
import tomllib

root = Path(__file__).resolve().parents[1]
failures = []
checked = 0
build_env = {**os.environ, 'AGENT_KIT_DIR': str(root)}
# main does not track the plugin packages (CI commits them to the dist branch): build them into a scratch
# folder and check that build.
with tempfile.TemporaryDirectory(prefix='verify-release-', dir=os.environ.get('TMPDIR')) as scratch:
    plugins = Path(scratch) / 'plugins'
    built = subprocess.run([sys.executable, str(root / 'scripts/build_plugins.py'), '--out', str(plugins)],
                           capture_output=True, text=True, env=build_env)
    if built.returncode:
        failures.append('plugin package build failed')
        sys.stderr.write(built.stderr)
    plugins.mkdir(exist_ok=True)
    folders = [root / name for name in ('bin', 'hooks', 'agents', 'commands', 'rules', 'skills', 'hosts', 'mcp',
                                        '.claude-plugin')]
    for folder in [*folders, plugins]:
        for path in folder.rglob('*'):
            if not path.is_file() or path.is_symlink() or '__pycache__' in path.parts:
                continue
            try:
                if path.suffix == '.json':
                    json.loads(path.read_text())
                elif path.suffix == '.toml':
                    tomllib.loads(path.read_text())
                elif path.suffix == '.py' or (path.parent.name == 'bin' and path.read_bytes().startswith(b'#!/usr/bin/env python3')):
                    ast.parse(path.read_text())
                checked += 1
            except (ValueError, SyntaxError):
                failures.append(str(path.relative_to(plugins.parent if plugins in path.parents else root)))
    market = json.loads((root / '.claude-plugin/marketplace.json').read_text())
    if sorted(row['name'] for row in market['plugins']) != sorted(path.name for path in plugins.iterdir()):
        failures.append('marketplace plugin matrix')
    for row in market['plugins']:
        if row.get('source') != {'source': 'git-subdir', 'url': 'https://github.com/mshoaibmanz/agent-kit.git',
                                 'path': f'plugins/{row["name"]}', 'ref': 'dist'}:
            failures.append(f'marketplace source for {row["name"]} is not plugins/{row["name"]} on dist')
    if any(plugins.rglob('critic.md')):
        failures.append('retired critic agent')
    if any('version' in json.loads(path.read_text()) for path in plugins.glob('*/.claude-plugin/plugin.json')):
        failures.append('generated plugin.json carries a version')
    # A second build matches the first: the packages are a pure function of the sources.
    result = subprocess.run([sys.executable, str(root / 'scripts/build_plugins.py'), '--check', '--out', str(plugins)],
                            capture_output=True, text=True, env=build_env)
    if result.returncode:
        failures.append('generated package validation failed')
        sys.stderr.write(result.stdout + result.stderr)
sys.stdout.write(json.dumps({'checked_files': checked, 'failures': failures}, indent=2) + '\n')
raise SystemExit(bool(failures))
