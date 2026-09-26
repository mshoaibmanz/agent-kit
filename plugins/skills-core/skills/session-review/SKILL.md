---
name: session-review
description: End-of-session retro. Routes each mistake, learned fact, environment caveat and durable rule to its ONE config layer after a dedup check across every layer, then applies the change-set (adds, edits, deletes, archives) on a single confirmation. Use at the end of a work session, on "session review" / "retro" / "what did you learn". "session-review audit" runs the periodic whole-setup sweep: spend and review-precision scripts, contradictions, dead references, budgets.
---

# Session Review

Review the full current conversation and produce a single change-set, ordered:

1. **Mistakes made or nearly made** — wrong assumptions, bad edits caught late, tool misuse, anything the user had to correct.
2. **Project-specific facts learned** — non-obvious facts about this codebase/domain not derivable from the code itself. **Diff against the domain docs; do not rely on recall.** For each `lib*` the session touched, open its `CONTEXT-MAP.md`, read the subject(s) the work actually hit *plus the "Proposed subjects (not yet written)" list*, and state what this session established that is absent or wrong there. A fact the session had to rediscover from source is a documentation gap by definition; a proposed subject the session just explored in depth is the strongest signal of all. Route the gaps to the docs-rollup worktree below.
3. **Local testing/environment caveats** — setup quirks, flaky commands, env vars, DB/pod/worktree gotchas.
4. **Durable rules worth saving** — guidance that would have prevented a mistake or helps future ones. Route each to its ONE layer (below) and run the dedup check first.
5. **Consolidate / delete / archive** — config now redundant, misfiled, or stale. This is what stops sprawl — treat it as first-class, not optional.
6. **Too temporary to save** — in-flight branches, one-off workarounds, transient state.

## The layer model — every durable fact has exactly ONE home

| Layer | Path | Holds |
|---|---|---|
| Global rules | `~/.claude/CLAUDE.md` | How the user works *everywhere*: style, test philosophy, git, tooling gotchas |
| Project rules | `<repo>/CLAUDE.md` | Project ABSOLUTE rules + architecture orientation. **In a repo listed in `~/.claude/local/no-project-settings`, route nothing here:** its `CLAUDE.md`/`AGENTS.md`/`.claude/{rules,skills}` belong to other devs and are not loaded (see "Where things live" in global CLAUDE.md); personal rules go to the repo rules doc. In any other repo a tracked file is a TEAM change: surface it as "needs a PR / your call", never auto-apply. |
| Repo rules | `~/.claude/local/<repo>-rules.md`, `<repo>-invariants.md` | One repo's tooling, CI, schema, PR labels, ROLLOUT; invariants mirror the repo's own code rules. Read on demand. |
| Test execution | `~/.claude/local/<repo>-testing.md` | How to run and write tests on this machine. Read on demand. |
| Domain knowledge | `<repo>/CONTEXT-MAP.md` → `context/*.md`, `docs/adr/` | Durable domain facts, flows, decisions (git-tracked — write via the docs-rollup worktree, see below) |
| Serena memories | `<repo>/.serena/memories/` | ONLY serena orientation: `serena_usage_notes`, `project_overview`, `tech_stack`. **Never conventions/commands/checklists** — those live in CLAUDE.md. |
| Skills | `~/.claude/skills/<skill>/` | Workflow mechanics the skill owns (e.g. the emulator drive loop → `qa-e2e`) |
| Agents, commands | `~/.claude/agents/*.md`, `~/.claude/commands/*.md` | One subagent's own instructions; one slash command's flow |
| Hooks | `~/.claude/hooks/bash-guards` | Anything with a fixed corrective command or a forbidden verb — fires at the violation, reaches subagents, costs no standing context |
| ~~Memory~~ | `~/.claude/projects/<enc>/memory/` | **Retired 2026-08-25** (`autoMemoryEnabled: false`). Not loaded, never written. |

**Routing rule:** cross-project preference a *subagent* must obey → global CLAUDE.md (budgeted, see below). Repo-specific rule → `~/.claude/local/<repo>-rules.md`. How-to-run-or-write-tests → `~/.claude/local/<repo>-testing.md`. Prod-data / analysis trap → the `debug` skill. Business semantics or a decision → the repo's domain docs / an ADR. Workflow mechanics → the owning skill. Serena orientation → serena memories. Enforceable mechanically → a hook.

