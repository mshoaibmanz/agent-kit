---
name: handoff
description: Compact the current conversation into a handoff document another agent or a later session can pick up cold - state, decisions made, what's done, what's next, open questions.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

Write a handoff document summarising the current conversation so a fresh agent can continue the work. Save it as `HANDOFF.md` in the bound work item's folder under the work root (`$AGENT_WORK_ROOT`), else `<work root>/handoffs/<TICKET-or-branch>.md` (create the folder if needed), replacing any older handoff for the same key. Tell the next session that path: no host is guaranteed to surface it on its own.

Include a "suggested skills" section in the document, naming which skills the next agent should load.

Do not duplicate content already captured in other artifacts (specs, plans, ADRs, issues, commits, diffs). Reference them by path or URL instead.

Redact any sensitive information, such as API keys, passwords, or personally identifiable information.

If the user passed arguments, treat them as a description of what the next session will focus on and tailor the doc accordingly.
