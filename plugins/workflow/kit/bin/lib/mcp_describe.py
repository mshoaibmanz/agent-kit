"""`agent-kit mcp describe`: start each catalog server once, ask it what it is (initialize, then
notifications/initialized, then tools/list) and cache its serverInfo, instructions and tools in
<kit>/state/mcp-describe.json, which the dashboard reads. Opt-in: nothing runs it automatically.

stdio servers always run; an http server runs only with static headers, since one without headers
signs in with OAuth in the host ("needs sign-in"). A server gets the env or headers the render
would give it, credentials resolved as the render resolves them; neither is ever printed or cached,
and a server's stderr is discarded. Each stdio server runs in its own process group, killed when
its answer is in."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import select
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

CACHE_NAME = "mcp-describe.json"
PROTOCOL = "2025-06-18"
CLIENT = {"name": "agent-kit-describe", "version": "1"}


class Unanswered(Exception):
    """The server did not answer in time, or not in JSON-RPC."""


def cache_path(kit: Path) -> Path:
    return kit / "state" / CACHE_NAME


def read_cache(kit: Path) -> tuple[Path, dict[str, Any]]:
    """(the cache file, its servers by name); empty when it is missing or unreadable."""
    path = cache_path(kit)
    try:
        servers = json.loads(path.read_text()).get("servers", {})
    except (OSError, ValueError, AttributeError):
        return path, {}
    return path, servers if isinstance(servers, dict) else {}


def initialize(request_id: int) -> dict[str, Any]:
    params = {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT}
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": params}


INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def answer(init: dict[str, Any], tools: dict[str, Any]) -> dict[str, Any]:
    """The cached entry from the initialize and tools/list replies."""
    for reply in (init, tools):
        if "error" in reply:
            message = reply["error"].get("message", "") if isinstance(reply["error"], dict) else ""
            raise Unanswered(f"JSON-RPC error: {str(message)[:200]}")
    result = init.get("result") or {}
    listed = (tools.get("result") or {}).get("tools") or []
    return {
        "status": "ok",
        "serverInfo": result.get("serverInfo") or {},
        "protocolVersion": result.get("protocolVersion", ""),
        "instructions": result.get("instructions") or "",
        "tools": [
            {"name": str(t.get("name", "")), "description": str(t.get("description") or "")}
            for t in listed
            if isinstance(t, dict)
        ],
    }


def stop(proc: subprocess.Popen[bytes]) -> None:
    """Kill the server's whole process group (npx and uvx start children), then reap it."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.wait(timeout=3)
            break
        except subprocess.TimeoutExpired:
            continue
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def describe_stdio(spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    argv = [str(spec["command"]), *[str(a) for a in spec.get("args") or []]]
    env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}}
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    deadline = time.monotonic() + timeout
    buffer = b""

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()

    def receive(request_id: int) -> dict[str, Any]:
        nonlocal buffer
        assert proc.stdout is not None
        while True:
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # a log line on stdout
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
            left = deadline - time.monotonic()
            if left <= 0:
                raise Unanswered(f"no answer in {timeout:g}s")
            ready, _, _ = select.select([proc.stdout], [], [], min(left, 1))
            if ready:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise Unanswered(f"exited ({proc.poll()}) before answering")
                buffer += chunk

    try:
        send(initialize(1))
        init = receive(1)
        send(INITIALIZED)
        send(TOOLS)
        return answer(init, receive(2))
    except OSError:  # a closed pipe: the server exited
        raise Unanswered(f"exited ({proc.poll()}) before answering") from None
    finally:
        stop(proc)


def post(url: str, headers: dict[str, str], message: dict[str, Any], timeout: float) -> tuple[dict[str, Any], str]:
    """(the JSON-RPC reply, the session id header) of one Streamable HTTP POST; a reply sent as an
    event stream is read from its data lines."""
    request = urllib.request.Request(
        url,
        data=json.dumps(message).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "agent-kit-describe/1",
            **headers,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        session = response.headers.get("Mcp-Session-Id", "")
        body = response.read().decode("utf-8", "replace")
        if "id" not in message:
            return {}, session
        if "text/event-stream" in response.headers.get("Content-Type", ""):
            for line in body.splitlines():
                if line.startswith("data:"):
                    try:
                        reply = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(reply, dict) and reply.get("id") == message["id"]:
                        return reply, session
            raise Unanswered("no reply in the event stream")
        try:
            reply = json.loads(body)
        except ValueError:
            raise Unanswered("the reply is not JSON") from None
        return reply if isinstance(reply, dict) else {}, session


def describe_http(spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
    url = str(spec["url"])
    try:
        init, session = post(url, headers, initialize(1), timeout)
        if session:
            headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = str((init.get("result") or {}).get("protocolVersion") or PROTOCOL)
        post(url, headers, INITIALIZED, timeout)
        tools, _ = post(url, headers, TOOLS, timeout)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return {"status": "needs sign-in"}
        raise Unanswered(f"HTTP {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise Unanswered(f"not reached: {type(reason).__name__}") from None
    return answer(init, tools)


def describe(api: ModuleType, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    """One server's entry. Credentials resolve here, as the render resolves them; a failure names
    only its kind, never a value."""
    if "url" in spec and not spec.get("headers"):
        return {"status": "needs sign-in"}
    try:
        resolved = api.resolve(spec)
    except SystemExit:
        return {"status": "error", "error": "a Keychain item it needs is missing"}
    try:
        if "url" in resolved:
            return describe_http(resolved, timeout)
        return describe_stdio(resolved, timeout)
    except Unanswered as error:
        return {"status": "error", "error": str(error)}
    except OSError as error:
        return {"status": "error", "error": f"not started: {type(error).__name__}"}


def cmd_describe(api: ModuleType, args: argparse.Namespace) -> int:
    servers = api.mcp_catalog().get("mcpServers", {})
    names = sorted(servers)
    if args.only:
        unknown = sorted(set(args.only) - set(servers))
        if unknown:
            raise SystemExit(f"agent-kit: not in the catalog: {', '.join(unknown)}")
        names = sorted(set(args.only))
    path, cached = read_cache(Path(api.KIT))
    failed = 0
    for name in names:
        spec = servers[name] if isinstance(servers[name], dict) else {}
        print(f"mcp describe: {name} ...", flush=True)
        entry = describe(api, spec, args.timeout)
        entry["checked"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        cached[name] = entry
        if entry["status"] == "ok":
            info = entry["serverInfo"]
            print(f"  ok: {info.get('name', '?')} {info.get('version', '')}, {len(entry['tools'])} tools")
        else:
            failed += entry["status"] == "error"
            print(f"  {entry['status']}" + (f": {entry['error']}" if entry.get("error") else ""))
    for gone in sorted(set(cached) - set(servers)):
        del cached[gone]
    path.parent.mkdir(parents=True, exist_ok=True)
    api.atomic_write(path, json.dumps({"servers": cached}, indent=2, sort_keys=True) + "\n", 0o600)
    print(f"mcp describe: wrote {path}")
    return 1 if failed else 0
