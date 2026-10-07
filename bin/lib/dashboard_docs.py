"""The docs view: one DocPart per subject, built each time the page is. Its lists and tables are
read from the files each part names (the hook registry, SKILL.md frontmatter, the rules files render
joins, the host table in hosts.py, the bin scripts' own argument parsers, kit.env.example, the MCP
catalog and roles.toml), through the row builders the Setup view uses. The concept text is
hand-written: LEADS, FIRST, EVENT_WHEN, REVIEW_STEPS and the figures' labels."""

from __future__ import annotations

import argparse
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dashboard_diagrams import Chip, HostBox, Layer, Moment, layers, lifecycle, review_loop, wiring
from dashboard_html import (
    Badge,
    Code,
    Command,
    Docs,
    DocPart,
    FileLink,
    Figure,
    Lines,
    Muted,
    Prose,
    Row,
    Steps,
    Strong,
    Table,
    anchor,
    home_label,
)
from dashboard_sections import (
    DATA_WRAPPERS,
    HookRow,
    Setup,
    TableRows,
    docstring_line,
    documented_keys,
    hook_table,
    kit_command,
    layer_cell,
    role_table,
    round_table,
    script_parser,
    server_line,
    setup_command,
    skill_text,
)
from hosts import EVENTS, HOSTS, RENDERED_FILES, frontmatter, skill_dirs
from kit_env import layers as env_layers
from kit_text import HOST_HOME_ENV, PACK_DIR, RULES_FILES

TITLE = "How the kit works"
INTRO = """
What the kit is made of and how it reaches each agent host. Every list, table and diagram is read
from the kit's own files each time this page is built, and each part links the files it read; only
the short concept text is written by hand.
"""
FIRST = (
    ("docs-layers", "Three layers: the public engine, an org pack and your overlay. The more personal one wins."),
    ("docs-flow", "`agent-setup` installs and records your choices; `agent-kit sync` re-renders after an edit."),
    ("docs-hosts", "Every host gets the same rules, hooks, skills and MCP servers, each in its own format."),
    ("docs-lifecycle", "Hooks run at fixed points of a session; a blocking one can refuse the step."),
    ("docs-review", "Tests, a self-check and review rounds stand between an edit and merge-ready."),
)
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
them without asking. Each step is a command to run in your own terminal; its text is the command's
own help.
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
**UserPromptSubmit** reads the prompt; **PreToolUse** can refuse a tool call before it runs;
**PostToolUse** records what happened; **Stop** checks the turn before it ends.

A blocking hook can refuse; an advisory one only adds a line. Setup installs the blocking ones
only with `--blocking-hooks`. Outside the host, a git layer that only agent shells see runs
`commit-msg` and `pre-push`, so a push from any tool passes the same gate.
""",
    "skills": """
A skill is a folder with a `SKILL.md`: frontmatter (name, description, the hosts it supports) and
the instructions a host loads when the description matches the task. Each skill is listed once,
under the layer setup takes it from.
""",
    "commands": """
The kit's commands live in `bin/`; each purpose below is its own docstring or header comment, and
the subcommands come from its own argument parser. Slash commands are prompt files a host offers as
`/name`.
""",
    "rules": """
Rules are the always-loaded instructions. Render joins the engine's shared rules with the host's
own file, appends each block the table lists after them, and writes the result into each host's
global instructions file between kit markers. Reference docs are read on demand, never loaded whole.
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
- **Production data is read-only by construction.** The data wrappers allow reads only, and the
  shell guard refuses raw database clients, so the wrappers are the only way in.
