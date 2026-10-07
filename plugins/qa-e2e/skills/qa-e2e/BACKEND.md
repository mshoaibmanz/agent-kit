# Stage 1 — Backend up, scenario, contract

Goal: prove the feature's backend contract against a real database before touching the FE.

Repo specifics (the runner and its setup, factory names, helpers, a worked example, traps) are in
`~/.claude/local/qa-e2e/*.md` and the repo's `~/.claude/local/<repo>-testing.md`. Read them first.

## Bring the backend up

Run tests the way the repo's testing doc says; a `test-exec-gate`
hook may refuse any other runner.

```
<runner> <path> -k <name> -x > <scratchpad>/qa-be.log 2>&1
```

- **Foreground, scoped, one invocation.** A path and/or `-k` plus `-x`. A new worktree or cold
  DB can take minutes, so give that Bash call `timeout: 600000`.
- **Capture to a log** in the scratchpad (from your system prompt; `/tmp` is refused) and read
  it; pytest prints captured logs only on failure. Failure block:
  `sed -n '/FAILURES/,/short test summary/p' <scratchpad>/qa-be.log`.

## Build the scenario — factories, never by hand

Build the entity under test with the repo's registered test-data factory, then drive state and
roles through its test-client helpers. No mocks/patches/inline-seeds (repo rule) — real
fixtures keep the test honest against the live schema.

Pick the factory whose stage reaches the state your feature needs. To force a precise data
condition the factory doesn't expose, set it with a raw UPDATE inside the repo's DB context
**relative to `CURRENT_DATE`**, so date-based buckets are deterministic vs the render clock.

## Assert the real contract

Assert the new fields on the actual response shape every renderer consumes — not a synthetic
dict. Cover each bucket of the acceptance rule (positive, threshold boundary, just over,
overdue, fallback), and assert on every payload the contract surfaces in. Check the real DB's
constraints first: a NOT NULL column can make a "missing" state unreachable, so name which
case a NULL input actually exercises.

If the feature adds a variant the existing test misses, extend the test test-first — same
factory, new assertions.
