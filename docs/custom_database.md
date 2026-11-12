# Using a Custom Database with resembl

Pass a SQLModel `Session` to the core functions to use your application's database instead of the default `sqlite:///assembly.db`.

## Supported Databases

Set `RESEMBL_DATABASE_URL` (or the unprefixed `DATABASE_URL` it falls back
to, or pass a URL to `create_db_engine`) to point the CLI at a different
backend.  The dialect-specific SQL (bucket upserts, single-row metadata
upserts, DDL) is selected automatically; the sampled average similarity in
`stats` is a portable key-range scan that runs the same on every backend:

| Database    | URL example                                      | Notes |
|-------------|-------------------------------------------------------------|-------|
| SQLite      | `sqlite:///assembly.db` (default)                           | WAL mode, band-major builds: the reference backend. |
| PostgreSQL  | `postgresql+pg8000://user:pass@host:5432/db`                | `ON CONFLICT DO NOTHING`; integration-tested in CI.  Any PG driver works (`psycopg2`, `pg8000`, …). |
| MySQL/MariaDB | `mysql+pymysql://user:pass@host:3306/db`                   | `INSERT IGNORE` / `ON DUPLICATE KEY UPDATE`.  Bucket keys are stored as indexable hex strings (a raw `BLOB` cannot be a primary key on MySQL).  Integration-tested in CI. |
| DuckDB      | `duckdb:///file.db` (via `duckdb-engine`)                   | Needs the `duckdb` extra: `uv sync --locked --extra duckdb` (or `pip install "resembl[duckdb]"` outside a uv checkout).  `ON CONFLICT DO NOTHING`; skips the SQLite-only pragmas.  Bulk paths use multi-row `VALUES` (its `executemany` path is several times slower, measured ~7–13x depending on the table); verified live at 100k snippets. |

