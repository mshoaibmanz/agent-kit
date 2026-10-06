"""Team presets for agent-setup (presets/example.toml documents the format), the team packs that
carry them with skills and a rules block (README "Team packs"), and the MCP catalog check they
share with setup's own catalog."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from hosts import HOSTS, normalize_transport, skill_hosts
from preflight import install_hint

# Preset [kit] keys: overlay keys (hooks/lib/README) a team shares. The first three also answer
# setup's own questions, so its summary and checks see them.
PRESET_ANSWERS = {'CODE_SEARCH_GH_OWNER': 'github_owner', 'CODE_DIRS_JSON': 'repo_roots',
                  'CODE_SEARCH_ZOEKT_URL': 'zoekt_url'}
PRESET_KIT_KEYS = (*PRESET_ANSWERS, 'REVIEW_BASE', 'RELEASE_BRANCH_RE', 'BQRO_PROJECT', 'GIT_AUTHOR',
                   'SUBAGENT_RESUME_MAX')
TOKEN_FORMATS = (r'gh[opsur]_[A-Za-z0-9]{8,}|github_pat_\w{8,}|sk-(?:ant-)?[\w-]{8,}|xox[abpr]-[\w-]{8,}'
                 r'|AKIA[0-9A-Z]{16}|glpat-[\w-]{8,}|AIza[\w-]{20,}')
# Token formats with a fixed prefix, credentials in a URL, and a bearer header. Plain words such as
# "secret" or "token" are not refused: they turn up in paths and names.
INLINE_SECRET = re.compile(
    rf'(?<![A-Za-z0-9])(?:{TOKEN_FORMATS})'
    r'|://[^/\s@]+@|[?&](?:access[-_]?token|auth[-_]?token|token|api[-_]?key|key|password|secret)='
    r'|\bbearer\s', re.I)
# Over every file of a pack: the token formats and a private key. Not the URL and bearer forms,
# which skill prose about auth legitimately shows.
PACK_SECRET = re.compile(rf'(?<![A-Za-z0-9])(?:{TOKEN_FORMATS})|-----BEGIN [A-Z ]*PRIVATE KEY-----'.encode())
PACK_SECRET_FILE = re.compile(
    r'\.env(?:\..+)?|.+\.(?:pem|key|p12|pfx|jks|keystore)|id_(?:rsa|dsa|ecdsa|ed25519)'
    r'|\.netrc|\.npmrc|\.pypirc|credentials\.json|auth\.json|kit\.env|settings\.local\.json')
PACK_SECRET_FILE_EXAMPLE = re.compile(r'\.env\.(?:example|sample|template)')
PACK_TOML = 'agent-kit-preset.toml'
PACK_RULES = 'rules.md'
PACK_RULES_WORDS = 300
PACK_MAX_BYTES = 20 * 1024 * 1024
PACK_SKILL_NAME = re.compile(r'[a-z0-9][a-z0-9_-]*')
# Where an install keeps the pack as fetched (skills/ and rules.md), so a later run without
# --preset installs the same content and rollback restores the previous one.
PACK_DIR = 'pack'
PACK_STALE = 'changed since setup: rerun agent-setup with --preset'
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


def _gh_api(arguments: list[str], repo: str, timeout: int) -> bytes:
    """`gh api` output with the user's own login; PresetUnavailable when it cannot answer."""
    if not shutil.which('gh'):
        raise PresetUnavailable(f'gh is not installed ({install_hint("gh")})')
    try:
        result = subprocess.run(['gh', 'api', *arguments], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PresetUnavailable(str(error)) from None
    if result.returncode:
        reason = (result.stderr.decode(errors='replace').strip().splitlines() or ['gh api failed'])[0]
        raise PresetUnavailable(f'{reason} (check `gh auth status` and your access to {repo})')
    return result.stdout


def gh_head(repo: str) -> str:
    """The commit at the head of repo's default branch."""
    commit = _gh_api([f'repos/{repo}/commits/HEAD', '--jq', '.sha'], repo, 20).decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise PresetUnavailable(f'gh returned no commit for {repo}')
    return commit


# A pack entry: (path relative to the pack, file / link / other, content or link target, mode).
Entry = tuple[str, str, bytes, int]


def _folder_entries(folder: Path) -> Iterator[Entry]:
    for directory, folders, names in os.walk(folder):
        folders.sort()
        for name in [*folders, *sorted(names)]:
            path = Path(directory) / name
            relative = path.relative_to(folder).as_posix()
            if path.is_symlink():
                yield relative, 'link', os.readlink(path).encode(), 0
            elif path.is_file():
                yield relative, 'file', path.read_bytes(), path.stat().st_mode & 0o777
            elif not path.is_dir():
                yield relative, 'other', b'', 0


def _archive_entries(archive: bytes, spec: str) -> Iterator[Entry]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:*') as tar:
        for member in tar:
            # GitHub's archive holds the tree under one <owner>-<repo>-<sha> folder.
            parts = PurePosixPath(member.name).parts[1:]
            if member.name.startswith('/') or '..' in PurePosixPath(member.name).parts:
                raise ValueError(f'preset {spec}: archive path {member.name} leaves the pack')
            if not parts or member.isdir():
                continue
            relative = '/'.join(parts)
            if member.issym():
                yield relative, 'link', member.linkname.encode(), 0
            elif member.isfile():
                handle = tar.extractfile(member)
                yield relative, 'file', handle.read() if handle else b'', member.mode & 0o777
            else:
                yield relative, 'other', b'', 0


def pack_files(entries: Iterator[Entry], spec: str) -> dict[str, tuple[bytes, int]]:
    """Every file of a pack (path -> content, mode), .git left out. A link that names a file in the
    pack reads as that file; a link out of the pack, to a folder, or any special file refuses."""
    files: dict[str, tuple[bytes, int]] = {}
    links: dict[str, str] = {}
    total = 0
    for relative, kind, data, mode in entries:
        if '.git' in relative.split('/'):
            continue
        if kind == 'other':
            raise ValueError(f'preset {spec}: {relative} is not a regular file or link')
        if kind == 'link':
            link = data.decode(errors='replace')
            target = PurePosixPath(os.path.normpath(os.path.join(os.path.dirname(relative), link))).as_posix()
            if link.startswith('/') or target == '..' or target.startswith('../'):
                raise ValueError(f'preset {spec}: symlink {relative} points outside the pack')
            links[relative] = target
            continue
        total += len(data)
        if total > PACK_MAX_BYTES:
            raise ValueError(f'preset {spec}: the pack is over {PACK_MAX_BYTES >> 20} MB')
        files[relative] = (data, mode)
    for relative, target in links.items():
        seen = {relative}
        while target in links and target not in seen:
            seen.add(target)
            target = links[target]
        if target not in files:
            raise ValueError(f'preset {spec}: symlink {relative} must name a file in the pack')
        files[relative] = files[target]
    return files


@dataclass(frozen=True)
class Pack:
    """A team pack's installable content: skills/<name>/... and rules.md, path -> (content, mode)."""
    files: dict[str, tuple[bytes, int]]

    @property
    def skills(self) -> list[str]:
        return sorted({path.split('/')[1] for path in self.files if path.startswith('skills/')})

    @property
    def rules(self) -> str:
        return self.files.get(PACK_RULES, (b'', 0))[0].decode()

    def digest(self) -> str:
        """Content, paths and executable bits: equal for the same tree fetched or kept."""
        total = hashlib.sha256()
        for path, (data, mode) in sorted(self.files.items()):
            total.update(f'{path}\0{int(bool(mode & 0o111))}\0{len(data)}\0'.encode() + data)
        return total.hexdigest()

    def skill_files(self, name: str) -> dict[str, tuple[bytes, int]]:
        return {path: value for path, value in self.files.items() if path.startswith(f'skills/{name}/')}


def build_pack(files: dict[str, tuple[bytes, int]], spec: str) -> Pack:
    """The pack in files, refused when any file looks like a credential (by name, a token format or
    a private key), a skill is malformed, or rules.md is over PACK_RULES_WORDS words."""
    for relative, (data, _) in sorted(files.items()):
        name = relative.rsplit('/', 1)[-1]
        if PACK_SECRET_FILE.fullmatch(name) and not PACK_SECRET_FILE_EXAMPLE.fullmatch(name):
            raise ValueError(f'preset {spec}: {relative} looks like a credential file; a pack holds no secrets')
        if PACK_SECRET.search(data):
            raise ValueError(f'preset {spec}: {relative} holds a token or private key; a pack holds no secrets')
    pack = Pack({path: value for path, value in files.items() if path.startswith('skills/') or path == PACK_RULES})
    for path in pack.files:
        if path.startswith('skills/') and path.count('/') == 1:
            raise ValueError(f'preset {spec}: {path}: a pack skill is a folder skills/<name>/ with a SKILL.md')
    for name in pack.skills:
        skill = pack.files.get(f'skills/{name}/SKILL.md')
        if not PACK_SKILL_NAME.fullmatch(name) or skill is None:
            raise ValueError(f'preset {spec}: skills/{name} needs a lowercase name and a SKILL.md')
        try:
            skill_hosts(Path(f'skills/{name}/SKILL.md'), skill[0].decode())
        except ValueError as error:
            raise ValueError(f'preset {spec}: {error}') from None
    words = len(pack.rules.split())
    if words > PACK_RULES_WORDS:
        raise ValueError(f'preset {spec}: rules.md has {words} words, over the cap of {PACK_RULES_WORDS}; '
                         'move the detail into a pack skill')
    return pack


def _git_head(folder: Path) -> str | None:
    if not (folder / '.git').exists() or not shutil.which('git'):
        return None
    result = subprocess.run(['git', '-C', str(folder), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                            timeout=10, stdin=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else None


def load_preset(spec: str) -> tuple[dict[str, Any], str, Pack | None, str | None]:
    """(validated preset, its sha256, the pack, its commit). A local TOML file is a preset alone. A
    local folder or gh:owner/repo[/path] is a team pack: the repository's tree (gh: at the head commit
    of its default branch, fetched with the user's own gh login) holding the preset at path (default
    agent-kit-preset.toml), skills/ and rules.md, each optional. PresetUnavailable when a gh: fetch
    fails; ValueError when the preset or pack is invalid or holds a secret."""
    pack = commit = None
    path, explicit = PACK_TOML, False
    if spec.startswith('gh:'):
        parts = spec[3:].split('/', 2)
        if len(parts) < 2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in parts[:2]):
            raise ValueError(f'preset {spec}: expected gh:owner/repo[/path]')
        if len(parts) == 3:
            path, explicit = parts[2].strip('/'), True
        repo = f'{parts[0]}/{parts[1]}'
        commit = gh_head(repo)
        files = pack_files(_archive_entries(_gh_api([f'repos/{repo}/tarball/{commit}'], repo, 120), spec), spec)
    elif Path(spec).expanduser().is_dir():
        folder = Path(spec).expanduser()
        files = pack_files(_folder_entries(folder), spec)
        commit = _git_head(folder)
    else:
        files = None
    if files is None:
        text = Path(spec).expanduser().read_text()
    else:
        pack = build_pack(files, spec)
        if explicit and path not in files:
            raise ValueError(f'preset {spec}: no {path} in the repository')
        text = files.get(path, (b'', 0))[0].decode()
        if not text and not pack.files:
            raise ValueError(f'preset {spec}: holds no {PACK_TOML}, skills/ or {PACK_RULES}')
    try:
        preset = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f'preset {spec}: not valid TOML: {error}') from None
    validate_preset(preset, spec)
    return preset, hashlib.sha256(text.encode()).hexdigest(), pack, commit


def pack_stale(record: dict[str, Any]) -> str:
    """The fix for a kept pack copy that was edited: fetch it again; backup keeps the edited files."""
    return f'{PACK_STALE} {record["source"]} --collision backup to restore it'


def installed_pack(root: Path, record: dict[str, Any] | None) -> Pack | None:
    """The pack an earlier install kept under <root>/pack, when its preset record names one. A copy
    that no longer matches the recorded digest refuses: reinstalling it would spread the edit."""
    folder = root / PACK_DIR
    if not record or 'pack' not in record or not folder.is_dir():
        return None
    pack = build_pack(pack_files(_folder_entries(folder), str(folder)), str(folder))
    if pack.digest() != record['pack']:
        raise ValueError(pack_stale(record))
    return pack


def _label(record: dict[str, Any]) -> str:
    return (record.get('commit') or record['pack'])[:12]


def pack_summary(before: Pack | None, was: dict[str, Any] | None, after: Pack, now: dict[str, Any]) -> str:
    """The preview line for a pack: what it installs, or what changed since the installed one."""
    if before is None or not was or 'pack' not in was:
        rules = f'; rules block of {len(after.rules.split())} words' if after.rules.strip() else ''
        return f'pack {now["source"]} at {_label(now)}: skills {", ".join(after.skills) or "none"}{rules}'
    if before.digest() == after.digest():
        return f'pack {now["source"]} at {_label(now)}: content unchanged since the install ({_label(was)})'
    changes = []
    for verb, names in (('added', sorted(set(after.skills) - set(before.skills))),
                        ('changed', sorted(name for name in set(after.skills) & set(before.skills)
                                           if after.skill_files(name) != before.skill_files(name))),
                        ('removed', sorted(set(before.skills) - set(after.skills)))):
        if names:
            changes.append(f'skills {verb} {", ".join(names)}')
    if before.rules != after.rules:
        changes.append('rules ' + ('removed' if not after.rules.strip() else 'added' if not before.rules.strip() else 'changed'))
    return f'pack {now["source"]}: {_label(was)} -> {_label(now)}: {"; ".join(changes)}'


def pack_status(root: Path, record: dict[str, Any]) -> dict[str, str]:
    """For doctor: the installed pack's commit, whether the copy setup keeps still matches what it
    installed, and whether the pack's source has moved on since."""
    status = {'source': record['source'], 'commit': record.get('commit') or '', 'digest': record['pack']}
    try:
        kept = installed_pack(root, record)
        status['installed'] = 'unchanged' if kept else pack_stale(record)
    except (OSError, ValueError) as error:
        status['installed'] = str(error) if str(error).startswith(PACK_STALE) else f'{pack_stale(record)} ({error})'
    try:
        if record['source'].startswith('gh:'):
            head = gh_head('/'.join(record['source'][3:].split('/')[:2]))
            moved = f'new commit {head[:12]}' if head != record.get('commit') else ''
        else:
            _, sha, pack, _ = load_preset(record['source'])
            moved = 'changed' if sha != record['sha256'] or not pack or pack.digest() != record['pack'] else ''
        status['upstream'] = f'{moved} since setup: rerun with --preset {record["source"]} to update' if moved else 'unchanged'
    except (PresetUnavailable, OSError, ValueError) as error:
        status['upstream'] = f'not checked: {error}'
    return status


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


def apply_preset(spec: str | None, saved: dict[str, Any], source: Path, interactive: bool, root: Path) -> Preset:
    """--preset loaded, else the preset an earlier install saved (configuration preset and
    preset_values, and the pack it keeps under <root>/pack), else none. A pack skill named like a
    kit skill refuses the run."""
    kept = saved.get('preset_values', {})
    try:
        earlier_pack = installed_pack(root, saved.get('preset'))
    except (OSError, ValueError):
        if not spec:
            raise
        earlier_pack = None  # the new pack replaces a kept copy that no longer loads
    earlier = Preset(kit=dict(kept.get('kit', {})), servers=dict(kept.get('servers', {})), record=saved.get('preset'),
                     answers=dict(kept.get('answers', {})), recommended_hosts=list(kept.get('recommended_hosts', [])),
                     pack=earlier_pack,
                     pack_skills=list(kept.get('pack_skills', earlier_pack.skills if earlier_pack else [])))
    kit_skills = [path.name for path in sorted((source / 'skills').iterdir()) if path.is_dir()]

    # Every pack this run installs, the kept one too: a kit update may add a skill of the same name.
    def refuse_clash(pack: Pack | None, label: str) -> None:
        clash = sorted(set(pack.skills) & set(kit_skills)) if pack else []
        if clash:
            raise ValueError(f'preset {label}: pack skill {", ".join(clash)} has the name of a kit skill; rename it in the pack')

    if not spec:
        refuse_clash(earlier.pack, earlier.record['source'] if earlier.record else '')
        return earlier
    try:
        preset, sha, pack, commit = load_preset(spec)
    except PresetUnavailable as error:
        refuse_clash(earlier.pack, earlier.record['source'] if earlier.record else '')
        print(f'agent-setup: preset {spec} unavailable: {error}; continuing with '
              f'{"interactive " if interactive else ""}defaults', file=sys.stderr)
        return Preset(earlier.kit, earlier.servers, earlier.record, earlier.answers, earlier.recommended_hosts,
                      [('Skipped', f'preset {spec}: {error}')], earlier.pack, earlier.pack_skills)
    refuse_clash(pack, spec)
    table = preset.get('kit', {})
    kit = {key: json.dumps(value) if isinstance(value, list) else value for key, value in table.items()}
    answers: dict[str, Any] = {answer: table[key] for key, answer in PRESET_ANSWERS.items() if key in table}
    skills = preset.get('skills', {})
    exclude = skills.get('exclude', [])
    if skills.get('include') or exclude:
        # No pack skill: those are selected through pack_skills, so a later pack that drops one
        # leaves no saved answer naming it. An unknown name stays, for setup's unknown-skill check.
        names = skills.get('include') or kit_skills
        answers['skills'] = [name for name in names if name not in exclude and not (pack and name in pack.skills)]
    for flag in ('model', 'effort'):
        values = [f'{name}={role[flag]}' for name, role in preset.get('roles', {}).items() if flag in role]
        if values:
            answers['role_' + flag] = values
    record: dict[str, Any] = {'source': spec if spec.startswith('gh:') else str(Path(spec).expanduser().absolute()),
                              'sha256': sha}
    notes = [('Ready', f'preset {record["source"]} (sha256 {sha[:12]})')]
    if pack:
        record.update(pack=pack.digest(), skills=pack.skills, **({'commit': commit} if commit else {}))
        notes.append(('Ready', pack_summary(earlier.pack, earlier.record, pack, record)))
    return Preset(kit=kit, servers=preset.get('mcp', {}).get('servers', {}), record=record, answers=answers,
                  recommended_hosts=preset.get('hosts', {}).get('recommended', []), notes=notes, pack=pack,
                  pack_skills=[name for name in pack.skills if name not in exclude] if pack else [])
