"""The personal overlay's skills and rules (README "Personal overlay"): <kit>/local/skills/<name>/ and
<kit>/local/rules.md, installed like a team pack's. Setup copies each overlay skill into <kit>/skills
and links it for every host its `hosts:` allows; the rules block follows the pack's in each host's
rules. An overlay skill replaces a kit or pack skill of the same name, and setup and doctor say so."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from hosts import skill_hosts
from kit_text import PACK_DIR
from pack import NOT_PACK, PACK_SKILL_NAME, PackFiles, normal_mode

OVERLAY_SKILLS = 'skills'
OVERLAY_RULES = 'rules.md'
# Personal rules ride in every session of every host, so they get a budget like the pack's own.
OVERLAY_RULES_WORDS = 400


@dataclass(frozen=True)
class Overlay:
    """The overlay's installable files, path (skills/<name>/..., rules.md) -> (content, mode)."""
    folder: Path
    files: PackFiles

    @property
    def skills(self) -> list[str]:
        return sorted({path.split('/')[1] for path in self.files if path.startswith(OVERLAY_SKILLS + '/')})

    @property
    def rules(self) -> str:
        return self.files.get(OVERLAY_RULES, (b'', 0))[0].decode()

    def digest(self) -> str:
        """One hash of every installable file: setup records it, doctor compares."""
        sha = hashlib.sha256()
        for path, (data, mode) in sorted(self.files.items()):
            sha.update(f'{path}\0{mode:o}\0{len(data)}\0'.encode() + data)
        return sha.hexdigest()


def skill_folders(local: Path) -> list[Path]:
    """The overlay's skill folders: each folder of local/skills. A loose file there is no skill."""
    skills = local / OVERLAY_SKILLS
    if not skills.is_dir():
        return []
    return [path for path in sorted(skills.iterdir()) if path.is_dir() and not NOT_PACK.fullmatch(path.name)]


def skill_names(local: Path) -> list[str]:
    return [folder.name for folder in skill_folders(local)]


def read_overlay(local: Path) -> Overlay:
    """The overlay under local (<kit>/local). ValueError for a skill folder without a lowercase name
    and a SKILL.md, a bad `hosts:` line, a link, a text that is not UTF-8, or a rules.md over
    OVERLAY_RULES_WORDS words."""
    files: PackFiles = {}
    rules = local / OVERLAY_RULES
    if rules.is_file():
        files[OVERLAY_RULES] = (rules.read_bytes(), normal_mode(rules.stat().st_mode))
    for folder in skill_folders(local):
        name = folder.name
        if not PACK_SKILL_NAME.fullmatch(name) or not (folder / 'SKILL.md').is_file():
            raise ValueError(f'overlay {local}: skills/{name} needs a lowercase name and a SKILL.md')
        for path in sorted(folder.rglob('*')):
            relative = path.relative_to(local)
            if any(NOT_PACK.fullmatch(part) for part in relative.parts):
                continue
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError(f'overlay {local}: {relative.as_posix()} is a link or special file; an overlay skill holds files')
            if path.is_dir():
                continue
            files[relative.as_posix()] = (path.read_bytes(), normal_mode(path.stat().st_mode))
    for relative, (data, _) in files.items():
        if relative.endswith('.md'):
            try:
                data.decode()
            except UnicodeDecodeError:
                raise ValueError(f'overlay {local}: {relative} is not UTF-8 text') from None
    for name in {path.split('/')[1] for path in files if path.startswith(OVERLAY_SKILLS + '/')}:
        try:
            skill_hosts(local / OVERLAY_SKILLS / name / 'SKILL.md')
        except ValueError as error:
            raise ValueError(f'overlay {local}: {error}') from None
    overlay = Overlay(local, files)
    words = len(overlay.rules.split())
    if words > OVERLAY_RULES_WORDS:
        raise ValueError(f'overlay {local}: rules.md has {words} words, over the cap of {OVERLAY_RULES_WORDS}; '
                         'move the detail into an overlay skill or a <repo>-rules.md')
    return overlay


def overrides(names: Iterable[str], kit: Iterable[str], pack: Iterable[str]) -> dict[str, str]:
    """Each overlay skill name that a pack or kit skill also has -> whose ('team pack' or 'kit')."""
    kit, pack = set(kit), set(pack)
    return {name: 'team pack' if name in pack else 'kit' for name in sorted(names) if name in pack or name in kit}


def installed_overrides(kit: Path, names: Iterable[str]) -> dict[str, str]:
    """overrides() for an installed kit: against its source checkout's skills (none when the install
    records no checkout) and the team pack's kept under <kit>/pack."""
    try:
        configuration = json.loads((kit / '.install-state/current.json').read_text()).get('configuration', {})
    except (OSError, ValueError):
        configuration = {}
    source = configuration.get('source_checkout')
    kit_names = [path.parent.name for path in Path(source).glob('skills/*/SKILL.md')] if source else []
    pack = [path.parent.name for path in (kit / PACK_DIR).glob('skills/*/SKILL.md')]
    return overrides(names, kit_names, pack)


def override_lines(clashes: dict[str, str]) -> list[str]:
    return [f'overlay skill {name} overrides the {whose} skill of that name' for name, whose in clashes.items()]


def summary(overlay: Overlay) -> str:
    """The preview line for the overlay."""
    rules = f'; rules block of {len(overlay.rules.split())} words' if overlay.rules.strip() else ''
    return f'overlay {overlay.folder}: skills {", ".join(overlay.skills) or "none"}{rules}'