- **Secrets stay in the OS secret store.** The catalog names a credential by service and account;
  render resolves it, and this page checks presence only.
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
# When each event fires, in session order; the registry decides which appear (others go last).
EVENT_WHEN = {
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
GIT_WHEN = "in agent shells only"
# The steps around the first two [review] lists in agent-kit's REVIEW_LISTS order.
REVIEW_STEPS = {
    "before": (
        ("Edit", "each source edit is marked for review and unlocks a test run"),
        ("Tests", "through the repo's own wrapper; each run is recorded"),
        ("Stop", "a self-check sized to the change, then a TALLY line"),
        ("Push gate", "git pre-push: review, tests, sprawl, force"),
    ),
    "fix": ("Fix", "address each finding by its prefix in one new commit"),
    "done": ("Merge-ready", "CI green, no open finding"),
}
# Render's constants for the rules blocks it appends, in order; one the engine lacks is skipped.
APPENDED_RULES = {
    "TEAM_RULES": "org pack: appended for every host",
    "OVERLAY_RULES": "personal overlay: appended after the pack's",
}


def kit_file(setup: Setup, relative: str) -> Path:
    """The source checkout's file (the whole kit, every hook and skill whether installed or not),
    else the installed kit's copy."""
    source = setup.source() / relative
    return source if source.exists() else setup.kit / relative


def label(setup: Setup, path: Path) -> str:
    """path relative to the source checkout or the kit, else written from ~."""
    for root in (setup.source(), setup.kit):
        if path.is_relative_to(root) and path != root:
            return str(path.relative_to(root))
    return home_label(path)


def from_files(setup: Setup, *paths: Path) -> list[Path | FileLink]:
    return [FileLink(p, label(setup, p)) for p in paths if p.exists()]


def shown(setup: Setup, command: str) -> Command:
    """command to copy, printed with the kit and source paths as <kit> and <source>."""
    text, source = command, str(setup.source())
    if source != str(setup.kit):
        text = text.replace(source, "<source>")
    return Command(command, text.replace(str(setup.kit), "<kit>"))


def registry(setup: Setup) -> tuple[Path, list[HookRow]]:
    path = kit_file(setup, "hooks/registry.json")
    return path, setup.registry(path)


def installed_hooks(setup: Setup) -> set[tuple[str, str]] | None:
    """(event, hook) pairs the install's own registry holds; None when it is the source's."""
    installed = setup.kit / "hooks/registry.json"
    if installed.resolve() == kit_file(setup, "hooks/registry.json").resolve():
        return None
    return {(r.event, r.name) for r in setup.registry(installed)}


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

    source, pack, local = setup.source(), setup.kit / PACK_DIR, setup.kit / "local"
    overlay_files = [p for p in env_layers() if p.exists() and p.parent != local]
    stack = [
        Layer(
            "Public engine",
            home_label(source),
            entries(source, ("local", PACK_DIR, "state", "LICENSES")),
        ),
        Layer("Org pack", home_label(pack), entries(pack) if pack.is_dir() else "no pack installed"),
        Layer(
            "Personal overlay",
            home_label(local),
            entries(local) if local.is_dir() else "no local/ folder yet",
        ),
    ]
    part.sources = from_files(setup, source, pack, local)
    figure = layers(
        stack, "Rendered into", list(HOSTS), "a higher layer wins", "The kit's layers and the hosts they render into"
    )
    part.blocks.append(Figure(figure, "Lowest layer at the bottom; a higher layer wins."))
    rows: TableRows = [(Strong(layer.title), Code(layer.where), layer.holds) for layer in stack]
    rows += [
        (Strong("Overlay keys elsewhere"), Code(home_label(p)), "a kit.env layer kit_env reads")
        for p in overlay_files
    ]
    part.blocks.append(Table(("Layer", "Where", "Holds"), rows, "What each layer holds"))


def subcommands(parser: argparse.ArgumentParser | None) -> dict[str, str]:
    """name -> help of every subcommand parser declares, nested ones as "mcp describe", and of each
    choice of a positional argument, whose help gives each as "name: text; name: text"."""
    out: dict[str, str] = {}
    if parser is None:
        return out
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:
                out[choice.dest] = choice.help or ""
                for name, text in subcommands(action.choices.get(choice.dest)).items():
                    out[f"{choice.dest} {name}"] = text
        elif action.choices and not action.option_strings:
            said = dict(
                (m.group(1), m.group(2).strip())
                for m in re.finditer(r"(?:^|;\s*)([\w-]+):\s*([^;]*)", action.help or "")
            )
            out.update({str(c): said.get(str(c), "") for c in action.choices})
    return out


def sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".") if text else ""


