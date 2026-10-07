# pr-study reference

Change-unit taxonomy, artifact component markup, and the structural risk sweep.
`SKILL.md` owns the workflow; this file owns the shapes.

## Change units

A **change unit** is the set of hunks that exist for one reason. Units are the review's
atoms — not files, not commits. A single unit routinely spans a table, a service, a route
and its test; a single file routinely carries three units.

Each unit carries:

| field | meaning |
| --- | --- |
| `id` | `u1`, `u2`, … — stable across redeploys; never renumber an existing unit |
| `title` | imperative, what it accomplishes (`gate invoice export on period state`) |
| `why` | the reason it is in this PR — the sentence the author would say out loud |
| `kind` | `core` · `propagation` · `test` · `generated` |
| `files` | paths + hunk anchors |
| `depends_on` | unit ids that must be understood first |
| `blast_radius` | what silently breaks if this unit is wrong |
| `key_lines` | the 1–3 `file:line` that carry the actual decision |
| `terms` | domain terms needed, sourced from the owning lib's `CONTEXT-MAP.md` |
| `check` | one question the reader should be able to answer after reading |

**Kinds.** `core` — a decision was made here; the behaviour is different afterwards.
`propagation` — a signature, import, or call site moved because a core unit forced it;
mechanical, verify-don't-read. `test` — asserts a core unit's contract. `generated` —
lockfiles, codegen, schema dumps; state the regeneration command and skip.

**Falsify `propagation` before accepting it.** Propagation means the *same* decision applied
elsewhere. Diff the unit against the sibling it supposedly follows: if it omits anything the
sibling has — a bound, a filter, a LIMIT, a test — it made a *different* decision and is `core`.
Misfiling a divergent unit as propagation is how a real defect gets described as mechanical churn
and skipped; that is a live failure of this skill, not a hypothetical one.

The split is the product. On a large PR most churn is `propagation` and `generated`.
`stats.json → signal_pct` reports what fraction is not — **do not lead with it.** It counts only
non-mechanical `source` churn as signal, so a PR carrying heavy real integration tests scores as
mostly noise, exactly backwards when those tests are the evidence the fix works. Lead with the
per-kind split; quote `signal_pct` only next to its caveat.

**Reading order** = topological sort on `depends_on`, then `core` before `propagation`
before `test` before `generated`, then smallest first inside a tier. Dependency-first in
practice means schema → model → service → route → consumer → test → FE.

## Structural risk sweep

Repo-specific invariants the deep-review subagents do not know about. Run every applicable
row; each is a claim you must resolve to `clear` or a risk-register entry, never left silent.

| trigger in the diff | what to check |
| --- | --- |
| column made nullable or widened | grep every reader and JOIN; classify each as *still correct*, not merely *won't crash*. A `LEFT JOIN` that tolerated NULL can still feed a required response field or split a `GROUP BY` key. Two queries behind one page can end up with opposite visibility rules. |
| a call moved sync → async behind a queue/worker | enumerate every trigger and consumer site; confirm the queue is drained in each affected test builder/flow. A missed drain breaks unrelated tests silently. |
| a string, assert, or method was moved or deleted | `grep -rn '<removed string>' tests/` — a dropped assert is a contract change, not cleanup. |
| a migration is present | ordering against concurrent deploys; is the column read before it is written on the old revision? |
| a test file is new or repaired | has this test ever passed in CI? A never-green test is suspect at the premise level — probe that the flow still reaches the asserted path before trusting a mechanical fix. |
| a feature flag or env var appears | is the off-path still the current behaviour, and who flips it? |
| the branch is long-lived | `meta.json → behind_base` / `stale_base`; a stale base means the CI verdict is against the wrong tree. |

## What the artifact must not contain

The artifact is a **review** deliverable. Environment and tooling state is not review signal, and
including it is the fastest way to make the page feel like machine output:

