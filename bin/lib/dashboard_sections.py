"""The dashboard's collectors: each fills the Section collect() made for it with plain values
(dashboard_html renders them). They read only; a credential is checked for presence, never read."""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

from blocking import BLOCKING, hook_name
from credentials import Credential, credential_name, declared_credentials, holds_secret
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
from dashboard_rows import (
    HookRow,
    SkillEntry,
    TableRows,
    hook_table,
    layer_cell,
    role_table,
    round_table,
    script_module,
    server_line,
    skill_text,
)
from hosts import (
    HOSTS,
    account_dirs,
    frontmatter,
    host_root_for,
    inventory,
    skill_dirs,
)
from kit_env import KEYS, kit_env, layers, parse
from kit_text import PACK_DIR, RULES_FILES, default_host_root, fill_servers
from mcp_describe import Described
from mcp_describe import read_cache as read_described
from pack import PACK_SKILLS
import secret_store

LIB = Path(__file__).resolve().parent
ENGINE = LIB.parents[1]
DATA_WRAPPERS = ("ro-mysql", "bqro")


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
    """present, missing, or unknown (no Keychain, or it failed to answer). Read through secret_store
    without reveal: the item's attributes are found, its secret is never asked for. secret-tool has
    no such read, so on Linux the state is unknown. AGENT_KIT_SECURITY (a test's fixture) replaces
    the security binary for this existence check alone."""
    security = os.environ.get("AGENT_KIT_SECURITY", "")
    if not security and secret_store.store() != "keychain":
        return "unknown"
    code, _ = secret_store.lookup(item.service, item.account, reveal=False, security=security)
    return "present" if code == 0 else "missing" if code == secret_store.ITEM_NOT_FOUND else "unknown"


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
        self.registries: dict[Path, list[HookRow] | ValueError] = {}
        self.scripts: dict[str, ModuleType | None | Exception] = {}

    def registry(self, path: Path) -> list[HookRow]:
        """The hook registry at path, read and validated once by agent-kit's own validate_registry;
        an invalid one raises with what is wrong on every read, so each part reading it shows that,
        not empty tables. A kit with no registry file has no hooks."""
        if path not in self.registries:
            try:
                self.registries[path] = self.read_registry(path)
            except ValueError as error:
                self.registries[path] = error
        found = self.registries[path]
        if isinstance(found, ValueError):
            raise ValueError(str(found))
        return found

    def read_registry(self, path: Path) -> list[HookRow]:
        if not path.is_file():
            return []
        try:
            entries = json.loads(path.read_text())
        except ValueError as error:
            raise ValueError(f"{path} is not JSON: {error}") from None
        try:
            self.api.validate_registry(entries, path)
        except SystemExit as error:
            raise ValueError(" ".join(str(error).split())) from None
        return [
            HookRow(
                event=e["event"],
                matcher=str(e.get("matcher") or ""),
                name=hook_name(str(e.get("command", ""))),
                command=str(e.get("command", "")),
                hosts=tuple(e["hosts"]),
                description=str(e.get("description") or ""),
                timeout=f"{e.get('timeout', '-')}{' async' if e.get('async') else ''}",
                blocking=hook_name(str(e.get("command", ""))) in BLOCKING,
            )
            for e in entries
        ]

    @functools.cached_property
    def skills(self) -> list[SkillEntry]:
        """Every skill once, as setup lays them out: the source checkout's (the kit's own without
        one) and the pack's (setup copies each into skills/ as well, so that copy is the pack's, not
        a second skill). A skill only the install's skills/ holds is one setup no longer lays out."""
        source, pack = self.source() / "skills", self.kit / PACK_DIR / PACK_SKILLS
        names = sorted({md.parent.name for d in (source, pack) for md in d.glob("*/SKILL.md")})
        out: list[SkillEntry] = []
        for name in names:
            engine = source / name / "SKILL.md"
            if (pack / name / "SKILL.md").is_file():
                layer, md = "pack", pack / name / "SKILL.md"
            else:
                layer, md = "kit", engine
            replaces = layer != "kit" and engine.is_file() and engine.read_bytes() != md.read_bytes()
            out.append(SkillEntry(name, layer, md, replaces))
        return out

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

    def script(self, name: str) -> ModuleType | None:
        """bin/<name> as a module (dashboard_rows.script_module), loaded once from the install's own
        copy: a newer source checkout's script imports names the bin/lib this page runs on lacks. A
        script that fails to load raises that error each time it is asked for."""
        if name not in self.scripts:
            try:
                self.scripts[name] = script_module(self.kit_bin(name))
            except Exception as error:  # noqa: BLE001  a broken script is its caller's one line
                self.scripts[name] = error
        found = self.scripts[name]
        if isinstance(found, Exception):
            raise found
        return found

    @functools.cached_property
    def setup_drift(self) -> list[Path]:
        """Paths agent-setup installed that changed since (its own managed_drift)."""
        module = self.script("agent-setup") if self.state else None
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
    names = entry.tools
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
        tools, fallback = described(cache.get(name))
        transport, target, description = server_line(setup, name)
        description = description or fallback
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
    """Every skill setup.skills lists, per configured host: linked, off, or left out by its own
    hosts: line."""
    source = setup.source()
    layered = {s.name for s in setup.skills if s.layer != "kit"}
    selected = setup.config.get("skills")
    if selected is not None:
        selected = [n for n in selected if n not in layered]
    sec.sources = [source / "skills"]
    rows: TableRows = []
    for entry in setup.skills:
        name, md = entry.name, entry.md
        text, allowed, allowed_text = skill_text(md)
        chips: list[Cell] = []
        changes: list[Cell] = []
        for host in setup.configured:
            if host not in allowed:
                chips.append(Badge(host, "off"))
                changes.append(Muted(f"{host}: left out by its hosts: line"))
                continue
            on = any((d / name).exists() for d in skill_dirs(host, setup.roots[host]))
            chips.append(Badge(host, "ok" if on else "off"))
            change = skill_toggle(setup, host, name, on, selected, entry.layer)
            if change not in changes:
                changes.append(change)
        rows.append(
            Row(
                (
                    Strong(name),
                    layer_cell(entry),
                    tuple(chips) or "-",
                    Fold("Change", (Lines(tuple(changes)),)) if changes else "-",
                    allowed_text,
                    md,
                ),
                anchor("skills", name),
                frontmatter(text).get("description", ""),
            )
        )
    sec.blocks.append(Table(("Skill", "Source", "Hosts", "Change", "hosts:", "File"), rows))
    sec.blocks.append(
        Para((Muted("Skills made by hand in a host folder are listed under Unmanaged sources."),))
    )
    pack = sum(s.layer == "pack" for s in setup.skills)
    sec.count = len(setup.skills)
    sec.summary = f"{len(setup.skills)} skills, {pack} from the pack"


