---
name: conventions
description: House style for writing code — Python (typing, logging, imports, deps, test layout, pydantic/FastAPI) and SQL (formatting, CTEs, casing). Read BEFORE writing or editing a .py or .sql file, or authoring a query, in any repo.
---

# Conventions

House style only. Footguns live elsewhere: `uv run --with` destroying a project `.venv` in
`~/.claude/CLAUDE.md`; a repo's type-checker false positives in `~/.claude/local/<repo>-rules.md`;
analytical-SQL validation in the `debug` skill ("Reading data").

**A repo's `~/.claude/local/<repo>-rules.md` overrides this file where they differ**, e.g. a repo
without a ruff config, where `ruff format` rewrites whole files.

One file, one entry point: add a language by adding a section here, never by creating a
sibling skill.

## Comments

Default to none. A comment earns its place only by recording what the code cannot: a
non-obvious constraint, an obscure caveat, or why an odd-looking branch exists. Everything
else is noise the next reader skims past.

- **Never narrate control flow.** `# loop over the invoices`, `# check if MPS already exists`
  — the line below already says it.
- **Never restate a name.** Self-naming constants and `_parse_row`-style helpers need no gloss.
- **No ASCII banner separators** (`# ---- parsing ----`). A file needing sections needs splitting.
- **Never reference a PRD, ticket decision, review, or external process.** State the constraint
  itself; provenance lives in the commit/PR, and the reference goes stale the day it merges.
- **Prose belongs in the docs**, not a module docstring — point at the doc in one line.
- **Worth keeping:** a units contract invisible locally (per-unit ×100, total computed
  downstream); a stdlib trap (`decimal.InvalidOperation` stringifies to its condition classes);
  why a tempting refactor is wrong (`lookup_id()` creates addresses as a side effect, so a
  read-only pre-flight can't use it).
- If a boolean condition needs six lines of justification, restructure the condition instead.

## Python

- Match the repo's `requires-python` — never assume a version.
- Type hints on every public function (args + return); no mutable default args.
- Logging via `logging` or `structlog` — never `print()`.
- Imports: stdlib → third-party → local; no wildcards.
- Formatter `ruff format` (line length 100); linter `ruff check --fix`. On Claude Code the
  `verify-edit` hook runs both after every edit, so there treat this as the standard to write
  to. Other hosts have no such hook: run `ruff format <file>` and `ruff check --fix <file>`
  yourself after editing Python.
- Avoid N+1 — bulk-query, loop in code.
- **Never put a non-printing character in a source literal** — write `\uXXXX`, or match by
  Unicode category. A raw zero-width/bidi char renders as mojibake in a diff and cannot be
  reviewed, and a hand-listed range silently misses members (U+061C is not in the common ones).
- Pydantic models for FastAPI request/response; never raw dicts.
- Tests: pytest under `tests/`; prefer end-to-end integration; cover happy + fail paths.
- Deps: `pyproject.toml` + `uv`, pinned in `uv.lock`; no `requirements.txt`. Never
  `pip install --user` or system Python.

## SQL

- Keywords UPPERCASE, identifiers snake_case.
- Leading commas, one column per line.
- JOINs on their own line.
- CTEs at the top (`WITH name AS (...)`) over nested subqueries.
- Never `SELECT *`.
- Formatter `sqlfluff fix` (dialect per-project in `.sqlfluff`).
