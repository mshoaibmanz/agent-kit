---
description: Ticket-to-merge-ready flow. Default mode builds new work (Jira ticket, worktree, one commit, PR, CI and review comments to green). `takeover <PR|handoff>` adopts started work; `sync <branch> [targets]` merges a branch downstream. Composes /jira, the push review, ci-watch and /address-review.
argument-hint: "<work description> | takeover <PR|handoff-path> | sync <branch> [targets]"
---

Pick the mode from the first word of the arguments: `takeover`, `sync`, or anything else (build).
Definition of DONE for build and takeover is phase 5: CI green AND review comments addressed.

Standing rules for every mode (on top of CLAUDE.md's one-PR-per-ticket, handoff and `engineer` rules):
- Never split a ticket into PRs per lib or phase; if it is too big for one, say so in the report.
- The ticket is an item of a project under the work root (`~/agent-work/projects/<p>/items/<TICKET>/`,
  or a legacy `tasks/<TICKET>-*/`); the injected PROJECT or TASK DIR line names it, and
  `"${CLAUDE_PLUGIN_ROOT}/kit/bin/agent-task" where --path` prints it. Its handoff is `HANDOFF.md` there; never read
  one from /tmp or an untracked `docs/` file. Stopping with work open: rewrite the handoff (state,
  decisions, next steps, open questions). Merge-ready: harvest (phase 5), then APPEND the summary;
  never overwrite the handoff with a one-liner. INDEX.md regenerates itself; add `use`/`proved` lines.
- Worktrees live in `<repo>/.claude/worktrees/`, never under the work root.
- Delegate a whole phase, never a fragment.

## Build mode

### 1. Ticket (module: `"${CLAUDE_PLUGIN_ROOT}/kit/commands/jira.md"`)

Read that file and run its "Preferences" and "Find or create" sections. Come back with the
ticket key `<KEY>-N` and its title. Transition it to In Progress and assign it to yourself.

### 2. Workspace

- `EnterWorktree` named `<KEY>-N-<short-title>` (kebab). Base = the prefs `base_branch_rule`
  unless the user named one; verify against `gh pr list --state merged --json baseRefName`.
- The worktree is isolated: one plain command per Bash call, scripts via the Write tool.
- Read the repo's `CONTEXT-MAP.md` and `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/local/<repo>-{invariants,testing}.md`
  before touching code.

### 3. Build

- Implement. Tests per the SessionStart TESTS line (one test-runner invocation per turn,
  redirected to the scratchpad). Add or extend a test for every behaviour change; prove a
  regression test fails on the pre-fix code.
- ONE commit, subject `<KEY>-N: <imperative>`, required trailers. Do not push yet.
- The review happens at push: the first `git push` refuses with the review instruction (round 1:
  bug-reviewer plus Codex, and quality-reviewer once, in one message). Reproduce each finding before
  fixing it, end with the `TALLY` line, fold every fix into the same commit with `--amend`, then
  push again. A Stop only self-checks small deltas.

### 4. Ship

- `git push -u origin <branch>` from the worktree (`git -C <worktree>` is fine; no chain). Under ~40
  lines since a completed round it passes with a self-check line; past the branch's 3 rounds,
  with a warning. A deliberate push past the gate (red CI fix, WIP share):
  `AGENT_PUSH_NOW="<reason>" git push ...`. Never `--no-verify`.
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
  separate your regression from a pre-existing flake (`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/local/ci-rules.md`); fix;
  batch into ONE new commit; push; wait again.
- GREEN: if `gh pr checks <pr> --json name,bucket` shows `Cursor Bugbot` as `pending`, run one
  background wait until it is not. Then run `"${CLAUDE_PLUGIN_ROOT}/kit/commands/address-review.md"` with
  `pr:<pr>` yourself: fix every unresolved thread you agree with, resolve the `cursor[bot]`
  threads you fixed, and keep human threads and rejected findings (with the evidence) for the
  report. Any fix is ONE new commit, push, back to the top of this phase.
- Green and every bot thread handled: transition the ticket to review, move it into the active
  sprint, post the closing note (PR URL, one line on the change, how it was verified).
- Harvest (Done): run `"${CLAUDE_PLUGIN_ROOT}/kit/bin/agent-task" harvest <project>/<TICKET>` and answer its three
  questions: facts for the domain docs (true beyond this ticket, business meaning, flows or
  decisions, verified in code; through the docs-rollup worktree, then a link in `knowledge/`),
  facts for the project's `knowledge/`, and scripts to move to its `scripts/` with a docstring.
  Then append `## Summary (harvested <date>)` to the HANDOFF with `Done: <PR URL> merge-ready at
  <sha>` and where each fact and script went, and record at most 3 learnings with
  `agent-task retro "<mistake|fact|doc|tooling>: <text>"`.

### 6. Report

First line: `You need to:` then the manual steps in the order to do them: DDL/DML (staging, then
prod), deploy targets in deploy order (say which must go out together), config or data fixes.
`You need to: nothing` when there are none. Never bury a deploy-order constraint lower down.
Then: PR URL · CI result · what changed (3 lines max) · how to verify · anything left open.
Review findings you rejected go under "not done, and why".

## Takeover mode: `takeover <PR | handoff-path>`

Adopt started work and carry it to merge-ready. Replaces phases 1-3:

1. Load state. PR: `gh pr view <n> --json number,headRefName,baseRefName,body,labels,commits`,
   its ticket, and the ticket's item (`/bind <TICKET>`, then its `HANDOFF.md` and the project's
   `INDEX.md` and `knowledge/` for reusable scripts, data and facts). Handoff path: read it, then its PR. Trust
   `git diff origin/<base>...origin/<head>` over a handoff older than the last push.
