#!/usr/bin/env python3
"""Sentry instances: the config layers (kit, preset, overlay), one MCP server per instance, the
wrapper's fail-fast line and its token handling, and sentry-map's missing-instance warning.
Synthetic kits only. `security` is a fake in every case (a module attribute in process, the copied
lib's constant in a subprocess), so no case reads the real Keychain."""

from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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
        self.saved_security = sentry.SECURITY
        sentry.SECURITY = str(self.security)
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
        sentry.SECURITY = self.saved_security
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

    def test_label_prefers_the_slug_then_the_config(self) -> None:
        alpha = sentry.load_instances(self.kit, self.overlay)["alpha"]
        self.assertEqual(
            [alpha.label(o) for o in ("main", "alt", "acmeprd-web", "acmestg-web", "other")],
            ["prod", "staging", "prod", "staging", "unknown"],
        )

    def test_presence_reads_attributes_only_and_falls_back_to_the_variable(self) -> None:
        beta = sentry.load_instances(self.kit, self.overlay)["beta"]
        self.assertEqual(beta.env_name, "SENTRY_ACCESS_TOKEN_BETA")
        self.assertFalse(sentry.token_present(beta))
        with self.assertRaises(sentry.KeychainMissing):
            sentry.read_token(beta)
        self.items.write_text(f"{self.beta_item}={TOKEN}\n")
        self.assertTrue(sentry.token_present(beta))
        self.assertEqual(sentry.read_token(beta), TOKEN)


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
        for name in (*names, "lib/sentry.py"):
            shutil.copy2(SOURCE / "bin" / name, self.kit / "bin" / name)
        lib = self.kit / "bin/lib/sentry.py"
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
            env={**os.environ, **(env or {})},
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
            self.mod.crawl(self.inst)
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
            self.mod.crawl(self.inst)
        self.home.routes["/api/0/organizations/"] = (200, {}, [{"slug": "o", "links": {"regionUrl": self.other_url}}])  # type: ignore[attr-defined]
        self.home.routes["/api/0/organizations/o/projects/"] = (200, {}, [{"slug": "p"}])  # type: ignore[attr-defined]
        self.home.routes["/api/0/organizations/o/events/"] = (200, {}, {"data": []})  # type: ignore[attr-defined]
        result = self.mod.crawl(self.inst)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
