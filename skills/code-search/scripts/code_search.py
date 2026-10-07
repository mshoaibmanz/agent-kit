#!/usr/bin/env python3
"""Search code and read files across an org: a Zoekt index behind browser SSO, and GitHub.

Config keys and auth: see ../SKILL.md.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

KEYCHAIN_TIMEOUT_SECONDS = 120
CACHE_SERVICE = 'claude-code-search'
CHROME_DIR = Path.home() / 'Library' / 'Application Support' / 'Google' / 'Chrome'
CHROME_EPOCH_OFFSET = 11644473600  # Chrome stores expiry as microseconds since 1601-01-01
HOST_HASH_SCHEMA = 24  # cookie DB schema >= 24 prefixes the plaintext with sha256(host_key)


class AuthError(Exception):
    pass


def load_config():
    """The CODE_SEARCH_* keys of the kit overlay, read by the kit's one parser (hooks/lib/kit_env.py)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'hooks' / 'lib'))
    os.environ.setdefault('KIT_ENV', str(Path(__file__).resolve().parents[3] / 'local/setup-paths.env'))
    from kit_env import kit_env

    return {k: v for k, v in kit_env().items() if k.startswith('CODE_SEARCH_')}


def _security(*args, timeout=KEYCHAIN_TIMEOUT_SECONDS):
    return subprocess.run(['security', *args], capture_output=True, text=True, timeout=timeout)


def _cached_cookie(host):
    out = _security('find-generic-password', '-s', CACHE_SERVICE, '-a', host, '-w', timeout=10)
    if out.returncode != 0:
        return None
    expires_at, _, value = out.stdout.strip().partition('|')
    if not value or float(expires_at) <= time.time() + 60:
        return None
    return value


def _cache_cookie(host, value, expires_at):
    _security('add-generic-password', '-U', '-s', CACHE_SERVICE, '-a', host, '-w', f'{expires_at}|{value}', timeout=10)


def reset_cache(host):
    _security('delete-generic-password', '-s', CACHE_SERVICE, '-a', host, timeout=10)


def _chrome_key():
    try:
        out = subprocess.run(['security', 'find-generic-password', '-w', '-s', 'Chrome Safe Storage', '-a', 'Chrome'],
                             capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT_SECONDS, check=True)
    except subprocess.CalledProcessError as exc:
        raise AuthError('The macOS keychain prompt for "Chrome Safe Storage" was denied. '
                        'Ask the user to retry and click Allow (not Always Allow).') from exc
    except subprocess.TimeoutExpired as exc:
        raise AuthError(f'Nobody answered the keychain prompt within {KEYCHAIN_TIMEOUT_SECONDS}s. Do not retry '
                        'unattended: tell the user a dialog will appear and to click Allow, then retry once.') from exc
    return hashlib.pbkdf2_hmac('sha1', out.stdout.strip().encode(), b'saltysalt', 1003, dklen=16)


def _decrypt(encrypted, key, host_key, schema):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if encrypted[:3] != b'v10':
        raise AuthError(f'Unsupported Chrome cookie encryption prefix {encrypted[:3]!r}')
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b' ' * 16)).decryptor()
    padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    if schema >= HOST_HASH_SCHEMA:
        if plain[:32] != hashlib.sha256(host_key.encode()).digest():
            raise AuthError(f'Chrome cookie host hash mismatch for {host_key}')
        plain = plain[32:]
    return plain.decode()


