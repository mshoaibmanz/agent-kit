"""agent-kit dashboard: install into the installer UX fake home, plant credential-shaped values in
the installed catalog, the live host config, the overlay and a Keychain fixture, generate the page
and check each section, that no planted value reaches it, and which processes it ran. No network or
real credential is used: the Keychain is a fixture script named by AGENT_KIT_SECURITY."""

from __future__ import annotations

import base64
import hashlib
import html
import json
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from installer_ux_test import SOURCE, InstallerUxFixture  # noqa: E402

sys.path.insert(0, str(SOURCE / 'bin/lib'))
sys.path.insert(0, str(SOURCE / 'hooks/lib'))
from credentials import MASK, mask_tokens, show_args, show_url  # noqa: E402
from preset import validate_catalog  # noqa: E402

SECTIONS = ('hosts', 'mcp', 'unmanaged', 'overlay', 'data', 'skills', 'roles', 'hooks', 'pack', 'work', 'actions')
# Values a page must never hold, built so this file holds no token a scanner would flag.
PLANTED = {
    'keychain value': 'kc-value-' + 'Zq81xw',
    'live env value': 'live-env-' + 'Pf27rk',
    'catalog env value': 'cat-env-' + 'Hn55dw',
    'overlay value': 'overlay-' + 'Vb93tl',
    'preset value': 'preset-' + 'Mm40sa',
    'url token': 'url-tok-' + 'Gq18ye',
    'header value': 'plainS3cret' + 'HDR',
    'flag value': 'plainS3cret' + 'CS',
    'url path key': 'plainS3cret' + 'PATH',
    'long path key': 'k3y' * 6,
    'userinfo password': 'ui-pass-' + 'Kd71qe',
    'assigned value': 'assigned-' + 'Wq62hn',
    'bearer value': 'bearer-' + 'Tz48mv',
    'after-flag value': 'after-flag-' + 'Lp93cx',
    'jwt': 'eyJ' + 'hbGciOiJIUzI1NiJ9' + '.eyJ' + 'zdWIiOiIxMjM0NTY3ODkwIn0' + '.c2lnbmF0dXJlc2ln',
    'sentry token': 'sntry' + 's_' + 'eyJpYXQiOjE3MDAwMDAw',
    'atlassian token': 'ATA' + 'TT3xFfGF0' + 'abcdefghij',
    'google token': 'ya' + '29.' + 'a0AfH6SMBx' + 'abcdefghij',
    'short flag value': 'shortFlag' + 'VALUE1',
    'license key': 'licVALUE' + '22',
    'json value': 'jsonVALUE' + '33',
    'passphrase': 'ppVALUE' + '44',
    'numeric password': '8392' + '01576',
    'path-shaped secret': '/hunter' + 'TWO',
    'dot-shaped secret': '.s3cr3t' + 'val',
}
TOKEN = 'gh' + 'p_' + 'A1b2C3d4E5f6G7h8I9j0'
SSH_CONFIG = '''Host db-tunnel-orders
  # ro-mysql: user=reader
  LocalForward 15306 db.example.test:3306
Host db-tunnel-orders-stg
  # ro-mysql: user=reader
  LocalForward 15307 db-stg.example.test:3306
'''
# The only processes the dashboard may start; a presence check never carries -w or -g.
ALLOWED = {'security', 'git', 'claude-account', 'open', 'xdg-open'}
LAUNCHER = '''import json, os, runpy, sys
log = open(os.environ['DASHBOARD_AUDIT_LOG'], 'a')
def hook(event, args):
    if event == 'subprocess.Popen':
        argv = args[1] if isinstance(args[1], (list, tuple)) else [args[1]]
        log.write(json.dumps([os.fspath(a) for a in argv]) + '\\n'); log.flush()
    elif event in ('os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'pty.spawn'):
        log.write(json.dumps([event, repr(args)[:200]]) + '\\n'); log.flush()
sys.addaudithook(hook)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


def page_text(page: str) -> str:
    return html.unescape(re.sub(r'<[^>]+>', ' ', page))


def section(page: str, key: str) -> str:
    start = page.index(f'<section id="{key}" class="card')
    return page[start:page.index('</section>', start)]


def append_line(target: Path) -> str:
    path = shlex.quote(str(target))
    return f'{{ [ ! -s {path} ] || [ -z "$(tail -c1 {path})" ] || echo; printf \'%s\\n\' KEY=value; }} >> {path}'


def nav(page: str, key: str) -> str:
    found = re.search(rf'<a href="#{key}" data-k="{key}">.*?</a>', page)
    assert found is not None, key
    return found.group()


def commands(page: str) -> list[str]:
    return [html.unescape(c) for c in re.findall(r'<div class="cmd"><code>[^<]*</code><button class="cp" type="button" data-c="([^"]*)"', page)]


class Fixture(InstallerUxFixture):
    """The fake home with an install, and the dashboard run through an audit-hook launcher."""

    def install(self, *flags: str, components: tuple[str, ...] = ('rules', 'skills', 'hooks', 'mcp')) -> Path:
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps({'mcpServers': {
            'docs': {'command': 'npx', 'args': ['-y', 'example-docs-server']},
            'tracker': {'url': 'https://mcp.example.com/v1'}}}))
        for tool in ('npx', 'claude'):
            (self.shim / tool).write_text('#!/bin/sh\nexit 0\n')
            (self.shim / tool).chmod(0o755)
        self.link('jq')
        (self.home / 'code').mkdir(exist_ok=True)
        self.setup('--hosts', 'claude', *flags, '--components', *components,
                   *(['--mcp-catalog', str(catalog)] if 'mcp' in components else []),
                   '--repo-roots', str(self.home / 'code'), '--apply')
        return catalog

    def doctor(self, *flags: str, kit: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.root / 'bin/agent-kit'), 'doctor', *flags], capture_output=True,
                              text=True, env=self.env(kit=kit), timeout=120, stdin=subprocess.DEVNULL)

    def keychain(self) -> Path:
        keychain = self.home / 'security'
        keychain.write_text('#!/bin/sh\n'
                            f'echo "$*" >> {json.dumps(str(self.home / "security.log"))}\n'
                            f'case " $* " in *" -w "*|*" -g "*) echo {PLANTED["keychain value"]}; exit 0 ;; esac\n'
                            'case " $* " in *" example/vault "*|*" reader@db-tunnel-orders "*) exit 0 ;; esac\n'
                            'exit 44\n')
        keychain.chmod(0o755)
        return keychain

    def plant(self) -> None:
        """Credential-shaped values everywhere the page reads, as a hand-edited install holds them."""
        installed = self.root / 'mcp/servers.json'
        servers = json.loads(installed.read_text())
        servers['mcpServers'].update({
            'vault': {'command': 'vault-mcp', 'env': {
                'VAULT_TOKEN': {'$keychain': {'service': 'example/vault', 'account': 'me'}},
                'VAULT_ADDR': 'https://vault.example.com', 'API_SECRET': PLANTED['catalog env value']}},
            'wrapped': {'command': '{{KIT_DIR}}/bin/example-mcp',
                        'credentials': [{'service': 'example/wrapped', 'account': 'me'}]},
            'shortflags': {'command': 'uvx', 'args': [
                'db-mcp', '-p', PLANTED['short flag value'], '--license-key', PLANTED['license key'],
                '{"apiKey":"' + PLANTED['json value'] + '"}', '--passphrase', PLANTED['passphrase'],
                '--password=' + PLANTED['numeric password'], 'DB_PASSWORD=' + PLANTED['numeric password'],
                '--token', PLANTED['path-shaped secret'], '--secret', PLANTED['dot-shaped secret']]},
            'notes': {'url': 'https://notes.example.com/mcp?token=' + PLANTED['url token']},
            'pathkey': {'url': 'https://mcp.example.com/v1/' + PLANTED['url path key'] + '/sse'},
            'longkey': {'url': 'https://mcp.example.com/' + PLANTED['long path key'] + '/mcp'},
            'userinfo': {'url': 'https://reader:' + PLANTED['userinfo password'] + '@mcp.example.com/x'},
            'flags': {'command': 'npx', 'args': [
                '-y', 'example-docs-server', '--header', 'X-Api-Key: ' + PLANTED['header value'],
                '--client-secret=' + PLANTED['flag value'], 'API_KEY=' + PLANTED['assigned value'],
                '-H', 'Authorization: Bearer ' + PLANTED['bearer value'], '--token', PLANTED['after-flag value'],
                PLANTED['jwt'], PLANTED['sentry token'], PLANTED['atlassian token'], PLANTED['google token']]},
        })
        installed.write_text(json.dumps(servers))
        live = self.home / '.claude/mcp.json'
        data = json.loads(live.read_text())
        data['mcpServers']['handmade'] = {'command': 'x', 'env': {'KEY': PLANTED['live env value']}}
        live.write_text(json.dumps(data))
        local = self.root / 'local'
        local.mkdir(exist_ok=True)
        (local / 'kit.env').write_text(f"REVIEW_BASE=main\nAPI_PASSWORD={PLANTED['overlay value']}\n"
                                       f"GIT_AUTHOR='{TOKEN}'\nBQRO_PROJECT=example-project\n")
        (local / 'preset.env').write_text(f"REVIEW_BASE=develop\nTEAM_SECRET={PLANTED['preset value']}\n")
        (self.home / '.ssh').mkdir(exist_ok=True)
        (self.home / '.ssh/config').write_text(SSH_CONFIG)
        state = self.home / '.claude/state'
        state.mkdir(parents=True, exist_ok=True)
        (state / 'db-tunnels.tsv').write_text('alias\tport\tkind\tchecked\tvia\tdatabases\n'
                                              'db-tunnel-orders-stg\t15307\tSTAGING\t2026-01-02\tvia login path orders-stg-ro\torders\n')
        (state / 'task-bindings').mkdir(exist_ok=True)
        (state / 'task-bindings/abcd1234').write_text('project:demo/first-item\n')
        work = self.home / 'agent-work/projects/demo'
        (work / 'items/first-item').mkdir(parents=True, exist_ok=True)
        # Token shapes in shown text: only the page's last pass can catch these.
        (work / 'PROJECT.md').write_text(f"# demo\nstatus: active {PLANTED['google token']} {PLANTED['jwt']}\n")
        (work / 'items/first-item/task.json').write_text('{"item": "first-item", "status": "open"}\n')
        self.keychain()
        (self.shim / 'mysql_config_editor').write_text(
            f'#!/bin/sh\necho "$*" >> {json.dumps(str(self.home / "mysql_config_editor.log"))}\n')
        (self.shim / 'mysql_config_editor').chmod(0o755)

    def plant_unmanaged(self) -> None:
        """What the kit does not own: a local settings file, a hand-made skill, a marketplace plugin,
        servers and connectors in Claude's state file, a repo with its own config, a kit command copy."""
        claude = self.home / '.claude'
        (claude / 'settings.local.json').write_text(json.dumps({'env': {'API_KEY': PLANTED['live env value']}}))
        (claude / 'skills/handmade-skill').mkdir(parents=True)
        (claude / 'skills/handmade-skill/SKILL.md').write_text('---\nname: handmade-skill\ndescription: x\n---\n')
        (claude / 'plugins').mkdir()
        (claude / 'plugins/installed_plugins.json').write_text(json.dumps(
            {'version': 2, 'plugins': {'lint-helper@example-market': [{'version': '1.2.3', 'installPath': '/x'}]}}))
        (self.home / '.claude.json').write_text(json.dumps({
            'oauthAccount': {'emailAddress': 'someone@example.com'},
            'mcpServers': {'scratch-server': {'command': 'x', 'env': {'TOKEN': PLANTED['catalog env value']}}},
            'claudeAiMcpEverConnected': ['claude.ai Example Docs']}))
        repo = self.home / 'code/sample-repo'
        (repo / '.claude').mkdir(parents=True)
        (repo / 'AGENTS.md').write_text('# rules\n')
        (repo / '.claude/settings.local.json').write_text('{}')
        (self.shim / 'agent-task').write_text('#!/bin/sh\nexit 0\n')
        (self.shim / 'agent-task').chmod(0o755)

    def env(self, kit: Path | None = None, security: Path | None = None) -> dict[str, str]:
        env = {'HOME': str(self.home), 'PATH': str(self.shim), 'TMPDIR': str(self.tmp), 'LANG': 'C.UTF-8',
               'AGENT_KIT_SECURITY': str(security or self.home / 'security'), 'GIT_CONFIG_GLOBAL': '/dev/null',
               'DASHBOARD_AUDIT_LOG': str(self.home / 'audit.log')}
        if kit is not None:
            env['AGENT_KIT_DIR'] = str(kit)
        return env

    def dashboard(self, *flags: str, kit: Path | None = None, security: Path | None = None
                  ) -> tuple[str, subprocess.CompletedProcess]:
        out = self.home / 'out/index.html'
        launcher = self.home / 'audit_launcher.py'
        launcher.write_text(LAUNCHER)
        script = (kit or self.root) / 'bin/agent-kit'
        result = subprocess.run([sys.executable, str(launcher), str(script), 'dashboard', '--no-open', '--out', str(out),
                                 *flags], capture_output=True, text=True, env=self.env(kit, security), timeout=120,
                                stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        return out.read_text(), result

    def processes(self) -> list[list[str]]:
        log = self.home / 'audit.log'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


class DashboardPageTests(unittest.TestCase):
    """One planted install and one page; each case reads one part of it."""

    fx: Fixture
    page: str
    text: str
    before: dict[Path, bytes]
    after: dict[Path, bytes]

    @classmethod
    def setUpClass(cls) -> None:
        fx = cls.fx = Fixture()
        fx.setUp()
        try:
            fx.host_cli('codex')
            fx.install('codex')
            fx.plant()
            fx.plant_unmanaged()
            snapshot = lambda: {p: p.read_bytes() for p in fx.root.rglob('*')  # noqa: E731
                                if p.is_file() and '__pycache__' not in p.parts}
            cls.before = snapshot()
            cls.page, _ = fx.dashboard()
            cls.after = snapshot()
            cls.text = page_text(cls.page)
        except BaseException:
            fx.tearDown()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fx.tearDown()

    def test_every_section_in_order_and_the_page_mode(self) -> None:
        found = re.findall(r'<section id="([a-z]+)" class="card', self.page)
        self.assertEqual(tuple(found), ('attention', *SECTIONS))
        nav = re.findall(r'<a href="#([a-z]+)" data-k=', self.page)
        self.assertEqual(tuple(nav), ('attention', *SECTIONS), 'one sidebar link per section, attention first')
        self.assertNotIn('not read:', self.page)
        self.assertEqual(stat.S_IMODE((self.fx.home / 'out/index.html').stat().st_mode), 0o600)
        self.assertNotIn('class="tiles"', self.page, 'the sidebar and card heads carry the numbers')
        ids = re.findall(r'\sid="([^"]+)"', self.page)
        self.assertEqual(len(ids), len(set(ids)), 'an element id twice')

    def test_the_sidebar_counts_and_card_heads(self) -> None:
        self.assertRegex(nav(self.page, 'hosts'), r'<span class="n">2</span>')
        self.assertRegex(nav(self.page, 'unmanaged'), r'<i class="dot warn"')
        self.assertIn('2 configured, 2 with drift', page_text(section(self.page, 'hosts')))
        self.assertRegex(page_text(section(self.page, 'hooks')), r'Hooks\s+\d+ hooks, 0 blocking')
        self.assertIn('2 tunnels, 1 staging', page_text(section(self.page, 'data')))

    def test_csp_allows_only_the_pages_own_script_and_style(self) -> None:
        policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', self.page)
        assert policy is not None
        self.assertNotIn('unsafe-inline', policy.group(1))
        for tag in ('script', 'style'):
            body = re.search(rf'<{tag}>(.*?)</{tag}>', self.page, re.S)
            assert body is not None
            digest = base64.b64encode(hashlib.sha256(body.group(1).encode()).digest()).decode()
            self.assertIn(f"{tag}-src 'sha256-{digest}'", policy.group(1))

    def test_each_attention_item_links_to_its_row(self) -> None:
        targets = re.findall(r'<li><a href="#([^"]+)">', section(self.page, 'attention'))
        for wanted in ('host-claude', 'mcp-wrapped', 'data-orders-stg', 'unmanaged-plugins'):
            self.assertIn(wanted, targets)
        for target in targets:
            self.assertIn(f' id="{target}"', self.page, f'a Needs attention link to no element: {target}')

    def test_no_planted_secret_reaches_the_page(self) -> None:
        for what, value in PLANTED.items():
            self.assertNotIn(value, self.page, f'the page shows the {what}')
        self.assertNotIn(TOKEN, self.page, 'a token-shaped overlay value is shown')
        self.assertNotIn('someone@example.com', self.page, "a value from Claude's state file")
        self.assertNotIn('reader:', self.page, 'URL userinfo is shown')

    def test_only_allowlisted_processes_run_and_no_value_is_asked_for(self) -> None:
        calls = self.fx.processes()
        self.assertTrue(calls, 'the audit hook saw no process')
        for argv in calls:
            self.assertIn(Path(argv[0]).name, ALLOWED, argv)
            self.assertFalse(any(a.endswith('mysql_config_editor') for a in argv), argv)
            if Path(argv[0]).name == 'security':
                self.assertEqual(argv[1], 'find-generic-password', argv)
                self.assertFalse({'-w', '-g'} & set(argv), argv)
            if Path(argv[0]).name == 'claude-account':
                self.assertEqual(argv[1:], ['dirs'], argv)
            if Path(argv[0]).name == 'git':
                self.assertEqual(argv[1], '--no-optional-locks', 'a git call that may write the index')
        self.assertTrue(any(Path(a[0]).name == 'security' for a in calls), 'the Keychain fixture was never asked')
        self.assertFalse((self.fx.home / 'mysql_config_editor.log').exists(), 'the login-path store was read')

    def test_nothing_is_written_into_the_kit_or_the_source(self) -> None:
        self.assertEqual(self.before, self.after, 'the dashboard wrote into the kit')
        self.assertEqual(self.fx.status(), self.fx.source_status, 'changed the source checkout')

    def test_hosts(self) -> None:
        hosts = page_text(section(self.page, 'hosts'))
        self.assertRegex(hosts, r'claude\s+configured')
        self.assertRegex(hosts, r'codex\s+configured')
        self.assertIn('mcp: servers differ from the catalog', hosts)
        self.assertIn('mcp vault in the catalog, not live', hosts, 'codex drift by server name')

    def test_mcp_reads_the_installed_catalog_and_masks_every_credential(self) -> None:
        mcp = page_text(section(self.page, 'mcp'))
        self.assertRegex(mcp, r'Keychain example/vault / me\s+present')
        self.assertRegex(mcp, r'Keychain example/wrapped / me\s+missing')
        self.assertIn('security add-generic-password -s example/wrapped -a me -w', mcp)
        self.assertIn('VAULT_ADDR', mcp)
        self.assertIn('Inline in the catalog: API_SECRET', mcp)
        hidden = shlex.quote(MASK)
        self.assertIn(f'uvx db-mcp -p {hidden} --license-key {hidden} {hidden} --passphrase {hidden}', mcp)
        self.assertIn('OAuth', mcp)
        self.assertIn('handmade', mcp, 'a live-only server is named')
        self.assertIn(f'https://notes.example.com/mcp?token={MASK}', mcp)
        self.assertIn(f'https://mcp.example.com/v1/{MASK}/sse', mcp)
        self.assertIn('https://mcp.example.com/x', mcp)
        self.assertIn(f'--client-secret={MASK}', mcp)
        self.assertIn(f'X-Api-Key: {MASK}', mcp)
        self.assertIn(str(self.fx.root / 'mcp/servers.json'), mcp, 'the installed catalog is the source')

    def test_needs_attention_names_drift_credentials_and_unmanaged_counts(self) -> None:
        block = page_text(section(self.page, 'attention'))
        self.assertRegex(block, r'claude: \d+ drift')
        self.assertIn('Missing Keychain item example/wrapped / me', block)
        self.assertIn('security add-generic-password -s ro-mysql -a reader@db-tunnel-orders-stg -w', block)
        self.assertIn('Unmanaged: 1 in marketplace plugins', block)
        self.assertNotIn('render --host claude', block, 'render refuses files agent-setup wrote')
        self.assertIn(f'--root-dir {self.fx.root} --apply --collision backup', block)

    def test_unmanaged(self) -> None:
        unmanaged = page_text(section(self.page, 'unmanaged'))
        for name in ('.claude/settings.local.json', 'handmade-skill', 'lint-helper@example-market', '1.2.3',
                     'scratch-server', 'claude.ai Example Docs', 'sample-repo', 'AGENTS.md', 'agent-task'):
            self.assertIn(name, unmanaged)
        self.assertNotIn('code-search', unmanaged, 'a kit skill counted as unmanaged')

    def test_overlay_reads_kit_envs_layers_and_names_the_users_layer(self) -> None:
        overlay = page_text(section(self.page, 'overlay'))
        self.assertRegex(overlay, r'REVIEW_BASE\s+main\s+kit\.env\s+preset\.env < kit\.env')
        set_key = [c for c in commands(self.page) if 'KEY=value' in c]
        self.assertEqual(set_key, [append_line(self.fx.root / 'local/kit.env')])

    def test_the_set_key_command_starts_a_new_line(self) -> None:
        target = self.fx.home / 'no newline.env'
        shown = next(c for c in commands(self.page) if 'KEY=value' in c)
        command = shown.replace(shlex.quote(str(self.fx.root / 'local/kit.env')), shlex.quote(str(target)))
        for before, after in (('A=1', 'A=1\nKEY=value\n'), ('A=1\n', 'A=1\nKEY=value\n'), ('', 'KEY=value\n')):
            target.write_text(before)
            subprocess.run(['/bin/sh', '-c', command], check=True)
            self.assertEqual(target.read_text(), after, repr(before))

    def test_data(self) -> None:
        data = page_text(section(self.page, 'data'))
        self.assertRegex(data, r'orders-stg\s+15307\s+STAGING')
        self.assertRegex(data, r'orders\s+15306\s+PROD\s+reader')
        self.assertIn('orders-stg-ro', data, 'the login-path name from the tunnel cache')
        self.assertIn('example-project', data)

    def test_skills_list_the_source_and_label_a_host_its_hosts_line_leaves_out(self) -> None:
        skills = page_text(section(self.page, 'skills'))
        self.assertIn('code-search', skills)
        self.assertRegex(skills, r'pr-study\s+kit\s+claude\s+codex\s+Change\b.*?codex: left out by its hosts: line')
        self.assertIn('The saved selection is every skill', skills)
        self.assertFalse([c for c in commands(self.page) if '--skills' in c], 'a command pins a skill list')

    def test_work_roles_hooks_pack(self) -> None:
        work = page_text(section(self.page, 'work'))
        self.assertIn('first-item', work)
        self.assertIn('Sessions bound (all time)', work)
        self.assertRegex(work, r'first-item\s+copy\s+open\s+\d{4}-\d\d-\d\d \d\d:\d\d\s+1\s', 'the bound session')
        self.assertIn('vscode://file/', self.page)
        self.assertIn('Engine', page_text(section(self.page, 'pack')))
        self.assertIn('Review rounds', page_text(section(self.page, 'roles')))
        actions = page_text(section(self.page, 'actions'))
        self.assertNotIn(' sync', actions)
        self.assertNotIn(' update', actions)


class DashboardScenarioTests(Fixture):
    def test_doctor_and_the_dashboard_report_the_same_claude_drift(self) -> None:
        self.install()
        mcp = self.home / '.claude/mcp.json'
        servers = json.loads(mcp.read_text())
        servers['mcpServers']['handmade'] = {'command': 'x'}
        mcp.write_text(json.dumps(servers))
        doctor = subprocess.run([sys.executable, str(self.root / 'bin/agent-kit'), 'doctor'], capture_output=True,
                                text=True, env=self.env(kit=self.root), timeout=120, stdin=subprocess.DEVNULL)
        page, _ = self.dashboard()
        row = re.search(r'<tr id="host-claude"><td><b>claude</b></td>.*?</tr>', page)
        assert row is not None
        cell = re.search(r'</summary>(.*?)</details>', row.group())
        assert cell is not None
        shown = [html.unescape(line) for line in cell.group(1).split('<br>')]
        found = [line for line in doctor.stdout.splitlines()
                 if re.match(r'(settings drift|mcp|agents drift|agents|roles|rules): ', line)
                 and not line.startswith('mcp: in sync')]
        self.assertTrue(found, doctor.stdout)
        self.assertEqual(shown, found)

    def test_overlay_without_setup_paths_uses_the_one_layer_kit_env_reads(self) -> None:
        self.install()
        local = self.root / 'local'
        (local / 'setup-paths.env').unlink()
        (local / 'kit.env').write_text('REVIEW_BASE=main\n')
        (local / 'preset.env').write_text('REVIEW_BASE=develop\nTEAM_ONLY=x\n')
        page, _ = self.dashboard()
        overlay = page_text(section(page, 'overlay'))
        self.assertRegex(overlay, r'REVIEW_BASE\s+main\s+kit\.env\s+kit\.env\s')
        self.assertNotIn('TEAM_ONLY', overlay, 'a layer kit_env does not read')
        self.assertIn(append_line(local / 'kit.env'), commands(page))

    def test_a_failing_collector_is_one_alert_and_the_rest_renders(self) -> None:
        self.install()
        (self.root / 'hooks/registry.json').write_text('["not an entry"]\n')
        installed = self.root / 'mcp/servers.json'
        installed.write_text(json.dumps({'mcpServers': {'odd': {'command': '{{NO_SUCH_PLACEHOLDER}}/x'}}}))
        page, _ = self.dashboard()
        self.assertIn('MCP catalog placeholders not filled', page_text(section(page, 'mcp')))
        self.assertIn('not read: AttributeError', page_text(section(page, 'hooks')))
        self.assertIn('Hooks: not read: AttributeError', page_text(section(page, 'attention')))
        for key in SECTIONS:
            if key != 'hooks':
                self.assertNotIn('not read:', section(page, key))

    def test_no_keychain_tool_is_not_checked_not_missing(self) -> None:
        self.install()
        installed = self.root / 'mcp/servers.json'
        servers = json.loads(installed.read_text())
        servers['mcpServers']['wrapped'] = {'command': 'example-mcp',
                                            'credentials': [{'service': 'example/wrapped', 'account': 'me'}]}
        installed.write_text(json.dumps(servers))
        page, _ = self.dashboard(security=self.home / 'absent')
        self.assertRegex(page_text(section(page, 'mcp')), r'Keychain example/wrapped / me\s+not checked')
        self.assertNotIn('add-generic-password', page)
        self.assertNotIn('Missing Keychain item', page)

    def test_a_fresh_install_needs_no_attention(self) -> None:
        self.install()
        page, _ = self.dashboard()
        self.assertIn('Nothing needs attention.', section(page, 'attention'), page_text(section(page, 'attention')))
        self.assertRegex(page_text(section(page, 'hosts')), r'claude\s+configured.*?clean')
        doctor = subprocess.run([sys.executable, str(self.root / 'bin/agent-kit'), 'doctor'], capture_output=True,
                                text=True, env=self.env(kit=self.root), timeout=120, stdin=subprocess.DEVNULL)
        self.assertIn('settings: agent-setup writes them', doctor.stdout)
        self.assertIn('mcp: in sync with the catalog', doctor.stdout)
        every = [c for c in commands(page) if c.endswith('doctor --host all')]
        self.assertTrue(every, commands(page))
        result = subprocess.run([sys.executable, *shlex.split(every[0])], capture_output=True, text=True,
                                env=self.env(kit=self.root), timeout=120, stdin=subprocess.DEVNULL)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual({'claude', 'codex', 'cursor'}, {r['host'] for r in json.loads(result.stdout)}, result.stdout)

    def test_doctor_reports_a_hook_removed_since_setup(self) -> None:
        self.install()
        settings = self.home / '.claude/settings.json'
        data = json.loads(settings.read_text())
        event = next(iter(data['hooks']))
        data['hooks'][event] = data['hooks'][event][1:]
        settings.write_text(json.dumps(data))
        doctor = self.doctor(kit=self.root)
        self.assertIn(f'changed since setup: {settings}', doctor.stdout)
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)

    def test_an_install_without_the_mcp_component_is_clean(self) -> None:
        self.install(components=('rules', 'skills'))
        self.assertFalse((self.root / 'mcp/servers.json').exists())
        doctor = self.doctor()
        self.assertNotIn('Traceback', doctor.stderr)
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        page, _ = self.dashboard()
        self.assertIn('Nothing needs attention.', section(page, 'attention'), page_text(section(page, 'attention')))

    def test_a_host_not_checked_offers_doctor_not_a_fix(self) -> None:
        self.install()
        (self.root / 'mcp/servers.json').write_text('{not json')
        page, _ = self.dashboard()
        items = [i for i in section(page, 'attention').split('<li>') if 'claude: not checked' in page_text(i)]
        self.assertEqual(len(items), 1, page_text(section(page, 'attention')))
        self.assertIn('JSONDecodeError', page_text(items[0]))
        self.assertEqual([c.split(' ', 1)[1] for c in commands(items[0])], ['doctor'])

    def test_a_named_skill_selection_offers_select_and_drop_commands(self) -> None:
        self.install('--skills', 'code-search', 'review-rubric')
        page, _ = self.dashboard()
        setup = [str(SOURCE / 'bin/agent-setup'), '--source', str(SOURCE), '--root-dir', str(self.root), '--skills']
        found = commands(page)
        self.assertIn(shlex.join([*setup, 'review-rubric', '--apply']), found, 'drop code-search')
        self.assertIn(shlex.join([*setup, 'code-search', 'review-rubric', 'debug', '--apply']), found, 'select debug')

    def test_every_setup_command_targets_a_kit_outside_the_default_root(self) -> None:
        kit = self.home / 'kits/alt'
        self.install('--root-dir', str(kit), '--skills', 'code-search', 'review-rubric')
        page, _ = self.dashboard(kit=kit)
        found = [c for c in commands(page) if 'agent-setup' in c]
        self.assertTrue(found)
        for command in found:
            tokens = shlex.split(command)
            self.assertIn('--root-dir', tokens, command)
            self.assertEqual(tokens[tokens.index('--root-dir') + 1], str(kit), command)
        select = next(c for c in found if ' debug ' in c)
        preview = [a for a in shlex.split(select) if a != '--apply']
        env = {k: v for k, v in self.env().items() if k != 'AGENT_KIT_DIR'}
        result = subprocess.run([sys.executable, *preview], capture_output=True, text=True, env=env, timeout=120,
                                stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(kit), result.stdout)
        self.assertNotIn('.local/share/agent-kit', result.stdout + result.stderr, 'the default root')

    def test_every_command_quotes_a_path_with_a_space(self) -> None:
        kit = self.home / 'my kit'
        self.install('--root-dir', str(kit))
        page, _ = self.dashboard(kit=kit)
        quoted = [c for c in commands(page) if 'my kit' in c]
        self.assertTrue([c for c in quoted if c.endswith('doctor --host all')], quoted)
        self.assertTrue([c for c in quoted if 'agent-setup' in c], quoted)
        for command in quoted:
            self.assertTrue(any('my kit' in token for token in shlex.split(command)), command)

    def test_the_page_loads_nothing_and_runs_on_a_kit_without_an_install(self) -> None:
        page, result = self.dashboard(kit=SOURCE)
        self.assertIn('dashboard: wrote', result.stdout)
        for key in SECTIONS:
            self.assertIn(f'<section id="{key}" class="card">', page)
        self.assertNotIn('not read:', page)
        self.assertNotRegex(page, r'<(?:link|img|iframe)\b|<script\s[^>]*src=|url\(', 'an external load')
        self.assertNotRegex(page, r'(?:src|href)="https?:', 'an external load')
        self.assertIn('color-scheme:light', page)
        hooks = section(page, 'hooks')
        for row, kind in (('hooks-PreToolUse-bash-guards', 'blocking'), ('hooks-Stop-stop-chime', 'advisory')):
            found = re.search(rf'<tr id="{row}">.*?</tr>', hooks)
            assert found is not None, row
            self.assertIn(f'>{kind}</span>', found.group(), row)
        self.assertEqual(self.status(), self.source_status, 'changed the source checkout')

    def test_the_default_page_goes_under_the_work_root(self) -> None:
        env = {**self.env(kit=SOURCE, security=self.home / 'absent'), 'CLAUDE_OUT_ROOT': str(self.home / 'work')}
        result = subprocess.run([sys.executable, str(SOURCE / 'bin/agent-kit'), 'dashboard', '--no-open'],
                                capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / 'work/dashboard/index.html').is_file())


class CredentialClassifierTests(unittest.TestCase):
    """credentials.py: what validate_catalog refuses is what the page masks."""

    CASES = {
        'header after --header': {'command': 'npx', 'args': ['--header', 'X-Api-Key: ' + PLANTED['header value']]},
        'header after -H': {'command': 'npx', 'args': ['-H', 'Authorization: Basic ' + PLANTED['bearer value']]},
        'credential flag with =': {'command': 'npx', 'args': ['--client-secret=' + PLANTED['flag value']]},
        'value after a credential flag': {'command': 'npx', 'args': ['--api-key', PLANTED['after-flag value']]},
        'credential assignment': {'command': 'npx', 'args': ['API_KEY=' + PLANTED['assigned value']]},
        'header string': {'command': 'npx', 'args': ['Authorization: ' + PLANTED['bearer value']]},
        'passphrase flag': {'command': 'npx', 'args': ['--passphrase', PLANTED['passphrase']]},
        'URL userinfo': {'url': 'https://reader:' + PLANTED['userinfo password'] + '@mcp.example.com/'},
        'URL query key': {'url': 'https://mcp.example.com/mcp?api_key=' + PLANTED['url token']},
        'jwt': {'command': 'npx', 'args': [PLANTED['jwt']]},
        'sentry token': {'command': 'npx', 'args': [PLANTED['sentry token']]},
        'atlassian token': {'command': 'npx', 'args': [PLANTED['atlassian token']]},
        'google token': {'command': 'npx', 'args': [PLANTED['google token']]},
        'number after --password': {'command': 'npx', 'args': ['--password', PLANTED['numeric password']]},
        'number in --password=': {'command': 'npx', 'args': ['--password=' + PLANTED['numeric password']]},
        'number in a password assignment': {'command': 'npx', 'args': ['DB_PASSWORD=' + PLANTED['numeric password']]},
        'path-shaped after --token': {'command': 'npx', 'args': ['--token', PLANTED['path-shaped secret']]},
        'dot-shaped after --secret': {'command': 'npx', 'args': ['--secret', PLANTED['dot-shaped secret']]},
        'negative number in --api-key=': {'command': 'npx', 'args': ['--api-key=-' + PLANTED['numeric password']]},
    }
    # A credential-ish name that does not hold the secret itself: the install takes the value (as
    # before the classifier split), and the page shows it or masks it, never refuses.
    INSTALLS = {
        'oauth=true': ['--oauth=true'], 'auth=none': ['--auth=none'], 'session=default': ['--session=default'],
        'sort-key=name': ['--sort-key=name'], 'token-limit=4k': ['--token-limit=4k'], 'USE_OAUTH': ['USE_OAUTH=true'],
        'auth-provider=github': ['--auth-provider=github'], 'cookie-domain': ['--cookie-domain=example.com'],
        'header Accept': ['--header', 'Accept: application/json'], 'header= Accept': ['--header=Accept: text/plain'],
        'key file': ['--key', '/etc/x/key.pem'], 'api-key env': ['--api-key', '${API_KEY}'],
        'password env': ['--password=${DB_PASSWORD}'], 'password assignment env': ['DB_PASSWORD=$DB_PASSWORD'],
    }
    # Not certain enough to refuse an install, so masked on the page only.
    MASKED = {
        'key-shaped URL path': {'url': 'https://mcp.example.com/v1/' + PLANTED['url path key'] + '/sse'},
        'long URL path key': {'url': 'https://mcp.example.com/' + PLANTED['long path key']},
        'short flag value': {'command': 'npx', 'args': ['-p', PLANTED['short flag value']]},
        'license key': {'command': 'npx', 'args': ['--license-key', PLANTED['license key']]},
        'json blob': {'command': 'npx', 'args': ['{"apiKey":"' + PLANTED['json value'] + '"}']},
        'mixed-case word': {'command': 'npx', 'args': ['x', PLANTED['flag value']]},
    }
    # Each passed the install before the classifier split, and shows as written.
    PLAIN = {
        'max-tokens': ['npx', '-y', 'some-llm-mcp', '--max-tokens', '4096'],
        'credentials path': ['npx', '-y', 'gdrive-mcp', '--credentials', '/Users/me/.config/gdrive/oauth.json'],
        'no-auth URL': ['npx', '-y', 'mcp-remote', '--allow-http', '--no-auth', 'http://localhost:3000/mcp'],
        'oauth URL': ['npx', '-y', 'some-mcp', '--oauth', 'https://mcp.example.com/mcp'],
        'authority URL': ['npx', '--authority', 'https://login.example.com/common'],
        'session-timeout': ['uvx', 'some-mcp', '--session-timeout', '600'],
        'host:port': ['npx', '-y', 'x', 'localhost:8080'],
        'scoped package': ['npx', '-y', '@example/docs-server@1.2.0', '--port=3000', '--token-file', '/etc/x/token'],
        'placeholder path': ['{{KIT_DIR}}/bin/example-mcp', '~/x', './y'],
    }

    def shown(self, spec: dict) -> str:
        return show_url(spec['url']).text if 'url' in spec else show_args([spec['command'], *spec['args']]).text

    def test_validate_catalog_refuses_every_case_and_the_page_shows_none(self) -> None:
        for name, spec in self.CASES.items():
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, 'inline credential refused'):
                    validate_catalog({'mcpServers': {'x': spec}})
                self.assertFalse([v for v in PLANTED.values() if v in self.shown(spec)], self.shown(spec))

    def test_the_page_masks_what_it_cannot_vouch_for_and_the_install_takes_it(self) -> None:
        for name, spec in self.MASKED.items():
            with self.subTest(name):
                validate_catalog({'mcpServers': {'x': spec}})
                self.assertIn(MASK, self.shown(spec))
                self.assertFalse([v for v in PLANTED.values() if v in self.shown(spec)], self.shown(spec))

    def test_a_credential_ish_name_without_a_secret_installs(self) -> None:
        for name, args in self.INSTALLS.items():
            with self.subTest(name):
                validate_catalog({'mcpServers': {'x': {'command': 'npx', 'args': ['-y', 'pkg', *args]}}})
        self.assertEqual(show_args(['--max-tokens', '4096', '--token-limit', '10']).text, '--max-tokens 4096 --token-limit 10')
        refs = ['--key', '/etc/x/key.pem', 'DB_PASSWORD=$DB_PASSWORD', '--token=${GITHUB_TOKEN}']
        self.assertEqual(show_args(refs).text, shlex.join(refs))

    def test_plain_arguments_pass_and_show_as_written(self) -> None:
        for name, argv in self.PLAIN.items():
            with self.subTest(name):
                validate_catalog({'mcpServers': {'x': {'command': argv[0], 'args': argv[1:]}}})
                self.assertEqual(show_args(argv).text, shlex.join(argv))
        uuid = '-'.join(('550e8400', 'e29b', '41d4', 'a716', '446655440000'))
        for url in (f'https://mcp.example.com/workspaces/{uuid}/mcp',
                    'https://mcp.example.com/servers/github-mcp-server-v2/mcp'):
            validate_catalog({'mcpServers': {'x': {'url': url}}})
            self.assertEqual(show_url(url).text, url)
        header = ['npx', '-y', 'mcp-remote', 'https://mcp.example.com/sse', '--header', 'Authorization:${AUTH_HEADER}']
        validate_catalog({'mcpServers': {'x': {'command': header[0], 'args': header[1:]}}})
        self.assertIn('Authorization: ${AUTH_HEADER}', show_args(header).text)

    def test_declared_credentials_pass_in_one_spelling(self) -> None:
        catalog = {'mcpServers': {
            'remote': {'url': 'https://mcp.example.com/v1/sse?transport=sse'},
            'wrapped': {'command': '{{KIT_DIR}}/bin/example-mcp',
                        'credentials': [{'service': 'example/wrapped', 'account': 'me'}]}}}
        self.assertEqual(set(validate_catalog(catalog)['mcpServers']), {'remote', 'wrapped'})
        self.assertEqual(show_url('https://mcp.example.com/v1/sse?transport=sse').text,
                         f'https://mcp.example.com/v1/sse?transport={MASK}')
        for wrong in ([{'service': 'a'}], [{'keychain': 'a', 'account': 'b'}]):
            with self.assertRaisesRegex(ValueError, 'credentials must list'):
                validate_catalog({'mcpServers': {'w': {'command': 'x', 'credentials': wrong}}})

    def test_a_row_description_shows_under_its_name(self) -> None:
        from dashboard_html import Row, Section, row, section as render_section

        self.assertEqual(row(Row(('a', 'b'), 'mcp-a', 'what it does')),
                         '<tr id="mcp-a"><td>a<div class="desc">what it does</div></td><td>b</td></tr>')
        self.assertIn('<div class="desc">one line</div>', render_section(Section('k', 'K', 'one line')))

    def test_mask_tokens_covers_every_shape(self) -> None:
        text = ' '.join([PLANTED['jwt'], PLANTED['sentry token'], PLANTED['atlassian token'], PLANTED['google token'],
                         'Bearer ' + PLANTED['bearer value'], 'https://u:' + PLANTED['userinfo password'] + '@h/', TOKEN])
        masked, count = mask_tokens(text)
        self.assertEqual(count, 7, masked)
        for value in (*PLANTED.values(), TOKEN):
            self.assertNotIn(value, masked)

    def test_frontmatter_joins_block_scalars(self) -> None:
        from hosts import frontmatter

        text = '---\nname: x\ndescription: >\n  folded over\n  two lines\nnotes: |\n  kept\n  apart\nhosts: [claude]\n---\nbody\n'
        self.assertEqual(frontmatter(text), {'name': 'x', 'description': 'folded over two lines',
                                             'notes': 'kept\napart', 'hosts': '[claude]'})

    def test_render_drops_the_credentials_declaration(self) -> None:
        from hosts import fill_servers

        filled = fill_servers({'w': {'command': 'x', 'credentials': [{'service': 'a', 'account': 'b'}]}}, '/kit')
        self.assertEqual(filled, {'w': {'command': 'x'}})


if __name__ == '__main__':
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite([loader.loadTestsFromTestCase(case) for case in
                                (CredentialClassifierTests, DashboardPageTests, DashboardScenarioTests)])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': result.testsRun, 'successful': result.wasSuccessful(),
                                'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
