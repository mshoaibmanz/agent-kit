"""`agent-kit mcp describe`: start each catalog server once, ask it what it is (initialize, then
notifications/initialized, then tools/list) and cache its serverInfo, instructions and tools in
<kit>/state/mcp-describe.json, which the dashboard reads. Opt-in: nothing runs it automatically.

stdio servers always run; an http server runs only with static headers, since one without headers
signs in with OAuth in the host ("needs sign-in"). A server gets the env or headers the render
would give it, credentials resolved as the render resolves them. No env or header value is printed
or cached: every one is masked out of what the server says before it is printed or cached, a
failure is cached as its kind only (timeout, exit code, protocol error), never the server's words,
a request carrying headers never follows a redirect, and a server's stderr is discarded. Each
server gets one deadline for its whole exchange. Each stdio server runs in its own process group,
killed when its answer is in."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import select
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

CACHE_NAME = "mcp-describe.json"
PROTOCOL = "2025-06-18"
CLIENT = {"name": "agent-kit-describe", "version": "1"}
GRACE_S = 3
REDACTED = "[redacted]"
MIN_SECRET = 4  # shorter values (a flag, a port) are not masked out of the text


class Unanswered(Exception):
    """The server did not answer in time, or not in JSON-RPC. Its text is a category of ours."""

    status = "error"


class ProtocolError(Unanswered):
    """The server answered, but not with the shapes MCP defines."""

    status = "protocol error"

    def __init__(self) -> None:
        super().__init__("protocol error")


class Described(NamedTuple):
    """One server's cached entry, validated: status is "ok", "error", "needs sign-in",
    "redirected" or "protocol error"; the rest is filled for "ok" only."""

    status: str
    checked: str = ""
    error: str = ""
    server: str = ""  # serverInfo's "name version"
    instructions: str = ""
    tools: tuple[tuple[str, str], ...] = ()  # (name, description)


def cache_path(kit: Path) -> Path:
    return kit / "state" / CACHE_NAME


def stored(path: Path) -> dict[str, Any]:
    """The cache file's servers as written; empty when it is missing or unreadable."""
    try:
        servers = json.loads(path.read_text()).get("servers", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return servers if isinstance(servers, dict) else {}


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def validated(entry: object) -> Described | None:
    """A cache entry as a Described; None when it has no status."""
    if not isinstance(entry, dict) or not isinstance(entry.get("status"), str):
        return None
    info = entry.get("serverInfo") if isinstance(entry.get("serverInfo"), dict) else {}
    tools = entry.get("tools") if isinstance(entry.get("tools"), list) else []
    return Described(
        status=entry["status"],
        checked=text(entry.get("checked")),
        error=text(entry.get("error")),
        server=" ".join(text(info.get(k)) for k in ("name", "version") if text(info.get(k))),
        instructions=text(entry.get("instructions")) or text(info.get("title")),
        tools=tuple((text(t.get("name")), text(t.get("description"))) for t in tools if isinstance(t, dict)),
    )


def read_cache(kit: Path) -> tuple[Path, dict[str, Described]]:
    """(the cache file, each server's validated entry by name); an invalid entry is left out."""
    path = cache_path(kit)
    entries = {name: validated(entry) for name, entry in stored(path).items()}
    return path, {name: entry for name, entry in entries.items() if entry is not None}


def initialize(request_id: int) -> dict[str, Any]:
    params = {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT}
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": params}


INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def is_reply(message: object, request_id: int) -> bool:
    """A response to request_id; a request or notification from the server (it has a method) is not."""
    return isinstance(message, dict) and message.get("id") == request_id and "method" not in message


def answer(init: dict[str, Any], tools: dict[str, Any]) -> dict[str, Any]:
    """The cached entry from the initialize and tools/list replies."""
    for reply in (init, tools):
        if "error" in reply:
            raise Unanswered("JSON-RPC error")
    result, listed = init.get("result", {}), tools.get("result", {})
    if not isinstance(result, dict) or not isinstance(listed, dict):
        raise ProtocolError
    info, instructions, items = result.get("serverInfo", {}), result.get("instructions") or "", listed.get("tools") or []
    if not isinstance(info, dict) or not isinstance(instructions, str) or not isinstance(items, list):
        raise ProtocolError
    return {
        "status": "ok",
        "serverInfo": {k: str(v) for k, v in info.items() if isinstance(v, (str, int, float))},
        "protocolVersion": str(result.get("protocolVersion", "")),
        "instructions": instructions,
        "tools": [
            {"name": str(t.get("name", "")), "description": str(t.get("description") or "")}
            for t in items
            if isinstance(t, dict)
        ],
    }


def secrets_of(spec: dict[str, Any]) -> list[str]:
    """Every env and header value the server gets, and each word of one (a token after `Bearer`),
    longest first so a value is masked before its parts."""
    found: set[str] = set()
    for field in ("env", "headers"):
        values = spec.get(field) if isinstance(spec.get(field), dict) else {}
        for value in values.values():
            found.update(v for v in (str(value), *str(value).split()) if len(v) >= MIN_SECRET)
    return sorted(found, key=len, reverse=True)


def redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    return value


def stop(proc: subprocess.Popen[bytes]) -> None:
    """Close its stdin, SIGTERM its process group (npx and uvx start children), give it the grace
    period, then SIGKILL the group whether or not the leader has exited (a child may ignore
    SIGTERM), and reap the leader."""
    for stream in (proc.stdin,):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()
    if proc.stdout is not None:
        proc.stdout.close()


def describe_stdio(spec: dict[str, Any], deadline: float) -> dict[str, Any]:
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
                if is_reply(message, request_id):
                    return message
            left = deadline - time.monotonic()
            if left <= 0:
                raise Unanswered("timeout")
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an HTTPError (3xx), never a second request carrying the headers elsewhere."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise Unanswered("timeout")
    return left


def lines_until(response: Any, deadline: float) -> Any:
    """The response's lines as they arrive, each read bounded by what is left of the deadline."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    while True:
        left = remaining(deadline)
        if isinstance(sock, socket.socket) and sock.fileno() >= 0:  # closed once a sized body is read
            sock.settimeout(left)
        try:
            line = response.readline(65536)
        except (TimeoutError, socket.timeout):
            raise Unanswered("timeout") from None
        if not line:
            return
        yield line.decode("utf-8", "replace")


def event_reply(response: Any, request_id: int, deadline: float) -> dict[str, Any]:
    """The matching JSON-RPC reply from an event stream, read event by event: it returns as soon as
    the reply arrives, however long the server keeps the stream open."""
    data: list[str] = []
    for line in lines_until(response, deadline):
        line = line.rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line or not data:
            continue
        try:
            reply = json.loads("\n".join(data))
        except ValueError:
            reply = None
        data = []
        if is_reply(reply, request_id):
            return reply
    if data:
        try:
            reply = json.loads("\n".join(data))
        except ValueError:
            reply = None
        if is_reply(reply, request_id):
            return reply
    raise ProtocolError


def post(url: str, headers: dict[str, str], message: dict[str, Any], deadline: float) -> tuple[dict[str, Any], str]:
    """(the JSON-RPC reply, the session id header) of one Streamable HTTP POST."""
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
    with OPENER.open(request, timeout=remaining(deadline)) as response:
        session = response.headers.get("Mcp-Session-Id", "")
        if "id" not in message:
            return {}, session
        if "text/event-stream" in response.headers.get("Content-Type", ""):
            return event_reply(response, message["id"], deadline), session
        body = "".join(lines_until(response, deadline))
    try:
        reply = json.loads(body)
    except ValueError:
        raise ProtocolError from None
    if not is_reply(reply, message["id"]):
        raise ProtocolError
    return reply, session


def describe_http(spec: dict[str, Any], deadline: float) -> dict[str, Any]:
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
    url = str(spec["url"])
    try:
        init, session = post(url, headers, initialize(1), deadline)
        if session:
            headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = str((init.get("result") or {}).get("protocolVersion") or PROTOCOL)
        post(url, headers, INITIALIZED, deadline)
        tools, _ = post(url, headers, TOOLS, deadline)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            return {"status": "needs sign-in"}
        if 300 <= error.code < 400:
            return {"status": "redirected"}
        raise Unanswered(f"HTTP {error.code}") from None
    except (TimeoutError, socket.timeout):
        raise Unanswered("timeout") from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise Unanswered(f"not reached: {type(reason).__name__}") from None
    return answer(init, tools)


def describe(api: ModuleType, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    """One server's entry, every env and header value masked out of it. Credentials resolve here,
    as the render resolves them; a failure names only its kind, never a value."""
    if "url" in spec and not spec.get("headers"):
        return {"status": "needs sign-in"}
    try:
        resolved = api.resolve(spec)
    except SystemExit:
        return {"status": "error", "error": "a Keychain item it needs is missing"}
    deadline = time.monotonic() + timeout
    try:
        if "url" in resolved:
            entry = describe_http(resolved, deadline)
        else:
            entry = describe_stdio(resolved, deadline)
    except ProtocolError:
        return {"status": ProtocolError.status}
    except Unanswered as error:
        return {"status": "error", "error": str(error)}
    except OSError as error:
        return {"status": "error", "error": f"not started: {type(error).__name__}"}
    return redact(entry, secrets_of(resolved))


def cmd_describe(api: ModuleType, args: argparse.Namespace) -> int:
    servers = api.mcp_catalog().get("mcpServers", {})
    names = sorted(servers)
    if args.only:
        unknown = sorted(set(args.only) - set(servers))
        if unknown:
            raise SystemExit(f"agent-kit: not in the catalog: {', '.join(unknown)}")
        names = sorted(set(args.only))
    path = cache_path(Path(api.KIT))
    cached = stored(path)
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
            failed += entry["status"] in ("error", ProtocolError.status)
            print(f"  {entry['status']}" + (f": {entry['error']}" if entry.get("error") else ""))
    for gone in sorted(set(cached) - set(servers)):
        del cached[gone]
    path.parent.mkdir(parents=True, exist_ok=True)
    api.atomic_write(path, json.dumps({"servers": cached}, indent=2, sort_keys=True) + "\n", 0o600)
    print(f"mcp describe: wrote {path}")
    return 1 if failed else 0
