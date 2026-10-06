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
from typing import Any, NamedTuple

from credentials import holds_secret, show_args, show_url, valid_declarations
from hosts import HOSTS, normalize_transport
from pack import (PACK_SKILLS, PACK_TOML, Pack, PackFiles, PresetUnavailable, StalePack, build_pack, folder_entries,
                  gh_entries, gh_file, gh_head, git_head, pack_files, pack_summary, utf8_text)

# Preset [kit] keys: overlay keys (hooks/lib/README) a team shares. The first three also answer
# setup's own questions, so its summary and checks see them.
PRESET_ANSWERS = {'CODE_SEARCH_GH_OWNER': 'github_owner', 'CODE_DIRS_JSON': 'repo_roots',
                  'CODE_SEARCH_ZOEKT_URL': 'zoekt_url'}
PRESET_KIT_KEYS = (*PRESET_ANSWERS, 'REVIEW_BASE', 'RELEASE_BRANCH_RE', 'BQRO_PROJECT', 'GIT_AUTHOR',
                   'SUBAGENT_RESUME_MAX')
def inline_secret(text: str) -> bool:
    return holds_secret(text)


def validate_catalog(catalog: Any) -> dict[str, Any]:
    """An MCP catalog ({"mcpServers": {name: spec}}) with each spec normalized; refuses inline
    credentials (credentials.py decides), which belong in a runtime wrapper or native OAuth. A
    spec may declare the Keychain items its wrapper reads (`credentials`), names only."""
    if not isinstance(catalog, dict) or not isinstance(catalog.get('mcpServers', {}), dict):
        raise ValueError('MCP catalog must map server names to objects')
    servers = {}
    for name, spec in catalog.get('mcpServers', {}).items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or not isinstance(spec, dict):
            raise ValueError('MCP catalog has an invalid server descriptor')
        if set(spec) - {'command', 'args', 'url', 'type', 'description', 'credentials'}:
            raise ValueError(f'MCP {name}: use native OAuth or a runtime wrapper, not inline secrets')
        if 'credentials' in spec and not valid_declarations(spec['credentials']):
            raise ValueError(f'MCP {name}: credentials must list {{"service": <service>, "account": <account>}}')
        normalized = normalize_transport(spec)
        arguments = normalized.get('args', [])
        found = [*show_url(normalized['url']).credentials] if 'url' in normalized else []
        if isinstance(arguments, list):
            found += show_args(arguments).credentials
        if found or inline_secret(json.dumps(normalized)):
            raise ValueError(f'MCP {name}: inline credential refused ({", ".join(found) or "a token shape"}); '
                             'use a runtime wrapper that reads it from the Keychain')
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


class LoadedPreset(NamedTuple):
    """A validated preset, its sha256 (over the pack's files too), its team pack, and the commit it
    was read at (every gh: preset; a local folder that is a clean git repository)."""
    preset: dict[str, Any]
    sha256: str
    pack: Pack | None
    commit: str | None


def _pack_preset(files: PackFiles, spec: str, commit: str | None) -> tuple[str, Pack | None]:
    """(the preset text, the pack) of a pack's files. The text's line endings are normalized, as a
    text read of the file does, so its sha256 does not depend on how it was fetched."""
    found = PACK_TOML in files
    text = preset_text(files.pop(PACK_TOML, (b'', 0))[0], PACK_TOML, spec)
    pack = build_pack(files, spec, commit)
    if not found and pack is None:
        raise ValueError(f'preset {spec}: holds no {PACK_TOML}, {PACK_SKILLS}/ or rules.md')
    return text, pack


def preset_text(data: bytes, name: str, spec: str) -> str:
    return utf8_text(data, name, spec).replace('\r\n', '\n').replace('\r', '\n')


def load_preset(spec: str, head: str | None = None) -> LoadedPreset:
    """The preset of spec and, when it is a pack root, its team pack. A pack root is a folder, or
    the folder of a file named agent-kit-preset.toml (gh:owner/repo reads the repository's): that
    preset, skills/ and rules.md, each optional, and nothing else there is read. A TOML file of any
    other name is read alone. gh:owner/repo[/path] is read at head, the commit at the head of the
    default branch (resolved here unless given), with the user's own gh login. PresetUnavailable when
    a gh: fetch fails; ValueError when the preset or pack is invalid or holds a secret."""
    if spec.startswith('gh:'):
        repo, path = _gh_repo(spec)
        root, _, toml = (path or PACK_TOML).rpartition('/')
        commit: str | None = head or gh_head(repo)
        if toml == PACK_TOML:
            files = pack_files(gh_entries(repo, commit, root, spec), spec)
            if path and PACK_TOML not in files:
                raise ValueError(f'preset {spec}: no {path} in the repository')
            text, pack = _pack_preset(files, spec, commit)
        else:
            text, pack = preset_text(gh_file(repo, commit, path or toml), toml, spec), None
    else:
        local = Path(spec).expanduser()
        if not local.exists():
            raise ValueError(f'preset {spec}: no such file or folder')
        # A link to a preset file is read where it points: its folder there is the pack root.
        file = local.resolve()
        if local.is_dir() or file.name == PACK_TOML:
            folder = local if local.is_dir() else file.parent
            commit = git_head(folder)
            text, pack = _pack_preset(pack_files(folder_entries(folder), spec), spec, commit)
        else:
            text, pack, commit = preset_text(file.read_bytes(), file.name, spec), None, None
    try:
        preset = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f'preset {spec}: not valid TOML: {error}') from None
    validate_preset(preset, spec)
    total = hashlib.sha256(text.encode())
    for relative, (data, mode) in sorted(pack.files.items() if pack else []):
        total.update(f'\0{relative}\0{int(bool(mode & 0o111))}\0{len(data)}\0'.encode() + data)
    return LoadedPreset(preset, total.hexdigest(), pack, commit)


