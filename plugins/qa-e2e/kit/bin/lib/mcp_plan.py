"""The MCP plan an install writes (README "MCP servers"): which catalog servers it can start, which it
leaves out and why, and the uv sync of the team pack's mcp/ project those servers need."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, NamedTuple

from kit_text import PACK_DIR, fill_servers
from pack import PACK_MCP, Pack
from preflight import install_hint, server_runtime

# In the kept pack's mcp/.venv: the sha256 of the uv.lock its last successful sync installed.
SYNC_STAMP = '.agent-kit-synced'
CODE_DIR_PATH = re.compile(r'\{\{CODE_DIR\}\}(?:/[A-Za-z0-9_.-]+)+')


class McpPlan(NamedTuple):
    """The catalog servers an install can start, a line for each one it leaves out and why, and the uv
    sync of the kept pack's mcp/ project when a runnable server needs it and its environment is absent
    or older than the pack's uv.lock (None otherwise)."""
    runnable: dict[str, Any]
    skipped: tuple[str, ...]
    sync: tuple[str, ...] | None


def mcp_synced(project: Path, lock: bytes) -> bool:
    """Whether project/.venv holds the stamp of a successful sync of lock (sync_pack_mcp writes it)."""
    try:
        return (project / '.venv' / SYNC_STAMP).read_text().strip() == hashlib.sha256(lock).hexdigest()
    except OSError:
        return False


def _clone_missing(spec: dict[str, Any], local: str) -> str | None:
    """The first {{CODE_DIR}} path of spec that the clone does not hold yet: a command that is not an
    executable, or an argument path that does not exist."""
    command = str(spec.get('command', ''))
    if '{{CODE_DIR}}' in command and not os.access(command.replace('{{CODE_DIR}}', local), os.X_OK):
        return f'{command.replace("{{CODE_DIR}}", local)} is not an executable in your clone yet'
    args = spec.get('args')
    if not isinstance(args, list):
        args = []
    for argument in args:
        for match in CODE_DIR_PATH.finditer(str(argument)):
            path = match.group().replace('{{CODE_DIR}}', local)
            if not os.path.exists(path):
                return f'{path} is not in your clone yet'
    return None


def mcp_plan(servers: dict[str, Any], *, view: Path, root: Path, pack: Pack | None, local: str) -> McpPlan:
    """Which catalog servers this install keeps (README "MCP servers"). A server whose runtime or
    command is missing is left out, so no host starts a command that cannot run; its descriptor stays
    in the catalog or preset, and a rerun once it is there adds it. The catalog keeps its kit
    placeholders (a later render fills them from the installed kit); filled with view here for the
    checks only. A server run from a local clone ({{CODE_DIR}}) waits for that clone to hold its
    command and argument paths; one run from the pack's mcp/ project ({{PACK_DIR}}) needs uv, which
    syncs it. A pack server's bare command is written as its absolute path: a host started from the
    Dock lacks ~/.local/bin on PATH."""
    uv = shutil.which('uv')
    runnable: dict[str, Any] = {}
    skipped: list[str] = []
    for name, spec in servers.items():
        text = json.dumps(spec)
        clone, packed = '{{CODE_DIR}}' in text, '{{PACK_DIR}}' in text
        why = None
        if clone and not local:
            why = '{{CODE_DIR}} needs a repository root (CODE_DIRS_JSON)'
        elif packed and (pack is None or not pack.has_mcp):
            why = 'the team pack has no mcp/ project for {{PACK_DIR}}'
        elif packed and not uv:
            why = f"uv is missing, which syncs the team pack's mcp/ project. Fix: {install_hint('uv')}, then rerun agent-setup"
        else:
            command = fill_servers({name: spec}, str(view), local)[name].get('command', '')
            runtime = server_runtime({'command': command})
            if packed and command and '/' not in command:
                if found := shutil.which(command):
                    spec = {**spec, 'command': found}
                else:
                    why = f'{command} is missing. Fix: {install_hint(command)}'
            elif runtime and not shutil.which(command):
                why = f'{runtime} is missing. Fix: {install_hint(runtime)}, then rerun agent-setup'
            elif clone and (missing := _clone_missing(spec, local)):
                why = f'{missing}; rerun agent-setup once it is'
        if why:
            skipped.append(f'MCP server {name}: {why}')
        else:
            runnable[name] = spec
    project = root / PACK_DIR / PACK_MCP
    lock = pack.files.get(f'{PACK_MCP}/uv.lock', (b'', 0))[0] if pack is not None else b''
    needs = uv and any('{{PACK_DIR}}' in json.dumps(spec) for spec in runnable.values())
    sync = (uv, 'sync', '--frozen', '--no-dev', '--project', str(project)) if needs and not mcp_synced(project, lock) else None
    return McpPlan(runnable, tuple(skipped), sync)


def sync_pack_mcp(command: Sequence[str] | None) -> str | None:
    """Run command, the uv sync of the team pack's mcp/ project, and say how it went (None: nothing to
    sync). The environment uv creates there (.venv) belongs to no record: neither drift nor a
    collision. A success stamps it with the uv.lock it synced; a failed sync leaves no stamp, so the
    next setup or sync retries it."""
    if not command:
        return None
    project = Path(command[-1])
    try:
        result = subprocess.run(list(command), capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'team pack mcp/ project not synced ({error}); the next agent-setup or agent-kit sync retries it'
    if result.returncode:
        reason = (result.stderr.strip().splitlines() or ['uv sync failed'])[-1]
        return f'team pack mcp/ project not synced ({reason}); the next agent-setup or agent-kit sync retries it'
    try:
        stamp = hashlib.sha256((project / 'uv.lock').read_bytes()).hexdigest()
        (project / '.venv' / SYNC_STAMP).write_text(stamp + '\n')
    except OSError as error:
        return (f'team pack mcp/ project synced ({project}), but its stamp could not be written ({error}); '
                'the next agent-setup or agent-kit sync runs it again')
    return f'team pack mcp/ project synced ({project})'
