# Architecture Decision Records

Numbered, immutable records of architecturally significant decisions. Once
accepted, an ADR is only amended to correct provable factual drift; changed
decisions get a new record that supersedes the old one.

| Record | Decision | Status |
| ------ | -------- | ------ |
| [ADR 001](001-minhash-over-simhash.md) | MinHash + LSH over SimHash | Accepted |
| [ADR 002](002-sqlite-primary-store.md) | SQLite as default storage backend | Accepted |
| [ADR 003](003-checksum-as-pk.md) | SHA256 checksum as primary key | Accepted |
| [ADR 004](004-database-backed-lsh-index.md) | Database-backed LSH index | Accepted |
| [ADR 005](005-vendored-minhash.md) | Vendored MinHash, no datasketch runtime dependency | Accepted |

ADR 004 supersedes ADR 001's "the LSH index must be cached to disk"
consequence; ADR 005 supersedes ADR 001's "datasketch is a runtime
dependency" consequence. ADR 001 is annotated accordingly.