The `pg8000` and `pymysql` drivers ship with the package; only DuckDB needs the
extra. The development lock pins DuckDB 2.0.0. By default, DuckDB 2.0 creates
new database files in a storage format that DuckDB before 2.0 cannot read. An
existing file created by an earlier version keeps its storage version when 2.0
writes to it. Keep a copy when testing an upgrade. The
[DuckDB 2.0 migration guide](https://duckdb.org/docs/2.0/operations_manual/migration)
covers files that older readers must open. DuckDB is file-backed, so
the corpus file itself is the backup unit; `resembl backup` does not
cover it, so copy the file only when no DuckDB process has it open and
prove the restore by opening the copy with the DuckDB version that
wrote it; see the [restore runbook](restore.md).

The schema is portable: string primary keys carry explicit lengths and long
text columns use `TEXT`, so `SQLModel.metadata.create_all` emits valid DDL on
all of the above (pinned by `tests/test_pg_dialect.py`).

## The Key Principle: SQLModel Metadata

Importing `resembl.models`, including a single name from it, registers all six table classes with `SQLModel.metadata`: `snippet`, `collection`, `snippetversion`, `lsh_bucket`, `lsh_meta` and `app_meta`.

`SQLModel.metadata.create_all(your_engine)` creates tables for all registered models, including your application's models, in that engine's database.

## Step-by-Step Example

Create an in-memory SQLite database and add a snippet:

```python
# main_app.py
from sqlmodel import SQLModel, create_engine, Session

# 1. Import the resembl models. Importing
#    `resembl.models` registers every table class it defines
#    (`snippet`, `collection`, `snippetversion`, `lsh_bucket`, `lsh_meta`,
#    `app_meta`) with SQLModel's central metadata catalog.
from resembl.models import Snippet

# 2. Import the resembl core functions you want to use.
#    These functions are designed to work with any compatible session.
from resembl.core import snippet_add, snippet_list

# 3. Create your application's custom database engine.
#    For this example, we'll use a temporary in-memory SQLite database,
#    but you could replace this with a PostgreSQL, MySQL, or any other
#    SQLAlchemy-compatible database URL.
my_custom_engine = create_engine("sqlite:///:memory:")

# 4. Create the tables on your custom engine.
#    Because we imported `resembl.models`, this call generates and executes
#    the `CREATE TABLE` statements for all registered `resembl` tables
#    (`snippet`, `collection`, `snippetversion`, `lsh_bucket`, `lsh_meta`,
#    `app_meta`) in our database.
SQLModel.metadata.create_all(my_custom_engine)

# 5. Use your engine to create a session and pass it to resembl functions.
#    From this point on, you interact with resembl by passing your session.
with Session(my_custom_engine) as session:
    print("Adding a snippet using our custom engine...")

    # Call a core resembl function with our session.
    # Each statement goes on its own line: ';' starts a comment in NASM
    # source, so code like "mov eax, 1; ret" would silently drop "; ret"
    # from the normalized snippet (and thus from its checksum).
    new_snippet = snippet_add(
        session=session,
        name="my_first_func",
        code="mov eax, 1\nret",
    )

    print(f"Snippet added. Checksum: {new_snippet.checksum}")

    # Verify the snippet was added to our custom database
    all_snippets = snippet_list(session)
    print(f"Found {len(all_snippets)} snippet(s) in our database.")
    print(f"Retrieved from custom DB: {all_snippets[0].name_list}")

```

## Scaling to Millions of Snippets and Beyond

A search uses indexed LSH bucket lookups with cached banding parameters, then fetches and scores the candidates in chunks. Query work grows with the candidate count: a crowded bucket can admit many snippets, so this is not a constant-time guarantee. In the recorded benchmark datasets, warm `find` latency stayed under a second from 5k to 500k snippets (the query itself was ~1.4 ms in-process; the rest was interpreter startup). Per-snippet import preparation (lexing + fingerprint) measured ~80 µs/snippet, with MinHash permutations cloned from a cached template instead of regenerated. The recorded timings per dataset and backend are maintained in the [README's Performance at Scale](../README.md#performance-at-scale) section.

What the code already supports:

- **PostgreSQL out of the box:** set `RESEMBL_DATABASE_URL=postgresql+pg8000://user:pass@host/db`; the LSH index SQL is dialect-aware (`INSERT OR IGNORE` on SQLite, `ON CONFLICT DO NOTHING` on PostgreSQL, `INSERT IGNORE` on MySQL/MariaDB), and the `reindex` that precedes a rebuild keeps a single final commit on PostgreSQL (the index build itself commits per band chunk on every backend, to bound memory and the SQLite WAL).
- **Bounded-memory bulk import:** `import --jobs N` prepares files in a process pool and flushes in chunks, expunging the session identity map after each chunk.
- **Parallel, crash-safe reindex** (`reindex --jobs`), with the old index cleared up front so an interrupted run can never serve stale results.
- **Incremental index maintenance:** `add` / `import` / `merge` / `rm` update only the affected bucket rows; no full rebuild after single changes.

Guidance for the next order of magnitude (billions of snippets), not implemented in this repository, but the seams are in place:

- **PostgreSQL with table partitioning:** partition `snippet` (e.g. by `checksum` hash) and `lsh_bucket` (by `band`).  The band column already leads the primary key, so per-band bucket partitions make inserts append-friendly and queries partition-prune to one band.
- **Distributed index build:** the build is a pure function over `(checksum, minhash)`; shard the keyset ranges across workers/machines and stream sorted band rows to the database (the band-major insertion in `lsh_index_build` is exactly the pattern a partitioned load would use).
- **Smaller fingerprints:** 128 permutations cost 520 bytes each; a 64-permutation configuration halves storage and band count (14 bands vs 25) at the cost of noisier Jaccard estimates; tune `num_permutations` per dataset.
- **Streaming/replication:** for a read-heavy service, run the store on PostgreSQL with replication and point the CLI at a read replica; the index is maintained by the writer and replicated with the table.
- **Memory is the constraint on one box, not throughput:** the same single-machine numbers extend roughly linearly to ~1M snippets; past that, the database file, import time, and reindex time grow with the data, so the distributed/partitioned setup above is the intended path.
