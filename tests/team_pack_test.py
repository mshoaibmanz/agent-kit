"""Team packs: a preset repository that also carries skills and a rules block. Each case runs
agent-setup in the installer UX fake home on a synthetic pack: a local folder, or a gh fixture that
serves gh:example-org/team-pack from a local git repository or a crafted archive (`gh api` answers the
head commit and the tarball, as GitHub does). No network or credential is used."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from installer_ux_test import SOURCE, InstallerUxFixture  # noqa: E402

RULES = '## Team rules\n\n- Name the ticket in every branch.\n'
GIT = ('git', '-c', 'user.name=Pack Author', '-c', 'user.email=author@example.com', '-c', 'commit.gpgsign=false')
GH = 'gh:example-org/team-pack'
FLAGS = ('--hosts', 'claude', '--components', 'rules', 'skills')
SKILL = b'---\nname: team-a\ndescription: A.\n---\n'
# Strings a secret scanner would flag, built so this file holds none.
TOKEN = 'gh' + 'p_' + 'A1b2C3d4E5f6G7h8I9j0'
PRIVATE_KEY = '-----BEGIN OPENSSH ' + 'PRIVATE KEY-----\n'


class TeamPackTests(InstallerUxFixture):
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
                              env={'PATH': str(self.shim), 'HOME': str(self.home), 'GIT_CONFIG_GLOBAL': '/dev/null'}).stdout.strip()

    def commit(self, folder: Path, message: str) -> str:
        self.git(folder, 'add', '-A')
        self.git(folder, 'commit', '-q', '-m', message)
        return self.git(folder, 'rev-parse', 'HEAD')

    def served(self, folder: Path | None = None) -> tuple[Path, str]:
        """A pack in a git repository that the gh fixture serves, and its first commit."""
        repository = self.pack(folder or self.home / 'served')
        self.git(repository, 'init', '-q')
        first = self.commit(repository, 'Pack v1')
        self.gh_fixture(repository)
        return repository, first

    def gh_fixture(self, repository: Path) -> None:
        """gh that serves repository as example-org/team-pack: the head commit and its tarball. Each
        call's endpoint is logged to gh.log in the fake home."""
        self.gh_script(f'''repo={json.dumps(str(repository))}
echo "$2" >> {json.dumps(str(self.home / 'gh.log'))}
case "$1 $2" in
  "api repos/example-org/team-pack/commits/HEAD") exec git -C "$repo" rev-parse HEAD ;;
  "api repos/example-org/team-pack/tarball/"*)
    sha=${{2##*/}}
    exec git -C "$repo" archive --format=tar --prefix="example-org-team-pack-$sha/" "$sha" ;;
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
        (repository / 'agent/preset.toml').write_text('[kit]\nREVIEW_BASE = "main"\n')
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
        result = self.setup('--preset', GH + '/agent/preset.toml', *FLAGS, '--apply')
        self.assertIn('skills team-a', result.stdout)
        self.assertEqual(sorted(path.relative_to(self.root / 'pack').as_posix() for path in (self.root / 'pack').rglob('*')
                                if path.is_file()), ['skills/team-a/SKILL.md'])
        self.assertIn('no agent/missing.toml in the repository',
                      self.setup('--preset', GH + '/agent/missing.toml', *FLAGS, code=2).stderr)
        local = repository / 'agent/preset.toml'
        self.assertIn(f'pack {local} at its working tree: content unchanged since the install',
                      self.setup('--preset', str(local), *FLAGS, '--apply').stdout)
        self.assertEqual(self.state()['configuration']['preset']['source'], str(local))
        self.assertIn('no such file or folder', self.setup('--preset', str(repository / 'agent/missing.toml'), *FLAGS, code=2).stderr)

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


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TeamPackTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': result.testsRun, 'successful': result.wasSuccessful(),
                                'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
