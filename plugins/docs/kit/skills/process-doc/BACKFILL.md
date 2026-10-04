# Backfill — import historical screenshots from Google Drive into the catalog

One-time (and re-runnable) pull of existing process screenshots out of Google Drive into the reusable
[screenshot catalog](CATALOG.md). After this, `/process-doc` reuses or adapts these
instead of asking for a fresh capture. Run it via `/process-doc backfill` or when the user says
"import my Drive screenshots".

The hard part is classification: Drive screenshots are timestamp-named (`Screenshot_YYYYMMDD-…png`)
and dumped in one folder, so the `process`/`app`/`screen`/`state` come from **looking at each image**,
not the filename. That judgement is the agent's; `scripts/catalog.py` does the deterministic placement,
hashing, and indexing.

## Loop

1. **Enumerate.** Find the screenshot source(s) in Drive with the `Google_Drive` MCP
   (`search_files`, e.g. `parentId = '<folder id>' and mimeType contains 'image/'`). Ask the user for
   their curated screenshot folder first; loose root screenshots are a messier second pass (filter out
   non-process images: photos, gifs, query-result exports).
2. **Download + decode without flooding context.** `download_file_content` returns base64. For more than
   a couple of images, delegate the download/decode/classify to a subagent (Agent tool) so the base64
   never enters the main context — have it write each PNG to a staging dir and return only compact
   classifications. For a couple, inline is fine: write the base64 to a `.b64` file, then
   `base64 -d in.b64 > out.png` (macOS) or `python3 -c "import base64,pathlib;pathlib.Path('out.png').write_bytes(base64.b64decode(pathlib.Path('in.b64').read_text()))"`.
3. **Classify by vision.** Read each PNG and infer:
   - `app` — which app or portal (chrome, nav, terminology give it away).
   - `screen` — lookup / confirm / manifest / list / detail / … (the screen's job).
   - `state` — the data/condition shown (e.g. "guard rejects a mixed batch").
   - `process` — which flow it belongs to. **Reuse existing process slugs** (`scripts/catalog.py list`
     shows them, e.g. `returns_sorting`, `inbound_receiving`) so neighbors group together.
     When unsure, propose the taxonomy to the user before committing names — process names must match
     their doc conventions.
   - `frame` — `device` for simple operator screens, `card` for screens that need call-outs.
4. **Register.** One `catalog.py add` per image (see below). It hash-dedupes (re-runs and Drive dupes
   are no-ops), copies the PNG to `<process>/<screen>.png`, and upserts the index entry with
   `drive_id` provenance.
5. **Verify.** `scripts/catalog.py validate` (files exist + hashes match) and `list` (eyeball the
   taxonomy). Spot-check a couple of placed PNGs render right.

## Register command

```
uv run --with pillow scripts/catalog.py add --src staging/abc.png \
    --process returns_sorting --app scanner --screen lookup \
    --state "suggested bucket; the first box of a batch shows any location" \
    --frame device --drive-id <drive file id> --source captured
```

For a `card` screen, also pass `--annotations '[{"pin":1,"label":"..."}]'`.

## Notes

- **Idempotent.** Dedup is by file content (sha256), so re-running after adding new Drive screenshots
  only ingests the new ones.
- **Drive stays the cold archive; the catalog is the working set.** Don't round-trip to Drive at
  doc-build time — build from the catalog. Re-run backfill only when new screenshots land in Drive.
- **Provenance.** Every imported entry records its `drive_id`, so any asset traces back to its origin.
- **Pending entries.** If a needed screen isn't in Drive, leave/keep its `pending` entry (file `null`)
  describing what's needed, so a future run or the user knows to supply it.