- worktree paths, `code_at`, commit SHAs, which branch the checkout was on
- base staleness / behind-counts, CI run ids
- anything about how the study was produced, beyond one footer line

All of it belongs in `study.md`, which is where you go when you need to reproduce or re-run. If a
piece of environment state genuinely changes how the code should be judged, translate it into a
reviewer-facing consequence ("any CI verdict here is against a stale base") and put *that* in the
risk register — never the raw count.

## Required diagrams

A unit-dependency graph is not enough. It shows how the *diff* is organised, not how the *code*
behaves, and on its own it leaves the reader still unable to picture the change running. Every
study carries these three, and the last two are what make a subtle bug legible:

1. **Unit dependency graph** — which units must be read before which. Mermaid `flowchart LR`.
2. **Runtime call graph** — the entry points, where they converge, and where the changed code sits
   in that order. Follow it with an ordered step list of the containing function, highlighting the
   step the PR touches. Position in a sequence is usually the whole argument for or against a
   change, and it is invisible in a diff.
3. **Scenario walkthrough — two outcomes, side by side.** Take the same mechanism and show it
   producing the intended result in one column and the defect in the other, as numbered steps with
   the actual codes/values. Name what differs between them in one line underneath. This is the
   single highest-value component: it converts "the guard unions history" into something a reader
   can check against their own understanding, and it makes the fix self-evident.

Build 2 and 3 as hand-authored HTML rather than mermaid when they carry annotations or emphasis —
mermaid's styling is not controllable enough to stay legible. Add a decision-tree diagram whenever
the PR introduces a new branch in how data resolves (`flowchart TB` with the outcomes as leaves).

## Artifact components

Fill `assets/template.html`. It carries the structure, the component set, and a **validated
starting palette** — not a fixed identity. Re-derive the palette from the subject per
`artifact-design`, keep the token names, and never ship the warm-cream-plus-terracotta or
near-black-plus-acid-green defaults. **Light theme only** — see SKILL.md Phase 5; the template has
no dark block and none should be added. Replace **every** occurrence of `STUDY_ID` in the script
with the study's slug — it keys both the read-progress state and the notes state, and a stale value
collides with another study's notes.

**Legibility floor — measure it, do not eyeball it.** Low contrast on secondary text is the most
common defect in these pages, and it is invisible to the author. Before publishing, compute the
ratio for every foreground/background pair and require ≥4.5:1; a five-line script
over the token list does it. Floors that held up: body ≥17px/1.65, tables and prose ≥15px, code
≥13px, and uppercase mono labels ≥11px — 9–10px uppercase mono is unreadable no matter its
contrast. Do not carry real content in the lightest ink token.

**Masthead + stat strip.** Every number comes from `stats.json`; none are typed by hand.

```html
<p class="eyebrow">sample-app · PR 1234</p>
<h1>Gate invoice export on ledger period state</h1>
<p class="sub">…</p>
<div class="meta-row">
  <span class="pill">base <code>release-2.4</code></span>
  <span class="pill">14 commits</span>
  <span class="pill">ABC-123</span>
</div>
<div class="stat-strip">
  <div class="stat"><div class="v">61</div><div class="k">files</div></div>
  <div class="stat"><div class="v">3,204</div><div class="k">lines changed</div></div>
  <div class="stat"><div class="v">9</div><div class="k">change units</div></div>
  <div class="stat"><div class="v">12%</div><div class="k">carries the decision</div></div>
</div>
```

**Shape section.** One mermaid block, plus one sentence naming the load-bearing idea.
Sequence diagram when the PR moves a call across a boundary; flowchart when it adds a gate
or branch; ER when it changes the schema.

```html
<pre class="mermaid">
sequenceDiagram
  participant R as route
  participant S as service
  R->>S: export(invoice_id)
  S-->>R: 409 period_open
</pre>
```

**Unit card.** `<details>`; opening one marks it read in the sidebar.

