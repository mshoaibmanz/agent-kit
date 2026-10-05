"""Native host rendering and a read-only configuration inventory."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks/lib"))
from kit_env import kit_env, work_root  # noqa: E402

HOSTS = ("claude", "codex", "cursor")

EVENTS = {
    "PreToolUse": "preToolUse",
    "PostToolUse": "postToolUse",
    "UserPromptSubmit": "beforeSubmitPrompt",
    "Stop": "stop",
    "SubagentStop": "subagentStop",
    "SessionStart": "sessionStart",
}
BEGIN = "# BEGIN agent-kit managed"
END = "# END agent-kit managed"
SHELL_BEGIN = "# BEGIN agent-kit shell"
SHELL_END = "# END agent-kit shell"


def shell_env(kit: Path, host: str) -> dict[str, str]:
    if os.environ.get('AGENT_GIT_HOOKS') == 'off':
        return {'AI_AGENT': host, 'AGENT_HOST': host, 'AGENT_GIT_HOOKS': 'off',
                'AGENT_KIT_DIR': str(kit), 'KIT_ENV': str(kit / 'local/setup-paths.env')}
    return {
        "AI_AGENT": host,
        "AGENT_HOST": host,
        "AGENT_KIT_DIR": str(kit),
        "KIT_ENV": str(kit / "local/setup-paths.env"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(kit / "git-hooks"),
    }


def block(existing: str, managed: str, begin: str = BEGIN, end: str = END) -> str:
    if existing.count(begin) != existing.count(end) or existing.count(begin) > 1:
        raise SystemExit("agent-kit: malformed or repeated managed markers")
    new = begin + "\n" + managed.rstrip() + "\n" + end
    if begin in existing:
        start, stop = existing.index(begin), existing.index(end) + len(end)
        if begin in (BEGIN, SHELL_BEGIN):
            for line in existing[stop:].splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if not stripped.startswith("["):
                    raise SystemExit(
                        "agent-kit: TOML keys after a managed END marker require a new table header; nothing written"
                    )
                break
        return existing[:start] + new + existing[stop:]
    return existing + ("\n" if existing and not existing.endswith("\n") else "") + new + "\n"


# The top-level tables a kit block writes: MCP servers, the sandbox roots, and (agent-setup's outer
# block) the shell policy.
OWNED_TABLES = frozenset({"mcp_servers", "sandbox_workspace_write", "shell_environment_policy"})


def _table_name(chunk: str) -> str:
    """The top-level key of a chunk's table header, quoted or bare."""
    try:
        return next(iter(tomllib.loads(chunk.split("\n", 1)[0])), "")
    except tomllib.TOMLDecodeError:
        return ""


def keep_host_tables(existing: str, begin: str = BEGIN, end: str = END) -> str:
    """Move tables the host wrote inside a kit block (hook trust, project trust) past its END.

    Codex appends `[hooks.state.*]` and `[projects.*]` before the file's trailing comment, which is
    our END marker, so replacing the block would silently revoke that trust.
    """
    if begin not in existing or end not in existing:
        return existing
    start = existing.index(begin) + len(begin)
    stop = existing.index(end)
    owned, host = [], []
    for chunk in re.split(r"(?m)^(?=\[)", existing[start:stop]):
        (owned if not chunk.startswith("[") or _table_name(chunk) in OWNED_TABLES else host).append(chunk)
    if not host:
        return existing
    tail = existing[stop + len(end) :].lstrip("\n")
    moved = "".join(host).rstrip() + "\n"
    return (
        existing[:start]
        + "".join(owned).rstrip()
        + "\n"
        + end
        + "\n\n"
        + moved
        + ("\n" + tail if tail else "")
    )


def shell_without_owned(existing: str) -> str:
    if (
        existing.count(SHELL_BEGIN) != existing.count(SHELL_END)
        or existing.count(SHELL_BEGIN) > 1
    ):
        raise SystemExit(
            "agent-kit: malformed or repeated shell ownership markers; nothing written"
        )
    if SHELL_BEGIN not in existing:
        return existing
    start, stop = (
        existing.index(SHELL_BEGIN),
        existing.index(SHELL_END) + len(SHELL_END),
    )
    if stop < start:
        raise SystemExit(
            "agent-kit: malformed shell ownership markers; nothing written"
        )
    owned = tomllib.loads(existing[start:stop])
    if set(owned) != {"shell_environment_policy"} or set(
        owned["shell_environment_policy"]
    ) != {"set"}:
        raise SystemExit("agent-kit: invalid owned shell table; nothing written")
    return existing[:start] + "[shell_environment_policy.set]" + existing[stop:]


