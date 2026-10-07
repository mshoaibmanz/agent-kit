"""agent-kit dashboard, How the kit works view: copy the source's files into a scratch kit,
add a fixture hook row, skill, command and slash command, and check that the generated view lists
each of them from its own source, that a registry row added later shows up with no code change,
and that the page still loads nothing and keeps its CSP."""

from __future__ import annotations

import html
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard_test import Fixture, page_text, section  # noqa: E402
from installer_ux_test import SOURCE  # noqa: E402

HOOK = {'event': 'PreToolUse', 'matcher': 'Bash|Read', 'command': '~/.claude/hooks/demo-guard',
        'description': 'Refuses the demo command before it runs.', 'hosts': ['claude', 'codex']}
TOOL = '''#!/usr/bin/env python3
"""demo-tool: Prints the demo inventory. A second sentence the purpose leaves out."""
import argparse


def parser():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers()
    sub.add_parser("inventory", help="list every demo item")
    return ap


if __name__ == "__main__":
    parser().parse_args()
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
    return re.findall(r'<svg class="dg.*?</svg>', view, re.S)


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
        self.assertIn('PreToolUse · Bash, Read', hook.group(), 'the matcher folded into the event')
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

    def install_from(self, kit: Path) -> None:
        """agent-setup from the scratch kit into the fixture's root, with its defaults (no blocking hooks)."""
        environment = {'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp), 'LANG': 'C.UTF-8',
                       'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null'}
        result = subprocess.run([sys.executable, str(kit / 'bin/agent-setup'), '--source', str(kit), '--root-dir',
                                 str(self.root), '--apply'], capture_output=True, text=True, env=environment,
                                timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_visible_word_names_the_home_folder(self) -> None:
        page, _ = self.dashboard(kit=self.scratch_kit())
        self.assertNotIn(str(self.home), page_text(page), 'a path under the home folder is written from ~')
        self.assertIn(f'title="{self.home}/kit', page, 'the whole path stays in the tooltip')
        self.assertIn(f'data-c="{self.home}/kit', page, 'and in the copy button')

    def test_a_source_checkout_newer_than_the_install_still_lists_its_commands(self) -> None:
        kit = self.scratch_kit()
        self.install_from(kit)
        # The checkout moves on: its lib gains a name its own agent-setup imports; the install's lacks it.
        hosts = kit / 'bin/lib/hosts.py'
        hosts.write_text(hosts.read_text() + '\nNEWNAME = 1\n')
        script = kit / 'bin/agent-setup'
        script.write_text(script.read_text().replace(
            'from hosts import (HOSTS,', 'from hosts import NEWNAME  # noqa: E402,F401\nfrom hosts import (HOSTS,', 1))
        view = docs(self.dashboard()[0])
        self.assertNotIn('not read:', view)
        row = re.search(r'<tr id="docs-cmd-agent-setup">.*?</tr>', view, re.S)
        assert row is not None
        self.assertIn('<code>rollback</code> <span class="muted">undo one install by its journal', row.group())
        flow = view[view.index('<section id="docs-flow"'):]
        self.assertIn('review every file it would write', flow[:flow.index('</section>')], "agent-setup's own text")

    def test_with_blocking_hooks_off_no_part_claims_a_guard_runs(self) -> None:
        kit = self.scratch_kit()
        self.install_from(kit)
        # A skill only the install's skills/ holds: setup no longer lays it out.
        (self.root / 'skills/left-behind').mkdir()
        (self.root / 'skills/left-behind/SKILL.md').write_text(SKILL.replace('demo-skill', 'left-behind'))
        page = self.dashboard()[0]
        view = docs(page)
        for key in ('review', 'safety'):
            part = view[view.index(f'<section id="docs-{key}"'):]
            self.assertIn('<p class="alert">Blocking hooks are off in this install', part[:part.index('</section>')], key)
        guard = re.search(r'<tr id="docs-guard-bash-guards">.*?</tr>', view, re.S)
        assert guard is not None
        self.assertIn('>not installed</span>', guard.group())
        self.assertNotIn('left-behind', view)
        self.assertNotIn('left-behind', section(page, 'skills'))

    def test_only_the_docs_hooks_table_is_compact(self) -> None:
        page, _ = self.dashboard(kit=self.scratch_kit())
        self.assertIn('<div class="tw"><table><thead><tr><th>Hook</th>', section(page, 'hooks'),
                      'the Setup view keeps a Source column wide enough for a full path')
        self.assertIn('<table class="hooks"><thead><tr><th>Hook</th>', docs(page))

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
        style = page[page.index('<style>'):page.index('</style>')]
        self.assertGreater(style.index('tbody tr.hit td'), style.index('tbody tr:nth-child(even) td'),
                           'the highlight wins over the zebra row')
        self.assertGreater(style.index('tbody tr.hit td'), style.index('tbody tr:hover td'))
        narrow = style[style.index('@media (max-width:600px)'):]
        self.assertGreater(narrow.index('.tw tbody tr.hit td{'), narrow.index('.tw tbody tr td,'),
                           'a stacked card row still shows the highlight')

    def test_an_unreadable_registry_is_the_part_s_alert_not_empty_tables(self) -> None:
        kit = self.scratch_kit()
        registry = kit / 'hooks/registry.json'
        for text, said in (('{"not": "a list"', 'is not JSON'),
                           ('{"event": "Stop"}', 'must be a JSON list'),
                           ('[{"event": "Stop", "hosts": "claude", "command": "x"}]', 'non-empty hosts list'),
                           ('[{"hosts": ["claude"], "command": "x"}]', 'needs an event'),
                           ('["<script>alert(1)</script>"]', 'not an object')):
            with self.subTest(registry=text):
                registry.write_text(text)
                view = docs(self.dashboard(kit=kit)[0])
                for key in ('hosts', 'lifecycle', 'safety'):
                    part = view[view.index(f'<section id="docs-{key}"'):]
                    part = part[:part.index('</section>')]
                    self.assertIn('<p class="alert">not read: ValueError: ', part, key)
                    self.assertIn(said, page_text(part), key)
                    self.assertNotIn('<tr id="docs-hook-', part, 'an empty or partial table')
                self.assertNotIn('<script>alert', view, 'the bad entry is escaped')
                self.assertIn('<tr id="docs-skill-kit-demo-skill">', view, 'the other parts still render')

    def test_rules_and_skills_list_only_what_render_and_setup_read(self) -> None:
        kit = self.scratch_kit()
        (kit / 'local/skills/mine').mkdir(parents=True)
        (kit / 'local/skills/mine/SKILL.md').write_text(SKILL.replace('demo-skill', 'mine'))
        (kit / 'local/rules.md').write_text('# Mine\n\nPersonal rules this engine does not render.\n')
        (kit / 'pack').mkdir(exist_ok=True)
        (kit / 'pack/rules.md').write_text('# Team\n\nThe pack rules render appends.\n')
        pack = kit / 'pack/skills'
        for name, text in (('demo-skill', SKILL), ('pack-only', SKILL.replace('demo-skill', 'pack-only'))):
            (pack / name).mkdir(parents=True)
            (pack / name / 'SKILL.md').write_text(text)
        # setup copies a pack skill into skills/ as well; the engine's copy of another differs
        (kit / 'skills/pack-only').mkdir()
        (kit / 'skills/pack-only/SKILL.md').write_text((pack / 'pack-only/SKILL.md').read_text())
        (pack / 'conventions').mkdir()
        (pack / 'conventions/SKILL.md').write_text(SKILL.replace('demo-skill', 'conventions'))
        view = docs(self.dashboard(kit=kit)[0])
        self.assertNotIn('local/rules.md', view, 'render does not read it')
        self.assertNotIn('docs-skill-overlay-', view, 'setup recorded no overlay skill')
        self.assertNotIn('<b>mine</b>', view)
        rows = re.findall(r'<tr id="docs-skill-(\w+)-(pack-only|demo-skill|conventions)">(.*?)</tr>', view, re.S)
        self.assertEqual(sorted((layer, name) for layer, name, _ in rows),
                         [('pack', 'conventions'), ('pack', 'demo-skill'), ('pack', 'pack-only')], 'each skill once')
        replaced = {name for _, name, row in rows if 'replaces the engine' in row}
        self.assertEqual(replaced, {'conventions'}, 'only a pack skill with other text than the engine copy')
        rules = re.findall(r'<tr id="docs-rule-([^"]+)">', view)
        self.assertEqual(rules[0], 'rules-AGENTS.md')
        self.assertEqual(rules[-1], 'pack-rules.md', "render's rule_sources, the pack's block last")
        self.assertTrue(all(r.startswith('rules-') for r in rules[:-1]), rules)
        pack_rules = re.search(r'<tr id="docs-rule-pack-rules.md">.*?</tr>', view, re.S)
        assert pack_rules is not None
        self.assertIn('pack: appended for every host', pack_rules.group())

    def test_a_source_checkout_s_install_command_installs_somewhere_else(self) -> None:
        kit = self.scratch_kit()
        view = docs(self.dashboard(kit=kit)[0])
        flow = view[view.index('<section id="docs-flow"'):]
        commands = [html.unescape(c) for c in re.findall(r'data-c="([^"]*)"', flow[:flow.index('</section>')])]
        install = shlex.split(commands[0])
        source, root = install[install.index('--source') + 1], install[install.index('--root-dir') + 1]
        self.assertEqual(source, str(kit))
        self.assertNotEqual(Path(root), kit, 'setup refuses to install into the source checkout')
        self.assertFalse([c for c in commands if ' sync' in c or ' update' in c or 'rollback' in c],
                         'install-only steps on a kit with no install')
        self.assertEqual({shlex.split(c)[0] for c in commands[1:]}, {f'{root}/bin/agent-kit'},
                         'every later step runs the new install, not the checkout')
        install = [str(self.root) if word == 'INSTALL_DIR' else word for word in install if word != '--apply']
        environment = {'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp), 'LANG': 'C.UTF-8',
                       'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null'}
        result = subprocess.run([sys.executable, *install], capture_output=True, text=True, env=environment,
                                timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('separate from the source checkout', result.stdout + result.stderr)

    def test_only_a_script_with_its_own_parser_is_loaded(self) -> None:
        kit = self.scratch_kit()
        marker = self.home / 'loaded'
        (kit / 'bin/acts-on-import').write_text(
            f'#!/usr/bin/env python3\n"""Acts on import."""\nopen({str(marker)!r}, "w").write("x")\n'
            'sub.add_parser("never", help="a regex would list this")\n')
        view = docs(self.dashboard(kit=kit)[0])
        self.assertFalse(marker.exists(), 'a script without parser() ran')
        row = re.search(r'<tr id="docs-cmd-acts-on-import">.*?</tr>', view, re.S)
        assert row is not None
        self.assertNotIn('never', row.group())
        setup = re.search(r'<tr id="docs-cmd-agent-setup">.*?</tr>', view, re.S)
        assert setup is not None
        self.assertIn('<code>rollback</code> <span class="muted">undo one install by its journal', setup.group())
        kit_row = re.search(r'<tr id="docs-cmd-agent-kit">.*?</tr>', view, re.S)
        assert kit_row is not None
        self.assertIn('<code>mcp describe</code>', kit_row.group(), 'a nested subcommand')

    def test_the_review_rounds_and_keys_come_from_the_kit_s_own_files(self) -> None:
        kit = self.scratch_kit()
        view = page_text(docs(self.dashboard(kit=kit)[0]))
        review = tomllib.loads((kit / 'roles.toml').read_text())['review']
        for key, roles in review.items():
            self.assertRegex(view, rf'\b{key}\s+{re.escape(", ".join(roles))}', 'roles.toml [review] as written')
        self.assertIn('REVIEW_BASE', view, 'a kit.env.example key')


if __name__ == '__main__':
    unittest.main(verbosity=2)
