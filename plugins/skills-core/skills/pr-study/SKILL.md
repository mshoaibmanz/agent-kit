---
name: pr-study
description: Understand a large PR or branch before reviewing it — cluster the diff into change units by intent, order them dependency-first, teach each one, and publish a navigable HTML study artifact you can keep asking questions about. Delegates bug-hunting to the thermos subagents and folds confirmed findings in. Use when the user says "study this PR", "walk me through PR <n>", "help me understand this branch", "/pr-study", or hands over a large PR they need to review or learn.
---

# pr-study

Comprehension first, verdict second. `/review` reads a diff top-to-bottom and returns a flat
opinion; on a 3,000-line PR that is unusable. This builds a **map** — the change units, what
each is for, the order to read them in — teaches them one at a time, and leaves behind an
Artifact you can come back to and interrogate.

Taxonomy, component markup, and the structural risk sweep live in [REFERENCE.md](REFERENCE.md).

## Scope

- `/pr-study 1234` — a GitHub PR
- `/pr-study ABC-123` — a local branch
- `/pr-study` — the current branch against its real base
- Anything after the target is extra instruction ("focus on the worker path", "I only care about the schema").

Never mix scopes. A PR's diff is the scope; local working-tree changes are out.

## Phase 0 — Gather

Run the script. Never eyeball line counts or file totals — every figure in the artifact comes
from `stats.json`.

```
python3 ${CLAUDE_SKILL_DIR}/scripts/gather.py <target> --out .claude/pr-study/<slug>/
```

`<slug>` is the PR number or branch name; a full PR URL works too. Pass `--out` an **absolute**
path — a `cd` earlier in the session persists and will otherwise scatter state into a worktree.
Writes `meta.json`, `diff.patch`, `files.json`, `stats.json`.

**Then read the two warnings the script prints, before reading any code:**

- `checkout_matches_pr: false` — **this checkout is not the PR.** Read every file from the
  `code_at` path instead; the current checkout returns pre-change code with no error, so anything
  you conclude from it is wrong. If `code_at` is null, stop and fetch the branch or add a worktree.
  **Pass `code_at` explicitly to every subagent you spawn** — they default to the main checkout
  and will silently audit the wrong tree.
