# Restore runbook

## What state exists

| State | Location | Backed up? | Rebuildable? |
| --- | --- | --- | --- |
| Snippet corpus (`snippet`, `collection`, `snippetversion` tables) | SQLite file named by `RESEMBL_DATABASE_URL` / `DATABASE_URL`, default `assembly.db` — or, on a DuckDB backend, the `duckdb:///file.db` file | Yes (SQLite) — `resembl backup DIR` (online backup API, single file). DuckDB and other non-SQLite backends: use the backend's own tooling (see below); `resembl backup` refuses non-SQLite URLs | No — this *is* the corpus. `export` to `.asm` files is a second, human-readable copy |
| LSH index (`lsh_bucket`, `lsh_meta`) + fingerprint stamps (`app_meta`) | Same file as the corpus (SQLite or DuckDB) | Yes (SQLite) — included in the backup copy; DuckDB/server backends copy with the corpus via the backend's own tooling | Yes — `reindex` / next `find` rebuilds from snippet code + fingerprints |
| Config (`config.toml`) | `~/.config/resembl/` (or `$RESEMBL_CONFIG_DIR`) | Yes (SQLite) — copied verbatim to `<backup-stem>.config.toml` beside `assembly-<stamp>.db`, SHA-256 in the manifest as `config_sha256`, cross-checked by `verify-backup` | Regenerable — defaults are sane; when no config existed at backup time the manifest records `config_sha256: null` and no sidecar is written |
| Legacy pickle cache, port file, export manifest | `~/.cache/resembl/` | **No** | Yes — cache, never loaded; port file rewritten by `serve`; manifest rewritten by next `export` |

Non-SQLite backends (PostgreSQL/MySQL/DuckDB) are **not** covered by
`resembl backup`: use that backend's own tooling (`pg_dump`, etc.) and test
its restore the same way. DuckDB is file-backed, so a file copy is a
viable backup — but never a live copy: stop every DuckDB process first. A
DuckDB 2.0 file is not readable by DuckDB before 2.0, so keep the copy
with the DuckDB version that wrote it and prove the restore by opening
the copy with that version (see `docs/custom_database.md`).

## RPO / RTO

- **RPO** = time since the last *verified* backup. No backup job runs
  automatically — `resembl backup` and `resembl verify-backup` are manual /
  cron-wrapped by the operator. Run `backup` after any bulk import, merge,
  or reindex.
- **RTO** = copy the backup back + `verify` (minutes for small corpora;
  a stale index rebuilds on the next `find`, which can dominate on
  multi-million-row corpora).

## Write durability (power loss / OS crash)

The SQLite engine runs WAL with `synchronous=NORMAL`
(`resembl/database.py`). That is durable across a process crash, but an
OS/power loss can lose the last committed transactions — a write the
CLI reported as done may not survive the machine going down. Treat the
backup, not the live file, as the durability boundary: run
`resembl backup` after any bulk import, merge, or reindex that you
cannot afford to lose, and accept that writes since the last verified
backup are lost in an OS-crash disaster. If that loss window matters,
the maintainer must own the trade-off of `synchronous=FULL`; this
runbook does not change it.

## Restore (instance loss / corrupted store)

```bash
# 1. Stop every process using the database (serve, other CLI sessions).
#    The live DB path is the sqlite:// URL in RESEMBL_DATABASE_URL /
#    DATABASE_URL (default: assembly.db in the working directory) — every
#    path below is relative to that file, not necessarily "assembly.db".
DB=<live-db-path>   # e.g. /srv/resembl/assembly.db
# 2. Pick the newest backup that verifies:
resembl verify-backup backups/assembly-<stamp>.db

# 3. Remove any stale WAL sidecars from the live file — a leftover
#    <live>-wal would replay old pages over the restored file.
rm -f "$DB-wal" "$DB-shm"

# 4. Copy the backup to the live path, remove any WAL sidecars the copy
#    cannot have brought with it, and prove the restored database:
cp backups/assembly-<stamp>.db "$DB"
rm -f "$DB-wal" "$DB-shm"
resembl verify
# Record the drill (date, backup filename, verify result) so the next
# operator knows restore was last proven; without that record the backup
# is a hypothesis. Re-run this restore at least quarterly and after any
# change to the database URL, backend, or backup tooling.
```

Restore `config.toml` from the sidecar written beside the backup
(`<backup-stem>.config.toml`, mode `0600`).  When the manifest records
`config_sha256: null` there was no config at backup time — defaults
apply, or re-apply overrides with `resembl config set`.  The sidecar's
hash is cross-checked by `verify-backup`; a missing or mismatched
sidecar fails verification.

## Restore (logical corruption — bad code wrote bad data for hours)

Retention is by count, not by age: a long-running corruption can flood
the window with post-corruption copies and age out the pre-corruption
one, so keep `keep` comfortably above the number of backups a bad day
can generate, and do not run `backup` again until you have identified
which copy predates the corruption. Backups are point-in-time, not
point-in-time-recovery: restore the newest
backup *before* the corruption began, verify it, and re-apply good writes.
Keep `keep` high enough (default 7) that the pre-corruption copy survives.

## Verifying backups periodically

A backup never restored is a hypothesis. Schedule (``db_backup`` now
re-verifies the published backup against its manifest and refuses to
report a copy that fails, so a *new* backup is only reported once it has
verified):

```bash
resembl verify-backup backups/assembly-<newest>.db   # exit 1 = unrestorable
```

Alert on: non-zero exit, a zero-byte `assembly-*.db`, a config mismatch (missing or tampered `.config.toml` sidecar), a `-wal`/`-shm` sidecar beside a backup (a restore copies only the `.db`, so such a backup cannot be trusted), or a backup directory
whose newest file is older than your RPO target. A missing manifest is an
issue (contents still verify); a count mismatch, content-hash mismatch,
config mismatch, `integrity_check` failure, missing table, or wal sidecar is a hard failure.

Before any destructive operation in the next section, run the backup and
`verify-backup` on the result — a backup that has never been verified is
not a pre-delete snapshot:

## Deletion paths

`resembl rm`, `collection delete`, and `clean` now take a verified
pre-delete backup with `db_backup_for_delete` (SQLite only; on other
backends the ``ValueError`` names the backend and the deletion is
refused). They are destructive with no soft-delete window, so the
backup is the soft-delete window: only proceed when it verifies:

```bash
resembl backup DIR --force
resembl verify-backup DIR/assembly-<stamp>.db
```

Only proceed when `verify-backup` exits 0; for `clean`, the LSH index is
rebuildable, so the pre-clean backup protects the corpus it vacuums.
Pass `--no-backup` to opt out. `export`'s stale-file prune is covered
by the export itself as its own pre-image; verify the export manifest
before relying on it to restore.

## Failure-domain notes

Backups are caller-chosen and live wherever the caller puts them: if the
backup directory is on the same disk/volume as `assembly.db`, instance loss
takes both. Put the backup directory on different storage (or sync it off
host) for the backup to protect against anything beyond a deleted file.
