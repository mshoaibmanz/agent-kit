---
name: handoff
description: Compact the current conversation into a handoff document another agent or a later session can pick up cold - state, decisions made, what's done, what's next, open questions.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

Write a handoff document summarising the current conversation so a fresh agent can continue the work. Save to `~/.claude/handoffs/<TICKET-or-branch>.md` (create the folder if needed), replacing any older handoff for the same key. SessionStart surfaces it when a session opens on that branch.

Include a "suggested skills" section in the document, naming which skills the next agent should call the Skill tool for.

Do not duplicate content already captured in other artifacts (specs, plans, ADRs, issues, commits, diffs). Reference them by path or URL instead.

Redact any sensitive information, such as API keys, passwords, or personally identifiable information.

If the user passed arguments, treat them as a description of what the next session will focus on and tailor the doc accordingly.
