"""Team packs: a preset repository that also carries skills and a rules block. Each case runs
agent-setup in the installer UX fake home on a synthetic pack: a local folder, or a local git
repository that a gh fixture serves as gh:example-org/team-pack (`gh api` answers the head commit and
the tarball from that repository, as GitHub does). No network or credential is used."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from installer_ux_test import SOURCE, InstallerUxTests  # noqa: E402

RULES = '## Team rules\n\n- Name the ticket in every branch.\n'
GIT = ('git', '-c', 'user.name=Pack Author', '-c', 'user.email=author@example.com', '-c', 'commit.gpgsign=false')


class TeamPackTests(InstallerUxTests):
    def pack(self, folder: Path | None = None) -> Path:
        """A synthetic pack: a preset, a skill for every host (with a reference), one for Claude only
        and a rules block."""
        folder = folder or self.home / 'team-pack'
        (folder / 'skills/team-howto/references').mkdir(parents=True)
        (folder / 'skills/team-howto/SKILL.md').write_text('---\nname: team-howto\ndescription: Team how-to.\n---\n\nRead references/deploys.md.\n')
        (folder / 'skills/team-howto/references/deploys.md').write_text('# Deploys\n')
        (folder / 'skills/team-claude/').mkdir()
        (folder / 'skills/team-claude/SKILL.md').write_text('---\nname: team-claude\nhosts: [claude]\ndescription: Claude only.\n---\n')
        (folder / 'rules.md').write_text(RULES)
        (folder / 'agent-kit-preset.toml').write_text('[kit]\nREVIEW_BASE = "main"\n')
        (folder / 'README.md').write_text('# Team pack\n')
        return folder

    def git(self, folder: Path, *arguments: str) -> str:
        return subprocess.run([*GIT, '-C', str(folder), *arguments], capture_output=True, text=True, check=True,
                              env={'PATH': str(self.shim), 'HOME': str(self.home), 'GIT_CONFIG_GLOBAL': '/dev/null'}).stdout.strip()

    def commit(self, folder: Path, message: str) -> str:
        self.git(folder, 'add', '-A')
        self.git(folder, 'commit', '-q', '-m', message)
        return self.git(folder, 'rev-parse', 'HEAD')

    def gh_fixture(self, repository: Path) -> None:
        """gh that serves repository as example-org/team-pack: the head commit and its tarball."""
        script = self.shim / 'gh'
        script.write_text(f'''#!/bin/sh
repo={json.dumps(str(repository))}
case "$1 $2" in
  "api repos/example-org/team-pack/commits/HEAD") exec git -C "$repo" rev-parse HEAD ;;
  "api repos/example-org/team-pack/tarball/"*)
    sha=${{2##*/}}
    exec git -C "$repo" archive --format=tar --prefix="example-org-team-pack-$sha/" "$sha" ;;
esac
echo "gh: not served by the fixture: $*" >&2
exit 1
''')
        script.chmod(0o755)

    def state(self) -> dict:
        return json.loads((self.root / '.install-state/current.json').read_text())

    def doctor(self) -> dict:
        return json.loads(self.setup('doctor').stdout)

    def test_a_local_pack_installs_its_skills_and_rules_for_each_host(self) -> None:
        self.host_cli('claude')
        self.host_cli('codex')
        pack = self.pack()
        result = self.setup('--preset', str(pack), '--hosts', 'claude', 'codex', '--components', 'rules', 'skills', '--apply')
        self.assertIn(f'pack {pack} at ', result.stdout)
        self.assertIn('skills team-claude, team-howto; rules block of 10 words', result.stdout)
        claude, codex = self.home / '.claude/skills', self.home / '.codex/skills'
        self.assertEqual((claude / 'team-howto').readlink(), self.root / 'skills/team-howto')
        self.assertTrue((claude / 'team-howto/references/deploys.md').is_file())
        self.assertTrue((claude / 'team-claude').is_symlink())
        self.assertTrue((codex / 'team-howto').is_symlink())
        self.assertFalse((codex / 'team-claude').exists(), 'a hosts: [claude] pack skill was linked for codex')
        self.assertTrue((claude / 'conventions').is_symlink(), 'the kit skills are still installed beside the pack')
        self.assertIn(RULES.strip(), (self.root / 'state/rendered/claude-host-rules.md').read_text())
        self.assertIn(RULES.strip(), (self.home / '.codex/AGENTS.md').read_text())
        self.assertIn('REVIEW_BASE=main', (self.root / 'local/preset.env').read_text())
        record = self.state()['configuration']['preset']
        self.assertEqual(record['skills'], ['team-claude', 'team-howto'])
        self.assertNotIn('commit', record, 'a folder that is not a git repository has no commit')
        journal = json.loads((self.root / '.install-state' / self.state()['id'] / 'journal.json').read_text())
        self.assertEqual(journal['preset']['pack'], record['pack'])
        # A later run without --preset installs the same pack from the copy setup keeps.
        self.setup('--hosts', 'claude', 'codex', '--components', 'rules', 'skills', '--apply')
        self.assertTrue((claude / 'team-howto').is_symlink())
        self.assertIn(RULES.strip(), (self.home / '.codex/AGENTS.md').read_text())
        # The installed kit renders the team rules on its own too.
        rendered = subprocess.run([sys.executable, str(self.root / 'bin/agent-kit'), 'render', '--host', 'codex',
                                   '--components', 'rules'], capture_output=True, text=True,
                                  env={'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp)})
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn(RULES.strip(), (self.home / '.codex/AGENTS.md').read_text())
        status = self.doctor()['preset']['pack_status']
        self.assertEqual((status['installed'], status['upstream']), ('unchanged', 'unchanged'))
        kit_doctor = subprocess.run([sys.executable, str(self.root / 'bin/agent-kit'), 'doctor'], capture_output=True, text=True,
                                    env={'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp),
                                         'AGENT_KIT_DIR': str(self.root)}).stdout
        self.assertIn(f'pack: {pack} at {status["digest"][:12]}; installed copy unchanged; source unchanged', kit_doctor)
        (self.root / 'pack/skills/team-howto/SKILL.md').write_text('edited\n')
        problems = json.loads(self.setup('doctor', code=1).stdout)['problems']
        fix = f'changed since setup: rerun agent-setup with --preset {pack} --collision backup to restore it'
        self.assertIn(f'team pack under {self.root / "pack"}: {fix}', problems)
        refused = self.setup('--hosts', 'claude', 'codex', '--components', 'rules', 'skills', '--apply', code=2).stderr
        self.assertIn(fix, refused, 'a rerun installed the edited copy of the pack')
        self.setup('--preset', str(pack), '--hosts', 'claude', 'codex', '--components', 'rules', 'skills',
                   '--collision', 'backup', '--apply')
        self.assertNotIn('edited', (self.root / 'skills/team-howto/SKILL.md').read_text())
        self.assertEqual(self.doctor()['preset']['pack_status']['installed'], 'unchanged')

    def test_a_kept_pack_whose_skill_a_newer_kit_also_ships_is_refused(self) -> None:
        kit = self.home / 'kit'
        shutil.copytree(SOURCE, kit, ignore=shutil.ignore_patterns('.git', 'plugins', '__pycache__'))
        pack = self.pack()
        flags = ('--source', str(kit), '--hosts', 'claude', '--components', 'rules', 'skills')
        self.setup('--preset', str(pack), *flags, '--apply')
        shutil.copytree(kit / 'skills/conventions', kit / 'skills/team-howto')
        result = self.setup(*flags, '--apply', code=2)
        self.assertIn('pack skill team-howto has the name of a kit skill', result.stderr)

    def test_a_pack_preset_keeps_an_unknown_included_skill_name_for_the_check(self) -> None:
        pack = self.pack()
        (pack / 'agent-kit-preset.toml').write_text('[skills]\ninclude = ["conventions-typo"]\n')
        result = self.setup('--preset', str(pack), '--hosts', 'claude', '--components', 'skills', '--apply', code=2)
        self.assertIn('Unknown selected skill: conventions-typo', result.stderr)

    def test_an_update_previews_its_diff_and_rollback_restores_the_previous_commit(self) -> None:
        pack = self.pack()
        self.git(pack, 'init', '-q')
        first = self.commit(pack, 'Pack v1')
        flags = ('--preset', str(pack), '--hosts', 'claude', '--components', 'rules', 'skills')
        self.setup(*flags, '--apply')
        self.assertEqual(self.state()['configuration']['preset']['commit'], first)
        (pack / 'skills/team-new').mkdir()
        (pack / 'skills/team-new/SKILL.md').write_text('---\nname: team-new\ndescription: New.\n---\n')
        (pack / 'skills/team-howto/references/deploys.md').write_text('# Deploys, v2\n')
        (pack / 'skills/team-claude/SKILL.md').unlink()
        (pack / 'rules.md').write_text(RULES + '- Second rule.\n')
        second = self.commit(pack, 'Pack v2')
        self.assertEqual(self.doctor()['preset']['pack_status']['upstream'],
                         f'changed since setup: rerun with --preset {pack} to update')
        preview = self.setup(*flags).stdout
        self.assertIn(f'pack {pack}: {first[:12]} -> {second[:12]}: skills added team-new; skills changed team-howto; '
                      'skills removed team-claude; rules changed', preview)
        self.setup(*flags, '--apply')
        claude = self.home / '.claude/skills'
        self.assertFalse((claude / 'team-claude').exists() or (claude / 'team-claude').is_symlink())
        self.assertFalse((self.root / 'skills/team-claude/SKILL.md').exists())
        self.assertFalse((self.root / 'pack/skills/team-claude/SKILL.md').exists())
        self.assertTrue((claude / 'team-new').is_symlink())
        self.assertIn('Second rule', (self.root / 'state/rendered/claude-host-rules.md').read_text())
        self.assertEqual(self.state()['configuration']['preset']['commit'], second)
        self.setup('rollback', self.state()['id'])
        self.assertEqual(self.state()['configuration']['preset']['commit'], first)
        self.assertTrue((claude / 'team-claude').is_symlink())
        self.assertFalse((claude / 'team-new').exists() or (claude / 'team-new').is_symlink())
        self.assertNotIn('Second rule', (self.root / 'state/rendered/claude-host-rules.md').read_text())
        self.assertEqual((self.root / 'skills/team-howto/references/deploys.md').read_text(), '# Deploys\n')
        self.assertEqual(self.doctor()['preset']['pack_status']['installed'], 'unchanged')

    def test_a_gh_pack_is_fetched_at_its_head_commit_with_your_gh(self) -> None:
        repository = self.pack(self.home / 'served')
        self.git(repository, 'init', '-q')
        first = self.commit(repository, 'Pack v1')
        self.gh_fixture(repository)
        result = self.setup('--preset', 'gh:example-org/team-pack', '--hosts', 'claude', '--components', 'rules', 'skills', '--apply')
        self.assertIn(f'pack gh:example-org/team-pack at {first[:12]}: skills team-claude, team-howto', result.stdout)
        record = self.state()['configuration']['preset']
        self.assertEqual((record['source'], record['commit']), ('gh:example-org/team-pack', first))
        self.assertTrue((self.home / '.claude/skills/team-howto').is_symlink())
        self.assertEqual(self.doctor()['preset']['pack_status']['upstream'], 'unchanged')
        (repository / 'rules.md').write_text(RULES + '- Another.\n')
        second = self.commit(repository, 'Pack v2')
        self.assertEqual(self.doctor()['preset']['pack_status']['upstream'],
                         f'new commit {second[:12]} since setup: rerun with --preset gh:example-org/team-pack to update')

    def test_a_pack_with_a_secret_an_escaping_link_long_rules_or_a_kit_skill_name_is_refused(self) -> None:
        cases = {
            'a .env file': lambda pack: (pack / '.env').write_text('NAME=value\n'),
            'a private key': lambda pack: (pack / 'skills/team-howto/references/deploys.md').write_text(
                '-----BEGIN OPENSSH ' + 'PRIVATE KEY-----\n'),
            'a token': lambda pack: (pack / 'README.md').write_text('gh' + 'p_' + 'A1b2C3d4E5f6G7h8I9j0\n'),
            'a link out of the pack': lambda pack: (pack / 'skills/team-howto/references/home.md').symlink_to('../../../../secret.md'),
            'rules over the cap': lambda pack: (pack / 'rules.md').write_text('word ' * 301),
            'a kit skill name': lambda pack: (pack / 'skills/conventions').mkdir() or (pack / 'skills/conventions/SKILL.md').write_text('# Mine\n'),
        }
        expected = {'a .env file': '.env looks like a credential file',
                    'a private key': 'deploys.md holds a token or private key',
                    'a token': 'README.md holds a token or private key',
                    'a link out of the pack': 'points outside the pack',
                    'rules over the cap': 'rules.md has 301 words, over the cap of 300',
                    'a kit skill name': 'pack skill conventions has the name of a kit skill'}
        for name, spoil in cases.items():
            with self.subTest(name):
                pack = self.pack(self.home / name.replace(' ', '-'))
                spoil(pack)
                result = self.setup('--preset', str(pack), '--hosts', 'claude', '--components', 'rules', 'skills', '--apply', code=2)
                self.assertIn(expected[name], result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
                self.assertFalse(self.root.exists(), 'a refused pack wrote the kit')


if __name__ == '__main__':
    suite = unittest.TestSuite(TeamPackTests(name) for name in sorted(vars(TeamPackTests)) if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': result.testsRun, 'successful': result.wasSuccessful(),
                                'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
