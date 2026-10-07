"""`agent-kit mcp describe` (opt-in): ask each catalog server what it is and cache an allowlist of
the answer (serverInfo name and version, an instructions excerpt, tool names) in
<kit>/state/mcp-describe.json, every resolved credential masked out. An http server runs only with
static headers and never follows a redirect; a failure is cached as its kind only. Each server gets
one deadline for its whole exchange; a stdio server runs in its own process group, killed when its
answer is in. README.md (dashboard) has the full contract."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import http.client
import json
import os
import select
import signal
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, get_args

CACHE_NAME = "mcp-describe.json"
PROTOCOL = "2025-06-18"
CLIENT = {"name": "agent-kit-describe", "version": "1"}
GRACE_S = 3
REDACTED = "[redacted]"
INSTRUCTIONS_MAX = 600  # the dashboard shows a line of it
TOOLS_MAX = 500
NAME_MAX = 200
READ_CHUNK = 4096
BODY_MAX = 4 << 20
AUTH_HEADERS = ("authorization", "proxy-authorization")  # `<scheme> <token>` values

Status = Literal["ok", "error", "needs sign-in", "redirected", "protocol error"]
STATUSES: tuple[str, ...] = get_args(Status)


class Unanswered(Exception):
    """No usable answer. `status` is what the cache records; the text is a category of ours, never
    the server's words."""

    status: Status = "error"


class ProtocolError(Unanswered):
    """The server answered, but not with the shapes MCP defines."""

    status = "protocol error"


class NeedsSignIn(Unanswered):
    """401 or 403: the host signs in with OAuth, or the static headers are wrong."""

    status = "needs sign-in"


class Redirected(Unanswered):
    """A 3xx: never followed, so the headers go nowhere else."""

    status = "redirected"