def shell_config(
    existing: str, values: dict[str, str], versions: list[dict[str, Any]]
) -> str:
    current = tomllib.loads(existing)
    if SHELL_BEGIN in existing:
        start, stop = (
            existing.index(SHELL_BEGIN),
            existing.index(SHELL_END) + len(SHELL_END),
        )
        owned = tomllib.loads(existing[start:stop])["shell_environment_policy"]["set"]
        if not any(set(owned) == set(version) for version in [values, *versions]) or any(
            key not in values
            or not any(value == version.get(key) for version in [values, *versions])
            for key, value in owned.items()
        ):
            raise SystemExit(
                "agent-kit: owned shell keys have user edits; nothing written"
            )
    clean = shell_without_owned(existing)
    user_config = tomllib.loads(clean)
    user_set = user_config.get("shell_environment_policy", {}).get("set", {})
    if not isinstance(user_set, dict) or not all(
        isinstance(value, str) for value in user_set.values()
    ):
        raise SystemExit(
            "agent-kit: shell set must map names to strings; nothing written"
        )
    if set(user_set) & set(values):
        raise SystemExit(
            "agent-kit: unowned agent shell keys already exist; nothing written"
        )
    expected = dict(current)
    expected["shell_environment_policy"] = {
        **current.get("shell_environment_policy", {}),
        "set": {**user_set, **values},
    }
    owned_text = (
        SHELL_BEGIN
        + "\n[shell_environment_policy.set]\n"
        + "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in values.items()
        )
        + "\n"
        + SHELL_END
    )
    lines = clean.splitlines(keepends=True)
    for index, line in enumerate(lines):
        try:
            header = tomllib.loads(line)
        except ValueError:
            continue
        if header == {"shell_environment_policy": {"set": {}}}:
            candidate = "".join(
                [*lines[:index], owned_text + "\n", *lines[index + 1 :]]
            )
            if tomllib.loads(candidate) == expected:
                return candidate
    user_text = ""
    if user_set:
        for index in range(len(lines)):
            candidate = "".join([*lines[:index], *lines[index + 1 :]])
            try:
                removed = tomllib.loads(candidate)
            except ValueError:
                continue
            wanted = dict(user_config)
            wanted["shell_environment_policy"] = {
                key: value
                for key, value in user_config["shell_environment_policy"].items()
                if key != "set"
            }
            if removed == wanted:
                clean = candidate
                user_text = "\n" + "\n".join(
                    f"{json.dumps(key)} = {json.dumps(value)}"
                    for key, value in user_set.items()
                )
                break
        else:
            raise SystemExit(
                "agent-kit: shell set layout needs a standalone table; nothing written"
            )
    candidate = (
        clean
        + ("\n" if clean and not clean.endswith("\n") else "")
        + owned_text
        + user_text
        + "\n"
    )
    if tomllib.loads(candidate) != expected:
        raise SystemExit(
            "agent-kit: shell merge would change user configuration; nothing written"
        )
    return candidate


def normalize_transport(spec: dict[str, Any]) -> dict[str, Any]:
    if ("command" in spec) == ("url" in spec):
        raise ValueError("MCP transport needs exactly one command or URL")
    field = "command" if "command" in spec else "url"
    if not isinstance(spec[field], str) or not spec[field].strip():
        raise ValueError("MCP transport must be a nonempty string")
    if "description" in spec and not isinstance(spec["description"], str):
        raise ValueError("MCP description must be a string")
    expected = "stdio" if field == "command" else "http"
    if "type" in spec and spec["type"] != expected:
        raise ValueError("MCP type must match the command or HTTP URL transport")
    return {key: value for key, value in spec.items() if key not in ("type", "description")}


