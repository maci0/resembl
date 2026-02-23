# ADR 003: SHA256 Checksum as Primary Key

## Status
Accepted

## Context
Each snippet needs a unique identifier. Traditional approaches use auto-incrementing integers. We considered content-addressing using a cryptographic hash instead.

## Decision
Each snippet is identified by the **SHA256 hash** of its normalized code content.

## Rationale
- **Content-addressable:** The same code always produces the same key, regardless of when or where it was added.
- **Natural deduplication:** Two users importing the same function will get the same key — no duplicates, no conflicts.
- **Stable across databases:** Merging databases from different machines is straightforward; identical snippets share the same key.
- **No collision risk:** SHA256's 256-bit output makes accidental collisions astronomically unlikely.

## Consequences
- If code changes, the checksum (and thus primary key) changes. The `SnippetVersion` model tracks history.
- Checksums are long (64 hex chars). The CLI supports prefix matching for convenience.
- Renaming (adding/removing alias names) does not change the checksum since names are metadata, not content.
