"""`agent-kit dashboard`: one self-contained, light-mode HTML page of the whole setup, generated from
the live install. Read-only apart from the page itself: it reads the kit, the host roots, the
overlay and the work root, and checks Keychain items for presence only. Every change it offers is a
command to copy; the page has no server and loads nothing from the network.

Collectors live in dashboard_sections.py and dashboard_unmanaged.py, the renderer in
dashboard_html.py with its template dashboard.html, and the credential masking in credentials.py."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

LIB = Path(__file__).resolve().parent
ENGINE = LIB.parents[1]
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(ENGINE / "hooks/lib"))
from credentials import mask_tokens  # noqa: E402
from dashboard_html import Action, Command, Para, Section, Table, Tile, page  # noqa: E402
from dashboard_sections import (  # noqa: E402
    Setup,
    data_section,
    hooks_section,
    hosts_section,
    keychain_add,
    mcp_section,
    overlay_section,
    pack_section,
    roles_section,
    run,
    skills_section,
    work_section,
)
from dashboard_unmanaged import unmanaged_section  # noqa: E402

ORDER: tuple[tuple[str, str, Callable[[Setup], Section]], ...] = (
    ("hosts", "Hosts", hosts_section),
    ("mcp", "MCP servers", mcp_section),
    ("unmanaged", "Unmanaged sources", unmanaged_section),
    ("overlay", "Overlay settings", overlay_section),
    ("data", "Data wrappers", data_section),
    ("skills", "Skills", skills_section),
    ("roles", "Roles and agents", roles_section),
    ("hooks", "Hooks", hooks_section),
    ("pack", "Preset and pack", pack_section),
    ("work", "Work root", work_section),
)


def collect(setup: Setup, key: str, title: str, collector: Callable[[Setup], Section]) -> Section:
    try:
        return collector(setup)
    except (Exception, SystemExit) as error:  # noqa: BLE001  one unreadable part must not hide the rest
        first = str(error).splitlines()[0] if str(error) else ""
        return Section(key, title, alerts=[f"not read: {type(error).__name__}: {first}"])


def actions_section(setup: Setup, sections: list[Section]) -> Section:
    actions: list[Action] = []
    for sec in sections:
        actions += [a for a in sec.actions if a not in actions]
    for service, account, why in setup.missing:
        actions.append(
            Action(
                f"Add the Keychain item for {why} (prompts for the value)",
                keychain_add(service, account),
            )
        )
    chosen = ["env", f"AGENT_KIT_DIR={setup.kit}"] if "AGENT_KIT_DIR" in os.environ else []
    actions.append(
        Action(
            "Regenerate this page",
            shlex.join([*chosen, str(ENGINE / "bin/agent-kit"), "dashboard"]),
        )
    )
    missing = len(setup.missing)
    return Section(
        "actions",
        "Actions",
        blocks=[
            Para(
                (
                    "Every change is a command to run in your own terminal; this page writes nothing.",
                )
            ),
            Table(("Action", "Command"), [(a.label, Command(a.command)) for a in actions]),
        ],
        tile=Tile(
            "Credentials",
            f"{missing} missing" if missing else "none missing",
            "presence only",
            "bad" if missing else "",
        ),
    )


def needs_attention(setup: Setup, sections: list[Section]) -> list[Action]:
    """Drift per host, each missing credential with its add command, unmanaged counts, and every
    part the page could not read."""
    out = [a for sec in sections if sec.key == "hosts" for a in sec.attention]
    out += [
        Action(
            f"Missing Keychain item {service} / {account} for {why}", keychain_add(service, account)
        )
        for service, account, why in setup.missing
    ]
    out += [a for sec in sections if sec.key != "hosts" for a in sec.attention]
    return out + [Action(f"{sec.title}: {alert}") for sec in sections for alert in sec.alerts]


def build(api: ModuleType, check_updates: bool = False) -> tuple[str, int]:
    setup = Setup(api, check_updates)
    sections = [collect(setup, key, title, collector) for key, title, collector in ORDER]
    sections.append(actions_section(setup, sections))
    return mask_tokens(page(sections, needs_attention(setup, sections), setup.kit, ENGINE))


def cmd_dashboard(api: ModuleType, args: Any) -> int:
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