def safe_servers(kit: Path) -> dict[str, Any]:
    servers = json.loads((kit / "mcp/servers.json").read_text()).get("mcpServers", {})
    out = {}
    if not isinstance(servers, dict):
        raise ValueError("MCP catalog must map server names to objects")
    for name, spec in servers.items():
        if not isinstance(spec, dict) or set(spec) - {"command", "args", "url", "env", "type", "description"}:
            raise ValueError("Unsupported MCP transport fields; nothing written")
        spec = normalize_transport(spec)
        if ("command" in spec) == ("url" in spec):
            raise ValueError("MCP transport needs exactly one command or URL")
        if "command" in spec and (not isinstance(spec["command"], str) or not spec["command"]):
            raise ValueError("MCP command must be a nonempty string")
        if "url" in spec and (not isinstance(spec["url"], str) or not spec["url"]):
            raise ValueError("MCP URL must be a nonempty string")
        if (
            name.lower() != "sentry"
            and "args" in spec
            and (
                "command" not in spec
                or not isinstance(spec["args"], list)
                or not all(isinstance(value, str) for value in spec["args"])
            )
        ):
            raise ValueError("MCP args must be strings on a command transport")
        if (
            name.lower() != "sentry"
            and "env" in spec
            and (
                "command" not in spec
                or not isinstance(spec["env"], dict)
                or not all(isinstance(value, str) for value in spec["env"].values())
            )
        ):
            raise ValueError("MCP environment must map names to strings on a command transport")
        if name.lower() == "sentry":
            from sentry import safe_sentry

            out[name] = safe_sentry(spec, kit / "bin/sentry-mcp")
        else:
            encoded = json.dumps(spec)
            if "$keychain" in encoded:
                raise SystemExit(f"agent-kit: {name} needs a runtime credential wrapper")
            out[name] = spec
    return out


def sandbox_roots() -> list[str]:
    """Paths a workspace-write Codex session must write outside the repo: the work root (task
    folders hold TMPDIR and evidence), named even before it exists because agent-setup creates it
    after rendering; and, when the overlay sets CODEX_SANDBOX_GCLOUD=1, gcloud's config dir, which
    bq rewrites on every call (it holds credentials, so it is opt-in)."""
    roots = [work_root()]
    gcloud = os.environ.get("CLOUDSDK_CONFIG") or str(Path.home() / ".config/gcloud")
    if kit_env()["CODEX_SANDBOX_GCLOUD"] == "1" and Path(gcloud).is_dir():
        roots.append(gcloud)
    return roots


def install_text(text: str, kit: str, skill: str | None = None, export: bool = True) -> str:
    """A kit markdown file as installed with its kit at <kit>: {{AGENT_KIT_DIR}} names the kit, each
    ```sh block first exports AGENT_KIT_DIR (with export), and ${CLAUDE_SKILL_DIR}, which only Claude
    Code expands, names <kit>/skills/<skill> as one shell word. A kit given as a shell expression
    (${CLAUDE_PLUGIN_ROOT}/kit) is double-quoted so it still expands."""
    word = f'"{kit}"' if "$" in kit else shlex.quote(kit)
    text = text.replace("{{AGENT_KIT_DIR}}", kit)
    if export:
        text = text.replace("```sh\n", f"```sh\nexport AGENT_KIT_DIR={word}\n")
    if skill:
        text = text.replace("${CLAUDE_SKILL_DIR}", shlex.quote(f"{kit}/skills/{skill}"))
    return text


def toml_sandbox(roots: list[str]) -> str:
    if not roots:
        return ""
    return f"[sandbox_workspace_write]\nwritable_roots = {json.dumps(roots)}\n"


def toml_servers(servers: dict[str, Any]) -> str:
    lines = []
    for name, spec in servers.items():
        lines.append(f"[mcp_servers.{json.dumps(name)}]")
        for key in ("command", "args", "url", "env"):
            value = spec.get(key)
            if value is None:
                continue
            if isinstance(value, dict):
                value = (
                    "{ "
                    + ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in value.items())
                    + " }"
                )
            else:
                value = json.dumps(value)
            lines.append(f"{key} = {value}")
        lines.append("")
    return "\n".join(lines)