- `stale_base: true` — the base has moved far ahead, so any CI verdict is against a stale tree.
  Note it in `study.md`, but keep it out of the artifact (see
  [REFERENCE.md](REFERENCE.md#what-the-artifact-must-not-contain)).
- **`code_at` can move under you.** It may be a worktree another live session is committing in
  (the script matches by SHA as well as branch name, and says so in `code_at_note`). Before citing
  any line, `git -C <code_at> rev-parse HEAD` and compare it with `origin/<head>`; if either moved,
  re-run gather and re-pin every anchor.

Branch mode auto-detects the base from where recent PRs actually merged, not from the ticket
prefix. Override with `--base` when you know better.

## Phase 1 — Frame

Before reading a single hunk, establish what the PR is *for*:

1. `meta.json` — title, body, author's own description, labels, base.
2. Ticket from the branch name or title → fetch it (Jira MCP) for the requirement behind the code.
3. The owning lib's domain docs — navigate the `CONTEXT-MAP.md` tree per the standing contract
   in `~/.claude/CLAUDE.md`; load only the 1–2 subjects the diff touches.

Output one paragraph: the problem, the author's claimed solution, and the gap between them if
there is one. If the PR body says nothing useful, say that plainly — it is itself a finding.

## Phase 2 — Map

The core of the skill. Cluster hunks into **change units by intent, not by file**, classify
each, and order them dependency-first. Full field list and the `core`/`propagation`/`test`/
`generated` split are in [REFERENCE.md](REFERENCE.md#change-units).

- Use Serena (`find_symbol`, `find_referencing_symbols`) to resolve what a changed symbol
  touches — that is what tells you a unit's real blast radius and which units depend on which.
  Grep is for plain text and known paths only.
- `files.json` pre-flags mechanical hunks (whitespace-only, import-only) and generated paths.
  Trust it to *demote*, never to promote — a one-line change in a `source` file can be the
  whole PR.
- Write the unit list to `study.md` before writing any prose. If the map is wrong the teaching
  is wrong.

Then state the headline as the **per-kind split** (`stats.json → by_kind`) plus which single unit
the PR exists for. Do **not** lead with `signal_pct` — it counts only non-mechanical `source` churn
as signal, so a PR carrying heavy real integration tests scores as mostly noise, which is exactly
backwards when those tests are the evidence the fix works. Quote it only alongside its caveat.

## Phase 3 — Teach

Walk the units in reading order. Per unit:

- **Before → after** in plain language, no diff vocabulary.
- **The lines that matter** — cite `file:line`, quote 2–5 lines, and say why *those* lines are
  the decision. On a propagation unit, say what forced it and move on; do not narrate 40 call sites.
- **What you need to know to judge it** — domain terms verbatim from `## Language`, the invariant
  it relies on, the caller that will hit it first.
- **One check-yourself question** with an answer the reader can verify in the diff.

Ask the user to stop you at any unit they want to go deeper on. This is a conversation, not a report.

## Phase 4 — Risk

Two tracks, run in parallel with Phase 3 so the artifact lands complete:

1. **Delegate the bug hunt.** Spawn `thermos:thermo-nuclear-review-subagent` and
   `thermos:thermo-nuclear-code-quality-review-subagent` **together in one message** via the
   Agent tool. Pass each the diff as `### Git / diff output` and the changed-file contents as
   `### Changed file contents`, plus the Phase 2 unit map as orientation, plus **`code_at` as the
   path they must read from** — state that the main checkout may be on another branch. Pass the
   head SHA too, and tell each agent to check `git rev-parse HEAD` in `code_at` against it before
   reading; on a mismatch they read the changed files with `git show <sha>:<path>`. A tree that
   moves mid-run otherwise splits the two reviews across two heads. Do not
   re-implement their rubric and do not invoke them via `Skill` — both are
   `disable-model-invocation`. Tell each what is already known-clear so they spend their run on
   open ground; then treat what comes back as claims, and re-verify anything you act on.
2. **Run the structural sweep yourself** — the repo-specific invariants in
   [REFERENCE.md](REFERENCE.md#structural-risk-sweep) that a fresh subagent cannot know:
   nullable-column readers, sync→async drain sites, deleted asserts, migration ordering,
   never-green tests. Each row resolves to `clear` or a register entry.

Merge both into one risk register, attributing every row. Keep **improvement candidates**
separate from risks — a suggestion the author may decline is not a finding they must address.

## Phase 5 — Publish

**First re-fetch the PR head.** `git fetch origin <head>`; if it moved since gather, re-run
`gather.py` and re-pin every anchor before writing a line. If this session is inside a ticket
worktree, the study dir under the main checkout is read-only to Edit/Write (isolation guard) —
run this phase from outside, or `ExitWorktree(keep)` first.

Copy `assets/template.html` to `.claude/pr-study/<slug>/study.html`, fill it per
[REFERENCE.md](REFERENCE.md#artifact-components), and publish with the `Artifact` tool
(`favicon: "🔬"`). Load the `artifact-design` skill first, as that tool requires.

**Look at the page before publishing.** Serve the dir (`python3 -m http.server <port>`; Playwright
cannot open `file://`) and open `http://localhost:<port>/<slug>/study.html`. Prepend a
`<meta charset="utf-8">` to a throwaway copy for the local render — `http.server` sends no charset,
so em dashes and arrows mojibake locally while the published artifact is fine. Then, with all
`<details>` forced open, run the geometry check and screenshot at 1280 and 420:

```js
document.querySelectorAll('details').forEach(d => d.open = true);
// any two text leaves whose per-line rects (getClientRects(), not getBoundingClientRect)
// overlap by >30% of the smaller — non-ancestor pairs only — is a layout bug.
// Skip anything inside a position:fixed overlay (the notes bar) — it floats over content
// by design, and a check that fires every run is a check nobody reads. Build that skip-set
// ONLY from elements whose computed position is 'fixed' (plus their descendants); adding
// every element yields leaves:0, which reads as a pass and is not one.
// Save screenshots to an absolute scratchpad path — Playwright's default is the repo root.
```

**If no browser is available** — Playwright errors `Browser is already in use for …/mcp-chrome-*`
(profile locked by another session) or claude-in-chrome is disconnected — do **not** claim a visual
pass. Fall back to a structural check and record in `study.md` that the visual pass is still owed:
parse the HTML for tag balance and nesting, assert no raw `<`/`>` inside `<pre>`, confirm every
`display:grid`/`flex` container wraps element children only, and compute contrast for every token
pair (≥4.5:1). That pass names real defects a screenshot wouldn't — a malformed selector list
ending in `@media`, for one.

Record the returned URL in `study.md`. Keep the file path and favicon stable — redeploying the
same path updates the same link, and a changed favicon reads as a different page.

**Light mode only.** These pages are read next to a diff and a terminal, and a dark study competing
with a light PR view is friction. Ship a single committed light theme — no `prefers-color-scheme`
block, no `data-theme` overrides, no dual-palette bookkeeping. Re-derive the light palette from the
subject as `artifact-design` requires; just don't build the second one.

**Density is the deliverable.** A study nobody finishes reading has failed, and prose is the thing
that makes it unfinishable. Hard limits per unit card: **before/after ≤25 words each**, **≤2 hunks**,
**note under a hunk ≤2 sentences**, **blast radius one sentence**. Risk rows: **≤40 words**, the
failure only — move the fix to Improvements. Cut every sentence that restates the code beneath it,
every "worth noting", and every recap of a section the reader just read. Prefer a table row to a
paragraph, a number to an adjective. If a unit needs more than that, it is two units.

## Phase 6 — Converse

The session stays open on the study. Handle follow-ups against the state dir:

- "explain u3 again, slower" / "why does this need the lock at all" — answer from the diff and
  the code, not from what the artifact already says.
- "add that to the questions" / "that risk is a non-issue, drop it" — edit `study.md`, re-render
  `study.html`, republish to the same URL.
- "now review it properly" — hand off to `/code-review` or the thermos pair with the unit map
  as scope. This skill does not issue verdicts.
- **Notes from the artifact.** Each hunk and unit carries a note box; the floating bar copies them
  all out in the format `/address-review` parses. When the user pastes them back — or saves them to
  `.claude/pr-study/<slug>/notes.md` — hand off to `/address-review`, which applies each at its
  anchor. That command runs in **the tree the branch is checked out in**; if the study's `code_at`
  is a worktree, say so explicitly rather than letting it edit the main checkout.

## Rules

- **The chat response carries the findings, not just the link.** Write the full finding list into
  the reply — grouped by severity, each with `file:line` and its concrete failure — and treat the
  artifact as the extra (unit map, annotated hunks, per-hunk notes). The artifact is a surface for
  working *through* a diff over time; review notes get read once and pasted onto the PR, so making
  the reader open a browser to find the substance adds a hop. Same for the copyable PR comments:
  print them, don't just link them.
- **Teach, don't verdict.** The deliverable is the reader's understanding. Bugs are Phase 4's
  job and they are delegated.
- **Every number is generated.** Counts, percentages, file totals come from `stats.json` — never
  transcribed. Diff the artifact's claims against that file before publishing.
- **Never cite a line number that isn't in the diff or the checked-out file.** If you cannot
  confirm a `file:line`, cite the symbol instead.
- **Don't invent domain language.** Terms come from the owning lib's `CONTEXT-MAP.md`. Unknown
  term with no doc entry → ask, don't guess.
- **The map is falsifiable.** If a unit's `depends_on` turns out wrong mid-teaching, fix the map
  and re-render — do not narrate around a wrong map.
- **Propagation is not content.** Resist explaining mechanical churn at length; naming what
  forced it is the whole job.
- **Never put `display: grid`/`flex` on an element whose content is bare inline markup.** A
  numbered `<li>` holding text plus `<code>`/`<span>`/`<em>` turns every one of those into its own
  grid item, and they spill into the counter column — the study's own prose is the payload, so
  this shreds it. Number such rows with an absolutely-positioned `::before` inside a padded block,
  or wrap the content in a single child element and keep the grid.

## Files

- `scripts/gather.py` — resolve scope, detect base, parse the diff, emit `meta/files/stats.json`.
  Stdlib only. `python3 scripts/gather.py --help`.
- `assets/template.html` — the study artifact shell: tokens, sidebar reading order, unit cards,
  annotated hunks, risk table, copyable questions, glossary. Theme-aware and self-contained.
- `REFERENCE.md` — change-unit taxonomy, structural risk sweep, component markup, publishing.
- `.claude/pr-study/<slug>/study.md` — per-study state: unit map, questions, risk register,
  Artifact URL. Read it first when resuming a study.
