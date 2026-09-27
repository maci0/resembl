# API Reference

Public API for using `resembl` as a Python library.

```python
from resembl import (
    snippet_add, snippet_add_batch, snippet_find_matches, snippet_compare,
    snippet_delete, snippet_get, snippet_list, snippet_prepare,
    code_tokenize, code_create_minhash, code_create_minhash_batch,
    string_checksum, string_normalize, normalize_unicode,
    Collection, Snippet, SnippetVersion,
)
```

## Text encoding

Snippet code, names and tags are stored in Unicode Normalization Form C, and
every checksum, fingerprint and query is taken over that same form. The same
string can reach resembl in two compositions (`café` as one code point, or
`café` as a base letter plus a combining acute), which are the same text to a
reader and different values to `str`; macOS hands back the second spelling for
any filename containing an accent. Without one form applied at ingestion those
spellings are stored as two snippets that no query can tell apart.

`normalize_unicode` is that form, exposed for callers comparing against stored
values themselves. Compatibility normalization (NFKC/NFKD) is deliberately
not used: it folds superscripts, fullwidth digits and roman numerals into
their ASCII equivalents, which changes the text rather than its spelling.

UTF-8 is the encoding at every boundary, never the platform default: snippet
files are read with it (`resembl import`, `resembl find --file`,
`resembl find-batch --file`), query bodies are UTF-8 JSON, and the process's
own output streams are switched to it at startup
(`resembl.paths.console_utf8_reconfigure`) with unencodable characters
replaced rather than raised. A file the local code page cannot read is
therefore never decoded into mojibake and stored that way, and a snippet
name the local code page cannot print never costs the user a report.

## Core Functions

### `normalize_unicode(text: str) → str`
Return *text* in Normalization Form C. The identity on ASCII, so it is safe to
apply to any string on the import hot path.

The four scoring helpers below (`shingle_weight`, `score_hybrid`,
`cfg_extract`, `cfg_similarity`) are not package-root exports; import them
from their module (`from resembl.scoring import score_hybrid`).

### `code_tokenize(code_snippet: str, normalize: bool = True) → list[str]`
Tokenize assembly code using the Pygments NASM lexer. When `normalize=True`, registers become `REG`, immediates become `IMM`, labels become `LABEL`, and memory sizes become `MEM_SIZE`. Supports x86, ARM, MIPS, and RISC-V register sets.

### `code_create_minhash(code_snippet: str, normalize: bool = True, ngram_size: int = 3, num_perm: int = 128) → MinHash`
Create a MinHash fingerprint for a code snippet using weighted n-gram shingling. Rare instruction shingles get 3× insertion weight, common instruction shingles get 1×.

### `code_create_minhash_batch(snippets: list[str], normalize: bool = True, ngram_size: int = 3, num_perm: int = 128) → list[MinHash]`
Batch version of `code_create_minhash` for multiple snippets.

### `string_checksum(code_snippet: str) → str`
Return the SHA256 hex digest of the normalized snippet.

### `string_normalize(code_snippet: str) → str`
Normalize an assembly snippet to a canonical string (strips comments, collapses whitespace).

## Snippet Operations

### `snippet_add(session, name: str, code: str, ngram_size: int = 3) → Snippet | None`
Add a snippet or alias. Stores the MinHash fingerprint in a compact packed format and keeps the database-backed LSH index in sync. Returns `None` for empty (blank) code.

### `snippet_prepare(name: str, code: str, ngram_size: int = 3) → tuple | None`
Pure function computing `(checksum, name, code, minhash_bytes)` for a snippet — safe to run in worker processes for parallel bulk import.

### `snippet_add_batch(session, prepared_items: list[tuple], ...) → dict`
Insert many prepared snippets in one pass (content-addressable dedup, alias merging, batched writes). Returns `{"added", "aliased", "skipped", "time_elapsed"}`.