def native_hooks(
    kit: Path, registry: list[dict[str, Any]], host: str, root: Path | None = None
) -> dict[str, Any]:
    hooks: dict[str, Any] = {}
    for entry in registry:
        if host not in entry["hosts"]:
            continue
        event = entry["event"]
        command = shlex.split(entry["command"])
        name = Path(command[0]).name
        if len(command) != 1 or not (kit / "hooks" / name).is_file():
            raise SystemExit(f"agent-kit: {name} needs an explicit native hook adapter")
        timeout = (
            max(entry.get("timeout", 60), 65)
            if name in ("edit-guard", "review-mark-changes", "tests-ran-mark")
            else entry.get("timeout", 60)
        )
        destination = (root or Path.home() / f".{host}").expanduser().absolute()
        adapter = f"AGENT_KIT_HOST_ROOT={shlex.quote(str(destination))} {shlex.quote(str(kit / 'hooks/host-adapter'))} {host} {shlex.quote(name)}"
        if host == "codex":
            if event not in EVENTS:
                raise SystemExit(f"agent-kit: unsupported Codex event {event}")
            group = {"hooks": [{"type": "command", "command": adapter, "timeout": timeout}]}
            if entry.get("matcher") is not None:
                group["matcher"] = re.sub(
                    r"\b(?:Edit|Write|MultiEdit)\b", "apply_patch", entry["matcher"]
                )
            hooks.setdefault(event, []).append(group)
        else:
            if event not in EVENTS:
                raise SystemExit(f"agent-kit: no Cursor adapter for {event}")
            hook = {"command": adapter, "timeout": timeout}
            if entry.get("matcher") is not None:
                hook["matcher"] = re.sub(
                    r"\b(?:Edit|Write|MultiEdit)\b",
                    "Write|Delete",
                    entry["matcher"].replace("Bash", "Shell"),
                )
            if event == "PreToolUse":
                hook["failClosed"] = True
            hooks.setdefault(EVENTS[event], []).append(hook)
    return {"hooks": hooks, **({"version": 1} if host == "cursor" else {})}


def merge_hooks(
    live: dict[str, Any], previous: dict[str, Any], wanted: dict[str, Any]
) -> dict[str, Any]:
    out = dict(live)
    hooks = {e: list(groups) for e, groups in live.get("hooks", {}).items()}
    for event, groups in previous.get("hooks", {}).items():
        for group in groups:
            if group in hooks.get(event, []):
                hooks[event].remove(group)
    for event, groups in wanted["hooks"].items():
        for group in groups:
            if group not in hooks.get(event, []):
                hooks.setdefault(event, []).append(group)
    out["hooks"] = {e: groups for e, groups in hooks.items() if groups}
    if "version" in wanted:
        out["version"] = wanted["version"]
    return out


def role_files(api: Any, roles: Any) -> dict[str, str]:
    result = {}
    if roles is None:
        return result
    api.expected_agents(roles, "codex")
    for name, role in roles.roles.items():
        if name == "main" or role.provider != roles.provider("codex"):
            continue
        text = (api.AGENT_SRC / f"{name}.md").read_text()
        end = text.index("\n---\n", 3)
        description = re.search(r"^description:\s*(.+)$", text[:end], re.MULTILINE)
        values = {
            "name": name,
            "description": description.group(1) if description else name,
            "developer_instructions": text[end + 5 :].strip(),
            "model": role.model,
            "model_reasoning_effort": role.effort,
            "sandbox_mode": "read-only",
        }
        result[f"{name}.toml"] = (
            "\n".join(f"{k} = {json.dumps(v)}" for k, v in values.items()) + "\n"
        )
    return result