def flow_part(setup: Setup, part: DocPart) -> None:
    source = setup.source()
    setup_script = source / "bin/agent-setup"
    setup_parser = script_parser(setup_script)
    kit_says, setup_says = subcommands(setup.api.parser()), subcommands(setup_parser)
    part.sources = from_files(setup, setup_script, setup.kit / "bin/agent-kit")

    def step(title: str, said: str, command: str) -> tuple[str, str, Command | None]:
        return title, sentence(said), shown(setup, command)

    if setup.state:
        steps = (
            step("Install", setup_says.get("setup", ""), setup_command(setup, "--apply")),
            step("Sync", kit_says.get("sync", ""), kit_command(setup, "sync")),
            step("Update", setup_says.get("update", ""), setup_command(setup, action="update")),
            step("Doctor", kit_says.get("doctor", ""), kit_command(setup, "doctor", "--host", "all")),
            step(
                "Roll back",
                setup_says.get("rollback", ""),
                setup_command(setup, "JOURNAL_ID", action="rollback"),
            ),
            step("See it", kit_says.get("dashboard", ""), kit_command(setup, "dashboard")),
        )
    else:
        # agent-setup refuses to install into the source checkout itself.
        default = setup_parser.get_default("root_dir") if setup_parser is not None else None
        target = default if default and Path(default).resolve() != source.resolve() else "INSTALL_DIR"
        install = [str(setup_script), "--source", str(source), "--root-dir", str(target), "--apply"]
        host = (setup.configured or list(HOSTS))[0]
        steps = (
            step("Install", setup_says.get("setup", ""), shlex.join(install)),
            step("Render", kit_says.get("render", ""), kit_command(setup, "render", "--host", host)),
            step("Doctor", kit_says.get("doctor", ""), kit_command(setup, "doctor", "--host", "all")),
            step("See it", kit_says.get("dashboard", ""), kit_command(setup, "dashboard")),
        )
        part.blocks.append(
            Prose(
                "This page reads a source checkout with no install record: install it into a "
                "folder of its own first. Sync, update and rollback work on an install."
            )
        )
    part.blocks.append(Steps(steps))


def hosts_part(setup: Setup, part: DocPart) -> None:
    _, rows_ = registry(setup)
    boxes: list[HostBox] = []
    rows: TableRows = []
    for host in HOSTS:
        root = setup.roots[host]
        folders = [Code(home_label(d) + "/") for d in skill_dirs(host, root)]
        files = (
            *RENDERED_FILES[host],
            *(d.name + "/" for d in skill_dirs(host, root) if d.parent == root),
        )
        boxes.append(HostBox(host, home_label(root), files, host in setup.configured))
        events = sorted({r.event for r in rows_ if host in r.hosts})
        named = [f"{e} ({EVENTS[e]})" if host == "cursor" and e in EVENTS else e for e in events]
        count = sum(host in r.hosts for r in rows_)
        env = HOST_HOME_ENV.get(host)
        state = Badge("configured", "ok") if host in setup.configured else Badge("not configured")
        rows.append(
            Row(
                (
                    (Strong(host), state),
                    (Code(home_label(root)), Muted(f"or ${env}") if env else ""),
                    Lines(tuple(Code(f) for f in RENDERED_FILES[host])),
                    Lines(tuple(folders)),
                    (f"{count} hook rows", Muted(", ".join(named) or "-")),
                ),
                anchor("docs-host", host),
            )
        )
    sources = ["hooks/registry.json", "rules/", "skills/", "roles.toml", "mcp/servers.json"]
    sources = [s for s in sources if kit_file(setup, s.rstrip("/")).exists()]
    part.sources = from_files(setup, kit_file(setup, "bin/lib/hosts.py"))
    figure = wiring(
        "Kit sources",
        sources,
        ("agent-setup", "agent-kit render"),
        boxes,
        "read",
        "write",
        "not configured",
        "How the kit's sources reach each host",
    )
    part.blocks.append(Figure(figure, "Render writes only the files each host reads."))
    part.blocks.append(
        Table(
            ("Host", "Config root", "Files render writes", "Skills read from", "Hook events"),
            rows,
            "Per host",
            fold=True,
        )
    )


