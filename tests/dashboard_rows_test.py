"""agent-kit dashboard rows: every row the kit ships says what it is in a `description` no host
receives, and the SQL instances add-connection form's script (run by node) agrees with ro-mysql's own
add checks. Fixtures are dashboard_test's."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard_test import SOURCE, load_agent_kit, validate_catalog  # noqa: E402


class DescriptionTests(unittest.TestCase):
    """Every row the kit ships says what it is, in a `description` the hosts never receive."""

    def test_every_registry_row_and_catalog_entry_has_a_description(self) -> None:
        rows = json.loads((SOURCE / 'hooks/registry.json').read_text())
        missing = [f"{r['event']} {r['command']}" for r in rows if not str(r.get('description', '')).strip()]
        self.assertEqual(missing, [], 'registry rows without a description')
        preset = tomllib.loads((SOURCE / 'presets/example.toml').read_text())
        self.assertTrue(preset.get('description'), 'the example preset has no description')
        catalogs = [json.loads((SOURCE / 'mcp/servers.json').read_text()).get('mcpServers', {}),
                    preset.get('mcp', {}).get('servers', {})]
        missing = [name for servers in catalogs for name, spec in servers.items() if not spec.get('description')]
        self.assertEqual(missing, [], 'MCP catalog entries without a description')

    def test_every_kit_env_key_and_shipped_agent_says_what_it_is(self) -> None:
        lines = (SOURCE / 'kit.env.example').read_text().splitlines()
        bare = [line.split('=', 1)[0] for i, line in enumerate(lines)
                if re.match(r'[A-Z_]+=', line) and not (i and lines[i - 1].startswith('# '))]
        self.assertEqual(bare, [], 'kit.env.example keys without a comment line above them')
        from hosts import frontmatter

        agents = sorted((SOURCE / 'agents').glob('*.md'))
        self.assertTrue(agents)
        missing = [p.name for p in agents if not str(frontmatter(p.read_text()).get('description', '')).strip()]
        self.assertEqual(missing, [], 'shipped agents without a frontmatter description')

    def test_the_hosts_never_get_a_description(self) -> None:
        from kit_text import CATALOG_ONLY, fill_servers

        api = load_agent_kit()
        hooks = api.render_hooks([{'event': 'Stop', 'command': 'x', 'hosts': ['claude'], 'description': 'd'}], 'claude')
        self.assertEqual(hooks, {'Stop': [{'hooks': [{'type': 'command', 'command': 'x'}]}]})
        self.assertEqual(fill_servers({'w': {'command': 'x', 'description': 'd'}}, '/kit'), {'w': {'command': 'x'}})
        self.assertEqual(api.CATALOG_ONLY, CATALOG_ONLY)

    def test_validation_keeps_the_description_for_the_installed_catalog(self) -> None:
        spec = {'command': 'x', 'description': 'Finds pages.', 'type': 'stdio'}
        self.assertEqual(validate_catalog({'mcpServers': {'w': spec}})['mcpServers']['w'],
                         {'command': 'x', 'description': 'Finds pages.'})

    def test_a_bare_agent_kit_renders_a_conventional_catalog(self) -> None:
        bare = Path(tempfile.mkdtemp(prefix='bare-kit-', dir=os.environ.get('TMPDIR')))
        self.addCleanup(shutil.rmtree, bare, True)
        (bare / 'bin').mkdir()
        (bare / 'mcp').mkdir()
        shutil.copy2(SOURCE / 'bin/agent-kit', bare / 'bin/agent-kit')
        # agent-kit finds its kit with hooks/lib (kit_env); bin/lib (hosts.fill_servers) stays out.
        shutil.copytree(SOURCE / 'hooks/lib', bare / 'hooks/lib')
        servers = {'w': {'command': 'x', 'description': 'd', 'credentials': [{'service': 's', 'account': 'a'}]}}
        (bare / 'mcp/servers.json').write_text(json.dumps({'mcpServers': servers}))
        code = ('import importlib.machinery as m, importlib.util as u, json, sys; '
                'l = m.SourceFileLoader("k", sys.argv[1]); mod = u.module_from_spec(u.spec_from_loader("k", l)); '
                'sys.modules["k"] = mod; l.exec_module(mod); print(json.dumps(mod.mcp_catalog()))')
        result = subprocess.run([sys.executable, '-c', code, str(bare / 'bin/agent-kit')], capture_output=True,
                                text=True, timeout=60, env={**os.environ, 'AGENT_KIT_DIR': str(bare)})
        self.assertEqual(json.loads(result.stdout or '{}'), {'mcpServers': {'w': {'command': 'x'}}}, result.stderr)

    def test_a_token_shaped_description_installs_and_each_refusal_names_its_field(self) -> None:
        token = 'gh' + 'p_' + 'A1b2C3d4E5f6G7h8I9j0'
        spec = {'command': 'x', 'description': f'Reads issues with a token such as {token}'}
        self.assertEqual(validate_catalog({'mcpServers': {'w': spec}})['mcpServers']['w'], spec)
        for wrong, field in (({'command': 'x', 'args': ['--token', token]}, 'in args'),
                             ({'command': token}, 'in command'),
                             ({'command': 'x', 'env': {'A': 'b'}}, 'env refused')):
            with self.subTest(field), self.assertRaisesRegex(ValueError, field):
                validate_catalog({'mcpServers': {'w': wrong}})

    def test_a_preset_description_is_one_line_of_text(self) -> None:
        from preset import validate_preset

        validate_preset({'description': 'Team defaults', 'kit': {'REVIEW_BASE': 'main'}}, 'p')
        for wrong in (['a list'], 'two\nlines', 'gh' + 'p_' + 'A1b2C3d4E5f6G7h8I9j0'):
            with self.assertRaisesRegex(ValueError, 'description takes one line'):
                validate_preset({'description': wrong}, 'p')
        with self.assertRaisesRegex(ValueError, 'description takes one line'):
            validate_catalog({'mcpServers': {'x': {'command': 'x', 'description': ['no']}}})


AH_CASES = '''
const R = ahRules(function (k) { return P[k]; });
const known = {ports: ['15306'], aliases: ['db-tunnel-orders']};
const out = {};
const read = ahRead('mysql://reader:hunter2@billing-db:3307/billing?via=jump&local_port=15310&staging=1');
out.read = read;
out.qpass = ahRead('mysql://reader@billing-db/billing?via=jump&password=hunter2&local_port=15311');
out.bad = ahRead('postgres://x@y');
out.fragment = ahRead('mysql://reader:hunter2@billing-db/billing#frag');
out.noScheme = ahRead('reader:hunter2@billing-db');
out.scrubbed = SCRUB.map(function (c) { return ahScrub(c).text; });
out.built = ahBuild(read.fields, known, R);
out.collide = ahBuild({name: 'orders', user: 'reader', via: 'jump', host: 'db', port: '3306', local: '15306'}, known, R);
out.stagingName = ahBuild({name: 'orders-stg', user: 'reader', via: 'jump', host: 'db', port: '3306', local: '15320'}, known, R);
out.missing = ahBuild({name: 'Bad Name', user: '', via: 'db-tunnel-x', host: 'db', port: '3306', local: '80'}, known, R);
out.parity = PARITY.map(function (f) { return ahBuild(f, {ports: [], aliases: []}, R); });
console.log(JSON.stringify(out));
'''
# Userinfo shapes, each with a password that must not survive.
SCRUB = {
    'mysql://u:p@ss@host:3306/db': 'mysql://u@host:3306/db',
    'mysql://u:pa/ss@host/db': 'mysql://u@host/db',
    'mysql://u:pa#ss@host': 'mysql://u@host',
    'mysql://u:pa?ss@host': 'mysql://u@host',
    'mysql://u:pa:ss@host': 'mysql://u@host',
    'mysql://u:plain@host:3306/db?via=jump&local_port=15310': 'mysql://u@host:3306/db?via=jump&local_port=15310',
    'mysql://u@host?pwd=x&via=j': 'mysql://u@host?via=j',
    'mysql://u:p%40ss@host': 'mysql://u@host',
}
GOOD = {'name': 'orders', 'user': 'reader', 'via': 'jump', 'host': 'db.example.test', 'port': '3306', 'local': '15310'}
# One table through ro-mysql's parse_add and the page's ahBuild: the same verdict and alias.
PARITY = [
    GOOD,
    {**GOOD, 'staging': True},
    {**GOOD, 'name': 'orders-stg'},
    {**GOOD, 'name': 'orders-stg', 'staging': True},
    {**GOOD, 'name': 'db-tunnel-orders'},
    {**GOOD, 'name': 'Orders'},
    {**GOOD, 'user': 'bad user'},
    {**GOOD, 'user': 'svc@team'},
    {**GOOD, 'via': 'db-tunnel-jump'},
    {**GOOD, 'via': 'DB-TUNNEL-jump'},
    {**GOOD, 'via': 'Jump.Host_1'},
    {**GOOD, 'host': 'bad_host'},
    {**GOOD, 'port': '0'},
    {**GOOD, 'port': '65535'},
    {**GOOD, 'port': '99999'},
    {**GOOD, 'local': '1023'},
    {**GOOD, 'local': '65535'},
    {**GOOD, 'local': '65536'},
    {**GOOD, 'local': '015310'},
]


def ro_mysql_module():
    from dashboard_rows import load_script

    module = load_script(SOURCE / 'bin/ro-mysql', 'ro_mysql_for_test')
    assert module is not None
    return module


def parse_add_verdict(m, f: dict) -> tuple[bool, str]:
    """(accepted, alias) of ro-mysql's parse_add for one form row."""
    argv = ['--name', f['name'], '--user', f['user'], '--via', f['via'], '--remote', f"{f['host']}:{f['port']}",
            '--local-port', f['local'], *(['--staging'] if f.get('staging') else [])]
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return True, m.parse_add(argv)[0].alias
    except SystemExit:
        return False, ''


