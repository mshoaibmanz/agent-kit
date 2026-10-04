# process-doc — reference

Design system, component markup, the screenshot system, and copy guidance. The whole token system
already lives in `assets/template.html`; this file shows how to fill it.

## Design system (do not change the tokens)

Industrial / warehouse-floor language. Cool concrete ground, hazard-tape amber accent,
monospace as a co-lead face (shelf codes / scan-terminal vernacular). Avoids the generic AI-doc
defaults (cream+serif, near-black+acid-green, broadsheet).

```
--ground #E6E7E2   --surface #F4F4F0   --surface-2 #FBFBF9
--ink #1E2127      --ink-soft #545960  --line #CBCDC5
--accent (amber) #D98A0B   --amber-deep #B5720A   --amber-tint #F3E5C6
--steel #41576B    --green #2E7D52     --red #BB3B2C
sans = system-ui stack · mono = ui-monospace stack
```

- **Type**: heavy uppercase system-sans for H1/H2 (signage), mono for eyebrows/codes/labels/data.
- **Hazard band**: `repeating-linear-gradient(45deg, ink 0 13px, amber 13px 26px)`. Use once or
  twice max as a section divider — it's the signature, not wallpaper.
- **Structure is information**: numbered markers (`01/02/...`) only when the content is a genuine
  ordered sequence (a real floor process). Don't number a set of parallel facts.

If a different subject genuinely needs a different palette, change the `:root` hexes once and let
every component inherit — never hardcode a color in a component.

## Components (markup lives in template.html)

Copy the matching block from `assets/template.html` and fill it. Available components:

| Component | When to use |
| --- | --- |
| Hero + Canvas | Open with the most characteristic thing. The Canvas slot in the template is for a live sort animation — keep it only if a live sort *is* the thesis; otherwise drop the `<canvas>`+script and lead with the headline. |
| Numbered steps (`.step`) | The ordered floor process. Each step: number + label, h3, short intro, `<ul>` of specifics. Add a `.step-media` figure for a screenshot. |
| Gate chips (`.chips`/`.chip`) | The checks/validations behind a step. Group by *when* they run (e.g. "At scan" vs "At placement"). `.chip.lock` for steel-dot (lock/serialize) gates. |
| Config flow (`.cfg-flow`) | The config/importer setup: a sheet sample → API import → enable. Includes `.sheet` (faux spreadsheet table), `.endpoint` (METHOD + route), `.btn` (link to the config sheet or API docs). |
| KV cards (`.kv`) | Runtime config keys and what each scopes. |
| Callout (`.callout`) | One load-bearing nuance worth pulling out (e.g. "the binding is stateless"). |

Sheet sample: show only the columns the user actually fills; note the derived columns in the
`.derive` line below it (e.g. "→ creates `<code>`, tagged `<type>`, active"). Pull the real
column names + validation regex from the importer.

## Config path (get it right)

A config flow names how data actually gets into the system: which sheet or upload holds the rows,
which endpoint runs the import, with which parameters, and who owns that data. Confirm each from
code and the user before writing the flow; the wrong endpoint is a real error. The company overlay's
`~/.claude/local/process-doc/config.md` documents the local importer mechanisms and API docs host;
without it, find the import route in code and link the service's own API docs.

## Screenshots

Two frame styles (the user picked "both, per step"):

- **Handheld device frame** (`.device`) — for flow steps; reads instantly as "the operator's
  screen". Portrait (9:16). Use for simple screens (lookup).
- **Flat annotated card** (`.shot-card`) — for screens needing call-outs. Landscape (16:10) with
  numbered `.pin`s positioned over the screen and a `.pin-legend`. Use for detailed screens (confirm).

Each figure has a slot. Until a real image exists, keep the `.shot-ph` placeholder (it shows
`<app> / <screen> / assets/<file>.png`). When you have the PNG, swap the placeholder for
`<img src="./assets/<file>.png" alt="...">` (the commented line in the template shows exactly where).

### The catalog (reuse across docs)

The catalog's `catalog.json` indexes every reusable screenshot. **Before** making a new
one, search it for a matching `process`/`app`/`screen`/`state`. To use one: copy its file into the
output `assets/` and reference it. **After** capturing/adapting a new screen, add an entry and save
the PNG into the catalog so future docs find it. Location, schema + how-to: [CATALOG.md](CATALOG.md).

### Getting a screenshot when you can't hit staging

The user often can't test on staging. In order of preference:

1. **Catalog hit** — reuse an existing one. Free.
2. **Adapt a neighbor** — the user provides a screenshot of an *adjacent* process (e.g. the
   lookup/confirm screens of a sibling flow). Relabel it to this process with `scripts/annotate.py`: cover the old
   title/codes/values with `cover` ops, write the new ones with `text` ops, then `pin` ops for the
   annotated-card call-outs. Record `source: "adapted from <process> <screen>"` in the catalog.
3. **Placeholder** — keep the labeled `.shot-ph` slot and tell the user which screen to send.

`uv run --with pillow scripts/annotate.py --help` — supports `cover` (filled rect to hide old UI),
`text` (label with optional background), `rect` (outline), `pin` (numbered amber circle). Pass ops
as JSON; coordinates are pixels from top-left. Always eyeball the output before shipping.

## Copy guidance

- **Strip internal code symbols for ops/config audiences.** No function/table names, no enum codes,
  no engine/lib names (`resolve_zone_config`-the-function, `id_shipment`, `libcore`, `in_transit`,
  `any_location`). Keep only what the reader actually types or sees: sheet tabs, importer keys,
  API routes, the codes they enter (`ZONE-####`, `site_code`), and the real config values. Translate
  status/enum codes to plain words ("Box is in transit", not "status = in_transit").
- **Link configs to the exact thing.** Deep-link a Google Sheet to the specific tab (`gid=…`), not the
  index; show a config key's *current value* ("Currently: `ZONE-2`"), not just its name.
- Write from the operator's side of the screen. Name things by what they control and recognize.
- Active voice, sentence case, plain verbs. "Scan the box", not "Box scanning is performed".
- An error line explains what's wrong and how to fix it, in the app's voice — quote the real string.
- Specific beats clever. No filler, no marketing. Each element does one job.
- A brand-new process has no "before" — don't write a changelog for it.

## Output

```
<process-name>-guide/
├── index.html        # copied from template.html, filled; CSS+JS inlined
└── assets/
    ├── step-lookup.png
    └── step-confirm.png
```

Verify with a local server (browser tool blocks `file://`):
`python3 -m http.server 8731` → navigate `http://localhost:8731/<dir>/index.html`. Check the
a11y snapshot for structure, console for errors, and that there's no horizontal scroll on mobile widths.
