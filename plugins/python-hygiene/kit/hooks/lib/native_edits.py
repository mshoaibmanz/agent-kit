"""Check native user edits against only the kit-owned parts of host files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

MARKERS = (
    ("# BEGIN agent-kit managed", "# END agent-kit managed"),
    ("# BEGIN agent-kit shell", "# END agent-kit shell"),
    ("<!-- BEGIN agent-kit managed -->", "<!-- END agent-kit managed -->"),
)


def state_key(kind: str, target: Path) -> str:
    real = str(target.parent.resolve() / target.name)
    if sys.platform == "darwin":
        real = real.casefold()
    return f"{kind}.{hashlib.sha256(real.encode()).hexdigest()[:12]}.json"


def ownership(kit: Path, host: str, root: Path, kind: str, target: Path) -> list[dict[str, Any]]:
    folder = kit / "state/rendered"
    key = state_key(kind, target)
    path = folder / key
    previous = json.loads(path.read_text()) if path.exists() else {}
    journal = folder / state_key(f"{host}-journal", root)
    pending = json.loads(journal.read_text()).get(key, []) if journal.exists() else []
    return [previous, *pending]


def managed_blocks(text: str) -> list[str]:
    result = []
    for begin, end in MARKERS:
        if text.count(begin) != text.count(end) or text.count(begin) > 1:
            raise ValueError("Malformed ownership markers")
        if begin in text:
            result.append(text[text.index(begin) : text.index(end) + len(end)])
    return result


def proposed(text: str, path: Path, inp: dict[str, Any], cwd: str) -> str | None:
    if isinstance(inp.get("content"), str):
        return inp["content"]
    if isinstance(inp.get("old_string"), str) and isinstance(inp.get("new_string"), str):
        old = inp["old_string"]
        if not old:
            return None
        return text.replace(old, inp["new_string"], -1 if inp.get("replace_all") else 1)
    patch = inp.get("patch") or inp.get("command")
    if not isinstance(patch, str):
        return None
    from host import patch_paths

    def matches_path(name: str) -> bool:
        candidate = Path(os.path.abspath(Path(cwd) / name))
        first, second = str(candidate), str(path)
        return (
            first.casefold() == second.casefold() if sys.platform == "darwin" else first == second
        )

    operations = [operation for operation, name in patch_paths(patch) if matches_path(name)]
    if operations != ["Update"]:
        return None
    body = []
    active = False
    mode = ""
    for raw in patch.split("\n"):
        line = raw.rstrip() if mode == "Update" else raw.strip()
        header = re.fullmatch(r"\*\*\* (Add|Update|Delete) File: (.+)", line)
        if header:
            mode, name = header.groups()
            active = matches_path(name)
            continue
        if line == "*** End Patch":
            active = False
            mode = ""
        if active:
            body.append(raw.rstrip("\r"))
    chunks: list[list[str]] = []
    for line in body:
        if line.startswith("@@"):
            # Anchors and multiple hunks require the native forward-search model. Refuse
            # these forms on owned files instead of trusting an approximate projection.
            if line != "@@" or chunks:
                return None
            chunks.append([])
        elif line.startswith("***"):
            return None
        elif chunks and line.startswith((" ", "+", "-")):
            chunks[-1].append(line)
        else:
            return None
    current = text.split("\n")
    if current and current[-1] == "":
        current.pop()
    for chunk in chunks:
        old = [line[1:] for line in chunk if line[0] in " -"]
        new = [line[1:] for line in chunk if line[0] in " +"]
        if not old:
            return None
        matches = []
        for normalize in (lambda value: value, str.rstrip, str.strip):
            matches = [
                offset
                for offset in range(len(current) - len(old) + 1)
                if [normalize(value) for value in current[offset : offset + len(old)]]
                == [normalize(value) for value in old]
            ]
            if matches:
                break
        if len(matches) != 1:
            return None
        offset = matches[0]
        current[offset : offset + len(old)] = new
    if not chunks:
        return None
    return "\n".join(current) + ("\n" if text.endswith("\n") else "")


def changes_owned(
    path: Path, inp: dict[str, Any], cwd: str, kit: Path, host: str, root: Path
) -> bool:
    text = path.read_text() if path.exists() else ""
    name = path.name.casefold() if sys.platform == "darwin" else path.name
    parent, agents = str(path.parent.resolve()), str((root / "agents").resolve())
    if sys.platform == "darwin":
        parent, agents = parent.casefold(), agents.casefold()
    if host == "codex" and parent == agents and name.endswith(".toml"):
        return any(
            name in {value.casefold() if sys.platform == "darwin" else value for value in version}
            for version in ownership(kit, host, root, "codex-agents", root / "agents")
        )
    if name in (
        "config.toml",
        "agents.md" if sys.platform == "darwin" else "AGENTS.md",
        "agent-kit.mdc",
    ):
        before = managed_blocks(text)
        if not before:
            return False
        after = proposed(text, path, inp, cwd)
        return (
            after is None
            or managed_blocks(after) != before
            or (name == "config.toml" and toml_tables_changed(text, after, before))
        )
    if name not in ("hooks.json", "mcp.json"):
        return False
    kind = f"{host}-hooks" if name == "hooks.json" else "cursor-mcp"
    versions = ownership(kit, host, root, kind, path)
    if not any(versions):
        return False
    after = proposed(text, path, inp, cwd)
    if after is None:
        return True
    previous, wanted = json.loads(text or "{}"), json.loads(after)
    if name == "mcp.json":
        owned = {name for version in versions for name in version}
        return any(
            previous.get("mcpServers", {}).get(name) != wanted.get("mcpServers", {}).get(name)
            for name in owned
        )
    for version in versions:
        for event, groups in version.get("hooks", {}).items():
            old_groups, new_groups = (
                previous.get("hooks", {}).get(event, []),
                wanted.get("hooks", {}).get(event, []),
            )
            for group in groups:
                if old_groups.count(group) != new_groups.count(group):
                    return True
    return False


def public_records(kit: Path, target: Path) -> list[dict[str, Any]]:
    """Discover the public installer's explicit target records, including project rules."""
    state = kit / ".install-state/current.json"
    if not state.exists():
        return []
    entries = json.loads(state.read_text()).get("managed", {})
    if not isinstance(entries, dict):
        raise ValueError("Malformed public ownership state")
    actual = str(target.parent.resolve() / target.name)
    if sys.platform == "darwin":
        actual = actual.casefold()
    result = []
    for entry in entries.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("target"), str):
            raise ValueError("Malformed public ownership entry")
        path = Path(entry["target"])
        recorded = str(path.parent.resolve() / path.name)
        if sys.platform == "darwin":
            recorded = recorded.casefold()
        if recorded == actual:
            result.append(entry)
    return result