def render(api: Any, args: Any) -> int:
    host = args.host
    if host not in ("codex", "cursor"):
        raise SystemExit(f"agent-kit: unsupported host {host}")
    root = Path(os.environ.get("AGENT_KIT_HOST_ROOT", str(Path.home() / f".{host}")))
    if (
        not args.dry_run
        and api.KIT.resolve() != (Path.home() / ".agents").resolve()
        and "AGENT_KIT_HOST_ROOT" not in os.environ
    ):
        raise SystemExit(
            "agent-kit: an uninstalled kit requires AGENT_KIT_HOST_ROOT to render another host"
        )
    components = set(args.components or ("hooks", "rules", "roles", "mcp"))
    roles = api.load_roles() if "roles" in components else None
    plans: list[tuple[Path, str]] = []
    journal_key = api.state_name(f"{host}-journal", root, follow=False)
    journal = api.read_state(journal_key) or {}
    ownership = {}

    def versions(key):
        return [api.read_state(key) or {}, *journal.get(key, [])]

    if "hooks" in components:
        registry = api.validate_registry(json.loads(api.REGISTRY.read_text()))
        wanted_hooks = native_hooks(api.KIT, registry, host, root)
        hook_path = root / "hooks.json"
        state = api.state_name(f"{host}-hooks", hook_path)
        live = json.loads(api.read_or_empty(hook_path) or "{}")
        previous = {"hooks": {}}
        for owned in versions(state):
            for event, groups in owned.get("hooks", {}).items():
                previous["hooks"].setdefault(event, []).extend(
                    group for group in groups if group not in previous["hooks"][event]
                )
        ownership[state] = wanted_hooks
        plans.append((hook_path, api.dump(merge_hooks(live, previous, wanted_hooks))))
    if "rules" in components:
        rules = (
            (api.KIT / "rules/AGENTS.md").read_text()
            + "\n"
            + (api.KIT / f"rules/hosts/{host}.md").read_text()
        )
        rules_path = root / ("AGENTS.md" if host == "codex" else "rules/agent-kit.mdc")
        existing_rules = api.read_or_empty(rules_path)
        if host == "cursor" and not existing_rules:
            existing_rules = "---\ndescription: Personal agent-kit rules\nalwaysApply: true\n---\n"
        plans.append(
            (
                rules_path,
                block(
                    existing_rules,
                    rules,
                    "<!-- BEGIN agent-kit managed -->",
                    "<!-- END agent-kit managed -->",
                ),
            )
        )
    servers = safe_servers(api.KIT) if "mcp" in components else {}
    if host == "codex" and ("mcp" in components or "hooks" in components):
        config_path = root / "config.toml"
        existing = api.read_or_empty(config_path)
        outside = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), "", existing, flags=re.DOTALL)
        outside = shell_without_owned(outside)
        config = tomllib.loads(outside)
        if set(config.get("hooks", {})) & set(EVENTS):
            raise SystemExit(
                "agent-kit: inline Codex hooks would duplicate hooks.json; reconcile them first"
            )
        collisions = sorted(set(config.get("mcp_servers", {})) & set(servers))
        if collisions:
            raise SystemExit(
                "agent-kit: unmanaged MCP names already exist: " + ", ".join(collisions)
            )
        new_config = existing
        if "hooks" in components:
            values = shell_env(api.KIT, host)
            policy = config.get("shell_environment_policy", {})
            filters = policy.get("filters", {})
            if filters and any(key in policy for key in ("include_only", "exclude")):
                raise SystemExit(
                    "agent-kit: shell policy mixes legacy arrays and keyed filters; nothing written"
                )
            include = policy.get("include_only")
            if include is None and filters:
                patterns = [pattern for pattern, action in filters.items() if action == "include"]
                include = patterns or None
            if include is not None:
                from fnmatch import fnmatchcase

                if any(
                    not any(fnmatchcase(key.upper(), pattern.upper()) for pattern in include)
                    for key in values
                ):
                    raise SystemExit(
                        "agent-kit: shell environment include_only excludes required dispatcher keys; nothing written"
                    )
            shell_key = api.state_name("codex-shell", config_path)
            ownership[shell_key] = values
            new_config = shell_config(new_config, values, versions(shell_key))
        if "mcp" in components:
            roots = sandbox_roots()
            if "sandbox_workspace_write" in config:
                # The user's own table stands; a second one would make the file invalid TOML.
                print(
                    "agent-kit: note: your [sandbox_workspace_write] is kept; add these to its "
                    f"writable_roots for task folders: {json.dumps(roots)}",
                    file=sys.stderr,
                )
                roots = []
            managed = toml_servers(servers) + toml_sandbox(roots)
            new_config = block(keep_host_tables(new_config), managed)
        tomllib.loads(new_config)
        plans.append((config_path, new_config))
    if host == "codex" and "roles" in components:
        agent_path = root / "agents"
        desired_agents = role_files(api, roles)
        manifest_key = api.state_name("codex-agents", agent_path, follow=False)
        manifests = versions(manifest_key)
        manifest = {name: text for version in manifests for name, text in version.items()}
        ownership[manifest_key] = desired_agents
        for name, text in desired_agents.items():
            path = agent_path / name
            old = api.read_or_empty(path)
            if old and old != text and not any(old == version.get(name) for version in manifests):
                raise SystemExit(f"agent-kit: {path} has user edits; nothing written")
            plans.append((path, text))
        for name, old in manifest.items():
            if (
                name not in desired_agents
                and api.read_or_empty(agent_path / name)
                and not any(
                    api.read_or_empty(agent_path / name) == version.get(name)
                    for version in manifests
                )
            ):
                raise SystemExit(f"agent-kit: {agent_path / name} has user edits; nothing written")
    elif host == "cursor" and "mcp" in components:
        mcp_path = root / "mcp.json"
        mcp = json.loads(api.read_or_empty(mcp_path) or "{}")
        old_servers = mcp.get("mcpServers", {})
        # Cursor keys are case-sensitive, so a hand-added "Sentry" plus the catalog's "sentry"
        # would register the same server twice. Render under the live spelling instead.
        live_names = {name.lower(): name for name in old_servers}
        servers = {live_names.get(name.lower(), name): spec for name, spec in servers.items()}
        mcp_key = api.state_name("cursor-mcp", mcp_path)
        mcp_versions = versions(mcp_key)
        previous_mcp = {name: spec for version in mcp_versions for name, spec in version.items()}
        ownership[mcp_key] = servers
        for name in set(previous_mcp) - set(servers):
            if name in old_servers and not any(
                old_servers[name] == version.get(name) for version in mcp_versions
            ):
                raise SystemExit(
                    f"agent-kit: retired Cursor MCP server {name} has user edits; nothing written"
                )
            old_servers.pop(name, None)
        for name, spec in servers.items():
            if (
                name in old_servers
                and old_servers[name] != spec
                and not any(old_servers[name] == version.get(name) for version in mcp_versions)
            ):
                raise SystemExit(
                    f"agent-kit: unmanaged Cursor MCP server {name}; reconcile it first"
                )
        mcp["mcpServers"] = {**old_servers, **servers}
        plans.append((mcp_path, api.dump(mcp)))
    for path, text in plans:
        if path.is_symlink():
            raise SystemExit(f"agent-kit: {path} is a user-owned link; nothing written")
    if not args.dry_run:
        for key, desired in ownership.items():
            pending = journal.setdefault(key, [])
            if desired not in pending:
                pending.append(desired)
        api.write_state(journal_key, journal)
    for path, text in plans:
        if api.read_or_empty(path) == text:
            continue
        if args.dry_run:
            print(f"would render {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            api.atomic_write(
                path, text, 0o600 if path.name in ("mcp.json", "config.toml") else 0o644
            )
            print(f"rendered {path}")
    if not args.dry_run:
        if "hooks" in components:
            api.write_state(state, wanted_hooks)
            if host == "codex":
                api.write_state(shell_key, values)
        if host == "codex" and "roles" in components:
            for name in manifest:
                if name not in desired_agents:
                    (agent_path / name).unlink(missing_ok=True)
            api.write_state(manifest_key, desired_agents)
            if roles is not None:
                api.atomic_write(api.STATE / "roles-codex.sh", api.roles_sh(roles, "codex"))
        elif host == "cursor" and "mcp" in components:
            api.write_state(api.state_name("cursor-mcp", mcp_path), servers)
        for key in ownership:
            journal.pop(key, None)
        if journal:
            api.write_state(journal_key, journal)
        else:
            (api.STATE / journal_key).unlink(missing_ok=True)
    return 0


