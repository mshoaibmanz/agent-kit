"""The How the kit works view: one DocPart per subject, generated each build from the kit's own
files (the hook registry, SKILL.md frontmatter, rules, the host table in hosts.py, bin scripts,
kit.env.example, the MCP catalog and roles.toml). Only the short concept text in LEADS is written
by hand; every list, table and figure is read from the source the part names."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from blocking import BLOCKING, hook_name
from dashboard_diagrams import Chip, HostBox, Layer, Moment, layers, lifecycle, review_loop, wiring
from dashboard_html import (
    Badge,
    Cell,
    Code,
    DocPart,
    Figure,
    Lines,
    Muted,
    Row,
    Steps,
    Strong,
    Table,
    anchor,
)
from dashboard_sections import (
    Setup,
    TableRows,
    docstring_line,
    documented_keys,
    kit_command,
    read_json,
    setup_command,
)
from hosts import EVENTS, HOSTS, RENDERED_FILES, frontmatter, skill_dirs, skill_hosts
from kit_env import layers as env_layers
from kit_text import HOST_HOME_ENV, RULES_FILES

LEADS = {
    "layers": """
The kit is three layers. The **public engine** is the source checkout: hooks, skills, rules, roles,
the MCP catalog format and the commands that install them. An **org pack** is a team's private
repository: a preset of shared, non-secret defaults plus its own skills and rules, pinned by commit.
The **personal overlay** is your own `local/` folder: your `kit.env` keys and whatever only you use.

Where two layers set the same value, the more personal one wins: your `local/kit.env` over the
pack's `preset.env`, and the pack over the engine's defaults. Nothing in a host's folder is edited
by hand; setup and render write each host from these layers.
""",
    "flow": """
Setup previews every file before it writes one and records each choice, so a later run repeats
them without asking. Each step below is a command to run in your own terminal.
""",
    "hosts": """
Each host gets the same kit in its own format: the rules become its global instructions file,
the hook registry becomes its native hook config, the catalog becomes its MCP config, and skills
are linked into the folders it reads. A hook row names the hosts it runs on; Codex and Cursor run
theirs through `hooks/host-adapter`, which turns the host's payload into the one the hook reads.
Render tracks the entries it wrote and leaves your own settings beside them.
""",
    "lifecycle": """
Hooks are small scripts a host runs at fixed points of a session. **SessionStart** injects context;
**UserPromptSubmit** reads the prompt (binding the session to its task, nudges); **PreToolUse**
can refuse a tool call before it runs; **PostToolUse** records what happened (edits, test runs,
pushes); **Stop** checks the turn before it ends.

A blocking hook can refuse; an advisory one only adds a line. Setup installs the blocking ones
only with `--blocking-hooks`. Outside the host, a git layer that only agent shells see runs
`commit-msg` and `pre-push`, so a push from any tool passes the same gate.
""",
    "skills": """
A skill is a folder with a `SKILL.md`: frontmatter (name, description, the hosts it supports) and
the instructions a host loads when the description matches the task. The layer column says where
each one comes from.
""",
    "commands": """
The kit's commands live in `bin/`; each purpose below is its own docstring or header comment, and
the subcommands are its own argument parser's. Slash commands are prompt files a host offers as
`/name`.
""",
    "rules": """
Rules are the always-loaded instructions. Render joins the engine's shared rules with the host's
own file, adds the team pack's rules, and writes the result into each host's global instructions
file between kit markers. Reference docs are read on demand, never loaded whole.
""",
    "review": """
Verification runs inside the session; review runs before and after the push.

- **Tests** run only through the repo's own wrapper, and each run is recorded with the scope it
  covered.
- **Stop** asks for a self-check sized to the change, ending in a TALLY line.
- **Push**: the git `pre-push` gate refuses a force push, an unreviewed change past a small size,
  a sprawling ticket and a push with no recorded test run. `AGENT_PUSH_NOW="<reason>"` is the one
  override, and it needs a reason.
- **Review rounds**: reviewer roles read the change, each finding carries its role's prefix, and
  fixes land as a new commit until a round finds nothing new.
""",
    "safety": """
- **Production data is read-only by construction.** `ro-mysql` runs one statement per call in a
  server-enforced read-only session; `bqro` dry-runs first and refuses anything but SELECT. The
  shell guard refuses raw database clients, so the wrappers are the only way in.
- **Secrets stay in the OS secret store** (Keychain on macOS, Secret Service on Linux). The catalog
  names a credential by service and account; render resolves it, and this page checks presence only.
