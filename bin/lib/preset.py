"""Team presets for agent-setup (presets/example.toml documents the format), the team pack a preset
repository may carry (pack.py), and the MCP catalog check they share with setup's own catalog."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from hosts import HOSTS, normalize_transport
from pack import (PACK_SKILLS, PACK_TOML, Pack, PackFiles, PresetUnavailable, StalePack, build_pack, folder_entries,
                  gh_entries, gh_head, git_head, holds_token, pack_files)

# Preset [kit] keys: overlay keys (hooks/lib/README) a team shares. The first three also answer
# setup's own questions, so its summary and checks see them.
PRESET_ANSWERS = {'CODE_SEARCH_GH_OWNER': 'github_owner', 'CODE_DIRS_JSON': 'repo_roots',
                  'CODE_SEARCH_ZOEKT_URL': 'zoekt_url'}
PRESET_KIT_KEYS = (*PRESET_ANSWERS, 'REVIEW_BASE', 'RELEASE_BRANCH_RE', 'BQRO_PROJECT', 'GIT_AUTHOR',
                   'SUBAGENT_RESUME_MAX')
# Credentials in a URL and a bearer header, beside the token formats (pack.holds_token). Plain words
# such as "secret" or "token" are not refused: they turn up in paths and names.
INLINE_CREDENTIAL = re.compile(
    r'://[^/\s@]+@|[?&](?:access[-_]?token|auth[-_]?token|token|api[-_]?key|key|password|secret)='
    r'|\bbearer\s', re.I)
CREDENTIAL_QUERY = {'apikey', 'key', 'accesskey', 'authkey', 'accesstoken', 'authtoken', 'token', 'password',
                    'secret', 'authorization'}
CREDENTIAL_ARGUMENT = re.compile(
    r'^--(?:key|api[-_]?key|token|access[-_]token|auth[-_]token|pat|password|secret)(?:=|$)', re.I)


def inline_secret(text: str) -> bool:
    return holds_token(text) or bool(INLINE_CREDENTIAL.search(text))


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
        if query & CREDENTIAL_QUERY or credential_argument or inline_secret(json.dumps(normalized)):
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
        if any(inline_secret(item) for item in (value if isinstance(value, list) else [value])):
            raise ValueError(f'preset {spec}: [kit] {key} looks like an inline secret; presets hold non-secret defaults only')
    try:
        validate_catalog({'mcpServers': preset.get('mcp', {}).get('servers', {})})
    except ValueError as error:
        raise ValueError(f'preset {spec}: {error}') from None


def _gh_repo(spec: str) -> tuple[str, str | None]:
    """(owner/repo, the preset's path in it when the spec names one) of gh:owner/repo[/path]."""
    parts = spec[3:].split('/', 2)
    if len(parts) < 2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in parts[:2]):
        raise ValueError(f'preset {spec}: expected gh:owner/repo[/path]')
    return f'{parts[0]}/{parts[1]}', parts[2].strip('/') if len(parts) == 3 else None


def _pack_preset(files: PackFiles, toml: str, spec: str, commit: str | None) -> tuple[str, Pack | None]:
    """(the preset text, the pack) of a pack's files."""
    text = files.pop(toml, (b'', 0))[0].decode()
    pack = build_pack(files, spec, commit)
    if not text and pack is None:
        raise ValueError(f'preset {spec}: holds no {PACK_TOML}, {PACK_SKILLS}/ or rules.md')
    return text, pack


