---
name: debug
description: This skill should be used when the user asks to "debug with production data", "RCA <X>", "why did <X> happen in prod", "investigate <order/entity/queue> in prod", or to query production data (MySQL replica or BigQuery). Gathers domain context (docs + code), then queries production data, MySQL first with BigQuery as the fallback, to confirm hypotheses and build a root-cause story.
---

# Debug with production data

Build a root-cause story for a production issue: understand the domain and the code first, then
query real production data to confirm or refute each hypothesis. **Prefer MySQL** (read-only
replica through `ro-mysql`, see `references/mysql.md`) for any system whose DB is reachable: it
is the cost-free current-state source. **Fall back to BigQuery** (`bqro`, see
`references/bigquery.md`) when no MySQL connection exists for that system, and always for binlog
history, cross-day aggregation and heavy analytics.

## Company knowledge lives in the skills that extend this one

This skill holds the method. Hosts, schemas, datasets, business keys, enum codes and ready-made
queries are company data. They live in skills that declare `extends: debug` in their frontmatter,
usually a team pack's. Setup lists the installed ones here:

<!-- agent-kit: extensions -->

Before the first query, read the SKILL.md of every skill listed and the files it names. A company
extension typically carries:

- `domain.md`: the systems, the schema source of truth, the business keys that bridge systems,
  and worked examples behind the traps below.
- `mysql.md`, `bigquery.md`: company additions to this skill's references of the same name
  (which schema is on which tunnel, projects and datasets, known replica caveats).
- `query-catalog.md`: ready composites. Lead with one over N single-table lookups.
- `scripts/`: company tools the catalog names. Run the script rather than re-deriving its queries.

The older overlay folder `~/.claude/local/debug/` is a fallback: when it exists, list it and read
every `*.md` in it too; an extension skill wins where they differ.

On a company fact the extension wins; the safety rules in this skill always hold. With neither,
say so, ask the user for the schema source and for the tunnel or dataset that carries the system,
and never guess a table, database or project name.

## When to use

Any debugging or RCA that needs real production data or change history: "why did item X get
dropped", "did A commit before B", "investigate this order", queue rows that were deleted or
updated, checking whether a recent change caused an incident or whether a fix already landed.

## Phase 1: gather context

1. **Read the code first, then the domain docs.** Use code intelligence (LSP, symbol search)
   before grep, and walk the repo's domain docs (`CONTEXT.md`, `CONTEXT-MAP.md`, `docs/adr/`,
   whatever the repo keeps) from the root map down to the subject. A subagent did not get the
   session's injected context: read the repo's root domain map yourself.
   **The code answering your question does not discharge the domain-doc read.** The doc carries
   the classification axes the code never names: which attribute decides a partition's behaviour,
   which partition a gate applies to, which of two fields is authoritative. Skip it and you get a
   confidently wrong headline number, not an error. Read the subject before the first query, not
   after the first correction.
   **For a live incident, read source at the DEPLOYED sha, not the checkout.** A Sentry event's
   `release` tag is that sha: `git show <sha>:<path>`. A local branch behind its remote returns
   pre-change code with no error, and the wrong conclusion looks sound. Check a default argument's
   real value the same way (an `ignore_errors=True` default the caller never overrides) before
   building a mechanism on top of it.
2. **Schema is data, not code.** Read table, column, enum and entity-type definitions from the
   repo's schema source of truth (ORM models, migrations; the overlay's `domain.md` names it).
3. **Map the systems and tables** involved, and the business keys between them, before querying.

## Phase 2: use production data

1. **Pick the backend, MySQL first.**
   - Current state, sync debugging, sensitive tables: MySQL through `ro-mysql` wherever the
     system's DB is reachable (`ro-mysql --tunnels` lists what each tunnel serves). A live
     replica, no scan cost.
   - No MySQL connection for that system, or binlog history, cross-day aggregation, heavy
     analytics: BigQuery through `bqro`. Check the overlay's `query-catalog.md` for a composite
     first.
   - Binlog forensics (deleted or updated rows, exact commit order) is BigQuery-only: the MySQL
     replica shows current state only.
2. **Universal safety, every backend, no exceptions:** read-only. Never run `INSERT`, `UPDATE`,
   `DELETE`, `DROP`, `TRUNCATE` or any other mutation, not even to "just fix a row" mid-debug:
   read, report, let the user run the fix. Validate every table and column against the schema
   source before running a query; never query a hallucinated schema. **Anchor every `WHERE` on an
   efficient column: an indexed column on MySQL, the partition column on BigQuery** (see the
   query-hygiene sections of both references).
3. **Staging is not production.** A tunnel or login path named `*staging*` or `*stg*` is staging,
   and `ro-mysql` prints each target's kind on stderr. Staging ids are fabricated: never carry one
   to prod, never answer a prod, RCA or customer question from staging, and say "staging" in any
   answer sourced from it (`references/mysql.md`, "Staging").

## Cross-system and engine isolation

Read before any cross-system query. When each system owns a separate DB engine, the same
surrogate id (`id_<entity>`) does **not** name the same row across engines and must never be
joined across them. Bridge systems on a shared **business key** (an order, shipment or tracking
number; the overlay names it), as separate statements.

## RCA method

1. **Understand the ask**: read the code, trace the flow, identify the tables involved (Phase 1).
2. **Enumerate plausible causes**: what scenarios could have produced this concern.
3. **Map systems to tables**: which system owns which table, and how they relate (business keys).
4. **Run diagnostic queries**: confirm or refute each hypothesis against production data.
5. **Build the complete story**: the timeline and root cause, with query evidence.
6. **Validate against code**: did a recent change cause it? Did a fix already land that needs
   manual data cleanup? Close the loop between data and code.
