"""`agent-kit dashboard`: one self-contained, light-mode HTML page of the whole setup, generated from
the live install. Read-only apart from the page itself: it reads the kit, the host roots, the
overlay and the work root, and checks Keychain items for presence only. Every change it offers is a
command to copy; the page has no server and loads nothing from the network.

Collectors live in dashboard_sections.py, dashboard_sql.py and dashboard_unmanaged.py, the rows both
views share in dashboard_rows.py, the renderer in dashboard_html.py with its template dashboard.html,
and the credential masking in credentials.py."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

LIB = Path(__file__).resolve().parent
ENGINE = LIB.parents[1]
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(ENGINE / "hooks/lib"))
from credentials import mask_tokens  # noqa: E402
from dashboard_docs import doc_parts  # noqa: E402
from dashboard_html import Action, Command, Para, Section, Table, page  # noqa: E402
from dashboard_sections import (  # noqa: E402
    Setup,
    TableRows,
    data_section,
    hooks_section,
    hosts_section,
    mcp_section,
    overlay_section,
    pack_section,
    roles_section,
    run,
    skills_section,
    work_section,
)
from dashboard_sql import sql_section  # noqa: E402
from dashboard_unmanaged import unmanaged_section  # noqa: E402

Collector = Callable[[Setup, Section], None]
ORDER: tuple[tuple[str, str, Collector], ...] = (
    ("hosts", "Hosts", hosts_section),
    ("mcp", "MCP servers", mcp_section),
    ("unmanaged", "Unmanaged sources", unmanaged_section),
    ("overlay", "Overlay settings", overlay_section),
    ("data", "Data wrappers", data_section),
    ("sql", "SQL instances", sql_section),
    ("skills", "Skills", skills_section),
    ("roles", "Roles and agents", roles_section),
    ("hooks", "Hooks", hooks_section),
    ("pack", "Preset and pack", pack_section),
    ("work", "Work root", work_section),
)


def collect(setup: Setup, key: str, title: str, collector: Collector) -> Section:
    """The section collector fills; if it fails, the same section holding only the failure."""
    sec = Section(key, title)
    try:
        collector(setup, sec)
    except (Exception, SystemExit) as error:  # noqa: BLE001  one unreadable part must not hide the rest
        first = str(error).splitlines()[0] if str(error) else ""
        return Section(key, title, alerts=[f"not read: {type(error).__name__}: {first}"])
    return sec


def actions_section(setup: Setup, sections: list[Section]) -> Section:
    actions: list[Action] = []
    for sec in sections:
        actions += [a for a in sec.actions if a not in actions]
    chosen = ["env", f"AGENT_KIT_DIR={setup.kit}"] if "AGENT_KIT_DIR" in os.environ else []
    regenerate = shlex.join([*chosen, str(ENGINE / "bin/agent-kit"), "dashboard"])
    actions.append(Action("Regenerate this page", regenerate))
    rows: TableRows = [(a.label, Command(a.command)) for a in actions]
    return Section(
        "actions",
        "Actions",
        blocks=[
            Para(("Every change is a command to run in your own terminal; this page writes nothing.",)),
            Table(("Action", "Command"), rows),
        ],
        count=len(actions),
        summary=f"{len(actions)} commands to copy",
    )


def needs_attention(sections: list[Section]) -> list[Action]:
    """Each section's own items (drift, missing credentials with their add commands, unmanaged
    counts), then every part the page could not read."""
    out = [a for sec in sections for a in sec.attention]
    return out + [Action(f"{sec.title}: {alert}", view=sec.key) for sec in sections for alert in sec.alerts]


def build(api: ModuleType, check_updates: bool = False) -> tuple[str, int]:
    setup = Setup(api, check_updates)
    sections = [collect(setup, key, title, collector) for key, title, collector in ORDER]
    sections.append(actions_section(setup, sections))
    return mask_tokens(page(sections, needs_attention(sections), setup.kit, ENGINE, doc_parts(setup)))


def cmd_dashboard(api: ModuleType, args: argparse.Namespace) -> int:
    from kit_env import work_root

    out = Path(args.out).expanduser() if args.out else Path(work_root()) / "dashboard/index.html"
    text, masked = build(api, args.check_updates)
    out.parent.mkdir(parents=True, exist_ok=True)
    api.atomic_write(out, text, 0o600)
    print(
        f"dashboard: wrote {out}" + (f" ({masked} token-shaped value(s) masked)" if masked else "")
    )
    if not args.no_open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if shutil.which(opener):
            run([opener, str(out)])
    return 0
