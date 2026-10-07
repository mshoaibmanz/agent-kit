"""The dashboard's collectors: each fills the Section collect() made for it with plain values
(dashboard_html renders them). They read only; a credential is checked for presence, never read."""

from __future__ import annotations

import datetime as dt
import functools
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

from blocking import BLOCKING, hook_name
from credentials import Credential, credential_name, declared_credentials, holds_secret, show_args, show_url
from dashboard_html import (
    Action,
    Badge,
    Cell,
    Code,
    Command,
    Fold,
    Lines,
    Muted,
    Para,
    Pre,
    Row,
    Section,
    State,
    Strong,
    Table,
    anchor,
)
from hosts import (
    HOSTS,
    RULES_FILES,
    account_dirs,
    default_host_root,
    fill_servers,
    frontmatter,
    host_root_for,
    inventory,
    skill_dirs,
    skill_hosts,
)
from kit_env import KEYS, kit_env, layers, parse
from mcp_describe import Described
from mcp_describe import read_cache as read_described

LIB = Path(__file__).resolve().parent
ENGINE = LIB.parents[1]
# The presence check runs this binary, never one from PATH; tests point it at a fixture.
SECURITY = os.environ.get("AGENT_KIT_SECURITY", "/usr/bin/security")
TableRows = list[Row | tuple[Cell, ...]]


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def when(stamp: float | None) -> str:
    return "never" if stamp is None else dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def run(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


@functools.cache
def keychain_present(item: Credential) -> State:
    """present, missing, or unknown (no Keychain tool, or it failed to run). No -w or -g: the item's
    attributes are found, its secret is never asked for, and the output is discarded."""
    if not Path(SECURITY).exists():
        return "unknown"
    proc = run([SECURITY, "find-generic-password", "-s", item.service, "-a", item.account])
    if proc is None:
        return "unknown"
    return "present" if proc.returncode == 0 else "missing"


def keychain_add(item: Credential) -> str:
    """The prompt form: -w last with no value, so the password is typed, never on a command line."""
    return shlex.join(["security", "add-generic-password", "-s", item.service, "-a", item.account, "-w"])


def kit_command(setup: Setup, *args: str) -> str:
    return shlex.join([str(setup.kit / "bin/agent-kit"), *args])


def setup_command(setup: Setup, *args: str, action: str = "") -> str:
    """agent-setup from the install's source checkout, for this kit's root (not the default one)."""
    source = setup.source()
    head = [str(source / "bin/agent-setup"), *([action] if action else [])]
    return shlex.join([*head, "--source", str(source), "--root-dir", str(setup.kit), *args])


def load_script(path: Path, name: str) -> ModuleType | None:
    """A kit bin script as a module, without writing bytecode beside it."""
    if not path.is_file():
        return None
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        return None
    module = importlib.util.module_from_spec(spec)
    dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules[name] = module  # a dataclass in the script looks its module up there
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
    return module


class Setup:
    """What every collector reads, once: the kit, its install record and each host's root."""

    def __init__(self, api: ModuleType, check_updates: bool) -> None:
        self.api = api
        self.kit = Path(api.KIT)
        self.check_updates = check_updates
        self.state: dict[str, Any] = read_json(self.kit / ".install-state/current.json") or {}
        self.config: dict[str, Any] = self.state.get("configuration", {})
        self.roots = {h: host_root_for(self.kit, h) or default_host_root(h) for h in HOSTS}
        if self.state:
            self.configured = [h for h in HOSTS if h in self.config.get("hosts", [])]
        else:
            self.configured = [h for h in HOSTS if self.roots[h].is_dir()]
        self.catalog_path = self.kit / "mcp/servers.json"
        chosen = self.config.get("mcp_catalog")
        self.catalog_input = Path(chosen) if chosen else None
        catalog = (read_json(self.catalog_path) or {}).get("mcpServers", {})
        self.catalog: dict[str, Any] = catalog if isinstance(catalog, dict) else {}
        self.fill_error = ""
        try:
            self.filled = fill_servers(self.catalog, str(self.kit))
        except (ValueError, SystemExit) as error:
            self.fill_error = str(error)
            self.filled = self.catalog
        self.roles = None
        self.roles_error = ""
        try:
            self.roles = api.load_roles()
        except SystemExit as error:
            self.roles_error = str(error)
        self.inventories: dict[str, dict[str, Any]] = {}

    def kit_bin(self, name: str) -> Path:
        """The kit's own copy of a bin script, else the engine's."""
        own = self.kit / "bin" / name
        return own if own.is_file() else ENGINE / "bin" / name

    def source(self) -> Path:
        """The checkout agent-setup installed from, else the kit itself."""
        source = self.config.get("source_checkout")
        return Path(source) if source else self.kit

    def inventory(self, host: str) -> dict[str, Any]:
        if host not in self.inventories:
            self.inventories[host] = inventory(self.api, host)
        return self.inventories[host]

    def live_servers(self, host: str) -> set[str] | None:
        """Server names only (hosts.inventory): the live files hold resolved credentials."""
        try:
            return set(self.inventory(host)["mcp_servers"])
        except (OSError, ValueError, SystemExit):
            return None

    @functools.cached_property
    def setup_drift(self) -> list[Path]:
        """Paths agent-setup installed that changed since (its own managed_drift)."""
        module = load_script(self.kit_bin("agent-setup"), "agent_setup_for_dashboard") if self.state else None
        if module is None:
            return []
        return [Path(p) for p in module.managed_drift(self.state.get("managed", {}).values())]


def credential_cell(sec: Section, item: Credential, why: str, row: str) -> tuple[Cell, ...]:
    """The item's presence; a missing one adds its add command to the section's Needs attention."""
    state = keychain_present(item)
    if state == "missing":
        action = Action(
            f"Missing Keychain item {item.service} / {item.account} for {why}",
            keychain_add(item),
            sec.key,
            row,
        )
        if action not in sec.attention:
            sec.attention.append(action)
    command: Cell = Command(keychain_add(item)) if state == "missing" else ""
    return (f"Keychain {item.service} / {item.account}", Badge.state(state), command)


def last_render(setup: Setup, host: str) -> float | None:
    rendered = setup.kit / "state/rendered"
    stamps = [mtime(p) for p in rendered.glob(f"{host}-*")] + [mtime(rendered / f"roles-{host}.sh")]
    return max((s for s in stamps if s is not None), default=None)


def host_drift(setup: Setup, host: str) -> list[tuple[str, str]]:
    """(kind, line) per way the host differs. claude: agent-kit doctor's own findings with MCP
    compared by name (no Keychain value is resolved). Others: hosts.inventory's errors and duplicate
    hooks, and MCP names. Both: the host's files agent-setup installed that changed since."""
    root = setup.roots[host]
    out = [("setup", f"changed since setup: {p}") for p in setup.setup_drift if p.is_relative_to(root)]
    if host == "claude":
        return [(f.kind, f.line) for f in setup.api.claude_findings(mcp="names")] + out
    inv = setup.inventory(host)
    out += [("inventory", str(e)) for e in inv["errors"]]
    out += [
        ("inventory", f"duplicate hook {d['event']} {d['matcher'] or ''} x{d['count']}")
        for d in inv["duplicate_hooks"]
    ]
    live, want = set(inv["mcp_servers"]), set(setup.catalog)
    out += [("mcp", f"mcp {n} in the catalog, not live") for n in sorted(want - live)]
    return out + [("mcp", f"mcp {n} live only, not in the catalog") for n in sorted(live - want)]


def host_fix(setup: Setup, host: str, kinds: set[str]) -> tuple[str, str]:
    """(why, command) that brings host back to the kit's version."""
    if setup.state:
        return (
            "agent-setup wrote these files, so re-apply the install; `agent-kit render` would refuse "
            "them (backup keeps your version in the install journal)",
            setup_command(setup, "--apply", "--collision", "backup"),
        )
    flags = [*(["--all"] if "settings" in kinds else []), *(["--force-mcp"] if "mcp" in kinds else [])]
    flags = flags if host == "claude" else []
    why = "Render writes the kit's sources over the drift"
    if flags:
        why += f" (without {' '.join(flags)} it refuses); `agent-kit adopt <key>` keeps a live setting"
    return why, kit_command(setup, "render", "--host", host, *flags)


def hosts_section(setup: Setup, sec: Section) -> None:
    sec.sources = [setup.kit / ".install-state/current.json"] if setup.state else []
    rows: TableRows = []
    drifted = 0
    for host in HOSTS:
        root, on = setup.roots[host], host in setup.configured
        drift: list[tuple[str, str]] = []
        if on:
            try:
                drift = host_drift(setup, host)
            except (Exception, SystemExit) as error:  # noqa: BLE001  one host's failure is one line
                first = str(error).splitlines()[0] if str(error) else ""
                drift = [("error", f"not checked: {type(error).__name__}: {first}")]
        lines = [line for _, line in drift]
        drifted += bool(drift)
        detail: Cell = Badge.state("clean") if on else Muted("-")
        row = anchor("host", host)
        if drift:
            detail = Fold(Badge(f"{len(drift)} drift", "warn"), (Lines(tuple(lines)),))
            kinds = {kind for kind, _ in drift}
            more = ", ..." if len(drift) > 1 else ""
            if "error" in kinds:
                # Not checked, so nothing to fix: re-applying or rendering would not touch the cause.
                what = f"{host}: {lines[0]}. Doctor shows the whole error"
                command = kit_command(setup, "doctor", *([] if host == "claude" else ["--host", host]))
            else:
                why, command = host_fix(setup, host, kinds)
                what = f"{host}: {len(drift)} drift ({lines[0]}{more}). {why}"
            sec.attention.append(Action(what, command, sec.key, row))
        rows.append(
            Row(
                (
                    Strong(host),
                    Badge("configured", "ok") if on else Badge("not configured"),
                    root,
                    root / RULES_FILES[host],
                    when(last_render(setup, host)),
                    detail,
                ),
                row,
            )
        )
        if on and not setup.state:
            sec.actions.append(Action(f"Render {host}", kit_command(setup, "render", "--host", host)))
    roots = [setup.roots[h] for h in setup.configured]
    elsewhere = [p for p in setup.setup_drift if not any(p.is_relative_to(r) for r in roots)]
    if elsewhere:
        _, command = host_fix(setup, "", set())
        sec.attention.append(
            Action(f"{len(elsewhere)} installed path(s) changed since setup: {elsewhere[0]}", command, sec.key)
        )
    sec.actions.append(Action("Check every host", kit_command(setup, "doctor", "--host", "all")))
    if setup.state:
        sec.actions.append(Action("Re-apply the install", setup_command(setup, "--apply")))
        sec.actions.append(Action("Check the install", setup_command(setup, action="doctor")))
    sec.blocks.append(
        Table(("Host", "State", "Config dir", "Rules file", "Last render", "Drift"), rows)
    )
    claude = setup.roots["claude"].resolve()
    extra = [d for d in account_dirs(setup.kit_bin("claude-account")) if d.resolve() != claude]
    if extra:
        accounts: TableRows = []
        for d in extra:
            settings = d / "settings.json"
            linked = f"link to {os.readlink(settings)}" if settings.is_symlink() else None
            skills = len(list((d / "skills").iterdir())) if (d / "skills").is_dir() else 0
            accounts.append((d, linked or ("own file" if settings.exists() else "missing"), skills))
        sec.blocks.append(Table(("Config dir", "settings.json", "Skills"), accounts, "Claude accounts"))
    sec.count = len(setup.configured)
    sec.summary = f"{len(setup.configured)} configured, " + (
        f"{drifted} with drift" if drifted else "no drift"
    )
    sec.level = "warn" if drifted else ""


def described(entry: Described | None) -> tuple[Cell, str]:
    """(the Tools cell, a fallback description) from one server's `agent-kit mcp describe` entry."""
    if entry is None:
        return Muted("not described"), ""
    if entry.status != "ok":
        return Badge(entry.status, "warn"), ""
    names = tuple(name for name, _ in entry.tools)
    cell: Cell = Fold(f"{len(names)} tools", (Lines(names),)) if names else "0 tools"
    shown: Cell = (cell, Muted(f"{entry.server}, checked {entry.checked or '?'}")) if entry.server else cell
    return shown, entry.instructions or entry.server


def mcp_section(setup: Setup, sec: Section) -> None:
    sec.sources = [setup.catalog_path] + ([setup.catalog_input] if setup.catalog_input else [])
    if setup.fill_error:
        sec.alerts.append(f"MCP catalog placeholders not filled: {setup.fill_error}")
    live = {h: setup.live_servers(h) for h in setup.configured}
    cache_file, cache = read_described(setup.kit)
    if cache:
        sec.sources.append(cache_file)
    rows: TableRows = []
    for name in sorted(setup.filled):
        spec = setup.filled[name] if isinstance(setup.filled[name], dict) else {}
        raw = setup.catalog.get(name) if isinstance(setup.catalog.get(name), dict) else {}
        tools, fallback = described(cache.get(name))
        description = str(raw.get("description") or "") or fallback
        if "url" in spec:
            transport, target = "http", show_url(str(spec["url"])).text
        else:
            transport = "stdio"
            target = show_args([spec.get("command", ""), *(spec.get("args") or [])]).text
        env: dict[str, object] = spec["env"] if isinstance(spec.get("env"), dict) else {}
        headers: dict[str, object] = spec["headers"] if isinstance(spec.get("headers"), dict) else {}
        row = anchor("mcp", name)
        creds: list[Cell] = [
            credential_cell(sec, item, f"MCP {name}", row)
            for item in declared_credentials(setup.catalog.get(name))
        ]
        inline = [
            k
            for k, v in {**env, **headers}.items()
            if credential_name(k) and not declared_credentials(v)
        ]
        if inline:
            creds.append(f"Inline in the catalog: {', '.join(sorted(inline))} (values not shown)")
        if not creds and transport == "http" and not headers:
            creds.append("OAuth: the host signs in (not checked)")
        on = tuple(
            Badge(h, "ok" if name in (live[h] or set()) else "bad") for h in setup.configured
        )
        rows.append(
            Row(
                (
                    Strong(name),
                    transport,
                    Code(target),
                    ", ".join(sorted({*env, *headers})) or "-",
                    Lines(tuple(creds)) if creds else "None",
                    on or "-",
                    tools,
                ),
                row,
                description,
            )
        )
    sec.blocks.append(
        Table(
            ("Server", "Transport", "Command / URL", "Env / header names", "Credential", "Live on", "Tools"),
            rows,
            empty="The installed catalog has no servers.",
        )
    )
    sec.actions.append(
        Action(
            "Ask each MCP server for its tools and instructions (starts each once; never automatic)",
            kit_command(setup, "mcp", "describe"),
        )
    )
    others: TableRows = [
        (h, Strong(n), "Added outside the kit catalog (values not shown)")
        for h, names in live.items()
        for n in sorted((names or set()) - set(setup.filled))
    ]
    sec.blocks.append(Table(("Host", "Server", "Note"), others, "Live servers outside the catalog"))
    example = {
        "<name>": {
            "command": "{{KIT_DIR}}/bin/<wrapper>",
            "args": [],
            "credentials": [{"service": "<service>", "account": "<account>"}],
        }
    }
    sec.blocks += [
        Para(
            (
                "Add a server to the catalog the install was made from, keep its credential in a Keychain "
                "item its wrapper reads, declare that item under credentials (names only; the render "
                "drops the field), then apply it (Actions has the command).",
            )
        ),
        Pre(json.dumps(example, indent=2)),
    ]
    if setup.state:
        sec.actions.append(Action("Apply the catalog to every host", setup_command(setup, "--apply")))
    else:
        for host in setup.configured:
            sec.actions.append(Action(f"Render {host}", kit_command(setup, "render", "--host", host)))
    sec.count = len(setup.filled)
    sec.summary = f"{len(setup.filled)} in the catalog, " + (
        f"{len(others)} outside it" if others else "none outside it"
    )
    sec.level = "warn" if others or setup.fill_error else ""


def skills_section(setup: Setup, sec: Section) -> None:
    """Every skill the source checkout ships (and the pack's), per configured host: linked, off, or
    left out by its own hosts: line."""
    source = setup.source()
    pack = {p.parent.name: p for p in sorted((setup.kit / "pack/skills").glob("*/SKILL.md"))}
    found = {p.parent.name: p for p in sorted((source / "skills").glob("*/SKILL.md"))}
    found.update({name: path for name, path in pack.items() if name not in found})
    selected = setup.config.get("skills")
    if selected is not None:
        selected = [n for n in selected if n not in pack]
    sec.sources = [source / "skills"]
    rows: TableRows = []
    for name, md in sorted(found.items()):
        text = md.read_text(errors="replace")
        try:
            allowed = skill_hosts(md, text)
            allowed_text = ", ".join(sorted(allowed))
        except ValueError:
            allowed, allowed_text = set(), "invalid hosts: line"
        chips: list[Cell] = []
        changes: list[Cell] = []
        for host in setup.configured:
            if host not in allowed:
                chips.append(Badge(host, "off"))
                changes.append(Muted(f"{host}: left out by its hosts: line"))
                continue
            on = any((d / name).exists() for d in skill_dirs(host, setup.roots[host]))
            chips.append(Badge(host, "ok" if on else "off"))
            change = skill_toggle(setup, host, name, on, selected, name in pack)
            if change not in changes:
                changes.append(change)
        desc = frontmatter(text).get("description", "")
        rows.append(
            Row(
                (
                    Strong(name),
                    Badge("pack" if name in pack else "kit"),
                    tuple(chips) or "-",
                    Fold("Change", (Lines(tuple(changes)),)) if changes else "-",
                    allowed_text,
                    md,
                ),
                anchor("skills", name),
                desc,
            )
        )
    sec.blocks.append(Table(("Skill", "Source", "Hosts", "Change", "hosts:", "File"), rows))
    sec.blocks.append(
        Para((Muted("Skills made by hand in a host folder are listed under Unmanaged sources."),))
    )
    sec.count = len(found)
    sec.summary = f"{len(found)} skills, {len(pack)} from the pack"


def skill_toggle(
    setup: Setup, host: str, name: str, on: bool, selected: list[str] | None, in_pack: bool
) -> Cell:
    """What changes the skill on host: a command with its label, or a note. A setup selection is
    one command for every host."""
    link = setup.roots[host] / "skills" / name
    if on and not (link.exists() or link.is_symlink()):
        return Muted(f"{host}: read from the shared skills folder")
    if in_pack:
        return Muted("The team pack's; change it in the preset")
    if setup.config.get("source_checkout"):
        if selected is None:
            if on:
                return Muted(
                    "The saved selection is every skill: to drop one, re-run agent-setup "
                    "with --skills naming the ones to keep"
                )
            return ("Re-apply:", Command(setup_command(setup, "--apply")))
        names = [n for n in selected if n != name] if on else [*selected, name]
        label = "Drop it from every host:" if on else "Select it:"
        return (label, Command(setup_command(setup, "--skills", *names, "--apply")))
    if on:
        return (f"{host}: disable", Command(shlex.join(["rm", str(link)])))
    target = str(setup.kit / "skills" / name)
    return (f"{host}: enable", Command(shlex.join(["ln", "-s", target, str(link)])))


def roles_section(setup: Setup, sec: Section) -> None:
    roles, roles_file = setup.roles, Path(setup.api.ROLES)
    sec.sources = [roles_file, setup.kit / "agents"]
    if roles is None:
        sec.blocks.append(Para((Muted(setup.roles_error or "This kit has no roles.toml."),)))
        if setup.roles_error:
            sec.alerts.append(setup.roles_error)
        return
    hosts = [h for h in roles.hosts if h in HOSTS]
    rows: TableRows = []
    for name, role in roles.roles.items():
        src = setup.kit / "agents" / f"{name}.md"
        cells: list[Cell] = []
        for host in hosts:
            try:
                cells.append(f"{host}: {roles.invoke(role, host)}")
            except SystemExit:
                cells.append(f"{host}: ?")
        rendered = setup.roots["claude"] / "agents" / f"{name}.md"
        description = ""
        if src.is_file():
            description = str(frontmatter(src.read_text(errors="replace")).get("description", ""))
        rows.append(
            Row(
                (
                    Strong(name),
                    f"{role.provider}:{role.model}",
                    role.effort,
                    role.prefix or "-",
                    role.fallback or "-",
                    Lines(tuple(cells)),
                    src if src.is_file() else Muted("None (main)"),
                    Badge("rendered", "ok") if rendered.is_file() else Muted("-"),
                ),
                anchor("roles", name),
                description,
            )
        )
    sec.blocks.append(
        Table(
            (
                "Role",
                "Model",
                "Effort",
                "Review prefix",
                "Fallback",
                "Runs as",
                "Agent file",
                "Claude agent",
            ),
            rows,
        )
    )
    rounds: TableRows = [(k, ", ".join(v)) for k, v in roles.review.items()]
    sec.blocks.append(Table(("Round", "Roles"), rounds, "Review rounds"))
    reviewers = sum(bool(r.prefix) for r in roles.roles.values())
    sec.count = len(roles.roles)
    sec.summary = f"{len(roles.roles)} roles, {reviewers} of them review roles"


def hooks_section(setup: Setup, sec: Section) -> None:
    registry_path = setup.kit / "hooks/registry.json"
    registry = read_json(registry_path) or []
    sec.sources = [registry_path]
    rows: TableRows = []
    blocking = 0
    ids: Counter[str] = Counter()
    for entry in registry if isinstance(registry, list) else []:
        command = str(entry.get("command", ""))
        name = hook_name(command)
        script = setup.kit / "hooks" / name
        blocking += name in BLOCKING
        row = anchor("hooks", f"{entry.get('event', '')}-{name}")
        ids[row] += 1
        row += f"-{ids[row]}" if ids[row] > 1 else ""
        rows.append(
            Row(
                (
                    Strong(name),
                    entry.get("event", ""),
                    entry.get("matcher") or "*",
                    Badge("blocking", "warn") if name in BLOCKING else Badge("advisory"),
                    tuple(Badge(h) for h in entry.get("hosts", [])),
                    f"{entry.get('timeout', '-')}{' async' if entry.get('async') else ''}",
                    script if script.is_file() else Code(command),
                ),
                row,
                str(entry.get("description", "")),
            )
        )
    if setup.state:
        state = "on" if setup.config.get("blocking_hooks") else "off"
        sec.blocks.append(
            Para(("Blocking hooks in this install:", Strong(state), "(agent-setup --blocking-hooks)."))
        )
    sec.blocks.append(
        Table(("Hook", "Event", "Matcher", "Kind", "Hosts", "Timeout", "Script"), rows)
    )
    sec.count = len(rows)
    sec.summary = f"{len(rows)} hooks, {blocking} blocking"


def documented_keys(setup: Setup) -> dict[str, str]:
    """The keys kit.env.example documents (the overlay's settings, none of them a credential), each
    with the comment lines right above it as its description."""
    for example in (setup.kit / "kit.env.example", ENGINE / "kit.env.example"):
        try:
            text = example.read_text()
        except OSError:
            continue
        keys: dict[str, str] = {}
        comment: list[str] = []
        for line in text.splitlines():
            key = re.match(r"([A-Z][A-Z0-9_]*)=", line)
            if key:
                keys.setdefault(key.group(1), " ".join(comment))
                comment = []
            elif line.startswith("#"):
                comment.append(line.lstrip("#").strip())
            else:
                comment = []
        return keys
    return {}


def append_line(target: Path, line: str) -> str:
    """A command that appends line to target, first ending a last line that has no newline."""
    path = shlex.quote(str(target))
    return (
        f'{{ [ ! -s {path} ] || [ -z "$(tail -c1 {path})" ] || echo; '
        f"printf '%s\\n' {shlex.quote(line)}; }} >> {path}"
    )


def overlay_section(setup: Setup, sec: Section) -> None:
    """The overlay as kit_env reads it: the same layers, lowest first, a later one winning a key."""
    files = layers()
    values: dict[str, tuple[str, Path, list[str]]] = {}
    for layer in files:
        try:
            text = layer.read_text()
        except OSError:
            continue
        for key, value in parse(text).items():
            seen = values[key][2] if key in values else []
            values[key] = (value, layer, seen if layer.name in seen else [*seen, layer.name])
    documented = documented_keys(setup)
    sec.sources = [p for p in files if p.exists()]
    rows: TableRows = []
    hidden = 0
    for key in sorted(set(values) | (set(documented) & set(KEYS))):
        value, layer, seen = values.get(key, ("", None, []))
        if key in documented and not credential_name(key) and not holds_secret(value):
            shown: Cell = Code(value) if value else Muted("unset")
        else:
            shown, hidden = Badge("•••• hidden", "warn"), hidden + 1
        note: Cell = "" if key in KEYS else Muted("(not a key this kit reads)")
        rows.append(
            Row(
                ((Strong(key), note), shown, layer.name if layer else "-", " < ".join(seen) or "-"),
                anchor("overlay", key),
                documented.get(key, ""),
            )
        )
    sec.blocks.append(
        Para(("Values show only for keys kit.env.example documents; every other key is hidden.",))
    )
    sec.blocks.append(Table(("Key", "Value", "From layer", "Layers that set it"), rows))
    # agent-setup rewrites setup-paths.env, so a key the user sets goes in the layer under it; the
    # label names the keys setup-paths.env still wins.
    target, label = files[-1], f"Set an overlay key in {files[-1]} (a later line wins)"
    if target.name == "setup-paths.env" and len(files) > 1:
        target = files[-2]
        won = sorted(k for k, v in values.items() if v[1] == files[-1])
        label = (
            f"Set an overlay key in {target}; setup-paths.env, which agent-setup rewrites, wins "
            f"for {', '.join(won) or 'no key'}"
        )
    sec.actions.append(Action(label, append_line(target, "KEY=value")))
    count = sum(1 for v in values.values() if v[0])
    sec.count = count
    sec.summary = f"{count} keys set, {hidden} hidden"


def git_line(folder: Path) -> str:
    """HEAD, uncommitted files and commits behind the last fetched upstream: local refs only, and
    no optional lock (a status refresh would write the index)."""
    if not (folder / ".git").exists() or not shutil.which("git"):
        return "Not a git checkout"
    git = ["git", "--no-optional-locks", "-C", str(folder)]
    head = run([*git, "rev-parse", "--short=12", "HEAD"])
    dirty = run([*git, "status", "--porcelain"])
    behind = run([*git, "rev-list", "--count", "HEAD..@{u}"])
    parts = [f"At {head.stdout.strip()}" if head and head.returncode == 0 else "No commit"]
    if dirty and dirty.returncode == 0 and dirty.stdout.strip():
        parts.append(f"{len(dirty.stdout.splitlines())} uncommitted")
    if behind and behind.returncode == 0:
        count = int(behind.stdout.strip() or 0)
        parts.append(
            f"{count} commit(s) behind its upstream as last fetched"
            if count
            else "up to date with its upstream as last fetched"
        )
    return ", ".join(parts)


def pack_section(setup: Setup, sec: Section) -> None:
    from pack import pack_label
    from preset import source_status

    engine, pack_dir = setup.source(), setup.kit / "pack"
    sec.sources = [p for p in (pack_dir, setup.kit / ".install-state/current.json") if p.exists()]
    rows: TableRows = [("Engine", engine, git_line(engine))]
    record = setup.config.get("preset")
    if record:
        spec = str(record.get("source", ""))
        if spec.startswith("gh:") and not setup.check_updates:
            status = "not checked (rerun with --check-updates; it asks GitHub)"
        else:
            status = source_status(record)["note"]
        table = setup.config.get("preset_values", {}).get("table", {})
        rows.append(
            Row(
                ("Preset", Code(spec), f"At {pack_label(record.get('commit'))}; update: {status}"),
                anchor("pack", "preset"),
                str(table.get("description", "")) if isinstance(table, dict) else "",
            )
        )
        sec.actions.append(Action("Preview the preset again", setup_command(setup, "--preset", spec)))
        sec.actions.append(Action("Apply it", setup_command(setup, "--preset", spec, "--apply")))
    else:
        rows.append(("Preset", Muted("None"), ""))
    skills = sorted(p.name for p in (pack_dir / "skills").glob("*") if p.is_dir())
    rules = pack_dir / "rules.md"
    if skills or rules.is_file():
        rows.append(("Pack skills", ", ".join(skills) or "None", ""))
        if rules.is_file():
            words = len(rules.read_text(errors="replace").split())
            rows.append(("Pack rules", rules, f"{words} words, {rules.stat().st_size} bytes"))
    sec.blocks.append(Table(("What", "Where", "State"), rows))
    sec.count = len(skills)
    sec.summary = ("A preset" if record else "No preset") + f", {len(skills)} pack skills"


def docstring_line(path: Path) -> str:
    """A script's description: the first sentence of its module docstring's first paragraph."""
    try:
        found = re.search(r'^"""(.*?)(?:\n\n|""")', path.read_text(errors="replace"), re.M | re.S)
    except OSError:
        return ""
    if found is None:
        return ""
    text = " ".join(found.group(1).split())
    end = re.search(r"\.(\s|$)", text)
    return text[: end.start() + 1] if end else text


def data_section(setup: Setup, sec: Section) -> None:
    rows: TableRows = []
    for name in ("ro-mysql", "bqro"):
        kit_copy, found = setup.kit_bin(name), shutil.which(name)
        note = "Not on PATH"
        if found:
            note = "On PATH"
            if kit_copy.is_file() and Path(found).resolve() != kit_copy.resolve():
                same = Path(found).read_bytes() == kit_copy.read_bytes()
                note = "On PATH, same as the kit copy" if same else "On PATH, DIFFERS from the kit copy"
        rows.append(
            Row(
                (Strong(name), Path(found) if found else "-", kit_copy if kit_copy.is_file() else "-", note),
                anchor("data", name),
                next((docstring_line(p) for p in (kit_copy, setup.source() / "bin" / name) if p.is_file()), ""),
            )
        )
    sec.blocks.append(Table(("Wrapper", "On PATH", "Kit copy", "State"), rows))
    sec.blocks.append(Para((Muted("ro-mysql's tunnels are under SQL instances."),)))
    project = kit_env().get("BQRO_PROJECT", "")
    gcloud = Path(os.environ.get("CLOUDSDK_CONFIG") or Path.home() / ".config/gcloud")
    adc = gcloud / "application_default_credentials.json"
    bqro: TableRows = [
        ("Jobs project (BQRO_PROJECT)", Code(project) if project else Muted("unset")),
        ("gcloud config folder", gcloud if gcloud.is_dir() else Badge("missing", "bad")),
        ("Application default credentials", Badge.state("present" if adc.is_file() else "missing")),
    ]
    sec.blocks.append(Table(("What", "State"), bqro, "bqro"))
    if not adc.is_file():
        sec.actions.append(Action("Sign in for bqro", "gcloud auth application-default login"))
    sec.count = len(rows)
    on_path = sum(1 for name in ("ro-mysql", "bqro") if shutil.which(name))
    sec.summary = f"{len(rows)} wrappers, {on_path} on PATH"


def work_section(setup: Setup, sec: Section) -> None:
    import agent_task

    root = Path(agent_task.work_root())
    bound = Counter(agent_task.binding_keys())
    rows: list[tuple[float, Row]] = []
    open_total = 0
    for project in agent_task.projects(root):
        key = project.name if project.legacy else f"project:{project.name}"
        sessions = sum(n for k, n in bound.items() if k == key or k.startswith(key + "/"))
        items: list[tuple[str, str, float | None, int]] = []
        for item in project.items():
            touched = max(
                (s for s in [mtime(item), *(mtime(c) for c in item.iterdir())] if s), default=None
            )
            items.append(
                (item.name, agent_task._item_status(item), touched, bound[f"{key}/{item.name}"])
            )
        open_items = [i for i in items if i[1] not in agent_task.DONE_STATUSES]
        open_total += len(open_items)
        touched = max([mtime(project.path) or 0] + [i[2] or 0 for i in items]) or None
        detail: Cell = Muted("-")
        if items:
            item_rows: TableRows = [
                (project.path / "items" / n, s, when(t), b) for n, s, t, b in items
            ]
            detail = Fold(
                f"{len(open_items)} open of {len(items)}",
                (Table(("Item", "Status", "Last touched", "Sessions bound (all time)"), item_rows),),
            )
        name: Cell = (
            (Strong(project.name), Badge("legacy")) if project.legacy else Strong(project.name)
        )
        cells = (
            name,
            project.meta.get("status", "-"),
            detail,
            when(touched),
            sessions,
            project.path,
        )
        rows.append((touched or 0, Row(cells, anchor("work", project.name))))
    rows.sort(key=lambda r: r[0], reverse=True)
    sec.sources = [root]
    sec.blocks.append(
        Table(
            ("Project", "Status", "Items", "Last touched", "Sessions bound (all time)", "Folder"),
            [r for _, r in rows],
            empty=f"No projects under {root}.",
        )
    )
    sec.count = len(rows)
    sec.summary = f"{len(rows)} projects, {open_total} open items"