def clip(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


@dataclasses.dataclass(frozen=True)
class Described:
    """One server's cached entry, the only shape written or read: name, version, instructions and
    tools are filled for "ok" only."""

    status: Status
    checked: str = ""
    error: str = ""
    name: str = ""
    version: str = ""
    instructions: str = ""
    tools: tuple[str, ...] = ()

    @property
    def server(self) -> str:
        return " ".join(v for v in (self.name, self.version) if v)

    def to_json(self) -> dict[str, Any]:
        return {
            k: list(v) if k == "tools" else v
            for k, v in dataclasses.asdict(self).items()
            if v
        }

    @classmethod
    def from_json(cls, entry: object) -> Described | None:
        """The entry as written by to_json, each field type-checked; None without a known status."""
        if not isinstance(entry, dict) or entry.get("status") not in STATUSES:
            return None
        tools = entry.get("tools") if isinstance(entry.get("tools"), list) else []
        return cls(
            status=entry["status"],
            checked=clip(entry.get("checked"), NAME_MAX),
            error=clip(entry.get("error"), NAME_MAX),
            name=clip(entry.get("name"), NAME_MAX),
            version=clip(entry.get("version"), NAME_MAX),
            instructions=clip(entry.get("instructions"), INSTRUCTIONS_MAX),
            tools=tuple(t[:NAME_MAX] for t in tools[:TOOLS_MAX] if isinstance(t, str)),
        )

    def masked(self, credentials: list[str]) -> Described:
        return dataclasses.replace(
            self,
            name=redact(self.name, credentials),
            version=redact(self.version, credentials),
            instructions=redact(self.instructions, credentials),
            tools=tuple(redact(t, credentials) for t in self.tools),
        )


def cache_path(kit: Path) -> Path:
    return kit / "state" / CACHE_NAME


def stored(path: Path) -> dict[str, Any]:
    """The cache file's servers as written; empty when it is missing or unreadable."""
    try:
        servers = json.loads(path.read_text()).get("servers", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return servers if isinstance(servers, dict) else {}


def read_cache(kit: Path) -> tuple[Path, dict[str, Described]]:
    """(the cache file, each server's validated entry by name); an invalid entry is left out."""
    path = cache_path(kit)
    entries = {name: Described.from_json(entry) for name, entry in stored(path).items()}
    return path, {name: entry for name, entry in entries.items() if entry is not None}


def initialize(request_id: int) -> dict[str, Any]:
    params = {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": params,
    }


INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def reply_in(text: str, request_id: int) -> dict[str, Any] | None:
    """The JSON-RPC response to request_id that text holds; None for anything else (a log line, a
    request or notification from the server, which has a method)."""
    try:
        message = json.loads(text)
    except ValueError:
        return None
    if (
        isinstance(message, dict)
        and message.get("id") == request_id
        and "method" not in message
    ):
        return message
    return None


def answer(init: dict[str, Any], tools: dict[str, Any]) -> Described:
    """The allowlisted entry from the initialize and tools/list replies."""
    for reply in (init, tools):
        if "error" in reply:
            raise Unanswered("JSON-RPC error")
    result, listed = init.get("result", {}), tools.get("result", {})
    if not isinstance(result, dict) or not isinstance(listed, dict):
        raise ProtocolError("protocol error")
    info, instructions, items = (
        result.get("serverInfo", {}),
        result.get("instructions") or "",
        listed.get("tools") or [],
    )
    if (
        not isinstance(info, dict)
        or not isinstance(instructions, str)
        or not isinstance(items, list)
    ):
        raise ProtocolError("protocol error")
    return Described(
        status="ok",
        name=clip(info.get("name"), NAME_MAX),
        version=clip(info.get("version"), NAME_MAX),
        instructions=clip(instructions, INSTRUCTIONS_MAX),
        tools=tuple(
            clip(t.get("name"), NAME_MAX)
            for t in items[:TOOLS_MAX]
            if isinstance(t, dict)
        ),
    )


def credentials_of(spec: Any, resolved: Any, field: str = "") -> list[str]:
    """Every resolved credential value, longest first so a value is masked before its parts: what a
    `$keychain` reference in spec resolved to, every header value whole, and a header's token after
    an Authorization scheme (`Bearer x`: x). Whole values only, whatever their length; a plain env value is
    not a credential."""
    found: set[str] = set()
    if isinstance(spec, dict) and set(spec) == {"$keychain"}:
        found.add(str(resolved))
    elif isinstance(spec, dict) and isinstance(resolved, dict):
        for key, value in resolved.items():
            found.update(
                credentials_of(spec.get(key), value, key if not field else field)
            )
            if field == "headers" and isinstance(value, str):
                found.add(value)
                scheme, _, token = value.strip().partition(" ")
                if key.lower() in AUTH_HEADERS and token.strip() and scheme.isalpha():
                    found.add(token.strip())
    elif isinstance(spec, list) and isinstance(resolved, list):
        for s, r in zip(spec, resolved):
            found.update(credentials_of(s, r, field))
    found.discard("")
    return sorted(found, key=len, reverse=True)


def redact(value: str, credentials: list[str]) -> str:
    for credential in credentials:
        value = value.replace(credential, REDACTED)
    return value


def stop(proc: subprocess.Popen[bytes]) -> None:
    """Close its stdin, SIGTERM its process group (npx and uvx start children), give it the grace
    period, then SIGKILL the group whether or not the leader has exited (a child may ignore
    SIGTERM), and reap the leader."""
    if proc.stdin is not None:
        with contextlib.suppress(OSError):
            proc.stdin.close()
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=GRACE_S)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    if proc.stdout is not None:
        proc.stdout.close()


def remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise Unanswered("timeout")
    return left


def describe_stdio(spec: dict[str, Any], deadline: float) -> Described:
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
                reply = reply_in(line.decode("utf-8", "replace"), request_id)
                if reply is not None:
                    return reply
            ready, _, _ = select.select(
                [proc.stdout], [], [], min(remaining(deadline), 1)
            )
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


@contextlib.contextmanager
def watchdog(conn: http.client.HTTPConnection, deadline: float) -> Iterator[None]:
    """Shut the connection's socket down at the deadline, so a read that keeps getting a byte at a
    time (headers included) cannot outlast it."""

    def cut() -> None:
        if conn.sock is not None:
            with contextlib.suppress(OSError):
                conn.sock.shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(max(deadline - time.monotonic(), 0), cut)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def chunks(
    conn: http.client.HTTPConnection,
    response: http.client.HTTPResponse,
    deadline: float,
) -> Iterator[bytes]:
    """The body in reads of at most READ_CHUNK bytes, the deadline checked before each and the socket
    timeout set to what is left of it."""
    total = 0
    while True:
        left = remaining(deadline)
        if conn.sock is not None:
            with contextlib.suppress(OSError):
                conn.sock.settimeout(left)
        chunk = response.read1(READ_CHUNK)
        if not chunk:
            remaining(
                deadline
            )  # a cut by the watchdog reads as the end: it is the timeout
            return
        total += len(chunk)
        if total > BODY_MAX:
            raise ProtocolError("protocol error")
        yield chunk


def lines(blocks: Iterator[bytes]) -> Iterator[str]:
    buffer = b""
    for block in blocks:
        buffer += block
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8", "replace").rstrip("\r")
    if buffer:
        yield buffer.decode("utf-8", "replace").rstrip("\r")


def event_reply(blocks: Iterator[bytes], request_id: int) -> dict[str, Any]:
    """The matching JSON-RPC reply from an event stream, read event by event: it returns as soon as
    the reply arrives, however long the server keeps the stream open."""
    data: list[str] = []
    for line in _with_end(lines(blocks)):
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line or not data:
            continue
        reply = reply_in("\n".join(data), request_id)
        data = []
        if reply is not None:
            return reply
    raise ProtocolError("protocol error")


def _with_end(source: Iterator[str]) -> Iterator[str]:
    """source, then a blank line: a stream that ends mid-event still dispatches it."""
    yield from source
    yield ""


def connection(url: str, deadline: float) -> tuple[http.client.HTTPConnection, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Unanswered("not reached: a URL that is not http(s)")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname,
            parts.port,
            timeout=remaining(deadline),
            context=ssl.create_default_context(),
        )
    else:
        conn = http.client.HTTPConnection(
            parts.hostname, parts.port, timeout=remaining(deadline)
        )
    return conn, path


def post(
    url: str, headers: dict[str, str], message: dict[str, Any], deadline: float
) -> tuple[dict[str, Any], str]:
    """(the JSON-RPC reply, the session id header) of one Streamable HTTP POST."""
    conn, path = connection(url, deadline)
    body = json.dumps(message).encode()
    sent = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "agent-kit-describe/1",
        **headers,
    }
    try:
        with watchdog(conn, deadline):
            conn.request("POST", path, body=body, headers=sent)
            try:
                response = conn.getresponse()
            except (http.client.HTTPException, OSError):
                remaining(deadline)
                raise
            if response.status in (401, 403):
                raise NeedsSignIn("needs sign-in")
            if 300 <= response.status < 400:
                raise Redirected("redirected")
            if not 200 <= response.status < 300:
                raise Unanswered(f"HTTP {response.status}")
            session = response.getheader("Mcp-Session-Id", "") or ""
            if "id" not in message:
                return {}, session
            blocks = chunks(conn, response, deadline)
            if "text/event-stream" in (response.getheader("Content-Type") or ""):
                return event_reply(blocks, message["id"]), session
            reply = reply_in(b"".join(blocks).decode("utf-8", "replace"), message["id"])
    finally:
        conn.close()
    if reply is None:
        raise ProtocolError("protocol error")
    return reply, session