7. **After a fix deploys, re-query prod for the SAME signature before calling the RCA closed.**
   "The fix is merged and the pods are up" is not evidence the issue stopped. Confirm three things
   separately, because they fail independently: (a) the deployed revision contains the commit;
   (b) NEW occurrences after the deploy timestamp are zero, bounding the query on time since
   pre-existing rows are still there; (c) rows already broken got cleaned up, or say plainly that
   they still need a data fix. If occurrences continue, the RCA was incomplete: go back to step 2
   and enumerate causes again rather than re-applying the same fix.

## Answer format (every RCA or prod-data answer)

- **Tag every claim `VERIFIED` or `INFERRED`.** VERIFIED names its evidence: the query (or its
  scratchpad file), the row, the log line, or `path:line` at the deployed sha. INFERRED says what
  would verify it. A code path that *could* cause an effect stays INFERRED until a row, log or
  binlog event shows it did: "service A is sending it", stated as fact, is INFERRED until a row
  or log line proves the send.
- **End an RCA with a reply draft** the user can paste to whoever asked (ops, a partner team):
  3 to 6 plain sentences with the verdict, which side owns the fix, and the next action. No
  internal table names; INFERRED claims stay hedged there too.

## Reading data: traps that produce a confident wrong answer

Each of these has already cost a wrong conclusion at least once. The overlay's `domain.md` holds
the concrete cases.

- **Validate ad-hoc analytical SQL with staged probes on real data** (existence, cohort, funnel,
  final), not review subagents: those are for deployable code, not throwaway analysis.
- **Sanity-check any headline number against the base transactional table before presenting
  it.** A figure that contradicts what you would expect ("this cohort is 5x that one") is a query
  bug until proven otherwise.
- **Before reporting a metric off a stored field, confirm the field IS the metric.** A structural
  attribute upstream (carrier or vendor type, route config, a per-client contract) can decide the
  outcome for a whole partition while the field is still written, so the totals reconcile
  perfectly and the answer is wrong. Name the deciding attribute in the writeup. Shape: a
  `vendor` field read the house default on shipments a third party actually handled.
- **Mining event SEQUENCES: pick the ENTITY set first, then fetch each entity's full history.**
  Filtering events by a PK or date window slices journeys mid-flight, so later events are absent
  for structural reasons, which reads as "this status never fires" when it fires fine. State which
  sampling frame any positional claim came from.
- **A `last_*` or current-state column bucketed by time is not an event histogram.** Re-touched
  rows move to a newer bucket, so the distribution always decays backwards and the newest buckets
  always look like a spike. For rates, use an append-only event table or the binlog.
- **"Why is X dominating?" is a FLOW question; current stock is a drain-rate artifact.** Two
  buckets fed at the same rate hold very different stock when their downstream sweeps differ, so
  a point-in-time count can invert the real share. Answer from the decision or allocation rows
  over a bounded window, and label a snapshot as one.
- **A field a background job recomputes cannot tell you what a synchronous check saw.** Compare
  `created_at`/`updated_at` against the timestamp of the action you are explaining before treating
  a stored value as evidence.
  **And `updated_at` is one column: it shows the LAST write only.** Two writes to a row inside your
  window collapse to one, so a single-row read can neither date a value nor prove it never
  changed. For "what was this worth at 03:00" or "what changed at 05:59", read the binlog; it is
  also the only view of tables with no `updated_at` at all, and one `GROUP BY table, type`
  bounded on `ts` enumerates every write in an engine. Reach for that before a third round of
  hypotheses.
- **A command returning nothing is not a negative result until you show it could have returned
  something.** Never redirect stderr on a command whose output you are counting, and establish a
  table's retention window before treating absence as evidence.
- **A pooled metric hides a subgroup effect whenever volume is skewed.** Split by the dimension
  the change was scoped to before concluding it did nothing: a region-wide rate can look flat
  across a step change that moved the small markets sharply, because two large markets carry most
  of the volume. Check the code system too: an ISO-code filter returns zero rows against internal
  codes that merely look like ISO, and zero reads as "structurally unmeasurable".
- **An assertion on an empty query result reports a cause it never checked.** A partner API error
  read "label not generated yet" while the label existed: the query behind the assert was scoped
  to the caller's account and returned zero rows, so "not yours" rendered as "not generated". Read
  the query behind any API error string for scoping clauses and re-run it without them.

## Notes

- **When the failing population has a business-partition column (client, marketplace, carrier,
  country), group the CONTROL by it, not just the outcome.** Outcome parity across a partition
  does not mean the mechanism is shared: a per-client contract can make an absent gate *correct*
  for one partition and a defect for another. One `GROUP BY <partition>` on the gate's own table
  settles it. Shape: an input field was present for every row of both marketplaces, so the axis
  was dismissed, while the gate itself fired for every row of one marketplace and never for the
  other, which inverted the whole RCA.
- **"X was just enabled, is it working?" is a gate-chain question.** Enumerate every gate between
  trigger and outcome and check each in order; the switch that was just flipped is usually the
  LAST gate, and a closed upstream one produces exactly the same "no rows" as a broken feature.
  Report which gate is closed, not that the outcome is absent.
- **Prefer the system's own decision log over reconstructing intent from state.** Where a pipeline
  writes a per-entity verdict row (`*_log` tables, reason-code columns), one GROUP BY on its
  reason codes says which gate rejected what. It is the ONLY readable source when the gate reads
  a remote or feature-flag config that is not in the database at all.
- Keep the loaded domain small: prefer the repo's existing domain docs and schema source over
  ad-hoc exploration.
- Build the story from evidence: cite the queries and rows that prove each step of the RCA.
