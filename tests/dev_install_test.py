"""Dev installs, sync and update: agent-setup in the installer UX fake home, from a copy of this
checkout committed to its own git repository (and, for update, pushed to a local bare remote), so a
case can edit, commit and dirty its source. No network or credential is used."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from installer_ux_test import SOURCE  # noqa: E402
from team_pack_test import QUIET_GIT, PackRepos  # noqa: E402

FLAGS = ('--hosts', 'claude', '--components', 'rules', 'skills', 'hooks', 'mcp')
# An upstream agent-setup that previews (sync --dry-run) and then fails, or fails at once.
FAILS_TO_APPLY = "#!/usr/bin/env python3\nimport sys\nsys.exit(0 if '--dry-run' in sys.argv else 3)\n"
FAILS = '#!/usr/bin/env python3\nimport sys\nprint("upstream installer broke", file=sys.stderr)\nsys.exit(4)\n'


class DevInstallTests(PackRepos):
    def setUp(self) -> None:
        super().setUp()
        self.link('jq')
        self.host_cli('claude')
        self.checkout = self.home / 'kit'
        shutil.copytree(SOURCE, self.checkout, ignore=shutil.ignore_patterns('.git', 'plugins', '__pycache__'))
        self.git(self.checkout, 'init', '-q', '-b', 'main')
        self.commit(self.checkout, 'Kit v1')

    def install(self, *flags: str, code: int = 0) -> subprocess.CompletedProcess:
        return self.setup('--source', str(self.checkout), *flags, code=code, env=QUIET_GIT)

    def installed(self, *arguments: str, code: int = 0) -> subprocess.CompletedProcess:
        """The install's own agent-setup, as a terminal runs it."""
        result = subprocess.run([sys.executable, str(self.root / 'bin/agent-setup'), *arguments], capture_output=True,
                                text=True, env={'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp),
                                                'GIT_CONFIG_GLOBAL': '/dev/null', **QUIET_GIT})
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def doctor(self, code: int = 0) -> dict:
        return json.loads(self.install('doctor', code=code).stdout)

    def kit(self, *arguments: str) -> subprocess.CompletedProcess:
        """The installed agent-kit as a terminal runs it: no AGENT_KIT_DIR, so it finds its own kit."""
        return subprocess.run([sys.executable, str(self.root / 'bin/agent-kit'), *arguments], capture_output=True,
                              text=True, env={'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp),
                                              **QUIET_GIT})

    def state(self) -> dict:
        return json.loads((self.root / '.install-state/current.json').read_text())

    def links(self) -> int:
        return sum(record['kind'] == 'link' and self.root in Path(record['target']).parents
                   for record in self.state()['managed'].values())

    def test_a_dev_install_links_kit_files_into_the_checkout_and_an_edit_is_live(self) -> None:
        result = self.install(*FLAGS, '--dev', '--apply')
        self.assertIn(f'dev install: kit files link into {self.checkout}', result.stdout)
        for relative in ('bin/agent-kit', 'hooks/bash-guards', 'rules/AGENTS.md', 'agents', 'hooks/lib/state'):
            for path in [self.root / relative] if (self.root / relative).is_symlink() else (self.root / relative).rglob('*'):
                if path.is_file():
                    self.assertEqual(path.readlink(), self.checkout / path.relative_to(self.root), path)
        for relative in ('hooks/registry.json', 'mcp/servers.json', 'local/setup-paths.env', 'bin/agent-setup',
                         'bin/lib/hosts.py', 'bin/lib/checkout.py', 'hooks/lib/kit_env.py', 'hooks/lib/hook-io'):
            self.assertFalse((self.root / relative).is_symlink(), f'{relative} is derived or setup code, so a copy')
        skills = list((self.root / 'skills').rglob('SKILL.md'))
        copied = [path for path in skills if not path.is_symlink()]
        self.assertTrue(copied and len(copied) < len(skills), 'expected linked and filled skill texts')
        for path in copied:
            self.assertNotEqual(path.read_bytes(), (self.checkout / path.relative_to(self.root)).read_bytes())
        self.assertTrue(self.state()['configuration']['dev'])
        self.assertEqual(self.doctor()['dev'], {'source': str(self.checkout), 'dirty': 0, 'links': self.links()})
        self.assertIn('Installed 0 changes', self.install('--apply').stdout, 'a rerun without --dev left dev mode')
        self.assertTrue((self.root / 'bin/agent-kit').is_symlink())

        with (self.checkout / 'rules/AGENTS.md').open('a') as rules:
            rules.write('\n- Edited in the checkout.\n')
        self.assertIn('Edited in the checkout.', (self.root / 'rules/AGENTS.md').read_text())
        self.assertIn('@' + str(self.root / 'rules/AGENTS.md'), (self.home / '.claude/CLAUDE.md').read_text())
        roles = self.checkout / 'roles.toml'
        text = roles.read_text()
        start = text.index('[roles.engineer]\n')
        model = text.index('model = ', start)
        roles.write_text(text[:model] + 'model = "anthropic:edited-model"' + text[text.index('\n', model):])
        shown = self.kit('roles')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('edited-model', shown.stdout, 'the installed agent-kit did not read the edited roles')

    def test_doctor_counts_a_dirty_checkout_and_reports_pending_sync_and_broken_links(self) -> None:
        self.install(*FLAGS, '--dev', '--apply')
        self.assertEqual(self.doctor()['problems'], [])
        (self.checkout / 'rules/AGENTS.md').write_text('dirty\n')
        (self.checkout / 'scratch-notes.txt').write_text('not a kit file\n')
        doctor = self.doctor()
        self.assertEqual((doctor['dev']['dirty'], doctor['problems']), (1, []), 'a live edit is what dev is for')
        self.git(self.checkout, 'checkout', '--', 'rules/AGENTS.md')

        catalog = self.checkout / 'mcp/servers.json'
        servers = json.loads(catalog.read_text())
        servers.setdefault('mcpServers', {})['docs-search'] = {'type': 'http', 'url': 'https://example.com/mcp'}
        catalog.write_text(json.dumps(servers, indent=2) + '\n')
        self.assertEqual(self.doctor(code=1)['problems'], ['2 derived change(s) pending: run agent-kit sync'])
        self.assertEqual(self.kit('sync').returncode, 0)
        self.assertEqual(self.doctor()['problems'], [])

        elsewhere = self.home / 'elsewhere.md'
        elsewhere.write_text('other\n')
        (self.root / 'rules/AGENTS.md').unlink()
        (self.root / 'rules/AGENTS.md').symlink_to(elsewhere)
        doctor = self.doctor(code=1)
        self.assertEqual(doctor['drift'], [str(self.root / 'rules/AGENTS.md')])
        self.assertIn('1 installed path(s) changed since setup', ' '.join(doctor['problems']))
        self.install('--collision', 'backup', '--apply')
        self.assertEqual(self.doctor()['problems'], [])

        self.git(self.checkout, 'rm', '-q', 'hooks/tests/accounts.test.sh')
        self.commit(self.checkout, 'Drop a hook test')
        self.assertEqual(self.doctor(code=1)['problems'],
                         [f'1 kit link(s) name a file gone from {self.checkout} (first '
                          f'{self.root / "hooks/tests/accounts.test.sh"}): run agent-kit sync to retire them'])
        self.assertEqual(self.kit('sync').returncode, 0)
        self.assertFalse((self.root / 'hooks/tests/accounts.test.sh').is_symlink(), 'the link to a deleted file was not retired')
        self.assertEqual(self.doctor()['problems'], [])

        self.checkout.rename(self.home / 'moved')
        problems = self.installed('doctor', code=1).stderr
        self.assertIn(f'dev source {self.checkout} is missing', problems)

    def test_doctor_reports_a_checkout_its_sync_cannot_preview(self) -> None:
        self.install(*FLAGS, '--dev', '--apply')
        # Mid-merge: conflict markers in the agent-kit the preview loads.
        with (self.checkout / 'bin/agent-kit').open('a') as script:
            script.write('<<<<<<< HEAD\nx\n=======\n')
        result = self.installed('doctor', code=1)
        self.assertEqual(result.stderr.splitlines(), [line for line in result.stderr.splitlines() if line.startswith('agent-setup doctor: ')])
        problems = json.loads(result.stdout)['problems']
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith('agent-kit sync cannot preview this install: SyntaxError: '), problems)

    def test_a_refused_settings_key_is_a_doctor_problem_and_a_setup_refusal(self) -> None:
        self.install(*FLAGS, '--dev', '--apply')
        user = self.root / 'local/settings.json'
        user.write_text('{"theme": "dark"}\n')
        result = self.installed('doctor', code=1)
        problems = json.loads(result.stdout)['problems']
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(f"{user} sets UI-owned keys ['theme']", problems[0])
        refused = self.install(*FLAGS, '--dev', '--apply', code=2)
        self.assertEqual(refused.stderr.strip(), f"agent-setup: {user} sets UI-owned keys ['theme']; remove them or drop them from uiOwned")

    def test_dev_mode_switches_from_copies_and_rollback_survives_a_broken_checkout(self) -> None:
        self.install(*FLAGS, '--apply')
        copy = (self.root / 'bin/agent-kit').read_bytes()
        self.assertFalse((self.root / 'bin/agent-kit').is_symlink())
        undo = shlex.split(self.install('--dev', '--apply').stdout.split('Roll back with: ', 1)[1])
        self.assertEqual(undo[:4], [sys.executable, str(self.root / 'bin/agent-setup'), 'rollback', self.state()['id']])
        self.assertTrue((self.root / 'bin/agent-kit').is_symlink(), 'switching to dev needed no --collision backup')
        self.assertEqual(self.doctor()['drift'], [])
        self.assertFalse(any(key.startswith('file:') and self.root / 'bin/agent-kit' == Path(record['target'])
                             for key, record in self.state()['managed'].items()), 'the copy record was kept')

        # A checkout mid-rebase or deleted: the printed command, the install's own agent-setup, still rolls back.
        self.checkout.rename(self.home / 'kit-away')
        self.installed(*undo[2:])
        self.assertFalse((self.root / 'bin/agent-kit').is_symlink())
        self.assertEqual((self.root / 'bin/agent-kit').read_bytes(), copy)
        self.assertFalse(self.state()['configuration'].get('dev'))
        (self.home / 'kit-away').rename(self.checkout)
        self.assertEqual(self.doctor()['drift'], [])

        self.install('--dev', '--apply')
        self.install('--no-dev', '--apply')
        self.assertFalse(any(path.is_symlink() for path in self.root.rglob('*') if '.install-state' not in path.parts))
        self.assertEqual(self.links(), 0)
        self.assertEqual(self.doctor()['drift'], [])

    def test_sync_does_nothing_until_a_source_changes_then_renders_it(self) -> None:
        self.install(*FLAGS, '--dev', '--apply')
        journal = self.state()['id']
        result = self.kit('sync')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, f'sync: nothing changed; {self.root} matches {self.checkout}\n')
        self.assertEqual(self.state()['id'], journal, 'an empty sync wrote a journal')
        catalog = self.checkout / 'mcp/servers.json'
        servers = json.loads(catalog.read_text())
        servers.setdefault('mcpServers', {})['docs-search'] = {'type': 'http', 'url': 'https://example.com/mcp'}
        catalog.write_text(json.dumps(servers, indent=2) + '\n')
        dry = self.kit('sync', '--dry-run')
        self.assertEqual(dry.stdout, f'  file         {self.root / "mcp/servers.json"}\n'
                                     f'  servers      {self.home / ".claude/mcp.json"}\n'
                                     'sync: 2 change(s) to apply; rerun without --dry-run to apply them\n')
        self.assertFalse((self.home / '.claude/mcp.json').exists())
        result = self.kit('sync')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Installed 2 changes', result.stdout)
        self.assertIn('docs-search', json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers'])
        self.assertIn('nothing changed', self.kit('sync').stdout)
        self.assertEqual(list(self.tmp.iterdir()), [], 'sync left files in TMPDIR')

        # A new kit file where the user keeps their own: sync lists the blocking row and fails.
        (self.checkout / 'rules/TEAM.md').write_text('# Team\n')
        (self.root / 'rules/TEAM.md').write_text('mine\n')
        for flags in (['--dry-run'], []):
            blocked = self.kit('sync', *flags)
            self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
            self.assertIn(str(self.root / 'rules/TEAM.md'), blocked.stderr)
            self.assertTrue(all(line.startswith('  ') for line in blocked.stdout.splitlines()), blocked.stdout)
        self.assertEqual((self.root / 'rules/TEAM.md').read_text(), 'mine\n')

    def test_sync_refuses_a_kit_that_setup_did_not_install(self) -> None:
        result = subprocess.run([sys.executable, str(self.checkout / 'bin/agent-kit'), 'sync'], capture_output=True,
                                text=True, env={'HOME': str(self.home), 'PATH': str(self.shim)})
        self.assertEqual(result.returncode, 1)
        self.assertIn('sync needs an agent-setup install', result.stderr)

    def test_a_cursor_hook_confirmation_is_kept_replaced_or_cleared_and_old_installs_keep_theirs(self) -> None:
        self.host_cli('cursor')
        upstream = self.remote()
        self.install('--hosts', 'cursor', '--components', 'hooks', '--confirm-hook-support', 'cursor', '--apply')
        self.assertIn('Installed 0 changes', self.install('--apply').stdout)
        self.assertEqual(self.state()['configuration']['confirm_hook_support'], ['cursor'])

        # A state the installer wrote before it saved the confirmation: its Cursor hooks record holds it.
        path = self.root / '.install-state/current.json'
        state = self.state()
        del state['configuration']['confirm_hook_support'], state['configuration']['dev']
        path.write_text(json.dumps(state, indent=2) + '\n')
        (upstream / 'NEW.md').write_text('new\n')
        self.commit(upstream, 'Upstream change')
        self.git(upstream, 'push', '-q', 'origin', 'main')
        self.assertIn('sync: nothing changed', self.install('update').stdout)
        self.assertIn('Installed 0 changes', self.install('--apply').stdout)
        self.assertEqual(self.state()['configuration']['confirm_hook_support'], ['cursor'])

        # Given again, it replaces the saved one, through update too: none leaves Cursor unconfirmed.
        blocked = 'cursor hooks: requires explicit confirmation'
        self.assertIn(blocked, self.install('--confirm-hook-support', 'none', '--apply', code=2).stdout)
        head = self.git(self.checkout, 'rev-parse', 'HEAD')
        self.push(upstream, 'NEWER.md', 'newer\n')
        refused = self.install('update', '--confirm-hook-support', 'none', code=2).stderr
        self.assertIn(blocked, refused)
        self.assertIn('cannot install its upstream', refused)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), head)
        self.install('update')
        self.assertNotEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), head)
        self.install('--hosts', 'claude', '--confirm-hook-support', 'none', '--apply')
        self.assertEqual(self.state()['configuration']['confirm_hook_support'], [])

    def remote(self) -> Path:
        """A local bare remote the checkout tracks, and a second clone that pushes to it."""
        remote = self.home / 'remote.git'
        self.git(self.home, 'init', '-q', '--bare', '-b', 'main', str(remote))
        self.git(self.checkout, 'remote', 'add', 'origin', str(remote))
        self.git(self.checkout, 'push', '-q', '-u', 'origin', 'main')
        upstream = self.home / 'upstream'
        self.git(self.home, 'clone', '-q', str(remote), str(upstream))
        return upstream

    def push(self, upstream: Path, relative: str, text: str, mode: int = 0o644) -> str:
        (upstream / relative).write_text(text)
        (upstream / relative).chmod(mode)
        commit = self.commit(upstream, 'Upstream ' + relative)
        self.git(upstream, 'push', '-q', 'origin', 'main')
        return commit

    def test_update_fast_forwards_a_clean_checkout_and_reapplies(self) -> None:
        upstream = self.remote()
        pack = self.pack()
        self.git(pack, 'init', '-q')
        pack_before = self.commit(pack, 'Pack v1')
        self.install(*FLAGS, '--preset', str(pack), '--apply')
        before = self.git(self.checkout, 'rev-parse', 'HEAD')
        after = self.push(upstream, 'rules/AGENTS.md', (upstream / 'rules/AGENTS.md').read_text() + '\n- Pushed upstream.\n')
        (pack / 'rules.md').write_text('## Team rules\n\n- Changed.\n')
        pack_after = self.commit(pack, 'Pack v2')

        result = self.install('update')
        self.assertIn(f'kit: {self.checkout} {before[:12]} -> {after[:12]}\n', result.stdout)
        self.assertIn(f'pack: {pack} {pack_before[:12]} -> {pack_after[:12]}\n', result.stdout)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), after)
        self.assertIn('Pushed upstream.', (self.root / 'rules/AGENTS.md').read_text())
        self.assertIn('- Changed.', (self.root / 'pack/rules.md').read_text())
        self.assertEqual(self.state()['configuration']['hosts'], ['claude'])
        self.assertIn('(already current)', self.install('update').stdout)
        self.assertEqual(list(self.tmp.iterdir()), [], 'update left its export in TMPDIR')

    def test_update_refuses_a_dirty_detached_or_diverged_checkout(self) -> None:
        upstream = self.remote()
        self.install(*FLAGS, '--apply')
        self.push(upstream, 'NEW.md', 'new\n')
        head = self.git(self.checkout, 'rev-parse', 'HEAD')
        journal = self.state()['id']

        def refused(expected: str, *flags: str) -> None:
            result = self.install('update', *flags, code=2)
            self.assertIn(expected if flags else 'update refused: ' + str(self.checkout) + ' ' + expected, result.stderr)
            self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), head)
            self.assertEqual(self.state()['id'], journal, 'a refused update reapplied')

        refused('update reapplies saved choices; change them with --apply (refused: --components, --hosts)',
                '--hosts', 'codex', '--components', 'rules')
        (self.checkout / 'rules/AGENTS.md').write_text('mine\n')
        refused('has uncommitted changes')
        self.git(self.checkout, 'checkout', '--', 'rules/AGENTS.md')
        self.git(self.checkout, 'checkout', '-q', '--detach')
        refused('has a detached HEAD')
        self.git(self.checkout, 'checkout', '-q', 'main')
        # An untracked file is no kit file, unless the upstream brings one of its name: git refuses.
        (self.checkout / 'NEW.md').write_text('mine\n')
        refused('could not fast-forward: error: The following untracked working tree files would be overwritten')
        self.assertEqual((self.checkout / 'NEW.md').read_text(), 'mine\n')
        (self.checkout / 'NEW.md').unlink()
        (self.checkout / 'LOCAL.md').write_text('local\n')
        self.commit(self.checkout, 'Local change')
        head = self.git(self.checkout, 'rev-parse', 'HEAD')
        refused('has commits its upstream lacks')
        self.assertTrue((self.checkout / 'LOCAL.md').is_file())

    def test_update_ignores_untracked_files(self) -> None:
        upstream = self.remote()
        self.install(*FLAGS, '--apply')
        after = self.push(upstream, 'NEW.md', 'new\n')
        (self.checkout / 'scratch-notes.txt').write_text('mine\n')
        self.install('update')
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), after)
        self.assertEqual((self.checkout / 'scratch-notes.txt').read_text(), 'mine\n')

    def test_update_source_names_the_checkout_it_moves_and_reapplies_from(self) -> None:
        upstream = self.remote()
        self.install(*FLAGS, '--apply')
        other = self.home / 'other'
        self.git(self.home, 'clone', '-q', str(self.home / 'remote.git'), str(other))
        rules = upstream / 'rules/AGENTS.md'
        after = self.push(upstream, 'rules/AGENTS.md', rules.read_text() + '\n- First push.\n')

        # The recorded checkout gone, or still there on its old commit: --source is what update applies.
        self.checkout.rename(self.home / 'kit-away')
        self.assertIn(f'kit: {other} ', self.installed('update', '--source', str(other)).stdout)
        self.assertEqual((self.git(other, 'rev-parse', 'HEAD'), self.state()['configuration']['source_checkout']),
                         (after, str(other)))
        self.assertIn('First push.', (self.root / 'rules/AGENTS.md').read_text())
        (self.home / 'kit-away').rename(self.checkout)
        self.install('--apply')
        after = self.push(upstream, 'rules/AGENTS.md', rules.read_text() + '\n- Second push.\n')
        self.installed('update', '--source', str(other))
        self.assertEqual((self.git(other, 'rev-parse', 'HEAD'), self.state()['configuration']['source_checkout']),
                         (after, str(other)))
        self.assertIn('Second push.', (self.root / 'rules/AGENTS.md').read_text())

    def test_update_leaves_the_checkout_when_the_upstream_installer_fails(self) -> None:
        upstream = self.remote()
        self.install(*FLAGS, '--dev', '--apply')
        head = self.git(self.checkout, 'rev-parse', 'HEAD')
        journal = self.state()['id']
        # A hook dropped while its registry row stays: every host would run a missing file.
        self.git(upstream, 'rm', '-q', 'hooks/tab-signal')
        self.commit(upstream, 'Drop a registered hook')
        self.git(upstream, 'push', '-q', 'origin', 'main')
        result = self.install('update', code=2)
        self.assertIn('hooks/registry.json runs tab-signal, which its hooks/ lacks', result.stderr)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), head)
        self.assertTrue((self.root / 'hooks/tab-signal').exists())

        upstream_head = self.push(upstream, 'bin/agent-setup', FAILS, 0o755)
        result = self.install('update', code=2)
        self.assertIn('upstream installer broke', result.stderr)
        self.assertIn(f'update refused: {self.checkout} cannot install its upstream {upstream_head[:12]}: its '
                      'agent-setup preview failed or blocked (above); nothing changed', result.stderr)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.state()['id'], journal)
        self.assertEqual(self.doctor()['problems'], [])
        self.assertEqual(list(self.tmp.iterdir()), [], 'update left its export in TMPDIR')

    def test_update_says_how_to_move_the_checkout_back_when_the_reapply_fails(self) -> None:
        upstream = self.remote()
        self.install(*FLAGS, '--dev', '--apply')
        before = self.git(self.checkout, 'rev-parse', 'HEAD')
        after = self.push(upstream, 'bin/agent-setup', FAILS_TO_APPLY, 0o755)
        result = self.install('update', code=3)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), after)
        self.assertIn(f'git -C {self.checkout} reset --keep {before}', result.stderr)
        self.assertIn("this dev install's links already run the new commit", result.stderr)

    def test_tools_in_a_dev_install_read_the_install_not_the_checkout(self) -> None:
        work_root = self.home / 'install-work'
        (work_root / 'projects/alpha').mkdir(parents=True)
        (work_root / 'projects/alpha/PROJECT.md').write_text('# alpha\n')
        self.install(*FLAGS[:-1], 'mcp', 'data-wrappers', 'roles', '--dev', '--work-root', str(work_root),
                     '--role-model', 'cross-reviewer=openai:install-model', '--apply')
        (self.root / 'local/kit.env').write_text('BQRO_PROJECT=kit-jobs\n')
        (self.root / 'local/kit-rules.md').write_text('# rules for the kit repo\n')
        for name in ('bin/bqro', 'bin/agent-run', 'bin/agent-task', 'hooks/host-session-context'):
            self.assertTrue((self.root / name).is_symlink(), name)
        bq = self.shim / 'bq'
        bq.write_text('#!/bin/sh\necho "bq $*" >&2\nexit 1\n')
        bq.chmod(0o755)
        environment = {key: value for key, value in os.environ.items() if not key.startswith(('AGENT_', 'KIT_', 'CLAUDE'))}
        environment.update(HOME=str(self.home), PATH=f'{self.shim}:/usr/bin:/bin', TMPDIR=str(self.tmp),
                           AGENT_SESSION_ID='dev-install-test', GIT_CONFIG_GLOBAL='/dev/null', **QUIET_GIT)

        def run(*command: str, payload: str | None = None) -> str:
            result = subprocess.run(command, capture_output=True, text=True, env=environment, input=payload)
            return result.stdout + result.stderr

        self.assertIn('--project_id=kit-jobs', run(str(self.root / 'bin/bqro'), 'SELECT 1'))
        self.assertIn('alpha', run(str(self.root / 'bin/agent-task'), 'ls'))
        self.assertIn('install-model', run(str(self.root / 'bin/agent-run'), 'cross-reviewer', str(self.checkout), '--dry-run'))
        context = run(str(self.root / 'hooks/host-session-context'), payload=json.dumps({'cwd': str(self.checkout)}))
        self.assertIn(str(self.root / 'local/kit-rules.md'), context)

        # The installed tools and hooks import from the checkout; none may leave bytecode in it.
        environment.pop('PYTHONDONTWRITEBYTECODE', None)
        run(str(self.root / 'bin/agent-kit'), 'roles')
        # ro-mysql is a link into the checkout here: it finds bin/lib (secret_store) beside its target.
        self.assertTrue((self.root / 'bin/ro-mysql').is_symlink())
        self.assertIn('usage: ro-mysql', run(str(self.root / 'bin/ro-mysql')))
        run(str(self.root / 'hooks/host-adapter'), 'codex', 'edit-guard', payload=json.dumps(
            {'hook_event_name': 'PreToolUse', 'tool_name': 'apply_patch', 'cwd': str(self.home),
             'tool_input': {'command': '*** Begin Patch\n*** Add File: notes.txt\n+x\n*** End Patch\n'}}))
        self.assertEqual(sorted(str(path) for path in self.checkout.rglob('__pycache__')), [])

    def test_agent_kit_loads_its_own_modules_in_an_older_agent_setup(self) -> None:
        """An older installed agent-setup runs the checkout's agent-kit in its own process (kit_api) after
        importing its own kit_env and hosts, which may lack names this agent-kit imports."""
        older = self.home / 'older-lib'
        older.mkdir()
        (older / 'kit_env.py').write_text('def kit_env(path=None):\n    return {}\n')
        (older / 'hosts.py').write_text('HOSTS = ()\n')
        script = ('import importlib.machinery, importlib.util, sys\n'
                  f'sys.path.insert(0, {str(older)!r})\n'
                  'import hosts, kit_env\n'
                  f'loader = importlib.machinery.SourceFileLoader("agent_setup_kit", {str(self.checkout / "bin/agent-kit")!r})\n'
                  'module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))\n'
                  'sys.modules[loader.name] = module\n'
                  'loader.exec_module(module)\n'
                  'print(module.KIT, module.rules_root("codex"))\n')
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                                env={'HOME': str(self.home), 'PATH': str(self.shim), 'AGENT_KIT_DIR': str(self.checkout)})
        self.assertEqual((result.returncode, result.stdout), (0, f'{self.checkout} {self.home / ".codex"}\n'), result.stderr)

    def test_doctor_reports_a_sync_that_a_collision_blocks(self) -> None:
        self.install(*FLAGS, '--dev', '--apply')
        (self.checkout / 'rules/TEAM.md').write_text('# Team\n')
        (self.root / 'rules/TEAM.md').write_text('mine\n')
        self.assertEqual(self.kit('sync', '--dry-run').returncode, 2)
        problems = self.doctor(code=1)['problems']
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith('agent-kit sync is blocked: '), problems)
        self.assertIn(str(self.root / 'rules/TEAM.md'), problems[0])

    def test_update_source_records_a_clone_with_the_same_files(self) -> None:
        self.remote()
        self.install(*FLAGS, '--apply')
        journal = self.state()['id']
        other = self.home / 'other'
        self.git(self.home, 'clone', '-q', str(self.home / 'remote.git'), str(other))
        result = self.installed('update', '--source', str(other))
        self.assertIn('configuration', result.stdout)
        self.assertEqual(self.state()['configuration']['source_checkout'], str(other))
        recorded = self.state()['id']
        self.assertNotEqual(recorded, journal)
        self.assertIn('nothing changed', self.installed('update').stdout)
        self.assertEqual(self.state()['id'], recorded, 'an unchanged sync wrote a journal')
        self.installed('rollback', recorded, '--root-dir', str(self.root))
        self.assertEqual((self.state()['id'], self.state()['configuration']['source_checkout']), (journal, str(self.checkout)))

    def test_update_previews_a_catalog_the_checkout_tracks_as_the_upstream_has_it(self) -> None:
        upstream = self.remote()
        catalog = self.checkout / 'mcp/team.json'
        catalog.write_text(json.dumps({'mcpServers': {'docs': {'type': 'http', 'url': 'https://example.com/mcp'}}}) + '\n')
        self.commit(self.checkout, 'Team catalog')
        self.git(self.checkout, 'push', '-q', 'origin', 'main')
        self.git(upstream, 'pull', '-q', '--ff-only')
        self.install(*FLAGS, '--mcp-catalog', str(catalog), '--apply')
        head = self.git(self.checkout, 'rev-parse', 'HEAD')
        journal = self.state()['id']
        inline = {'mcpServers': {'docs': {'command': 'docs-mcp', 'env': {'DOCS_TOKEN': 'x'}}}}
        self.push(upstream, 'mcp/team.json', json.dumps(inline) + '\n')
        result = self.install('update', code=2)
        self.assertIn('MCP docs: use native OAuth or a runtime wrapper', result.stderr)
        self.assertEqual(self.git(self.checkout, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.state()['id'], journal)
        self.assertEqual(self.state()['configuration']['mcp_catalog'], str(catalog))


if __name__ == '__main__':
    unittest.main()
