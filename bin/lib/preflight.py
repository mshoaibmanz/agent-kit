"""agent-setup's detection and its preflight summary: what is installed, signed in and readable,
each finding a row whose fix is this OS's command."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from hosts import HOSTS, default_host_root
import secret_store

KIT = Path(__file__).resolve().parents[2]
REPO_PARENTS = ('Code', 'src', 'dev', 'projects')
# Runtimes an MCP server command may need; a server whose runtime is missing is not registered.
MCP_RUNTIMES = ('npx', 'node', 'uvx', 'uv')


@dataclass(frozen=True)
class Row:
    """One summary line. blocks: apply refuses while it stands."""
    section: str
    line: str
    blocks: bool = False


def install_hint(tool: str) -> str:
    """This OS's install command for tool, from hooks/lib/install-hint (the one source)."""
    try:
        result = subprocess.run(['sh', str(KIT / 'hooks/lib/install-hint'), tool], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return f'install {tool} with your package manager'


def run_text(command: list[str], env: dict[str, str] | None = None) -> tuple[int | None, str]:
    """(exit code, stdout); the code is None when the command cannot run (missing, or no answer in
    10 seconds)."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None, ''
    return result.returncode, result.stdout.strip()


def server_runtime(spec: dict[str, Any]) -> str | None:
    """The runtime an MCP server's command needs (npx, uvx and the like), None for any other."""
    command = str(spec.get('command', ''))
    return Path(command).name if Path(command).name in MCP_RUNTIMES else None


def host_login(host: str, command: str, target: Path) -> tuple[bool | None, str]:
    """(logged in, how it was checked); None when this host's login cannot be checked locally."""
    if host == 'claude':
        if os.environ.get('ANTHROPIC_API_KEY'):
            return True, 'ANTHROPIC_API_KEY is set'
        state = target / '.claude.json' if target != Path.home() / '.claude' else Path.home() / '.claude.json'
        try:
            account = 'oauthAccount' in json.loads(state.read_text())
        except (OSError, ValueError):
            account = False
        return account, ('signed in' if account else f'not signed in: run `{command}` and log in')
    if host == 'codex':
        if not shutil.which(command):
            return None, f'{command} not on PATH'
        code, _ = run_text([command, 'login', 'status'], dict(os.environ, CODEX_HOME=str(target)))
        return (code == 0), ('signed in' if code == 0 else f'not signed in: run `{command} login`')
    if target.is_dir():
        return True, f'configured ({target} exists)'
    return None, 'open Cursor once and sign in, then rerun'


def detect_cli(host: str) -> str:
    """The host's CLI name on PATH, including the install locations each host's installer uses."""
    candidates = {'claude': ['claude', str(Path.home() / '.claude/local/claude')], 'codex': ['codex'],
                  'cursor': ['cursor', 'cursor-agent']}[host]
    return next((name for name in candidates if shutil.which(name)), host)


def detect_hosts() -> list[str]:
    """Hosts with their CLI on PATH or a config dir (CLAUDE_CONFIG_DIR and CODEX_HOME honoured)."""
    return [host for host in HOSTS if shutil.which(detect_cli(host)) or default_host_root(host).is_dir()]


def detect_repo_roots() -> list[str]:
    """~/Code, ~/src, ~/dev and ~/projects when they hold at least one git checkout."""
    roots = []
    for name in REPO_PARENTS:
        parent = Path.home() / name
        try:
            if parent.is_dir() and any((child / '.git').exists() for child in parent.iterdir() if child.is_dir()):
                roots.append(str(parent))
        except OSError:
            continue
    return roots


def detect_owner(roots: list[str]) -> tuple[str, str]:
    """(GitHub owner, where it came from): your only org per `gh api user/orgs`, else the most common
    owner among the origin remotes of the repos found. Empty when neither answers."""
    if shutil.which('gh'):
        code, text = run_text(['gh', 'api', 'user/orgs', '--jq', '.[].login'])
        orgs = text.split() if code == 0 else []
        if len(orgs) == 1:
            return orgs[0], 'gh api user/orgs'
    owners: dict[str, int] = {}
    for root in roots:
        try:
            children = sorted(Path(root).expanduser().iterdir())[:200]
        except OSError:
            continue
        for child in children:
            try:
                text = (child / '.git/config').read_text(errors='replace')
            except OSError:
                continue
            match = re.search(r'url = (?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+)/', text)
            if match:
                owners[match.group(1)] = owners.get(match.group(1), 0) + 1
    if owners:
        return max(owners, key=lambda name: owners[name]), 'origin remotes'
    return '', ''


def org_access(owner: str, zoekt: str) -> Row:
    """Whether your gh login reads owner's private repos, code-search's default scope. An org's public
    metadata answers anyone, so membership is the probe; a user account needs none."""
    if not shutil.which('gh'):
        return Row('Skipped', f'GitHub org check for {owner}: gh is missing. Fix: {install_hint("gh")}')
    code, state = run_text(['gh', 'api', f'user/memberships/orgs/{owner}', '--jq', '.state'])
    if code == 0 and state == 'active':
        return Row('Ready', f'GitHub org {owner}: you are a member, so gh reads its private repos')
    code, kind = run_text(['gh', 'api', f'users/{owner}', '--jq', '.type'])
    if code == 0 and kind == 'User':
        return Row('Ready', f'GitHub owner {owner}: a user account; code-search reads the repos your gh login can')
    if code is None:
        return Row('Skipped', f'GitHub org check for {owner}: gh did not answer in time')
    if zoekt:
        return Row('Skipped', f'GitHub org {owner}: gh cannot read its private repos, so code-search uses its Zoekt '
                   f'backend ({zoekt}) for those')
    return Row('Needs attention', f'GitHub org {owner}: gh cannot read its private repos (not a member, or no read:org '
               'scope); code-search finds only public and local repos. Fix: gh auth refresh -s read:org, or ask an '
               'org admin for access')


def dependencies(args: argparse.Namespace, servers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    needs_hooks = 'hooks' in args.components
    required = {'git': 'shared kit', **({'jq': 'hooks'} if needs_hooks else {})}
    optional = {}
    if 'skills' in args.components:
        names = args.skills if args.skills is not None else [path.name for path in (Path(args.source) / 'skills').iterdir() if path.is_dir()]
        if 'code-search' in names:
            optional.update({'rg': 'local code search', 'gh': 'GitHub code search', 'jq': 'code-search root listing'})
    for host in args.hosts:
        command = getattr(args, host + '_bin')
        (required if needs_hooks else optional)[command] = host
    if 'commands' in args.components:
        optional.update({'gh': 'workflow PR/CI commands; Jira commands also require an authorized connector'})
    if 'data-wrappers' in args.components:
        optional.update({'mysql': 'MySQL read wrapper', 'bq': 'BigQuery read wrapper', 'gcloud': 'BigQuery read wrapper login'})
    if 'mcp' in args.components:
        for name, spec in servers.items():
            runtime = server_runtime(spec)
            if runtime:
                optional.setdefault(runtime, 'MCP server ' + name)
    result = {name: {'available': bool(shutil.which(name)), 'required_for_install': name in required,
                     'used_by': required.get(name, optional.get(name))} for name in required | optional}
    for name, data in result.items():
        if not data['available']:
            data['install'] = install_hint(name)
    return result


def capabilities(args: argparse.Namespace) -> dict[str, str]:
    if 'hooks' not in args.components:
        return {host: 'hooks unselected' for host in args.hosts}
    if args.hook_runtime != 'local':
        raise ValueError('Command hooks require a supported local host runtime; choose rules or skills for cloud runs')
    result = {}
    for host in args.hosts:
        command = getattr(args, host + '_bin')
        if not shutil.which(command):
            result[host] = 'provider CLI missing'
            continue
        if host == 'codex':
            probe = subprocess.run([command, 'features', 'list'], capture_output=True, text=True, timeout=10)
            match = re.search(r'^hooks\s+.*?\s+(true|false)\s*$', probe.stdout, re.M)
            result[host] = 'supported' if probe.returncode == 0 and match and match.group(1) == 'true' else 'hooks present but disabled: explicitly enable the feature in Codex first' if match else 'hooks unsupported or capability unknown'
        elif host == 'cursor':
            result[host] = 'supported' if host in args.confirm_hook_support else 'requires explicit confirmation of native hooks support'
        else:
            result[host] = 'supported'
    return result


def preflight(args: argparse.Namespace, targets: dict[str, Path], deps: dict, support: dict, duplicates: list[str],
              collisions: list[tuple[str, bool]], notes: list[Row], servers: dict[str, Any],
              recommended: list[str]) -> list[Row]:
    """The summary rows. Each one that needs action ends in its fix for this OS; blocks marks the
    ones apply refuses on."""
    rows = [Row('Ready', f'python {sys.version.split()[0]}')]
    if not args.hosts:
        rows.append(Row('Skipped', 'hosts: none detected or selected (no claude, codex or cursor CLI or config dir); '
                        'kit files install now and activate when you install a host'
                        + (f' (the preset recommends {" ".join(recommended)})' if recommended else '') + ': '
                        + '; '.join(f'{host}: {install_hint(host)}' for host in HOSTS)))
    for host in args.hosts:
        command = getattr(args, host + '_bin')
        if not shutil.which(command):
            rows.append(Row('Needs attention', f'{host}: {command} not on PATH; files install and activate once it is. '
                            f'Fix: {install_hint(host)}'))
            continue
        logged, how = host_login(host, command, targets[host])
        rows.append(Row('Ready' if logged else 'Needs attention', f'{host}: {command}, {how}'))
    host_commands = {*args.hosts, *(getattr(args, host + '_bin') for host in args.hosts)}
    for name, data in deps.items():
        if name in host_commands:
            continue
        if data['available']:
            rows.append(Row('Ready', f'{name}: for {data["used_by"]}'))
        elif data['required_for_install']:
            rows.append(Row('Needs attention', f'{name} missing, required for {data["used_by"]} (blocks apply). '
                            f'Fix: {data["install"]}', True))
        else:
            rows.append(Row('Skipped', f'{data["used_by"]} off: {name} missing. Fix: {data["install"]}'))
    if shutil.which('gh') and 'gh' in deps and run_text(['gh', 'auth', 'status'])[0] != 0:
        rows.append(Row('Skipped', 'GitHub search and PR flows off: gh is not logged in. Fix: gh auth login'))
    if 'data-wrappers' in args.components:
        store = secret_store.store()
        rows.append(Row('Ready' if store != 'env' else 'Skipped', {
            'keychain': 'credentials: wrappers read the macOS Keychain, else env vars',
            'secret-tool': 'credentials: no Keychain here; wrappers read secret-tool (libsecret), else env vars',
            'env': 'credentials: no Keychain or secret-tool; wrappers read env vars (RO_MYSQL_PASSWORD_<account '
                   f'encoded>, SENTRY_ACCESS_TOKEN). Fix for a store: {install_hint("secret-tool")}'}[store]))
    if 'mcp' in args.components and not servers:
        rows.append(Row('Skipped', 'mcp: no MCP servers configured (add them in mcp/servers.json or a preset)'))
    for host, state in support.items():
        if state in ('supported', 'hooks unselected'):
            continue
        fix = ('--confirm-hook-support cursor after checking Cursor > Settings > Hooks' if host == 'cursor'
               else install_hint(getattr(args, host + '_bin')) if state == 'provider CLI missing'
               else 'enable hooks in the host, or drop the hooks component')
        rows.append(Row('Needs attention', f'{host} hooks: {state} (blocks apply). Fix: {fix}', True))
    if duplicates:
        rows.append(Row('Needs attention', 'plugins with the same hooks are enabled: ' + ', '.join(duplicates)
                        + ' (blocks apply). Fix: disable them in Claude, or leave hooks unselected', True))
    for path, can_backup in collisions:
        rows.append(Row('Needs attention', f'{path} holds your own content (blocks apply). Fix: '
                        + ('--collision backup or --collision skip' if can_backup else '--collision skip, or move yours'),
                        True))
    rows.extend(notes)
    return rows


def print_summary(rows: list[Row], prepared: list, args: argparse.Namespace, root: Path) -> None:
    out = [f'agent-setup {"apply" if args.apply else "preview"}: hosts {" ".join(args.hosts) or "none"}; '
           f'components {" ".join(args.components)}; kit root {root}']
    for section in ('Ready', 'Needs attention', 'Skipped'):
        lines = [row.line for row in rows if row.section == section]
        if lines:
            out.append(f'{section}:')
            out.extend('  ' + line for line in lines)
    counts: dict[str, int] = {}
    for change, _ in prepared:
        counts[change.kind] = counts.get(change.kind, 0) + 1
    out.append(f'Changes: {len(prepared)}' + (' (' + ', '.join(f'{kind} {n}' for kind, n in sorted(counts.items())) + ')' if counts else ''))
    if args.verbose:
        out.extend(f'  {change.kind:12} {change.target}' for change, _ in prepared)
    elif prepared:
        out.append('  --verbose lists every path; --json prints the full plan')
    sys.stdout.write('\n'.join(out) + '\n')