- **Edits stay on the kit's sources.** A guard refuses edits to files render writes into a host's
  folder: change the source, then render.
- **Risky shell commands are refused before they run**: production writes, raw database clients
  and destructive git.
""",
    "config": """
Settings live in `kit.env` layers, never in code; the keys below are the ones `kit.env.example`
documents, with its own comment as their meaning (this view shows no values). MCP servers come from
the catalog setup installed: a credential there is a reference to the secret store, not a value.
""",
}
# When each host event fires: the hosts' lifecycle, in the order the timeline draws it. An event
# the registry uses that is not here is drawn after these, so none is dropped.
EVENT_ORDER = {
    "SessionStart": "start, resume, clear or compact",
    "UserPromptSubmit": "each prompt, before the model reads it",
    "PreToolUse": "before a tool call; can refuse it",
    "PostToolUse": "after a tool call",
    "SubagentStart": "a subagent starts",
    "SubagentStop": "a subagent finishes",
    "Notification": "the host asks for attention",
    "PreCompact": "before the context is compacted",
    "Stop": "the turn is about to end",
    "SessionEnd": "the session closes",
}
GIT_HOOKS = {
    "commit-msg": "git commit, agent shells only",
    "pre-push": "git push, agent shells only",
}
REVIEW_LABELS = {
    "round1": "Round 1",
    "later": "Later rounds",
    "once": "Once per branch",
    "sensitive_adds": "A sensitive change adds",
}


def kit_file(setup: Setup, relative: str) -> Path:
    """The source checkout's file (the whole kit, every hook and skill whether installed or not),
    else the installed kit's copy."""
    source = setup.source() / relative
    return source if source.exists() else setup.kit / relative


def installed_hooks(setup: Setup) -> set[tuple[str, str]] | None:
    """(event, hook) pairs the install's own registry holds; None when it is the source's."""
    installed = setup.kit / "hooks/registry.json"
    if installed.resolve() == kit_file(setup, "hooks/registry.json").resolve():
        return None
    rows = read_json(installed)
    rows = rows if isinstance(rows, list) else []
    return {
        (str(r.get("event", "")), hook_name(str(r.get("command", ""))))
        for r in rows
        if isinstance(r, dict)
    }


def home(path: Path | str) -> str:
    text, user = str(path), str(Path.home())
    return "~" + text[len(user) :] if text == user or text.startswith(user + "/") else text


