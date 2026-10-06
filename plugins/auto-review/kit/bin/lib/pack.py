"""Team packs (README "Team packs"): a preset's skills/ and rules.md, read from a gh: repository at a
commit or a local folder, scanned for secrets before setup writes any of it, and the copy an install
keeps under <kit root>/pack."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
from typing import Any, Literal, NamedTuple

from hosts import skill_hosts
from preflight import install_hint

TOKEN_FORMATS = (r'gh[opsur]_[A-Za-z0-9]{8,}|github_pat_\w{8,}|sk-(?:ant-)?[\w-]{8,}|[sr]k_live_[A-Za-z0-9]{16,}'
                 r'|xox[abpr]-[\w-]{8,}|hooks\.slack\.com/(?:services|workflows|triggers)/[\w/-]{16,}'
                 r'|AKIA[0-9A-Z]{16}|glpat-[\w-]{8,}|AIza[\w-]{20,}|npm_[A-Za-z0-9]{20,}')
TOKEN = re.compile(rf'(?<![A-Za-z0-9])(?:{TOKEN_FORMATS})')
# Documentation writes a token's shape with a run of X (ghp_XXXXXXXXXXXXXXXXXXXX); a real token is random.
TOKEN_PLACEHOLDER = re.compile(r'[Xx]{4}')
PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
# By name: files that hold a credential whatever their content. A .pem or .key may hold a public
# certificate, so those are refused by the private key check on their content.
SECRET_FILE = re.compile(
    r'\.env(?:\..+)?|\.envrc|.+\.tfvars(?:\.json)?|.+\.(?:p12|pfx|jks|keystore)|id_(?:rsa|dsa|ecdsa|ed25519)'
    r'|\.netrc|\.npmrc|\.pypirc|\.pgpass|secrets\.ya?ml|credentials\.json|auth\.json|kit\.env|settings\.local\.json')
SECRET_FILE_EXAMPLE = re.compile(r'\.env\.(?:example|sample|template)')
PACK_TOML = 'agent-kit-preset.toml'
PACK_RULES = 'rules.md'
PACK_SKILLS = 'skills'
PACK_RULES_WORDS = 300
PACK_MAX_BYTES = 20 * 1024 * 1024
PACK_SKILL_NAME = re.compile(r'[a-z0-9][a-z0-9_-]*')
PACK_DIR = 'pack'

PackFiles = dict[str, tuple[bytes, int]]


class PresetUnavailable(Exception):
    """A gh: preset that could not be fetched: setup continues on its own defaults."""


class StalePack(ValueError):
    """A file of the kept pack changed or went missing since the install that wrote it."""


class Entry(NamedTuple):
    """A path of a pack (relative to its root) as read: a link's data is its target."""
    path: str
    kind: Literal['file', 'link', 'other']
    data: bytes
    mode: int


def holds_token(text: str) -> bool:
    """Whether text holds a token format, other than one written as a placeholder."""
    return any(not TOKEN_PLACEHOLDER.search(match.group()) for match in TOKEN.finditer(text))


def in_pack(path: str, toml: str) -> bool:
    return path in (PACK_RULES, toml, PACK_SKILLS) or path.startswith(PACK_SKILLS + '/')


def gh_api(arguments: list[str], repo: str, timeout: int) -> bytes:
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
    commit = gh_api([f'repos/{repo}/commits/HEAD', '--jq', '.sha'], repo, 20).decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise PresetUnavailable(f'gh returned no commit for {repo}')
    return commit


def gh_entries(repo: str, commit: str, toml: str, spec: str) -> Iterator[Entry]:
    """The pack at commit of repo: rooted at the folder of toml (a path in the repository)."""
    archive = gh_api([f'repos/{repo}/tarball/{commit}'], repo, 120)
    root, _, name = toml.rpartition('/')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:*') as tar:
        for member in tar:
            if member.name.startswith('/') or '..' in PurePosixPath(member.name).parts:
                raise ValueError(f'preset {spec}: archive path {member.name} leaves the pack')
            # GitHub's archive holds the tree under one <owner>-<repo>-<sha> folder.
            relative = '/'.join(PurePosixPath(member.name).parts[1:])
            if root:
                if not relative.startswith(root + '/'):
                    continue
                relative = relative[len(root) + 1:]
            if member.isdir() or not in_pack(relative, name):
                continue
            if member.issym():
                yield Entry(relative, 'link', member.linkname.encode(), 0)
            elif member.isfile():
                handle = tar.extractfile(member)
                yield Entry(relative, 'file', handle.read() if handle else b'', member.mode & 0o777)
            else:
                yield Entry(relative, 'other', b'', 0)


def folder_entries(folder: Path, toml: str = PACK_TOML) -> Iterator[Entry]:
    """The pack in a local folder: its skills/, rules.md and toml, nothing else of it."""
    def entry(path: Path) -> Entry | None:
        relative = path.relative_to(folder).as_posix()
        if path.is_symlink():
            return Entry(relative, 'link', os.readlink(path).encode(), 0)
        if path.is_file():
            return Entry(relative, 'file', path.read_bytes(), path.stat().st_mode & 0o777)
        return None if path.is_dir() else Entry(relative, 'other', b'', 0)

    for name in (PACK_RULES, toml, PACK_SKILLS):
        if os.path.lexists(folder / name) and (found := entry(folder / name)):
            yield found
    skills = folder / PACK_SKILLS
    if skills.is_symlink():
        return
    for directory, folders, names in os.walk(skills):
        folders[:] = sorted(name for name in folders if name != '.git')
        for name in [*folders, *sorted(names)]:
            if found := entry(Path(directory) / name):
                yield found


