"""Team packs: a preset repository that also carries skills and a rules block. Each case runs
agent-setup in the installer UX fake home on a synthetic pack: a local folder, or a gh fixture that
serves gh:example-org/team-pack from a local git repository or a crafted archive (`gh api` answers the
head commit and the tarball, as GitHub does). No network or credential is used."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from installer_ux_test import SOURCE, InstallerUxFixture  # noqa: E402

RULES = '## Team rules\n\n- Name the ticket in every branch.\n'
GIT = ('git', '-c', 'user.name=Pack Author', '-c', 'user.email=author@example.com', '-c', 'commit.gpgsign=false')
# The environment of every git a test runs, its own or the installer's: no auto maintenance, which git
# runs detached after a commit or fetch, racing the fake home's cleanup.
QUIET_GIT = {'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'maintenance.auto', 'GIT_CONFIG_VALUE_0': 'false',
             'GIT_CONFIG_KEY_1': 'gc.auto', 'GIT_CONFIG_VALUE_1': '0'}
GH = 'gh:example-org/team-pack'
FLAGS = ('--hosts', 'claude', '--components', 'rules', 'skills')
SKILL = b'---\nname: team-a\ndescription: A.\n---\n'
# Strings a secret scanner would flag, built so this file holds none.
TOKEN = 'gh' + 'p_' + 'A1b2C3d4E5f6G7h8I9j0'
PRIVATE_KEY = '-----BEGIN OPENSSH ' + 'PRIVATE KEY-----\n'


class PackRepos(InstallerUxFixture):
    """The installer UX fixture with git repositories in its fake home and a synthetic team pack."""

    def pack(self, folder: Path | None = None) -> Path:
        """A synthetic pack: a preset, a skill for every host (with a reference), one for Claude only,
        a rules block, and a README that is not part of the pack."""
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
                              env={'PATH': str(self.shim), 'HOME': str(self.home), 'GIT_CONFIG_GLOBAL': '/dev/null',
                                   **QUIET_GIT}).stdout.strip()

    def commit(self, folder: Path, message: str) -> str:
        self.git(folder, 'add', '-A')
        self.git(folder, 'commit', '-q', '-m', message)
        return self.git(folder, 'rev-parse', 'HEAD')


class TeamPackTests(PackRepos):
    def served(self, folder: Path | None = None) -> tuple[Path, str]:
        """A pack in a git repository that the gh fixture serves, and its first commit."""
        repository = self.pack(folder or self.home / 'served')
        self.git(repository, 'init', '-q')
        first = self.commit(repository, 'Pack v1')
        self.gh_fixture(repository)
        return repository, first

    def gh_fixture(self, repository: Path) -> None:
        """gh that serves repository as example-org/team-pack: the head commit, its tarball and a file
        at a commit. Each call's endpoint is logged to gh.log in the fake home."""
        self.gh_script(f'''repo={json.dumps(str(repository))}
echo "$2" >> {json.dumps(str(self.home / 'gh.log'))}
case "$1 $2" in
  "api repos/example-org/team-pack/commits/HEAD") exec git -C "$repo" rev-parse HEAD ;;
  "api repos/example-org/team-pack/tarball/"*)
    sha=${{2##*/}}
    exec git -C "$repo" archive --format=tar --prefix="example-org-team-pack-$sha/" "$sha" ;;
  "api repos/example-org/team-pack/contents/"*)
    path=${{2#*/contents/}}
    exec git -C "$repo" show "${{path##*\\?ref=}}:${{path%\\?ref=*}}" ;;
esac
echo "gh: not served by the fixture: $*" >&2
exit 1''')

    def gh_archive(self, members: list[tarfile.TarInfo | tuple[str, bytes]]) -> None:
        """gh that serves an archive of members (a name and its content, or a crafted member)."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as tar:
            for member in members:
                if isinstance(member, tarfile.TarInfo):
                    tar.addfile(member)
                    continue
                info = tarfile.TarInfo('example-org-team-pack-0/' + member[0])
                info.size, info.mode = len(member[1]), 0o644
                tar.addfile(info, io.BytesIO(member[1]))
        (self.home / 'pack.tar').write_bytes(buffer.getvalue())
        self.gh_script(f'case "$2" in\n  *commits/HEAD) echo {"a" * 40} ;;\n  *tarball*) cat {self.home / "pack.tar"} ;;\nesac')

    def gh_script(self, body: str) -> None:
        script = self.shim / 'gh'
        script.write_text('#!/bin/sh\n' + body + '\n')
        script.chmod(0o755)

    def state(self) -> dict:
        return json.loads((self.root / '.install-state/current.json').read_text())

    def doctor(self, code: int = 0) -> dict:
        return json.loads(self.setup('doctor', code=code).stdout)

    def kit(self, *arguments: str) -> subprocess.CompletedProcess:
        """The installed kit's own agent-kit."""
        return subprocess.run([sys.executable, str(self.root / 'bin/agent-kit'), *arguments], capture_output=True,
                              text=True, env={'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp),
                                              'AGENT_KIT_DIR': str(self.root)})

    def refused(self, spec: str, expected: str) -> None:
        result = self.setup('--preset', spec, *FLAGS, '--apply', code=2)
        self.assertIn(expected, result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        self.assertFalse(self.root.exists(), 'a refused pack wrote the kit')

    def test_a_local_pack_installs_its_skills_and_rules_for_each_host(self) -> None:
        self.host_cli('claude')
        self.host_cli('codex')
        pack = self.pack()
        result = self.setup('--preset', str(pack), '--hosts', 'claude', 'codex', '--components', 'rules', 'skills', '--apply')
        self.assertIn(f'pack {pack} at its working tree: skills team-claude, team-howto; rules block of 10 words', result.stdout)
        claude, codex = self.home / '.claude/skills', self.home / '.codex/skills'
        self.assertEqual((claude / 'team-howto').readlink(), self.root / 'skills/team-howto')
        self.assertTrue((claude / 'team-howto/references/deploys.md').is_file())
        self.assertTrue((claude / 'team-claude').is_symlink())
        self.assertTrue((codex / 'team-howto').is_symlink())
        self.assertFalse((codex / 'team-claude').exists(), 'a hosts: [claude] pack skill was linked for codex')
        self.assertTrue((claude / 'conventions').is_symlink(), 'the kit skills are still installed beside the pack')
        self.assertIn(RULES.strip(), (self.root / 'state/rendered/claude-host-rules.md').read_text())
        self.assertIn(RULES.strip(), (self.home / '.codex/AGENTS.md').read_text())
        self.assertEqual((self.root / 'pack/rules.md').read_text(), RULES)
        self.assertFalse((self.root / 'rules/team.md').exists() or (self.root / 'pack/README.md').exists())
        self.assertIn('REVIEW_BASE=main', (self.root / 'local/preset.env').read_text())
        self.assertEqual(set(self.state()['configuration']['preset']), {'source', 'sha256'},
                         'a folder that is not a git repository has no commit')

    def test_a_run_without_preset_reinstalls_the_kept_pack(self) -> None:
        self.host_cli('codex')
        pack = self.pack()
        flags = ('--hosts', 'claude', 'codex', '--components', 'rules', 'skills', '--apply')
        self.setup('--preset', str(pack), *flags)
        shutil.rmtree(pack)
        self.assertIn('Installed 0 changes', self.setup(*flags).stdout)
        self.assertTrue((self.home / '.claude/skills/team-howto').is_symlink())
        self.assertIn(RULES.strip(), (self.home / '.codex/AGENTS.md').read_text())
        rendered = self.kit('render', '--host', 'codex', '--components', 'rules')
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn(RULES.strip(), (self.home / '.codex/AGENTS.md').read_text())
        doctor = self.kit('doctor').stdout
        self.assertIn(f'pack: {pack} at its working tree; source not checked: ', doctor)

    def test_an_edited_kept_pack_refuses_a_rerun_until_restored(self) -> None:
        pack = self.pack()
        self.setup('--preset', str(pack), *FLAGS, '--apply')
        self.assertEqual(self.doctor()['drift'], [])
        edited = self.root / 'pack/skills/team-howto/SKILL.md'
        edited.write_text('edited\n')
        self.assertEqual(self.doctor(code=1)['drift'], [str(edited)])
        refused = self.setup(*FLAGS, '--apply', code=2).stderr
        self.assertIn(f'changed since setup ({edited}): rerun agent-setup with --preset {pack} --collision backup', refused)
        self.setup('--preset', str(pack), *FLAGS, '--collision', 'backup', '--apply')
        self.assertNotIn('edited', edited.read_text())
        self.assertEqual(self.doctor()['drift'], [])

    def test_a_stray_file_in_the_kept_pack_is_not_part_of_it(self) -> None:
        pack = self.pack()
        self.setup('--preset', str(pack), *FLAGS, '--apply')
        (self.root / 'pack/skills/team-howto/.DS_Store').write_bytes(b'\x00\x00\x00\x01Bud1')
        self.setup(*FLAGS, '--apply')
        self.assertFalse((self.root / 'skills/team-howto/.DS_Store').exists())
        self.assertEqual(self.doctor()['drift'], [])

    def test_a_kept_pack_whose_skill_a_newer_kit_also_ships_is_refused(self) -> None:
        kit = self.home / 'kit'
        shutil.copytree(SOURCE, kit, ignore=shutil.ignore_patterns('.git', 'plugins', '__pycache__'))
        pack = self.pack()
        flags = ('--source', str(kit), *FLAGS)
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
        for path in pack.rglob('*'):
            if path.is_file():
                path.chmod(0o664)  # as written under a umask of 002
        self.git(pack, 'init', '-q')
        first = self.commit(pack, 'Pack v1')
        flags = ('--preset', str(pack), *FLAGS)
        self.setup(*flags, '--apply')
        self.assertEqual(self.state()['configuration']['preset']['commit'], first)
        self.assertIn(f'pack {pack} at {first[:12]}: content unchanged since the install ({first[:12]})',
                      self.setup(*flags).stdout)
        (pack / 'skills/team-new').mkdir()
        (pack / 'skills/team-new/SKILL.md').write_text('---\nname: team-new\ndescription: New.\n---\n')
        (pack / 'skills/team-howto/references/deploys.md').write_text('# Deploys, v2\n')
        (pack / 'skills/team-claude/SKILL.md').unlink()
        (pack / 'rules.md').write_text(RULES + '- Second rule.\n')
        second = self.commit(pack, 'Pack v2')
        self.assertEqual(self.doctor()['preset']['source_status'],
                         {'changed': True, 'note': f'new commit {second[:12]} since setup: rerun with --preset {pack} to apply it'})
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
        self.assertEqual(self.doctor()['drift'], [])

    def test_a_local_pack_with_uncommitted_changes_records_no_commit(self) -> None:
        pack = self.pack()
        self.git(pack, 'init', '-q')
        self.commit(pack, 'Pack v1')
        (pack / 'rules.md').write_text(RULES + '- Not committed.\n')
        self.assertIn(f'pack {pack} at its working tree:', self.setup('--preset', str(pack), *FLAGS, '--apply').stdout)
        self.assertNotIn('commit', self.state()['configuration']['preset'])

    def test_a_gh_pack_is_fetched_at_its_head_commit_with_your_gh(self) -> None:
        repository, first = self.served()
        result = self.setup('--preset', GH, *FLAGS, '--apply')
        self.assertIn(f'pack {GH} at {first[:12]}: skills team-claude, team-howto', result.stdout)
        record = self.state()['configuration']['preset']
        self.assertEqual((record['source'], record['commit']), (GH, first))
        self.assertTrue((self.home / '.claude/skills/team-howto').is_symlink())
        self.assertEqual(self.doctor()['preset']['source_status'], {'changed': False, 'note': 'unchanged'})
        self.assertIn(f'pack: {GH} at {first[:12]}; source unchanged', self.kit('doctor').stdout)
        (repository / 'rules.md').write_text(RULES + '- Another.\n')
        second = self.commit(repository, 'Pack v2')
        self.assertEqual(self.doctor()['preset']['source_status']['note'],
                         f'new commit {second[:12]} since setup: rerun with --preset {GH} to apply it')

    def test_doctor_and_a_rerun_at_the_installed_commit_do_not_download_it(self) -> None:
        _, first = self.served()
        self.setup('--preset', GH, *FLAGS, '--apply')
        log = self.home / 'gh.log'
        log.unlink()
        self.assertEqual(self.doctor()['preset']['source_status'], {'changed': False, 'note': 'unchanged'})
        self.assertIn(f'pack {GH} at {first[:12]}: content unchanged since the install ({first[:12]})',
                      self.setup('--preset', GH, *FLAGS).stdout)
        self.assertIn('Installed 0 changes', self.setup('--preset', GH, *FLAGS, '--apply').stdout)
        self.assertEqual([call for call in log.read_text().split() if 'team-pack' in call],
                         ['repos/example-org/team-pack/commits/HEAD'] * 3)
        self.assertTrue((self.home / '.claude/skills/team-howto').is_symlink())

    def test_a_new_commit_outside_the_pack_previews_its_content_unchanged(self) -> None:
        repository, first = self.served()
        self.setup('--preset', GH, *FLAGS, '--apply')
        (repository / 'README.md').write_text('# Team pack, v2\n')
        second = self.commit(repository, 'Docs only')
        # git archive, like GitHub's tarball, writes files 0664; setup installs them 0644.
        self.assertIn(f'pack {GH} at {second[:12]}: content unchanged since the install ({first[:12]})',
                      self.setup('--preset', GH, *FLAGS).stdout)

    def test_a_gh_answer_that_is_no_archive_keeps_the_installed_pack(self) -> None:
        self.served()
        self.setup('--preset', GH, *FLAGS, '--apply')
        answers = {'a proxy page': "echo '<html>proxy</html>'", 'a broken gzip stream': r"printf '\037\213\010\000\000\000\000\000\000\003broken'"}
        for name, answer in answers.items():
            with self.subTest(name):
                self.gh_script(f'case "$2" in\n  *commits/HEAD) echo {"b" * 40} ;;\n  *tarball*) {answer} ;;\nesac')
                result = self.setup('--preset', GH, *FLAGS, '--apply')
                self.assertIn(f'preset {GH} unavailable: gh api returned no readable archive of example-org/team-pack',
                              result.stderr)
                self.assertIn('Installed 0 changes', result.stdout)
                self.assertTrue((self.home / '.claude/skills/team-howto').is_symlink())
                self.assertIsNone(self.doctor()['preset']['source_status']['changed'])

    def test_os_files_in_a_pack_folder_are_not_part_of_it(self) -> None:
        files = {'skills/.DS_Store': b'\x00\x00\x00\x01Bud1', 'skills/team-howto/._SKILL.md': b'\x00\x05\x16\x07\xff',
                 'skills/team-howto/references/Thumbs.db': b'\xd0\xcf\x11\xe0'}
        pack = self.pack()
        for path, data in files.items():
            (pack / path).write_bytes(data)
        self.setup('--preset', str(pack), *FLAGS, '--apply')
        repository, _ = self.served()
        for path, data in files.items():
            (repository / path).write_bytes(data)
        self.commit(repository, 'OS files')
        self.setup('--preset', GH, *FLAGS, '--apply')
        self.assertEqual(sorted(path.relative_to(self.root / 'pack').as_posix() for path in (self.root / 'pack').rglob('*')
                                if path.is_file()),
                         ['rules.md', 'skills/team-claude/SKILL.md', 'skills/team-howto/SKILL.md',
                          'skills/team-howto/references/deploys.md'])

    def test_a_toml_only_gh_preset_records_its_commit_and_no_pack(self) -> None:
        repository = self.home / 'served'
        repository.mkdir()
        text = '[kit]\nREVIEW_BASE = "main"\n'
        (repository / 'agent-kit-preset.toml').write_bytes(text.replace('\n', '\r\n').encode())
        self.git(repository, 'init', '-q')
        first = self.commit(repository, 'Preset v1')
        self.gh_fixture(repository)
        result = self.setup('--preset', GH, *FLAGS, '--apply')
        self.assertNotIn(f'pack {GH}', result.stdout)
        # The sha256 an install before team packs recorded: of the text with its line endings normalized.
        self.assertEqual(self.state()['configuration']['preset'],
                         {'source': GH, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'commit': first})
        self.assertFalse((self.root / 'pack').exists())
        doctor = self.doctor()
        self.assertEqual((doctor['problems'], doctor['preset']['source_status']['changed']), ([], False))
        self.assertNotIn('pack:', self.kit('doctor').stdout)
        self.assertIn('Installed 0 changes', self.setup('--preset', GH, *FLAGS, '--apply').stdout)

    def test_an_unreachable_gh_pack_keeps_the_installed_one(self) -> None:
        self.served()
        self.setup('--preset', GH, *FLAGS, '--apply')
        self.gh_script('echo "HTTP 401: Bad credentials" >&2\nexit 1')
        result = self.setup('--preset', GH, *FLAGS, '--apply')
        self.assertIn(f'agent-setup: preset {GH} unavailable: HTTP 401: Bad credentials', result.stderr)
        self.assertIn('Installed 0 changes', result.stdout)
        self.assertTrue((self.home / '.claude/skills/team-claude').is_symlink())

    def test_an_unreachable_gh_pack_with_an_edited_kept_copy_refuses_with_the_fetch_error(self) -> None:
        self.served()
        self.setup('--preset', GH, *FLAGS, '--apply')
        (self.root / 'pack/rules.md').write_text('edited\n')
        self.gh_script('echo "HTTP 401: Bad credentials" >&2\nexit 1')
        result = self.setup('--preset', GH, *FLAGS, code=2)
        self.assertIn(f'preset {GH} unavailable: HTTP 401: Bad credentials', result.stderr)
        self.assertIn('the pack it installed cannot be reinstalled offline', result.stderr)

    def test_pack_text_keeps_a_placeholder_that_is_not_the_kits(self) -> None:
        self.host_cli('codex')
        pack = self.pack()
        (pack / 'skills/team-howto/references/ci.md').write_text(
            'Use `${{ secrets.DEPLOY_KEY }}`; helm: {{ .Values.image }}; kit: {{KIT_DIR}}\n')
        (pack / 'rules.md').write_text('- CI secrets are `${{ secrets.NAME }}`, never inline; see {{SKILLS_DIR}}.\n')
        self.setup('--preset', str(pack), '--hosts', 'claude', 'codex', '--components', 'rules', 'skills', '--apply')
        self.assertEqual((self.root / 'skills/team-howto/references/ci.md').read_text(),
                         f'Use `${{{{ secrets.DEPLOY_KEY }}}}`; helm: {{{{ .Values.image }}}}; kit: {self.root}\n')
        for rules in (self.root / 'state/rendered/claude-host-rules.md', self.home / '.codex/AGENTS.md'):
            self.assertIn(f'- CI secrets are `${{{{ secrets.NAME }}}}`, never inline; see {self.root}/skills.', rules.read_text())

    def test_only_skills_rules_and_the_preset_of_the_preset_folder_are_read(self) -> None:
        repository = self.home / 'served'
        (repository / 'agent/skills/team-a').mkdir(parents=True)
        (repository / 'agent/agent-kit-preset.toml').write_text('[kit]\nREVIEW_BASE = "main"\n')
        (repository / 'agent/skills/team-a/SKILL.md').write_bytes(SKILL)
        (repository / 'agent/notes.md').write_text(TOKEN + '\n')
        (repository / 'skills').mkdir()
        (repository / 'skills/README.md').write_text('# Our skills index\n')
        (repository / 'tests').mkdir()
        (repository / 'tests/server.pem').write_text('dummy\n')
        (repository / '.env').write_text('NAME=value\n')
        (repository / '.claude').mkdir()
        (repository / '.claude/skills').symlink_to('../skills')
        self.git(repository, 'init', '-q')
        self.commit(repository, 'v1')
        self.gh_fixture(repository)
        result = self.setup('--preset', GH + '/agent/agent-kit-preset.toml', *FLAGS, '--apply')
        self.assertIn('skills team-a', result.stdout)
        self.assertEqual(sorted(path.relative_to(self.root / 'pack').as_posix() for path in (self.root / 'pack').rglob('*')
                                if path.is_file()), ['skills/team-a/SKILL.md'])
        self.assertIn('no agent/missing/agent-kit-preset.toml in the repository',
                      self.setup('--preset', GH + '/agent/missing/agent-kit-preset.toml', *FLAGS, code=2).stderr)
        local = repository / 'agent/agent-kit-preset.toml'
        self.assertIn(f'pack {local} at its working tree: content unchanged since the install',
                      self.setup('--preset', str(local), *FLAGS, '--apply').stdout)
        self.assertEqual(self.state()['configuration']['preset']['source'], str(local))

    def test_a_preset_file_of_another_name_is_read_alone(self) -> None:
        repository = self.home / 'served'
        (repository / 'team/skills/team-a').mkdir(parents=True)
        (repository / 'team/skills/team-a/SKILL.md').write_bytes(SKILL)
        (repository / 'team/rules.md').write_text(RULES)
        text = '[kit]\nREVIEW_BASE = "main"\n'
        (repository / 'team/preset.toml').write_text(text)
        self.git(repository, 'init', '-q')
        first = self.commit(repository, 'v1')
        self.gh_fixture(repository)
        spec = GH + '/team/preset.toml'
        result = self.setup('--preset', spec, *FLAGS, '--apply')
        self.assertNotIn(f'pack {spec}', result.stdout)
        self.assertFalse((self.root / 'pack').exists() or (self.home / '.claude/skills/team-a').exists())
        self.assertEqual(self.state()['configuration']['preset'],
                         {'source': spec, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'commit': first})
        self.assertEqual([call.split('?')[0] for call in (self.home / 'gh.log').read_text().split() if 'team-pack' in call],
                         ['repos/example-org/team-pack/commits/HEAD', 'repos/example-org/team-pack/contents/team/preset.toml'])
        self.assertIn('REVIEW_BASE=main', (self.root / 'local/preset.env').read_text())
        # A user's own folder: its skills/ links to installed skills (absolute paths), which is no pack.
        claude = self.home / 'dotclaude'
        (claude / 'skills').mkdir(parents=True)
        (claude / 'skills/conventions').symlink_to(self.root / 'skills/conventions')
        (claude / 'team.toml').write_text(text)
        self.setup('--preset', str(claude / 'team.toml'), *FLAGS, '--apply')
        self.assertEqual(self.state()['configuration']['preset'],
                         {'source': str(claude / 'team.toml'), 'sha256': hashlib.sha256(text.encode()).hexdigest()})
        self.assertFalse((self.root / 'pack').exists())

    def test_a_link_to_a_preset_file_reads_the_pack_where_it_points(self) -> None:
        pack = self.pack()
        links = self.home / 'links'
        links.mkdir()
        for name, target in (('absolute.toml', pack / 'agent-kit-preset.toml'),
                             ('relative.toml', Path('../team-pack/agent-kit-preset.toml'))):
            with self.subTest(name):
                (links / name).symlink_to(target)
                result = self.setup('--preset', str(links / name), *FLAGS, '--apply')
                self.assertIn(f'pack {links / name} at its working tree', result.stdout)
                self.assertTrue((self.home / '.claude/skills/team-howto').is_symlink())
                self.assertEqual(self.state()['configuration']['preset']['source'], str(links / name))

    def test_a_preset_that_does_not_exist_is_refused_before_any_read(self) -> None:
        folder = self.home / 'dotclaude'
        (folder / 'skills').mkdir(parents=True)
        (folder / 'skills/elsewhere').symlink_to('/')
        for missing in (folder / 'team', folder / 'gone/agent-kit-preset.toml'):
            with self.subTest(missing.name):
                self.refused(str(missing), f'preset {missing}: no such file or folder')

    def test_an_exclude_only_preset_follows_a_kit_update(self) -> None:
        repository, _ = self.served()
        (repository / 'agent-kit-preset.toml').write_text('[skills]\nexclude = ["process-doc"]\n')
        self.commit(repository, 'Exclude')
        kit = self.home / 'kit'
        shutil.copytree(SOURCE, kit, ignore=shutil.ignore_patterns('.git', 'plugins', '__pycache__'))
        flags = ('--source', str(kit), *FLAGS, '--apply')
        self.setup('--preset', GH, *flags)
        skills = self.home / '.claude/skills'
        self.assertTrue((skills / 'unslop').is_symlink())
        log = self.home / 'gh.log'
        for name, removed, preset in (('kit-new', 'unslop', ('--preset', GH)), ('kit-newer', 'prototype', ())):
            with self.subTest(preset=preset):
                log.unlink(missing_ok=True)
                (kit / 'skills' / name).mkdir()
                (kit / 'skills' / name / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: New.\n---\n')
                shutil.rmtree(kit / 'skills' / removed)
                self.setup(*preset, *flags)
                self.assertTrue((skills / name).is_symlink(), 'a kit skill added since setup is not installed')
                self.assertFalse((skills / removed).exists() or (skills / removed).is_symlink())
                self.assertFalse((skills / 'process-doc').exists(), 'the excluded skill was installed')
                self.assertTrue((skills / 'team-howto').is_symlink())
                self.assertNotIn('tarball', log.read_text() if log.exists() else '')

    def test_links_in_skills_that_name_a_pack_file_install_as_that_file(self) -> None:
        pack = self.pack()
        references = pack / 'skills/team-howto/references'
        (references / 'alias.md').symlink_to('deploys.md')
        (references / 'chain.md').symlink_to('alias.md')
        (references / 'rules.md').symlink_to('../../../rules.md')
        (pack / 'skills/team-howto/.git').mkdir()
        (pack / 'skills/team-howto/.git/credentials.json').write_text('{}\n')
        (pack / 'docs').mkdir()
        (pack / 'docs/server.key').write_text(PRIVATE_KEY)
        (pack / '.claude').mkdir()
        (pack / '.claude/skills').symlink_to('../skills')
        self.setup('--preset', str(pack), *FLAGS, '--apply')
        installed = self.root / 'skills/team-howto/references'
        self.assertEqual([(installed / name).read_text() for name in ('alias.md', 'chain.md', 'rules.md')],
                         ['# Deploys\n', '# Deploys\n', RULES])
        self.assertFalse((installed / 'alias.md').is_symlink())
        self.assertFalse((self.root / 'skills/team-howto/.git').exists())

    def test_a_preset_sets_the_engine_keys_enables_plugins_and_runs_a_server_from_a_clone(self) -> None:
        self.host_cli('claude')
        code = self.home / 'Code'
        server = code / 'tools-mcp/.venv/bin/tools-mcp'
        server.parent.mkdir(parents=True)
        server.write_text('#!/bin/sh\n')
        server.chmod(0o755)
        pack = self.pack()
        (pack / 'agent-kit-preset.toml').write_text(
            f'[kit]\nCODE_DIRS_JSON = ["{code}"]\nTICKET_PREFIXES = "ABC,OPS"\n'
            '[plugins.codex]\nmarketplace = "openai-codex"\nsource = "github:openai/codex-plugin-cc"\n'
            '[plugins.thermos]\nmarketplace = "team-plugins"\n'
            '[mcp.servers.tools]\ncommand = "{{CODE_DIR}}/tools-mcp/.venv/bin/tools-mcp"\n'
            '[mcp.servers.absent]\ncommand = "{{CODE_DIR}}/absent-mcp/.venv/bin/absent-mcp"\n')
        flags = ('--hosts', 'claude', '--components', 'rules', 'mcp')
        self.setup('--preset', str(pack), *flags, '--apply')
        preset_env = (self.root / 'local/preset.env').read_text()
        self.assertIn('TICKET_PREFIXES=ABC,OPS', preset_env)
        settings = json.loads((self.home / '.claude/settings.json').read_text())
        self.assertEqual(settings['enabledPlugins'], {'codex@openai-codex': True, 'thermos@team-plugins': True})
        self.assertEqual(settings['extraKnownMarketplaces'],
                         {'openai-codex': {'source': {'source': 'github', 'repo': 'openai/codex-plugin-cc'}}})
        servers = json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers']
        self.assertEqual(servers['tools']['command'], str(server))
        self.assertNotIn('absent', servers, 'a server whose clone lacks the command was installed')

        # The user's own plugin stays; a plugin the preset drops is taken out again.
        settings['enabledPlugins']['personal@own'] = True
        (self.home / '.claude/settings.json').write_text(json.dumps(settings))
        (pack / 'agent-kit-preset.toml').write_text(
            '[plugins.thermos]\nmarketplace = "team-plugins"\n')
        self.setup('--preset', str(pack), *flags, '--apply')
        settings = json.loads((self.home / '.claude/settings.json').read_text())
        self.assertEqual(settings['enabledPlugins'], {'thermos@team-plugins': True, 'personal@own': True})
        self.assertNotIn('openai-codex', settings.get('extraKnownMarketplaces', {}))

    def test_a_preset_lists_the_sentry_instances_the_installed_kit_reads(self) -> None:
        self.host_cli('claude')
        pack = self.pack()
        (pack / 'agent-kit-preset.toml').write_text(
            '[mcp.servers.sentry]\ncommand = "{{KIT_DIR}}/bin/sentry-mcp"\nargs = ["--disable-skills=seer"]\n'
            '[sentry.instances.main]\nhost = "sentry.example.com"\nkeychain = "agent-kit/mcp/sentry"\nserver = "sentry"\n'
            'orgs = { example = "prod" }\n')
        flags = ('--hosts', 'claude', '--components', 'rules', 'mcp')
        self.setup('--preset', str(pack), *flags, '--apply')
        instances = json.loads((self.root / 'mcp/sentry-instances.json').read_text())['instances']
        self.assertEqual(instances, {'main': {'host': 'sentry.example.com', 'keychain': 'agent-kit/mcp/sentry',
                                              'server': 'sentry', 'orgs': {'example': 'prod'}}})
        servers = json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers']
        self.assertEqual(list(servers), ['sentry'])
        self.assertEqual(servers['sentry']['command'], str(self.root / 'bin/sentry-mcp'))

    def test_a_preset_with_mcp_servers_installs_them_by_default_unless_the_components_are_chosen(self) -> None:
        self.host_cli('claude')
        pack = self.pack()
        (pack / 'agent-kit-preset.toml').write_text(
            '[mcp.servers.sentry]\ncommand = "{{KIT_DIR}}/bin/sentry-mcp"\n'
            '[sentry.instances.main]\nhost = "sentry.example.com"\nkeychain = "agent-kit/mcp/sentry"\nserver = "sentry"\n')
        self.setup('--preset', str(pack), '--hosts', 'claude', '--apply')
        self.assertIn('mcp', self.state()['configuration']['components'])
        self.assertIn('main', json.loads((self.root / 'mcp/sentry-instances.json').read_text())['instances'])
        self.assertEqual(list(json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers']), ['sentry'])
        self.assertIn('sync: nothing changed', self.setup('sync').stdout)
        self.setup('--hosts', 'claude', '--components', 'rules', 'skills', '--apply')
        self.assertNotIn('mcp', self.state()['configuration']['components'])
        self.setup('--hosts', 'claude', '--apply')
        self.assertNotIn('mcp', self.state()['configuration']['components'], 'the chosen components were overridden')

    def test_a_preset_with_a_malformed_engine_key_plugin_or_clone_command_is_refused(self) -> None:
        cases = {
            'lowercase ticket keys': ('[kit]\nTICKET_PREFIXES = "abc"\n', 'TICKET_PREFIXES takes Jira project keys'),
            'a ticket prefix over 6 characters': ('[kit]\nTICKET_PREFIXES = "ABCDEFG"\n', 'TICKET_PREFIXES takes Jira project keys'),
            'a key no kit code reads': ('[kit]\nGH_ORG = "example-org"\n', '[kit] takes one-line strings'),
            'a token as a value': (f'[kit]\nREVIEW_BASE = "{TOKEN}"\n', 'REVIEW_BASE looks like an inline secret'),
            'a plugin marketplace URL': ('[plugins.codex]\nmarketplace = "m"\nsource = "https://example.com/m.git"\n',
                                         '[plugins] takes [plugins.<name>] tables'),
            'a plugin without a marketplace': ('[plugins.codex]\nsource = "github:o/r"\n', '[plugins] takes'),
            'a clone command that climbs out': ('[mcp.servers.x]\ncommand = "{{CODE_DIR}}/repo/../../bin/sh"\n',
                                                'a local clone command is {{CODE_DIR}}/<repo>/<path'),
            'a bare clone root': ('[mcp.servers.x]\ncommand = "{{CODE_DIR}}/server"\n', 'a local clone command is'),
            'an unknown placeholder': ('[mcp.servers.x]\ncommand = "{{HOME}}/bin/server"\n',
                                       'unknown command placeholder HOME'),
            'an unknown placeholder in an arg': ('[mcp.servers.x]\ncommand = "uv"\nargs = ["{{HOME}}/mcp"]\n',
                                                 'unknown command placeholder HOME'),
            'a pack path that climbs out': ('[mcp.servers.x]\ncommand = "uv"\nargs = ["--project", "{{PACK_DIR}}/../../bin"]\n',
                                            'a team pack path is {{PACK_DIR}}/<path>, without ..'),
            'a bare pack root': ('[mcp.servers.x]\ncommand = "{{PACK_DIR}}"\n', 'a team pack path is'),
            'a pack command inside .venv': ('[mcp.servers.x]\ncommand = "{{PACK_DIR}}/mcp/.venv/bin/x"\n',
                                            'a team pack path points into .venv'),
            'a Sentry instance without a keychain service': ('[sentry.instances.main]\nhost = "sentry.example.com"\n',
                                                             '[sentry]: Sentry instance main needs a keychain service'),
            'a Sentry keychain service a shell would split': (
                '[sentry.instances.main]\nhost = "sentry.example.com"\nkeychain = "k; curl x"\n',
                '[sentry]: Sentry instance main needs a keychain service'),
            'two Sentry instances on one server': (
                '[sentry.instances.a]\nhost = "a.example.com"\nkeychain = "k"\nserver = "Sentry-X"\n'
                '[sentry.instances.b]\nhost = "b.example.com"\nkeychain = "k2"\nserver = "sentry-x"\n',
                '[sentry]: Sentry instances must each name a different MCP server'),
            'a token in a Sentry instance': (
                f'[sentry.instances.main]\nhost = "sentry.example.com"\nkeychain = "k"\nnote = "{TOKEN}"\n',
                '[sentry] looks like it holds an inline secret'),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                preset = self.home / f'{name}.toml'
                preset.write_text(text)
                self.refused(str(preset), expected)

    def test_placeholders_and_certificates_are_not_secrets(self) -> None:
        pack = self.pack()
        (pack / 'skills/team-howto/references/auth.md').write_text(
            'Export ' + 'gh' + 'p_XXXXXXXXXXXXXXXXXXXX or ' + 'sk-' + 'ant-... from your vault.\n')
        (pack / 'skills/team-howto/references/ca.pem').write_text('-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n')
        self.setup('--preset', str(pack), *FLAGS, '--apply')
        self.assertTrue((self.root / 'skills/team-howto/references/ca.pem').is_file())

    def test_a_pack_with_a_secret_an_escaping_link_long_rules_or_a_kit_skill_name_is_refused(self) -> None:
        skill = Path('skills/team-howto')
        cases = {
            'a .env file': (skill / '.env', 'NAME=value\n', '.env looks like a credential file'),
            'an .envrc': (skill / '.envrc', 'export NAME=value\n', '.envrc looks like a credential file'),
            'a tfvars file': (skill / 'prod.tfvars', 'name = "value"\n', 'prod.tfvars looks like a credential file'),
            'a pgpass': (skill / '.pgpass', 'host:5432:db:user:value\n', '.pgpass looks like a credential file'),
            'a secrets file': (skill / 'secrets.yml', 'name: value\n', 'secrets.yml looks like a credential file'),
            'a private key': (skill / 'references/deploys.md', PRIVATE_KEY, 'deploys.md holds a token or private key'),
            'a key file': (skill / 'deploy.key', PRIVATE_KEY, 'deploy.key holds a token or private key'),
            'a PGP key': (skill / 'references/signing.asc', '-----BEGIN PGP ' + 'PRIVATE KEY BLOCK-----\nlQOYBF\n',
                          'signing.asc holds a token or private key'),
            'a PuTTY key': (skill / 'deploy.ppk', 'PuTTY-User-' + 'Key-File-3: ssh-ed25519\n', 'deploy.ppk holds a token'),
            'text that is not UTF-8': (skill / 'references/notes.md', b'caf\xe9\n',
                                       'skills/team-howto/references/notes.md is not UTF-8 text'),
            'a token': (Path('rules.md'), TOKEN + '\n', 'rules.md holds a token or private key'),
            'a stripe key': (skill / 'pay.md', 'sk_' + 'live_' + 'A1b2C3d4E5f6G7h8I9j0\n', 'pay.md holds a token'),
            'a slack webhook': (skill / 'chat.md', 'https://hooks.slack.com/' + 'services/T0123ABCD/B0123ABCD/A1b2C3d4E5f6G7h8\n',
                                'chat.md holds a token'),
            'an npm token': (skill / 'npm.md', 'np' + 'm_' + 'A1b2C3d4E5f6G7h8I9j0K1l2\n', 'npm.md holds a token'),
            'rules over the cap': (Path('rules.md'), 'word ' * 301, 'rules.md has 301 words, over the cap of 300'),
            'a kit skill name': (Path('skills/conventions/SKILL.md'), '# Mine\n', 'pack skill conventions has the name of a kit skill'),
        }
        for name, (path, text, expected) in cases.items():
            with self.subTest(name):
                pack = self.pack(self.home / name.replace(' ', '-'))
                (pack / path).parent.mkdir(parents=True, exist_ok=True)
                (pack / path).write_bytes(text if isinstance(text, bytes) else text.encode())
                self.refused(str(pack), expected)
        links = {'a link out of the pack': ('../../../../secret.md', 'points outside the pack'),
                 'a link to a folder': ('..', 'must name a file in the pack'),
                 'a link to a file outside skills and rules': ('../../../README.md', 'must name a file in the pack')}
        for name, (target, expected) in links.items():
            with self.subTest(name):
                pack = self.pack(self.home / name.replace(' ', '-'))
                (pack / 'skills/team-howto/references/link.md').symlink_to(target)
                self.refused(str(pack), expected)

    def test_an_archive_with_unsafe_or_colliding_entries_is_refused(self) -> None:
        def member(name: str, kind: bytes, link: str = '') -> tarfile.TarInfo:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, link
            return info

        skill = ('skills/team-a/SKILL.md', SKILL)
        cases = {
            'a path with ..': (member('example-org-team-pack-0/../evil.md', tarfile.REGTYPE), 'leaves the pack'),
            'an absolute path': (member('/example-org-team-pack-0/evil.md', tarfile.REGTYPE), 'leaves the pack'),
            'a hard link': (member('example-org-team-pack-0/skills/team-a/hard.md', tarfile.LNKTYPE,
                                   'example-org-team-pack-0/skills/team-a/SKILL.md'), 'hard.md is not a regular file or link'),
            'a FIFO': (member('example-org-team-pack-0/skills/team-a/pipe', tarfile.FIFOTYPE), 'pipe is not a regular file or link'),
            'names that differ in case': (('skills/team-a/notes.md', b'two\n'), 'skills/team-a/Notes.md and skills/team-a/notes.md differ only in case'),
            'names that differ in Unicode form': (('skills/team-a/Nótes.md', b'two\n'),
                                                  'skills/team-a/Nótes.md and skills/team-a/Nótes.md differ only in case or Unicode form'),
        }
        for name, (extra, expected) in cases.items():
            with self.subTest(name):
                self.gh_archive([skill, ('skills/team-a/Notes.md', b'one\n'), ('skills/team-a/Nótes.md', b'one\n'), extra])
                self.refused(GH, expected)


    def test_a_skill_that_extends_debug_is_listed_in_debug_on_every_host(self) -> None:
        hosts = ('claude', 'codex', 'cursor')
        for host in hosts:
            self.host_cli(host)
        pack = self.pack()
        (pack / 'skills/team-data').mkdir()
        (pack / 'skills/team-data/SKILL.md').write_text(
            '---\nname: team-data\nextends: debug\ndescription: >\n  Team schemas and\n  tunnels.\n---\n')
        flags = ('--hosts', *hosts, '--components', 'rules', 'skills', '--apply')
        self.setup('--preset', str(pack), *flags)
        line = f'- `team-data`: Team schemas and tunnels. Read {self.root}/skills/team-data/SKILL.md.'
        for host in hosts:
            with self.subTest(host):
                debug = (self.home / f'.{host}/skills/debug/SKILL.md').read_text()
                self.assertIn(line, debug)
                self.assertNotIn('<!-- agent-kit: extensions -->', debug)
                self.assertNotIn('team-howto', debug, 'a skill that does not extend debug is listed')
        (pack / 'skills/team-data/SKILL.md').write_text('---\nname: team-data\ndescription: Team data.\n---\n')
        self.setup('--preset', str(pack), *flags)
        self.assertIn('No installed skill extends debug.', (self.home / '.codex/skills/debug/SKILL.md').read_text())

    def test_a_server_left_out_for_a_missing_command_is_listed_under_skipped(self) -> None:
        self.host_cli('claude')
        code = self.home / 'Code'
        code.mkdir()
        pack = self.pack()
        (pack / 'agent-kit-preset.toml').write_text(
            f'[kit]\nCODE_DIRS_JSON = ["{code}"]\n'
            '[mcp.servers.absent]\ncommand = "{{CODE_DIR}}/absent-mcp/.venv/bin/absent-mcp"\n'
            '[mcp.servers.by-arg]\ncommand = "/bin/sh"\nargs = ["{{CODE_DIR}}/absent-repo/serve.sh"]\n'
            '[mcp.servers.by-npx]\ncommand = "npx"\nargs = ["-y", "some-mcp"]\n'
            '[mcp.servers.packed]\ncommand = "uv"\nargs = ["run", "--project", "{{PACK_DIR}}/mcp", "packed-mcp"]\n')
        result = self.setup('--preset', str(pack), '--hosts', 'claude', '--components', 'rules', 'mcp')
        skipped = result.stdout.split('Skipped:\n')[1]
        self.assertIn(f'MCP server absent: {code}/absent-mcp/.venv/bin/absent-mcp is not an executable in your clone yet', skipped)
        self.assertIn(f'MCP server by-arg: {code}/absent-repo/serve.sh is not in your clone yet', skipped)
        self.assertIn('MCP server by-npx: npx is missing', skipped)
        self.assertIn('MCP server packed: the team pack has no mcp/ project for {{PACK_DIR}}', skipped)
        (code / 'absent-repo').mkdir()
        (code / 'absent-repo/serve.sh').write_text('#!/bin/sh\n')
        result = self.setup('--preset', str(pack), '--hosts', 'claude', '--components', 'rules', 'mcp')
        self.assertNotIn('MCP server by-arg', result.stdout, 'a server whose clone now holds its path is still left out')
        self.write_mcp_project(pack / 'mcp')
        result = self.setup('--preset', str(pack), '--hosts', 'claude', '--components', 'rules', 'mcp', '--apply')
        self.assertIn("MCP server packed: uv is missing, which syncs the team pack's mcp/ project", result.stdout)
        servers = json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers']
        self.assertEqual(list(servers), ['by-arg'], 'a server that cannot start was installed')

    def write_mcp_project(self, folder: Path, lock: bool = False) -> None:
        """A uv project whose console script answers an MCP initialize request on stdin, with the
        local leftovers a pack copy leaves out; its uv.lock written by the real uv when lock."""
        (folder / 'src/team_tools_mcp').mkdir(parents=True)
        (folder / 'pyproject.toml').write_text(
            '[project]\nname = "team-tools-mcp"\nversion = "0.1.0"\nrequires-python = ">=3.9"\n'
            '[project.scripts]\nteam-tools-mcp = "team_tools_mcp:main"\n'
            '[build-system]\nrequires = ["uv_build>=0.8"]\nbuild-backend = "uv_build"\n')
        (folder / 'src/team_tools_mcp/__init__.py').write_text(
            'import json\nimport sys\n\n\ndef main() -> None:\n    request = json.loads(sys.stdin.readline())\n'
            '    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": '
            'request["params"]["protocolVersion"], "capabilities": {}, "serverInfo": {"name": "team-tools", "version": "0.1.0"}}}))\n')
        if not lock:
            (folder / 'uv.lock').write_text('version = 1\n')
        if lock:
            subprocess.run([shutil.which('uv') or 'uv', 'lock', '--project', str(folder)], check=True, capture_output=True,
                           env={**self.uv_env(), 'PATH': '/usr/bin:/bin'})
        for leftover in ('.venv/bin/python', '__pycache__/x.pyc', '.ruff_cache/x', '.pytest_cache/x'):
            (folder / leftover).parent.mkdir(parents=True, exist_ok=True)
            (folder / leftover).write_text('local\n')

    def uv_env(self) -> dict[str, str]:
        """uv offline from a Python download, with a TMPDIR of its own: it keeps a lock file there."""
        (self.home / 'tmp').mkdir(exist_ok=True)
        return {'HOME': str(self.home), 'UV_PYTHON': sys.executable, 'UV_PYTHON_DOWNLOADS': 'never',
                'TMPDIR': str(self.home / 'tmp')}

    def test_a_pack_bundled_mcp_server_is_synced_and_runs_from_its_rendered_command(self) -> None:
        if not shutil.which('uv'):
            if os.environ.get('CI'):
                self.fail('uv is not installed on this CI runner')
            self.skipTest('uv is not installed')
        self.host_cli('claude')
        self.link('uv')
        pack = self.pack()
        self.write_mcp_project(pack / 'mcp', lock=True)
        (pack / 'agent-kit-preset.toml').write_text(
            '[mcp.servers.pack-tools]\ncommand = "uv"\n'
            'args = ["run", "--project", "{{PACK_DIR}}/mcp", "--frozen", "--no-dev", "team-tools-mcp"]\n')
        flags = ('--preset', str(pack), '--hosts', 'claude', '--components', 'rules', 'mcp', '--apply')
        result = self.setup(*flags, env=self.uv_env())
        project = self.root / 'pack/mcp'
        self.assertIn(f'MCP: team pack mcp/ project synced ({project})', result.stdout)
        kept = [record['target'] for record in self.state()['managed'].values() if str(project) in record['target']]
        self.assertEqual(sorted(Path(target).relative_to(project).as_posix() for target in kept),
                         ['pyproject.toml', 'src/team_tools_mcp/__init__.py', 'uv.lock'])
        server = json.loads((self.home / '.claude/mcp.json').read_text())['mcpServers']['pack-tools']
        self.assertEqual(server['command'], str(self.shim / 'uv'), 'not the uv setup found on its PATH')
        self.assertEqual(server['args'][:3], ['run', '--project', str(project)])
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                   'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}}}
        # A host started from the Dock: no ~/.local/bin on PATH.
        answer = subprocess.run([server['command'], *server['args']], input=json.dumps(request) + '\n', capture_output=True,
                                text=True, timeout=120, env={**self.uv_env(), 'PATH': '/usr/bin:/bin'})
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(json.loads(answer.stdout)['result']['serverInfo']['name'], 'team-tools')

        # The environment uv made in the pack copy is neither drift nor a collision.
        self.assertTrue((project / '.venv').is_dir())
        self.assertEqual(self.doctor()['drift'], [])
        rerun = self.setup(*flags, env=self.uv_env())
        self.assertIn('Installed 0 changes', rerun.stdout)
        self.assertNotIn('MCP:', rerun.stdout, 'a project synced at its uv.lock is synced again')
        self.assertEqual(self.doctor()['drift'], [])
        # A sync that failed (no environment) is retried by the next sync, though nothing else changed.
        shutil.rmtree(project / '.venv')
        retried = self.setup('sync', env=self.uv_env())
        self.assertIn('sync: nothing changed', retried.stdout)
        self.assertIn(f'MCP: team pack mcp/ project synced ({project})', retried.stdout)
        self.assertTrue((project / '.venv').is_dir())
        self.assertEqual([path.name for path in (self.home / 'tmp').iterdir() if not path.name.startswith('uv-')], [],
                         'setup left files in TMPDIR')


class SyncStampTests(unittest.TestCase):
    def test_a_sync_whose_stamp_cannot_be_written_says_so(self) -> None:
        sys.path.insert(0, str(SOURCE / 'bin/lib'))
        import mcp_plan

        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / 'uv.lock').write_text('version = 1\n')
            (project / '.venv').write_text('not a folder\n')
            said = mcp_plan.sync_pack_mcp(('/usr/bin/true', str(project)))
        self.assertIn(f'team pack mcp/ project synced ({project}), but its stamp could not be written', said)
        self.assertIn('the next agent-setup or agent-kit sync runs it again', said)

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TeamPackTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': result.testsRun, 'successful': result.wasSuccessful(),
                                'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