**There is no "everything else" bucket.** If a candidate fits none of the rows above, it is in-flight state or a war story: put it in the PR, the Jira ticket, or the session handoff, and drop it from the change-set. Say so explicitly rather than inventing a home.

## Category 4 — additions (dedup check is mandatory)

- Only propose rules that generalise beyond this session; near-misses count.
- **Before ANY addition, run the dedup check and print its evidence.** Grep 2–3 distinctive phrases of the candidate across every layer. Duplicates hide in local docs, skills, agents and hook messages as often as in CLAUDE.md:

  ```
  grep -rniF --exclude-dir=synced --exclude-dir=__pycache__ -e '<phrase 1>' -e '<phrase 2>' ~/.claude/CLAUDE.md ~/.claude/local ~/.claude/skills/*/ ~/.claude/agents ~/.claude/commands ~/.claude/hooks <repo>/CLAUDE.md <repo>/.serena/memories
  ```

  Keep `skills/*/`: most skills are symlinks into `~/.agents/skills`, which `grep -r ~/.claude/skills` silently skips. `~/.claude/hooks` covers the deny/advice strings and the `session-context` injection (its `echo` lines are the injected text). Hits under `local/archive/` and `local/session-digests/` are history, not a live layer. For a domain fact, also grep the docs-rollup worktree. Show it inline, e.g. `dedup "retry budget": 0 hits → new` or `dedup "worktree per ticket": hit in hooks/bash-guards deny message → edit in place, not a new rule`.
- An add without printed dedup evidence is invalid output. If the rule already exists anywhere, edit in place or do nothing; never add a duplicate.
- **Structural-enforcement check, before any rule is accepted as prose.** Ask: would a hook, a settings entry, a script, or a formatter enforce this more reliably than a sentence? If yes, it is not a `CLAUDE.md` bullet — propose the mechanism instead. Prose reaches the model only if it is read and remembered; a `PreToolUse` gate fires exactly at the violation, reaches subagents, and costs no standing context. Good candidates: anything with a fixed corrective command, a required flag, or a forbidden verb. Bad candidates: anything needing semantic judgement (is this token a symbol? is this test mock-based by design?) — those stay prose. `~/.claude/hooks/bash-guards` is the extension point; add a check there rather than a new hook file.
- **A rule that a subagent must obey without reading anything else belongs in global `CLAUDE.md` or a hook message — never only in `~/.claude/local/*`.** Subagents inherit the global file but never receive SessionStart output. Before routing, ask who has to obey it.

## Category 5 — consolidate / delete / archive

Actively hunt and act on:
- **Duplicates:** a second copy of a rule that already has a canonical layer → delete the copy.
- **Misfiled:** a global rule sitting in one repo's local doc (promote, then delete); a repo rule sitting in global; conventions dumped into serena memories.
- **Stale:** shipped-ticket notes, resolved in-flight state, handoffs and parking-lot files under `~/.claude/local/` → delete.