### `snippet_get(session, checksum: str) → Snippet | None`
Retrieve a snippet by checksum.

### `snippet_list(session, start: int = 0, end: int | None = None) → list[Snippet]`
List snippets, optionally within a `[start, end)` window of the full listing.
`end=None` (the default) lists everything; an explicit `end` selects a window,
so `end=0` is the empty window rather than "no window".

### `snippet_delete(session, checksum: str) → bool`
Delete a snippet. Returns `True` on success.

### `snippet_export(session, export_dir: str) → dict`
Write every snippet to `export_dir` as `<primary name>.asm`. Returns
`num_exported`, `time_elapsed`, `avg_time_per_snippet`, and `num_removed`
when the run dropped files an earlier run had written. The run converges: the
`.resembl-export.json` manifest in `export_dir` records the files this export
wrote, and a later run removes the ones the database no longer produces (a
snippet renamed or deleted since). Only manifest entries are ever removed,
and only as plain file names, so nothing else in the directory is touched; a
missing or damaged manifest disables pruning for that run.

### `snippet_find_matches(session, query_string: str, top_n: int = 3, threshold: float | None = None, ...) → tuple[int, list]`
Find similar snippets. Returns the LSH candidate count and the top matches
(snippet + hybrid score).  Candidates are scored with a vectorized numpy
Jaccard pass, an early exit that skips Levenshtein for candidates that
cannot beat the current top-N, and full rows are fetched only for
survivors — so the data movement is proportional to the top-N, not the
candidate count.

### `snippet_compare(session, checksum1: str, checksum2: str) → dict | None`
Compare two snippets. Returns Jaccard similarity, Levenshtein score, hybrid score, CFG similarity, and shared normalized token count, or `None` when either checksum is not in the database.

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
Create a snippet collection, or return the existing one of that name with its
stored description and `created_at` untouched. Idempotent.

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
SQLModel with fields: `checksum` (PK), `names` (JSON), `code`, `minhash` (bytes), `tags` (JSON), `collection` (optional collection name; a soft reference to `Collection.name`, not an enforced FK).

### `name_normalize(name: str) → str`
Return *name* in NFC, the form every name is stored and compared in. A name
is identity (a collection's primary key, a snippet alias, an exported file
stem), and the text it comes from is a filesystem entry, a command-line
argument or a file read as UTF-8. macOS yields decomposed spellings and Linux
yields composed ones, so the two platforms spell one file differently. The
ingestion paths (`snippet_add`, `snippet_prepare`, `collection_create`) and the
lookup paths (`Snippet.get_by_name`, `Collection.get_by_name`,
`Snippet.get_by_collection`) both normalize; lookups probe both canonical
spellings, so a row written before normalization still resolves.


### `Collection`
SQLModel with fields: `name` (PK), `description`, `created_at`. `created_at` is
an aware-UTC ISO 8601 string, written by `timestamp_now()`. `db_merge`
re-expresses an imported timestamp in UTC (`timestamp_normalize`), so the
string order of a collection table is chronological; a source row with no
readable timestamp is stamped with the import moment instead.

### `SnippetVersion`
SQLModel with fields: `id` (integer PK, set by the caller; no database-side
autoincrement, which DuckDB does not support), `snippet_checksum`, `code`, `minhash`, `created_at`.

## Configuration

### `ResemblConfig` (dataclass)
Typed config with fields: `lsh_threshold`, `num_permutations`, `top_n`, `ngram_size`, `jaccard_weight`, `format`. Supports `items()`, `to_dict()`, and `update()`, which merges a dict (or another `ResemblConfig`) in, coercing each value to its field's type and, via `validate_value`, rejecting an out-of-enum or out-of-range one with a warning (so a hand-edited `ngram_size = 0` cannot reach the reindex).

