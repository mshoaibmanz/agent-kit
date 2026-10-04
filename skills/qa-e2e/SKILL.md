---
name: qa-e2e
description: Run a full end-to-end QA pass on a feature — stand up the backend and seed the scenario, launch the app on an Android emulator (or web), drive the real screens, capture screenshots, summarize findings by verification level, and generate an ops SOP for process changes. Use when the user says "QA this", "test this feature end-to-end", "verify <ticket> on device", hands over a testing handoff / PR / Jira ticket, or asks for a process SOP after a UX change.
---

# qa-e2e — feature QA pass

Verify an already-built feature the way a human QA would: build the real scenario, run the
real app, look at the real screen, then write up what you saw and — if operators' work
changes — hand them an SOP. You are **verifying, not building**. The deliverable is an
honest account of *what verification level you reached*, not a green tick.

Repo and machine specifics (test runner, factories, apps, AVDs, render paths, auth, worked
examples) live in `~/.claude/local/qa-e2e/*.md`. Read every file there in Stage 0; with none,
discover what you need and offer to record it there.

## When to use
Someone hands you a feature to check out: a testing handoff, a PR (or set of PRs across
repos), a Jira ticket, or "QA <thing> on device". Also when they want an ops/end-user SOP
written up after a process-changing UX tweak.

## The five stages

Run them in order. Each later stage assumes the earlier one held.

### Stage 0 — Intake & scope
Read the source of truth (handoff / PR diffs / Jira). Do NOT re-derive the design. Extract:
- **Repos touched** and the branch/worktree for each.
- **Backend contract** — the new fields / endpoints / values this feature adds.
- **FE screens** and their render path (which component, which app).
- **Acceptance rule** — the exact condition that makes the change appear (e.g. a date rule).
- **The scenario** to build (which entity, which state, priority vs normal cases).
- **Is this an operator-facing process change?** (adds/reorders a step ops physically do) →
  if yes, Stage 4 produces an SOP; if it's pure backend/no-UX, skip Stage 4.
State the scope back in one paragraph before proceeding.

### Stage 1 — Backend up + scenario + contract  →  see [BACKEND.md](./BACKEND.md)
Stand the backend up, build the scenario with real fixtures/factories (no mocks, no inline
seeds — repo rule), and prove the contract with an integration test. Do this **before** the
FE — a red contract makes FE observation meaningless.

### Stage 2 — Frontend observe (emulator-first)  →  see [FRONTEND.md](./FRONTEND.md)
Launch the app on a real Android emulator, drive to the target screen, and confirm the change
renders on the **priority/positive case AND is absent on the normal/negative case**. Capture a
real screenshot of each. Read logcat / Metro console — a clean `tsc` does not mean runtime-safe.
Web frontends use the Playwright path in the same doc. If a build tier blocks, descend the
**degradation ladder** and record where you stopped.

### Stage 3 — Findings summary
Report per component, tagged by the **verification level actually reached**:
- `contract-green` — integration test passes against the real DB.
- `compile-clean` — the edited FE files typecheck.
- `rendered` — the component renders the change (emulator pixels, or device-free harness tree).
- `on-device` — observed live on the emulator/device wired to a real backend.

For each component: level reached, evidence (test name, screenshot path), what broke, and what
stayed unverified **and why**. Be concrete about the gap — "external app: compile-clean only,
Metro build blocked on node@25" beats "external app: not fully tested".

### Stage 4 — SOP (only if Stage 0 flagged a process change)  →  see [SOP.md](./SOP.md)
Generate a self-contained HTML SOP for ops/end-user consumption via the `process-doc` skill,
fed with the Stage-2 screenshots and the Stage-0 rule. Publish as an Artifact.

## Operating principles
- **Honesty over green.** The point is the verification-level report. Never imply on-device
  when you reached compile-clean.
- **Degrade, don't fail.** If the top tier blocks, drop one rung and say so — some real
  verification beats none.
- **One variable per check.** Confirm priority renders AND normal doesn't — a label that always
  shows is as broken as one that never does.
- **No fabricated artifacts.** A screenshot is the real component on a real surface — never a
  hand-built mockup posing as the app. Can't observe it (auth/deploy/hardware)? Say so and leave a
  placeholder; never fake it.
- **Reuse, don't rebuild.** Backend uses existing factories; SOP uses `process-doc`; web uses
  the Playwright MCP loop. This skill orchestrates; it doesn't reinvent those.