def pack_files(entries: Iterable[Entry], spec: str) -> PackFiles:
    """Every file of a pack (path -> content, mode). A link that names a file in the pack reads as
    that file; a link out of the pack or to a folder, a special file, or two paths that differ only
    in case (one file on macOS) refuses."""
    files: PackFiles = {}
    links: dict[str, str] = {}
    folded: dict[str, str] = {}
    total = 0
    for relative, kind, data, mode in entries:
        parts = relative.split('/')
        for depth in range(1, len(parts) + 1):
            path = '/'.join(parts[:depth])
            other = folded.setdefault(path.casefold(), path)
            if other != path:
                raise ValueError(f'preset {spec}: {other} and {path} differ only in case')
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
    """A team pack's installable content: skills/<name>/... and rules.md, path -> (content, mode),
    and the commit it was read at (None for a folder outside git or with uncommitted changes)."""
    files: PackFiles
    commit: str | None = None

    @property
    def skills(self) -> list[str]:
        return sorted({path.split('/')[1] for path in self.files if path.startswith(PACK_SKILLS + '/')})

    @property
    def rules(self) -> str:
        return self.files.get(PACK_RULES, (b'', 0))[0].decode()

    def skill_files(self, name: str) -> PackFiles:
        return {path: value for path, value in self.files.items() if path.startswith(f'{PACK_SKILLS}/{name}/')}


def build_pack(files: PackFiles, spec: str, commit: str | None) -> Pack | None:
    """The pack in files (skills/ and rules.md), None when it has neither. Refused when a file looks
    like a credential (by name, a token format or a private key), a skill is malformed, or rules.md is
    over PACK_RULES_WORDS words."""
    if not files:
        return None
    for relative, (data, _) in sorted(files.items()):
        name = relative.rsplit('/', 1)[-1]
        if SECRET_FILE.fullmatch(name) and not SECRET_FILE_EXAMPLE.fullmatch(name):
            raise ValueError(f'preset {spec}: {relative} looks like a credential file; a pack holds no secrets')
        text = data.decode('latin-1')
        if holds_token(text) or PRIVATE_KEY.search(text):
            raise ValueError(f'preset {spec}: {relative} holds a token or private key; a pack holds no secrets')
        if relative == PACK_SKILLS or (relative.startswith(PACK_SKILLS + '/') and relative.count('/') == 1):
            raise ValueError(f'preset {spec}: {relative}: a pack skill is a folder skills/<name>/ with a SKILL.md')
    pack = Pack(files, commit)
    for name in pack.skills:
        skill = files.get(f'{PACK_SKILLS}/{name}/SKILL.md')
        if not PACK_SKILL_NAME.fullmatch(name) or skill is None:
            raise ValueError(f'preset {spec}: skills/{name} needs a lowercase name and a SKILL.md')
        try:
            skill_hosts(Path(f'{PACK_SKILLS}/{name}/SKILL.md'), skill[0].decode())
        except ValueError as error:
            raise ValueError(f'preset {spec}: {error}') from None
    words = len(pack.rules.split())
    if words > PACK_RULES_WORDS:
        raise ValueError(f'preset {spec}: rules.md has {words} words, over the cap of {PACK_RULES_WORDS}; '
                         'move the detail into a pack skill')
    return pack


def git_head(folder: Path) -> str | None:
    """The commit of a git repository folder, None when it has uncommitted changes."""
    if not (folder / '.git').exists() or not shutil.which('git'):
        return None
    head, status = (subprocess.run(['git', '-C', str(folder), *arguments], capture_output=True, text=True,
                                   timeout=10, stdin=subprocess.DEVNULL)
                    for arguments in (['rev-parse', 'HEAD'], ['status', '--porcelain', '--untracked-files=all']))
    return head.stdout.strip() if head.returncode == 0 and status.returncode == 0 and not status.stdout else None


def kept_records(root: Path, managed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The managed file records of the pack kept under <root>/pack, by path in the pack."""
    folder = (root / PACK_DIR).resolve()
    kept = {}
    for record in managed.values():
        target = Path(record['target']).resolve()
        if record['kind'] == 'file' and folder in target.parents:
            kept[target.relative_to(folder).as_posix()] = record
    return kept


def installed_pack(root: Path, state: dict[str, Any], drift: Callable[[dict[str, Any]], Collection[str]]) -> Pack | None:
    """The pack an earlier install kept under <root>/pack: the files it recorded, read back (a file
    added there since is not part of it). StalePack when drift (setup's check of a state's managed
    records) finds one of them changed: reinstalling an edited copy would spread the edit."""
    kept = kept_records(root, state.get('managed', {}))
    if not kept:
        return None
    record = state.get('configuration', {}).get('preset') or {}
    stale = sorted(drift({'managed': kept}))
    if stale:
        raise StalePack(f'{len(stale)} file(s) of the team pack kept under {root / PACK_DIR} changed since setup '
                        f'({", ".join(stale[:3])}): rerun agent-setup with --preset {record.get("source", "<its source>")} '
                        '--collision backup to restore it')
    folder = root / PACK_DIR
    return Pack({path: ((folder / path).read_bytes(), entry['mode'] or 0o644) for path, entry in kept.items()},
                record.get('commit'))
