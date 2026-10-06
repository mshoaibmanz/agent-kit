"""Team presets for agent-setup (presets/example.toml documents the format) and the MCP catalog
check they share with setup's own catalog."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from hosts import HOSTS, normalize_transport
from preflight import install_hint

# Preset [kit] keys: overlay keys (hooks/lib/README) a team shares. The first three also answer
# setup's own questions, so its summary and checks see them.
PRESET_ANSWERS = {'CODE_SEARCH_GH_OWNER': 'github_owner', 'CODE_DIRS_JSON': 'repo_roots',
                  'CODE_SEARCH_ZOEKT_URL': 'zoekt_url'}
PRESET_KIT_KEYS = (*PRESET_ANSWERS, 'REVIEW_BASE', 'RELEASE_BRANCH_RE', 'BQRO_PROJECT', 'GIT_AUTHOR',
                   'SUBAGENT_RESUME_MAX')
# Token formats with a fixed prefix, credentials in a URL, and a bearer header. Plain words such as
# "secret" or "token" are not refused: they turn up in paths and names.
INLINE_SECRET = re.compile(
    r'(?<![A-Za-z0-9])(?:gh[opsur]_[A-Za-z0-9]{8,}|github_pat_\w{8,}|sk-(?:ant-)?[\w-]{8,}|xox[abpr]-[\w-]{8,}'
    r'|AKIA[0-9A-Z]{16}|glpat-[\w-]{8,}|AIza[\w-]{20,})'
    r'|://[^/\s@]+@|[?&](?:access[-_]?token|auth[-_]?token|token|api[-_]?key|key|password|secret)='
    r'|\bbearer\s', re.I)
CREDENTIAL_QUERY = {'apikey', 'key', 'accesskey', 'authkey', 'accesstoken', 'authtoken', 'token', 'password',
                    'secret', 'authorization'}
CREDENTIAL_ARGUMENT = re.compile(
    r'^--(?:key|api[-_]?key|token|access[-_]token|auth[-_]token|pat|password|secret)(?:=|$)', re.I)


def validate_catalog(catalog: Any) -> dict[str, Any]:
    """An MCP catalog ({"mcpServers": {name: spec}}) with each spec normalized; refuses inline
    credentials, which belong in a runtime wrapper or native OAuth."""
    if not isinstance(catalog, dict) or not isinstance(catalog.get('mcpServers', {}), dict):
        raise ValueError('MCP catalog must map server names to objects')
    servers = {}
    for name, spec in catalog.get('mcpServers', {}).items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or not isinstance(spec, dict):
            raise ValueError('MCP catalog has an invalid server descriptor')
        if set(spec) - {'command', 'args', 'url', 'type', 'description'}:
            raise ValueError(f'MCP {name}: use native OAuth or a runtime wrapper, not inline secrets')
        normalized = normalize_transport(spec)
        query = {re.sub(r'[-_]', '', key).casefold() for key, _ in parse_qsl(urlsplit(normalized.get('url', '')).query)}
        arguments = normalized.get('args', [])
        credential_argument = isinstance(arguments, list) and any(
            isinstance(argument, str) and CREDENTIAL_ARGUMENT.match(argument) for argument in arguments)
        if query & CREDENTIAL_QUERY or credential_argument or INLINE_SECRET.search(json.dumps(normalized)):
            raise ValueError(f'MCP {name}: common inline credential patterns are refused; use a runtime wrapper')
        if 'args' in normalized and ('command' not in normalized or not isinstance(normalized['args'], list)
                                    or not all(isinstance(value, str) for value in normalized['args'])):
            raise ValueError('MCP args must be strings on a command transport')
        servers[name] = normalized
    return {'mcpServers': servers}


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and '\n' not in item for item in value)


def _kit(table: dict[str, Any]) -> bool:
    return set(table) <= set(PRESET_KIT_KEYS) and all(
        _strings(value) if key == 'CODE_DIRS_JSON' else isinstance(value, str) and '\n' not in value
        for key, value in table.items()) and table.get('SUBAGENT_RESUME_MAX', '1').isdigit()


# Each table: (what it takes, for the refusal; its check).
PRESET_SCHEMA: dict[str, tuple[str, Callable[[dict[str, Any]], bool]]] = {
    'kit': (f'one-line strings for {", ".join(PRESET_KIT_KEYS)} (CODE_DIRS_JSON a list of them, '
            'SUBAGENT_RESUME_MAX digits)', _kit),
    'mcp': ('[mcp.servers.<name>] tables only', lambda table: set(table) <= {'servers'}
            and isinstance(table.get('servers', {}), dict)),
    'hosts': (f'recommended, a list from {", ".join(HOSTS)}', lambda table: set(table) <= {'recommended'}
              and _strings(table.get('recommended', [])) and set(table.get('recommended', [])) <= set(HOSTS)),
    'roles': ('[roles.<name>] tables of model and effort strings', lambda table: all(
        isinstance(role, dict) and set(role) <= {'model', 'effort'} and all(isinstance(v, str) for v in role.values())
        for role in table.values())),
    'skills': ('include and exclude lists only', lambda table: set(table) <= {'include', 'exclude'}
               and all(_strings(value) for value in table.values())),
}


def validate_preset(preset: dict[str, Any], spec: str) -> None:
    """Refuse unknown tables or keys, wrong types, and anything that looks like an inline secret."""
    unknown = set(preset) - set(PRESET_SCHEMA)
    if unknown:
        raise ValueError(f'preset {spec}: unknown table(s) {", ".join(sorted(unknown))}; expected {", ".join(PRESET_SCHEMA)}')
    for table, value in preset.items():
        takes, check = PRESET_SCHEMA[table]
        if not isinstance(value, dict) or not check(value):
            raise ValueError(f'preset {spec}: [{table}] takes {takes}')
    for key, value in preset.get('kit', {}).items():
        if any(INLINE_SECRET.search(item) for item in (value if isinstance(value, list) else [value])):
            raise ValueError(f'preset {spec}: [kit] {key} looks like an inline secret; presets hold non-secret defaults only')
    try:
        validate_catalog({'mcpServers': preset.get('mcp', {}).get('servers', {})})
    except ValueError as error:
        raise ValueError(f'preset {spec}: {error}') from None


class PresetUnavailable(Exception):
    """A gh: preset that could not be fetched: setup continues on its own defaults."""


def load_preset(spec: str) -> tuple[dict[str, Any], str]:
    """(validated preset, its sha256). A local path or gh:owner/repo[/path] (default path
    agent-kit-preset.toml), fetched with the user's own gh login. PresetUnavailable when a gh: fetch
    fails; ValueError when the preset is invalid or holds an inline secret."""
    if spec.startswith('gh:'):
        parts = spec[3:].split('/', 2)
        if len(parts) < 2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in parts[:2]):
            raise ValueError(f'preset {spec}: expected gh:owner/repo[/path]')
        path = parts[2] if len(parts) == 3 else 'agent-kit-preset.toml'
        if not shutil.which('gh'):
            raise PresetUnavailable(f'gh is not installed ({install_hint("gh")})')
        try:
            result = subprocess.run(['gh', 'api', '-H', 'Accept: application/vnd.github.raw',
                                     f'repos/{parts[0]}/{parts[1]}/contents/{path}'], capture_output=True, text=True,
                                    timeout=20, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PresetUnavailable(str(error)) from None
        if result.returncode:
            reason = (result.stderr.strip().splitlines() or ['gh api failed'])[0]
            raise PresetUnavailable(f'{reason} (check `gh auth status` and your access to {parts[0]}/{parts[1]})')
        text = result.stdout
    else:
        text = Path(spec).expanduser().read_text()
    try:
        preset = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f'preset {spec}: not valid TOML: {error}') from None
    validate_preset(preset, spec)
    return preset, hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Preset:
    """A preset as setup uses it. kit: its [kit] values as local/preset.env lines hold them, the
    layer under the user's kit.env. answers: the setup answers it supplies, under the user's own."""
    kit: dict[str, str] = field(default_factory=dict)
    servers: dict[str, Any] = field(default_factory=dict)
    record: dict[str, str] | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    recommended_hosts: list[str] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)

    def saved(self) -> dict[str, Any]:
        """What current.json keeps, so a later run without --preset applies the same values."""
        return {'kit': self.kit, 'servers': self.servers, 'answers': self.answers,
                'recommended_hosts': self.recommended_hosts}

    def env_text(self) -> str:
        return ''.join(f'{key}={value}\n' for key, value in self.kit.items())


def apply_preset(spec: str | None, saved: dict[str, Any], source: Path, interactive: bool) -> Preset:
    """--preset loaded, else the preset an earlier install saved (configuration preset and
    preset_values), else none."""
    kept = saved.get('preset_values', {})
    earlier = Preset(kit=dict(kept.get('kit', {})), servers=dict(kept.get('servers', {})), record=saved.get('preset'),
                     answers=dict(kept.get('answers', {})), recommended_hosts=list(kept.get('recommended_hosts', [])))
    if not spec:
        return earlier
    try:
        preset, sha = load_preset(spec)
    except PresetUnavailable as error:
        print(f'agent-setup: preset {spec} unavailable: {error}; continuing with '
              f'{"interactive " if interactive else ""}defaults', file=sys.stderr)
        return Preset(earlier.kit, earlier.servers, earlier.record, earlier.answers, earlier.recommended_hosts,
                      [('Skipped', f'preset {spec}: {error}')])
    table = preset.get('kit', {})
    kit = {key: json.dumps(value) if isinstance(value, list) else value for key, value in table.items()}
    answers: dict[str, Any] = {answer: table[key] for key, answer in PRESET_ANSWERS.items() if key in table}
    skills = preset.get('skills', {})
    if skills.get('include') or skills.get('exclude'):
        names = skills.get('include') or [path.name for path in sorted((source / 'skills').iterdir()) if path.is_dir()]
        answers['skills'] = [name for name in names if name not in skills.get('exclude', [])]
    for flag in ('model', 'effort'):
        values = [f'{name}={role[flag]}' for name, role in preset.get('roles', {}).items() if flag in role]
        if values:
            answers['role_' + flag] = values
    record = {'source': spec if spec.startswith('gh:') else str(Path(spec).expanduser().absolute()), 'sha256': sha}
    return Preset(kit=kit, servers=preset.get('mcp', {}).get('servers', {}), record=record, answers=answers,
                  recommended_hosts=preset.get('hosts', {}).get('recommended', []),
                  notes=[('Ready', f'preset {record["source"]} (sha256 {sha[:12]})')])
