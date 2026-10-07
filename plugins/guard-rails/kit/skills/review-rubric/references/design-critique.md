# Design critique rubric

For plans, designs, and diffs that add or reshape a module, table, endpoint, queue or flow. Pair with `architecture.md` for module-level red flags. An objection names a concrete cost of the current shape; "I would have done it differently" is not one.

## Checks

1. **Existing mechanism missed.** Tell: grep the domain noun and event, not only the verb, plus `docs/`, `context/` and `docs/adr/` for a PRD or ADR on the surface. Report: what the design duplicates or contradicts, with its path.
2. **One shape, no contest.** Tell: a single design for a one-way door (schema, partner or public contract, queue payload, data migration) or for a problem with no precedent in the repo. Report: the strongest structurally distinct alternative (a different shape, not a variant), what it changes, and the axis it might win on: depth, locality, seam placement, rollout risk. Skip for mechanical work with a settled pattern.
3. **Bolted on.** Tell: ask what the code would be had the requirement existed on day one. Signs: a flag, mode or nullable column threaded through existing layers; a special case in a shared flow; a new signal carried through several types or schemas to reach one consumer; a parallel table for an existing concept. Report: the day-one shape and the smallest step toward it.
4. **Data shape fights access.** Tell: trace each dominant read and write through the proposed structure. "Add an index, cache or lookup later", or every use reshaping the same data, means the structure is wrong. Report: the access pattern and the shape that serves it.
5. **N specific over one generic.** Tell: a new command, skill, endpoint or package per use case where a mode or declarative config on an existing entry point fits. Report: that entry point.
6. **No caller's view.** Tell: no usage sketch, or an interface shaped by the implementation rather than the dominant caller (the next maintainer is a caller too). Report: the awkward call site and the interface it implies.
7. **Repeated friction.** Tell: the same workaround in unrelated places; unrelated edge cases each needing a branch; types needing casts or always-set optionals; "we need a lock" where the design said nothing was shared; callers needing to know internal rules. Complexity in the data is not complexity in the design, and one hard case is not a pattern. Report: the pattern, and a redesign smaller than the current shape.
8. **Next requirement cost.** Tell: from `git log` on the area, name the likeliest next change and count what it touches. Skip purely hypothetical changes. Report: one module, or the file list.
9. **Plan hygiene.** Tell: steps written before an earlier step's evidence and never revised by it; a done-criterion that cannot fail; an experiment changing two variables; DDL, config or deploy with no order or rollback. Report: the step and its fix.

## Before reporting

- Trace every "what if" to a caller that can actually produce it.
- An abstraction is premature unless the code must vary a second way today.
- A pattern consistent with the rest of the repo is not a finding without a cost.
