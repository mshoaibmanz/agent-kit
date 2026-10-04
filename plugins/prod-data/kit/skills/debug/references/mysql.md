# MySQL backend (preferred)

Production reads go through the `ro-mysql` wrapper. It is the preferred backend for current
state, sync debugging and sensitive tables: live data, no scan cost. Use BigQuery
(`bigquery.md`) only when a system has no reachable MySQL connection, or for binlog history,
cross-day aggregation and heavy analytics.

Company specifics (which schema lives on which tunnel, the DB users, replica and credential
caveats, worked examples) are in `~/.claude/local/debug/mysql.md` when present. Read it too.

> **The session is read-only; the credential may not be.** Before relying on the server to stop
> a write, know what the DB user can do (`SHOW GRANTS`) and whether the target is a replica
> (`@@read_only`, `@@super_read_only`). An account holding `SUPER` or `CONNECTION_ADMIN` is not
> bound by `read_only`. Treat the wrapper as the safety margin.

> **Some tunnels are STAGING.** A tunnel or login path named `*staging*` or `*stg*` is staging;
> every other one is PRODUCTION, and `ro-mysql` prints each target's kind on stderr
> (`ro-mysql: STAGING <tunnel> @<port> …`). Staging carries fabricated test data, so a number
> read there is never evidence about a real entity, and a prod investigation must never quote it.
> See [Staging](#staging) before using it.

## Access

- **Wrapper only:** `ro-mysql --tunnel=<name> -D <db> -e 'SELECT ...'`. Raw `mysql*` clients are
  permission-denied, and a `pymysql` or python one-liner is denied by the kit's Bash guard; neither
  is a workaround.
- **One read-only statement per call**: `SELECT`, `SHOW`, `EXPLAIN`, `DESCRIBE` or `WITH` only.
  The wrapper enforces server-side `transaction_read_only` and a `max_execution_time` (15s by
  default, `MYSQL_RO_MAX_MS`); inline passwords are rejected.
- **A leading `SELECT`/`WITH`/`EXPLAIN` is not enough.** `WITH ... UPDATE`, `EXPLAIN ANALYZE
  UPDATE` and `SELECT ... FOR UPDATE` all write or lock, and the wrapper rejects them. `REPLACE()`
  and `INSERT()` as string functions are fine.
- **The only targets are the `Host db-tunnel-*` entries in `~/.ssh/config`.** `-h` and `-P` are
  rejected, `MYSQL_RO_HOST` must be local, and a port that no `db-tunnel-*` LocalForward serves is
  refused. Without `--tunnel`, `MYSQL_RO_PORT` picks the tunnel. Passthru args are an allowlist
  (`-D`, `--vertical`, `--table`, `--batch`, `--raw`, `--silent`, `-N`, …); anything else is
  refused rather than forwarded.
- **Credentials follow the tunnel.** The Host block's `# ro-mysql: user=<db user>` line names the
  DB user, whose Keychain password `ro-mysql` hands only to the client. Without a Keychain entry it
  falls back to a login path of the same kind on that port. Pass `--login-path` only to force one;
  a login path whose kind differs from the tunnel's is refused.
- **There is no default DATABASE.** A tunnel is not a selected schema: a bare
  `ro-mysql -e 'SELECT …'` fails `ERROR 1046 (3D000) No database selected`. Always pass `-D <db>`.
- **NEVER write**: no `INSERT`, `UPDATE`, `DELETE` or DDL, not even to "just fix a row" mid-debug.
  Read, report, let the user run any fix.
- **One statement per `-e`.** `SELECT …; SELECT …` is refused with
  `one statement per call (multi-statement rejected)`.
- **`lines` is reserved in MySQL 8.** `COUNT(*) lines` fails with 1064 and the message points at
  the *next* token, so it reads as a typo in the wrong place. Rename the alias (`n_lines`).
- **Tunnels drop when idle.** On `(2003) Can't connect`, `ro-mysql` runs `ssh -f -N <alias>` for
  its target and retries the query once. To open one yourself, run `ssh -f -N db-tunnel-<name>`
  exactly: a suffix such as `2>&1; echo rc=$?` falls outside a prefix allow rule.
- **`ERROR 1045 Access denied for user '<db user>'` means the database answered: the password is
  stale.** Re-creating the tunnel changes nothing. `ro-mysql` records the user in
  `~/.claude/state/db-auth-failed`; the user runs `ro-mysql --rotate` in their own terminal, which
  prompts once per DB user, tests it on every tunnel of that user and stores it in the Keychain.
  Never ask for or handle the password yourself.
- **Which tunnel carries a schema: `ro-mysql --tunnels`** (a cached `SHOW DATABASES` per tunnel;
  `--tunnels --refresh` re-checks).
- **A long single-quoted `-e '…'` can die in the harness shell with `(eval):1: unmatched '`**
  even when the quoting is balanced. Do not debug the quote: rewrite as `-e "…"` with every `$`
  backslash-escaped (`\$.json_path`), SQL strings in single quotes inside.
- **MySQL 8 rejects `LIMIT` inside `IN (subquery)`**: `ERROR 1235 … doesn't yet support 'LIMIT &
  IN/ALL/ANY/SOME subquery'`. Wrap it (`IN (SELECT x FROM (SELECT … LIMIT n) t)`), or pull the ids
  with `GROUP_CONCAT` over a derived table and paste them into the next call.

## Query hygiene

- **ALWAYS filter on an indexed column, no exceptions.** Anchor every `WHERE` on the PK or a
  defined index (`id_<entity>`, `<business_key>`, …); an unindexed predicate is a full-table scan
  that hits `max_execution_time` and gets killed. Before the cap kills it, it loads a replica other
  readers share, or competes with live traffic on a primary: the cap limits the damage, it does
  not make a bad query harmless.
- Confirm the index exists before running: `ro-mysql -D <db> -e 'SHOW INDEX FROM <table>'`, or the
  index definitions in the schema source. When unsure a query is index-hit, prefix it with
  `EXPLAIN` and check `key` is non-`NULL` and `type` is not `ALL`.
- **A non-`NULL` `key` is not the RIGHT key.** With both `(col)` and `(created_at, col)` defined,
  `WHERE created_at >= … AND col = X` can pick the narrow `(col)` index and scan that value's
  entire history: instant for a rare value, capped for a common one, and `EXPLAIN` looks healthy
  either way. Read `key` against the index you intended and force it with
  `USE INDEX (<composite>)`. If a hinted aggregation is still near the cap, split the date window
  into chunks rather than widening the query.
- Batch multi-entity lookups with `IN (...)` on the indexed column rather than N single-row
  queries, **but only on a selective column** (a PK or business key). On a low-cardinality
  secondary index each value already fans out to thousands of rows: one value returns instantly
  and ten hit the cap. There, loop single values and aggregate in Python, or `UNION ALL` a
  per-value aggregate.
- **No client backslash-commands**: `\G`, `\.` and `\!` are rejected by the wrapper. For a wide
  row, name the columns you need instead of reaching for vertical output.
- **Stamp `NOW()` into any snapshot you report, and re-run the headline count before writing it
  up.** Operational tables move under you: a group's membership can drain to zero during one
  investigation, so different totals reach intermediate output. If two runs disagree, report
  "snapshot at <ts>, membership is changing", never just whichever ran last.

## Tunnels

- **`ro-mysql --tunnels` is the registry:** every `Host db-tunnel-*` in `~/.ssh/config` with its
  port, kind, DB user, up/down state and the databases it serves. Nothing else lists them.
- **Adding a DB is one `Host db-tunnel-<name>` block** with a `LocalForward <free port>
  <db-host>:3306` and a `# ro-mysql: user=<db user>` line inside it, then
  `ro-mysql --tunnels --refresh`. Name it `*-staging` when it is staging; the name is the only
  thing that makes it one.
- **Two tunnels on one port cannot both be open**, and `ro-mysql` never guesses between them:
  `--tunnel` picks one and is refused while the other holds the port; `--tunnels` flags the clash.
- `Unknown database '<db>'` means the wrong tunnel: look the schema up in `ro-mysql --tunnels`
  rather than hunting for the name.
- On `ERROR 2003 (can't connect)` the wrapper already restarted the tunnel once. If the restart
  reports `Address already in use` yet 2003 persists, an old ssh is holding the port without
  forwarding: `lsof -ti :<port> | xargs kill`, then re-establish.
- **`ERROR 2013 ... 'waiting for initial communication packet', system error: 60` is not a
  credential problem.** The local listener accepted but the jump host could not reach the DB, so
  auth was never attempted. Do not start rotating passwords. Probe the remote leg from the jump
  host instead (`ClearAllForwardings=yes`, or it fights the running tunnel for the port):

  ```
  ssh -o ClearAllForwardings=yes db-tunnel-<name> 'timeout 5 bash -c "</dev/tcp/<db-host>/3306" && echo OPEN || echo UNREACHABLE'
  ```

  Test every DB host behind that jump host: one OPEN and one UNREACHABLE isolates a moved or
  downed box rather than jump-host egress.
- **The DB host lives in the `~/.ssh/config` `LocalForward`, and nowhere else that matters.**
  `ro-mysql` always connects to `127.0.0.1:<tunnel port>` over TCP; a login path's stored host and
  port are never used. After a DB migration, updating `.mylogin.cnf` alone changes nothing.
- **ssh reads its config once at startup.** After editing `LocalForward`, an already-running
  tunnel still carries the old target. Always `lsof -ti :<port> | xargs kill` then re-establish,
  and confirm a fresh pid (`ps -ax -o pid,etime,command | grep db-tunnel`) before concluding
  anything.
- The authoritative current DB host is the secret the application itself resolves (the overlay
  names it). Filter the output rather than printing it; it carries the password.

## Staging

**Staging tunnels (named `*staging*` or `*stg*`) are not production.** They exist for one purpose:
exercising a flow end-to-end against test data (QA runs, verifying an importer or consumer wrote
what you expect, checking a migration's shape before it ships). They are never a source of truth
about a real order, customer, site or client.

```
ro-mysql --tunnel=<name>-staging -D <db> -e 'SELECT …'
```

- **Every staging identifier is fabricated.** A PK or business key from staging does not name the
  same entity in prod, and often names nothing at all. Never carry an id across the two, and never
  resolve a prod investigation with a staging row.
- **Label the environment in every answer that came from a STAGING target** (the kind `ro-mysql`
  prints on stderr): "staging" in the sentence, not only in the command you ran. An unlabelled
  count reads as prod and gets acted on as prod.
- **Prod questions never route here.** "Why did X happen in prod", an RCA, a customer-facing
  number, an "is this data correct" check: those are PROD tunnels. If the prod tunnel is down, fix
  the tunnel; a staging read is not a degraded substitute, it is a different answer.
- **Volumes and topology differ by design.** Staging has a fraction of the rows and a partly
  different set of sites, routes and clients, so "this cohort is empty" or "this status never
  fires" there says nothing about prod. Do not generalise a staging aggregate.
- **A schema missing from staging is not evidence about prod.** Check `ro-mysql --tunnels` for
  which staging tunnel carries what; a schema name that looks close is not the same schema. A
  staging rollout that assumes a missing schema fails at DDL time. When staging cannot answer a
  question, say so and use the application's own endpoints rather than inferring one system's
  state from another's tables.
- **Still read-only, and still one statement per call.** `ro-mysql` enforces the same session
  guarantees; there is no write mode for staging. If a test needs rows seeded or reset, report what
  is needed and let the user run it.
- **A staging DB may be a writable primary** (`@@read_only = 0`). Then there is no server-side
  backstop at all and the wrapper is the *only* thing between a statement and a write. Do not
  reach for a raw client "because it's just staging".
- **A schema name is not evidence of environment.** A staging server can carry a schema whose name
  says `prod`. Confirm the environment by the kind `ro-mysql` prints, never by a schema name.
- **`ro-mysql` pins the pair.** A staging login path on a prod tunnel, or the reverse, is refused,
  because the failure mode is silently reading one environment and reporting it as the other.

## Scanning big tables without hitting the cap

- **`created_at` is often unindexed**, so a bare date filter hits the cap. Bound with an auto-inc
  PK range first: `id >= (SELECT MAX(id) …) - N`.
- Under heavy write churn (an active backfill or upsert storm), even previously fast indexed range
  scans hit the cap: shrink the chunk range.
- **GROUPED scans blow the cap past a few hundred thousand rows** on tables of tens of millions
  (a `GROUP BY` over a JSON extract, say). Loop ~100–150K PK slices and aggregate in Python rather
  than widening the SQL.
- **Anchor a multi-hop join on the LEAF or log table's PK and join outward, not on the root
  entity's.** A root-entity PK range hopping out through four joins hits the cap even on a small
  slice, because the range scan drags every root row through every join. Anchoring the same
  question on the narrow log table you actually care about and joining outward returns
  instantly: the row set is small before the joins start.
- **`GROUP_CONCAT` silently truncates at 1024 chars** (`group_concat_max_len`), and the clipped
  trailing id then makes the next `IN (...)` fail with `ERROR 1064`. Slice the list
  (`cut -d, -f1-N`) before reusing it.

## Preparing a bulk write for the user to run

Pre-compute the row set read-only (chunked), then generate `INSERT … VALUES` batch statements of
~10K rows each. Scan-heavy `INSERT … SELECT` and file imports (`LOAD DATA`) hit a SQL console's
statement caps; VALUES batches are cap-immune and safely re-runnable.

## Picking the database

- When each system is a **separate DB engine** (see the engine-isolation note in `SKILL.md`),
  select the schema with `-D <db>`. The system-to-database map is company data: the overlay's
  `mysql.md` and `bigquery.md`.
- **Confirm reachability first:** `ro-mysql --tunnels` shows which tunnel carries the schema. If
  no tunnel carries a system's DB, **fall back to BigQuery for that system** rather than guessing.
- Validate every table and column against the schema source before querying. MySQL is the schema
  of record; a BigQuery replica can carry fewer columns.

## Cross-system queries

Surrogate ids do not cross engines. Query each engine separately and bridge on a business key in
code; never JOIN across databases. The composite JOINs in the overlay's `query-catalog.md` work
here per engine with `<db>.<table>` references (standard SQL); no partition predicate is needed on
MySQL, that is a BigQuery-only cost rule.
