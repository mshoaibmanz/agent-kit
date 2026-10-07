"""agent-kit mcp describe against one configurable stdio server and one configurable Streamable HTTP
server, both real processes or sockets on this machine: what it caches (an allowlist, every
resolved credential masked), cleanup of the server's process group, redirects and deadlines."""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard_test import Fixture, load_agent_kit, page_text, section  # noqa: E402
from installer_ux_test import SOURCE  # noqa: E402

sys.path.insert(0, str(SOURCE / 'bin/lib'))
import mcp_describe  # noqa: E402
from mcp_describe import Described  # noqa: E402

# argv: mode [mark]. Modes: echo (default), child (a SIGTERM-ignoring child writes its pid to mark),
# srvreq (a server request before the reply), badresult, echoerror. FAKE_SECRET is echoed into every
# field and as a serverInfo key; FAKE_MCP_LOG records the env and each method.
FAKE_STDIO = '''import json, os, subprocess, sys, time
mode, mark = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else '')
secret, log = os.environ.get('FAKE_SECRET', ''), os.environ.get('FAKE_MCP_LOG')
def note(record):
    if log:
        with open(log, 'a') as f:
            f.write(json.dumps(record) + '\\n')
if mode == 'child':
    code = 'import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(120)'
    subprocess.Popen([sys.executable, '-c', code, mark], stdin=subprocess.DEVNULL)
    while not os.path.exists(mark) or not open(mark).read():
        time.sleep(0.05)
note({'env': secret})
for line in sys.stdin:
    msg = json.loads(line)
    note({'method': msg.get('method')})
    if msg.get('method') == 'initialize':
        info = {'name': 'fake ' + secret, 'version': '1.2', (secret or 'key'): 'echoed', 'title': secret}
        result = {'protocolVersion': '2025-06-18', 'serverInfo': info, 'instructions': 'Use ' + secret}
        if mode == 'badresult':
            result = 'not an object'
        if mode == 'echoerror':
            print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': 1, 'message': 'bad ' + secret}}), flush=True)
            continue
    elif msg.get('method') == 'tools/list':
        if mode == 'srvreq':
            print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'method': 'roots/list'}), flush=True)
        result = {'tools': [{'name': 'search', 'description': 'reads with ' + secret}, {'name': 'read'}]}
    else:
        continue
    print('a log line on stdout')
    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)
'''


class FakeHttp(http.server.BaseHTTPRequestHandler):
    """A Streamable HTTP MCP server configured by its server's `cfg`: it wants X-Team `team`
    (default static-1) and its session id, answers initialize as JSON (serverInfo name `name`, or
    the Authorization token and scheme with `echo`) and tools/list as an event stream kept open with
    heartbeats, the reply first unless `silent`. `redirect` answers every POST with a 307; `drip`
    sends one byte every 0.5 s inside the status line (`headers`) or the stream's first line
    (`body`). Each request's headers go to cfg['seen']."""

    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args) -> None:
        pass

    def send(self, status: int, body: bytes = b'', headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def drip(self) -> None:
        self.close_connection = True
        try:
            for _ in range(40):
                self.wfile.write(b'x')
                self.wfile.flush()
                time.sleep(0.5)
        except OSError:
            pass

    def do_POST(self) -> None:
        cfg = self.server.cfg  # type: ignore[attr-defined]
        cfg.setdefault('seen', []).append(dict(self.headers))
        message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if cfg.get('redirect'):
            return self.send(307, headers={'Location': cfg['redirect']})
        if cfg.get('drip') == 'headers':
            self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
            return self.drip()
        if self.headers.get('X-Team') != cfg.get('team', 'static-1'):
            return self.send(401)
        method = message.get('method')
        if method == 'notifications/initialized':
            return self.send(202)
        if method == 'initialize':
            scheme, _, token = self.headers.get('Authorization', '').partition(' ')
            name = f'{token} via {scheme}' if cfg.get('echo') else cfg.get('name', 'web-docs')
            result = {'protocolVersion': '2025-06-18', 'serverInfo': {'name': name, 'version': '3'}}
            body = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode()
            return self.send(200, body, {'Content-Type': 'application/json', 'Mcp-Session-Id': 's-1'})
        if self.headers.get('Mcp-Session-Id') != 's-1':
            return self.send(400)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        if cfg.get('drip') == 'body':
            return self.drip()
        try:
            if not cfg.get('silent'):
                reply = {'jsonrpc': '2.0', 'id': message['id'], 'result': {'tools': [{'name': 'lookup', 'description': 'd'}]}}
                self.wfile.write(f'event: message\ndata: {json.dumps(reply)}\n\n'.encode())
            for _ in range(600):
                self.wfile.write(b': ping\n\n')
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass


def serve(test: unittest.TestCase, **cfg) -> tuple[str, dict]:
    """(the URL of a FakeHttp server configured by cfg, that cfg, which collects `seen`)."""
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeHttp)
    server.cfg = cfg  # type: ignore[attr-defined]
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f'http://127.0.0.1:{server.server_address[1]}/mcp', cfg


