# ADR 002: SQLite as Default Storage Backend

## Status

Accepted (2026-08-11)

## Context
resembl needs a persistent store for snippets and their MinHash fingerprints. Options considered: flat JSON files, SQLite, PostgreSQL.

## Decision
**SQLite** is the default backend, with support for alternative backends
(PostgreSQL, MySQL/MariaDB, DuckDB) via the `RESEMBL_DATABASE_URL`
environment variable. The unprefixed `DATABASE_URL` is still honored when
`RESEMBL_DATABASE_URL` is unset; `DB_URL_ENV_VARS` in `paths.py` holds the
lookup order.

## Rationale
- **Zero configuration:** No server process, no port, no credentials. A single file.
- **Portable:** The database file can be copied, backed up, or shared.
- **Fast enough:** WAL mode + `synchronous=NORMAL` gives excellent single-user performance.
- **SQLModel/SQLAlchemy:** The ORM layer abstracts the SQL dialect, making a server backend a drop-in replacement when teams need concurrency.

## Consequences
- SQLite has limited concurrent write support. `resembl serve` exposes only the
  read-only `/find` and `/find-batch` endpoints (`docs/http_api.md`), so the
  served API never issues writes; concurrent writers serialize instead, with a
  30 s busy timeout applied as a pragma.
- Teams needing shared databases should set `RESEMBL_DATABASE_URL` to a
  PostgreSQL, MySQL/MariaDB or DuckDB connection string.
- SQLite-specific pragmas (WAL, synchronous, busy timeout) are applied
  conditionally in `database.py`.
- The DuckDB driver is an opt-in extra rather than a runtime dependency, so a
  DuckDB URL needs the extra installed.
