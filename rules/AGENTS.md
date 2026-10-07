# Shared working rules

Budget: **≤1200 words**, the host notes included. Loaded into every session and subagent. A rule
belongs here only if no overlay file, skill, domain doc or hook can hold it. Adding means deleting.

## Absolute

- **Never write to production from an agent session.** Read production data only through the
  kit's read-only wrappers (`ro-mysql`, `bqro`) or the read-only path your overlay names; raw
  clients and write paths are out. No INSERT/UPDATE/DELETE/DDL, no "just fix the row": read,
  report, let the user run the fix.
- **Staging is any tunnel, login path or environment named `*staging*`/`*stg*`**; every other one
  is production. Staging ids are fabricated: never carry one to production or answer a production
  question from it, and say "staging" when you use it.
- **No mocks, patches or stubs in backend or integration tests.** Use real fixtures, a real
  database and existing factories; no inline seed data. A suite that is mock-based by design (a
  frontend unit suite the repo's overlay names) stays so.
- **Ask before destructive operations** (force-push, `reset --hard`, dropping data, deleting
  branches or worktrees) and public publication, even in an auto or unattended mode.
- **Run a representative local test subset before pushing**, chosen by who consumes the outcome:
  when a failing path starts succeeding, grep what depended on the old result.
- **Never read credential stores** (OS keychains, database login files, browser cookie stores,
  `.env` secrets). Keep credentials in the provider's native store or a system secret store; never
  print or commit them.

## Where things live

- Inspect repository instructions before editing its code. `CONTEXT-MAP.md`, `context/*.md` and
  `docs/adr/` are domain docs, not instructions.
- Keep repo-specific commands, ticket prefixes and domain facts in the per-user overlay:
  `<repo>-invariants.md` before writing code, `<repo>-testing.md` before running tests,
  `<repo>-rules.md` for conventions and PR labels.
- **What outlives a session goes to its bound task or project folder**: handoffs, reusable
  knowledge, decisions. Reuse its knowledge and scripts before writing new ones; never a repo's
  `docs/` unless asked. Disposable test and build directories go there too; set `TMPDIR` inside
  it. System temporary directories are never evidence. Keep source worktrees with their repository.
- Production questions go through the `debug` skill when installed: its reading traps and answer
  format apply before you present any figure.

## Subagents

- **Delegating? Restate the rules the task depends on** (test command, docs, task folder).
  Implementation goes to one `engineer` per PR or phase, from a brief file, never a fragment;
  read-only discovery goes to a `researcher`.
- A subagent cannot spawn agents; it says so when a review needs them.
- Make an access loosening the user approved yourself; never delegate it.

## Git and PRs

- Work on a branch or isolated worktree. **One commit per push.** Before a PR exists, amend the
  unpushed commit. Once a PR is open, never amend or rebase: add one commit right before the push.
  Never force-push.
- **Diff every path before staging**, never `git add -A`: a path may hold someone else's edits.
  Preserve unrelated user edits.
- **Diff three-dot against `origin/<base>...HEAD`.** A stale local branch of the same name silently
  wins. Re-derive ahead/behind counts yourself.
- **Verify `HEAD` before reading a branch you didn't check out** (`git worktree list` when it
  disagrees). A wrong checkout returns old code with no error.
- **Never merge in a shared worktree.** Create an isolated worktree from `origin/<branch>`, merge,
  verify, push. A non-fast-forward push: fetch and merge, never force.
- Match the repo's PR template.
- **The final message leads with "You need to:"** when there are deploy-order constraints or
  manual steps. Never bury them in a list.

## Planning, docs and refactors

- **Plans are a PRD with sequential numbered steps** (Context, Goals, Non-goals, Steps,
  Verification, Open questions). No phases or time estimates.
- **One entry point with declarative config beats N specific ones.** Add a mode to an existing
  skill; extend a module before adding a package.
- **Hook the state writer, not one caller.** Grep the status value and enumerate every writer.
- **Refactor: grep the tests for every assert, error string and method you move or delete.** A
  dropped assert is a contract change.
- **Change one variable per experiment.**
- **When an early step produces findings, re-read the remaining steps.**
- **Figures in a deliverable come from a script, not transcription.** Durable docs describe shape,
  not counts.
- **Before claiming "no such mechanism", grep the domain noun** and `docs/`.
- **Verify semantics at the source.** A field means what its owner does with it; a gate mirrors
  what the consumer enforces.
- **Writing a test:** scope queries to its own entity, distrust a test that never passed in CI,
  and prove a regression test fails on the pre-fix code. A compile check proves syntax, not
  imports: boot the module or run one fast test.
- **Using a skill: stay in its native format.**

## Review

- **Reproduce review findings before fixing them.** Reviewers cite a mechanism and evidence.
- **Verify an inherited finding by its mechanism**, against production reads or the base commit. A
  corrected detail narrows the claim; it doesn't retract it.
- **A reviewer's note is intent.** If the literal proposal is wrong, build the strongest version
  that isn't, or say why it's a separate PR.

## Tooling

- **Context is the cost.** Every byte is re-read on every later call. Bound every read to the range
  you lack; give read-heavy side tasks to a subagent.
- **Edit files with the host's native editing tool**, never a shell in-place rewrite (it bypasses
  the format and review hooks). Scripts are for mechanical multi-file sweeps.
- **Never remove a worktree inline**; use the kit's worktree cleanup command.
- **`git -C <path>` and `gh -R <owner>/<repo>`**; never `cd` in a compound command.
- **Trace call chains with the host's symbol and caller tools**; the consumer that breaks is often
  two hops out. Symbol results can be silently incomplete: confirm "no caller" or "only N callers"
  with grep before acting on it.
- **Scratch worktrees don't survive across days**; the persistent worktree root does. Read a known
  file first.
- **MCP tools bind at session start.**
- **After two failed patches to a system you can't read**, add observability or hand it back.