def gone(pid: str) -> bool:
    for _ in range(50):
        if subprocess.run(['/bin/ps', '-p', pid], capture_output=True).returncode:
            return True
        time.sleep(0.1)
    os.kill(int(pid), 9)
    return False


class McpDescribeTests(Fixture):
    def test_describe_caches_each_answer_kills_the_server_and_the_page_shows_it(self) -> None:
        self.install()
        url, _ = serve(self)
        script, log, mark = self.home / 'fake_stdio.py', self.home / 'fake_mcp.log', self.home / 'child.pid'
        script.write_text(FAKE_STDIO)
        installed = self.root / 'mcp/servers.json'
        catalog = json.loads(installed.read_text())
        catalog['mcpServers'].update({
            'fake': {'command': sys.executable, 'args': [str(script), 'child', str(mark)],
                     'env': {'FAKE_SECRET': 'docs', 'FAKE_MCP_LOG': str(log)}},
            'web': {'url': url, 'headers': {'X-Team': 'static-1'}},
            'webauth': {'url': url, 'headers': {'X-Team': 'wrong'}},
            'oauth': {'url': url}})
        installed.write_text(json.dumps(catalog))
        result = subprocess.run([sys.executable, str(self.root / 'bin/agent-kit'), 'mcp', 'describe', '--only', 'fake',
                                 'web', 'webauth', 'oauth', '--timeout', '30'], capture_output=True, text=True,
                                env=self.env(), timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cached = json.loads((self.root / 'state/mcp-describe.json').read_text())['servers']
        self.assertEqual({k: v for k, v in cached['fake'].items() if k != 'checked'},
                         {'status': 'ok', 'name': 'fake docs', 'version': '1.2', 'instructions': 'Use docs',
                          'tools': ['search', 'read']})
        self.assertEqual(cached['web']['tools'], ['lookup'])
        self.assertEqual(cached['webauth']['status'], 'needs sign-in')
        self.assertEqual(cached['oauth']['status'], 'needs sign-in')
        self.assertNotIn('docs', cached, '--only describes only the named servers')
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(records[0], {'env': 'docs'}, 'the server got its catalog env')
        self.assertEqual([r['method'] for r in records[1:]], ['initialize', 'notifications/initialized', 'tools/list'])
        self.assertTrue(gone(mark.read_text()), "the server's child outlived describe: its process group was not killed")
        for text in (result.stdout, result.stderr, json.dumps(cached)):
            self.assertNotIn('static-1', text)
        page, _ = self.dashboard()
        mcp = section(page, 'mcp')
        self.assertRegex(mcp, r'<b>fake</b><div class="desc">Use docs</div>')
        self.assertIn('2 tools', page_text(mcp))
        self.assertIn('needs sign-in', page_text(mcp))
        self.assertIn('mcp describe', page_text(section(page, 'actions')))


class McpDescribeUnitTests(unittest.TestCase):
    """mcp_describe.describe with the real agent-kit API against the fakes."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix='describe-', dir=os.environ.get('TMPDIR')))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.script = self.dir / 'fake_stdio.py'
        self.script.write_text(FAKE_STDIO)
        self.api = load_agent_kit()

    def stdio(self, mode: str, *args: str, env: dict | None = None) -> Described:
        spec = {'command': sys.executable, 'args': [str(self.script), mode, *args], 'env': env or {}}
        return mcp_describe.describe(self.api, spec, 20)

    def http(self, headers: dict, timeout: float = 20, **cfg) -> Described:
        url, _ = serve(self, **cfg)
        return mcp_describe.describe(self.api, {'url': url, 'headers': headers}, timeout)

    def test_a_child_that_ignores_sigterm_is_killed_with_the_group(self) -> None:
        mark = self.dir / 'child.pid'
        self.assertEqual(self.stdio('child', str(mark)).status, 'ok')
        self.assertTrue(gone(mark.read_text()), 'the SIGTERM-ignoring child outlived describe')

    def test_a_server_request_is_not_taken_for_the_reply(self) -> None:
        self.assertEqual(self.stdio('srvreq').tools, ('search', 'read'))

    def test_a_failure_is_cached_as_its_kind_only(self) -> None:
        self.assertEqual(self.stdio('badresult'), Described('protocol error'))
        self.assertEqual(self.stdio('echoerror', env={'FAKE_SECRET': 'pw-' + 'Zq81mmXv'}),
                         Described('error', error='JSON-RPC error'))
        self.assertEqual(self.http({'X-Team': 'wrong'}), Described('needs sign-in'))

    def test_the_cache_holds_an_allowlist_and_no_server_controlled_key(self) -> None:
        planted = 'key-' + 'Pq83nw'
        cached = self.stdio('echo', env={'FAKE_SECRET': planted}).to_json()
        self.assertEqual(set(cached), {'status', 'name', 'version', 'instructions', 'tools'})
        self.assertEqual(cached['tools'], ['search', 'read'], 'tool names only, no descriptions')
        self.assertNotIn('echoed', json.dumps(cached), 'a serverInfo key the server chose is not cached')

    def test_every_resolved_credential_is_masked_whole_at_any_length_and_plain_env_is_not(self) -> None:
        entry = self.http({'X-Team': 'static-1', 'Authorization': 'Bearer abc'}, echo=True)
        self.assertEqual(entry.name, '[redacted] via Bearer', 'the token after the scheme, however short')
        plain = self.stdio('echo', env={'FAKE_SECRET': 'true'})
        self.assertEqual((plain.name, plain.instructions), ('fake true', 'Use true'), 'a plain env value is no credential')

    def test_credentials_are_keychain_values_and_headers_never_split_into_words(self) -> None:
        ref = {'$keychain': {'service': 's', 'account': 'a'}}
        spec = {'env': {'TOKEN': ref, 'MODE': 'true'}, 'args': ['--key', ref],
                'headers': {'Authorization': 'Bearer t0k', 'X-Team': 'my team'}}
        resolved = {'env': {'TOKEN': 'k1', 'MODE': 'true'}, 'args': ['--key', 'k2'],
                    'headers': {'Authorization': 'Bearer t0k', 'X-Team': 'my team'}}
        self.assertEqual(sorted(mcp_describe.credentials_of(spec, resolved)),
                         sorted(['k1', 'k2', 'Bearer t0k', 't0k', 'my team']))

    def test_a_redirect_never_carries_the_headers_elsewhere(self) -> None:
        elsewhere, seen = serve(self)
        entry = self.http({'Authorization': 'Bearer ' + 'tok-' + 'Lr93kd0aQ'}, redirect=elsewhere)
        self.assertEqual(entry, Described('redirected'))
        self.assertEqual(seen.get('seen', []), [], 'the redirect target got a request')

    def test_an_open_event_stream_returns_on_the_reply_and_heartbeats_cannot_outlast_the_deadline(self) -> None:
        started = time.monotonic()
        self.assertEqual(self.http({'X-Team': 'static-1'}).tools, ('lookup',))
        self.assertLess(time.monotonic() - started, 10)
        started = time.monotonic()
        self.assertEqual(self.http({'X-Team': 'static-1'}, 2, silent=True), Described('error', error='timeout'))
        self.assertLess(time.monotonic() - started, 4)

    def test_a_byte_drip_inside_one_line_or_the_headers_stops_at_the_deadline(self) -> None:
        for where in ('body', 'headers'):
            with self.subTest(where):
                started = time.monotonic()
                entry = self.http({'X-Team': 'static-1'}, 2, drip=where)
                self.assertEqual(entry, Described('error', error='timeout'))
                self.assertLess(time.monotonic() - started, 3)

    def test_the_cache_reader_validates_every_entry(self) -> None:
        good = Described('ok', 'today', '', 'n', '2', 'Hi', ('t',))
        (self.dir / 'state').mkdir()
        (self.dir / 'state/mcp-describe.json').write_text(json.dumps({'servers': {
            'good': good.to_json(),
            'odd': {'status': 'ok', 'name': ['x'], 'tools': 'junk', 'instructions': 'y' * 5000},
            'unknown': {'status': 'fine'},
            'junk': 'not an entry'}}))
        _, cache = mcp_describe.read_cache(self.dir)
        self.assertEqual(cache['good'], good)
        self.assertEqual(cache['odd'], Described('ok', instructions='y' * mcp_describe.INSTRUCTIONS_MAX))
        self.assertEqual(set(cache), {'good', 'odd'})


if __name__ == '__main__':
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite([loader.loadTestsFromTestCase(case) for case in (McpDescribeUnitTests, McpDescribeTests)])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.write(json.dumps({'cases': result.testsRun, 'successful': result.wasSuccessful(),
                                'failures': len(result.failures), 'errors': len(result.errors)}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