def inventory(
    api: Any, host: str, repo: Path | None = None, components: set[str] | None = None
) -> dict[str, Any]:
    root = Path(os.environ.get("AGENT_KIT_HOST_ROOT", str(Path.home() / f".{host}")))
    config_path = root / ("config.toml" if host == "codex" else "settings.json")
    errors = []
    try:
        config = (
            tomllib.loads(api.read_or_empty(config_path))
            if host == "codex"
            else json.loads(api.read_or_empty(config_path) or "{}")
        )
    except (ValueError, OSError):
        config = {}
        errors.append("unreadable configuration")
    hooks_path = root / ("hooks.json" if host != "claude" else "settings.json")
    try:
        hooks = json.loads(api.read_or_empty(hooks_path) or "{}").get("hooks", {})
    except (ValueError, OSError):
        hooks = {}
        errors.append("unreadable hooks configuration")
    commands = []
    for event, groups in hooks.items():
        for group in groups:
            for hook in group.get("hooks", [group]):
                commands.append((event, group.get("matcher"), hook.get("command", "")))
    duplicate_hooks = [
        {"event": key[0], "matcher": key[1], "count": count}
        for key, count in Counter(commands).items()
        if count > 1
    ]
    if host == "codex" and set(config.get("hooks", {})) & set(EVENTS):
        errors.append("inline hooks and hooks.json are both configured")
    rules = [
        root
        / (
            "CLAUDE.md"
            if host == "claude"
            else "AGENTS.md"
            if host == "codex"
            else "rules/agent-kit.mdc"
        )
    ]
    if repo:
        if host == "codex":
            if (root / "AGENTS.override.md").exists():
                rules[0] = root / "AGENTS.override.md"
            ancestors = list(reversed(repo.resolve().parents)) + [repo.resolve()]
            for folder in ancestors:
                for name in (
                    "AGENTS.override.md",
                    "AGENTS.md",
                    *config.get("project_doc_fallback_filenames", []),
                ):
                    if (folder / name).is_file():
                        rules.append(folder / name)
                        break
        else:
            rules += [repo / n for n in ("AGENTS.md", "CLAUDE.md") if (repo / n).is_file()]
    skills, incompatible = [], []
    for path in sorted((api.KIT / "skills").glob("*/SKILL.md")):
        text = path.read_text()
        match = re.search(r"^hosts:\s*\[([^\]]*)\]", text, re.MULTILINE)
        allowed = (
            [h.strip().strip("\"'") for h in match.group(1).split(",")] if match else ["claude"]
        )
        (skills if host in allowed else incompatible).append(path.parent.name)
    if components is not None and "mcp" not in components:
        mcp = {}
    elif host == "codex":
        mcp = config.get("mcp_servers", {})
    else:
        mcp = json.loads(api.read_or_empty(root / "mcp.json") or "{}").get("mcpServers", {})
    roles = api.load_roles()
    native_selection = roles is not None and roles.hosts.get("codex", {}).get("native_agents", True)
    hooks_enabled = config.get("features", {}).get(
        "hooks", config.get("features", {}).get("codex_hooks", True)
    )
    managed_only = bool(config.get("allow_managed_hooks_only", False))
    return {
        "host": host,
        "repo": str(repo) if repo else None,
        "rules_configured_in_order": [str(p) for p in rules if p.exists()],
        "runtime_rules_proven": False,
        "cursor_rules_activation": "Project .cursor/rules scope or manual global User Rules import; home export is pending activation"
        if host == "cursor"
        else None,
        "shell_dispatcher_proven": False,
        "custom_agent_selection": (
            "enabled in roles; runtime pilot required"
            if native_selection
            else "disabled in roles after unsupported selector pilot; use agent-run"
        )
        if host == "codex"
        else "unverified",
        "subagent_tool_hooks": "unverified",
        "parent_child_session_attribution": "unverified; Codex subagent hooks use the parent session_id, and child shell attribution requires a real runtime comparison",
        "hook_runtime": "configuration only; host trust and runtime pilot required",
        "hooks_enabled_in_user_config": hooks_enabled if host == "codex" else None,
        "managed_hooks_only": managed_only if host == "codex" else None,
        "rotation_ready": False,
        "fallback_host": "claude",
        "hooks": len(commands),
        "duplicate_hooks": duplicate_hooks,
        "skills": skills,
        "incompatible_skills": incompatible,
        "mcp_servers": sorted(mcp),
        "model": config.get("model"),
        "cli_available": bool(shutil.which(host)),
        "shared_db_wrappers": {name: shutil.which(name) for name in ("ro-mysql", "bqro")},
        "database_credentials": "Shared native Keychain/gcloud stores. Retry a sandbox-denied read-only wrapper with approved elevated access before diagnosing missing credentials or requesting rotation. Never copy credential files between hosts.",
        "errors": errors,
        "cursor_usage": "usage export only" if host == "cursor" else None,
    }
