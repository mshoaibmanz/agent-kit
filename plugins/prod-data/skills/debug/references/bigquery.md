# BigQuery backend (fallback)

Rules and patterns for querying production data with `bqro`. This is the **fallback** backend
(MySQL is preferred where a connection exists, see `mysql.md`): use BigQuery when a system has no
reachable MySQL DB, and always for binlog forensics, cross-day aggregation and analytics.

Projects, datasets, the system-to-dataset map and the binlog location are company data:
the `bigquery.md` of a skill that extends debug (SKILL.md lists them), else
`~/.claude/local/debug/bigquery.md` when present. Read it too; the placeholders below
(`<jobs_project>`, `<data_project>`, `<dataset>`, `<binlog_project>`, `<binlog_dataset>`,
`<binlog_table>`) come from there.

## Defaults and access

- **Query with `bqro 'SELECT …'`.** Raw `bq query` is permission-denied. `bqro` dry-runs first,
  rejects non-SELECT, prints scan size and on-demand cost, and enforces `--maximum_bytes_billed`
  (`BQ_MAX_BYTES_BILLED`, default 200 GiB).
- **Jobs project and data project are different things.** Jobs run in a project where you hold
  `bigquery.jobs.create`: `bqro` uses `--project_id=<jobs_project>`, else `$BQRO_PROJECT`, else
  `BQRO_PROJECT` in `~/.claude/local/kit.env`, and refuses to run with none of the three.
  A project you only read from can lack that permission; name it in the table reference, never as
  the jobs project.
- **Read project and dataset names character by character.** A near-miss name 404s exactly like a
  missing table, and a transposed name is easy to type from memory.
- **Datasets live in a region.** List them with
  `` `<data_project>`.`region-<location>`.INFORMATION_SCHEMA.SCHEMATA ``; the project-level
  `<data_project>.INFORMATION_SCHEMA.SCHEMATA` silently returns `[]`.
- **A view-layer project is not the whole surface.** When a data project is a pass-through view
  layer over a base project, not every base table has a view. Check the base project before
  concluding "not synced to BigQuery".
- **Read-only: never** run `DELETE`, `DROP`, `TRUNCATE` or `INSERT`.
- **`bqro` auth rides gcloud credentials and expires.** A non-interactive refresh dies with
  `Reauthentication failed. cannot prompt`. Have the user run `! gcloud auth login` in-session,
  then retry once; do not loop on it.

## `bqro` CLI quirks

- **Don't pass `--format=csv`**: it breaks the statement-type dry-run guard ("could not verify
  statementType from dry run; refusing to execute"). Use the default output.
- **The write guard reads the whole command string, so a read-only SELECT can be refused for its
  text.** A binlog query filtering `AND type = 'update'` trips `Refusing a production WRITE` even
  though it is a plain SELECT on a column named `type`. Rewrite the predicate
  (`type LIKE 'upd%'`) rather than fighting the guard. The same guard fires on any Bash command
  that merely *names* the CLI, a `grep` for it included, and its own error text gives the
  workaround: put the content in a file with Write and reference the path.
- **`bqro` truncates at 100 rows when `--max_rows` is omitted, silently.** Pass `--max_rows=5000`
  on every aggregate query. A result of exactly 100 (or exactly your N) means you hit the cap.
  Reconcile a GROUP BY's total against a second, coarser cut before trusting any of its cells.
- **`bq show --schema` takes 30s+ per table.** Answer schema and partition questions in one query
  against `<dataset>.INFORMATION_SCHEMA.COLUMNS` instead (`is_partitioning_column = "YES"` finds
  the partition key).

## Query hygiene

- **Always use fully qualified table names**: `<data_project>.<dataset>.<table>`.
- **ALWAYS filter the partition column on every query, no exceptions** (`event_date`,
  `created_at`, `_PARTITIONTIME`, …). An unpartitioned query is a full-table scan and the single
  biggest cost in a session. A point lookup by id still needs a partition predicate: widen the
  window if the exact date is unknown, never drop it.
