# Architecture rubric

Adds to the thermos quality rubric; never re-raise its checks: 1k-line rule, ad-hoc branching, thin wrappers, casts and `any`, canonical-helper reuse, non-atomic orchestration.

Terms, used exactly: **module** (anything with an interface and an implementation), **interface** (all a caller must know: signature, invariants, ordering, error modes, config), **depth** (behaviour per unit of interface), **seam** (where an interface lives), **adapter** (what fills a seam), **leverage** (what callers gain), **locality** (change lands in one module). Say seam, not boundary; module, not component or service. Domain nouns: the repo's context docs.

Each finding: `file:line`, check name, evidence. All are structural options unless one breaks a stated invariant.

## Checks

1. **Shallow module.** Tell: deletion test; if deleting it makes complexity vanish, it was a pass-through; if it reappears in N callers, it earns its keep. Also: callers chain calls to finish one operation; parameters expose internal stages. Report: the coordinating callers and the one call replacing them.
2. **Information leakage.** Tell: one decision (a status encoding, row or wire shape, a policy) is built or parsed in 2+ modules, so changing it is a coordinated edit. Grep the field or literal. Report: every site and the owning module.
3. **Temporal decomposition.** Tell: modules split by step (load, validate, transform, save) each re-knowing one shape and its invariants. Report: the module grouped by that knowledge.
4. **Hypothetical seam.** Tell: an ABC, Protocol, factory, registry or strategy with one adapter. A test mock is not a second adapter. Report: inline it; name what would justify it.
5. **Testing past the interface.** Tell: tests import private helpers, assert internal state, or break on a behaviour-preserving refactor; a pure function extracted for tests while bugs live in how callers compose it. Report: the interface the test should cross.
6. **Boundary discipline.** Tell: parsing, validation or None-checks deep inside for data the entry point (request, CLI, config, partner response, DB row) already validated; raw dicts past the entry point; business logic in a route, consumer or hook instead of a pure function it calls. Report: where parsing belongs and the checks to delete.
7. **Illegal states representable.** Tell: field combinations whose validity needs a comment (a flag plus an optional timestamp); two same-primitive arguments meaning different ids; an enum match with no `assert_never`; a hand-written model duplicating a schema, migration or partner spec. Report: the union, NewType or derived model.
8. **Dual path.** Tell: a new API, flag or module lands while the old one stays live with only internal callers (grep them). Report: callers to migrate and the path to delete in the same change; an old-API test deleted instead of migrated is a dropped contract.
9. **N specific entry points.** Tell: a new command, endpoint, job or package near-duplicating an existing one where a mode or declarative config would do. Report: the existing entry point and its config.
10. **Reader load.** Tell: "where does X come from?" or "what can change X?" takes more than 3 files; stored state where a local or return value would do; two values synced by hand instead of one derived. Report: the layer to collapse or the state to shrink.
11. **Speculative surface.** Tell: validators, knobs, retries or parameters for inputs nothing produces today. Report: what to delete.

## Deepening a candidate

Classify each dependency. In-process: merge, test through the new interface. Local-substitutable (DB, filesystem): test against the real local instance. Remote or third-party: a port at the seam, tested as the overlay's `<repo>-testing.md` prescribes (`~/.claude/local/` on Claude, the kit's `local/` elsewhere), never with a mock adapter in a backend suite. The deepened module replaces the shallow ones; if they survive underneath, it added a layer.
