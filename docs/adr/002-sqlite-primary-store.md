# ADR 002: SQLite as Default Storage Backend

## Status

Accepted (2026-08-11)

## Context
resembl needs a persistent store for snippets and their MinHash fingerprints. Options considered: flat JSON files, SQLite, PostgreSQL.

## Decision
**SQLite** is the default backend, with support for alternative backends
(PostgreSQL, MySQL/MariaDB, DuckDB) via the
`RESEMBL_DATABASE_URL` / `DATABASE_URL` environment variables as the
lookup order in ADR 006, and with the same semantics for the other
environment-derived paths ADR 006 moved into `resembl.paths`.
The unprefixed `DATABASE_URL` is still honored when
`RESEMBL_DATABASE_URL` is unset; `DB_URL_ENV_VARS` in `paths.py` holds the
lookup order.

## Rationale
- **Zero configuration:** No server process, no port, no credentials. A single file.
- **Portable:** The database file can be copied, backed up, or shared.
- **Fast enough:** WAL mode + `synchronous=NORMAL` gives excellent single-user performance.
- **SQLModel/SQLAlchemy:** The ORM layer abstracts the SQL dialect, making a server backend a drop-in replacement when teams need concurrency.

## Consequences
- SQLite has limited concurrent write support. Every `resembl serve` endpoint
  is read-only in the sense that none answers by writing corpus data:
  `POST /find`, `POST /find-batch` and `POST /find-pinned`
  search, `POST /score` and `POST /band-keys` score caller-supplied listings
  without touching the database, `GET /scheme` reports the fingerprint
  scheme, `GET /runtime-generation` reads the process generation,
  `GET /health` runs one `SELECT 1`, and `GET /metrics` reads counters held
  in the process (`docs/http_api.md`). `POST /find-pinned` additionally
  holds a transient SQLite writer fence (`BEGIN IMMEDIATE` … `ROLLBACK`) for
  the duration of its index-identity proof (`_handle_find_pinned` in
  `resembl/server.py`, `docs/http_api.md`); the fence never changes the
  index and is released with the request. The served API therefore never
  issues corpus writes; concurrent writers serialize instead, with a 30 s
  busy timeout applied as a pragma. On a non-loopback bind (which requires
  `--allow-unauthenticated` / `RESEMBL_SERVE_ALLOW_UNAUTHENTICATED`),
  `POST /score`, `POST /band-keys`, `GET /scheme` and `GET /metrics`
  answer `403`; `GET /health` stays open for probes.
- Teams needing shared databases should set `RESEMBL_DATABASE_URL` to a
  PostgreSQL, MySQL/MariaDB or DuckDB connection string.
- SQLite-specific pragmas (WAL, synchronous, busy timeout) are applied
  conditionally in `database.py`.
- The DuckDB driver is an opt-in extra rather than a runtime dependency, so a
  DuckDB URL needs the extra installed.
