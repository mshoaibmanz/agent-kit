"""Validate manifests, schemas and canonical generated packages without host configuration."""

from pathlib import Path
import ast
import json
import os
import subprocess
import sys
import tomllib

root = Path(__file__).resolve().parents[1]
failures = []
checked = 0
for folder in ('bin', 'hooks', 'agents', 'commands', 'rules', 'skills', 'plugins', 'hosts', 'mcp', '.claude-plugin'):
    for path in (root / folder).rglob('*'):
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
        except (ValueError, SyntaxError) as error:
            failures.append(str(path.relative_to(root)))
market = json.loads((root / '.claude-plugin/marketplace.json').read_text())
if sorted(row['name'] for row in market['plugins']) != sorted(path.name for path in (root / 'plugins').iterdir()):
    failures.append('marketplace plugin matrix')
if any((root / 'plugins').rglob('critic.md')):
    failures.append('retired critic agent')
result = subprocess.run([sys.executable, str(root / 'scripts/build_plugins.py'), '--check'],
                        capture_output=True, text=True, env={**os.environ, 'AGENT_KIT_DIR': str(root)})
if result.returncode:
    failures.append('generated package validation failed')
    sys.stderr.write(result.stderr)
sys.stdout.write(json.dumps({'checked_files': checked, 'failures': failures}, indent=2) + '\n')
raise SystemExit(bool(failures))
