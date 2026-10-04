#!/usr/bin/env python3
"""Tests for ~/.agents/bin/agent-kit, on a synthetic kit in a sandbox.

    python3 ~/.agents/hooks/tests/agent-kit.test.py [--sandbox DIR] [--keep]

AGENT_KIT_DIR, AGENT_KIT_SETTINGS and AGENT_KIT_MCP all point into the sandbox, so no live file,
no state and no secret is read or written. AGENT_KIT=<path> tests another copy of the script, e.g.
the pre-fix one to prove a case fails. Exits 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent-kit.test")

KIT_SRC = Path(os.environ.get("AGENT_KIT_SOURCE") or Path(__file__).resolve().parents[2])
AGENT_KIT = Path(os.environ.get("AGENT_KIT") or KIT_SRC / "bin/agent-kit")
HOST = {"settings": "~/agent-kit-test-unused.json", "mcp": "~/agent-kit-test-unused.json", "uiOwned": ["theme", "model"]}
BASE: dict[str, Any] = {
    "cleanupPeriodDays": 30,
    "env": {"A": "1", "B": "2"},
    "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm -rf:*)"], "defaultMode": "default"},
    "modelSettings": {"m1": {"effortLevel": "high"}, "m2": {"effortLevel": "low"}},
}
REGISTRY = [
    {"event": "PreToolUse", "matcher": "Bash", "command": "/usr/bin/true", "timeout": 10, "hosts": ["claude"]},
    {"event": "Stop", "command": "/usr/bin/true", "hosts": ["claude"]},
    {"event": "Stop", "command": "/usr/bin/false", "hosts": ["codex"]},
]
CATALOG = {"mcpServers": {"demo": {"command": "/usr/bin/true", "args": []}}}
RENDERED = {
    **BASE,
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/true", "timeout": 10}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}],
    },
}

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    logger.info("%s %s", "ok  " if cond else "FAIL", label)
    if not cond:
        failures.append(label)
        if detail:
            logger.info("     %s", detail.strip().replace("\n", "\n     "))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.kit = root / "kit"
        self.real = root / "real/settings.json"
        self.link = root / "settings.json"
        self.mcp = root / "mcp.json"
        write_json(self.kit / "hosts/claude/host.json", HOST)
        write_json(self.kit / "hosts/claude/settings.base.json", BASE)
        write_json(self.kit / "hooks/registry.json", REGISTRY)
        write_json(self.kit / "mcp/servers.json", CATALOG)
        self.real.parent.mkdir(parents=True)
        self.link.symlink_to(self.real)

    def env(self, settings: Path | None = None) -> dict[str, str]:
        return dict(
            os.environ,
            AGENT_KIT_DIR=str(self.kit),
            AGENT_KIT_SETTINGS=str(settings or self.link),
            AGENT_KIT_MCP=str(self.mcp),
        )

    def run(self, *args: str, settings: Path | None = None) -> tuple[int, str]:
        proc = subprocess.run(
            [str(AGENT_KIT), *args], env=self.env(settings), capture_output=True, text=True, check=False
        )
        return proc.returncode, proc.stdout + proc.stderr

    def render(self, *args: str, settings: Path | None = None) -> tuple[int, str]:
        return self.run("render", "--host", "claude", *args, settings=settings)

    def live(self) -> dict[str, Any]:
        return json.loads(self.real.read_text())

    def edit_live(self, fn: Callable[[dict[str, Any]], Any]) -> None:
        data = self.live()
        fn(data)
        write_json(self.real, data)

    def base(self) -> dict[str, Any]:
        return json.loads((self.kit / "hosts/claude/settings.base.json").read_text())

    def edit_base(self, fn: Callable[[dict[str, Any]], Any]) -> None:
        data = self.base()
        fn(data)
        write_json(self.kit / "hosts/claude/settings.base.json", data)

    def edit_registry(self, entries: list[dict[str, Any]]) -> None:
        write_json(self.kit / "hooks/registry.json", entries)


def first_render(sb: Sandbox) -> None:
    rc, out = sb.render()
    check("1b: missing settings.json: render writes the source, rc 0", rc == 0 and sb.real.exists(), out)
    if not sb.real.exists():
        write_json(sb.real, RENDERED)
    check("1b: the written file holds the base and the registry's claude hooks", sb.live() == RENDERED, out)
    check("render writes through the link and leaves it a link", sb.link.is_symlink(), out)
    for text, label in (("", "empty"), ("{}\n", "{}")):
        sb.real.write_text(text)
        rc, out = sb.render()
        ok = sb.real.read_text() == json.dumps(RENDERED, indent=2) + "\n"
        check(f"1b: {label} settings.json is a first render", rc == 0 and ok, out)
        write_json(sb.real, RENDERED)
    sb.render()
    before = sb.real.read_bytes()
    rc, out = sb.render()
    check("a second render is a no-op", rc == 0 and sb.real.read_bytes() == before and "unchanged" in out, out)


def ui_changes(sb: Sandbox) -> None:
    sb.edit_live(lambda d: d.update(theme="light"))
    sb.edit_live(lambda d: d["modelSettings"]["m1"].update(effortLevel="medium"))
    order = list(sb.live())
    sb.edit_base(lambda d: d["modelSettings"]["m2"].update(effortLevel="max"))
    rc, out = sb.render()
    cur = sb.live()
    check("UI-owned theme survives and is not reported", cur["theme"] == "light" and "theme" not in out, out)
    check("a UI change to a kit leaf survives", cur["modelSettings"]["m1"]["effortLevel"] == "medium", out)
    check("and is reported as drift naming --take", "settings drift (" in out and "modelSettings.m1.effortLevel" in out
          and "--take modelSettings.m1.effortLevel" in out, out)
    check("1c: drift outside hooks/permissions exits 0", rc == 0, out)
    check("a source change is applied", cur["modelSettings"]["m2"]["effortLevel"] == "max", out)
    check("key order kept", list(cur) == order, out)
    sb.run("adopt", "modelSettings.m1.effortLevel")
    check("adopt copies the live value into the source", sb.base()["modelSettings"]["m1"]["effortLevel"] == "medium")
    rc, out = sb.render()
    check("no drift after adopt", rc == 0 and "drift" not in out, out)
    rc, out = sb.run("adopt", "theme")
    check("adopt refuses a UI-owned key", rc != 0 and "UI-owned" in out, out)


def absent_and_take(sb: Sandbox) -> None:
    sb.edit_live(lambda d: d.pop("cleanupPeriodDays"))
    rc, out = sb.render()
    check("1b: a non-guarded key deleted live is kept deleted", "cleanupPeriodDays" not in sb.live(), out)
    check("1b: and reported as drift labelled absent live", "absent live" in out and "cleanupPeriodDays" in out, out)
    check("1c: that drift exits 0", rc == 0, out)
    rc, out = sb.run("adopt", "cleanupPeriodDays")
    check("1d: adopt of a key absent live refuses without --delete", rc != 0 and "--delete" in out, out)
    check("1d: and leaves the source alone", sb.base().get("cleanupPeriodDays") == 30)
    rc, out = sb.render("--take", "cleanupPeriodDays")
    check("1a: --take restores the source value", rc == 0 and sb.live().get("cleanupPeriodDays") == 30, out)
    rc, out = sb.render()
    check("1a: no drift after --take", rc == 0 and "drift" not in out, out)

    sb.edit_live(lambda d: d["env"].pop("B"))
    rc, out = sb.run("adopt", "env.B", "--delete")
    check("1d: adopt --delete removes the key from the source", rc == 0 and "B" not in sb.base()["env"], out)
    rc, out = sb.render()
    check("1d: no drift after adopt --delete", rc == 0 and "drift" not in out, out)


def guarded(sb: Sandbox) -> None:
    sb.edit_live(lambda d: d["permissions"].pop("deny"))
    rc, out = sb.render()
    check("1b: permissions.deny absent live is restored", sb.live()["permissions"].get("deny") == BASE["permissions"]["deny"], out)
    check("1b: restore says so and exits 0", rc == 0 and "restored permissions.deny" in out, out)

    sb.edit_live(lambda d: d.pop("permissions"))
    rc, out = sb.render()
    check("1b: a whole permissions block absent live is restored", rc == 0 and sb.live().get("permissions") == BASE["permissions"], out)

    sb.edit_live(lambda d: d["hooks"].pop("Stop"))
    rc, out = sb.render()
    check("1b: a hook event absent live is restored", rc == 0 and "Stop" in sb.live()["hooks"], out)

    sb.edit_live(lambda d: d["permissions"]["allow"].append("Bash(curl:*)"))
    rc, out = sb.render()
    check("1c: a UI change under permissions is kept", "Bash(curl:*)" in sb.live()["permissions"]["allow"], out)
    check("1c: and makes render exit 1, naming --take", rc == 1 and "--take <key>" in out, out)
    rc, out = sb.render("--take", "permissions.allow")
    check("1a: --take permissions.allow restores it, rc 0", rc == 0 and sb.live()["permissions"]["allow"] == BASE["permissions"]["allow"], out)

    sb.edit_live(lambda d: d["hooks"]["PreToolUse"][0]["hooks"][0].update(timeout=99))
    rc, out = sb.render("--dry-run")
    check("1c: hooks drift exits 1 on --dry-run too", rc == 1 and "hooks.PreToolUse" in out, out)
    rc, out = sb.run("adopt", "hooks.PreToolUse")
    check("adopt refuses hooks and names --take", rc != 0 and "--take hooks.PreToolUse" in out, out)

    sb.edit_live(lambda d: d["modelSettings"]["m1"].update(effortLevel="low"))
    rc, out = sb.render("--all")
    cur = sb.live()
    check("1a: --all writes the source over every drifted leaf", rc == 0
          and cur["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 10
          and cur["modelSettings"]["m1"]["effortLevel"] == "medium", out)
    check("1a: --all keeps UI-owned keys", cur.get("theme") == "light", out)

    rc, out = sb.render("--take", "theme")
    check("1a: --take refuses a UI-owned key", rc != 0 and "UI-owned" in out, out)
    rc, out = sb.render("--take", "no.such.key")
    check("1a: --take refuses an unknown key", rc != 0 and "neither" in out, out)


def doctor(sb: Sandbox) -> None:
    sb.edit_live(lambda d: d.update(liveOnly=True))
    rc, out = sb.run("doctor")
    check("2: doctor reports a live-only key as information", "live-only key" in out and "liveOnly" in out, out)
    check("2: and not as drift", "0 kit-owned key(s) differ" in out and rc == 0, out)
    rc, out = sb.render()
    check("2: render keeps the live-only key too", sb.live().get("liveOnly") is True and rc == 0, out)
    sb.edit_live(lambda d: d["env"].update(A="9"))
    rc, out = sb.run("doctor")
    check("doctor fails on a kit-owned difference", rc == 1 and "settings drift: env.A" in out, out)
    sb.render("--take", "env.A")


def state_per_target(sb: Sandbox) -> None:
    other = sb.root / "other/settings.json"
    other.parent.mkdir()
    sb.edit_base(lambda d: d["env"].update(A="2"))
    sb.render(settings=other)
    rc, out = sb.render()
    check("3: rendering another target does not rewrite this target's state",
          rc == 0 and sb.live()["env"]["A"] == "2" and "drift" not in out, out)
    states = sorted(p.name for p in (sb.kit / "state/rendered").iterdir())
    check("3: one state file per target", len([s for s in states if s.startswith("claude-settings.")]) == 2, str(states))


def registry(sb: Sandbox) -> None:
    before = sb.real.read_bytes()
    for entries, label in (
        ([*REGISTRY, {"event": "Stop", "command": "/usr/bin/true"}], "without hosts"),
        ([*REGISTRY, {"event": "Stop", "command": "/usr/bin/true", "hosts": []}], "with empty hosts"),
    ):
        sb.edit_registry(entries)
        rc, out = sb.render()
        check(f"4: render fails on an entry {label}", rc != 0 and "non-empty hosts" in out, out)
        check(f"4: and writes nothing ({label})", sb.real.read_bytes() == before)
        rc, out = sb.run("doctor")
        check(f"4: doctor fails on an entry {label}", rc != 0 and "non-empty hosts" in out, out)
    sb.edit_registry(REGISTRY)


def mcp(sb: Sandbox) -> None:
    sb.mcp.write_text('{"mcpServers": {}}\n')
    rc, out = sb.render()
    check("mcp.json changed outside the kit is not overwritten", rc == 1 and sb.mcp.read_text() == '{"mcpServers": {}}\n', out)
    rc, out = sb.render("--force-mcp")
    check("--force-mcp writes it, mode 600", rc == 0 and "demo" in sb.mcp.read_text()
          and (sb.mcp.stat().st_mode & 0o777) == 0o600, out)


ROLES_TOML = """[roles.main]
model = "anthropic:opus[1m]"
effort = "high"
[roles.worker]
model = "anthropic:opus"
effort = "high"
[roles.thermo-bugs]
model = "anthropic:inherit"
effort = "high"
prefix = "B"
[roles.review-cross]
model = "openai:gpt-6.1-sol"
effort = "high"
prefix = "CX"
fallback = "thermo-bugs"
[review]
round1 = ["thermo-bugs", "review-cross"]
later = ["review-cross"]
[hosts.claude]
provider = "anthropic"
effort_models = ["m1", "m2"]
[hosts.codex]
provider = "openai"
"""
AGENT_MD = "---\nname: {n}\ndescription: d\ntools: Read, Grep\nmodel: sonnet\neffort: low\nmaxTurns: 9\n---\nbody {n}\n"


def roles(root: Path) -> None:
    """roles.toml renders agents, settings and the review rounds; bad roles fail before any write."""
    kit = root / "roles-kit"
    write_json(kit / "hosts/claude/host.json", {**HOST, "uiOwned": ["theme"]})
    base = {k: v for k, v in BASE.items() if k != "modelSettings"}
    write_json(kit / "hosts/claude/settings.base.json", base)
    write_json(kit / "hooks/registry.json", REGISTRY)
    write_json(kit / "mcp/servers.json", CATALOG)
    (kit / "roles.toml").write_text(ROLES_TOML)
    isolated = root / "isolated-kit"
    (isolated / "bin").mkdir(parents=True)
    shutil.copy2(AGENT_KIT, isolated / "bin/agent-kit")
    (isolated / "roles.toml").write_text(ROLES_TOML.replace('model = "anthropic:opus[1m]"', 'model = "anthropic:sonnet"'))
    isolated_env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_KIT_")}
    proc = subprocess.run([str(isolated / "bin/agent-kit"), "roles", "--format", "sh"],
                          env=isolated_env, capture_output=True, text=True, check=False)
    check("roles: an isolated executable reads its own kit, not the live kit",
          proc.returncode == 0 and "main anthropic sonnet high agent" in proc.stdout,
          proc.stdout + proc.stderr)
    for n in ("worker", "thermo-bugs", "review-cross"):
        (kit / "agents").mkdir(exist_ok=True)
        (kit / "agents" / f"{n}.md").write_text(AGENT_MD.format(n=n))
    target = root / "claude-agents"
    target.symlink_to(kit / "agents")
    settings = root / "roles-settings.json"
    env = dict(os.environ, AGENT_KIT_DIR=str(kit), AGENT_KIT_SETTINGS=str(settings),
               AGENT_KIT_MCP=str(root / "roles-mcp.json"), AGENT_KIT_AGENTS=str(target))

    def run(*args: str) -> tuple[int, str]:
        p = subprocess.run([str(AGENT_KIT), *args], env=env, capture_output=True, text=True, check=False)
        return p.returncode, p.stdout + p.stderr

    def render() -> tuple[int, str]:
        return run("render", "--host", "claude")

    rc, out = render()
    check("roles: render rc 0", rc == 0, out)
    check("roles: the agents link became a directory", target.is_dir() and not target.is_symlink(), out)
    names = sorted(p.name for p in target.iterdir()) if target.is_dir() else []
    check("roles: one file per host-provider role; review-cross (openai) renders none",
          names == ["thermo-bugs.md", "worker.md"], names)
    worker = (target / "worker.md").read_text() if (target / "worker.md").exists() else ""
    check("roles: model and effort from roles.toml, after tools:, other keys kept",
          worker == "---\nname: worker\ndescription: d\ntools: Read, Grep\nmodel: opus\neffort: high\nmaxTurns: 9\n---\nbody worker\n", worker)
    bugs = (target / "thermo-bugs.md").read_text() if (target / "thermo-bugs.md").exists() else ""
    check("roles: inherit renders no model line", "model:" not in bugs and "effort: high" in bugs, bugs)
    live = json.loads(settings.read_text()) if settings.exists() else {}
    check("roles: settings model, effortLevel and modelSettings from [roles.main]",
          live.get("model") == "opus[1m]" and live.get("effortLevel") == "high"
          and live.get("modelSettings") == {"m1": {"effortLevel": "high"}, "m2": {"effortLevel": "high"}}, live)
    sh = kit / "state/rendered/roles-claude.sh"
    text = sh.read_text() if sh.exists() else ""
    check("roles: roles-claude.sh has the rounds and a run row for review-cross",
          "RV_ROUND1='thermo-bugs review-cross'" in text and "review-cross openai gpt-6.1-sol high run CX thermo-bugs 900" in text, text)
    ino = target.stat().st_ino if target.exists() else 0
    rc, out = render()
    check("roles: a second render writes nothing, same directory",
          rc == 0 and "0 written, 0 removed" in out and target.stat().st_ino == ino, out)

    (kit / "roles.toml").write_text(ROLES_TOML.replace(
        '[roles.worker]\nmodel = "anthropic:opus"\neffort = "high"', '[roles.worker]\nmodel = "anthropic:opus"\neffort = "xhigh"'))
    rc, out = render()
    check("roles: changing one role's effort rewrites only its file",
          rc == 0 and "1 written" in out and "effort: xhigh" in (target / "worker.md").read_text(), out)

    switched = ROLES_TOML.replace('model = "openai:gpt-6.1-sol"', 'model = "anthropic:opus"')
    (kit / "roles.toml").write_text(switched)
    rc, out = render()
    check("roles: review-cross on anthropic renders its agent file", rc == 0 and (target / "review-cross.md").exists(), out)
    check("roles: ...and its row is an Agent spawn",
          "review-cross anthropic opus high agent CX" in (sh.read_text() if sh.exists() else ""))
    (kit / "roles.toml").write_text(ROLES_TOML)
    rc, out = render()
    check("roles: back on openai, its rendered file is removed", rc == 0 and not (target / "review-cross.md").exists(), out)

    before = settings.read_text()
    for bad, label, want in (
        (ROLES_TOML.replace('effort = "xhigh"', 'effort = "minimal"').replace(
            '[roles.worker]\nmodel = "anthropic:opus"\neffort = "high"', '[roles.worker]\nmodel = "anthropic:opus"\neffort = "minimal"'),
         "an anthropic role at minimal", "not on the anthropic scale"),
        (ROLES_TOML.replace('model = "openai:gpt-6.1-sol"\neffort = "high"', 'model = "openai:gpt-6.1-sol"\neffort = "max"'),
         "an openai role at max", "not on the openai scale"),
        (ROLES_TOML.replace('model = "openai:gpt-6.1-sol"', 'model = "gpt-6.1-sol"'), "a model with no provider", "<provider>:<model>"),
        (ROLES_TOML.replace('later = ["review-cross"]', 'later = ["nobody"]'), "a round naming no role", "has no [roles.nobody]"),
        # B-3: a typo'd, missing, mistyped or once-only [review] used to render a round nobody completes.
        (ROLES_TOML.replace("round1 =", "round_1 ="), "a typo'd [review] key", "unknown keys ['round_1']"),
        (ROLES_TOML.replace("[review]", "[reveiw]"), "a typo'd top-level table", "unknown top-level keys ['reveiw']"),
        ("review = 3\n" + ROLES_TOML.replace('[review]\nround1 = ["thermo-bugs", "review-cross"]\nlater = ["review-cross"]\n', ""),
         "a [review] that is not a table", "review must be a table"),
        ("roles = 1\n[review]" + ROLES_TOML.split("[review]")[1],
         "a roles key that is not a table", "roles must be a table"),
        (ROLES_TOML.replace('round1 = ["thermo-bugs", "review-cross"]', 'round1 = ["thermo-bugs"]\nonce = ["thermo-bugs"]'),
         "a round 1 of the once-per-PR role alone", "round1 names no correctness reviewer"),
        (ROLES_TOML.replace('model = "anthropic:inherit"', 'model = "openai:inherit"'),
         "an openai role inheriting main's anthropic model", "would take main's anthropic model"),
        (ROLES_TOML.replace('fallback = "thermo-bugs"', 'fallback = "worker"'),
         "a fallback that is not a review role", "must be another review role"),
    ):
        (kit / "roles.toml").write_text(bad)
        rc, out = render()
        check(f"roles: render fails on {label}", rc != 0 and want in out, out)
        check(f"roles: ...and writes nothing ({label})", settings.read_text() == before)
    (kit / "roles.toml").write_text(ROLES_TOML)

    (kit / "agents/orphan.md").write_text(AGENT_MD.format(n="orphan"))
    (kit / "roles.toml").write_text(ROLES_TOML.replace('effort = "high"', 'effort = "xhigh"'))
    before = settings.read_text()
    rc, out = render()
    check("roles: an agent source with no role fails the render", rc != 0 and "no [roles.orphan]" in out, out)
    check("roles: an invalid agent source leaves settings unchanged", settings.read_text() == before)
    (kit / "agents/orphan.md").unlink()
    (kit / "roles.toml").write_text(ROLES_TOML)
    # CX-3: a role in the rounds with no source used to render, publishing a reviewer nobody can spawn.
    (kit / "agents/thermo-bugs.md").rename(kit / "thermo-bugs.md.off")
    rc, out = render()
    check("roles: a role with no agents/<role>.md fails the render",
          rc != 0 and "has no agents/thermo-bugs.md" in out and settings.read_text() == before, out)
    (kit / "thermo-bugs.md.off").rename(kit / "agents/thermo-bugs.md")
    (kit / "agents/review-cross.md").write_text("no frontmatter\n")
    rc, out = render()
    check("roles: an openai role's source is validated too", rc != 0 and "review-cross.md has no frontmatter" in out, out)
    (kit / "agents/review-cross.md").write_text(AGENT_MD.format(n="review-cross"))
    write_json(kit / "hosts/claude/settings.base.json", {**base, "effortLevel": "low"})
    rc, out = render()
    check("roles: settings.base.json may not set a role key", rc != 0 and "[roles.main]" in out, out)
    write_json(kit / "hosts/claude/settings.base.json", base)
    rc, out = run("adopt", "effortLevel")
    check("roles: adopt refuses a role key", rc != 0 and "roles.toml" in out, out)
    rc, out = run("roles", "--host", "codex")
    check("roles: on the codex host review-cross is native and worker runs through agent-run",
          rc == 0 and any(ln.startswith("review-cross") and " agent " in ln for ln in out.splitlines())
          and any(ln.startswith("worker") and " run " in ln for ln in out.splitlines()), out)
    rc, out = render()
    (target / "worker.md").write_text("hand edit\n")
    rc, out = run("doctor")
    check("roles: doctor reports a hand-edited rendered agent", "agents drift" in out and "worker.md" in out, out)

    # CX-2: the first render replaces a link; a role moved off the host on the very next render must
    # lose its file. The manifest used to be keyed through the link, so the next render found none.
    fresh = root / "claude-agents-2"
    fresh.symlink_to(kit / "agents")
    env2 = dict(env, AGENT_KIT_AGENTS=str(fresh))
    (kit / "roles.toml").write_text(switched)
    p = subprocess.run([str(AGENT_KIT), "render", "--host", "claude"], env=env2, capture_output=True, text=True, check=False)
    check("roles: first render over a link, review-cross on anthropic", p.returncode == 0 and (fresh / "review-cross.md").exists(), p.stdout + p.stderr)
    (kit / "roles.toml").write_text(ROLES_TOML)
    p = subprocess.run([str(AGENT_KIT), "render", "--host", "claude"], env=env2, capture_output=True, text=True, check=False)
    check("roles: ...moved to openai on the next render, its file is removed",
          p.returncode == 0 and not (fresh / "review-cross.md").exists(), p.stdout + p.stderr)

    # B-4: a kit other than ~/.agents (a worktree) renders live only on purpose.
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    foreign = root / "foreign-kit"
    shutil.copytree(kit, foreign, ignore=shutil.ignore_patterns("state"))
    (foreign / "bin").mkdir()
    shutil.copy2(AGENT_KIT, foreign / "bin/agent-kit")
    plain = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_KIT_")}
    p = subprocess.run([str(foreign / "bin/agent-kit"), "render", "--host", "claude"], env=dict(plain, HOME=str(home)),
                       capture_output=True, text=True, check=False)
    check("roles: a non-installed kit refuses to render the live targets",
          p.returncode != 0 and "not the installed kit" in p.stdout + p.stderr
          and not (home / "agent-kit-test-unused.json").exists() and not (home / ".claude/agents").exists(),
          p.stdout + p.stderr)
    targets = {"AGENT_KIT_SETTINGS": str(root / "foreign.json"), "AGENT_KIT_MCP": str(root / "foreign-mcp.json"),
               "AGENT_KIT_AGENTS": str(root / "foreign-agents")}
    p = subprocess.run([str(foreign / "bin/agent-kit"), "render", "--host", "claude"], env=dict(plain, HOME=str(home), **targets),
                       capture_output=True, text=True, check=False)
    check("roles: ...and renders to targets the environment names", p.returncode == 0 and (root / "foreign.json").exists(),
          p.stdout + p.stderr)


def real_sources(root: Path) -> None:
    """The installed kit's own sources validate and render."""
    kit = root / "real-kit"
    for rel in ("hosts/claude/host.json", "hosts/claude/settings.base.json", "hooks/registry.json"):
        (kit / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(KIT_SRC / rel, kit / rel)
    write_json(kit / "mcp/servers.json", CATALOG)
    if (KIT_SRC / "roles.toml").exists():
        shutil.copy2(KIT_SRC / "roles.toml", kit / "roles.toml")
        shutil.copytree(KIT_SRC / "agents", kit / "agents")
    env = dict(os.environ, AGENT_KIT_DIR=str(kit), AGENT_KIT_SETTINGS=str(root / "real-kit.json"),
               AGENT_KIT_MCP=str(root / "real-kit-mcp.json"), AGENT_KIT_AGENTS=str(root / "real-kit-agents"))
    proc = subprocess.run([str(AGENT_KIT), "render", "--host", "claude"], env=env, capture_output=True, text=True, check=False)
    check("the installed kit's base, registry, roles and agents render cleanly", proc.returncode == 0, proc.stdout + proc.stderr)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sandbox", type=Path, help="a directory to create (default: a new temp dir)")
    ap.add_argument("--keep", action="store_true", help="keep the sandbox for inspection")
    args = ap.parse_args()
    if args.sandbox:
        args.sandbox.mkdir(parents=True)
        root = args.sandbox
    else:
        root = Path(tempfile.mkdtemp(prefix="agent-kit-test."))
    try:
        sb = Sandbox(root)
        for case in (first_render, ui_changes, absent_and_take, guarded, doctor, state_per_target, registry, mcp):
            logger.info("--- %s", case.__name__)
            try:
                case(sb)
            except Exception:  # noqa: BLE001 - one broken case must not hide the rest
                check(f"{case.__name__} ran to the end", False, traceback.format_exc())
        logger.info("--- roles")
        try:
            roles(root)
        except Exception:  # noqa: BLE001
            check("roles ran to the end", False, traceback.format_exc())
        logger.info("--- real_sources")
        real_sources(root)
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    logger.info("%d failure(s)", len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
