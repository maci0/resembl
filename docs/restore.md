# Restore runbook

## What state exists

| State | Location | Backed up? | Rebuildable? |
| --- | --- | --- | --- |
| Snippet corpus (`snippet`, `collection`, `snippetversion` tables) | SQLite file named by `RESEMBL_DATABASE_URL` / `DATABASE_URL`, default `assembly.db`, or, on a DuckDB backend, the `duckdb:///file.db` file | Yes (SQLite): `resembl backup DIR` (online backup API, single file). DuckDB and other non-SQLite backends: use the backend's own tooling (see below); `resembl backup` refuses non-SQLite URLs | No, this *is* the corpus. `export` to `.asm` files is a second, human-readable copy |
| LSH index (`lsh_bucket`, `lsh_meta`) + fingerprint stamps (`app_meta`) | Same file as the corpus (SQLite or DuckDB) | Yes (SQLite): included in the backup copy; DuckDB/server backends copy with the corpus via the backend's own tooling | Yes: `reindex` / next `find` rebuilds from snippet code + fingerprints |
| Config (`config.toml`) | `~/.config/resembl/` (or `$RESEMBL_CONFIG_DIR`) | Yes (SQLite): copied verbatim to `<backup-stem>.config.toml` beside `assembly-<stamp>.db`, SHA-256 in the manifest as `config_sha256`, cross-checked by `verify-backup` | Regenerable: defaults are sane; when no config existed at backup time the manifest records `config_sha256: null` and no sidecar is written |
| Legacy pickle cache, port file | `~/.cache/resembl/` | **No** | Yes: cache, never loaded; port file rewritten by `serve` |
| Export files + export manifest | Export directory chosen at `export` time (`DIRECTORY/.resembl-export.json`) | **No** (the export itself is the copy) | Yes: re-export from restored corpus; manifest rewritten by next `export` |
| Runtime manifest (`RESEMBL_RUNTIME_MANIFEST`) | Path named by `RESEMBL_RUNTIME_MANIFEST`, required by `serve` for `/find-pinned` | Yes (SQLite): copied verbatim to `<backup-stem>.runtime-manifest.json` beside `assembly-<stamp>.db`, SHA-256 in the manifest as `runtime_sha256`, cross-checked by `verify-backup` | Regenerable only by regenerating hashes of interpreter + loaded modules; copy + SHA-256 (`artifact_sha256`) with backup set, write-protected, writable manifest can attest tampered code |

Non-SQLite backends (PostgreSQL/MySQL/DuckDB) are **not** covered by
`resembl backup`: use that backend's own tooling (`pg_dump`, etc.) and test
its restore the same way. DuckDB is file-backed, so a file copy is a
viable backup, but never a live copy: stop every DuckDB process first. A
DuckDB 2.0 file is not readable by DuckDB before 2.0, so keep the copy
with the DuckDB version that wrote it and prove the restore by opening
the copy with that version (see `docs/custom_database.md`).


## Scheduled backups and restore drills

Backups taken by `resembl backup` and the pre-delete/pre-merge backups in
`.resembl-pre-delete-backups/` are **not** committed to git: the repository
`.gitignore` ignores `.resembl-pre-delete-backups/` and database files, and
`git check-ignore` must report both ignored. A backup that re-enters the
tree re-enters the build context and ships corpus data and its config with
the repo. Schedule the operator backup explicitly: `make backup
BACKUP_DIR=<off-host-dir>` (or cron-wrap `resembl backup <off-host-dir>
--force`) and keep that directory on different storage from
`assembly.db` (see Failure-domain notes below). The make target fails
loudly when `BACKUP_DIR` is unset, when the backup exits non-zero, or
when the published copy fails verification, so a cron job that never
ran is visible instead of a silent gap in the restore points.

A backup that has never been restored is a hypothesis: run a restore drill
at least quarterly with `make backup-drill BACKUP_DIR=<off-host-dir>`:
it copies the newest verified backup to a scratch path, `verify-backup`
proves it there, and records (date, backup filename, verify result) in
`<off-host-dir>/.restore-drill.log` beside the backup directory.
`make backup-restore-drill BACKUP_DIR=<off-host-dir>` proves the full
`resembl restore` path on a throwaway live path.  The drill
walks newest-first by the timestamp embedded in the filename (the same
order `db_backup` retains and prunes by, `_db_backup_candidates`), and
proves the newest backup that *passes* verify-backup,
so a corrupt newest copy cannot block the drill forever while an older
good copy sits unproven.  `db_backup` re-verifies a reused retained
backup and refuses a copy with a `-wal`/`-shm` sidecar, but only the drill
proves the restore procedure itself.  The drill copies NLP backup,
manifest, and sidecars under their original names so `verify-backup`
cross-checks the manifest and config hash rather than reporting "no
readable manifest".

`make backup-drill` covers SQLite `assembly-*.db` copies only.  For DuckDB,
PostgreSQL, or MySQL there is no `resembl backup` to drill: stop every
DuckDB process, copy the `duckdb:///file.db` file to scratch, and open the
copy with the DuckDB version that wrote it (a DuckDB 2.0 file is not
readable before 2.0), see `docs/custom_database.md`.  For PostgreSQL use
`pg_dump`/`pg_restore` and for MySQL `mysqldump`/`mysql` on the same
scratch-copy-then-open pattern.  A drill that only ever ran against SQLite
proves nothing about those backends; record the backend-specific drill
result in the same `.restore-drill.log` so the gap is visible.
## RPO / RTO