def lifecycle_part(setup: Setup, part: DocPart) -> None:
    path, rows_ = registry(setup)
    by_event: dict[str, list[Chip]] = {}
    for hook in rows_:
        chips = by_event.setdefault(hook.event, [])
        if all(c.text != hook.name for c in chips):
            chips.append(Chip(hook.name, hook.blocking))
    order = [e for e in EVENT_WHEN if e in by_event] + [e for e in by_event if e not in EVENT_WHEN]
    moments = [Moment(e, EVENT_WHEN.get(e, ""), tuple(by_event[e])) for e in order]
    git = kit_file(setup, "git-hooks/agent")
    git_hooks = sorted(p for p in git.iterdir() if p.is_file()) if git.is_dir() else []
    moments += [Moment(f"git {p.name}", GIT_WHEN, (Chip(p.name, True),)) for p in git_hooks]
    part.sources = from_files(setup, path, *([git] if git_hooks else []))
    figure = lifecycle(
        moments,
        "can refuse (blocking)",
        "advisory",
        "no hook",
        "Which hooks fire at each point of a session",
    )
    part.blocks.append(Figure(figure, "Each hook appears once per event, however many matchers it has."))
    installed = installed_hooks(setup)
    note = (
        ""
        if installed is None
        else "A row the install's own registry does not hold is marked not installed."
    )
    part.blocks.append(
        hook_table(
            rows_,
            kit_file(setup, "hooks"),
            "docs-hook",
            FileLink,
            installed,
            title="Every hook row",
            note=note,
            fold=True,
        )
    )
    git_rows: TableRows = [(Strong(p.name), header_line(p), FileLink(p)) for p in git_hooks]
    if git_rows:
        part.blocks.append(
            Table(("Git hook", "What it does", "Source"), git_rows, "Git hooks in agent shells", fold=True)
        )


