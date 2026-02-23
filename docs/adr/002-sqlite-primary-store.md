# ADR 002: SQLite as Default Storage Backend

## Status
Accepted

## Context
resembl needs a persistent store for snippets and their MinHash fingerprints. Options considered: flat JSON files, SQLite, PostgreSQL.

## Decision
**SQLite** is the default backend, with support for alternative backends via the `DATABASE_URL` environment variable.

## Rationale
- **Zero configuration:** No server process, no port, no credentials. A single file.
- **Portable:** The database file can be copied, backed up, or shared.
- **Fast enough:** WAL mode + `synchronous=NORMAL` gives excellent single-user performance.
- **SQLModel/SQLAlchemy:** The ORM layer abstracts the SQL dialect, making PostgreSQL a drop-in replacement when teams need concurrency.

## Consequences
- SQLite has limited concurrent write support — adequate for a CLI tool but not for a web API.
- Teams needing shared databases should set `DATABASE_URL` to a PostgreSQL connection string.
- SQLite-specific pragmas (WAL, synchronous) are applied conditionally in `database.py`.
