"""Sentry instances (mcp/sentry-instances.json), where each one's token lives, the MCP servers
rendered for them, and the credential policy for a Sentry server. Runs under macOS /usr/bin/python3
(3.9) too: an MCP host may launch bin/sentry-mcp with a PATH that has no newer python3."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

SAFE_ENV = frozenset(("SENTRY_HOST", "MCP_URL", "MCP_SKILLS", "MCP_DISABLE_SKILLS", "EMBEDDED_AGENT_PROVIDER"))
TOKEN_OPTIONS = frozenset(("--access-token", "--token"))
HOST_OPTIONS = frozenset(("--host", "--url"))

INSTANCES_FILE = "sentry-instances.json"
TEMPLATE_SERVER = "sentry"
LEGACY_SERVICE = "agent-kit/mcp/sentry"
ACCOUNT = "sentry"
TOKEN_ENV = "SENTRY_ACCESS_TOKEN"
# Root-owned fixed paths, never one from PATH (bin/ro-mysql keeps the same two lists).
SECURITY = "/usr/bin/security"
SECRET_TOOL_PATHS = ("/usr/bin/secret-tool", "/usr/local/bin/secret-tool")
# `security` exits 44 (errSecItemNotFound) when no item matches; any other failure is a denial or a
# locked Keychain.
ITEM_NOT_FOUND = 44
LABELS = ("prod", "staging")
INSTANCE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
# The service name is printed inside a shell command the user copies, so it stays shell-safe.
SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
HOST = re.compile(r"(https?://)?[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._/-]*)?")


def secret_store() -> str:
    """Where tokens live: the macOS Keychain, libsecret's secret-tool on Linux, else environment
    variables. A store without the item also falls back to the variable."""
    if os.path.exists(SECURITY):
        return "keychain"
    if any(os.path.exists(path) for path in SECRET_TOOL_PATHS):
        return "secret-tool"
    return "env"


@dataclass
class Instance:
    name: str
    host: str
    keychain: str
    server: str
    orgs: Dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def base_url(self) -> str:
        return self.host if "://" in self.host else f"https://{self.host}"

    @property
    def env_name(self) -> str:
        """The variable a machine without a store reads: the catalog's own server keeps the
        historical SENTRY_ACCESS_TOKEN, every other instance has its own."""
        if self.server.lower() == TEMPLATE_SERVER:
            return TOKEN_ENV
        return TOKEN_ENV + "_" + re.sub(r"[^A-Z0-9]", "_", self.name.upper())

    def enable_command(self, store: Optional[str] = None) -> str:
        store = store or secret_store()
        if store == "keychain":
            return f"security add-generic-password -s {self.keychain} -a {ACCOUNT} -w"
        if store == "secret-tool":
            return f"secret-tool store --label={ACCOUNT} service {self.keychain} account {ACCOUNT}"
        return f"export {self.env_name}=<token> in your shell profile"

    def missing(self, store: Optional[str] = None) -> str:
        store = store or secret_store()
        if store == "keychain":
            return f"no Keychain item {self.keychain}"
        if store == "secret-tool":
            return f"no secret-tool item service={self.keychain} account={ACCOUNT}"
        return f"no Keychain or secret-tool here, and {self.env_name} is unset"

    def label(self, org: str) -> str:
        """prod/staging for an org: from its slug when the slug says so, else the configured label,
        else unknown. Discovery names the orgs; the config only labels ones whose names don't."""
        slug = org.lower()
        if any(part in slug for part in ("prd", "prod")):
            return "prod"
        if any(part in slug for part in ("stg", "staging")):
            return "staging"
        return self.orgs.get(org, "unknown")


LEGACY = Instance("default", "", LEGACY_SERVICE, TEMPLATE_SERVER)


class KeychainMissing(Exception):
    pass


class KeychainDenied(Exception):
    pass


def overlay_dir(kit: Path) -> Path:
    """The per-user overlay folder, as hooks/lib/kit_env.kit_env_path resolves kit.env's."""
    if os.environ.get("KIT_ENV"):
        return Path(os.environ["KIT_ENV"]).parent
    for name in ("setup-paths.env", "kit.env"):
        if (kit / "local" / name).is_file():
            return kit / "local"
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "local"