def public_changes_owned(
    path: Path, inp: dict[str, Any], cwd: str, records: list[dict[str, Any]]
) -> bool:
    text = path.read_text() if path.exists() else ""
    after = proposed(text, path, inp, cwd)
    if after is None:
        return True
    for record in records:
        kind, owned = record.get("kind"), record.get("owned")
        if kind == "link":
            return True
        if kind == "file":
            if hashlib.sha256(after.encode()).hexdigest() != record.get("hash"):
                return True
        elif kind in ("text", "toml"):
            if not isinstance(owned, str) or not owned:
                raise ValueError("Malformed owned block")
            if text.count(owned) != 1 or after.count(owned) != 1:
                return True
            if kind == "toml" and toml_tables_changed(text, after, [owned]):
                return True
        elif kind in ("hooks", "cursor-hooks", "servers", "environment"):
            previous, wanted = json.loads(text or "{}"), json.loads(after)
            if not isinstance(owned, dict):
                raise ValueError("Malformed owned native entries")
            if kind in ("servers", "environment"):
                field = "mcpServers" if kind == "servers" else "env"
                for name in owned:
                    if previous.get(field, {}).get(name) != wanted.get(field, {}).get(name):
                        return True
            else:
                for event, groups in owned.items():
                    for group in groups:
                        if previous.get("hooks", {}).get(event, []).count(group) != wanted.get(
                            "hooks", {}
                        ).get(event, []).count(group):
                            return True
        else:
            raise ValueError("Unsupported public ownership kind")
    return False


def toml_tables_changed(before: str, after: str, blocks: list[str]) -> bool:
    """TOML comments do not end tables; compare owned entries in their actual tables."""
    old, new = tomllib.loads(before), tomllib.loads(after)
    for block in blocks:
        owned = tomllib.loads(block)
        for name in owned.get("mcp_servers", {}):
            if old.get("mcp_servers", {}).get(name) != new.get("mcp_servers", {}).get(name):
                return True
        for key in owned.get("shell_environment_policy", {}).get("set", {}):
            if old.get("shell_environment_policy", {}).get("set", {}).get(
                key
            ) != new.get("shell_environment_policy", {}).get("set", {}).get(key):
                return True
    return False
