"""The dashboard's Unmanaged sources section: what changes agent behaviour without the kit owning it.
Names and paths only; no value from these files reaches the page."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from dashboard_html import Action, Fold, Para, Section, Strong, Table
from dashboard_sections import ENGINE, Setup, TableRows, read_json
from hosts import RENDERED_FILES, claude_state_file
from kit_env import kit_env

# Per host: the config-root entries that change behaviour, and why. A render writes RENDERED_FILES;
# anything else here comes from the user, the host or another tool.
BEHAVIOUR = {
    "claude": {
        "settings.local.json": "overrides settings.json on this host; the kit never reads or renders it",
        "agents": "agent files load as subagents",
        "commands": "slash commands",
        "skills": "skills load in every session",
        "hooks": "hook scripts",
        "rules": "rule files the host loads",
        "output-styles": "output styles change how the model writes",
        "keybindings.json": "key bindings",
    },
    "codex": {
        "AGENTS.override.md": "replaces AGENTS.md, the kit's rules, on this host",
        "agents": "agent files load as subagents",
        "skills": "skills load in every session",
        "rules": "rule files the host loads",
        "prompts": "custom prompts",
    },
    "cursor": {
        "agents": "agent files load as subagents",
        "skills": "skills load in every session",
        "skills-cursor": "skills load in every session",
        "rules": "rule files the host loads",
    },
}
REPO_MARKERS = (".claude", "AGENTS.md", "CLAUDE.md", ".cursor/rules", ".codex")
REPO_SCAN_MAX = 3000
GROUP_OVER = 5  # more unowned entries than this in one folder show as one row
PLUGIN_WHY = "a marketplace plugin: its skills, hooks, agents and MCP servers load beside the kit's"


def inside(path: Path, folder: Path) -> bool:
    """path is in folder, or links into it: a kit skill may itself link on to a folder elsewhere."""
    try:
        if path.resolve().is_relative_to(folder.resolve()):
            return True
        if path.is_symlink():
            target = Path(os.path.normpath(path.parent / os.readlink(path)))
            return target.is_relative_to(Path(os.path.normpath(folder.absolute())))
    except OSError:
        return False
    return False


def kit_owned(setup: Setup, host: str, path: Path, managed: set[Path]) -> bool:
    root = setup.roots[host]
    relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else ""
    if relative in RENDERED_FILES[host] or path in managed or inside(path, setup.kit):
        return True
    # An agent file rendered from roles.toml.
    return (
        path.parent.name == "agents" and setup.roles is not None and path.stem in setup.roles.roles
    )


def host_extras(setup: Setup, host: str) -> TableRows:
    root, rows = setup.roots[host], []
    managed = {
        Path(r["target"])
        for r in setup.state.get("managed", {}).values()
        if isinstance(r, dict) and "target" in r
    }
    if not root.is_dir():
        return rows
    for name, why in BEHAVIOUR[host].items():
        entry = root / name
        if not (entry.exists() or entry.is_symlink()) or kit_owned(setup, host, entry, managed):
            continue
        children = sorted(entry.iterdir()) if entry.is_dir() and not entry.is_symlink() else [entry]
        found = [
            c
            for c in children
            if not c.name.startswith(".") and not kit_owned(setup, host, c, managed)
        ]
        if len(found) > GROUP_OVER:
            names = ", ".join(c.name for c in found)
            rows.append(
                (
                    host,
                    entry,
                    (f"{len(found)} entries the kit does not own: {why}", Fold("Names", (names,))),
                )
            )
            continue
        for child in found:
            note = f"link to {os.readlink(child)}, outside the kit; " if child.is_symlink() else ""
            rows.append((host, child, note + why))
    for entry in sorted(root.iterdir()):
        if (
            entry.is_symlink()
            and entry.name not in BEHAVIOUR[host]
            and not kit_owned(setup, host, entry, managed)
        ):
            rows.append((host, entry, f"link to {os.readlink(entry)}, outside the kit"))
    return rows


def plugin_rows(setup: Setup) -> TableRows:
    rows: TableRows = []
    if "claude" in setup.configured:
        root = setup.roots["claude"]
        listing = root / "plugins/installed_plugins.json"
        installed = (read_json(listing) or {}).get("plugins", {})
        enabled = (read_json(root / "settings.json") or {}).get("enabledPlugins", {})
        for name, installs in sorted(installed.items() if isinstance(installed, dict) else []):
            versions = (
                sorted({str(i.get("version", "?")) for i in installs if isinstance(i, dict)})
                if isinstance(installs, list)
                else []
            )
            on = isinstance(enabled, dict) and enabled.get(name)
            rows.append(
                (
                    "claude",
                    (Strong(name), ", ".join(versions)),
                    "enabled" if on else "installed, not enabled",
                    listing,
                    PLUGIN_WHY,
                )
            )
    if "codex" in setup.configured:
        config = setup.roots["codex"] / "config.toml"
        try:
            plugins = tomllib.loads(config.read_text()).get("plugins", {})
        except (OSError, ValueError):
            plugins = {}
        for name, spec in sorted(plugins.items() if isinstance(plugins, dict) else []):
            on = not isinstance(spec, dict) or spec.get("enabled", True)
            rows.append(
                ("codex", Strong(name), "enabled" if on else "disabled", config, PLUGIN_WHY)
            )
    if "cursor" in setup.configured:
        local = setup.roots["cursor"] / "plugins/local"
        for entry in sorted(local.iterdir()) if local.is_dir() else []:
            if not entry.name.startswith("."):
                rows.append(("cursor", Strong(entry.name), "installed", entry, PLUGIN_WHY))
    return rows


def claude_mcp_rows(setup: Setup) -> TableRows:
    """Servers Claude Code keeps in its own state file (`claude mcp add`, user and project scope)
    and the claude.ai connectors it has seen: names only, never their settings."""
    if "claude" not in setup.configured:
        return []
    state = claude_state_file(setup.roots["claude"])
    data = read_json(state)
    if not isinstance(data, dict):
        return []
    rows: TableRows = []
    servers = data.get("mcpServers")
    for name in sorted(servers if isinstance(servers, dict) else {}):
        same = "; the catalog has one of the same name" if name in setup.catalog else ""
        rows.append(
            (
                Strong(name),
                "user-scope MCP server in Claude's state file",
                state,
                "loads in every session beside the kit's mcp.json" + same,
            )
        )
    projects = data.get("projects")
    for folder, spec in sorted(projects.items() if isinstance(projects, dict) else []):
        names = (
            sorted(spec["mcpServers"])
            if isinstance(spec, dict) and isinstance(spec.get("mcpServers"), dict)
            else []
        )
        if names:
            rows.append(
                (
                    Strong(", ".join(names)),
                    f"project-scope MCP servers for {folder}",
                    state,
                    "load in sessions started in that folder",
                )
            )
    connectors = data.get("claudeAiMcpEverConnected")
    for name in connectors if isinstance(connectors, list) else []:
        rows.append(
            (
                Strong(str(name)),
                "claude.ai connector",
                state,
                "connected in the claude.ai account; its tools reach every session",
            )
        )
    return rows


def repo_roots(setup: Setup) -> list[Path]:
    roots = setup.config.get("repo_roots") or []
    if not roots:
        values = kit_env()
        try:
            roots = json.loads(values.get("CODE_DIRS_JSON") or "[]")
        except ValueError:
            roots = []
        roots = roots or (values.get("CODE_DIRS") or "").split()
    return [Path(os.path.expanduser(str(r))) for r in roots if str(r).strip()]


def repo_rows(setup: Setup) -> tuple[TableRows, bool]:
    """Repositories under the repo roots, two levels deep, that carry their own agent config."""
    rows: TableRows = []
    seen = 0
    for root in repo_roots(setup):
        if not root.is_dir():
            continue
        level = [root]
        for _ in range(2):
            following = []
            for folder in level:
                try:
                    children = sorted(
                        c for c in folder.iterdir() if c.is_dir() and not c.name.startswith(".")
                    )
                except OSError:
                    continue
                for child in children:
                    seen += 1
                    if seen > REPO_SCAN_MAX:
                        return rows, True
                    found = [m for m in REPO_MARKERS if (child / m).exists()]
                    if found:
                        local = (
                            ["settings.local.json"]
                            if (child / ".claude/settings.local.json").exists()
                            else []
                        )
                        rows.append(
                            (
                                child,
                                ", ".join(found + local),
                                "the repo's own instructions and settings load in sessions started there",
                            )
                        )
                    following.append(child)
            level = following
    return rows, False


def path_rows(setup: Setup) -> TableRows:
    """Kit commands found on PATH that are not the kit's own copy."""
    names = sorted(
        {
            p.name
            for folder in (ENGINE / "bin", setup.kit / "bin")
            if folder.is_dir()
            for p in folder.iterdir()
            if p.is_file() and not p.name.startswith(".")
        }
    )
    rows: TableRows = []
    for folder in dict.fromkeys(os.environ.get("PATH", "").split(os.pathsep)):
        if not folder or not Path(folder).is_dir():
            continue
        for name in names:
            found = Path(folder) / name
            if not (found.is_file() and os.access(found, os.X_OK)):
                continue
            if inside(found, setup.kit) or inside(found, ENGINE):
                continue
            kit_copy = setup.kit_bin(name)
            same = kit_copy.is_file() and found.read_bytes() == kit_copy.read_bytes()
            rows.append(
                (
                    Strong(name),
                    found,
                    ("same content as" if same else "DIFFERS from")
                    + " the kit's copy; a call by name may run it",
                )
            )
    return rows