def read_instances(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: not readable JSON ({e})") from e
    instances = data.get("instances", {}) if isinstance(data, dict) else None
    if not isinstance(instances, dict) or not all(isinstance(v, dict) for v in instances.values()):
        raise ValueError(f"{path}: `instances` must map names to objects")
    return instances


def merge_instances(*layers: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Later layers override earlier ones field by field."""
    merged: Dict[str, Dict[str, Any]] = {}
    for layer in layers:
        for name, spec in layer.items():
            merged.setdefault(name, {}).update(copy.deepcopy(spec))
    return merged


def check_instance(name: str, spec: Dict[str, Any]) -> Instance:
    if not INSTANCE_NAME.fullmatch(name):
        raise ValueError(f"Sentry instance name {name!r} must be lowercase letters, digits, - or _")
    unknown = set(spec) - {"host", "keychain", "server", "orgs", "note"}
    if unknown:
        raise ValueError(f"Sentry instance {name}: unknown field(s) {', '.join(sorted(unknown))}")
    host, keychain = spec.get("host"), spec.get("keychain")
    server = spec.get("server") or f"sentry-{name}"
    orgs = spec.get("orgs", {})
    if not (isinstance(host, str) and HOST.fullmatch(host)):
        raise ValueError(f"Sentry instance {name} needs a host (a hostname or http(s) URL)")
    if not (isinstance(keychain, str) and SERVICE_NAME.fullmatch(keychain)):
        raise ValueError(f"Sentry instance {name} needs a keychain service (letters, digits, . _ / -)")
    if not (isinstance(server, str) and INSTANCE_NAME.fullmatch(server.lower())):
        raise ValueError(f"Sentry instance {name}: server must be a plain MCP server name")
    if not isinstance(orgs, dict) or not all(isinstance(v, str) and v in LABELS for v in orgs.values()):
        raise ValueError(f"Sentry instance {name}: orgs must map org slugs to {' or '.join(LABELS)}")
    return Instance(name, host, keychain, server, dict(orgs), str(spec.get("note", "")))


def load_instances(kit: Path, overlay: Optional[Path] = None) -> Dict[str, Instance]:
    """The kit's instances (a preset's [sentry.instances] land in <kit>/mcp/sentry-instances.json at
    setup), each overlaid field by field by <overlay>/sentry-instances.json, which may also add its
    own. None configured: the catalog's `sentry` server works as it always did."""
    merged = merge_instances(
        read_instances(kit / "mcp" / INSTANCES_FILE),
        read_instances((overlay or overlay_dir(kit)) / INSTANCES_FILE),
    )
    out = {name: check_instance(name, spec) for name, spec in merged.items()}
    servers = [inst.server.lower() for inst in out.values()]
    if len(servers) != len(set(servers)):
        raise ValueError("Sentry instances must each name a different MCP server")
    return out


def default_instance(instances: Dict[str, Instance]) -> Instance:
    """The instance behind the catalog's own `sentry` server, else the legacy single item."""
    return next((i for i in instances.values() if i.server.lower() == TEMPLATE_SERVER), LEGACY)


def _store_lookup(service: str, reveal: bool) -> "tuple[int, str]":
    """(exit code, secret or "") from the store; 0 means found. Without reveal the Keychain is
    asked for attributes only, never the secret."""
    store = secret_store()
    if store == "env":
        return ITEM_NOT_FOUND, ""
    if store == "keychain":
        command = [SECURITY, "find-generic-password", "-s", service, "-a", ACCOUNT, *(["-w"] if reveal else [])]
    else:
        tool = next(path for path in SECRET_TOOL_PATHS if os.path.exists(path))
        command = [tool, "lookup", "service", service, "account", ACCOUNT]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120 if reveal else 10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    secret = proc.stdout.strip()
    if store == "secret-tool" and (proc.returncode != 0 or not secret):
        return ITEM_NOT_FOUND, ""  # secret-tool exits 1 for a missing item and for a refusal alike
    return proc.returncode, secret if reveal else ""


def token_present(inst: Instance) -> bool:
    """Whether the instance has a token: its store item, else its variable."""
    return _store_lookup(inst.keychain, reveal=False)[0] == 0 or bool(os.environ.get(inst.env_name))


def read_token(inst: Instance) -> str:
    """The instance's token from its store item, else its variable. KeychainMissing when neither
    has one, KeychainDenied when the item exists but could not be read."""
    code, token = _store_lookup(inst.keychain, reveal=True)
    if code == 0 and token:
        return token
    fallback = os.environ.get(inst.env_name, "")
    if fallback:
        return fallback
    if code == ITEM_NOT_FOUND:
        raise KeychainMissing(inst.keychain)
    raise KeychainDenied(inst.keychain)


def _server_args(spec: Dict[str, Any]) -> list:
    """The template's options without its npx package prefix and host, for a per-instance clone."""
    args = list(spec.get("args", []))
    if Path(str(spec.get("command", ""))).name == "npx":
        if args and args[0] in ("-y", "--yes"):
            args = args[1:]
        if args and args[0].startswith("@sentry/mcp-server"):
            args = args[1:]
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in HOST_OPTIONS:
            skip = True
        elif not any(arg.startswith(option + "=") for option in HOST_OPTIONS):
            out.append(arg)
    return out


def expand_servers(
    kit: Path,
    servers: Dict[str, Any],
    present: Optional[Callable[[Instance], bool]] = None,
    overlay: Optional[Path] = None,
) -> Dict[str, Any]:
    """The catalog with one server per configured instance, each cloned from the `sentry` entry
    right after it. That entry stays as it is; any other instance renders only once it has a token,
    so a host never lists a server that cannot start."""
    template_name = next((n for n in servers if n.lower() == TEMPLATE_SERVER), None)
    if template_name is None or not isinstance(servers[template_name], dict):
        return servers
    present = present or token_present
    instances = load_instances(kit, overlay)
    out: Dict[str, Any] = {}
    for name, spec in servers.items():
        out[name] = spec
        if name != template_name:
            continue
        for inst in instances.values():
            if inst.server.lower() == TEMPLATE_SERVER or inst.server in servers or not present(inst):
                continue
            clone = copy.deepcopy(spec)
            if Path(str(spec.get("command", ""))).name == "npx":
                clone["command"] = str(kit / "bin/sentry-mcp")
            clone["args"] = ["--instance", inst.name, *_server_args(spec)]
            clone["env"] = {**spec.get("env", {}), "SENTRY_HOST": inst.host}
            out[inst.server] = clone
    return out


def server_names(kit: Path, overlay: Optional[Path] = None) -> "set[str]":
    """Lowercased names of every Sentry server: they take the wrapper's credential policy."""
    try:
        names = {inst.server.lower() for inst in load_instances(kit, overlay).values()}
    except ValueError:
        names = set()
    return names | {TEMPLATE_SERVER}


def safe_sentry(spec: dict[str, Any], wrapper: Path) -> dict[str, Any]:
    if set(spec) - {"command", "args", "env"}:
        raise ValueError("Unsupported Sentry transport fields; use an explicit runtime wrapper")
    command = spec.get("command")
    if not isinstance(command, str) or Path(command).name not in ("npx", "sentry-mcp"):
        raise ValueError("Unsupported Sentry launcher; expected npx or the Sentry runtime wrapper")
    raw = spec.get("args", [])
    if not isinstance(raw, list) or not all(isinstance(arg, str) for arg in raw):
        raise ValueError("Sentry args must be strings")
    index = 0
    if Path(command).name == "npx":
        if raw and raw[0] in ("-y", "--yes"):
            index = 1
        if index >= len(raw) or not (
            raw[index] == "@sentry/mcp-server" or raw[index].startswith("@sentry/mcp-server@")
        ):
            raise ValueError("Unsupported Sentry npx package; expected @sentry/mcp-server")
        index += 1
    args = []
    while index < len(raw):
        arg = raw[index]
        if arg in TOKEN_OPTIONS:
            if index + 1 >= len(raw) or raw[index + 1].startswith("--"):
                raise ValueError("Sentry token option has no value")
            index += 2
            continue
        if any(arg.startswith(option + "=") for option in TOKEN_OPTIONS):
            if not arg.split("=", 1)[1]:
                raise ValueError("Sentry token option has no value")
            index += 1
            continue
        args.append(arg)
        index += 1
    env = spec.get("env", {})
    if not isinstance(env, dict):
        raise ValueError("Sentry env must be an object")
    if set(env) - SAFE_ENV - {"SENTRY_ACCESS_TOKEN"}:
        raise ValueError(
            "Unsupported Sentry environment fields; use an explicit runtime credential wrapper"
        )
    safe_env = {key: value for key, value in env.items() if key in SAFE_ENV}
    if not all(isinstance(value, str) for value in safe_env.values()):
        raise ValueError("Sentry host environment must contain strings")
    return {"command": str(wrapper), "args": args, **({"env": safe_env} if safe_env else {})}
