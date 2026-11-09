# Security policy

Last reviewed: 2026-11-09, against the tree at version 4.0.0
(`pyproject.toml:3`) plus post-4.0.0 commits through `bebc0fd` (including `5303616` env vars, `91c7aa1` cache partition, `d982e94` mkstemp, `8e078ed` config lock, `da86484` merge/collection force, `fb78133` non-loopback `/metrics` gate, `de205f7` runtime-manifest fail-fast, `cf2a276` terminal/error-detail sanitization, `2d7bc3f` handler-rejection counter, `5662563` config/clock/seed fail-fast, `c6a76b3` Go 429, `e2d4500` ASCII-only protocol-token folding), with the
loopback-client fix (the `build_opener(ProxyHandler({}))` opener opened
via `OpenerDirector.open` instead of passed as `urlopen`'s context,
`resembl/cli.py` `_server_request` and `resembl/find_client.py` `_main`)
re-verified on that date. This review
covers the Python surfaces and the
experimental Go service in `service/`; the threat-model boundaries B9 and
risks R13-R18 in `docs/THREAT_MODEL.md` record the Go surface (including
the Lambda event adapter in `service/internal/lambdaproxy/`) and the
`backup` / `verify-backup` commands.

The `serve` controls below (the loopback `Host` check, the non-loopback
bind refusal without `--allow-unauthenticated` or a qualifying `RESEMBL_SERVE_ALLOW_UNAUTHENTICATED` value (not blank and not `0`/`false`/`no`/`off`, case-insensitive, `resembl/paths.py` `serve_allow_unauthenticated_get`), the Go service's
`-token-file` requirement for a non-loopback `-listen`, `serve -v` request
logging, the `0700` cache directory, the `0600` port file, the `0600`
config file on save, the standalone client's 1-65535 port-range check and
the in-process client's 1024-65535 check, the loopback proxy bypass and
the 16 MiB bounded client response reads, the log-path control-character
stripping on both servers,
the 1000-row `top_n` cap, the 96 handler-thread bound in `resembl/server.py`, the
`413` envelope for oversized bodies in `resembl/server.py` `_read_body`,
and the
JSON `405` envelope for any unhandled verb, the verified pre-delete backup
before `rm`/`clean`/`collection delete` and the verified pre-merge backup
before `merge` (`db_backup_for_delete`, `resembl/core.py`, with `--no-backup`
to opt out), the single-file backup publication (`3d77755`, `db_backup` in
`resembl/core.py`), the end-to-end `X-Request-Id` adoption (bounded to 64 UTF-8 bytes,
printable-only; `26e95cf`, byte-counted in `ef333e8`), the fail-fast
`RESEMBL_NOW`/`RESEMBL_SEED` validation on read and startup paths (base-10
only), the
warm-server refusal log line before the in-process fallback, the `pool_pre_ping`/`pool_timeout` pooled-connection health check (`7ff2128`, `create_db_engine` in `resembl/database.py`), the terminal output sanitization (`cf2a276`, `_display` in `resembl/cli.py`), and the Go
service's graceful drain on SIGINT/SIGTERM) are documented in `CHANGELOG.md`.

## Reporting a vulnerability

The intended channel is GitHub's private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**. That
opens a private advisory visible only to the reporter and the maintainers,
which is the right channel for a working exploit or a proof-of-concept.

That option is not currently available. Private vulnerability reporting is
not enabled on this repository, so the Security tab does not offer it. Until
it is enabled, the only channel that reaches a maintainer is the public issue
tracker, which is the wrong channel for an unfixed vulnerability: an issue
there is visible to everyone from the moment it is opened. A reporter who
has a working exploit should therefore hold it until a maintainer contact
address is published here, and not open the issue in the meantime.

The repository is <https://github.com/maci0/resembl>.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 4.x (current release line) | yes |
| 3.x and earlier | no |

The current release is 4.0.0. Older lines receive no fixes; upgrade to the
current line before reporting an issue against them.

## Threat model

`docs/THREAT_MODEL.md` records the entry points, trust boundaries, assets,
and ranked risks for the code as it stands. Read it before reporting: it
names the mitigations that already exist, so a report of a
missing control is far more useful than a report of a known gap.

The one gap to be aware of while using `resembl serve` is that
`POST /find` is unauthenticated: it answers any caller that can reach the
port, so the corpus is readable a page at a time. A single response is
capped at 1000 rows, but a sequence of requests is not. On a loopback bind
that is a concern for other processes on the same host, and on a
non-loopback bind it is the same exposure as the bind itself. Run
`resembl serve -v` if the served queries have to be attributable after the
fact; nothing is recorded per request at the default log level except a
slow-request warning (over 1s) and exception logs. A caller-supplied
`X-Request-Id` is adopted end to end (bounded to 64 UTF-8 bytes,
printable-only), so a request carrying one can be pivoted from the Go
service's log line to the engine's records, without adding a
default-level per-request record.

## Deployment assumptions

resembl is a single-user local tool. It has no authentication, no
authorization, and no multi-tenancy. The following are assumptions the code
relies on, not configuration options.

- **`resembl serve` stays on loopback.** It binds `127.0.0.1` by default and
  answers every request that reaches the port. Passing `--host` with a
  non-loopback address is refused unless `--allow-unauthenticated` or a
  a `RESEMBL_SERVE_ALLOW_UNAUTHENTICATED` value that is not blank and not `0`/`false`/`no`/`off` (case-insensitive) is given; with either it binds and publishes an unauthenticated read of
  the whole snippet corpus to that network, printing a warning that does
  not stop the bind. Do not put it behind a reverse proxy that does not
  authenticate. The Go `resembl-service` likewise requires `-token-file`
  for a non-loopback `-listen`.
- **The cache directory is private to the user.** The port file in it is
  read as an unauthenticated port number by every `find` client, which then
  dials that port on `127.0.0.1`. The standalone client `resembl-find`
  refuses a port outside 1-65535 before dialing, so a planted port that
  cannot be bound is not dialed; the in-process `find` client refuses a
  port outside 1024-65535. A forged port inside the admitted range, and
  the in-process client's low-port exclusion of a legitimately bound
  low port, are still unchecked.
  `resembl serve` creates that directory with
  mode `0700` and the port file with mode `0600`, so a directory the server
  made itself is not writable by anyone else. Nothing re-checks the mode, so
  a cache directory created earlier by an operator's own `mkdir`, by another
  tool, or with a different umask keeps that mode, and a shared or
  world-writable one lets another local user point queries at a listener of
  their own. The config directory (`RESEMBL_CONFIG_DIR`, else
  `$XDG_CONFIG_HOME/resembl`, else `~/.config/resembl`) is a separate directory
  with the same
  requirement: it supplies thresholds and match settings, is written
  mode `0600` on save, and the tool does
  not report that it is running under a non-default configuration.
- **`DATABASE_URL` is trusted verbatim.** `RESEMBL_DATABASE_URL` takes
  precedence over it, and either may carry a password and name a remote
  host. Keep it in the process environment, not in a shell profile or a
  shared dotfile.
- **Imported directories are the operator's own.** `resembl import` walks a
  directory, keeps every `.asm`/`.txt` path in memory, and runs a process
  pool over the set (by default one worker per ~100 files, capped at the CPU
  count; `--jobs` sets it). There is no file-count or file-size cap, so a
  large or hostile tree costs the machine memory and CPU, and the
  confirmation prompt is not a bound.
- **Merge sources are trusted.** `resembl merge` inserts another database's
  snippets, names, tags, and collections into the local corpus. Point it only
  at databases you control. It first takes a verified pre-merge backup
  (default to `.resembl-pre-delete-backups/`, `db_backup_for_delete` in
  `resembl/core.py`, aborting with exit 1 on failure, `--no-backup` to opt
  out), so a bad merge is restorable; the backup does not authenticate the
  source, and is SQLite-only and forgeable like any backup directory
  (THREAT_MODEL.md R17). Its one closed edge is fingerprint
  deserialization: blobs without the `RMLH` magic are rejected and
  recomputed rather than unpickled. The failure path is also closed: a
  source that will not open makes `merge` log the driver's error text and
  return only a stable message naming the masked source, so host, port,
  file-path, and SQL detail no longer reach the caller. `merge` takes a
  verified pre-merge backup first (`db_backup_for_delete` in
  `resembl/core.py`, with `--no-backup` to opt out); a backup that does not
  verify aborts the merge with exit 1.
- **The Lambda engine address is trusted verbatim.** The Lambda bootstrap (`service/cmd/resembl-lambda/main.go`) reads `RESEMBL_ENGINE_URL` once surrounding whitespace is trimmed, treating a blank value as unset (default `http://127.0.0.1:8081`), and hands it to the loopback-only engine adapter. Keep that environment private to the deployment; a hostile value points the adapter at an attacker-chosen loopback listener once an authorizer is supplied (THREAT_MODEL.md R18). Today the bootstrap is deny-all, so no query is dispatched.
- **Backup directories are trusted.** `resembl backup` writes a full copy of
  the SQLite corpus plus a per-backup manifest, a `<backup-stem>.config.toml`
  config sidecar, and a `<backup-stem>.runtime-manifest.json` runtime
  sidecar (copied verbatim from `RESEMBL_RUNTIME_MANIFEST` when set) into
  a caller-chosen directory, pruning to the newest `--keep` (default 7)
  by the embedded filename timestamp, folds the published copy to a single-file restore
  point, and re-verifies the published file before reporting success
  (removing it on failure; exits 1 when the manifest could not be written
  or the published copy fails verification). The published copy is folded
  to single-file (`journal_mode=DELETE` with `wal_checkpoint(TRUNCATE)`,
  `3d77755`, `db_backup` in `resembl/core.py`) so it restores without
  `-wal`/`-shm` sidecars. A retained backup is
  re-verified before reuse and replaced by a fresh self-verified copy
  when it no longer verifies. `verify-backup`
  checks tables, runs `PRAGMA integrity_check`, fails on a `-wal`/`-shm`
  sidecar beside the backup, compares the snippet
  count and content hash (which includes the `minhash` payload) against
  the manifest, and cross-checks the config and runtime sidecars against
  their recorded `config_sha256` / `runtime_sha256`, but the manifest is
  unauthenticated: a tampered backup with an edited manifest (and
  sidecars) passes. The manifest's `config` and `runtime_manifest`
  values are plain basenames only, so a forged manifest cannot make
  `verify-backup` read/hash a file outside the backup directory
  (`4678fd1`). Keep the backup directory private (backup, manifest, and
  sidecars are mode `0600`) and treat any backup whose manifest you did
  not write as untrusted (THREAT_MODEL.md R17). Schedule the off-host
  copy with `make backup BACKUP_DIR=<off-host-dir>` and prove the newest
  restores with `make backup-drill BACKUP_DIR=<off-host-dir>`
  (`Makefile`, `docs/restore.md`); both fail loudly when `BACKUP_DIR` is
  unset.
- **Destructive commands take a verified pre-delete backup.** `resembl rm`,
  `resembl clean`, and `resembl collection delete` write a verified backup
  first (`db_backup_for_delete` in `resembl/core.py`, default to
  `.resembl-pre-delete-backups/` beside the working directory); a backup
  that does not verify aborts the deletion with exit 1. The backup is
  SQLite-only: on other backends the deletion is refused unless
  `--no-backup` is passed, which removes the restore point entirely. The
  pre-delete directory has the same forgeability as any backup directory
  (THREAT_MODEL.md R17): it protects against accidents and corruption, not
  against a writer to that directory.
- **Environment pins fail fast.** An unparseable `RESEMBL_NOW` or
  `RESEMBL_SEED` now raises instead of silently pinning the clock or
  falling back to OS randomness, on read and retry paths as well as on
  stamping writes; the Go service validates both at startup and refuses
  to start. `RESEMBL_NOW` without an offset is UTC, never host local zone
  (`5d2aaa3`). `RESEMBL_SEED` is parsed as decimal only, and `MinHash`
  rejects `2**32` permutations (`9aeab2b`, `1f210e7`). A malformed or
  readable `config.toml` fails `serve` and every CLI command startup instead of
  the server silently binding with built-in defaults (`_find_defaults_load`
  in `resembl/server.py` via `_read_config_dict` in `resembl/config.py`,
  `ConfigError`, and the CLI callback's `_read_config_dict` gate);
  library `load_config` keeps the report-and-default behavior. `pinned_snapshot.index_digest` raises
  `ValueError` on a stored non-finite `REAL` instead of leaking a
  `json.dumps` encoder message (`_snapshot_value` in
  `resembl/pinned_snapshot.py`).
- Go service and Lambda adapter: see docs/THREAT_MODEL.md B9 and R13-R18. Experimental, deny-all by default, with the Go `resembl-service` draining in-flight queries on SIGINT/SIGTERM (30s bound) and retiring pooled engine connections on shutdown (`CloseIdleConnections`) instead of being killed mid-request, so a stopped service no longer orphans custody slots and engine calls (the Lambda bootstrap has no token path at all), single shared bearer token where a token is supplied, no per-caller audit, rotation by restart, engine must stay on loopback (`RESEMBL_ENGINE_URL` is trusted verbatim by the Lambda bootstrap), runtime manifest must be write-protected, custody slots need operator repair after uncertain outcome.
