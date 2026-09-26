---
description: Ticket-to-merge-ready flow. Default mode builds new work (Jira ticket, worktree, one commit, PR, CI and review comments to green). `takeover <PR|handoff>` adopts started work; `sync <branch> [targets]` merges a branch downstream. Composes /jira, the Stop-hook review, ci-watch and /address-review.
argument-hint: "<work description> | takeover <PR|handoff-path> | sync <branch> [targets]"
---

Pick the mode from the first word of the arguments: `takeover`, `sync`, or anything else (build).
Definition of DONE for build and takeover is phase 5: CI green AND review comments addressed.

Standing rules for every mode (on top of CLAUDE.md's one-PR-per-ticket, handoff and `worker` rules):
- Never split a ticket into PRs per lib or phase; if it is too big for one, say so in the report.
- Read handoffs only from `~/.claude/handoffs/`, never /tmp or an untracked `docs/` file.
  Stopping with work open: rewrite the handoff (state, decisions, next steps, open questions);
  never append. Merge-ready: replace its body with `Done: <PR URL> merge-ready at <sha>`.
- Delegate a whole phase, never a fragment.

## Build mode

### 1. Ticket (module: `${CLAUDE_PLUGIN_ROOT}/commands/jira.md`)

Read that file and run its "Preferences" and "Find or create" sections. Come back with the
ticket key `<KEY>-N` and its title. Transition it to In Progress and assign it to yourself.

### 2. Workspace

- `EnterWorktree` named `<KEY>-N-<short-title>` (kebab). Base = the prefs `base_branch_rule`
  unless the user named one; verify against `gh pr list --state merged --json baseRefName`.
- The worktree is isolated: one plain command per Bash call, scripts via the Write tool.
- Read the repo's `CONTEXT-MAP.md` and `~/.claude/local/<repo>-{invariants,testing}.md`
  before touching code.

### 3. Build

- Implement. Tests per the SessionStart TESTS line (one test-runner invocation per turn,
  redirected to the scratchpad). Add or extend a test for every behaviour change; prove a
  regression test fails on the pre-fix code.
- ONE commit, subject `<KEY>-N: <imperative>`, required trailers. Do not push yet.
- Stop once. The Stop-hook review runs (light or full by size); fold every verified fix into
  the same commit with `--amend`.

### 4. Ship

- `git push -u origin <branch>` from the worktree cwd (plain, no `-C`, no chain).
- `gh pr create`: title `<KEY>-N: <ticket title>`, base = the branch you branched from, body
  links the ticket, states what changed, how to verify, and a ROLLOUT section (DDL/DML before
  deploy, deploy targets mapped from changed paths). Apply every label in `pr_labels`.
- On the ticket: link the PR in a one-line comment.

### 5. CI and review to green (the finish line)

- Pushing started `ci-watch`; its state is `~/.claude/tmp/ci-watch-<pr>.status`.
- Run ONE background wait and stop: `until grep -qE '^(green|RED|idle|no )|gave up'
  ~/.claude/tmp/ci-watch-<pr>.status; do sleep 30; done; head -1 ~/.claude/tmp/ci-watch-<pr>.status`
  with `run_in_background: true` (the same waiter the unfinished-work Stop hook hands out). The
  completion notification wakes you; do not poll.
- RED: read `tail -80 $TMPDIR/ci-watch-<pr>.agent.log` for what the repair agent tried;
  separate your regression from a pre-existing flake (`~/.claude/local/ci-rules.md`); fix;
  batch into ONE new commit; push; wait again.
- GREEN: if `gh pr checks <pr> --json name,bucket` shows `Cursor Bugbot` as `pending`, run one
  background wait until it is not. Then run `${CLAUDE_PLUGIN_ROOT}/commands/address-review.md` with
  `pr:<pr>` yourself: fix every unresolved thread you agree with, resolve the `cursor[bot]`
  threads you fixed, and keep human threads and rejected findings (with the evidence) for the
  report. Any fix is ONE new commit, push, back to the top of this phase.
- Green and every bot thread handled: transition the ticket to review, move it into the active
  sprint, post the closing note (PR URL, one line on the change, how it was verified).

### 6. Report

First line: `You need to:` then the manual steps in the order to do them: DDL/DML (staging, then
prod), deploy targets in deploy order (say which must go out together), config or data fixes.
`You need to: nothing` when there are none. Never bury a deploy-order constraint lower down.
Then: PR URL · CI result · what changed (3 lines max) · how to verify · anything left open.
Review findings you rejected go under "not done, and why".

## Takeover mode: `takeover <PR | handoff-path>`

Adopt started work and carry it to merge-ready. Replaces phases 1-3:

1. Load state. PR: `gh pr view <n> --json number,headRefName,baseRefName,body,labels,commits`,
   its ticket, and `~/.claude/handoffs/<TICKET>.md`. Handoff path: read it, then its PR. Trust
   `git diff origin/<base>...origin/<head>` over a handoff older than the last push.
2. Workspace: `git worktree list` first and reuse a worktree that holds the branch; otherwise
   `EnterWorktree` on it. Confirm `HEAD` equals `origin/<head>` before reading code.
3. Deep review of the whole PR diff; fix correctness and quality findings in scope and finish
   the open items the handoff lists. The PR is pushed: new work is ONE new commit on top, never
   a history rewrite.

Then phase 4 (update the PR body and labels; do not open a new PR), phase 5 and phase 6.

## Sync mode: `sync <branch> [targets]`

Merge `<branch>` into each target. With no targets, use the downstream branches that the repo's
`~/.claude/local/<repo>-rules.md` "Branches and deploys" section declares for `<branch>`. If the
repo has no such section, derive it from `git ls-remote --heads origin` and recent merged PR
bases, state the targets you picked, and offer to record them there.

Per target, in order:
1. `git fetch origin <branch> <target>`, then `git worktree add --detach
   <scratchpad>/sync-<target> origin/<target>`. Never merge in a long-lived shared worktree.
2. `git -C <that worktree> merge --no-ff origin/<branch>` with the required trailers. Resolve
   mechanical conflicts; stop and report a semantic one with the conflicted paths.
3. Run the tests covering every conflict-resolved file. Record `git rev-parse HEAD^{tree}`.
4. A target the rules mark PR-only: push `<branch>-to-<target>` and open a PR into it. Any other
   target: confirm `HEAD^{tree}` is unchanged, then `git push origin HEAD:<target>`. Rejected as
   non-fast-forward: fetch and merge again. Never force.
5. Leave the worktree for a later prune.

Report with the `You need to:` line first: which branch to deploy, on which environment, and
the deploy targets the merged diff touches.

Work: $ARGUMENTS