```html
<details class="unit" id="u3" data-kind="core">
  <summary>
    <span class="idx">3</span>
    <div>
      <h3>Reject export while the period is open</h3>
      <p class="why">The gate that the whole PR exists to add. Everything else propagates it.</p>
      <div class="tags">
        <span class="tag kind-core">core</span>
        <span class="tag">billing</span>
        <span class="tag">2 files · 41 lines</span>
        <span class="tag">needs u1</span>
      </div>
    </div>
  </summary>
  <div class="body">
    <div class="beforeafter">
      <div class="ba"><div class="k">Before</div><p>…</p></div>
      <div class="ba"><div class="k">After</div><p>…</p></div>
    </div>

    <div class="hunk">
      <div class="loc">src/billing/services/invoice.py:118</div>
      <pre><span class="c">    def export(self, invoice_id: str) -&gt; Export:</span>
<span class="a">+       if period.state is PeriodState.OPEN:</span>
<span class="a">+           raise ConflictError("period_open")</span>
<span class="c">        return self._export(invoice_id)</span></pre>
      <div class="note">The decision is these two lines. Everything else in the unit exists to make them reachable.</div>
    </div>

    <p><strong>Blast radius:</strong> …</p>
    <div class="check"><div class="k">Check yourself</div>Which caller sees the 409 first — the route, or the worker retry?</div>
  </div>
</details>
```

Escape `<` and `>` inside `<pre>` as `&lt;` / `&gt;`. Wrap every code block, table, and
diagram in its own horizontal scroller — the body must never scroll sideways.

**Risk register.** Merged: thermos findings + structural sweep. Attribute each row.

```html
<div class="tablewrap"><table>
  <thead><tr><th>Risk</th><th>Where</th><th>Severity</th><th>Source</th></tr></thead>
  <tbody><tr>
    <td>…<div class="src">reader still requires non-null</div></td>
    <td><code>src/ledger/queries/period.py:—</code></td>
    <td><span class="sev high">High</span></td>
    <td><span class="src">thermos</span></td>
  </tr></tbody>
</table></div>
```

**Questions.** Each is copyable as a standalone PR comment — self-contained, cites the
file, and asks one thing.

```html
<li>
  <div>
    <div class="q">Should the 409 be retryable? The worker treats ConflictError as terminal, so an open period drops the export permanently.</div>
    <div class="ctx">u3 · src/billing/services/invoice.py:118</div>
  </div>
  <button class="copy">copy</button>
</li>
```

**Improvements** go in their own section, never mixed with risks — a suggestion the author
can decline is not a finding they must address.

**Reviewer notes.** The script injects a note box into every `.hunk` (anchored to that hunk's
`.loc`) and one per unit, persists them in `localStorage`, and exposes a floating bar that copies
them out. **Author no note markup by hand** — the injection walks `.unit` and `.hunk`, so a
correctly-built unit card gets its boxes for free. What this buys: the reader annotates on the
surface where they understood the change — clustered by intent, in reading order — instead of on a
file list sorted alphabetically.

The exported format is the contract `/address-review` parses. One note per line, newlines within a
note collapsed:

```
src/billing/services/invoice.py:118 — u3 — bulk-fetch here, this is N+1
u3 — why not reuse the existing guard instead of a second one?
```

A `path:line` prefix means the note is anchored; a bare `<unit> — <text>` line is unit-level and the
addressing agent resolves the location. Notes never reach the published page — they are per-browser
state, which is why the footer says so and the bar exists.

**Glossary.** Only terms the reader needs that this repo defines. Pull the wording from the
owning lib's `CONTEXT-MAP.md` `## Language` section verbatim; if a term is not there, ask
rather than invent a definition.

## Publishing

Publish with the `Artifact` tool: `favicon` `"🔬"`, a one-sentence `description`, and a
`<title>` matching the headline. Keep the favicon and the file path fixed across redeploys —
same path means same URL. Record the returned URL in `study.md` so later turns update in
place instead of minting a new link.