**Archive procedure:** (1) distill at most one durable line into its canonical layer if any survives; (2) a skill moves to `~/.claude/skills-archive/`; history that a script or doc still points at moves to `~/.claude/local/archive/` (never loaded, never instructs); any other file is deleted (`~/.claude` is a git repo, so history is the archive); (3) remove every reference to it (the audit's dead-reference grep finds them).

## Apply mode (default) vs `--advisory`

**Default = apply-with-confirmation.** Assemble the WHOLE change-set as a compact table (layer · add/edit/delete/archive · one-line summary) plus unified diffs for edits/adds. Ask for ONE confirmation. On yes, apply everything directly (edits, deletes, archives — deletes/archives too, not just adds), then end with a git-style summary: `N added, M edited, K deleted, J archived`. This is what fixes the old additive-only drift.

Run `session-review --advisory` to only emit the diffs/proposals and change nothing.

Team-shared files (a team repo's tracked `CLAUDE.md`, `docs/`, `.serena` aside) are NOT auto-applied even in apply mode — surface those as a separate "needs a PR / your call" list. **Exception: domain-doc learnings** — those ARE applied, but only as commits to the standing docs-rollup worktree (next section), never to the user's checkout and never auto-PR'd.

## Domain-doc learnings → docs-rollup worktree (not per-session branches)

New domain knowledge routed to git-tracked domain docs (`context/*.md`, `CONTEXT-MAP.md`, lib `docs/adr/`) accumulates in ONE standing local worktree per repo, merged as a whole after some days — do NOT create a fresh branch per session.

- **Location:** `<repo>/.claude/worktrees/domain-docs`, branch `domain-docs-rollup`, based off the current release branch. First use: `git worktree add .claude/worktrees/domain-docs -b domain-docs-rollup origin/<REL>`. If the worktree exists, reuse it — pull nothing, just commit on top.
- **One commit per session**, normal commit rules (author, `Co-authored-by: Claude`, `Claude-Session:` trailers), subject `docs(<lib>): <what>` or `TE-<N>: …` when the learning came from a ticket. Never touch the main checkout; never rewrite commits already on the rollup.
- **Report rollup state every session-review** that touches it: commit count + age of oldest commit. When the oldest commit is **>~5 days** old, several sessions have accumulated, or the release train rolled past the base — prompt the user to push + PR the rollup (their call; never auto-PR). After it merges, remove the worktree/branch so the next learning re-creates it fresh off the new REL.
- If a doc the session wants to edit changed on the release branch since the rollup's base, rebase the rollup onto current `origin/<REL>` first if clean; if conflicted, flag it in the change-set instead of forcing.
- **READ domain docs from the rollup worktree, not the main checkout.** Unmerged rollup commits mean the main checkout is a stale view: `ls`/`grep` under `<repo>/src/lib*/context/` will report a subject missing that has existed on the rollup for weeks, and you will propose a duplicate. Always `ls`/`grep`/`cat` under `<repo>/.claude/worktrees/domain-docs/src/...` with **absolute paths** before concluding a subject is absent — a stray `cd` persists across Bash calls, so a bare relative path silently reads whichever tree you last landed in.
- **A worktree-isolated session cannot touch the rollup at all — reads are fine, writes and git are
  refused.** `git -C <rollup>` and `Edit`/`Write` against `<repo>/.claude/worktrees/domain-docs/...` are
  both blocked by the isolation guard, so a session-review run from inside a ticket worktree can diff the
  docs but cannot apply or commit them. Get rollup state by reading
  `<repo>/.git/worktrees/<name>/logs/HEAD` (plain file, epoch-stamped) + `date -r <epoch>`. Plan for this
  **before** promising the change-set: write the intended doc content to the scratchpad, hand over a
  one-line apply command, and report the domain-doc rows as *staged, not applied*.

## Standing-context budget (enforced every run — run this FIRST)

There is no `memory/` layer to route to. Durable rule → a CLAUDE.md or a local doc; durable domain fact → a domain doc; in-flight ticket state → the PR or the Jira ticket, never a config file.

**`~/.claude/CLAUDE.md` is capped at the word budget its first lines state.** It is the only file injected into every session *and* every subagent, so a line there is paid for on every turn forever, and additive-only drift moves to whatever you do not measure.

**Before proposing any add, print the current count** (`wc -w ~/.claude/CLAUDE.md`) and state the post-change count. Over budget → the change-set MUST contain a matching relocation or deletion. Relocation is almost always the answer:

| The rule is about… | It belongs in | Loaded |
| --- | --- | --- |
| A specific repo's tooling, CI, schema, logging | `~/.claude/local/<repo>-rules.md` | read on demand |
| Running or writing tests in a repo | `~/.claude/local/<repo>-testing.md` | read on demand |
| Querying prod / analysing data | `~/.claude/skills/debug/SKILL.md` | on invoke |
| Business semantics, vocabulary, a decision | the repo's `context/<subject>.md` or an ADR | on demand |
| A fixed corrective command or forbidden verb | `~/.claude/hooks/bash-guards` | fires at the violation |
| How the user works *everywhere* | `~/.claude/CLAUDE.md` | **every turn — budgeted** |

A rule earns a global slot only if a **subagent** must obey it without reading anything else. That is the test — apply it before anything else.

**Structural-enforcement check stays mandatory.** Would a hook, settings entry, or formatter enforce this more reliably than a sentence? Then it is not a bullet. Anything needing semantic judgement stays prose.

**No war stories in the rule.** One imperative sentence, then at most one clause of evidence. The incident belongs in the commit message or the handoff, not in standing context.

## Audit mode (`session-review audit`)

A periodic whole-setup sweep, every two to four weeks. Ignore the live conversation; the subject is the
config itself and what the transcripts say it did. Scratch output goes to the scratchpad.

**1. Measure.** Window = since the last audit (the last review commit in `~/.claude`'s git log);
the scripts default to the last 14 days.

```
python3 ${CLAUDE_SKILL_DIR}/scripts/transcript_quant.py --since <date> --json <scratchpad>/quant.json
python3 ${CLAUDE_SKILL_DIR}/scripts/review_precision.py --since <date> --json <scratchpad>/precision.json
```

`transcript_quant` prints spend by model and by main/subagent/workflow agent, context percentiles
and spend by context band, spend by what woke the model, idle cache rewrites, denials by first line,
hook errors by hook, and subagent spend by agent type; the JSON adds steering prompts, top sessions,
oversized tool results, Bash hygiene and spend by attributed skill. `review_precision` prints per
review agent spawns, findings and cost, and critic verdicts by the agent that raised each finding.
Every figure in the report comes from these outputs, never transcribed. Read the JSON with bounded
reads. A denial repeated across sessions is a rule the model lacks or a hook misfiring; a hook-error
row is a hook exiting non-zero on real payloads.

**2. Sweep** layers against each other: `~/.claude/CLAUDE.md`, the `session-context` output, every
project `CLAUDE.md`/`AGENTS.md`, `~/.claude/local/*`, `~/.claude/{agents,commands}/*.md`,
`~/.claude/hooks/*` and their wiring in `settings.json`, `~/.claude/skills/*/SKILL.md`, the repo's own
`.claude/skills/`, enabled plugins, and the domain-doc tree. Report with a file:line per finding:

1. **Contradictions:** two layers that cannot both be followed, a file contradicting itself, prose
   contradicting what a hook enforces. Rank these first: they make correct behaviour impossible.
2. **Dead references:** grep the dedup path list for each name in `ls ~/.claude/skills-archive`,
   for paths and tools that no longer exist, and for a skill whose `name` doesn't match its directory.
3. **Broken hooks:** run the hook suites (`bash tests/run-tests.sh` in a claude-kit checkout, or
   `~/.claude/hooks/tests/` for hand-installed hooks); every hook in the hook-error table must be
   reproduced by piping it the payload shape it failed on. When a fix revives a silent hook, check
   what its output now unleashes before calling it fixed.
4. **Stale content:** dates, PR numbers and ticket keys in standing files:
   `grep -rnE '20[0-9]{2}-[0-9]{2}-[0-9]{2}|#[0-9]{4,}|PR [0-9]{3,}|[A-Z]{2,5}-[0-9]{2,5}' ~/.claude/CLAUDE.md ~/.claude/local/*.md ~/.claude/skills/*/SKILL.md ~/.claude/agents ~/.claude/commands`.
   A date on a live decision stays; one in a war story goes with the story.
5. **Parking-lot files:** anything in `~/.claude/local/` or a skill dir that is not a rules,
   testing, invariants or CI doc, a script something runs, or referenced from a layer: handoffs,
   digests, pending patches, one-off scripts. Move the content to its PR, ticket or handoff, then delete.
6. **Budgets:** `wc -w ~/.claude/CLAUDE.md` against its stated budget; for each repo with a local
   doc, `CLAUDE_PROJECT_DIR=<repo> ~/.claude/hooks/session-context </dev/null | wc -c` against the
   hook's `BUDGET=` line. Over budget → the change-set must cut.
7. **Wrong facts:** reference docs and skill instructions asserting things untrue now. Verify the
   load-bearing ones against the live system: a skill that is confidently wrong is worse than none.
8. **Redundancy and misplacement:** run the dedup grep on each CLAUDE.md rule and each hook
   message; flag conditional tool mechanics in always-loaded context and a rule in a layer its
   audience never sees.
9. **Rules that should be structure:** apply the structural-enforcement check to existing prose.
10. **Trigger hygiene:** skills that overlap in what they claim, descriptions with no trigger phrases.

Same apply-with-confirmation model. **Verify before you delete:** zero recent invocations is not
evidence a skill is dead; a newly authored one looks identical.

If a category is empty, say so in one line; don't pad.
