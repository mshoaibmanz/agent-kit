---
name: process-doc
description: Generate a presentable, self-contained standalone HTML process guide for a feature/flow — for hosting on a static site. Industrial design system, numbered floor-process steps, gate chips, config/importer flow, an optional Canvas hero, and process screenshots (handheld device frame + flat annotated card) pulled from a reusable screenshot catalog. Use when the user wants to document a process/flow for ground ops / config / stakeholders, asks for a "process doc", "ops guide", "standalone HTML dump", "share this flow", or to add a screen/screenshot to an existing process guide.
---

# process-doc

Builds one standalone `index.html` (+ `assets/`) that explains a process to a non-engineer
audience (ground ops, config team, stakeholders). Self-contained and portable: drop the folder on
any static host. Design system + component snippets live in [REFERENCE.md](REFERENCE.md).

## Company overlay

Anything specific to one company lives outside this skill, in `~/.claude/local/process-doc/` when
present: `config.md` (how config is loaded and where the API docs are), `hosting.md` (where guides are
published) and `screenshot-catalog/` (the reusable screenshots and `catalog.json`). Read the `.md`
files there before step 2. Without an overlay, confirm the config path from code and ask the user
where the guide goes.

## Workflow

1. **Scope it.** Pin one subject, the audience, and the page's single job. Identify which sections
   apply: hero (± Canvas), numbered floor steps, gate chips, config/importer flow, screenshots.
2. **Gather the truth from code — never invent.** Use LSP/grep and the repo's domain docs.
   For each claim collect the real value: gate asserts + error strings, status/enum names, importer
   columns + validation regex, the API route, config keys. Cite where you got each.
3. **Build from the template.** Copy `assets/template.html` to the output dir as `index.html`. It
   carries the whole design system (tokens + components). Fill content by adapting the commented
   component skeletons in REFERENCE.md. Do not restyle — keep the token system intact.
4. **Screenshots** — see [REFERENCE.md](REFERENCE.md#screenshots). Search the catalog first
   (`uv run ${CLAUDE_SKILL_DIR}/scripts/catalog.py find --process <p> --screen <s>`); reuse a match,
   adapt a neighboring process's screen with `scripts/annotate.py`, or leave a labeled placeholder
   slot. Register anything new with `scripts/catalog.py add` so the next doc reuses it. Copy used PNGs
   into the output `assets/`.
5. **Verify the render.** Serve the dir and load it (file:// is blocked in the browser tool —
   `python3 -m http.server` then navigate). Check the accessibility snapshot for structure and read
   console for errors. Confirm: no horizontal scroll, Canvas draws, screenshots resolve, links work.
6. **Deliver the dump.** Output dir = `index.html` + `assets/` (PNGs only — CSS/JS are inlined).
   Tell the user the path; offer to publish a claude.ai Artifact preview (single-file: inline the
   screenshots as `data:` URIs or use placeholder slots, since an Artifact can't carry an assets/ folder).
   A guide that is already hosted lives in a plain dir (no git) — update that dir in place rather than
   starting a new dump, or the hosted page and the source diverge. When the host sits behind a login,
   **publishing is the user's manual step**: hand over the path, don't try to fetch or push the URL.

## Rules

- **Accuracy beats polish.** Every gate, code, regex, route, and enum must match the code. If you
  can't confirm a value, leave a clearly-marked TODO — don't guess.
- **New process ≠ changelog.** Only frame as "what changed / before→after" if the user confirms an
  existing process is being modified. For a brand-new flow, describe it as it is.
- **Copy is for the end user.** Name things by what the operator/config-person controls. Plain verbs,
  sentence case, no marketing. See the copy section in REFERENCE.md.
- **Spend boldness once.** One Canvas hero or one hazard band as the signature — keep the rest quiet.
- Keep the output a true standalone dump: inline all CSS/JS; the only external files are screenshots
  under `assets/`, referenced relatively (`./assets/...`).

## Backfill (`/process-doc backfill`)

To bulk-import historical screenshots from Google Drive into the catalog so future docs reuse them,
follow [BACKFILL.md](BACKFILL.md): enumerate Drive (the user's curated screenshot folder first) →
download/decode (delegate to a subagent for volume, to keep base64 out of context) → classify by
vision → `catalog.py add`. Idempotent (sha256 dedupe); re-run when new screenshots land in Drive.

## Files

- `assets/template.html` — base doc: full design system + commented component skeletons. Start here.
- `REFERENCE.md` — tokens, every component's markup, the screenshot system, copy guidance.
- `CATALOG.md` — the screenshot catalog's layout and schema.
- `BACKFILL.md` — bulk-import screenshots from Google Drive into the catalog.
- `scripts/catalog.py` — manage the catalog: `add`/`find`/`list`/`validate`/`reindex`, sha256 dedupe,
  `<process>/<screen>.png` placement. `uv run --with pillow scripts/catalog.py --help`. The catalog is
  `$PROCESS_DOC_CATALOG`, else `~/.claude/local/process-doc/screenshot-catalog/`.
- `scripts/annotate.py` — Pillow helper: cover/relabel regions of a neighbor's screenshot and bake
  annotation pins. `uv run --with pillow scripts/annotate.py --help`.
