"""Kit text as installed: the {{NAME}} placeholders, what they name (the kit, its pack, each host's
rules file, the subagent resume limit), and the markdown and MCP commands filled with them."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks/lib"))
from kit_env import code_dir, kit_env  # noqa: E402

PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
PACK_DIR = "pack"  # the installed kit's copy of its team pack: {{PACK_DIR}}
# The line of a kit SKILL.md that install_text replaces with the installed skills extending it.
EXTENSIONS_MARKER = "<!-- agent-kit: extensions -->"
SUBAGENT_RESUME_MAX = 300000
# Each host's global instructions file under its config root, the one the kit's rules reach.
RULES_FILES = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "cursor": "rules/agent-kit.mdc"}
# The variable each host reads for its config root, when it has one.
HOST_HOME_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


def default_host_root(host: str) -> Path:
    """The host's config root when nothing names another: its own variable, else ~/.<host>."""
    configured = os.environ.get(HOST_HOME_ENV.get(host, ""))
    return Path(configured or Path.home() / f".{host}").expanduser().absolute()


def resume_max(raw: str | None = None) -> int:
    """SUBAGENT_RESUME_MAX (kit.env, default 300000): context tokens past which a finished subagent
    is replaced, not resumed. Anything but empty or a positive integer refuses, since rule text
    carries it."""
    raw = (kit_env()["SUBAGENT_RESUME_MAX"] if raw is None else raw).strip()
    if not raw:
        return SUBAGENT_RESUME_MAX
    if not raw.isdigit() or int(raw) <= 0:
        raise SystemExit(f"agent-kit: SUBAGENT_RESUME_MAX={raw!r} is not a positive token count")
    return int(raw)


def rules_file(host: str, host_root: str | None = None) -> str:
    """The host's global instructions file, under default_host_root when no root is named."""
    host_root = str(default_host_root(host)) if host_root is None else host_root
    return f"{host_root.rstrip('/')}/{RULES_FILES[host]}"


def fill(
    text: str,
    kit: str,
    host: str | None = None,
    host_root: str | None = None,
    what: str = "text",
    resume: int | None = None,
    strict: bool = True,
    extra: dict[str, str] | None = None,
) -> str:
    """text with every {{NAME}} filled: AGENT_KIT_DIR and KIT_DIR (the kit), SKILLS_DIR, OVERLAY_DIR
    (<kit>/local), PACK_DIR (<kit>/pack), RULES_FILE (the host's instructions file; host text only),
    SUBAGENT_RESUME_MAX_K, and extra (an MCP command's CODE_DIR). An unknown one refuses: a literal
    {{...}} would reach the model as a path it cannot open. Not strict (a team pack's text, where
    {{...}} is often a CI or template example), an unknown one stays as written."""
    names = {match.group(1) for match in PLACEHOLDER.finditer(text)}
    if not names:
        return text
    values = {"AGENT_KIT_DIR": kit, "KIT_DIR": kit, "SKILLS_DIR": f"{kit}/skills", "OVERLAY_DIR": f"{kit}/local"}
    values["PACK_DIR"] = f"{kit}/{PACK_DIR}"
    values.update(extra or {})
    if host:
        values["RULES_FILE"] = rules_file(host, host_root)
    if "SUBAGENT_RESUME_MAX_K" in names:
        limit = resume_max() if resume is None else resume
        values["SUBAGENT_RESUME_MAX_K"] = f"{limit // 1000}" if limit % 1000 == 0 else f"{limit / 1000:g}"
    bad = sorted("{{" + name + "}}" for name in names - set(values))
    if bad and strict:
        known = [*values, *({"RULES_FILE", "SUBAGENT_RESUME_MAX_K"} - values.keys())]
        raise SystemExit(
            f"agent-kit: {what}: unknown placeholder {', '.join(bad)}"
            + (f" for host {host}" if host else " (host-neutral text)")
            + "; known: " + ", ".join("{{" + name + "}}" for name in known)
        )
    return PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), text)


CATALOG_ONLY = ("credentials", "description")


def fill_servers(servers: dict[str, Any], kit: str, code: str | None = None) -> dict[str, Any]:
    """servers as a host starts them: each command and args filled (fill) for the kit at <kit>, and
    the catalog's `credentials` and `description` (read by the dashboard only) dropped. A host starts
    an MCP command as written, without expanding ~ or a variable, so a preset names a wrapper the kit
    ships as {{KIT_DIR}}/bin/sentry-mcp, and a server run from a local clone as
    {{CODE_DIR}}/<repo>/<path> (code: that folder, else code_dir() reads the overlay)."""
    out = {}
    for name, spec in servers.items():
        if isinstance(spec, dict):
            what = f"MCP server {name}"
            spec = {key: value for key, value in spec.items() if key not in CATALOG_ONLY}
            extra: dict[str, str] = {}
            if "{{CODE_DIR}}" in json.dumps(spec):
                extra["CODE_DIR"] = code_dir() if code is None else code
                if not extra["CODE_DIR"]:
                    raise SystemExit(f"agent-kit: {what}: {{{{CODE_DIR}}}} needs a repository root (CODE_DIRS_JSON)")
            if isinstance(spec.get("command"), str):
                spec["command"] = fill(spec["command"], kit, what=what, extra=extra)
            if isinstance(spec.get("args"), list):
                spec["args"] = [
                    fill(arg, kit, what=what, extra=extra) if isinstance(arg, str) else arg for arg in spec["args"]
                ]
        out[name] = spec
    return out


def install_text(
    text: str,
    kit: str,
    skill: str | None = None,
    export: bool = True,
    host: str | None = None,
    host_root: str | None = None,
    resume: int | None = None,
    strict: bool = True,
    extensions: Sequence[str] = (),
) -> str:
    """A kit markdown file as installed with its kit at <kit>: its {{...}} placeholders filled (fill,
    strict or not), its EXTENSIONS_MARKER line replaced by extensions (the installed skills extending
    it, pack.extension_lists), each ```sh block first exports AGENT_KIT_DIR (with export), and
    ${CLAUDE_SKILL_DIR}, which only Claude Code expands, names <kit>/skills/<skill> as one shell word.
    A kit given as a shell expression (${CLAUDE_PLUGIN_ROOT}/kit) is double-quoted so it still expands."""
    word = f'"{kit}"' if "$" in kit else shlex.quote(kit)
    text = fill(text, kit, host, host_root, skill or "kit text", resume, strict)
    text = text.replace(
        EXTENSIONS_MARKER, "\n".join(extensions) or f"No installed skill extends {skill or 'this one'}."
    )
    if export:
        text = text.replace("```sh\n", f"```sh\nexport AGENT_KIT_DIR={word}\n")
    if skill:
        text = text.replace("${CLAUDE_SKILL_DIR}", shlex.quote(f"{kit}/skills/{skill}"))
    return text
