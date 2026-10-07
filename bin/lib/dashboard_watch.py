"""`agent-kit dashboard --watch`: serve the page on 127.0.0.1 and regenerate it when a config file
changes. Polls mtimes every POLL_SECONDS (no file-watching dependency); the served copy carries a
small script that reloads the open page when a newer one is ready. The page written to disk stays the
self-contained one."""

from __future__ import annotations

import html
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hosts import RENDERED_FILES

POLL_SECONDS = 2
# Kit files the page reads; a host root's own config files by name (its other files change on every
# prompt, history and caches among them).
KIT_FILES = (
    ".install-state/current.json",
    "mcp/servers.json",
    "mcp/sentry-instances.json",
    "roles.toml",
    "hooks/registry.json",
    "state/mcp-describe.json",
    "pack/agent-kit-preset.toml",
)
RELOAD_JS = (
    "(function(){var v=document.currentScript.getAttribute('data-version');"
    "function poll(){fetch('/version',{cache:'no-store'}).then(function(r){return r.text()})"
    f".then(function(t){{if(t!==v){{location.reload()}}else{{setTimeout(poll,{POLL_SECONDS * 1000})}}}},"
    f"function(){{setTimeout(poll,{POLL_SECONDS * 1000})}})}}setTimeout(poll,{POLL_SECONDS * 1000})}})();\n"
)
# Host files the page reads that setup does not render: Claude's own local settings.
HOST_EXTRAS = {"claude": ("settings.local.json",)}
POLICY = re.compile(r'(http-equiv="Content-Security-Policy" content=")([^"]*)(")')

Fingerprint = dict[str, tuple[int, int]]


def watched(kit: Path, layers: Iterable[Path], host_roots: Mapping[str, Path]) -> list[Path]:
    """The config files the page is generated from: the kit's own; the overlay layers kit_env reads
    (kit_env.layers()), each with its folder and that folder's top-level files, and the kit's local/
    the same way (a folder's own mtime moves when a file is added or removed); and each configured
    host's rendered files plus its HOST_EXTRAS."""
    layers = list(layers)
    paths = [kit / name for name in KIT_FILES] + layers
    for folder in sorted({kit / "local", *(layer.parent for layer in layers)}):
        paths.append(folder)
        try:
            paths += sorted(p for p in folder.iterdir() if p.is_file())
        except OSError:
            pass
    for host, root in sorted(host_roots.items()):
        paths += [root / name for name in (*RENDERED_FILES[host], *HOST_EXTRAS.get(host, ()))]
    return paths


def fingerprint(paths: Iterable[Path]) -> Fingerprint:
    """path -> (mtime ns, size); an absent path is (0, -1), so creating one counts."""
    out = {}
    for path in paths:
        try:
            st = path.stat()
            out[str(path)] = (st.st_mtime_ns, st.st_size)
        except OSError:
            out[str(path)] = (0, -1)
    return out


def first_change(now: Fingerprint, seen: Fingerprint) -> str:
    return next(path for path in sorted(set(now) | set(seen)) if now.get(path) != seen.get(path))


def live_page(text: str, version: str) -> str:
    """text as served: its policy also allows the reload script and its poll, both from this server."""
    found = POLICY.search(text)
    if not found:
        raise ValueError("the page has no Content-Security-Policy to extend")
    policy = found.group(2).replace("script-src ", "script-src 'self' ", 1) + "; connect-src 'self'"
    text = text[: found.start(2)] + policy + text[found.end(2) :]
    tag = f'<script src="/reload.js" data-version="{html.escape(version)}"></script>\n'
    return text.replace("</body>", tag + "</body>", 1)


def handler(current: list[tuple[str, str]]) -> type[BaseHTTPRequestHandler]:
    """current[0] is the served (page, version). A request whose Host is not this server's own
    127.0.0.1 or localhost address is refused, so a page from another site cannot read this one
    through a name rebound to 127.0.0.1."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802  (http.server's name)
            port = self.connection.getsockname()[1]
            if self.headers.get("Host", "").lower() not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self.send_error(403)
                return
            text, version = current[0]
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self.reply(text, "text/html; charset=utf-8")
            elif path == "/version":
                self.reply(version, "text/plain; charset=utf-8")
            elif path == "/reload.js":
                self.reply(RELOAD_JS, "text/javascript; charset=utf-8")
            else:
                self.send_error(404)

        def reply(self, body: str, kind: str) -> None:
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Handler


def watch(
    rebuild: Callable[[], str],
    paths: Callable[[], list[Path]],
    port: int,
    opener: Callable[[str], object] | None,
) -> int:
    """Serve rebuild()'s page until Ctrl-C, rebuilding it when a file of paths() changes. paths() runs
    on every poll, so a file created since is watched; a failed rebuild keeps the last page and is retried on the next poll."""
    # Each fingerprint is taken before its rebuild, so an edit made during one is caught by the next poll.
    seen = fingerprint(paths())
    version = 1
    current = [(live_page(rebuild(), str(version)), str(version))]
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(current))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"dashboard: serving {url}; regenerating when a config file changes (Ctrl-C stops)", flush=True)
    if opener:
        opener(url)
    failure = ""
    try:
        while True:
            time.sleep(POLL_SECONDS)
            try:
                now = fingerprint(paths())
                if now == seen:
                    continue
                # seen moves only once the rebuild succeeds, so a failed one is retried on the next poll.
                current[0] = (live_page(rebuild(), str(version + 1)), str(version + 1))
                changed, seen = first_change(now, seen), now
                version += 1
            except Exception as error:  # noqa: BLE001  a half-written file must not end the watch
                if f"{error!r}" != failure:
                    print(f"dashboard: regenerating failed, still serving the last page: {type(error).__name__}: "
                          f"{error}", file=sys.stderr, flush=True)
                failure = f"{error!r}"
                continue
            failure = ""
            print(f"dashboard: regenerated ({changed} changed)", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    print("dashboard: stopped", flush=True)
    return 0