@unittest.skipUnless(shutil.which('node'), 'node runs the page script')
class ConnectionHelperScriptTests(unittest.TestCase):
    """The add-connection form's functions, run by node from the template itself with the patterns
    the page gets from ro-mysql."""

    out: dict

    @classmethod
    def setUpClass(cls) -> None:
        from dashboard_sql import form_patterns

        template = (SOURCE / 'bin/lib/dashboard.html').read_text()
        # The page is a string.Template: its $$ is the script's $.
        code = template[template.index('/*ah:begin*/'):template.index('/*ah:end*/')].replace('$$', '$')
        data = (f'var P = {json.dumps(dict(form_patterns(ro_mysql_module())))}, SCRUB = {json.dumps(list(SCRUB))}, '
                f'PARITY = {json.dumps(PARITY)};')
        result = subprocess.run([str(shutil.which('node')), '-e', code + data + AH_CASES], capture_output=True,
                                text=True, timeout=60, check=True)
        cls.out = json.loads(result.stdout)

    def test_a_pasted_uri_fills_the_fields_and_its_password_is_dropped(self) -> None:
        read = self.out['read']
        self.assertEqual(read['fields'], {'user': 'reader', 'host': 'billing-db', 'port': '3307',
                                          'name': 'billing', 'via': 'jump', 'local': '15310', 'staging': True})
        self.assertIn('password in the URI was removed', ' '.join(read['notes']))
        self.assertNotIn('hunter2', json.dumps(self.out))
        self.assertEqual(read['value'], 'mysql://reader@billing-db:3307/billing?via=jump&local_port=15310&staging=1')
        self.assertEqual(self.out['qpass']['value'], 'mysql://reader@billing-db/billing?via=jump&local_port=15311')
        self.assertIn('Not a mysql://', ' '.join(self.out['bad']['notes']))
        self.assertEqual(self.out['bad']['value'], 'postgres://x@y')

    def test_the_password_goes_before_any_validation(self) -> None:
        self.assertEqual(self.out['scrubbed'], list(SCRUB.values()))
        self.assertEqual(self.out['fragment']['value'], 'mysql://reader@billing-db/billing#frag')
        self.assertIn('Not a mysql://', ' '.join(self.out['fragment']['notes']))
        self.assertEqual(self.out['noScheme']['value'], '', 'unreadable text that looks like it has a password is cleared')

    def test_one_add_command_and_the_checked_manual_steps(self) -> None:
        built = self.out['built']
        cmd = 'ro-mysql add --name billing --user reader --via jump --remote billing-db:3307 --local-port 15310 --staging'
        self.assertEqual(built['cmd'], cmd)
        self.assertEqual(built['warnings'], [])
        manual = built['manual']
        self.assertIn(f'\n{cmd} --dry-run\n', manual)
        self.assertNotIn('LocalForward', manual, 'the page writes no ssh block: --dry-run prints the checked one')
        self.assertIn('security add-generic-password -U -s ro-mysql -a reader@db-tunnel-billing-staging -w\n', manual)
        self.assertTrue(manual.endswith('ro-mysql --tunnels --refresh'))

    def test_a_known_port_or_alias_and_a_staging_name_are_flagged(self) -> None:
        warnings = ' '.join(self.out['collide']['warnings'])
        self.assertIn('Local port 15306 is already forwarded', warnings)
        self.assertIn('db-tunnel-orders already exists', warnings)
        self.assertIn('reads as STAGING', ' '.join(self.out['stagingName']['warnings']))
        self.assertEqual(self.out['missing']['cmd'], '')
        self.assertEqual(sorted(self.out['missing']['missing']), ['local', 'name', 'user', 'via'])

    def test_the_form_and_ro_mysql_accept_the_same_rows(self) -> None:
        m = ro_mysql_module()
        for f, js in zip(PARITY, self.out['parity']):
            with self.subTest(f=f):
                self.assertEqual((not js['missing'], js['alias']), parse_add_verdict(m, f))

    def test_a_changed_ro_mysql_pattern_and_its_flags_flow_into_the_page(self) -> None:
        from dashboard_html import AddHelper, add_helper
        from dashboard_rows import load_script
        from dashboard_sql import form_patterns

        text = (SOURCE / 'bin/ro-mysql').read_text()
        changed = text.replace('TUNNEL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")',
                               'TUNNEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")')
        changed = changed.replace('(?:staging|stg)", re.IGNORECASE)', '(?:staging|stg)")')
        self.assertNotEqual(changed.count('re.IGNORECASE'), text.count('re.IGNORECASE'))
        folder = Path(tempfile.mkdtemp(prefix='ro-mysql-', dir=os.environ.get('TMPDIR')))
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / 'ro-mysql').write_text(changed)
        patterns = {'shipped': form_patterns(ro_mysql_module()),
                    'changed': form_patterns(load_script(folder / 'ro-mysql', 'ro_mysql_changed'))}
        self.assertIn('data-p-staging-flags="i"', add_helper(AddHelper((), (), patterns['shipped'])))
        self.assertIn('data-p-staging-flags=""', add_helper(AddHelper((), (), patterns['changed'])))
        template = (SOURCE / 'bin/lib/dashboard.html').read_text()
        code = template[template.index('/*ah:begin*/'):template.index('/*ah:end*/')].replace('$$', '$')
        row = {**GOOD, 'name': 'Orders-STG'}
        js = code + (f'var P = {json.dumps({k: dict(v) for k, v in patterns.items()})}, F = {json.dumps(row)};'
                     'var out = {}; Object.keys(P).forEach(function (k) {'
                     ' out[k] = ahBuild(F, {ports: [], aliases: []}, ahRules(function (n) { return P[k][n]; })); });'
                     'console.log(JSON.stringify(out));')
        out = json.loads(subprocess.run([str(shutil.which('node')), '-e', js], capture_output=True, text=True,
                                        timeout=60, check=True).stdout)
        self.assertEqual(out['shipped']['missing'], ['name'], 'uppercase is refused, and the name reads as STAGING')
        self.assertEqual((out['changed']['missing'], out['changed']['alias']), ([], 'db-tunnel-Orders-STG'),
                         'the changed pattern takes uppercase, and without its i flag STG is not staging')


if __name__ == '__main__':
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite([loader.loadTestsFromTestCase(case) for case in
                                (DescriptionTests, ConnectionHelperScriptTests)])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': result.testsRun, 'successful': result.wasSuccessful(),
                                'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
