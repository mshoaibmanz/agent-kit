"""The personal overlay (README "Personal overlay"): <kit>/local/skills/<name>/ and <kit>/local/rules.md,
read and installed like a team pack (pack.py) with its own rules budget. An overlay skill replaces a
kit or pack skill of the same name, and setup and both doctors say so. Claude's <config dir>/local is
a link to <kit>/local, which both doctors check."""

from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pack import Pack

# Personal rules ride in every session of every host, so they get a budget like the pack's own.
OVERLAY_RULES_WORDS = 400


def read_overlay(local: Path) -> Pack | None:
    """The overlay under local (<kit>/local) as a pack of its skills/ and rules.md, None when it has
    neither. A loose file in skills/ is no skill and is left out; a link is refused. ValueError as
    pack.build_pack refuses, with the remedy both doctors print."""
    # Here, not at the top: pack imports hosts, which imports this module.
    from pack import PACK_RULES, PACK_SKILLS, build_pack, folder_entries, pack_files

    entries = (entry for entry in folder_entries(local) if entry.path in (PACK_RULES, PACK_SKILLS) or (
        entry.path.startswith(PACK_SKILLS + '/') and not (entry.kind == 'file' and entry.path.count('/') == 1)))
    label = f'overlay {local}'
    try:
        return build_pack(pack_files(entries, label, allow_links=False), label, None, words=OVERLAY_RULES_WORDS)
    except ValueError as error:
        raise ValueError(f'{error}; fix it, then run agent-kit sync') from None


def overrides(names: Iterable[str], kit: Iterable[str], pack: Iterable[str]) -> dict[str, str]:
    """Each overlay skill name that a pack or kit skill also has -> whose ('team pack' or 'kit')."""
    kit, pack = set(kit), set(pack)
    return {name: 'team pack' if name in pack else 'kit' for name in sorted(names) if name in pack or name in kit}


def override_lines(clashes: dict[str, str]) -> list[str]:
    return [f'overlay skill {name} overrides the {whose} skill of that name' for name, whose in clashes.items()]


def summary(personal: Pack, local: Path) -> str:
    """The preview line for the overlay."""
    rules = f'; rules block of {len(personal.rules.split())} words' if personal.rules.strip() else ''
    return f'overlay {local}: skills {", ".join(personal.skills) or "none"}{rules}'


def claude_link_problem(config_root: Path, kit: Path) -> str | None:
    """Why Claude's <config_root>/local is not the overlay, None when it resolves to <kit>/local."""
    local, wanted = config_root / 'local', kit / 'local'
    if os.path.realpath(local) == os.path.realpath(wanted):
        return None
    if not (local.exists() or local.is_symlink()):
        return f'{local} is missing: agent-setup --apply links it to {wanted}'
    what = f'a link to {os.readlink(local)}' if local.is_symlink() else 'a folder' if local.is_dir() else 'a file'
    return (f'{local} is {what}, not the link to {wanted}: move its files into {wanted}, remove it '
            '(for a link, only the link) and rerun agent-setup --apply')