### `load_config() → ResemblConfig`
Load from `~/.config/resembl/config.toml`. `RESEMBL_CONFIG_DIR` overrides that
directory outright; otherwise `$XDG_CONFIG_HOME/resembl` is used when
`XDG_CONFIG_HOME` is set. A missing, malformed, or unreadable file yields the
defaults (malformed and unreadable files are logged as errors).

### `validate_value(key: str, value: object) → str | None`
The one configuration-value check: returns why *value* is unusable as a
setting for *key* (wrong enum, outside `VALUE_BOUNDS`, non-finite), or `None`
when it is fine. `resembl config set` refuses a value that returns a message;
`ResemblConfig.update` warns and keeps the current value for one found in a
hand-edited file.

## Paths

`resembl.paths` owns every path derived from the environment. It imports
nothing from the rest of the package and only the standard library, so the CLI,
`resembl serve` and the stdlib-only `resembl-find` client all resolve the same
files without any of them importing another's dependencies.

### `db_url_get() → str` / `db_url_mask(url: str) → str`
The configured database URL, read from the environment at call time:
`RESEMBL_DATABASE_URL` first, then `DATABASE_URL`, else
`sqlite:///assembly.db`. An empty value of either variable counts as unset.
`db_url_mask` replaces an embedded password with `***` for display.

### `cache_dir_get() → str` / `config_dir_get() → str` / `config_path_get() → str`
`RESEMBL_CACHE_DIR` / `RESEMBL_CONFIG_DIR` win outright; otherwise
`$XDG_CACHE_HOME/resembl` or `$XDG_CONFIG_HOME/resembl` when set, else
`~/.cache/resembl` or `~/.config/resembl`.

### `server_port_path(db_url: str, cache_dir: str) → str`
The port file `resembl serve` advertises itself with, named from a SHA-1 prefix
of the *unmasked* URL. The client resolves the same path, so a
credential-carrying `DATABASE_URL` stays discoverable.

## Database

### `create_db_engine(url: str | None = None)`
Create a SQLAlchemy engine, defaulting to `db_url_get()`. SQLite pragmas applied automatically (WAL, `synchronous=NORMAL`, `busy_timeout`). Pass a PostgreSQL URL for team use.

### `db_stats(session) → dict` / `db_clean(session) → dict` / `db_merge(session, source_db_path: str) → dict`
Database statistics (count, avg snippet size, vocabulary, sampled avg Jaccard — all SQL-aggregated or sampled, safe at scale); clean (index wipe + `VACUUM` on SQLite only); and merge another database's snippets, deduplicating by checksum while keeping the LSH index in sync.

### `db_reindex(session, ngram_size: int = 3, batch_size: int = 500, jobs: int = 1, num_perm: int = 128, progress=None) → dict`
Recompute every snippet's MinHash. With `jobs > 1` *and* more than `batch_size` snippets, the CPU-bound tokenization runs in a process pool; below that the pool's spawn cost exceeds the work and it stays sequential. Clears any built index up front (a crash mid-reindex never leaves a stale index) and commits periodically on SQLite so the WAL stays bounded. `progress(done, total)` is called with snippets processed so far.

## LSH Index & Fingerprints

The similarity index is database-backed rather than an in-memory datasketch
structure — band buckets live in the `lsh_bucket` table with parameters in
`lsh_meta`. `ResemblLSH`, `band_buckets`, and `lsh_index_clear` / `lsh_meta_get`
live in `resembl.lsh`; the build/save/load helpers (`lsh_index_build`,
`lsh_cache_save`, `lsh_cache_load`) live in `resembl.cache`; the packed
fingerprint primitives below are defined in `resembl.scoring` and re-exported
by `resembl.models`.

