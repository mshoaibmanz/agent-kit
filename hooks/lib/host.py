"""Normalize native hook events to the kit's policy input."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from session import resolve

# The hosts the kit installs into: agent-setup, agent-kit and bin/lib/hosts.py import this one list.
HOSTS = ("claude", "codex", "cursor")

EVENTS = {
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "stop": "Stop",
    "subagentStop": "SubagentStop",
    "sessionStart": "SessionStart",
}


def git_layer_env(
    env: Mapping[str, str], hooks_dir: str, owned: Iterable[str] = (), enabled: bool = True
) -> dict[str, str]:
    """The GIT_CONFIG_* keys that put core.hooksPath=hooks_dir into an agent's shell beside the
    command-scope entries env already holds; with enabled false, the keys that take it out again.

    Git applies the entries in index order and the last core.hooksPath wins, so the layer's entry
    must be the last one: then it wins over a core.hooksPath of the user's and the dispatcher chains
    to that one. Its entry (its value, or a GIT_CONFIG_KEY_n named in owned, an earlier render's;
    the last such) keeps its index while no core.hooksPath follows it, else it becomes a no-op entry
    and the layer's goes after all the others. Removing or moving an entry would renumber the user's
    keys, so a dropped one stays as a no-op instead. Each earlier owned entry below the count is
    returned again as that no-op: a caller that drops the owned keys it no longer gets back would
    otherwise leave a hole below GIT_CONFIG_COUNT, which git rejects. ValueError: a GIT_CONFIG_COUNT
    that git itself would reject.
    """
    raw = str(env.get("GIT_CONFIG_COUNT", "") or "0")
    if not raw.isdigit():
        raise ValueError(f"GIT_CONFIG_COUNT is not a count: {raw!r}")
    count, owned = int(raw), set(owned)

    def hooks_path(i: int) -> bool:
        return env.get(f"GIT_CONFIG_KEY_{i}", "").lower() == "core.hookspath"

    def no_op(*indexes: int) -> dict[str, str]:
        out = {}
        for i in indexes:
            out |= {f"GIT_CONFIG_KEY_{i}": "agent-kit.gitHooks", f"GIT_CONFIG_VALUE_{i}": "off"}
        return out

    mine = [
        i
        for i in range(count)
        if f"GIT_CONFIG_KEY_{i}" in owned or (hooks_path(i) and env.get(f"GIT_CONFIG_VALUE_{i}") == hooks_dir)
    ]
    index = mine[-1] if mine else count
    retired = no_op(*(i for i in mine[:-1] if f"GIT_CONFIG_KEY_{i}" in owned))
    key, value = f"GIT_CONFIG_KEY_{index}", f"GIT_CONFIG_VALUE_{index}"
    if enabled:
        if any(hooks_path(i) for i in range(index + 1, count)):
            return {
                "GIT_CONFIG_COUNT": str(count + 1),
                **retired,
                **no_op(index),
                f"GIT_CONFIG_KEY_{count}": "core.hooksPath",
                f"GIT_CONFIG_VALUE_{count}": hooks_dir,
            }
        return {
            "GIT_CONFIG_COUNT": str(max(count, index + 1)),
            **retired,
            key: "core.hooksPath",
            value: hooks_dir,
        }
    if index == count:
        return {}
    if index + 1 < count:
        return {"GIT_CONFIG_COUNT": str(count), **retired, **no_op(index)}
    return {"GIT_CONFIG_COUNT": str(index), **retired} if index else {}



def beside_user_layer(user: Mapping[str, str], values: dict[str, str]) -> dict[str, str]:
    """values (a shell block git_layer_env laid out over the user's own variables) for a host whose
    own settings (user) already set GIT_CONFIG_COUNT. The block cannot hold a second count, and
    setup never edits the user's lines: the layer's keys are left out once the user's lines carry
    its entry. ValueError naming the lines the user adds while they do not."""
    if "GIT_CONFIG_COUNT" not in user or "GIT_CONFIG_COUNT" not in values:
        return values
    if values["GIT_CONFIG_COUNT"] != user["GIT_CONFIG_COUNT"]:
        index = user["GIT_CONFIG_COUNT"]
        raise ValueError(
            f"your shell_environment_policy.set has its own GIT_CONFIG_COUNT. Add "
            f'GIT_CONFIG_KEY_{index} = "core.hooksPath" and GIT_CONFIG_VALUE_{index} = '
            f'"{values[f"GIT_CONFIG_VALUE_{index}"]}" there, set GIT_CONFIG_COUNT = '
            f'"{values["GIT_CONFIG_COUNT"]}", and rerun'
        )
    return {key: value for key, value in values.items() if not key.startswith("GIT_CONFIG_")}


def detect_host(payload: dict[str, Any], env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if "cursor_version" in payload or "conversation_id" in payload:
        return "cursor"
    if "turn_id" in payload or payload.get("tool_name") == "apply_patch":
        return "codex"
    if env.get("AGENT_HOST") in HOSTS:
        return env["AGENT_HOST"]
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_HOME"):
        return "codex"
    return "claude"


def normalize(payload: dict[str, Any], host: str) -> list[dict[str, Any]]:
    data = dict(payload)
    data["hook_event_name"] = EVENTS.get(
        data.get("hook_event_name"), data.get("hook_event_name", "")
    )
    data["session_id"] = (
        resolve(
            data.get("session_id") or data.get("conversation_id") or "",
            dict(os.environ, AGENT_HOST=host),
        )
        or "unknown"
    )
    roots = data.get("workspace_roots") or []
    data["cwd"] = data.get("cwd") or (roots[0] if roots else os.getcwd())
    data["agent_type"] = data.get("agent_type") or data.get("subagent_type") or ""
    data["agent_id"] = data.get("agent_id") or data.get("subagent_id") or ""
    data["last_assistant_message"] = data.get("last_assistant_message") or data.get("summary") or ""
    data["stop_hook_active"] = data.get("stop_hook_active", bool(data.get("loop_count", 0)))
    tool = data.get("tool_name", "")
    data["tool_name"] = {
        "Shell": "Bash",
        "WriteFile": "Write",
        "EditFile": "Edit",
        "Delete": "Edit",
    }.get(tool, tool)
    inp = data.get("tool_input") or {}
    if isinstance(inp, str):
        import json

        inp = json.loads(inp)
    inp = dict(inp)
    if "path" in inp and "file_path" not in inp:
        inp["file_path"] = inp["path"]
    data["tool_input"] = inp
    if host == "cursor" and tool == "Shell" and inp.get("working_directory"):
        data["cwd"] = str((Path(data["cwd"]) / inp["working_directory"]).resolve())
    if "tool_response" in data or "tool_output" in data:
        import json

        output = data.get("tool_response", data.get("tool_output"))
        if isinstance(output, str):
            try:
                parsed = json.loads(output)
                output = parsed if isinstance(parsed, dict) else {"stdout": output}
            except ValueError:
                output = {"stdout": output}
        data["tool_response"] = output
    if tool != "apply_patch":
        if inp.get("file_path"):
            inp["file_path"] = str(Path(data["cwd"]) / inp["file_path"])
        return [data]
    patch = inp.get("command", "")
    result = []
    for operation, path in patch_paths(patch):
        item = dict(data)
        item["tool_name"] = "Write" if operation == "Add" else "Edit"
        item["tool_input"] = {"file_path": str(Path(data["cwd"]) / path), "patch": patch}
        result.append(item)
    if not result:
        raise ValueError("apply_patch payload has no recognizable file paths")
    return result


def patch_paths(patch: str) -> list[tuple[str, str]]:
    paths = []
    mode = ""
    for raw in patch.split("\n"):
        line = raw.rstrip() if mode == "Update" else raw.strip()
        header = re.fullmatch(r"\*\*\* (Add|Update|Delete) File: (.+)", line)
        if header:
            mode, path = header.groups()
            paths.append((mode, path))
        elif mode == "Update" and line.startswith("*** Move to: "):
            paths.append(("Add", line[len("*** Move to: ") :]))
        elif line == "*** End Patch":
            mode = ""
    return paths


def cursor_output(output: dict[str, Any], event: str) -> dict[str, Any]:
    specific = output.get("hookSpecificOutput") or {}
    reason = specific.get("permissionDecisionReason") or output.get("reason", "")
    if event == "PreToolUse":
        decision = specific.get("permissionDecision")
        return {
            "permission": "deny" if decision in ("deny", "ask") else "allow",
            **({"user_message": reason, "agent_message": reason} if reason else {}),
        }
    if event in ("Stop", "SubagentStop"):
        return {"followup_message": reason} if output.get("decision") == "block" else {}
    context = specific.get("additionalContext") or output.get("systemMessage")
    return {"additional_context": context} if context else {}