- **RPO** = time since the last *verified* backup. No backup job runs
  automatically; `resembl backup` and `resembl verify-backup` are manual /
  cron-wrapped by the operator. Run `backup` after any bulk import, merge,
  or reindex.
- **RTO** = copy the backup back + `verify` (minutes for small corpora;
  a stale index rebuilds on the next `find`, which can dominate on
  multi-million-row corpora).

## Write durability (power loss / OS crash)

The SQLite engine runs WAL with `synchronous=NORMAL`
(`resembl/database.py`). That is durable across a process crash, but an
OS/power loss can lose the last committed transactions: a write the
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
#    DATABASE_URL (default: assembly.db in the working directory); every
#    path below is relative to that file, not necessarily "assembly.db".
DB=<live-db-path>   # e.g. /srv/resembl/assembly.db
# 2. Pick the newest backup that verifies:
resembl verify-backup backups/assembly-<stamp>.db

# 3. Restore it (verifies first, atomic single-file copy, removes stale
#    -wal/-shm, then proves the restored database with `verify`):
resembl restore backups/assembly-<stamp>.db --force
# ...or to a non-default path:
resembl restore backups/assembly-<stamp>.db --dest "$DB" --force
# Record the drill (date, backup filename, verify result) so the next
# operator knows restore was last proven; without that record the backup
# is a hypothesis. Re-run this restore at least quarterly and after any
# change to the database URL, backend, or backup tooling.
```

Restore copies through an exclusively created private temporary file in the
destination directory, keeping its descriptor open for the copy before the
atomic rename. A link planted at the former predictable temporary name is not
opened or removed.

Restore `config.toml` from the sidecar written beside the backup
(`<backup-stem>.config.toml`, mode `0600`).  When the manifest records
`config_sha256: null` there was no config at backup time; defaults
apply, or re-apply overrides with `resembl config set`.  The sidecar's
hash is cross-checked by `verify-backup`; a missing or mismatched
sidecar fails verification.

Restore the runtime manifest the same way: copy the stored
`RESEMBL_RUNTIME_MANIFEST` file back to the path `serve` reads, keep it
write-protected, and confirm its SHA-256 still equals the
`artifact_sha256` pinned by the Go service (`-artifact-sha256`).
`/find-pinned` refuses to answer until manifest, index digest, and
process generation all match; a missing or mismatched manifest is a
fail-closed `503`, not a silent unpinned find.  The manifest **is**
copied by `resembl backup` to `<backup-stem>.runtime-manifest.json`
(mode `0600`, SHA-256 in the manifest as `runtime_sha256`, cross-checked
by `verify-backup`); a backup taken before that sidecar existed leaves
`/find-pinned` unrestorable even though the corpus restores cleanly, so
re-take it rather than restoring an old copy alone.

## Restore (logical corruption: bad code wrote bad data for hours)

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
resembl verify-backup backups/assembly-<newest>.db --max-age-days <RPO days>
# (--max-age-days fails a backup whose manifest created_at is older than
# the bound, so a directory whose newest copy silently ages past the RPO
# target does not pass on contents alone.)
```

Alert on: non-zero exit, a zero-byte `assembly-*.db`, a config mismatch (missing or tampered `.config.toml` sidecar), a `-wal`/`-shm` sidecar beside a backup (a restore copies only the `.db`, so such a backup cannot be trusted), a `--max-age-days` stale-backup failure, or a backup directory
whose newest file is older than your RPO target. A missing manifest is an
issue (contents still verify); a count mismatch, content-hash mismatch,
config mismatch, `integrity_check` failure, missing table, or wal sidecar is a hard failure.

Before any destructive operation in the next section, run the backup and
`verify-backup` on the result: a backup that has never been verified is
not a pre-delete snapshot. `resembl backup` exits 1 when the published
copy fails verification or its manifest could not be written
(`manifest_written: false` / `verified: false` in JSON output), or when
the backup directory shares a volume with the live database
(`shared_volume: true`, see Failure-domain notes below); treat
any non-zero exit, or `verified: false`, as "no backup was taken" and
do not proceed:

## Deletion paths

`resembl rm`, `collection delete`, and `clean` now take a verified
pre-delete backup with `db_backup_for_delete` (SQLite only; on other
backends the ``ValueError`` names the backend and the deletion is
refused). They are destructive with no soft-delete window, so the
backup is the soft-delete window: only proceed when it verifies.
`resembl merge` takes the same pre-merge backup, since a merge rewrites
names, tags, collections, and fingerprint stamps in place and a bad
source can corrupt the corpus as surely as a delete can:

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
`db_backup` logs a warning when the backup directory shares the live
database's filesystem device (`st_dev`), so a co-located backup is a
visible, accepted risk rather than a silent downgrade; a backup that
stays on the same device must be paired with an off-host copy to cover
instance loss.