### `ResemblLSH(session, threshold: float, num_perm: int)`
A banded MinHash LSH facade over the `lsh_bucket` table. Methods `insert(key, minhash_or_packed, *, commit=True)`, `insert_batch(items, *, commit=True)`, `query(value) → list[str]`, and `remove(checksum)` accept either a `resembl.minhash.MinHash` or a packed fingerprint blob. The banding parameters `(b, r)` are computed once per `(threshold, num_perm)` and cached (the numpy banding search would otherwise add ~13 ms per construction), and `query` issues all band lookups in a single `UNION ALL` round trip. `remove` does not commit: the caller owns the transaction, which is what lets `snippet_delete` commit the bucket purge together with the snippet row (via `lsh_index_purge` in `resembl.cache`). The same holds for `insert` / `insert_batch` with `commit=False`: `snippet_add`, `snippet_add_batch`, and `db_merge` write a snippet row and its bucket rows in one commit, so a process that dies between the two leaves neither. Re-running the write is free — the `(band, bucket, checksum)` primary key makes both halves naturally idempotent.

### `band_buckets(packed: bytes, num_perm: int, b: int, r: int) → list[str]`
Compute the canonical bucket key for each band of a packed fingerprint
(fixed-width lowercase hex), matching datasketch's banding math. Malformed
blobs raise `ValueError`, as does a banding that does not fit the fingerprint
(`b * r > num_perm`, which would slice past the end of the blob and collapse
the over-long bands into one empty key).

### `lsh_index_build(session, threshold: float, num_perm: int, progress=None) → ResemblLSH | None`
Build (or replace) the database-backed index in `resembl.cache`. Band-major sorted inserts, periodic commits, a deferred `checksum` index, and a raised page cache keep a 500k-snippet build near-linear (~1.8 min on a busy machine). `progress(done, total)` is invoked as snippets are processed. Rebuilding an index is also the lazy path taken by the first `find` on a fresh database.

### `lsh_index_clear(session)` / `lsh_meta_get(session) → tuple[float, int] | None`
Drop the bucket table and metadata (the next find rebuilds), and read the `(threshold, num_perm)` the index was built with.

### `minhash_pack(m) → bytes` / `minhash_unpack(data) → MinHash` / `minhash_jaccard(a, b) → float` / `minhash_jaccard_batch(query, blobs) → list[float]` / `minhash_ensure_packed(data) → bytes`
Packed uint32 fingerprint serialization (520 bytes at 128 permutations, `RMLH`-prefixed, self-describing) and a fast Jaccard computed directly from packed blobs. Blobs without the `RMLH` magic (including legacy pickled fingerprints) are rejected with `ValueError` — they are never deserialized, so a hostile `merge` source or corrupted database cannot execute code; such rows self-heal by recomputing fingerprints from their code (the version-stamp reindex). `minhash_jaccard_batch` scores one query against many blobs in a single numpy (SIMD) pass, bit-identical to repeated `minhash_jaccard` calls, with chunked memory. Malformed blobs raise `ValueError` (never low-level `struct` errors), so hostile or corrupted data cannot crash the query path.

### `minhash_new(num_perm: int = 128) → MinHash`
Return a fresh all-max `MinHash` by cloning a cached template instead of constructing one, which regenerates the permutation arrays with numpy random on every call (~260 µs — the dominant cost of building a fingerprint). The permutations depend only on `(num_perm, seed)`, so cloned fingerprints are byte-identical to directly constructed ones — this is what makes bulk import and `reindex` fast (~80 µs/snippet end to end).

### `resembl.minhash`
First-party MinHash implementation (see ADR 005): bit-compatible with
`datasketch.MinHash` (same seed-1 permutations, SHA1 element hashes, uint64
permutation arithmetic), plus the Gauss-Legendre banding search that replaces
datasketch's scipy-based `_optimal_param`. `tests/test_minhash_equivalence.py`
pins fingerprints, Jaccard values and `(b, r)` parameters against the real
library, which remains a dev-only test oracle.

### `Snippet.iter_minhash_batches(session, batch_size=1000)`
Keyset-paginated iterator over `(checksum, minhash)` pairs only — the projected read the index build uses, so building never loads the (much larger) code bodies.
