# ADR 005: Vendored MinHash, No datasketch Runtime Dependency

## Status

Accepted (2026-08-23)

## Context

ADR 001 chose MinHash + LSH "via the `datasketch` library" and listed
`datasketch` as a runtime dependency. By 2026 the codebase had already grown
its own implementations around that dependency: the LSH index lives in SQL
(ADR 004), Jaccard scoring runs directly on packed uint32 fingerprints with a
numpy vectorized batch path, fingerprints use a custom self-describing byte
format, and MinHash construction already bypassed datasketch's constructor via
cached template cloning.

What remained was a narrow surface: the `MinHash` class itself (permutation
tables, element hashing, per-position minimum), the private
`datasketch.lsh._optimal_param` banding search, and two type references. For
that surface the runtime tree paid for:

- **scipy** (~60 MB installed, pulled in unconditionally by datasketch and
  loaded by any `datasketch` import) — used by datasketch only to evaluate
  two smooth one-dimensional integrals.
- A coupling to a **private API** (`_optimal_param`) with no stability
  guarantee.
- The full datasketch package (LSH variants, HyperLogLog, weighted/B-bit
  MinHash, HNSW, GPU backends) of which resembl used none.

## Decision

Vendor a minimal, bit-compatible MinHash into `resembl.minhash`:

- Same seed-1 permutation tables (legacy MT19937 stream, same draw order),
  same little-endian SHA1-prefix element hash, same uint64 wraparound
  arithmetic in datasketch's operation order — produced fingerprints are
  byte-for-byte identical, so stored databases stay valid without a
  fingerprint-version bump.
- `optimal_param()` reproduces `_optimal_param`'s error minimization with
  fixed-node Gauss-Legendre quadrature (numpy only) instead of
  `scipy.integrate.quad`; it selects the identical `(b, r)` across the
  threshold/num_perm/weight grid pinned in tests.

Remove `datasketch` from `[project.dependencies]` and keep it as a dev-only
test oracle: `tests/test_minhash_equivalence.py` cross-checks permutations,
digests, Jaccard values and banding parameters against the real library, so a
future datasketch behavior change fails CI instead of silently diverging.
numpy stays a direct production dependency (vectorized batch Jaccard).

ADR 001's MinHash-over-SimHash decision is unchanged; only the implementation
provenance changes.

## Consequences

- Production installs drop `datasketch` and `scipy`: smaller attack surface,
  faster cold install, no scipy startup cost anywhere in the CLI.
- The private-API coupling (`datasketch.lsh._optimal_param`) is gone; the
  banding math lives in-tree under tests.
- Compatibility with datasketch is now an asserted contract rather than an
  inherited guarantee; the equivalence suite is the enforcement point.
- If fingerprints must ever change format or semantics, bump
  `FINGERPRINT_VERSION` (the existing self-healing reindex handles migration).
