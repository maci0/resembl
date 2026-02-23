# ADR 001: MinHash + LSH Over SimHash

## Status
Accepted

## Context
We needed a fast, scalable similarity search mechanism for assembly code snippets. The two main contenders were **SimHash** (single-hash fingerprint with Hamming distance) and **MinHash + LSH** (set-based Jaccard estimation with locality-sensitive hashing).

## Decision
We chose **MinHash + LSH** via the `datasketch` library.

## Rationale

| Criterion         | SimHash               | MinHash + LSH           |
|-------------------|-----------------------|-------------------------|
| Similarity metric | Cosine (angular)      | Jaccard (set overlap)   |
| Threshold tuning  | Fixed Hamming radius  | Configurable threshold  |
| Scalability       | O(n) linear scan      | O(1) amortized via LSH  |
| Token ordering    | Lost (bag-of-words)   | Preserved via n-gram shingling |
| Library maturity  | Custom implementation | `datasketch` (maintained, tested) |

MinHash with n-gram shingling preserves local token ordering, which is critical for assembly code where instruction sequences carry structural meaning. LSH provides sub-linear query time as the database grows.

## Consequences
- The `datasketch` library is a runtime dependency.
- The LSH index must be cached to disk for fast startup (see `cache.py`).
- Threshold is user-configurable via `lsh_threshold` in config.
