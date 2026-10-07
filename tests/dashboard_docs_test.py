"""agent-kit dashboard, How the kit works view: copy the source's files into a scratch kit,
add a fixture hook row, skill, command and slash command, and check that the generated view lists
each of them from its own source, that a registry row added later shows up with no code change,
and that the page still loads nothing and keeps its CSP."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard_test import Fixture, page_text  # noqa: E402
from installer_ux_test import SOURCE  # noqa: E402

HOOK = {'event': 'PreToolUse', 'matcher': 'Bash|Read', 'command': '~/.claude/hooks/demo-guard',
        'description': 'Refuses the demo command before it runs.', 'hosts': ['claude', 'codex']}
TOOL = '''#!/usr/bin/env python3
"""demo-tool: Prints the demo inventory. A second sentence the purpose leaves out."""
import argparse
ap = argparse.ArgumentParser()
sub = ap.add_subparsers()
sub.add_parser("inventory", help="list every demo item")
'''
SKILL = '''---
name: demo-skill
description: Explains the demo fixture to a reader.
hosts: [claude, codex]
---

# Demo
'''


def docs(page: str) -> str:
    start = page.index('<article id="docs" class="view">')
    return page[start:page.index('</article>', start)]


def svgs(view: str) -> list[str]:
    return re.findall(r'<svg\b.*?</svg>', view, re.S)


class DashboardDocsTests(Fixture):
    def scratch_kit(self) -> Path:
        kit = self.home / 'kit'
        # The checkout's own files, committed or not: the view is generated from what is there.
        files = subprocess.run(['git', '-C', str(SOURCE), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                               capture_output=True, text=True, check=True).stdout.split('\0')
        for name in filter(None, files):
            source, target = SOURCE / name, kit / name
            if not source.exists() and not source.is_symlink():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
        registry = kit / 'hooks/registry.json'
        registry.write_text(json.dumps([*json.loads(registry.read_text()), HOOK], indent=2))
        (kit / 'hooks/demo-guard').write_text('#!/bin/sh\nexit 0\n')
        (kit / 'skills/demo-skill').mkdir()
        (kit / 'skills/demo-skill/SKILL.md').write_text(SKILL)
        (kit / 'bin/demo-tool').write_text(TOOL)
        (kit / 'commands/demo-command.md').write_text('---\ndescription: Runs the demo flow.\n---\nDo it.\n')
        return kit

    def test_the_view_lists_a_fixture_hook_skill_and_command_from_their_sources(self) -> None:
        kit = self.scratch_kit()
        page, _ = self.dashboard(kit=kit)
        view = docs(page)
        self.assertNotIn('not read:', view)
        self.assertIn('<a class="tab" href="#docs" data-v="docs">How the kit works</a>', page)
        hook = re.search(r'<tr id="docs-hook-PreToolUse-demo-guard">.*?</tr>', view, re.S)
        assert hook is not None, 'the fixture hook row'
        self.assertIn('Refuses the demo command before it runs.', hook.group())
        self.assertIn('>advisory</span>', hook.group())
        self.assertIn(f'vscode://file{kit}/hooks/demo-guard', hook.group(), 'links to its script')
        self.assertIn('<code>Bash</code><br><code>Read</code>', hook.group(), 'one matcher per line')
        skill = re.search(r'<tr id="docs-skill-kit-demo-skill">.*?</tr>', view, re.S)
        assert skill is not None, 'the fixture skill row'
        self.assertIn('Explains the demo fixture to a reader.', skill.group())
        self.assertIn('claude, codex', skill.group())
        command = re.search(r'<tr id="docs-cmd-demo-tool">.*?</tr>', view, re.S)
        assert command is not None, 'the fixture command row'
        self.assertIn('<div class="desc">Prints the demo inventory.</div>', command.group(), 'its first sentence')
        self.assertIn('<code>inventory</code> <span class="muted">list every demo item</span>', command.group())
        self.assertIn('id="docs-slash-demo-command"', view)
        self.assertIn('Runs the demo flow.', view)
        lifecycle = next(s for s in svgs(view) if 'demo-guard' in s)
        self.assertIn('PreToolUse', lifecycle)
        for key in ('layers', 'flow', 'hosts', 'lifecycle', 'skills', 'commands', 'rules', 'review', 'safety',
                    'config'):
            self.assertIn(f'<section id="docs-{key}" class="doc">', view)
            self.assertIn(f'<a href="#docs-{key}">', page, 'a table of contents link per part')
        self.assertEqual(len(svgs(view)), 4, 'layers, host wiring, hook lifecycle and the review loop')
        for figure in svgs(view):
            ET.fromstring(figure)  # well-formed, so a browser draws all of it

    def test_a_registry_row_added_later_shows_up_with_no_code_change(self) -> None:
        kit = self.scratch_kit()
        before = docs(self.dashboard(kit=kit)[0])
        self.assertNotIn('late-hook', before)
        registry = kit / 'hooks/registry.json'
        late = {'event': 'PostToolUseFailure', 'matcher': None, 'command': '~/.claude/hooks/late-hook',
                'description': 'Notes a failed tool call.', 'hosts': ['claude']}
        registry.write_text(json.dumps([*json.loads(registry.read_text()), late], indent=2))
        after = docs(self.dashboard(kit=kit)[0])
        row = re.search(r'<tr id="docs-hook-PostToolUseFailure-late-hook">.*?</tr>', after, re.S)
        assert row is not None
        self.assertIn('Notes a failed tool call.', row.group())
        lifecycle = next(s for s in svgs(after) if 'late-hook' in s)
        self.assertIn('PostToolUseFailure', lifecycle, 'an event the timeline does not know is still drawn')

    def test_the_page_loads_nothing_and_keeps_its_policy(self) -> None:
        page, _ = self.dashboard(kit=self.scratch_kit())
        view = docs(page)
        self.assertNotRegex(page, r'<(?:link|img|iframe|object|embed)\b|<script\s[^>]*src=|url\(|@import')
        self.assertNotRegex(page, r'(?:src|href|action)="(?:https?:)?//', 'an external load')
        self.assertNotRegex(view, r'https?://', 'the view names no external URL at all')
        self.assertNotRegex(page, r'\sstyle="|\son[a-z]+=', 'an inline style or handler the CSP refuses')
        policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page)
        assert policy is not None
        self.assertTrue(policy.group(1).startswith("default-src 'none'; style-src 'sha256-"), policy.group(1))
        self.assertEqual(len(re.findall(r'<script\b', page)), 1)
        self.assertEqual(len(re.findall(r'<style\b', page)), 1)

    def test_the_review_rounds_and_keys_come_from_the_kit_s_own_files(self) -> None:
        kit = self.scratch_kit()
        view = page_text(docs(self.dashboard(kit=kit)[0]))
        review = tomllib.loads((kit / 'roles.toml').read_text())['review']
        for key, roles in review.items():
            self.assertRegex(view, rf'\b{key}\s+{re.escape(", ".join(roles))}', 'roles.toml [review] as written')
        self.assertIn('REVIEW_BASE', view, 'a kit.env.example key')


if __name__ == '__main__':
    unittest.main(verbosity=2)
