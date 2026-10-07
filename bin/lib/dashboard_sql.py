"""The dashboard's SQL instances section: one row per db-tunnel-* forward from ro-mysql's own ssh
config walker and --refresh cache, and the add-connection form. It never runs `ro-mysql --tunnels`,
which may read the MySQL login-path store, and opens no tunnel."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from credentials import Credential
from dashboard_html import Action, AddHelper, Badge, Cell, Muted, Row, Section, Strong, Table, anchor
from dashboard_sections import ENGINE, Setup, TableRows, credential_cell, load_script


def ro_mysql(setup: Setup) -> ModuleType | None:
    """The kit's ro-mysql as a module, for its config walker, cache reader and patterns."""
    for path in (ENGINE / "bin/ro-mysql", setup.source() / "bin/ro-mysql"):
        module = load_script(path, "ro_mysql_for_dashboard")
        if module is not None:
            return module
    return None


def form_patterns(module: ModuleType) -> tuple[tuple[str, str], ...]:
    """What `ro-mysql add` accepts, as the form's data-p-* attributes: the form checks with
    ro-mysql's own patterns and ranges, never a copy."""
    return (
        ("name", module.TUNNEL_NAME_RE.pattern),
        ("user", module.DB_USER_RE.pattern),
        ("via", module.SSH_ALIAS_RE.pattern),
        ("host", module.REMOTE_HOST_RE.pattern),
        ("local-ports", "-".join(map(str, module.LOCAL_PORTS))),
        ("remote-ports", "-".join(map(str, module.REMOTE_PORTS))),
        ("staging", module.STAGING_RE.pattern),
        ("prefix", module.TUNNEL_PREFIX),
        ("suffix", "-staging"),
        ("service", module.KEYCHAIN_SERVICE),
    )


def tunnel_state(cached: dict[str, str], rejected: bool) -> Cell:
    """The tunnel's state as ro-mysql last recorded it, never probed: a rejected login
    (db-auth-failed), the last --refresh's error or login path, or not checked."""
    if rejected:
        return (Badge("login rejected", "bad"), Muted("run ro-mysql --rotate in your own terminal"))
    if not cached:
        return Badge("not checked")
    databases, via = cached.get("databases", ""), cached.get("via", "")
    if databases.startswith("?"):
        return (Badge("failed", "bad"), Muted(f"{cached.get('checked', '')}: {databases[1:].strip()}"))
    return (Badge("ok", "ok"), Muted(f"{cached.get('checked', '')} {via}".strip()))


def target(t: Any) -> str:
    """The row's one line: where the forward goes, and the bastion it goes through."""
    return f"{t.remote} through {t.hostname}" if t.remote and t.hostname else t.remote


def sql_section(setup: Setup, sec: Section) -> None:
    config = Path.home() / ".ssh/config"
    sec.sources = [config]
    module = ro_mysql(setup)
    rows: TableRows = []
    staging = 0
    found: list[Any] = []
    if module is not None:
        cache = module.read_cache()
        found = module.load_tunnels(str(config))
        shared = module.multi_port_aliases(found)
        failed = module.auth_failure()
        for t in found:
            cached = cache.get((t.alias, t.port), {})
            row = anchor("sql", t.name)
            if t.user and t.alias not in shared:
                item = Credential(module.KEYCHAIN_SERVICE, module.keychain_account(t, t.user))
                cred: Cell = credential_cell(sec, item, f"ro-mysql {t.name}", row)
            else:
                cred = Muted("No Keychain item (no annotated user, or a shared alias)")
            staging += t.kind == "STAGING"
            databases = cached.get("databases", "?")  # a failed --refresh caches "? <reason>"
            rows.append(
                Row(
                    (
                        Strong(t.alias),
                        t.port,
                        Badge(t.kind, "warn" if t.kind == "PROD" else "ok"),
                        t.user or "?",
                        cred,
                        ", ".join(databases.split(",")) if not databases.startswith("?") else "-",
                        tunnel_state(cached, bool(t.user) and failed == (t.user, t.alias)),
                    ),
                    row,
                    target(t),
                )
            )
    sec.blocks.append(
        Table(
            ("Instance", "Port", "Kind", "DB user", "Keychain item", "Databases (cached)", "State"),
            rows,
            empty="No db-tunnel-* LocalForward in ~/.ssh/config or the files it Includes.",
            note="State is what ro-mysql last recorded (its --refresh cache and rejected logins); "
            "this page opens no tunnel.",
        )
    )
    if module is not None:
        sec.blocks.append(
            AddHelper(
                tuple(sorted({t.port for t in found})),
                tuple(sorted({t.alias for t in found})),
                form_patterns(module),
            )
        )
    sec.actions.append(Action("List tunnels (in your own terminal)", "ro-mysql --tunnels"))
    sec.actions.append(Action("Store DB passwords (in your own terminal)", "ro-mysql --rotate"))
    sec.actions.append(
        Action(
            "Add a SQL instance (in your own terminal; it prompts for the password)",
            "ro-mysql add --name <svc> --user <db user> --via <bastion> --remote <host:port> "
            "--local-port <N> [--staging]",
        )
    )
    sec.count = len(rows)
    sec.summary = f"{len(rows)} tunnels, {staging} staging"
