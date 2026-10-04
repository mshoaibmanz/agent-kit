# Stage 4 — SOP artifact (process changes only)

Trigger this stage **only** when Stage 0 flagged the feature as an operator-facing process
change — it adds, removes, or reorders something a human on the floor physically does. A
pure-backend change or an invisible tweak gets no SOP.

## Build it with `process-doc` — don't hand-roll HTML

Invoke the `process-doc` skill. It produces a **self-contained standalone HTML** process guide
built for a non-engineer audience (ground ops, config team, stakeholders): a hero, numbered
floor-process steps, gate chips for the validations behind a step, handheld **device-frame**
and flat **annotated-card** screenshots, and an industrial design system — publishable as a
claude.ai **Artifact** for the ops/end-user to open. Do not build a bespoke page; this skill
already solves the layout, the screenshot framing, and the reusable screenshot catalog.

Feed it:
- **Screenshots** from Stage 2 (`<scratchpad>/qa-<app>-<case>.png`) → the device-frame figures. Its
  device-free screenshot workflow (catalog reuse / annotate-a-neighbor / labeled placeholder)
  covers any state you couldn't capture on the emulator.
- **The acceptance rule** from Stage 0/1 → a gate chip (e.g. the priority-date condition).
- **The step sequence** → what the operator now does differently.

## Frame it as *what the operator does*, not *what the code does*

The SOP is a work instruction, not a changelog. Steps are imperative operator actions; the
new UI element is a thing they look for and act on. Example spine for a priority label added to
a handover screen:
1. Open the handover on the handheld.
2. For each package, read the package row.
3. **New:** packages showing a red **PRIORITY · PDD `<date>`** line are due today/tomorrow or
   overdue — physically segregate and handle these first.
4. Continue the normal pack/unpack scan flow for the rest.
- Gate chip: "Priority = due date (promised, else item PDD) is today, tomorrow, or past."

## Verify the artifact renders before handing it over
`process-doc` outputs `index.html` + `assets/`. Serve and check it — `python3 -m http.server
<port>` then open `http://localhost:<port>/<dir>/index.html` (the browser tool blocks `file://`).
Confirm the screenshots embed and the steps read cleanly, then publish the Artifact and give the
user the URL.

## Guardrail
The SOP describes a process to real operators — keep it accurate to what you actually observed in
Stage 2. **Every screen visual must be a real capture** (Stage-2 emulator/Storybook screenshot) or
an honest `process-doc` `.shot-ph` placeholder — **never a hand-built HTML/CSS mockup dressed to
look like the app**. If you only reached a lower verification rung, the process steps are still
valid (the UI is real) but say so, and leave placeholders where you have no real screenshot rather
than fabricating one.
