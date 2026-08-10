# ADR 004: Database-Backed LSH Index

## Status
Accepted

## Context
ADR 001 chose MinHash + LSH via `datasketch` and noted "the LSH index must be
cached to disk for fast startup." In practice that meant the entire in-memory
`MinHashLSH` object was pickled to a cache file on every change and loaded
back on every search.

This design does not scale:

- The pickled index is O(database size) — hundreds of megabytes at 100k+
  snippets, taking seconds to write and read.
- Every add/delete invalidated the whole cache, forcing a full rebuild on the
  next search (rebuilds also materialized every snippet + fingerprint in
  memory at once).
- Candidate scoring unpickled a `MinHash` object per candidate (~300 µs each),
  which dominated query time when LSH returned many candidates.

## Decision
Replace the in-memory `datasketch.MinHashLSH` + pickle cache with a
**database-backed banded LSH index**:

- A `lsh_bucket` table holds one row per (band, bucket, checksum) triple —
  the bucket key being the canonical big-endian uint32 encoding of each band
  of hash values, computed directly from the packed fingerprint bytes.
- A single-row `lsh_meta` table records the `(threshold, num_perm)` the index
  was built with; a mismatch (or absence) triggers a rebuild.
- Banding parameters are still derived with datasketch's `_optimal_param`,
  so recall behavior at a given threshold is unchanged.
- Fingerprints are stored as packed uint32 arrays (520 bytes at 128
  permutations, `RMLH`-prefixed, self-describing) instead of pickles.

## Rationale
- **Constant-time warm queries:** a search is a handful of indexed point
  lookups (one per band) plus a chunked `IN` fetch — independent of database
  size (measured ~0.5 s wall including interpreter startup at 100k snippets).
- **Incremental maintenance:** `add`, `import`, `merge`, and `rm` update only
  the affected snippets' bucket rows, so a search never requires a rebuild.
- **Streaming builds:** rows are inserted in batches; on SQLite the build
  commits periodically (every ~100k rows) so WAL autocheckpoint keeps the
  write-ahead log bounded — one giant transaction spanning the whole build
  would grow the WAL to the size of the index (hundreds of MB at scale) and
  force one huge checkpoint at commit.  A crash mid-build leaves only a
  partial index, which is invisible until `lsh_meta` is set and is wiped on
  the next build, so atomicity is not required.  PostgreSQL segments its own
  WAL and pays an fsync per commit, so it keeps a single final commit.
- **Append-friendly builds:** rows are buffered per band and inserted
  band-major sorted by bucket, so the `(band, bucket, checksum)` primary key
  grows by sequential append instead of random probe — random inserts into a
  deep b-tree decay badly (measured ~4× slower by the tail of a 12.5M-row
  build).  The build also raises the SQLite page cache (the default 8 MiB
  thrashes) and drops/recreates the `checksum` secondary index around the
  bulk load.  Keyset pagination keeps memory bounded, and the build reads
  only the `checksum` / `minhash` columns (not the code bodies).
- **Bounded memory:** no full index is ever held in RAM; the identity map is
  expunged per batch.
- **Faster scoring:** Jaccard is computed element-wise directly from packed
  bytes, matching `datasketch.MinHash.jaccard` semantics without constructing
  objects (measured ~75× faster over thousands of candidates).

## Consequences
- The `lsh_bucket` / `lsh_meta` tables are created automatically via the
  ORM metadata (`db_create`, `table_ensure`).
- Legacy pickle cache files (pre-ADR-004) still load transparently and
  migrate to the database-backed index on the next write operation.
- The index SQL is dialect-aware: SQLite uses `INSERT OR IGNORE`, PostgreSQL
  uses `ON CONFLICT DO NOTHING`.
- `reindex --jobs N` recomputes fingerprints in a process pool (~5× faster
  with 8 workers), since the CPU-bound tokenization was the last sequential
  bottleneck.  On SQLite it commits periodically (bounded WAL) and clears
  any built index *before* rewriting fingerprints, so a crash mid-reindex
  can never leave a stale index behind.
- ADR 001's consequence "the LSH index must be cached to disk" no longer
  applies; the index lives in the database.
