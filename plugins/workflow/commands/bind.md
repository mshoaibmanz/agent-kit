---
description: Bind this session to a project (and work item) under the work root, so its knowledge, scripts and handoff are injected now and after /clear or compaction. No argument lists the candidates.
argument-hint: "<project>[/<item>] | <TICKET> | (none: list projects)"
---

With no arguments: run `"${CLAUDE_PLUGIN_ROOT}/kit/bin/agent-task" ls --paths`, show the list, and ask which
project (and item, usually the ticket key) this session belongs to. Then bind.

Otherwise run, once:

`"${CLAUDE_PLUGIN_ROOT}/kit/bin/agent-task" bind $ARGUMENTS`

The session id comes from `$CLAUDE_CODE_SESSION_ID`; pass `--session <id>` only if it is unset.
`<project>/<item>` creates the item (and the project) when missing; a bare ticket key binds to the
project that lists it, else a legacy `tasks/` folder for it, else a new project of one.

Read what it prints: the project slice (scope, knowledge and scripts index, open items, the
item's HANDOFF). Read the HANDOFF and the knowledge files it points at before re-deriving
anything. Keep durable files in the project (`knowledge/`, `scripts/`, `data/`,
`items/<item>/{briefs,out}`); INDEX.md regenerates on every write there.