- **One composite query, not N single-table lookups.** Within one engine, JOIN the entities you
  need in a single statement keyed on the business key instead of round-tripping table by table,
  and batch multi-entity lookups with `<key> IN (...)`. Ready composites live in the overlay's
  `query-catalog.md`.
- **Discover columns through `INFORMATION_SCHEMA.COLUMNS`**: one query returns names, types and
  the partition key. `SELECT * … LIMIT 1` also works but costs a scan and shows nothing about
  partitioning.

## Replica datasets

When BigQuery holds a replica of the MySQL tables (typically one dataset per engine and schema):

- **Some tables are excluded from sync by policy** (the overlay names the rule, such as a table
  suffix). Do not query for them; get that data another way.
- Replication is eventually consistent (binlog to BigQuery pipeline); data may lag seconds to
  minutes behind prod.
- **Validate query structure against the schema source** before running, especially for new
  tables and columns. No hallucinated schema.
- **The replica can carry FEWER columns than the MySQL schema.** Confirm a column exists in
  BigQuery (`INFORMATION_SCHEMA.COLUMNS`, or `SELECT * … LIMIT 1`) before selecting it, or the
  query 400s with `Name … not found`.

## System to dataset mapping

Company data. The overlay fills this table in:

| System     | Dataset                                  |
| ---------- | ---------------------------------------- |
| `<system>` | `<data_project>.<dataset>.<table>`       |

## Cross-system joins

Surrogate ids do not cross engine boundaries (see the engine-isolation note in `SKILL.md`). Join
across systems on a shared **business key**, never a surrogate id. **Check the key's coverage
before trusting the join:** a key that covers one entity shape can miss another (a consolidated
parent keyed on a different column than a single piece), and a partner's reference number is not
yours. One `COUNT` of matched vs unmatched rows settles it.

## Binlog forensics

For queue tables where rows are **deleted or updated**, the current-state replica shows nothing:
use the binlog stream (Maxwell-style JSON rows: `data`, `old`, `ts`, `xid`, `commit`, `position`).
The overlay names its project, datasets and table.

### Example binlog query

```sql
SELECT data, old, TIMESTAMP_SECONDS(ts) AS event_ts, *
FROM `<binlog_project>.<binlog_dataset>.<binlog_table>`
WHERE _PARTITIONTIME >= TIMESTAMP('<event_date>')        -- PRUNE: ingestion-time partition
  AND ts >= UNIX_SECONDS(TIMESTAMP '<event_date>')       -- CORRECTNESS: exact event-time window
  AND `table` = '<table>'
  AND JSON_EXTRACT_SCALAR(data, "$.<pk_column>") = "<id>"
ORDER BY ts ASC
LIMIT 100
```

**Partition is not event time.** When the binlog table is **ingestion-time partitioned**
(`_PARTITIONTIME`, midnight-truncated), ingestion can lag the event by hours, up to about a day,
so an event lands in the next day's partition. Therefore:

- Prune on `_PARTITIONTIME` (or the ingestion timestamp column) with the window **padded forward
  ~2 days** past the event date; a tight `_PARTITIONTIME = <event_date>` MISSES late-ingested rows.
- Filter the *real* event time on `ts` (Unix seconds) for correctness; never treat the ingestion
  timestamp as the event clock.
- An unbounded binlog scan (no `_PARTITIONTIME`) is the single biggest cost in a debug session.

### Ordering: intra-second and race RCAs

`TIMESTAMP_SECONDS(ts)` has **1-second granularity** and hides intra-second order. For race and
ordering RCAs, select `xid`, `commit` and `position` and `ORDER BY position ASC` for exact commit
order; **group by `xid`** to see which writes were one transaction. This is decisive for "did A
commit before B" questions (two workers racing on one row).

### Deleted-row reconstruction

Read `data` (new image) and `old` (prior image) across the insert, update and delete sequence to
reconstruct a row's state at any point in time, including rows no longer present in the replica.