def load_preset(spec: str) -> tuple[dict[str, Any], str, Pack | None]:
    """(validated preset, its sha256, its team pack). A local TOML file is a preset alone. gh:owner/repo[/path]
    (fetched at the head commit of the default branch with the user's own gh login) and a local folder
    are a team pack rooted at the preset's folder: the preset (default agent-kit-preset.toml), skills/
    and rules.md, each optional; nothing else there is read. The sha256 covers the pack's files too.
    PresetUnavailable when a gh: fetch fails; ValueError when the preset or pack is invalid or holds a
    secret."""
    if spec.startswith('gh:'):
        repo, path = _gh_repo(spec)
        commit = gh_head(repo)
        files = pack_files(gh_entries(repo, commit, path or PACK_TOML, spec), spec)
        toml = (path or PACK_TOML).rpartition('/')[2]
        if path and toml not in files:
            raise ValueError(f'preset {spec}: no {path} in the repository')
        text, pack = _pack_preset(files, toml, spec, commit)
    elif Path(spec).expanduser().is_dir():
        folder = Path(spec).expanduser()
        text, pack = _pack_preset(pack_files(folder_entries(folder), spec), PACK_TOML, spec, git_head(folder))
    else:
        text, pack = Path(spec).expanduser().read_text(), None
    try:
        preset = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f'preset {spec}: not valid TOML: {error}') from None
    validate_preset(preset, spec)
    total = hashlib.sha256(text.encode())
    for path, (data, mode) in sorted(pack.files.items() if pack else []):
        total.update(f'\0{path}\0{int(bool(mode & 0o111))}\0{len(data)}\0'.encode() + data)
    return preset, total.hexdigest(), pack


def source_status(record: dict[str, Any]) -> dict[str, Any]:
    """For doctor: whether the preset's source (a TOML file, a pack folder or gh:) moved on since setup.
    changed is None when it could not be checked."""
    try:
        _, sha, pack = load_preset(record['source'])
    except (PresetUnavailable, OSError, ValueError) as error:
        return {'changed': None, 'note': f'not checked: {error}'}
    if sha == record['sha256']:
        return {'changed': False, 'note': 'unchanged'}
    moved = f'new commit {pack.commit[:12]}' if pack and pack.commit and pack.commit != record.get('commit') else 'changed'
    return {'changed': True, 'note': f'{moved} since setup: rerun with --preset {record["source"]} to apply it'}


def kit_skills(source: Path) -> list[str]:
    """The skills a kit checkout ships."""
    return [path.name for path in sorted((source / 'skills').iterdir()) if path.is_dir()]


def _label(pack: Pack) -> str:
    return pack.commit[:12] if pack.commit else 'its working tree'


def pack_summary(source: str, before: Pack | None, after: Pack) -> str:
    """The preview line for a pack: what it installs, or what changed since the installed one."""
    if before is None:
        rules = f'; rules block of {len(after.rules.split())} words' if after.rules.strip() else ''
        return f'pack {source} at {_label(after)}: skills {", ".join(after.skills) or "none"}{rules}'
    if before.files == after.files:
        return f'pack {source} at {_label(after)}: content unchanged since the install ({_label(before)})'
    changes = []
    for verb, names in (('added', sorted(set(after.skills) - set(before.skills))),
                        ('changed', sorted(name for name in set(after.skills) & set(before.skills)
                                           if after.skill_files(name) != before.skill_files(name))),
                        ('removed', sorted(set(before.skills) - set(after.skills)))):
        if names:
            changes.append(f'skills {verb} {", ".join(names)}')
    if before.rules != after.rules:
        changes.append('rules ' + ('removed' if not after.rules.strip() else 'added' if not before.rules.strip() else 'changed'))
    return f'pack {source}: {_label(before)} -> {_label(after)}: {"; ".join(changes)}'


@dataclass(frozen=True)
class Preset:
    """A preset as setup uses it. kit: its [kit] values as local/preset.env lines hold them, the
    layer under the user's kit.env. answers: the setup answers it supplies, under the user's own.
    pack: a team pack's skills and rules block; pack_skills: its skills this install selects, on top
    of the kit skills the answers select."""
    kit: dict[str, str] = field(default_factory=dict)
    servers: dict[str, Any] = field(default_factory=dict)
    record: dict[str, Any] | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    recommended_hosts: list[str] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    pack: Pack | None = None
    pack_skills: list[str] = field(default_factory=list)

    def saved(self) -> dict[str, Any]:
        """What current.json keeps, so a later run without --preset applies the same values."""
        return {'kit': self.kit, 'servers': self.servers, 'answers': self.answers,
                'recommended_hosts': self.recommended_hosts, 'pack_skills': self.pack_skills}

    def env_text(self) -> str:
        return ''.join(f'{key}={value}\n' for key, value in self.kit.items())


