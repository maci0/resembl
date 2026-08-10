# Using a Custom Database with resembl

The `resembl` library is designed to be flexible, allowing you to integrate it with your own application's database infrastructure. Instead of being locked into the default `sqlite:///assembly.db` file, you can provide your own database engine. This is possible because all core `resembl` functions operate on a `Session` object that you provide, a design principle known as **Dependency Injection**.

This guide will walk you through the process of using `resembl` with a custom database managed by your application.

## The Key Principle: SQLModel Metadata

The magic behind this flexibility lies in how `SQLModel` manages database schemas. When you import a model class that inherits from `SQLModel` (like `resembl.models.Snippet`), it registers its schema with a central `SQLModel.metadata` object.

When you are ready to create your database tables, you call `SQLModel.metadata.create_all(your_engine)`. This command iterates through all registered models—both yours and `resembl`'s—and creates the corresponding tables in the database pointed to by your engine.

## Step-by-Step Example

Here is a complete example of how an external application can set up its own database and use `resembl` to manage assembly snippets within it.

```python
# main_app.py
from sqlmodel import SQLModel, create_engine, Session

# 1. Import the resembl.models you need.
#    This is the crucial step that registers the `Snippet` model's schema
#    with SQLModel's central metadata catalog.
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
#    Because we imported `Snippet`, this call will generate and execute the
#    `CREATE TABLE` statement for the 'snippet' table in our database.
SQLModel.metadata.create_all(my_custom_engine)

# 5. Use your engine to create a session and pass it to resembl functions.
#    From this point on, you interact with resembl by passing your session.
with Session(my_custom_engine) as session:
    print("Adding a snippet using our custom engine...")

    # Call a core resembl function with our session
    new_snippet = snippet_add(
        session=session,
        name="my_first_func",
        code="mov eax, 1; ret"
    )

    print(f"Snippet added! Checksum: {new_snippet.checksum}")

    # Verify the snippet was added to our custom database
    all_snippets = snippet_list(session)
    print(f"Found {len(all_snippets)} snippet(s) in our database.")
    print(f"Retrieved from custom DB: {all_snippets[0].name_list}")

```

## Summary of the Workflow

To use your own database with `resembl`, follow these steps:

1.  **Import Models:** Before creating your tables, make sure to `import` the `resembl` models you intend to use (e.g., `from resembl.models import Snippet`).
2.  **Create Your Engine:** Instantiate your own `create_engine()` with the desired database URL.
3.  **Create Tables:** Call `SQLModel.metadata.create_all(your_engine)` to create the tables for all registered models in your database.
4.  **Create and Pass the Session:** Whenever you need to call an `resembl` core function, create a `Session` from your engine and pass it as the `session` argument.

By following this pattern, you can seamlessly integrate `resembl`'s functionality into any application while maintaining full control over the database.

## Scaling to Millions of Snippets and Beyond

The architecture is designed to keep the **query path constant-time** regardless of database size: a search is a handful of indexed LSH bucket lookups (all in a single round trip, with banding parameters cached) plus a chunked candidate fetch, so warm `find` latency stays ~0.6 s from 5k to 500k snippets (the query itself is ~1.4 ms in-process; the rest is interpreter startup).  The per-snippet import preparation (lexing + fingerprint) runs at ~80 µs/snippet, since MinHash permutations are cloned from a cached template instead of regenerated.  Measured on a shared machine (busy, under load):

| Dataset | Bulk import | Warm `find` | Cold `find` (one-time index build) | `reindex --jobs` |
|--------:|------------:|------------:|-----------------------------------:|-----------------:|
| 100,000 | ~25 s | ~0.6 s | ~14 s | ~11 s |
| 500,000 | ~2 min | ~0.6 s | ~1.8 min | ~53 s |

(The 100k/500k rows predate the hot-path optimizations; at 20k the measured import and reindex improved ~15–25% — ~4.5 s and ~2.9 s respectively.)

What the code already supports:

- **PostgreSQL out of the box:** set `DATABASE_URL=postgresql://user:pass@host/db`; the LSH index SQL is dialect-aware (`INSERT OR IGNORE` on SQLite, `ON CONFLICT DO NOTHING` on PostgreSQL) and the build keeps a single final commit there.
- **Bounded-memory bulk import:** `import --jobs N` prepares files in a process pool and flushes in chunks, expunging the session identity map after each chunk.
- **Parallel, crash-safe reindex** (`reindex --jobs`), with the old index cleared up front so an interrupted run can never serve stale results.
- **Incremental index maintenance:** `add` / `import` / `merge` / `rm` update only the affected bucket rows — no full rebuild after single changes.

Guidance for the next order of magnitude (billions of snippets) — not implemented in this repository, but the seams are in place:

- **PostgreSQL with table partitioning:** partition `snippet` (e.g. by `checksum` hash) and `lsh_bucket` (by `band`).  The band column already leads the primary key, so per-band bucket partitions make inserts append-friendly and queries partition-prune to one band.
- **Distributed index build:** the build is a pure function over `(checksum, minhash)` — shard the keyset ranges across workers/machines and stream sorted band rows to the database (the band-major insertion in `lsh_index_build` is exactly the pattern a partitioned load would use).
- **Smaller fingerprints:** 128 permutations cost 520 bytes each; a 64-permutation configuration halves storage and band count (14 bands vs 25) at the cost of noisier Jaccard estimates — tune `num_permutations` per dataset.
- **Streaming/replication:** for a read-heavy service, run the store on PostgreSQL with replication and point the CLI at a read replica; the index is maintained by the writer and replicated with the table.
- **Memory is the constraint on one box, not throughput:** the same single-machine numbers extend roughly linearly to ~1M snippets; past that, the database file, import time, and reindex time grow with the data, so the distributed/partitioned setup above is the intended path.
