# API Reference

Public API for using `resembl` as a Python library.

```python
from resembl import (
    snippet_add, snippet_find_matches, snippet_compare,
    snippet_delete, snippet_get, snippet_list,
    code_tokenize, code_create_minhash, string_checksum, string_normalize,
    Collection, Snippet, SnippetVersion,
)
```

## Core Functions

### `code_tokenize(code_snippet: str, normalize: bool = True) → list[str]`
Tokenize assembly code using the Pygments NASM lexer. When `normalize=True`, registers become `REG`, immediates become `IMM`, labels become `LABEL`, and memory sizes become `MEM_SIZE`. Supports x86, ARM, MIPS, and RISC-V register sets.

### `code_create_minhash(code_snippet: str, normalize: bool = True, ngram_size: int = 3) → MinHash`
Create a MinHash fingerprint for a code snippet using weighted n-gram shingling. Rare instruction shingles get 3× insertion weight, common instruction shingles get 1×.

### `code_create_minhash_batch(snippets: list[str], normalize: bool = True, ngram_size: int = 3) → list[MinHash]`
Batch version of `code_create_minhash` for multiple snippets.

### `string_checksum(code_snippet: str) → str`
Return the SHA256 hex digest of the normalized snippet.

### `string_normalize(code_snippet: str) → str`
Normalize an assembly snippet to a canonical string (strips comments, collapses whitespace).

## Snippet Operations

### `snippet_add(session, name: str, code: str, ...) → Snippet`
Add a snippet or alias. Stores the MinHash fingerprint in a compact packed format and keeps the database-backed LSH index in sync.

### `snippet_prepare(name: str, code: str, ngram_size: int = 3) → tuple | None`
Pure function computing `(checksum, name, code, minhash_bytes)` for a snippet — safe to run in worker processes for parallel bulk import.

### `snippet_add_batch(session, prepared_items: list[tuple], ...) → dict`
Insert many prepared snippets in one pass (content-addressable dedup, alias merging, batched writes). Returns `{"added", "aliased", "skipped", "time_elapsed"}`.

### `snippet_get(session, checksum: str) → Snippet | None`
Retrieve a snippet by checksum.

### `snippet_list(session, start: int = 0, end: int = 0) → list[Snippet]`
List snippets, optionally within a `[start, end)` window of the full listing.

### `snippet_delete(session, checksum: str) → bool`
Delete a snippet. Returns `True` on success.

### `snippet_find_matches(session, query: str, top_n: int = 3, threshold: float | None = None, ...) → tuple[int, list]`
Find similar snippets. Returns the LSH candidate count and the top matches
(snippet + hybrid score).  Candidates are scored with a vectorized numpy
Jaccard pass, an early exit that skips Levenshtein for candidates that
cannot beat the current top-N, and full rows are fetched only for
survivors — so the data movement is proportional to the top-N, not the
candidate count.

### `snippet_compare(session, checksum1: str, checksum2: str) → dict`
Compare two snippets. Returns Jaccard similarity, Levenshtein score, hybrid score, CFG similarity, and shared normalized token count.

### `shingle_weight(shingle: str) → int`
Return the insertion weight for a shingle: 3 (rare instruction), 1 (all common), or 2 (default).

### `score_hybrid(jaccard: float, levenshtein: float, jaccard_weight: float = 0.4) → float`
Combine Jaccard (0–1) and Levenshtein (0–100) into a single 0–100 hybrid score.

### `cfg_extract(code: str) → dict`
Extract a simplified control-flow graph from assembly code. Returns `{num_blocks, num_edges, block_sizes, adj}`.

### `cfg_similarity(cfg1: dict, cfg2: dict) → float`
Compute structural similarity between two CFGs (0.0–1.0) using block/edge ratios and cosine similarity on block-size histograms.

### `snippet_version_list(session, checksum: str) → list[dict]`
Return version history for a snippet.

## Collection Operations

### `collection_create(session, name: str, description: str = "") → Collection`
Create a new snippet collection.

### `collection_delete(session, name: str) → bool`
Delete a collection (snippets are kept but unassigned).

### `collection_list(session) → list[dict]`
List all collections with snippet counts.

### `collection_add_snippet(session, collection_name: str, checksum: str) → Snippet | None`
Add a snippet to a collection.

### `collection_remove_snippet(session, checksum: str) → Snippet | None`
Remove a snippet from its collection.

## Models

### `Snippet`
SQLModel with fields: `checksum` (PK), `names` (JSON), `code`, `minhash` (bytes), `tags` (JSON), `collection` (optional FK).

### `Collection`
SQLModel with fields: `name` (PK), `description`, `created_at`.