def _loaded_preset(spec: str, preset: dict[str, Any], sha: str, pack: Pack | None, kept: Pack | None,
                   kit_names: list[str]) -> Preset:
    table = preset.get('kit', {})
    kit = {key: json.dumps(value) if isinstance(value, list) else value for key, value in table.items()}
    answers: dict[str, Any] = {answer: table[key] for key, answer in PRESET_ANSWERS.items() if key in table}
    skills = preset.get('skills', {})
    exclude = skills.get('exclude', [])
    pack_names = pack.skills if pack is not None else []
    if skills.get('include') or exclude:
        # No pack skill: those are selected through pack_skills, so a later pack that drops one
        # leaves no saved answer naming it. An unknown name stays, for setup's unknown-skill check.
        names = skills.get('include') or kit_names
        answers['skills'] = [name for name in names if name not in exclude and name not in pack_names]
    for flag in ('model', 'effort'):
        values = [f'{name}={role[flag]}' for name, role in preset.get('roles', {}).items() if flag in role]
        if values:
            answers['role_' + flag] = values
    record: dict[str, Any] = {'source': spec if spec.startswith('gh:') else str(Path(spec).expanduser().absolute()),
                              'sha256': sha}
    notes = [('Ready', f'preset {record["source"]} (sha256 {sha[:12]})')]
    if pack is not None:
        if pack.commit:
            record['commit'] = pack.commit
        notes.append(('Ready', pack_summary(record['source'], kept, pack)))
    return Preset(kit=kit, servers=preset.get('mcp', {}).get('servers', {}), record=record, answers=answers,
                  recommended_hosts=preset.get('hosts', {}).get('recommended', []), notes=notes, pack=pack,
                  pack_skills=[name for name in pack_names if name not in exclude])


def apply_preset(spec: str | None, saved: dict[str, Any], source: Path, interactive: bool,
                 kept_pack: Callable[[], Pack | None]) -> Preset:
    """--preset loaded, else the preset an earlier install saved (configuration preset and
    preset_values, and the pack kept_pack reads back from that install), else none. A pack skill
    named like a kit skill refuses the run, the kept one too: a kit update may add a skill of the
    same name. So does a kept pack that changed since setup, unless --preset loads a new one."""
    values = saved.get('preset_values', {})
    try:
        kept, stale = kept_pack(), None
    except StalePack as error:
        kept, stale = None, error
    earlier = Preset(kit=dict(values.get('kit', {})), servers=dict(values.get('servers', {})), record=saved.get('preset'),
                     answers=dict(values.get('answers', {})), recommended_hosts=list(values.get('recommended_hosts', [])),
                     pack=kept, pack_skills=list(values.get('pack_skills', kept.skills if kept is not None else [])))
    kit_names = kit_skills(source)
    if not spec:
        if stale is not None:
            raise stale
        result = earlier
    else:
        try:
            result = _loaded_preset(spec, *load_preset(spec), kept, kit_names)
        except PresetUnavailable as error:
            if stale is not None:
                raise ValueError(f'preset {spec} unavailable: {error}; the pack it installed cannot be reinstalled '
                                 f'offline: {stale}') from None
            print(f'agent-setup: preset {spec} unavailable: {error}; continuing with '
                  f'{"interactive " if interactive else ""}defaults', file=sys.stderr)
            result = replace(earlier, notes=[('Skipped', f'preset {spec}: {error}')])
    clash = sorted(set(result.pack.skills) & set(kit_names)) if result.pack is not None else []
    if clash:
        label = result.record['source'] if result.record else spec
        raise ValueError(f'preset {label}: pack skill {", ".join(clash)} has the name of a kit skill; rename it in the pack')
    return result