def recorded_head(record: dict[str, Any] | None, spec: str) -> tuple[str | None, bool]:
    """(the head commit of a gh: spec, whether it is the commit record was installed from). None and
    False for a local spec."""
    if not spec.startswith('gh:'):
        return None, False
    head = gh_head(_gh_repo(spec)[0])
    return head, bool(record) and record.get('source') == spec and record.get('commit') == head


def source_status(record: dict[str, Any]) -> dict[str, Any]:
    """For doctor: whether the preset's source (a TOML file, a pack folder or gh:) moved on since setup.
    A gh: source still at the recorded commit is unchanged without a download. changed is None when it
    could not be checked."""
    try:
        head, unmoved = recorded_head(record, record['source'])
        if unmoved:
            return {'changed': False, 'note': 'unchanged'}
        loaded = load_preset(record['source'], head)
    except (PresetUnavailable, OSError, ValueError) as error:
        return {'changed': None, 'note': f'not checked: {error}'}
    if loaded.sha256 == record['sha256']:
        return {'changed': False, 'note': 'unchanged'}
    moved = f'new commit {loaded.commit[:12]}' if loaded.commit and loaded.commit != record.get('commit') else 'changed'
    return {'changed': True, 'note': f'{moved} since setup: rerun with --preset {record["source"]} to apply it'}


def kit_skills(source: Path) -> list[str]:
    """The skills a kit checkout ships."""
    return [path.name for path in sorted((source / 'skills').iterdir()) if path.is_dir()]


@dataclass(frozen=True)
class Preset:
    """A preset as setup uses it. kit: its [kit] values as local/preset.env lines hold them, the
    layer under the user's kit.env. answers: the setup answers it supplies, under the user's own.
    pack: a team pack's skills and rules block; pack_skills: its skills this install selects, on top
    of the kit skills the answers select. table: the validated preset these were derived from, which
    a later run derives them from again, against the kit skills it finds."""
    kit: dict[str, str] = field(default_factory=dict)
    servers: dict[str, Any] = field(default_factory=dict)
    record: dict[str, Any] | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    recommended_hosts: list[str] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    pack: Pack | None = None
    pack_skills: list[str] = field(default_factory=list)
    table: dict[str, Any] | None = None

    def saved(self) -> dict[str, Any]:
        """What current.json keeps, so a later run without --preset applies the same values."""
        values = {'kit': self.kit, 'servers': self.servers, 'answers': self.answers,
                  'recommended_hosts': self.recommended_hosts, 'pack_skills': self.pack_skills}
        return values if self.table is None else {**values, 'table': self.table}

    def env_text(self) -> str:
        return ''.join(f'{key}={value}\n' for key, value in self.kit.items())


def _notes(record: dict[str, Any], pack: Pack | None, kept: Pack | None) -> list[tuple[str, str]]:
    notes = [('Ready', f'preset {record["source"]} (sha256 {record["sha256"][:12]})')]
    if pack is not None:
        notes.append(('Ready', pack_summary(record['source'], kept, pack)))
    return notes


def _loaded_preset(spec: str, loaded: LoadedPreset, kept: Pack | None, kit_names: list[str]) -> Preset:
    preset, pack = loaded.preset, loaded.pack
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
                              'sha256': loaded.sha256}
    if loaded.commit:
        record['commit'] = loaded.commit
    return Preset(kit=kit, servers=preset.get('mcp', {}).get('servers', {}), record=record, answers=answers,
                  recommended_hosts=preset.get('hosts', {}).get('recommended', []), notes=_notes(record, pack, kept),
                  pack=pack, pack_skills=[name for name in pack_names if name not in exclude], table=preset)


def _earlier(saved: dict[str, Any], kept: Pack | None, kit_names: list[str]) -> Preset:
    """The preset an earlier install saved (configuration preset and preset_values) with kept, the pack
    it kept: derived again from its table, so a [skills] exclude follows the kit skills of this run.
    An install from before the table was saved has its values as saved."""
    values = saved.get('preset_values', {})
    record = saved.get('preset')
    if record and 'table' in values:
        loaded = LoadedPreset(values['table'], record['sha256'], kept, record.get('commit'))
        return _loaded_preset(record['source'], loaded, kept, kit_names)
    return Preset(kit=dict(values.get('kit', {})), servers=dict(values.get('servers', {})), record=record,
                  answers=dict(values.get('answers', {})), recommended_hosts=list(values.get('recommended_hosts', [])),
                  pack=kept, pack_skills=list(values.get('pack_skills', kept.skills if kept is not None else [])))


def apply_preset(spec: str | None, saved: dict[str, Any], source: Path, interactive: bool,
                 installed: Pack | StalePack | None) -> Preset:
    """--preset loaded, else the preset an earlier install saved, else none. installed is the pack
    that install kept, or a StalePack when it changed since: that refuses the run unless --preset
    loads a new one. A gh: preset still at the commit of that install is not downloaded again. A pack
    skill named like a kit skill refuses the run, the kept one too: a kit update may add a skill of
    the same name."""
    kept = installed if isinstance(installed, Pack) else None
    stale = installed if isinstance(installed, StalePack) else None
    kit_names = kit_skills(source)
    earlier = _earlier(saved, kept, kit_names)
    if not spec:
        if stale is not None:
            raise stale
        result = replace(earlier, notes=[])
    else:
        try:
            head, unmoved = recorded_head(earlier.record, spec)
            if unmoved and earlier.table is not None and stale is None:
                result = earlier
            else:
                result = _loaded_preset(spec, load_preset(spec, head), kept, kit_names)
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