### `SnippetVersion`
SQLModel with fields: `id` (integer PK, set by the caller; no database-side
autoincrement, which DuckDB does not support), `snippet_checksum`, `code`, `minhash`, `created_at`.

## Configuration

### `ResemblConfig` (dataclass)
Typed config with fields: `lsh_threshold`, `num_permutations`, `top_n`, `ngram_size`, `jaccard_weight`, `format`. Supports dict-like `get()`, `items()`, `update()`.

### `load_config() → ResemblConfig`
Load from `~/.config/resembl/config.toml` (or `RESEMBL_CONFIG_DIR`).

## Database

### `create_db_engine(url: str | None = None)`
Create a SQLAlchemy engine. SQLite pragmas applied automatically (WAL, `synchronous=NORMAL`, `busy_timeout`). Pass a PostgreSQL URL for team use.

### `db_stats(session) → dict` / `db_clean(session) → dict` / `db_merge(session, source_db_path: str) → dict`
Database statistics (count, avg snippet size, vocabulary, sampled avg Jaccard — all SQL-aggregated or sampled, safe at scale); clean (index wipe + `VACUUM` on SQLite only); and merge another database's snippets, deduplicating by checksum while keeping the LSH index in sync.

### `db_reindex(session, ngram_size: int = 3, batch_size: int = 500, jobs: int = 1) → dict`
Recompute every snippet's MinHash. With `jobs > 1` the CPU-bound tokenization runs in a process pool. Clears any built index up front (a crash mid-reindex never leaves a stale index) and commits periodically on SQLite so the WAL stays bounded.

## LSH Index (`resembl.lsh`)

The similarity index is database-backed rather than an in-memory datasketch
structure — band buckets live in the `lsh_bucket` table with parameters in
`lsh_meta`.

### `ResemblLSH(session, threshold: float, num_perm: int)`
A banded MinHash LSH facade over the `lsh_bucket` table. Methods `insert(key, minhash_or_packed)`, `insert_batch(items)`, `query(value) → list[str]`, and `remove(checksum)` accept either a `datasketch.MinHash` or a packed fingerprint blob. The banding parameters `(b, r)` are computed once per `(threshold, num_perm)` and cached (the underlying scipy optimization would otherwise add ~13 ms per construction), and `query` issues all band lookups in a single `UNION ALL` round trip.

### `band_buckets(packed: bytes, num_perm: int, b: int, r: int) → list[str]`
Compute the canonical bucket key for each band of a packed fingerprint
(fixed-width lowercase hex), matching datasketch's banding math. Malformed
blobs raise `ValueError`.

### `lsh_index_build(session, threshold: float, num_perm: int, progress=None) → ResemblLSH | None`
Build (or replace) the database-backed index in `resembl.cache`. Band-major sorted inserts, periodic commits, a deferred `checksum` index, and a raised page cache keep a 500k-snippet build near-linear (~1.8 min on a busy machine). `progress(done, total)` is invoked as snippets are processed. Rebuilding an index is also the lazy path taken by the first `find` on a fresh database.

### `lsh_index_clear(session)` / `lsh_meta_get(session) → tuple[float, int] | None`
Drop the bucket table and metadata (the next find rebuilds), and read the `(threshold, num_perm)` the index was built with.

### `minhash_pack(m) → bytes` / `minhash_unpack(data) → MinHash` / `minhash_jaccard(a, b) → float` / `minhash_jaccard_batch(query, blobs) → list[float]` / `minhash_ensure_packed(data) → bytes`
Packed uint32 fingerprint serialization (520 bytes at 128 permutations, `RMLH`-prefixed, self-describing) and a fast Jaccard computed directly from packed blobs — legacy pickles load transparently. `minhash_jaccard_batch` scores one query against many blobs in a single numpy (SIMD) pass, bit-identical to repeated `minhash_jaccard` calls, with legacy-pickle fallback and chunked memory. Malformed packed blobs raise `ValueError` (never low-level `struct` errors), so hostile or corrupted data cannot crash the query path.

### `minhash_new(num_perm: int = 128) → MinHash`
Return a fresh all-max `MinHash` by cloning a cached template instead of calling datasketch's constructor, which regenerates the permutation arrays with numpy random on every call (~260 µs — the dominant cost of building a fingerprint). The permutations depend only on `(num_perm, seed)`, so cloned fingerprints are byte-identical to directly constructed ones — this is what makes bulk import and `reindex` fast (~80 µs/snippet end to end).

### `Snippet.iter_minhash_batches(session, batch_size=1000)`
Keyset-paginated iterator over `(checksum, minhash)` pairs only — the projected read the index build uses, so building never loads the (much larger) code bodies.
