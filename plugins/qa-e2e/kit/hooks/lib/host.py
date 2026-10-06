"""Normalize native hook events to the kit's policy input."""

from __future__ import annotations

import os
import re
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
