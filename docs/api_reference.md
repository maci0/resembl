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
Add a snippet to the database. Returns the created `Snippet`.

### `snippet_get(session, checksum: str) → Snippet | None`
Retrieve a snippet by checksum.

### `snippet_list(session) → list[dict]`
List all snippets with names, tags, and checksums.

### `snippet_delete(session, checksum: str) → bool`
Delete a snippet. Returns `True` on success.

### `snippet_find_matches(session, query: str, top_n: int = 5, threshold: float = 0.5, ...) → dict`
Find similar snippets. Returns a dict with `"matches"` list containing checksums, names, and similarity scores.

### `snippet_compare(session, checksum_a: str, checksum_b: str) → dict`
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
SQLModel with fields: `id` (auto PK), `snippet_checksum`, `code`, `minhash`, `created_at`.

## Configuration

### `ResemblConfig` (dataclass)
Typed config with fields: `lsh_threshold`, `num_permutations`, `top_n`, `ngram_size`, `jaccard_weight`, `format`. Supports dict-like `get()`, `items()`, `update()`.

### `load_config() → ResemblConfig`
Load from `~/.config/resembl/config.toml` (or `RESEMBL_CONFIG_DIR`).

## Database

### `create_db_engine(url: str | None = None)`
Create a SQLAlchemy engine. SQLite pragmas applied automatically. Pass a PostgreSQL URL for team use.