def describe_http(spec: dict[str, Any], deadline: float) -> Described:
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
    url = str(spec["url"])
    try:
        init, session = post(url, headers, initialize(1), deadline)
        if session:
            headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = str(
            (init.get("result") or {}).get("protocolVersion") or PROTOCOL
        )
        post(url, headers, INITIALIZED, deadline)
        tools, _ = post(url, headers, TOOLS, deadline)
    except (TimeoutError, socket.timeout):
        raise Unanswered("timeout") from None
    except (http.client.HTTPException, OSError) as error:
        raise Unanswered(f"not reached: {type(error).__name__}") from None
    return answer(init, tools)


def describe(api: ModuleType, spec: dict[str, Any], timeout: float) -> Described:
    """One server's entry, every resolved credential masked out of it. Credentials resolve here, as
    the render resolves them; a failure names only its kind, never a value."""
    if "url" in spec and not spec.get("headers"):
        return Described("needs sign-in")
    try:
        resolved = api.resolve(spec)
    except SystemExit:
        return Described("error", error="a Keychain item it needs is missing")
    deadline = time.monotonic() + timeout
    try:
        entry = (
            describe_http(resolved, deadline)
            if "url" in resolved
            else describe_stdio(resolved, deadline)
        )
    except Unanswered as error:
        return Described(
            error.status, error=str(error) if error.status == "error" else ""
        )
    except OSError as error:
        return Described("error", error=f"not started: {type(error).__name__}")
    return entry.masked(credentials_of(spec, resolved))


def cmd_describe(api: ModuleType, args: argparse.Namespace) -> int:
    servers = api.mcp_catalog().get("mcpServers", {})
    names = sorted(servers)
    if args.only:
        unknown = sorted(set(args.only) - set(servers))
        if unknown:
            raise SystemExit(f"agent-kit: not in the catalog: {', '.join(unknown)}")
        names = sorted(set(args.only))
    path = cache_path(Path(api.KIT))
    cached = {
        name: entry.to_json()
        for name, entry in read_cache(Path(api.KIT))[1].items()
        if name in servers
    }
    failed = 0
    for name in names:
        spec = servers[name] if isinstance(servers[name], dict) else {}
        print(f"mcp describe: {name} ...", flush=True)
        entry = dataclasses.replace(
            describe(api, spec, args.timeout),
            checked=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        cached[name] = entry.to_json()
        if entry.status == "ok":
            print(f"  ok: {entry.server or '?'}, {len(entry.tools)} tools")
        else:
            failed += entry.status in ("error", "protocol error")
            print(f"  {entry.status}" + (f": {entry.error}" if entry.error else ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    api.atomic_write(
        path, json.dumps({"servers": cached}, indent=2, sort_keys=True) + "\n", 0o600
    )
    print(f"mcp describe: wrote {path}")
    return 1 if failed else 0
