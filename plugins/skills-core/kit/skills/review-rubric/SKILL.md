---
hosts: [claude, codex, cursor]
name: review-rubric
description: The code-review rubric shared by every reviewer in the kit (bug-reviewer, quality-reviewer, task-reviewer, second-opinion and cross-reviewer) - correctness checks, architecture and design-critique checks, and the one fix policy. Reference material read by path; not a task skill.
disable-model-invocation: true
---

# Review rubric

One rubric, read by path from `{{AGENT_KIT_DIR}}/skills/review-rubric/references/`. The installer owns this skill and its references.

| File | Read by | Use |
|---|---|---|
| `references/correctness.md` | bug-reviewer, task-reviewer, cross-reviewer (inlined in its prompt by `bin/agent-run`) | Defect checks 1-17: root cause, writers and consumers, reruns and races, inputs, proof. Name the check in each finding. |
| `references/architecture.md` | quality-reviewer, task-reviewer, second-opinion | Module depth, seams, leakage. Findings are structural options unless they break a stated invariant. |
| `references/design-critique.md` | second-opinion | Plans and diffs that add or reshape a module, table, endpoint, queue or flow. |
| `references/fix-policy.md` | the fixer (inlined by the review instruction), quality-reviewer, `/address-review` | What gets fixed this round, what becomes an option, how a dismissal is justified. |

The fixer verifies findings directly. The fixer reproduces each finding before fixing it
and closes with one line, `TALLY <id>=FIXED|NOT_REPRODUCED|OPTION|SKIPPED:<reason> ...`. Ids carry
the raising role (`prefix` in `{{AGENT_KIT_DIR}}/roles.toml`): `B-` bug-reviewer, `Q-` quality-reviewer, `R-`
task-reviewer, `CX-` cross-reviewer (the cross-model reviewer, Codex by default). A finding
named after a `correctness.md` check is a defect claim and needs its trigger; one named after an
`architecture.md` or `design-critique.md` check is an option unless it claims a behavioural failure.

`review-output.schema.json` is the structured output every `bin/agent-run` review writes, whatever
the provider (`codex exec --output-schema`, `claude -p --json-schema`). Copied from the Codex Claude
Code plugin `codex@openai-codex` v1.0.6 (`openai/codex-plugin-cc`, `schemas/review-output.schema.json`)
on 2026-10-03, with one change: every finding has a required `id`, which `agent-run` renumbers to
`<prefix>-<n>` by position. Regenerate after a plugin update with
`jq '.properties.findings.items.required = ["id"] + .properties.findings.items.required | .properties.findings.items.properties = ({id: {type: "string", minLength: 1}} + .properties.findings.items.properties)' <plugin>/schemas/review-output.schema.json`.