def skill_toggle(
    setup: Setup, host: str, name: str, on: bool, selected: list[str] | None, layer: str
) -> Cell:
    """What changes the skill on host: a command with its label, or a note. A setup selection is
    one command for every host."""
    link = setup.roots[host] / "skills" / name
    if on and not (link.exists() or link.is_symlink()):
        return Muted(f"{host}: read from the shared skills folder")
    if layer == "pack":
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
    sec.blocks.append(role_table(setup, "roles", lambda p: p, live=True))
    sec.blocks.append(round_table(setup, title="Review rounds"))
    reviewers = sum(bool(r.prefix) for r in roles.roles.values())
    sec.count = len(roles.roles)
    sec.summary = f"{len(roles.roles)} roles, {reviewers} of them review roles"


def hooks_section(setup: Setup, sec: Section) -> None:
    registry_path = setup.kit / "hooks/registry.json"
    rows = setup.registry(registry_path)
    sec.sources = [registry_path]
    if setup.state:
        state = "on" if setup.config.get("blocking_hooks") else "off"
        sec.blocks.append(
            Para(("Blocking hooks in this install:", Strong(state), "(agent-setup --blocking-hooks)."))
        )
    sec.blocks.append(hook_table(rows, setup.kit / "hooks", "hooks", lambda p: p, timeout=True))
    sec.count = len(rows)
    sec.summary = f"{len(rows)} hooks, {sum(r.blocking for r in rows)} blocking"


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
    for name in DATA_WRAPPERS:
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
    on_path = sum(1 for name in DATA_WRAPPERS if shutil.which(name))
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
                (item.name, agent_task.item_status(item), touched, bound[f"{key}/{item.name}"])
            )
        open_items = project.open_items()
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
