# Screenshot catalog

Reusable app/portal screenshots for `process-doc`. The goal: capture or adapt a screen once, reuse
it across every future guide. The catalog is company data, so it lives outside this skill:
`$PROCESS_DOC_CATALOG`, else `~/.claude/local/process-doc/screenshot-catalog/`. PNGs are laid out in
`<process>/<screen>.png` subfolders there; `catalog.json` indexes them. Use `scripts/catalog.py` to
search and add — don't hand-edit `catalog.json` or move PNGs by hand (the script keeps paths, hashes,
and the index in sync). The first `add` creates the catalog.

## Schema (`catalog.json` → `screenshots[]`, version 2)

| field | meaning |
| --- | --- |
| `id` | stable kebab id, e.g. `scanner-confirm-zone` |
| `file` | relative path `<process>/<screen>.png` in the catalog, or `null` if still pending |
| `status` | `ready` \| `pending` \| `adapted` |
| `process` | the flow it belongs to, e.g. `returns_sorting` |
| `app` | the app or portal name |
| `screen` | `lookup` \| `confirm` \| `manifest` \| ... |
| `state` | what's on screen (the data/condition shown) |
| `frame` | `device` (handheld) \| `card` (flat annotated) — the default frame to render it in |
| `source` | how it was made: `captured` / `adapted from <process> <screen>` |
| `drive_id` | originating Google Drive file id (provenance), or `null` |
| `sha256` | content hash — the dedupe key; maintained by `catalog.py`, never hand-set |
| `annotations` | for `card` frames: `[{pin, label}]` — the call-outs to bake or render as `.pin`s |

## Using one in a doc

1. Search: `uv run scripts/catalog.py find --process <p> --screen <s>` (matches process/app/screen,
   substring on state).
2. Copy its `file` into the output guide's `assets/` and reference `./assets/<basename>`.
3. Render in its `frame`; for `card`, place a `.pin` per annotation and mirror the labels in the legend.

## Adding one

Always via `uv run --with pillow scripts/catalog.py add ...` — it hash-dedupes, copies the PNG to
`<process>/<screen>.png`, and upserts the entry. It infers `status` (`adapted` when `--source` starts
with `adapted`, else `ready`).

- **Captured** (real screenshot): `add` the PNG with `--source captured`.
- **Adapted** (relabeled neighbor): relabel an adjacent process's screen with `scripts/annotate.py`
  (cover old labels/codes, write the new ones), then `add` the result with
  `--source "adapted from <process> <screen>"`.
- **Bulk from Drive**: see [BACKFILL.md](BACKFILL.md).
- **Pending**: no image yet — keep a `pending` entry (`file: null`) describing what's needed so the
  next run knows to ask for it.

Keep `id`s stable — docs may reference them. Don't delete a `ready` entry that a guide still uses.
Run `scripts/catalog.py validate` after edits; `reindex` repairs hashes/status from on-disk files.