def _chrome_cookie(host, name, profile):
    db = CHROME_DIR / profile / 'Cookies'
    if not db.exists():
        raise AuthError(f'Chrome cookie DB not found at {db} (set CODE_SEARCH_CHROME_PROFILE).')
    parts = host.split('.')
    host_keys = [host] + ['.' + '.'.join(parts[i:]) for i in range(len(parts) - 1)]
    with sqlite3.connect(f'file:{quote(str(db))}?immutable=1', uri=True) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        schema = int(row[0]) if row else 0
        rows = conn.execute(
            f"SELECT host_key, encrypted_value, expires_utc FROM cookies WHERE name=? AND host_key IN ({','.join('?' * len(host_keys))})",
            [name, *host_keys]).fetchall()
    if not rows:
        raise AuthError(f'No {name} cookie for {host} in Chrome profile {profile!r}. Ask the user to open https://{host}/ in Chrome and log in.')
    host_key, encrypted, expires_utc = max(rows, key=lambda r: len(r[0]))  # most specific host wins, like the browser
    expires_at = expires_utc / 1_000_000 - CHROME_EPOCH_OFFSET
    if expires_at <= time.time():
        raise AuthError(f'The Chrome session for {host} has expired. Ask the user to open https://{host}/ in Chrome, then retry.')
    return _decrypt(encrypted, _chrome_key(), host_key, schema), expires_at


def session_cookie(cfg, host, *, fresh=False):
    name = cfg.get('CODE_SEARCH_SSO_COOKIE')
    if not name:
        raise AuthError('CODE_SEARCH_SSO_COOKIE is not set in the kit overlay.')
    value = None if fresh else _cached_cookie(host)
    if value is None:
        value, expires_at = _chrome_cookie(host, name, cfg.get('CODE_SEARCH_CHROME_PROFILE') or 'Default')
        _cache_cookie(host, value, expires_at)
    return f'{name}={value}'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def zoekt_post(cfg, path, body):
    base = cfg.get('CODE_SEARCH_ZOEKT_URL')
    if not base:
        raise AuthError('CODE_SEARCH_ZOEKT_URL is not set in the kit overlay.')
    host = urlparse(base).hostname
    opener = urllib.request.build_opener(_NoRedirect)
    for fresh in (False, True):
        req = urllib.request.Request(base.rstrip('/') + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'content-type': 'application/json', 'cookie': session_cookie(cfg, host, fresh=fresh)})
        try:
            with opener.open(req, timeout=60) as resp:
                ctype = resp.headers.get('content-type', '')
                text = resp.read().decode()
            if 'json' in ctype:
                return json.loads(text)
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 401, 403):
                raise
        reset_cache(host)  # rejected or redirected to the login page: the cached cookie is stale
    raise AuthError(f'{base} rejected the Chrome session. Ask the user to open {base} in Chrome and log in, then retry.')


def _b64(s):
    return base64.b64decode(s).decode('utf-8', 'replace') if s else ''


def _repo_short(name):
    return name.removeprefix('github.com/')


def zoekt_search(cfg, query, num, ctx):
    res = zoekt_post(cfg, '/api/search', {'Q': query, 'Opts': {
        'NumContextLines': ctx, 'MaxDocDisplayCount': num, 'MaxMatchDisplayCount': num * 20}})['Result']
    files = res.get('Files') or []
    for f in files:
        print(f"== {_repo_short(f['Repository'])}:{f['FileName']}  @{f.get('Version', '')[:10]}")
        for m in f.get('LineMatches') or []:
            ln = m['LineNumber']
            before = _b64(m.get('Before')).splitlines()
            for i, line in enumerate(before):
                print(f'{ln - len(before) + i}-  {line}')
            print(f'{ln}:  {_b64(m.get("Line")).rstrip(chr(10))}')
            for i, line in enumerate(_b64(m.get('After')).splitlines(), 1):
                print(f'{ln + i}-  {line}')
            if ctx:
                print('--')
    total = res.get('FileCount', len(files))
    more = f' (showing {len(files)}; raise -n or narrow the query)' if total > len(files) else ''
    print(f"[zoekt] {total} files, {res.get('MatchCount', 0)} matches{more}")
    return len(files)


def _zoekt_exact_repo(cfg, repo):
    if '/' not in repo:
        owner = cfg.get('CODE_SEARCH_GH_OWNER')
        repo = f'{owner}/{repo}' if owner else repo
    if not repo.startswith('github.com/'):
        repo = 'github.com/' + repo
    return repo