def skills_part(setup: Setup, part: DocPart) -> None:
    rows: TableRows = []
    for entry in setup.skills:
        text, _, allowed = skill_text(entry.md)
        rows.append(
            Row(
                (Strong(entry.name), layer_cell(entry), allowed, FileLink(entry.md)),
                anchor("docs-skill", f"{entry.layer}-{entry.name}"),
                frontmatter(text).get("description", ""),
            )
        )
    part.sources = from_files(setup, setup.source() / "skills", setup.kit / PACK_DIR / "skills")
    part.blocks.append(
        Table(
            ("Skill", "Layer", "Hosts", "Source"),
            rows,
            "Every skill",
            empty="No skills found.",
            fold=True,
        )
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
    line = re.sub(rf"^(?:\w+ )?{re.escape(path.name)}\s*(?::|—|-)\s*", "", line)
    return line[:1].upper() + line[1:]


def command_parser(setup: Setup, path: Path) -> Any:
    """agent-kit's parser from the running engine (loading a second agent-kit would swap the
    modules this page imported), any other script's from its own parser()."""
    return setup.api.parser() if path.name == "agent-kit" else script_parser(path)


def commands_part(setup: Setup, part: DocPart) -> None:
    folder = kit_file(setup, "bin")
    rows: TableRows = []
    for path in sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")):
        subs = subcommands(command_parser(setup, path))
        cells = tuple((Code(name), Muted(said)) for name, said in subs.items())
        rows.append(
            Row(
                (Strong(path.name), Lines(cells) if cells else Muted("-"), FileLink(path)),
                anchor("docs-cmd", path.name),
                header_line(path),
            )
        )
    part.blocks.append(Table(("Command", "Subcommands", "Source"), rows, "bin/ commands", fold=True))
    slash = kit_file(setup, "commands")
    slash_rows: TableRows = []
    for md in sorted(slash.glob("*.md")) if slash.is_dir() else []:
        meta = frontmatter(md.read_text(errors="replace"))
        slash_rows.append(
            Row(
                (Strong(f"/{md.stem}"), FileLink(md)),
                anchor("docs-slash", md.stem),
                meta.get("description", ""),
            )
        )
    part.blocks.append(
        Table(
            ("Slash command", "Source"),
            slash_rows,
            "Slash commands",
            empty="No slash commands.",
            fold=True,
        )
    )
    part.sources = from_files(setup, folder, slash)


def rules_part(setup: Setup, part: DocPart) -> None:
    """The files render joins into each host's rules: the engine's two, then each block render
    appends (APPENDED_RULES), read from the kit render runs on."""
    every = tuple(HOSTS)
    kit = setup.kit
    candidates: list[tuple[Path, tuple[str, ...], str]] = [
        (kit / "rules/AGENTS.md", every, "engine: shared by every host"),
        *((kit / f"rules/hosts/{h}.md", (h,), f"engine: {h} only") for h in HOSTS),
    ]
    for name, layer in APPENDED_RULES.items():
        path = getattr(setup.api, name, None)
        if isinstance(path, Path):
            candidates.append((path, every, layer))
    rows: TableRows = []
    for path, hosts, layer in candidates:
        if not path.is_file():
            continue
        words = len(path.read_text(errors="replace").split())
        targets = tuple(Code(home_label(setup.roots[h] / RULES_FILES[h])) for h in hosts)
        rows.append(
            Row(
                (Strong(label(setup, path)), layer, Lines(targets), f"{words} words", FileLink(path)),
                anchor("docs-rule", label(setup, path)),
            )
        )
    part.blocks.append(
        Table(("File", "Layer", "Written into", "Size", "Source"), rows, "Rules files")
    )
    docs = [kit_file(setup, "README.md"), kit_file(setup, "hooks/lib/README")]
    references = kit_file(setup, "references")
    docs += sorted(references.glob("*.md")) if references.is_dir() else []
    doc_rows: TableRows = [
        (Strong(p.name), first_heading(p), FileLink(p)) for p in docs if p.is_file()
    ]
    part.blocks.append(Table(("Doc", "Subject", "Source"), doc_rows, "Reference docs", fold=True))
    part.sources = from_files(setup, kit / "rules")


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
    part.sources = from_files(setup, Path(setup.api.ROLES), setup.kit / "agents")
    review: dict[str, list[str]] = dict(roles.review) if roles is not None else {}
    keys = [k for k in setup.api.REVIEW_LISTS if k in review]
    rounds = [(f"[review] {k}", ", ".join(review[k]) or "-") for k in keys[:2]]
    stages = [
        *REVIEW_STEPS["before"],
        *rounds[:1],
        REVIEW_STEPS["fix"],
        *rounds[1:],
        REVIEW_STEPS["done"],
    ]
    notes = [f"[review] {k}: {', '.join(review[k])}" for k in keys[2:]]
    figure = review_loop(
        stages,
        notes,
        "until a round finds nothing new",
        "The verification and review loop before a push and after it",
    )
    part.blocks.append(Figure(figure, "The rounds and their roles come from roles.toml [review]."))
    if roles is None:
        part.alerts.append(setup.roles_error or "This kit has no roles.toml.")
        return
    part.blocks.append(role_table(setup, "docs-role", FileLink, title="Roles", fold=True))
    part.blocks.append(round_table(setup, title="Review rounds", fold=True))


def safety_part(setup: Setup, part: DocPart) -> None:
    path, rows_ = registry(setup)
    guards = list({r.name: r for r in reversed(rows_) if r.blocking}.values())[::-1]
    rows: TableRows = [
        Row(
            (Strong(r.name), r.event, FileLink(kit_file(setup, f"hooks/{r.name}"))),
            anchor("docs-guard", r.name),
            r.description,
        )
        for r in guards
    ]
    part.blocks.append(Table(("Guard", "First event", "Source"), rows, "Hooks that can refuse", fold=True))
    tools = [kit_file(setup, f"bin/{n}") for n in DATA_WRAPPERS]
    tools += [kit_file(setup, f"bin/lib/{n}.py") for n in ("secret_store", "credentials")]
    tool_rows: TableRows = [
        (Strong(p.name), header_line(p), FileLink(p)) for p in tools if p.is_file()
    ]
    part.blocks.append(
        Table(
            ("Tool", "What it does", "Source"),
            tool_rows,
            "Data wrappers and the secret store",
            fold=True,
        )
    )
    part.sources = from_files(setup, path)


def config_part(setup: Setup, part: DocPart) -> None:
    keys: TableRows = [
        Row((Code(k),), anchor("docs-key", k), meaning)
        for k, meaning in sorted(documented_keys(setup).items())
    ]
    part.blocks.append(
        Table(
            ("kit.env key",),
            keys,
            "Overlay keys",
            empty="kit.env.example documents no key.",
            fold=True,
        )
    )
    servers: TableRows = []
    for name in sorted(setup.catalog):
        transport, target, description = server_line(setup, name)
        servers.append(
            Row((Strong(name), transport, Code(target)), anchor("docs-mcp", name), description)
        )
    part.blocks.append(
        Table(
            ("MCP server", "Transport", "Command / URL"),
            servers,
            "MCP catalog",
            empty="The catalog has no server.",
            fold=True,
        )
    )
    presets_dir = kit_file(setup, "presets")
    presets = sorted(presets_dir.glob("*.toml")) if presets_dir.is_dir() else []
    if presets:
        rows: TableRows = [(Strong(p.name), FileLink(p)) for p in presets]
        part.blocks.append(Table(("Example preset", "Source"), rows, "Preset examples", fold=True))
    part.sources = from_files(setup, kit_file(setup, "kit.env.example"), setup.catalog_path)


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


def doc_parts(setup: Setup) -> Docs:
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
    return Docs(TITLE, INTRO, out, FIRST)
