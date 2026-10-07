---
description: Find review notes — in the working tree, in a pr-study export, or unresolved on the PR — and address each at its line, then strip the note
argument-hint: "[path scope · pr:<n> · notes:<file> · tag:FIXME]"
allowed-tools: Bash, Read, Edit, Grep, Glob, LSP, mcp__ide__getDiagnostics
---

You left review notes while reading a diff. Find them, fix each at its location, remove the note. Notes may be **tagged** (`CR:`, `@review`, `NIT:`, `FIXME(review)`), **freeform** — a sentence typed into a comment that reads as an instruction to the author rather than real documentation — or **out of band**, exported from a pr-study artifact or left on the PR itself. Watch the diff, don't rely on a tag.

## 0. Confirm you are in the tree you reviewed

The expensive failure in this command is editing the wrong checkout. Agents here work in `.claude/worktrees/<TICKET>`; the main root sits on a different branch and returns pre-change code **with no error**, so every edit lands somewhere harmless and every report reads as success.

- `git rev-parse --show-toplevel` and `git rev-parse --abbrev-ref HEAD`.
- `git worktree list` when the branch isn't the one you expect. A `+` prefix in `git branch -a` means that branch is checked out in a linked worktree, not here.
- If a note names a symbol, file, or line that does not exist at HEAD, **stop and name the worktree that holds the branch.** Do not fuzzy-match onto the nearest thing in the current tree.

State the resolved root and branch in one line before doing anything else.

## 1. Gather notes

Take the **union** of whatever these turn up. Absent sources are silently skipped; if all three are empty, say so and stop.

**A · Working tree** — the primary source.
- `git diff --no-color` (tracked, unstaged), `git diff --no-color --cached` (staged)
- `git status --porcelain`, then read untracked files for notes
- Clean tree → fall back to the branch diff: `git merge-base HEAD <base>` (the release base if branched from one, per `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/local/<repo>-rules.md` "Branches and deploys", else the default branch) → `git diff --no-color <base>...HEAD`

**B · A pr-study export** — when `$ARGUMENTS` carries `notes:<file>`, or `.claude/pr-study/*/notes.md` exists, or the user pasted the export into the conversation. One note per line, `#` and blank lines ignored:

```
src/services/handover.py:118 — u3 — bulk-fetch here, this is N+1
u3 — why not reuse the existing guard instead of a second one?
```

A line with a `path:line` prefix is anchored — address it there. A bare `<unit> — <text>` line is unit-level: resolve the location yourself from the unit's files, and say which line you picked.

**C · Unresolved PR review comments** — when `$ARGUMENTS` carries `pr:<n>` (as `/kickoff` phase 5 passes it), or the current branch has an open PR and the user asked for `--from-pr`. Resolution state is GraphQL-only; the REST comments endpoint cannot tell you what is already handled. Bugbot posts as `cursor[bot]`; add `id` to the thread fields when you will resolve threads:

```bash
gh api graphql -F owner=<owner> -F repo=<repo> -F pr=<n> -f query='
query($owner:String!,$repo:String!,$pr:Int!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$pr){
      reviewThreads(first:100){
        nodes{
          isResolved isOutdated
          comments(first:20){ nodes{ path line originalLine body author{login} } }
        }
      }
    }
  }
}' --jq '.data.repository.pullRequest.reviewThreads.nodes[]
         | select(.isResolved | not)
         | .comments.nodes[0]
         | "\(.path):\(.line // .originalLine) — \(.body)"'
```

Those line numbers are against the PR head. If the local branch has moved since, **re-anchor by content, not by number** — read the file and find the code the comment is about.

### Writing draft comments back (when asked)

GraphQL only, and there is **ONE pending review per user** — a second `addPullRequestReview` on the same PR fails, so add threads to the existing pending review instead of opening another.

- Read what's already pending: `pullRequest.reviews(states:PENDING)`
- Add a thread: `addPullRequestReviewThread`
- Edit one you already wrote: `updatePullRequestReviewComment`

A comment anchors only to a line **inside the diff**. For a finding outside it, anchor to the nearest in-diff line and cite the real `file:line` in the body.

## 2. Identify what counts as a note

For source A, a line is a review note if it is a **comment** (any language's syntax) AND either:
- carries a known marker: `CR:`, `CR!`, `@review`, `REVIEW:`, `NIT:`, `FIXME(review)`, `TODO(review)`, plus anything from `$ARGUMENTS`; **or**
- is freeform but reads as a directive/critique aimed at the author — imperative ("bulk-fetch here", "rename this", "this is N+1", "why not reuse X?", "extract", "wrong layer"). Recently-added comments (present as `+` lines in the diff) are the strong signal.

Do **not** treat genuine code documentation, existing comments unchanged by this diff, or commented-out code as notes. When a line is ambiguous, list it under "Skipped — ambiguous" rather than acting.

Sources B and C are notes by construction — no classification needed.

## 3. Confirm, then act

- Print what you found as a compact list, tagged by origin: `[tree|study|pr] path:line — <note> → <what you'll do>`.
- List anything ambiguous you're skipping.
- If any note is unclear, or needs a judgment call the note doesn't settle, **ask** — don't guess at directional intent. Run from `/kickoff`, don't ask: make the call and name it in the report.
- Otherwise proceed: make each change **at that location**, then delete the note comment (and its line, if the line existed only for the note). Notes from sources B and C have nothing to strip. Keep each change minimal and local — this is cleanup of your own tweaks, not a refactor pass. Honor all repo CLAUDE.md rules (match existing style, jsql, etc.).
- A note that is a review finding (a bot or reviewer comment on the PR, a review agent's report) follows `"${AGENT_KIT_DIR}/skills/review-rubric/references/fix-policy.md"`: fix it when it is confirmed or small, make a larger one a ranked option, and give every one you dismiss its reason class.

## 4. Report

- One line per note: `✓ [origin] path:line — <what changed>`.
- Note anything skipped or asked about.
- Do **not** commit, stage, or push unless asked. Leave the result in the working tree.
- Do **not** post replies or resolve threads on GitHub unless asked — that is outward-facing and the author may be someone else. Offer it; don't do it.
- Run from `/kickoff`, the caller owns the rest: it commits, pushes, and resolves (`resolveReviewThread`) only the `cursor[bot]` threads it fixed.
