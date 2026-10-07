---
description: Bind this session to a project (and work item) under the work root, so its knowledge, scripts and handoff are injected now and after /clear or compaction. No argument lists the candidates.
argument-hint: "<project>[/<item>] | <TICKET> | (none: list projects and items)"
---

With no arguments: run `"${CLAUDE_PLUGIN_ROOT}/kit/bin/agent-task" ls`, show the projects with their items (status,
last touched), and ask which this session belongs to. Then bind.

Otherwise run, once:

`"${CLAUDE_PLUGIN_ROOT}/kit/bin/agent-task" bind $ARGUMENTS`

The session id comes from `$CLAUDE_CODE_SESSION_ID`; pass `--session <id>` only if it is unset.
`<project>/<item>` creates the item (and the project) when missing, recording this checkout's
worktree and branch on it. `<project>` alone binds its sole open item, else the project only and
lists its open items. A bare ticket key binds the item the index maps it to, else
`<the project that lists it>/<ticket>`, else a new project of one.

Read what it prints: the `bound:` line, then the slice (scope, open items, knowledge titles, proven
scripts, the item's HANDOFF opening). Read the HANDOFF and the knowledge it names before
re-deriving anything. Reusable scripts go to the project's `scripts/` with a `  - use:` line in
INDEX.md; the item keeps `HANDOFF.md`, `briefs/`, `out/` (evidence) and `tmp/` (disposable).
