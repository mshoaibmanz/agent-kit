"""`agent-kit dashboard --watch`: serve the page on 127.0.0.1 and regenerate it when a config file
changes. Polls mtimes every POLL_SECONDS (no file-watching dependency); the served copy carries a
small script that reloads the open page when a newer one is ready. The page written to disk stays the
self-contained one."""

from __future__ import annotations

import html
import re
import threading
import time
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

POLL_SECONDS = 2.0
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
HOST_FILES = ("settings.json", "settings.local.json", "mcp.json", "hooks.json", "config.toml", "CLAUDE.md", "AGENTS.md")
RELOAD_JS = (
    "(function(){var v=document.currentScript.getAttribute('data-version');"
    "function poll(){fetch('/version',{cache:'no-store'}).then(function(r){return r.text()})"
    ".then(function(t){if(t!==v){location.reload()}else{setTimeout(poll,2000)}},"
    "function(){setTimeout(poll,2000)})}setTimeout(poll,2000)})();\n"
)
POLICY = re.compile(r'(http-equiv="Content-Security-Policy" content=")([^"]*)(")')

Fingerprint = tuple[tuple[str, int, int], ...]


def watched(kit: Path, overlay: Path, host_roots: Iterable[Path]) -> list[Path]:
    """The config files the page is generated from: the kit's own, the top-level files of its local/
    folder and of the overlay folder, and each configured host root's config files."""
    paths = [kit / name for name in KIT_FILES]
    for folder in {kit / "local", overlay}:
        try:
            paths += sorted(p for p in folder.iterdir() if p.is_file())
        except OSError:
            pass
    paths += [root / name for root in host_roots for name in HOST_FILES]
    return paths


def fingerprint(paths: Iterable[Path]) -> Fingerprint:
    """(path, mtime ns, size) per path; an absent file is (path, 0, -1), so creating one counts."""
    out = []
    for path in paths:
        try:
            st = path.stat()
            out.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(path), 0, -1))
    return tuple(out)


def live_page(text: str, version: str) -> str:
    """text as served: its policy also allows the reload script and its poll, both from this server."""
    found = POLICY.search(text)
    if not found:
        raise ValueError("the page has no Content-Security-Policy to extend")
    policy = found.group(2).replace("script-src ", "script-src 'self' ", 1) + "; connect-src 'self'"
    text = text[: found.start(2)] + policy + text[found.end(2):]
    tag = f'<script src="/reload.js" data-version="{html.escape(version)}"></script>\n'
    return text.replace("</body>", tag + "</body>", 1)


class Served:
    """The current page and its version, shared with the server threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.version = 0
        self.text = ""

    def update(self, text: str) -> None:
        with self.lock:
            self.version += 1
            self.text = text

    def current(self) -> tuple[str, str]:
        with self.lock:
            return self.text, str(self.version)


def handler(served: Served) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802  (http.server's name)
            text, version = served.current()
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self.reply(live_page(text, version), "text/html; charset=utf-8")
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
    """Serve rebuild()'s page until Ctrl-C, rebuilding it when a file of paths() changes."""
    served = Served()
    # Each fingerprint is taken before its rebuild, so an edit made during one is caught by the next poll.
    watching = paths()
    seen = fingerprint(watching)
    served.update(rebuild())
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(served))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"dashboard: serving {url}; regenerating when a config file changes (Ctrl-C stops)", flush=True)
    if opener:
        opener(url)
    try:
        while True:
            time.sleep(POLL_SECONDS)
            now = fingerprint(watching)
            if now == seen:
                continue
            changed = next(new[0] for new, old in zip(now, seen) if new != old)
            watching = paths()
            seen = fingerprint(watching)
            served.update(rebuild())
            print(f"dashboard: regenerated ({changed} changed)", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    print("dashboard: stopped", flush=True)
    return 0