def zoekt_file(cfg, repo, path):
    q = f'r:^{re.escape(_zoekt_exact_repo(cfg, repo))}$ f:^{re.escape(path)}$'
    files = zoekt_post(cfg, '/api/search', {'Q': q, 'Opts': {'Whole': True, 'MaxDocDisplayCount': 1}})['Result'].get('Files') or []
    if not files:
        raise SystemExit(f'[zoekt] {repo}:{path} is not in the index (check the repo name with `repos`).')
    return _b64(files[0].get('Content')), files[0].get('Version', '')[:10]


def zoekt_repos(cfg, pattern):
    repos = zoekt_post(cfg, '/api/list', {'Q': f'r:{pattern}'})['List'].get('Repos') or []
    for r in repos:
        repo = r['Repository']
        branches = ', '.join(f"{b['Name']}@{b['Version'][:10]}" for b in repo.get('Branches') or [])
        print(f"{_repo_short(repo['Name'])}  {branches}")
    print(f'[zoekt] {len(repos)} repos')


def _install_hint(tool):
    """The kit's per-OS install command for tool (hooks/lib/install-hint), or a generic line."""
    kit = os.environ.get('AGENT_KIT_DIR') or str(Path(__file__).resolve().parents[3])
    try:
        out = subprocess.run(['sh', os.path.join(kit, 'hooks/lib/install-hint'), tool],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or f'install {tool}'
    except (OSError, subprocess.TimeoutExpired):
        return f'install {tool}'


def _gh(*args):
    try:
        out = subprocess.run(['gh', *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit(f'[gh] gh is not installed: GitHub search is off (local and Zoekt still work). '
                         f'Fix: {_install_hint("gh")}, then gh auth login') from None
    if out.returncode != 0:
        raise SystemExit(f'[gh] {" ".join(args[:3])}… failed: {out.stderr.strip()[:500]}')
    return out.stdout


def _gh_repo(cfg, repo):
    repo = repo.removeprefix('github.com/')
    if '/' not in repo:
        owner = cfg.get('CODE_SEARCH_GH_OWNER')
        if not owner:
            raise SystemExit('Give the repo as owner/name, or set CODE_SEARCH_GH_OWNER.')
        repo = f'{owner}/{repo}'
    return repo


def gh_search(cfg, query, repo, lang, file, num):
    args = ['search', 'code', query, '--limit', str(num), '--json', 'repository,path,textMatches']
    if repo:
        args += ['--repo', _gh_repo(cfg, repo)]
    elif cfg.get('CODE_SEARCH_GH_OWNER'):
        args += ['--owner', cfg['CODE_SEARCH_GH_OWNER']]
    else:
        raise SystemExit('Scope GitHub search with --repo owner/name or CODE_SEARCH_GH_OWNER.')
    if lang:
        args += ['--language', lang]
    if file:
        args += ['--filename', file]
    hits = json.loads(_gh(*args) or '[]')
    for h in hits:
        print(f"== {h['repository']['nameWithOwner']}:{h['path']}")
        for tm in h.get('textMatches') or []:
            for line in tm.get('fragment', '').splitlines()[:6]:
                print(f'   {line}')
            print('--')
    print(f'[gh] {len(hits)} files (default branches of repos you can read; no line numbers; not exhaustive)')
    return len(hits)


def gh_file(cfg, repo, path, ref):
    api = f'repos/{_gh_repo(cfg, repo)}/contents/{quote(path)}' + (f'?ref={quote(ref)}' if ref else '')
    return _gh('api', api, '-H', 'Accept: application/vnd.github.raw'), ref or 'default'


def gh_repos(cfg, pattern):
    # `gh search repos` names the field fullName; nameWithOwner is `gh repo view`'s and is refused here.
    args = ['search', 'repos', pattern, '--limit', '50', '--json', 'fullName,pushedAt,isArchived']
    if cfg.get('CODE_SEARCH_GH_OWNER'):
        args += ['--owner', cfg['CODE_SEARCH_GH_OWNER']]
    repos = json.loads(_gh(*args) or '[]')
    for r in repos:
        print(f"{r['fullName']}  pushed {r['pushedAt'][:10]}{'  ARCHIVED' if r['isArchived'] else ''}")
    print(f'[gh] {len(repos)} repos')


def zoekt_query(query, repo, file, lang):
    parts = [query]
    if repo:
        parts.append(f'r:{repo}')
    if file:
        parts.append(f'f:{file}')
    if lang:
        parts.append(f'lang:{lang}')
    return ' '.join(parts)


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('search')
    s.add_argument('query')
    s.add_argument('--backend', choices=['auto', 'zoekt', 'gh', 'both'], default='gh')
    s.add_argument('--repo')
    s.add_argument('--file', help='path filter (zoekt: regex; gh: filename)')
    s.add_argument('--lang')
    s.add_argument('-n', '--num', type=int, default=25, help='max files')
    s.add_argument('-C', '--context', type=int, default=3, help='context lines (zoekt only)')
    f = sub.add_parser('file')
    f.add_argument('repo')
    f.add_argument('path')
    f.add_argument('--lines', help='A:B, 1-based inclusive')
    f.add_argument('--backend', choices=['auto', 'zoekt', 'gh'], default='gh')
    f.add_argument('--ref', help='git ref (gh only; zoekt serves the indexed HEAD)')
    r = sub.add_parser('repos')
    r.add_argument('pattern')
    r.add_argument('--backend', choices=['auto', 'zoekt', 'gh'], default='gh')
    a = sub.add_parser('auth')
    a.add_argument('--reset', action='store_true', help='drop the cached cookie and re-read it from Chrome')
    args = p.parse_args()

    cfg = load_config()
    has_zoekt = bool(cfg.get('CODE_SEARCH_ZOEKT_URL'))
    backend = getattr(args, 'backend', 'auto')
    if backend == 'auto':
        backend = 'gh' if (not has_zoekt or getattr(args, 'ref', None)) else 'zoekt'

    try:
        if args.cmd == 'auth':
            host = urlparse(cfg.get('CODE_SEARCH_ZOEKT_URL', '')).hostname
            if not host:
                raise AuthError('CODE_SEARCH_ZOEKT_URL is not set in the kit overlay.')
            if args.reset:
                reset_cache(host)
            res = zoekt_post(cfg, '/api/list', {'Q': 'r:^$'})
            print(f"[zoekt] authenticated to {host}; {len(res['List'].get('Repos') or [])} repos matched the probe")
        elif args.cmd == 'search':
            if backend in ('zoekt', 'both'):
                zoekt_search(cfg, zoekt_query(args.query, args.repo, args.file, args.lang), args.num, args.context)
            if backend in ('gh', 'both'):
                gh_search(cfg, args.query, args.repo, args.lang, args.file, args.num)
        elif args.cmd == 'file':
            text, version = (gh_file(cfg, args.repo, args.path, args.ref) if backend == 'gh'
                             else zoekt_file(cfg, args.repo, args.path))
            lines = text.splitlines()
            start, end = 1, len(lines)
            if args.lines:
                a_, _, b_ = args.lines.partition(':')
                start, end = int(a_ or 1), int(b_ or len(lines))
            for i in range(start, min(end, len(lines)) + 1):
                print(f'{i:>6}  {lines[i - 1]}')
            print(f'[{backend}] {args.repo}:{args.path} @{version}, {len(lines)} lines')
        elif args.cmd == 'repos':
            (gh_repos if backend == 'gh' else zoekt_repos)(cfg, args.pattern)
    except AuthError as exc:
        print(f'AUTH: {exc}', file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