2. Workspace: `git worktree list` first and reuse a worktree that holds the branch; otherwise
   `EnterWorktree` on it. Confirm `HEAD` equals `origin/<head>` before reading code.
3. Deep review of the whole PR diff; fix correctness and quality findings in scope and finish
   the open items the handoff lists. The PR is pushed: new work is ONE new commit on top, never
   a history rewrite.

Then phase 4 (update the PR body and labels; do not open a new PR), phase 5 and phase 6.

## Sync mode: `sync <branch> [targets]`

Merge `<branch>` into each target. With no targets, use the downstream branches that the repo's
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/local/<repo>-rules.md` "Branches and deploys" section declares for `<branch>`. If the
repo has no such section, derive it from `git ls-remote --heads origin` and recent merged PR
bases, state the targets you picked, and offer to record them there.

Per target, in order:
1. `git fetch origin <branch> <target>`, then, with `<s>` = `git rev-parse --short origin/<branch>`,
   `git worktree add -b tmp-sync-<branch>-to-<target>-<s> <repo>/.claude/worktrees/sync-<branch>-to-<target>-<s>
   origin/<target>`. The name is unique per sync: an earlier one's worktree and branch stay until
   `claude-gc` (which never deletes branches). Never merge in a long-lived shared worktree.
2. `git -C <that worktree> merge --no-ff origin/<branch>` with the required trailers. Resolve
   mechanical conflicts; stop and report a semantic one with the conflicted paths.
3. Run the tests covering every conflict-resolved file. Record `git rev-parse HEAD^{tree}`.
4. A target the rules mark PR-only: push `<branch>-to-<target>` and open a PR into it. Any other
   target: confirm `HEAD^{tree}` is unchanged, then `git push -u origin HEAD:<target>`. Rejected as
   non-fast-forward: fetch and merge again. Never force.
5. Leave the worktree and its `tmp-` branch for `claude-gc`.
6. If the user asks to deploy, use the project's documented deployment procedure and the PR's
   ROLLOUT. Confirm the target environment and authorization before changing it.
   a. Confirm each running revision is contained in the proposed revision with
      `git merge-base --is-ancestor <running> <new>`. If it is not, identify missing unique patches
      and stop until their owner confirms the replacement.
   b. Run the project's schema compatibility checks. Report required migrations and unchecked
      areas; deployment waits for migrations this change requires.
   c. Preview the deployment with the configured project tooling. Complete any interactive
      authentication in the user's trusted session before applying the approved deployment.
   d. Observe the project's soak period and configured health/error dashboards. Report the
      environment, revision, checks and any targets requiring manual action.

Report with the `You need to:` line first: which branch to deploy, on which environment, and
the deploy targets the merged diff touches (or, after step 6, what deployed and what is left).

Work: $ARGUMENTS