def unmanaged_section(setup: Setup, sec: Section) -> None:
    extras = [r for host in setup.configured for r in host_extras(setup, host)]
    repos, truncated = repo_rows(setup)
    tables = [
        Table(
            ("Host", "Path", "Why it matters"),
            extras,
            "Host config files outside the install",
            anchor="unmanaged-host-files",
        ),
        Table(
            ("Host", "Plugin", "State", "Recorded in", "Why it matters"),
            plugin_rows(setup),
            "Marketplace plugins",
            anchor="unmanaged-plugins",
        ),
        Table(
            ("Name", "What", "Recorded in", "Why it matters"),
            claude_mcp_rows(setup),
            "Host-kept MCP servers and connectors",
            anchor="unmanaged-host-mcp",
        ),
        Table(
            ("Repository", "Found", "Why it matters"),
            repos,
            "Repositories with their own agent config",
            "None under the repo roots, or no repo roots set.",
            f"Scan stopped after {REPO_SCAN_MAX} folders." if truncated else "",
            anchor="unmanaged-repos",
        ),
        Table(
            ("Command", "Path", "Why it matters"),
            path_rows(setup),
            "Kit commands elsewhere on PATH",
            anchor="unmanaged-path",
        ),
    ]
    sec.blocks.append(
        Para(
            (
                "Everything here changes agent behaviour but the kit does not own it. "
                "Names and paths only.",
            )
        )
    )
    sec.blocks += tables
    total = 0
    for table in tables:
        if table.rows:
            total += len(table.rows)
            sec.attention.append(
                Action(f"Unmanaged: {len(table.rows)} in {table.title.lower()}", "", sec.key, table.anchor)
            )
    sec.count = total
    sec.summary = f"{total} sources outside the kit"
    sec.level = "warn" if total else ""
