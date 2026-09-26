# Correctness rubric

Adds to the thermos review rubric and your own efficacy check; do not restate either.

## Root cause

1. **Symptom guard.** Tell: the fix adds a None-check, try/except, default, retry or cast where it crashed. Follow the bad value to its producer. Report: the producer line, the invariant it breaks, and other readers of the bad value the guard misses.
2. **Instance, not pattern.** Tell: grep the defective shape (same call, query or comparison) repo-wide. Report: sibling sites left unfixed.
3. **Wrong module.** Tell: module A compensates for module B's contract (reshapes, filters or re-sorts its output). Report: the contract in B to change.
4. **Rule by comment.** Tell: the fix is a comment or convention ("call X before Y") where a type, DB constraint, unique key or runtime check would make the wrong call impossible. Report: the enforcement.
5. **Stale state.** Tell: fails after restart, redeploy or retry and clears when a cache, lock, config row or serialized blob is reset. Report: that state and the missing check on load.

## Writers and consumers

6. **Every writer.** Tell: the change guards or keys on a status, column or key. Grep the value and the column; list every path that writes it, including bulk ops, scripts, consumers and combined operations. Report: writers that bypass the change.
7. **Every consumer of the outcome.** Tell: a path that used to fail, fall back, raise or return empty now succeeds, or the reverse. Grep what read the old result. Report: the consumer whose behaviour changes, and how.
8. **Meaning at the source.** Tell: behaviour keyed on a field's name. Read its writer; check that a new validation mirrors what the consumer (partner API, DB constraint, downstream service) enforces. Report: the mismatch, both sides quoted.

## Reruns and races

9. **Runs twice.** Tell: in a state-mutating handler, job, consumer, webhook, migration or script: INSERT without a unique key or upsert; unconditional status transition; counter increment; partner call or message sent before commit; backfill without a not-yet-done predicate. Check what upstream retries or redelivers. Report: the state after the second run.
10. **Crashes halfway.** Tell: several writes, or a write plus a side effect, with no transaction or reconciliation. Walk a crash after each step. Report: the step, the state left, and whether the next run converges.
11. **Shared mutable target.** Tell: two actors (requests, workers, cron and request, threads) write one row, key, file or cache entry; read-modify-write with no lock or compare-and-swap; check-then-act; module-level mutable state in a multi-worker process; single-writer held only by a comment. Report: the interleaving that loses or corrupts a write. Prefer removing the sharing (per-actor row or key, merged at read) over a lock.

## Proof

12. **Regression test that cannot fail.** Tell: a bug fix whose new test would pass on the base commit (run it there when cheap, or show its assertion never reaches the changed line). Report: the test and why the old code passes it.
13. **Proxy assertion.** Tell: asserts a mock call, log line, status code or return flag instead of the persisted value or observable output; a mock, patch or stub in a backend or integration test is blocking. Report: the real value to assert.
14. **Weakened contract.** Tell: a test edited with the code has an assert dropped, loosened or rewritten to match new output. Report: each one as a contract change the objective must justify.
15. **Unverified claim.** Tell: presented as verified on compile, lint, type-check or a job's self-report alone. Report: the cheapest check on the real artifact.
