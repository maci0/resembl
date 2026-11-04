# Restore runbook

## What state exists

| State | Location | Backed up? | Rebuildable? |
| --- | --- | --- | --- |
| Snippet corpus (`snippet`, `collection`, `snippetversion` tables) | SQLite file named by `RESEMBL_DATABASE_URL` / `DATABASE_URL`, default `assembly.db` | Yes — `resembl backup DIR` (online backup API, single file) | No — this *is* the corpus. `export` to `.asm` files is a second, human-readable copy |
| LSH index (`lsh_bucket`, `lsh_meta`) + fingerprint stamps (`app_meta`) | Same SQLite file | Yes — included in the backup copy | Yes — `reindex` / next `find` rebuilds from snippet code + fingerprints |
| Config (`config.toml`) | `~/.config/resembl/` (or `$RESEMBL_CONFIG_DIR`) | **No** | Regenerable — defaults are sane; re-apply overrides with `resembl config set` |
| Legacy pickle cache, port file, export manifest | `~/.cache/resembl/` | **No** | Yes — cache, never loaded; port file rewritten by `serve`; manifest rewritten by next `export` |

Non-SQLite backends (PostgreSQL/MySQL/DuckDB) are **not** covered by
`resembl backup`: use that backend's own tooling (`pg_dump`, etc.) and test
its restore the same way.

## RPO / RTO

- **RPO** = time since the last *verified* backup. No backup job runs
  automatically — `resembl backup` and `resembl verify-backup` are manual /
  cron-wrapped by the operator. Run `backup` after any bulk import, merge,
  or reindex.
- **RTO** = copy the backup back + `verify` (minutes for small corpora;
  a stale index rebuilds on the next `find`, which can dominate on
  multi-million-row corpora).

## Restore (instance loss / corrupted store)

```bash
# 1. Stop every process using the database (serve, other CLI sessions).
# 2. Pick the newest backup that verifies:
resembl verify-backup backups/assembly-<stamp>.db

# 3. Remove any stale WAL sidecars from the live file — a leftover
#    assembly.db-wal would replay old pages over the restored file.
rm -f assembly.db-wal assembly.db-shm

# 4. Copy the backup to the live path and prove it:
cp backups/assembly-<stamp>.db assembly.db
resembl verify
```

## Restore (logical corruption — bad code wrote bad data for hours)

Backups are point-in-time, not point-in-time-recovery: restore the newest
backup *before* the corruption began, verify it, and re-apply good writes.
Keep `keep` high enough (default 7) that the pre-corruption copy survives.

## Verifying backups periodically

A backup never restored is a hypothesis. Schedule:

```bash
resembl verify-backup backups/assembly-<newest>.db   # exit 1 = unrestorable
```

Alert on: non-zero exit, a zero-byte `assembly-*.db`, or a backup directory
whose newest file is older than your RPO target. A missing manifest is an
issue (contents still verify); a count mismatch or missing table is a
hard failure.

## Deletion paths

`resembl rm`, `collection delete`, `clean`, and `export`'s stale-file prune
are destructive with no soft-delete window. Take a backup first:

```bash
resembl backup DIR --force
```

## Failure-domain notes

Backups are caller-chosen and live wherever the caller puts them: if the
backup directory is on the same disk/volume as `assembly.db`, instance loss
takes both. Put the backup directory on different storage (or sync it off
host) for the backup to protect against anything beyond a deleted file.