def registry(setup: Setup) -> tuple[Path, list[dict[str, Any]]]:
    path = kit_file(setup, "hooks/registry.json")
    rows = read_json(path)
    return path, [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def skill_rows(setup: Setup) -> list[tuple[str, str, Path]]:
    """(name, layer, SKILL.md) for every skill of every layer: the engine's, the pack's and the
    personal overlay's."""
    out: list[tuple[str, str, Path]] = []
    seen: set[Path] = set()
    for layer, folder in (
        ("kit", setup.source() / "skills"),
        ("kit", setup.kit / "skills"),
        ("pack", setup.kit / "pack/skills"),
        ("overlay", setup.kit / "local/skills"),
    ):
        for md in sorted(folder.glob("*/SKILL.md")):
            if md.resolve() in seen or (
                layer == "kit" and any(n == md.parent.name for n, _, _ in out)
            ):
                continue
            seen.add(md.resolve())
            out.append((md.parent.name, layer, md))
    return out


def layers_part(setup: Setup, part: DocPart) -> None:
    def entries(folder: Path, skip: tuple[str, ...] = ()) -> str:
        try:
            names = sorted(
                p.name + ("/" if p.is_dir() else "")
                for p in folder.iterdir()
                if not p.name.startswith(".") and p.name not in skip
            )
        except OSError:
            return "not present"
        return ", ".join(names) or "empty"

    source, pack, local = setup.source(), setup.kit / "pack", setup.kit / "local"
    overlay_files = [p for p in env_layers() if p.exists() and p.parent != local]
    stack = [
        Layer(
            "Public engine", home(source), entries(source, ("local", "pack", "state", "LICENSES"))
        ),
        Layer("Org pack", home(pack), entries(pack) if pack.is_dir() else "no pack installed"),
        Layer(
            "Personal overlay",
            home(local),
            entries(local) if local.is_dir() else "no local/ folder yet",
        ),
    ]
    part.sources = [p for p in (source, pack, local) if p.exists()]
    part.blocks.append(
        Figure(layers(stack, list(HOSTS)), "Lowest layer at the bottom; a higher layer wins.")
    )
    rows: TableRows = [(Strong(layer.title), layer.where, layer.holds) for layer in stack]
    rows += [
        (Strong("Overlay keys elsewhere"), home(p), "a kit.env layer kit_env reads")
        for p in overlay_files
    ]
    part.blocks.append(Table(("Layer", "Where", "Holds"), rows, "What each layer holds"))


def flow_part(setup: Setup, part: DocPart) -> None:
    source = setup.source()
    part.sources = [source / "bin/agent-setup", setup.kit / "bin/agent-kit"]
    part.blocks.append(
        Steps(
            (
                (
                    "Install",
                    "Detects the hosts, previews every file it would write, then applies with "
                    "`--apply` and records a journal.",
                    setup_command(setup, "--apply"),
                ),
                (
                    "Sync",
                    "Re-renders every host from the recorded source checkout: the step after you "
                    "edit a kit source or your overlay.",
                    kit_command(setup, "sync"),
                ),
                (
                    "Update",
                    "Fast-forwards the source checkout to its upstream, then syncs with the new "
                    "commit's own installer.",
                    setup_command(setup, action="update"),
                ),
                (
                    "Doctor",
                    "Reports drift between the kit and each host; `agent-setup doctor` checks the "
                    "install record itself.",
                    kit_command(setup, "doctor", "--host", "all"),
                ),
                (
                    "Roll back",
                    "Undoes one install by its journal and stops on any later edit.",
                    setup_command(setup, "JOURNAL_ID", action="rollback"),
                ),
                (
                    "See it",
                    "Writes this page from the live install.",
                    kit_command(setup, "dashboard"),
                ),
            )
        )
    )


def hosts_part(setup: Setup, part: DocPart) -> None:
    _, rows_ = registry(setup)
    boxes: list[HostBox] = []
    rows: TableRows = []
    for host in HOSTS:
        root = setup.roots[host]
        folders = [home(d) + "/" for d in skill_dirs(host, root)]
        files = (
            *RENDERED_FILES[host],
            *(d.name + "/" for d in skill_dirs(host, root) if d.parent == root),
        )
        boxes.append(HostBox(host, home(root), files, host in setup.configured))
        events = sorted({r.get("event", "") for r in rows_ if host in r.get("hosts", [])})
        named = [f"{e} ({EVENTS[e]})" if host == "cursor" and e in EVENTS else e for e in events]
        count = sum(host in r.get("hosts", []) for r in rows_)
        env = HOST_HOME_ENV.get(host)
        rows.append(
            Row(
                (
                    (
                        Strong(host),
                        Badge("configured", "ok")
                        if host in setup.configured
                        else Badge("not configured"),
                    ),
                    (Code(home(root)), Muted(f"or ${env}") if env else ""),
                    Lines(tuple(Code(f) for f in RENDERED_FILES[host])),
                    Lines(tuple(folders)),
                    (f"{count} hook rows", Muted(", ".join(named) or "-")),
                ),
                anchor("docs-host", host),
            )
        )
    sources = ["hooks/registry.json", "rules/", "skills/", "roles.toml", "mcp/servers.json"]
    sources = [s for s in sources if kit_file(setup, s.rstrip("/")).exists()]
    part.sources = [kit_file(setup, "bin/lib/hosts.py")]
    part.blocks.append(
        Figure(wiring(sources, boxes), "Render writes only the files each host reads.")
    )
    part.blocks.append(
        Table(
            ("Host", "Config root", "Files render writes", "Skills read from", "Hook events"),
            rows,
            "Per host",
        )
    )


def lifecycle_part(setup: Setup, part: DocPart) -> None:
    path, rows_ = registry(setup)
    by_event: dict[str, list[Chip]] = {}
    for entry in rows_:
        name = hook_name(str(entry.get("command", "")))
        chips = by_event.setdefault(str(entry.get("event", "")), [])
        if all(c.text != name for c in chips):
            chips.append(Chip(name, name in BLOCKING))
    order = [e for e in EVENT_ORDER if e in by_event] + sorted(set(by_event) - set(EVENT_ORDER))
    moments = [Moment(e, EVENT_ORDER.get(e, ""), tuple(by_event[e])) for e in order]
    git = kit_file(setup, "git-hooks/agent")
    git_hooks = sorted(p for p in git.iterdir() if p.is_file()) if git.is_dir() else []
    moments += [
        Moment(
            f"git {p.name}", GIT_HOOKS.get(p.name, "git, agent shells only"), (Chip(p.name, True),)
        )
        for p in git_hooks
    ]
    part.sources = [path, *([git] if git_hooks else [])]
    part.blocks.append(
        Figure(
            lifecycle(moments), "Each hook appears once per event, however many matchers it has."
        )
    )
    rows: TableRows = []
    ids: dict[str, int] = {}
    installed = installed_hooks(setup)
    for entry in rows_:
        command = str(entry.get("command", ""))
        name = hook_name(command)
        event = str(entry.get("event", ""))
        script = kit_file(setup, f"hooks/{name}")
        key = anchor("docs-hook", f"{event}-{name}")
        ids[key] = ids.get(key, 0) + 1
        kind: Cell = Badge("blocking", "warn") if name in BLOCKING else Badge("advisory")
        if installed is not None and (event, name) not in installed:
            kind = (kind, Badge("not installed"))
        rows.append(
            Row(
                (
                    Strong(name),
                    event,
                    Lines(tuple(Code(m) for m in str(entry.get("matcher") or "*").split("|"))),
                    kind,
                    tuple(Badge(h) for h in entry.get("hosts", [])),
                    script if script.is_file() else Code(command),
                ),
                key + (f"-{ids[key]}" if ids[key] > 1 else ""),
                str(entry.get("description", "")),
            )
        )
    note = (
        ""
        if installed is None
        else "A row the install's own registry does not hold is marked not installed."
    )
    part.blocks.append(
        Table(
            ("Hook", "Event", "Matcher", "Kind", "Hosts", "Source"),
            rows,
            "Every hook row",
            note=note,
        )
    )
    git_rows: TableRows = [(Strong(p.name), header_line(p), p) for p in git_hooks]
    if git_rows:
        part.blocks.append(
            Table(("Git hook", "What it does", "Source"), git_rows, "Git hooks in agent shells")
        )


def skills_part(setup: Setup, part: DocPart) -> None:
    rows: TableRows = []
    names = [n for n, _, _ in skill_rows(setup)]
    for name, layer, md in skill_rows(setup):
        text = md.read_text(errors="replace")
        try:
            allowed: Cell = ", ".join(sorted(skill_hosts(md, text)))
        except ValueError:
            allowed = Muted("invalid hosts: line")
        same = names.count(name) > 1 and layer != "kit"
        rows.append(
            Row(
                (
                    Strong(name),
                    (
                        Badge(layer, "ok" if layer == "overlay" else ""),
                        Muted("same name as a lower layer's") if same else "",
                    ),
                    allowed,
                    md,
                ),
                anchor("docs-skill", f"{layer}-{name}"),
                frontmatter(text).get("description", ""),
            )
        )
    part.sources = [
        p
        for p in (setup.source() / "skills", setup.kit / "pack/skills", setup.kit / "local/skills")
        if p.is_dir()
    ]
    part.blocks.append(
        Table(("Skill", "Layer", "Hosts", "Source"), rows, "Every skill", empty="No skills found.")
    )


def header_line(path: Path) -> str:
    """A script's purpose: its docstring's first sentence, else its header comment's, without a
    leading "<name>:" label."""
    line = docstring_line(path)
    if not line:
        comment: list[str] = []
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        for raw in lines[1:] if lines and lines[0].startswith("#!") else lines:
            if raw.startswith("#"):
                comment.append(raw.lstrip("#").strip())
            elif comment or raw.strip():
                break
        text = " ".join(c for c in comment if c)
        end = re.search(r"\.(\s|$)", text)
        line = text[: end.start() + 1] if end else text
    return re.sub(rf"^{re.escape(path.name)}\s*(?::|—|-)\s*", "", line)


SUBCOMMAND = re.compile(
    r"""\.add_parser\(\s*["']([\w-]+)["'](?:[^()]|\([^()]*\))*?help=["']([^"']*)["']""", re.S
)
ACTIONS = re.compile(r"""add_argument\(\s*["']action["']\s*,\s*choices=\(([^)]*)\)""")


def subcommands(path: Path) -> list[Cell]:
    """The subcommands a bin script's own argument parser declares, with their help."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out: list[Cell] = [(Code(name), Muted(help_)) for name, help_ in SUBCOMMAND.findall(text)]
    for found in ACTIONS.findall(text):
        out += [Code(name) for name in re.findall(r"""["']([\w-]+)["']""", found)]
    return out


def commands_part(setup: Setup, part: DocPart) -> None:
    folder = kit_file(setup, "bin")
    rows: TableRows = []
    for path in sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")):
        subs = subcommands(path)
        rows.append(
            Row(
                (Strong(path.name), Lines(tuple(subs)) if subs else Muted("-"), path),
                anchor("docs-cmd", path.name),
                header_line(path),
            )
        )
    part.blocks.append(Table(("Command", "Subcommands", "Source"), rows, "bin/ commands"))
    slash = kit_file(setup, "commands")
    slash_rows: TableRows = []
    for md in sorted(slash.glob("*.md")) if slash.is_dir() else []:
        meta = frontmatter(md.read_text(errors="replace"))
        slash_rows.append(
            Row(
                (Strong(f"/{md.stem}"), md),
                anchor("docs-slash", md.stem),
                meta.get("description", ""),
            )
        )
    part.blocks.append(
        Table(("Slash command", "Source"), slash_rows, "Slash commands", empty="No slash commands.")
    )
    part.sources = [p for p in (folder, slash) if p.exists()]


def rules_part(setup: Setup, part: DocPart) -> None:
    every = ", ".join(HOSTS)
    candidates: list[tuple[str, str, str]] = [
        ("rules/AGENTS.md", every, "engine: shared by every host"),
        *((f"rules/hosts/{h}.md", h, f"engine: {h} only") for h in HOSTS),
        ("pack/rules.md", every, "org pack: the team's rules"),
        ("local/rules.md", every, "personal overlay"),
    ]
    rows: TableRows = []
    for relative, hosts, layer in candidates:
        path = kit_file(setup, relative)
        if not path.is_file():
            continue
        words = len(path.read_text(errors="replace").split())
        targets = tuple(
            Code(home(setup.roots[h] / RULES_FILES[h])) for h in HOSTS if h in hosts.split(", ")
        )
        rows.append(
            Row(
                (Strong(relative), layer, Lines(targets), f"{words} words", path),
                anchor("docs-rule", relative),
            )
        )
    part.blocks.append(
        Table(("File", "Layer", "Written into", "Size", "Source"), rows, "Rules files")
    )
    docs = [kit_file(setup, "README.md"), kit_file(setup, "hooks/lib/README")]
    references = kit_file(setup, "references")
    docs += sorted(references.glob("*.md")) if references.is_dir() else []
    doc_rows: TableRows = [(Strong(p.name), first_heading(p), p) for p in docs if p.is_file()]
    part.blocks.append(Table(("Doc", "Subject", "Source"), doc_rows, "Reference docs"))
    part.sources = [kit_file(setup, "rules")]


def first_heading(path: Path) -> str:
    """A doc's subject: its first markdown heading, else its first non-empty line."""
    try:
        lines = [
            line.strip() for line in path.read_text(errors="replace").splitlines() if line.strip()
        ]
    except OSError:
        return ""
    heading = next((line for line in lines if line.startswith("#")), lines[0] if lines else "")
    return heading.lstrip("# ").strip()


def review_part(setup: Setup, part: DocPart) -> None:
    roles = setup.roles
    part.sources = [Path(setup.api.ROLES), setup.kit / "agents"]
    review: dict[str, list[str]] = dict(roles.review) if roles is not None else {}
    first = ", ".join(review.get("round1", [])) or "-"
    later = ", ".join(review.get("later", [])) or "-"
    stages = [
        ("Edit", "each source edit is marked for review and unlocks a test run"),
        ("Tests", "through the repo's own wrapper; each run is recorded"),
        ("Stop", "a self-check sized to the change, then a TALLY line"),
        ("Push gate", "git pre-push: review, tests, sprawl, force"),
        ("Round 1", first),
        ("Fix", "address each finding by its prefix in one new commit"),
        ("Later rounds", later),
        ("Merge-ready", "CI green, no open finding"),
    ]
    notes = [
        f"{REVIEW_LABELS.get(k, k)}: {', '.join(v)}"
        for k, v in review.items()
        if k not in ("round1", "later")
    ]
    part.blocks.append(
        Figure(
            review_loop(stages, notes), "The rounds and their roles come from roles.toml [review]."
        )
    )
    if roles is None:
        part.alerts.append(setup.roles_error or "This kit has no roles.toml.")
        return
    rows: TableRows = []
    for name, role in roles.roles.items():
        src = setup.kit / "agents" / f"{name}.md"
        description = (
            str(frontmatter(src.read_text(errors="replace")).get("description", ""))
            if src.is_file()
            else ""
        )
        rows.append(
            Row(
                (
                    Strong(name),
                    Code(f"{role.provider}:{role.model}"),
                    role.effort,
                    Badge(role.prefix, "warn") if role.prefix else Muted("-"),
                    role.fallback or Muted("-"),
                    src if src.is_file() else Muted("the main session"),
                ),
                anchor("docs-role", name),
                description,
            )
        )
    part.blocks.append(
        Table(
            ("Role", "Model", "Effort", "Finding prefix", "Fallback", "Agent file"), rows, "Roles"
        )
    )
    rounds: TableRows = [
        (REVIEW_LABELS.get(k, k), Code(k), ", ".join(v)) for k, v in review.items()
    ]
    part.blocks.append(Table(("Round", "roles.toml key", "Roles"), rounds, "Review rounds"))


def safety_part(setup: Setup, part: DocPart) -> None:
    path, rows_ = registry(setup)
    rows: TableRows = []
    seen: set[str] = set()
    for entry in rows_:
        name = hook_name(str(entry.get("command", "")))
        if name not in BLOCKING or name in seen:
            continue
        seen.add(name)
        rows.append(
            Row(
                (Strong(name), str(entry.get("event", "")), kit_file(setup, f"hooks/{name}")),
                anchor("docs-guard", name),
                str(entry.get("description", "")),
            )
        )
    part.blocks.append(Table(("Guard", "First event", "Source"), rows, "Hooks that can refuse"))
    tools = [kit_file(setup, f"bin/{n}") for n in ("ro-mysql", "bqro")]
    tools += [kit_file(setup, f"bin/lib/{n}.py") for n in ("secret_store", "credentials")]
    tool_rows: TableRows = [(Strong(p.name), header_line(p), p) for p in tools if p.is_file()]
    part.blocks.append(
        Table(("Tool", "What it does", "Source"), tool_rows, "Data wrappers and the secret store")
    )
    part.sources = [path]


def transport(spec: Any) -> str:
    if not isinstance(spec, dict):
        return "?"
    return (
        "http"
        if spec.get("url")
        else "stdio"
        if spec.get("command")
        else str(spec.get("type", "?"))
    )


def config_part(setup: Setup, part: DocPart) -> None:
    keys: TableRows = [
        Row((Code(k),), anchor("docs-key", k), meaning)
        for k, meaning in sorted(documented_keys(setup).items())
    ]
    part.blocks.append(
        Table(("kit.env key",), keys, "Overlay keys", empty="kit.env.example documents no key.")
    )
    servers: TableRows = [
        Row(
            (Strong(name), transport(spec)),
            anchor("docs-mcp", name),
            str(spec.get("description", "")) if isinstance(spec, dict) else "",
        )
        for name, spec in sorted(setup.catalog.items())
    ]
    part.blocks.append(
        Table(
            ("MCP server", "Transport"), servers, "MCP catalog", empty="The catalog has no server."
        )
    )
    presets = (
        sorted(kit_file(setup, "presets").glob("*.toml"))
        if kit_file(setup, "presets").is_dir()
        else []
    )
    if presets:
        part.blocks.append(Table(("Example preset",), [(p,) for p in presets], "Preset examples"))
    part.sources = [
        p for p in (kit_file(setup, "kit.env.example"), setup.catalog_path) if p.exists()
    ]


Builder = Callable[[Setup, DocPart], None]
PARTS: tuple[tuple[str, str, Builder], ...] = (
    ("layers", "Layers and precedence", layers_part),
    ("flow", "Install, sync, update, doctor", flow_part),
    ("hosts", "Hosts and wiring", hosts_part),
    ("lifecycle", "Hook lifecycle", lifecycle_part),
    ("skills", "Skills", skills_part),
    ("commands", "Commands", commands_part),
    ("rules", "Rules and docs", rules_part),
    ("review", "Review and verification", review_part),
    ("safety", "Safety guards", safety_part),
    ("config", "Configuration", config_part),
)


def doc_parts(setup: Setup) -> list[DocPart]:
    """Every part; one that fails to build is its title, its concept text and the failure."""
    out = []
    for key, title, builder in PARTS:
        part = DocPart(f"docs-{key}", title, LEADS.get(key, ""))
        try:
            builder(setup, part)
        except (Exception, SystemExit) as error:  # noqa: BLE001  one unreadable source must not hide the rest
            first = str(error).splitlines()[0] if str(error) else ""
            part.blocks, part.alerts = [], [f"not read: {type(error).__name__}: {first}"]
        out.append(part)
    return out
