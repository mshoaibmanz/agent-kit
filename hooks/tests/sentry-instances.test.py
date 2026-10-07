#!/usr/bin/env python3
"""Sentry instances: the config layers (kit, preset, overlay), one MCP server per instance, the
wrapper's fail-fast line and its token handling, and sentry-map's missing-instance warning.
Synthetic kits only. `security` is a fake in every case (a module attribute in process, the copied
lib's constant in a subprocess), so no case reads the real Keychain."""

from __future__ import annotations

import contextlib
import http.server
import importlib.machinery
import io
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SOURCE = Path(os.environ.get("AGENT_KIT_SOURCE", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(SOURCE / "bin/lib"))

import sentry  # noqa: E402

TEMPLATE = {
    "type": "stdio",
    "command": "/kit/bin/sentry-mcp",
    "args": ["--host=sentry.alpha.invalid", "--disable-skills=seer"],
    "env": {"SENTRY_HOST": "sentry.alpha.invalid", "MCP_DISABLE_SKILLS": "seer"},
}
TOKEN = "fake-token-value-0123456789"
FAKE_SECURITY = """#!/bin/sh
# find-generic-password -s SERVICE -a ACCOUNT [-w]: the item from $FAKE_ITEMS (service=token lines).
service= reveal=
while [ $# -gt 0 ]; do
  case "$1" in
    -s) service=$2; shift ;;
    -w) reveal=1 ;;
  esac
  shift
done
line=$(grep -F "$service=" "${FAKE_ITEMS:-/dev/null}" 2>/dev/null | head -n 1)
[ -n "$line" ] || exit 44
[ -z "$reveal" ] || printf '%s\\n' "${line#*=}"
"""
FAKE_NPX = """#!/bin/sh
{ printf 'argv=%s\\n' "$*"; printf 'token=%s\\n' "$SENTRY_ACCESS_TOKEN"; printf 'host=%s\\n' "$SENTRY_HOST"; } > "$NPX_LOG"
"""


def script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.kit = self.root / "kit"
        self.overlay = self.root / "overlay"
        (self.kit / "mcp").mkdir(parents=True)
        (self.kit / "bin/lib").mkdir(parents=True)
        self.overlay.mkdir()
        self.items = self.root / "items"
        self.items.write_text("")
        self.security = script(self.root / "fake/security", FAKE_SECURITY)
        self.saved = {k: os.environ.get(k) for k in ("KIT_ENV", "FAKE_ITEMS")}
        os.environ.update(KIT_ENV=str(self.overlay / "kit.env"), FAKE_ITEMS=str(self.items))
        self.saved_security = sentry.secret_store.SECURITY
        sentry.secret_store.SECURITY = str(self.security)
        self.beta_item = "agent-kit-test/sentry-beta"
        self.write(
            self.kit / "mcp" / sentry.INSTANCES_FILE,
            {
                "alpha": {"host": "sentry.alpha.invalid", "keychain": "agent-kit-test/sentry-alpha",
                          "server": "sentry", "orgs": {"main": "prod", "alt": "staging"}},
                "beta": {"host": "sentry.beta.invalid", "keychain": self.beta_item},
            },
        )

    def tearDown(self) -> None:
        sentry.secret_store.SECURITY = self.saved_security
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def write(self, path: Path, instances: dict) -> None:
        path.write_text(json.dumps({"instances": instances}))


class Instances(Fixture):
    def test_overlay_overrides_field_by_field(self) -> None:
        self.write(self.overlay / sentry.INSTANCES_FILE, {"beta": {"host": "eu.sentry.beta.invalid"}})
        beta = sentry.load_instances(self.kit, self.overlay)["beta"]
        self.assertEqual((beta.host, beta.keychain, beta.server), ("eu.sentry.beta.invalid", self.beta_item, "sentry-beta"))

    def test_overlay_alone_may_add_an_instance(self) -> None:
        (self.kit / "mcp" / sentry.INSTANCES_FILE).unlink()
        self.write(self.overlay / sentry.INSTANCES_FILE, {"gamma": {"host": "sentry.gamma.invalid", "keychain": "k/gamma"}})
        self.assertEqual(list(sentry.load_instances(self.kit, self.overlay)), ["gamma"])

    def test_no_file_means_no_instances_and_the_legacy_item(self) -> None:
        (self.kit / "mcp" / sentry.INSTANCES_FILE).unlink()
        self.assertEqual(sentry.load_instances(self.kit, self.overlay), {})
        self.assertEqual(sentry.default_instance({}).keychain, sentry.LEGACY_SERVICE)

    def test_bad_values_refuse(self) -> None:
        for bad in (
            {"t": {"host": "h.invalid", "keychain": "k", "orgs": {"o": "live"}}},
            {"t": {"host": "h.invalid", "keychain": "k; rm -rf ~"}},
            {"t": {"host": "h.invalid"}},
            {"t": {"host": "h.invalid", "keychain": "k", "token": "x"}},
            {"a": {"host": "h.invalid", "keychain": "k", "server": "s"}, "b": {"host": "h.invalid", "keychain": "k", "server": "s"}},
        ):
            self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sentry.load_instances(self.kit, self.overlay)

    def test_label_prefers_the_config_then_the_slug(self) -> None:
        alpha = sentry.load_instances(self.kit, self.overlay)["alpha"]
        self.assertEqual(
            [alpha.label(o) for o in ("main", "alt", "acmeprd-web", "acmestg-web", "other")],
            ["prod", "staging", "prod", "staging", "unknown"],
        )
        self.write(self.overlay / sentry.INSTANCES_FILE, {"alpha": {"orgs": {"xprd": "staging", "xstg": "prod"}}})
        alpha = sentry.load_instances(self.kit, self.overlay)["alpha"]
        self.assertEqual([alpha.label(o) for o in ("xprd", "xstg", "yprd")], ["staging", "prod", "prod"])

    def test_presence_reads_attributes_only_and_falls_back_to_the_variable(self) -> None:
        instances = sentry.load_instances(self.kit, self.overlay).values()
        beta = sentry.load_instances(self.kit, self.overlay)["beta"]
        self.assertEqual(beta.env_name, "SENTRY_ACCESS_TOKEN_BETA")
        self.assertFalse(sentry.token_present(beta, instances))
        with self.assertRaises(sentry.KeychainMissing):
            sentry.read_token(beta, instances)
        self.items.write_text(f"{self.beta_item}={TOKEN}\n")
        self.assertTrue(sentry.token_present(beta, instances))
        self.assertEqual(sentry.read_token(beta, instances), TOKEN)


class Servers(Fixture):
    def test_absent_token_renders_only_the_template(self) -> None:
        out = sentry.expand_servers(self.kit, {"sentry": TEMPLATE, "x": {"url": "u"}}, overlay=self.overlay)
        self.assertEqual(out, {"sentry": TEMPLATE, "x": {"url": "u"}})

    def test_present_token_adds_a_clone_after_the_template(self) -> None:
        out = sentry.expand_servers(
            self.kit, {"sentry": TEMPLATE, "x": {"url": "u"}}, present=lambda _i: True, overlay=self.overlay
        )
        self.assertEqual(list(out), ["sentry", "sentry-beta", "x"])
        self.assertEqual(out["sentry"], TEMPLATE)
        self.assertEqual(out["sentry-beta"]["args"], ["--instance", "beta", "--disable-skills=seer"])
        self.assertEqual(out["sentry-beta"]["env"], {"SENTRY_HOST": "sentry.beta.invalid", "MCP_DISABLE_SKILLS": "seer"})
        safe = sentry.safe_sentry({k: v for k, v in out["sentry-beta"].items() if k != "type"}, Path("/kit/bin/sentry-mcp"))
        self.assertEqual(safe["args"][:2], ["--instance", "beta"])

    def test_an_npx_template_clones_onto_the_wrapper(self) -> None:
        npx = {"command": "npx", "args": ["-y", "@sentry/mcp-server@0.37.0", "--host", "sentry.alpha.invalid"]}
        out = sentry.expand_servers(self.kit, {"sentry": npx}, present=lambda _i: True, overlay=self.overlay)
        self.assertEqual(out["sentry-beta"]["command"], str(self.kit / "bin/sentry-mcp"))
        self.assertEqual(out["sentry-beta"]["args"], ["--instance", "beta"])

    def test_every_instance_server_takes_the_sentry_policy(self) -> None:
        self.assertEqual(sentry.server_names(self.kit, self.overlay), {"sentry", "sentry-beta"})

    def test_hosts_safe_servers_renders_a_clone_once_its_item_exists(self) -> None:
        shutil.copyfile(SOURCE / "bin/lib/sentry.py", self.kit / "bin/lib/sentry.py")
        (self.kit / "mcp/servers.json").write_text(json.dumps({"mcpServers": {"sentry": TEMPLATE}}))
        loader = importlib.machinery.SourceFileLoader("hosts_under_test", str(SOURCE / "bin/lib/hosts.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        hosts = importlib.util.module_from_spec(spec)
        loader.exec_module(hosts)
        self.assertEqual(list(hosts.safe_servers(self.kit)), ["sentry"])
        self.items.write_text(f"{self.beta_item}={TOKEN}\n")
        out = hosts.safe_servers(self.kit)
        self.assertEqual(list(out), ["sentry", "sentry-beta"])
        self.assertTrue(out["sentry-beta"]["command"].endswith("bin/sentry-mcp"))
        self.assertNotIn(TOKEN, json.dumps(out))


class Preset(unittest.TestCase):
    def setUp(self) -> None:
        import preset

        self.preset = preset

    def test_sentry_instances_table_validates(self) -> None:
        good = {"sentry": {"instances": {"beta": {"host": "sentry.beta.invalid", "keychain": "agent-kit/mcp/sentry-beta",
                                                  "orgs": {"acme": "prod"}}}}}
        self.preset.validate_preset(good, "t")
        for bad in (
            {"sentry": {"instances": {"beta": {"host": "sentry.beta.invalid"}}}},
            {"sentry": {"servers": {}}},
            {"sentry": {"instances": {"beta": {"host": "sentry.beta.invalid", "keychain": "k", "note": "ghp_" + "a1" * 10}}}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.preset.validate_preset(bad, "t")


class Wrapper(Fixture):
    def install(self, *names: str) -> None:
        for name in (*names, "lib/sentry.py", "lib/secret_store.py"):
            shutil.copy2(SOURCE / "bin" / name, self.kit / "bin" / name)
        (self.kit / "hooks/lib").mkdir(parents=True, exist_ok=True)
        for name in ("kit_env.py", "hook-io"):
            shutil.copy2(SOURCE / "hooks/lib" / name, self.kit / "hooks/lib" / name)
        lib = self.kit / "bin/lib/secret_store.py"
        text = lib.read_text().replace('SECURITY = "/usr/bin/security"', f'SECURITY = "{self.security}"')
        self.assertIn(str(self.security), text)
        lib.write_text(text)

    def run_bin(self, name: str, *args: str, env: dict | None = None, python: str | None = None) -> subprocess.CompletedProcess:
        self.install(name)
        command = [str(self.kit / "bin" / name), *args]
        return subprocess.run(
            [python, *command] if python else command,
            capture_output=True,
            text=True,
            # No inherited SENTRY_* (a real token or host in the caller's shell): each test sets its own.
            env={**{k: v for k, v in os.environ.items() if not k.startswith("SENTRY_")}, **(env or {})},
            timeout=60,
        )

    def test_missing_item_fails_fast_with_the_command(self) -> None:
        proc = self.run_bin("sentry-mcp", "--instance", "beta", "--check")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(
            proc.stderr,
            f"Sentry MCP off: no Keychain item {self.beta_item} for instance beta. Fix: "
            f"security add-generic-password -s {self.beta_item} -a sentry -w (in your own terminal)\n",
        )
        self.assertEqual(proc.stdout, "")

    def test_unknown_instance_names_the_configured_ones(self) -> None:
        proc = self.run_bin("sentry-mcp", "--instance", "nope")
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(proc.stderr.strip().endswith("configured: alpha, beta (mcp/sentry-instances.json)"), proc.stderr)

    def test_token_reaches_only_the_server_environment(self) -> None:
        self.items.write_text(f"{self.beta_item}={TOKEN}\n")
        bindir = self.root / "path"
        script(bindir / "npx", FAKE_NPX)
        log = self.root / "npx.log"
        proc = self.run_bin("sentry-mcp", "--instance", "beta", "--disable-skills=seer",
                            env={"PATH": f"{bindir}:/usr/bin:/bin", "NPX_LOG": str(log)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
        lines = log.read_text().splitlines()
        self.assertEqual(lines[0], "argv=-y @sentry/mcp-server@0.37.0 --disable-skills=seer")
        self.assertEqual(lines[1:], [f"token={TOKEN}", "host=sentry.beta.invalid"])

    def test_no_token_reaches_another_instances_host(self) -> None:
        """The default instance's token goes to its own host whatever SENTRY_HOST the client inherits,
        a host argument naming another is refused, and the legacy item never reaches a configured host."""
        self.items.write_text(f"agent-kit-test/sentry-alpha={TOKEN}\n{sentry.LEGACY_SERVICE}=legacy-token\n")
        script(self.root / "path/npx", FAKE_NPX)
        log = self.root / "npx.log"
        env = {"PATH": f"{self.root / 'path'}:/usr/bin:/bin", "NPX_LOG": str(log), "SENTRY_HOST": "sentry.beta.invalid"}
        proc = self.run_bin("sentry-mcp", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.read_text().splitlines()[1:], [f"token={TOKEN}", "host=sentry.alpha.invalid"])
        for args in (["--host=sentry.beta.invalid"], ["--url", "https://sentry.beta.invalid/"]):
            log.unlink(missing_ok=True)
            proc = self.run_bin("sentry-mcp", *args, env=env)
            with self.subTest(args=args):
                self.assertEqual(proc.returncode, 1)
                self.assertIn("instance alpha is configured for sentry.alpha.invalid", proc.stderr)
                self.assertFalse(log.exists())
        self.assertEqual(self.run_bin("sentry-mcp", "--host", "https://sentry.alpha.invalid", env=env).returncode, 0)
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, {"beta": {"host": "sentry.beta.invalid", "keychain": self.beta_item}})
        log.unlink()
        for args in ([], ["--host", "sentry.beta.invalid"]):
            proc = self.run_bin("sentry-mcp", *args, env={**env, "SENTRY_HOST": "sentry.beta.invalid" if not args else ""})
            with self.subTest(legacy=args):
                self.assertEqual(proc.returncode, 1)
                self.assertIn("sentry.beta.invalid is a configured instance's host", proc.stderr)
                self.assertFalse(log.exists())

    def test_an_inherited_sentry_url_is_a_host_and_is_refused_before_any_token_read(self) -> None:
        """@sentry/mcp-server prefers SENTRY_URL over SENTRY_HOST: one naming another host is refused
        for the default, a named and the legacy instance, before any store is read."""
        script(self.root / "path/npx", FAKE_NPX.replace("; } >", "; printf 'url=%s\\n' \"$SENTRY_URL\"; } >"))
        log = self.root / "npx.log"
        env = {"PATH": f"{self.root / 'path'}:/usr/bin:/bin", "NPX_LOG": str(log)}
        refused = (([], "https://sentry.beta.invalid"), (["--instance", "beta"], "https://sentry.alpha.invalid/"))
        for args, url in refused:
            proc = self.run_bin("sentry-mcp", *args, env={**env, "SENTRY_URL": url})
            with self.subTest(args=args):
                self.assertEqual(proc.returncode, 1)
                self.assertIn("is configured for", proc.stderr)
                self.assertNotIn("Keychain", proc.stderr)
                self.assertFalse(log.exists())
        self.items.write_text(f"agent-kit-test/sentry-alpha={TOKEN}\n{self.beta_item}=beta-token\n{sentry.LEGACY_SERVICE}=legacy\n")
        proc = self.run_bin("sentry-mcp", env={**env, "SENTRY_URL": "https://sentry.alpha.invalid"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.read_text().splitlines()[1:], [f"token={TOKEN}", "host=sentry.alpha.invalid", "url="])
        check = self.run_bin("sentry-mcp", "--check", env={**env, "SENTRY_URL": "https://sentry.alpha.invalid"})
        self.assertIn("(host sentry.alpha.invalid,", check.stdout)
        log.unlink()
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, {"beta": {"host": "sentry.beta.invalid", "keychain": self.beta_item}})
        proc = self.run_bin("sentry-mcp", env={**env, "SENTRY_URL": "https://sentry.beta.invalid"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("sentry.beta.invalid is a configured instance's host", proc.stderr)
        self.assertFalse(log.exists())
        check = self.run_bin("sentry-mcp", "--check", "--host", "h.invalid", env={**env, "SENTRY_URL": "https://u.invalid"})
        self.assertIn("(host u.invalid,", check.stdout)

    def test_the_legacy_item_never_reaches_a_configured_host_in_any_spelling(self) -> None:
        """Hosts compare as the server resolves them (WHATWG URL host): a default port, userinfo, a
        trailing dot, percent-encoding or a backslash cannot carry the legacy token to a configured
        host, nor can the server's default sentry.io when an instance is configured for it."""
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, {"beta": {"host": "sentry.beta.invalid", "keychain": self.beta_item}})
        self.items.write_text(f"{self.beta_item}=beta-token\n{sentry.LEGACY_SERVICE}=legacy-token\n")
        script(self.root / "path/npx", FAKE_NPX)
        log = self.root / "npx.log"
        env = {"PATH": f"{self.root / 'path'}:/usr/bin:/bin", "NPX_LOG": str(log)}
        urls = ("https://sentry.beta.invalid:443/", "https://me:@sentry.beta.invalid", "https://sentry.beta.invalid./",
                "https://sentry%2Ebeta.invalid", "https://SENTRY.beta.invalid\\@other.invalid")
        cases = [([], {"SENTRY_URL": url}) for url in urls]
        cases += [([], {"SENTRY_HOST": "sentry.beta.invalid:443"}), (["--url=https://x:@sentry.beta.invalid"], {}),
                  (["--host", "other.invalid", "--host", "sentry.beta.invalid."], {})]
        for args, extra in cases:
            proc = self.run_bin("sentry-mcp", *args, env={**env, **extra})
            with self.subTest(args=args, env=extra):
                self.assertEqual(proc.returncode, 1)
                self.assertIn("sentry.beta.invalid is a configured instance's host", proc.stderr)
                self.assertFalse(log.exists())
        proc = self.run_bin("sentry-mcp", env={**env, "SENTRY_URL": "https://bücher.invalid"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("is not a plain host name", proc.stderr)
        self.assertFalse(log.exists())
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, {"beta": {"host": "https://SENTRY.io/", "keychain": self.beta_item}})
        proc = self.run_bin("sentry-mcp", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("sentry.io is a configured instance's host", proc.stderr)
        self.assertFalse(log.exists())
        proc = self.run_bin("sentry-mcp", env={**env, "SENTRY_HOST": "sentry.beta.invalid:8443"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.read_text().splitlines()[1:], ["token=legacy-token", "host=sentry.beta.invalid:8443"])

    def test_an_instance_takes_its_own_host_with_the_default_port(self) -> None:
        self.items.write_text(f"agent-kit-test/sentry-alpha={TOKEN}\n")
        script(self.root / "path/npx", FAKE_NPX)
        log = self.root / "npx.log"
        env = {"PATH": f"{self.root / 'path'}:/usr/bin:/bin", "NPX_LOG": str(log)}
        for args, extra in (([], {"SENTRY_URL": "https://sentry.alpha.invalid:443/"}), (["--url", "https://Sentry.Alpha.invalid:443"], {})):
            log.unlink(missing_ok=True)
            proc = self.run_bin("sentry-mcp", *args, env={**env, **extra})
            with self.subTest(args=args, env=extra):
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(log.read_text().splitlines()[1:], [f"token={TOKEN}", "host=sentry.alpha.invalid"])
        proc = self.run_bin("sentry-mcp", "--url", "https://sentry.alpha.invalid:8443", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SENTRY_URL sentry.alpha.invalid:8443", proc.stderr)

    def test_no_dsn_reaches_the_server(self) -> None:
        """The server reports its own errors to SENTRY_DSN or DEFAULT_SENTRY_DSN; neither is passed on."""
        self.items.write_text(f"agent-kit-test/sentry-alpha={TOKEN}\n")
        script(self.root / "path/npx", '#!/bin/sh\nenv | grep -c "SENTRY_DSN=" > "$NPX_LOG"\nexit 0\n')
        log = self.root / "npx.log"
        dsn = {"SENTRY_DSN": "https://o.invalid/1", "DEFAULT_SENTRY_DSN": "https://o.invalid/2"}
        proc = self.run_bin("sentry-mcp", env={"PATH": f"{self.root / 'path'}:/usr/bin:/bin", "NPX_LOG": str(log), **dsn})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.read_text().strip(), "0")

    def test_two_instances_may_share_a_host(self) -> None:
        shared = {"host": "sentry.shared.invalid"}
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, {
            "alpha": {**shared, "keychain": "agent-kit-test/sentry-alpha", "server": "sentry"},
            "beta": {**shared, "keychain": self.beta_item}})
        self.items.write_text(f"agent-kit-test/sentry-alpha={TOKEN}\n{self.beta_item}=beta-token\n")
        script(self.root / "path/npx", FAKE_NPX)
        log = self.root / "npx.log"
        env = {"PATH": f"{self.root / 'path'}:/usr/bin:/bin", "NPX_LOG": str(log)}
        for args, token in (([], TOKEN), (["--instance", "alpha"], TOKEN), (["--instance", "beta"], "beta-token")):
            proc = self.run_bin("sentry-mcp", *args, env=env)
            with self.subTest(args=args):
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(log.read_text().splitlines()[1:], [f"token={token}", "host=sentry.shared.invalid"])

    def test_runs_under_the_system_python(self) -> None:
        if not Path("/usr/bin/python3").exists():
            self.skipTest("no /usr/bin/python3")
        proc = self.run_bin("sentry-mcp", "--instance", "beta", "--check", python="/usr/bin/python3")
        self.assertIn("security add-generic-password", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_sentry_map_warns_and_skips_a_missing_instance(self) -> None:
        cache = self.root / "map.json"
        proc = self.run_bin("sentry-map", "find", "anything", env={"SENTRY_MAP_CACHE": str(cache)})
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            f"sentry-map: warning: instance beta (sentry.beta.invalid) skipped: no Keychain item {self.beta_item}; "
            f"enable with: security add-generic-password -s {self.beta_item} -a sentry -w",
            proc.stderr.splitlines(),
        )
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(cache.exists())


def load_sentry_map():  # type: ignore[no-untyped-def]
    loader = importlib.machinery.SourceFileLoader("sentry_map", str(SOURCE / "bin/sentry-map"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Recorder(http.server.BaseHTTPRequestHandler):
    """Answers GET from the server's `routes` (path -> (status, headers, body)) and records each
    request's path and Authorization header."""

    def do_GET(self) -> None:  # noqa: N802
        self.server.seen.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        status, headers, body = self.server.routes.get(self.path.split("?")[0], (404, {}, []))  # type: ignore[attr-defined]
        data = json.dumps(body).encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        pass


class Origins(Fixture):
    """The token goes only to the instance's own origin: a second local server on another port plays
    the other origin and records any Authorization header that reaches it."""

    def serve(self) -> tuple[http.server.ThreadingHTTPServer, str]:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        server.routes, server.seen = {}, []  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def setUp(self) -> None:
        super().setUp()
        self.home, self.home_url = self.serve()
        self.other, self.other_url = self.serve()
        self.other.routes["/api/0/organizations/"] = (200, {}, [{"slug": "stolen"}])  # type: ignore[attr-defined]
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE,
                   {"local": {"host": self.home_url, "keychain": "agent-kit-test/sentry-local"}})
        self.items.write_text(f"agent-kit-test/sentry-local={TOKEN}\n")
        self.mod = load_sentry_map()
        self.inst = sentry.load_instances(self.kit, self.overlay)["local"]

    def test_a_redirect_to_another_origin_is_refused_before_the_token_leaves(self) -> None:
        self.home.routes["/api/0/organizations/"] = (302, {"Location": f"{self.other_url}/api/0/organizations/"}, [])  # type: ignore[attr-defined]
        with self.assertRaisesRegex(RuntimeError, "another origin"):
            self.mod.crawl(self.inst, [self.inst])
        self.assertEqual(self.other.seen, [])  # type: ignore[attr-defined]

    def test_a_same_origin_redirect_is_followed(self) -> None:
        self.home.routes["/old/"] = (302, {"Location": "/api/0/organizations/"}, [])  # type: ignore[attr-defined]
        self.home.routes["/api/0/organizations/"] = (200, {}, [{"slug": "o"}])  # type: ignore[attr-defined]
        client = self.mod.Client(self.inst, TOKEN)
        self.assertEqual(client.get_all(f"{self.home_url}/old/"), [{"slug": "o"}])

    def test_pagination_and_region_urls_on_another_origin_get_no_token(self) -> None:
        self.home.routes["/api/0/organizations/"] = (  # type: ignore[attr-defined]
            200, {"Link": f'<{self.other_url}/api/0/organizations/>; rel="next"; results="true"'}, [])
        with self.assertRaisesRegex(RuntimeError, "another origin"):
            self.mod.crawl(self.inst, [self.inst])
        self.home.routes["/api/0/organizations/"] = (200, {}, [{"slug": "o", "links": {"regionUrl": self.other_url}}])  # type: ignore[attr-defined]
        self.home.routes["/api/0/organizations/o/projects/"] = (200, {}, [{"slug": "p"}])  # type: ignore[attr-defined]
        self.home.routes["/api/0/organizations/o/events/"] = (200, {}, {"data": []})  # type: ignore[attr-defined]
        result = self.mod.crawl(self.inst, [self.inst])
        self.assertEqual([(o["slug"], o["region_url"], [p["slug"] for p in o["projects"]]) for o in result["orgs"]],
                         [("o", self.home_url, ["p"])])
        self.assertEqual(self.other.seen, [])  # type: ignore[attr-defined]
        self.assertTrue(all(auth == f"Bearer {TOKEN}" for _path, auth in self.home.seen))  # type: ignore[attr-defined]

    def test_token_variables_never_collide(self) -> None:
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, {
            "prod-a": {"host": "a.invalid", "keychain": "k/a"}, "prod_a": {"host": "b.invalid", "keychain": "k/b"},
            "beta": {"host": "c.invalid", "keychain": "k/c"}})
        names = {name: inst.env_name for name, inst in sentry.load_instances(self.kit, self.overlay).items()}
        self.assertEqual(names, {"prod-a": "SENTRY_ACCESS_TOKEN_PROD_2DA", "prod_a": "SENTRY_ACCESS_TOKEN_PROD_5FA",
                                 "beta": "SENTRY_ACCESS_TOKEN_BETA"})

    def test_the_variable_name_before_hex_is_read_with_a_warning_to_rename(self) -> None:
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, {"prod-a": {"host": "a.invalid", "keychain": "k/a"}})
        instances = sentry.load_instances(self.kit, self.overlay)
        inst = instances["prod-a"]
        os.environ["SENTRY_ACCESS_TOKEN_PROD_A"] = TOKEN
        self.addCleanup(os.environ.pop, "SENTRY_ACCESS_TOKEN_PROD_A", None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertTrue(sentry.token_present(inst, instances.values()))
            self.assertEqual(sentry.read_token(inst, instances.values()), TOKEN)
        self.assertIn("sentry: rename SENTRY_ACCESS_TOKEN_PROD_A to SENTRY_ACCESS_TOKEN_PROD_2DA", err.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())

    def test_an_old_variable_name_two_instances_share_reaches_neither(self) -> None:
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, {
            "prod-a": {"host": "a.invalid", "keychain": "k/a"}, "prod_a": {"host": "b.invalid", "keychain": "k/b"}})
        instances = sentry.load_instances(self.kit, self.overlay)
        os.environ["SENTRY_ACCESS_TOKEN_PROD_A"] = TOKEN
        self.addCleanup(os.environ.pop, "SENTRY_ACCESS_TOKEN_PROD_A", None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for inst in instances.values():
                self.assertEqual(sentry.env_token(inst, instances.values()), "", inst.name)
                self.assertFalse(sentry.token_present(inst, instances.values()), inst.name)
                with self.assertRaises(sentry.KeychainMissing):
                    sentry.read_token(inst, instances.values())
            out = sentry.expand_servers(self.kit, {"sentry": TEMPLATE}, overlay=self.overlay)
        self.assertEqual(list(out), ["sentry"])
        self.assertIn("SENTRY_ACCESS_TOKEN_PROD_A is not read for prod-a: another instance shares that name; "
                      "set SENTRY_ACCESS_TOKEN_PROD_2DA", err.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())

    def test_an_old_variable_name_that_is_another_instances_own_reaches_only_that_one(self) -> None:
        self.write(self.kit / "mcp" / sentry.INSTANCES_FILE, {
            "prod_a": {"host": "a.invalid", "keychain": "k/a"}, "prod_5fa": {"host": "b.invalid", "keychain": "k/b"}})
        instances = sentry.load_instances(self.kit, self.overlay)
        self.assertEqual(instances["prod_5fa"].old_env_name, instances["prod_a"].env_name)
        os.environ["SENTRY_ACCESS_TOKEN_PROD_5FA"] = TOKEN
        self.addCleanup(os.environ.pop, "SENTRY_ACCESS_TOKEN_PROD_5FA", None)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sentry.read_token(instances["prod_a"], instances.values()), TOKEN)
            self.assertEqual(sentry.env_token(instances["prod_5fa"], instances.values()), "")
            with self.assertRaises(sentry.KeychainMissing):
                sentry.read_token(instances["prod_5fa"], instances.values())


class Match(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_sentry_map()
        self.rows = [{"project": p, "org": o} for p, o in (
            ("web-billing-api-ledger", "acme"), ("web-billing-api-gateway", "acme-alt"), ("auth-portal", "acme"),
        )]

    def test_substring_ignores_punctuation(self) -> None:
        found, exact = self.mod.match(self.rows, ["billing_api"])
        self.assertTrue(exact)
        self.assertEqual(len(found), 2)

    def test_repo_name_falls_back_to_shared_words(self) -> None:
        found, exact = self.mod.match(self.rows, ["shop-billing-api"])
        self.assertFalse(exact)
        self.assertEqual([r["project"] for r in found], ["web-billing-api-ledger", "web-billing-api-gateway"])


def cached_org(slug: str, last_seen: str, project: str = "billing") -> dict:
    return {"slug": slug, "name": slug, "region_url": f"https://{slug}.invalid",
            "projects": [{"slug": project, "platform": "python", "environments": ["production"], "last_seen": last_seen}]}


class LiveCopy(Fixture):
    """One slug on two instances, each with prod and staging copies, from a fresh cache: `find`
    never crawls, so no case reaches Sentry or a token."""

    install = Wrapper.install
    run_bin = Wrapper.run_bin

    def setUp(self) -> None:
        super().setUp()
        self.cache = self.root / "map.json"
        now = time.time()
        alpha = [cached_org("main", "2026-10-06"), cached_org("alt", "2026-09-01"), cached_org("main", "2026-10-07", "auth")]
        beta = [cached_org("betaprd", "2026-08-15"), cached_org("oldprd", ""), cached_org("betastg", "2026-10-05"),
                cached_org("misc", "2026-10-07")]
        self.cache.write_text(json.dumps({"instances": {
            "alpha": {"host": "sentry.alpha.invalid", "fetched_at": now, "orgs": alpha},
            "beta": {"host": "sentry.beta.invalid", "fetched_at": now, "orgs": beta},
        }}))

    def find(self, *args: str) -> subprocess.CompletedProcess:
        proc = self.run_bin("sentry-map", "find", "billing", *args, env={"SENTRY_MAP_CACHE": str(self.cache)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_the_newest_copy_per_label_is_live_and_unknown_labels_sort_last(self) -> None:
        found = json.loads(self.find("--json").stdout)
        self.assertEqual([(r["status"], r["label"], r["instance"], r["org"], r["last_seen"]) for r in found], [
            ("live", "prod", "alpha", "main", "2026-10-06"),
            ("older", "prod", "beta", "betaprd", "2026-08-15"),
            ("stale", "prod", "beta", "oldprd", "-"),
            ("live", "staging", "beta", "betastg", "2026-10-05"),
            ("older", "staging", "alpha", "alt", "2026-09-01"),
            ("label unknown", "unknown", "beta", "misc", "2026-10-07"),
        ])

    def test_the_table_leads_with_the_status(self) -> None:
        lines = self.find().stdout.splitlines()
        self.assertEqual(lines[0].split()[:3], ["status", "label", "project"])
        self.assertEqual([line.split()[:2] for line in lines[1:3]], [["live", "prod"], ["older", "prod"]])
        self.assertTrue(lines[-1].startswith("label unknown"), lines[-1])

    def test_copies_tied_on_the_newest_day_are_all_live(self) -> None:
        mod = load_sentry_map()
        rows = [{"project": "p", "label": "prod", "instance": i, "org": "o", "last_seen": "2026-10-01"} for i in ("a", "b")]
        self.assertEqual([r["status"] for r in mod.mark_live(rows)], ["live", "live"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
