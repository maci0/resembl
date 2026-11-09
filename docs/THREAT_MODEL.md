# resembl threat model

Last reviewed: 2026-11-09, against the code at version 4.0.0
(`pyproject.toml:3`) plus the post-4.0.0 commits on `main` through
`e85b123`, with the loopback-client fix (the opener built by
`build_opener(ProxyHandler({}))` now opened via `OpenerDirector.open`
instead of being passed as `urlopen`'s context — `599923a`,
`resembl/cli.py` `_server_request` and `resembl/find_client.py`
`_main`, and the pinning tests in `tests/test_cli_extended.py`
`TestServerFallback` retargeted to mock `build_opener`) re-verified
on that date. Every reference below was re-read in that tree on that
date, except where a line number is explicitly historical. The
`file:line` references are historical: later releases moved the
code, so find a cited function by name before relying on a line number. The
model has been re-reviewed for this release; the Go experimental service in
`service/` (see `service/README.md`) is in scope alongside the Python
surfaces, and the `backup` / `verify-backup` commands (`db_backup` in
`resembl/core.py`, `db_verify_backup` in `resembl/core.py`) are now modeled
below (R17, assets).

This pass re-checked the unimplemented-verb and threading claims against
`resembl/server.py` (`send_error`, `__getattr__`, `_MAX_HANDLER_THREADS`)
and modeled the Lambda event adapter (`service/internal/lambdaproxy/`,
R18); the corrections land in B1, B2, B8, B9 and Mitigations below. A
follow-up pass on the same date re-checked the backup, LSH-metadata, CSV/YARA
export, and Go engine-adapter paths added after that review: `db_backup`
now re-verifies before reporting success, `lsh_meta_get` fails instead of
rebuilding on a transient lock, the CSV guard covers Unicode leading
characters, the YARA escape covers code points above U+00FF, and the Go
adapter surfaces engine error detail in the operator log only. Those land in
R17, B2, B9, and Mitigations below. A 2026-11-09 pass re-checked the post-4.0.0
surface: `db_merge` now masks credentials in its source-URL error path
(`0afeba7`, `db_merge` in `resembl/core.py`) which corrects R6 and SECURITY.md
below; `serve` now answers `413` for oversized bodies (`2cd23c9`,
`resembl/server.py` `_read_body`); `snippet_version_list`
(`ec7a77d`, `resembl/core.py`), the LSH-metadata streaming in
`resembl/models.py`, and the similarity sample in `resembl/diagnostics.py`
now stream / select only needed columns (`ec7a77d`); and `cfg_similarity`
(`8f9e69c`, `resembl/scoring.py`) rejects non-integer counts. The
`file:line` audit this pass found two out-of-range references
(`resembl/scoring.py:1315`, twice) and a cluster of stale line numbers in
`resembl/server.py` / `resembl/core.py` / `resembl/cli.py`; those are corrected
where this pass touched them and otherwise left historical per the note above.
A later 2026-11-09 pass re-checked the commits after `f896006`: `db_merge`
no longer returns raw driver/SQL error text to callers, only a stable
message naming the masked source (`c1eb6cb`, `db_merge` in
`resembl/core.py`, logged at `logger.exception`), which closes the B6
failure-path leak; the port-file name now hashes the database URL with
SHA-256 instead of SHA-1 (`c1eb6cb`, `server_port_path` in
`resembl/paths.py`), which corrects R9 below; `db_backup` now also copies
the live `config.toml` to a `<backup-stem>.config.toml` sidecar (mode
`0600`) and records its SHA-256 as `config_sha256` in the manifest,
with `verify-backup` cross-checking that sidecar (`8616290`, `db_backup`
and `db_verify_backup` in `resembl/core.py`), which extends R17; and a
failed `COMMIT` on the pinned path now answers `503 pinned index
unavailable` instead of `200` (`1e1fa33`, `_handle_find_pinned` in
`resembl/server.py`). The Go engine adapter now honors the caller's
per-request timeout instead of a hardcoded 30s (`1e1fa33`,
`NewLocalEngineWithTimeout` in `service/http_engine.go`,
`NewPinnedSQLiteEngineWithTimeout` in `service/pinned_engine.go`),
closing the gap where a handler could outlive its custody claim.
A later 2026-11-09 pass re-checked the commits after `5f9337c`: the
standalone client `resembl-find` now refuses a port-file port outside
the ephemeral range 1024-65535 before dialing it (`6273fa0`,
`_main` in `resembl/find_client.py`), so a planted port naming a
privileged service (22, 3306, 6379) is no longer dialed and its
response exfiltrated as find results, which narrows R3 but does not
close it; the result-cache key now renders the engine URL with the
password hidden (`6273fa0`, `_find_one` in `resembl/server.py`,
`render_as_string(hide_password=True)`), correcting the B7 wording
below; `save_config` now writes the config file mode `0600`
(`6273fa0`, `save_config` in `resembl/config.py`); the Lambda
bootstrap now trims surrounding whitespace from `RESEMBL_ENGINE_URL`
and treats a blank value as unset (`6963a53`,
`service/cmd/resembl-lambda/main.go`, `strings.TrimSpace`), so a
blank environment value no longer becomes an opaque engine-address
error; and the platform-separator normalization of a home-expanded
config path (`3a9c4e1`, `_expand_home` in `resembl/paths.py`) is a
portability fix with no trust change.
A later 2026-11-09 pass re-checked the commits after `1c03f6f`: `resembl serve`
now refuses a non-loopback `--host` unless `--allow-unauthenticated` is given
(`6cbddc6`, `serve` in `resembl/cli.py`), and the Go `resembl-service`
likewise now requires `-token-file` for a non-loopback `-listen`
(`6cbddc6`, `isLoopbackListen` in `service/cmd/resembl-service/main.go`),
which corrects R2 below; `db_verify_backup` now fails a backup with a
`-wal`/`-shm` sidecar beside it so a single-file restore cannot verify a
copy that restores empty (`e95b19d`, `db_verify_backup` in
`resembl/core.py`), and `db_backup` now re-verifies a retained backup
before reusing it and writes a fresh self-verified copy instead of
reporting success for a corrupt one (`0e258ed`, `db_backup` in
`resembl/core.py`), which extends R17; and the Lambda adapter now strips
the body from `HEAD` answers (`6dfaa9b`, `service/internal/lambdaproxy/proxy.go`).
A later 2026-11-09 pass re-checked the commits after `6cbddc6`: the
standalone client `resembl-find` now admits only ports 1-65535 (`6406aee`,
`_main` in `resembl/find_client.py`), so a legitimately bound low port
(e.g. `serve --port 80`) is dialable there, while the in-process `find`
client guards the narrower ephemeral 1024-65535 range (`cdabd39`,
`_server_request` in `resembl/cli.py`), which corrects R3, B5, and the
query-text-capture abuse case below; both loopback clients now bypass
`http_proxy` via `ProxyHandler({})` (`456e464`, `resembl/cli.py`,
`resembl/find_client.py`) and bound the response body they read at 16 MiB
(`709d31b`, `_MAX_RESPONSE_BYTES` / `_MAX_SERVER_RESPONSE_BYTES`); `serve`'s
loopback guard now uses the full `127.0.0.0/8` test via `ipaddress`
(`709d31b`, `_is_loopback_bind` in `resembl/cli.py`); both servers strip
control characters from logged request paths/methods (`709d31b`,
`_LOG_CONTROL_CHARS` in `resembl/server.py`, `safeLogPath` in
`service/service.go`); the handler-slot wait is bounded (`7c10336`,
`_HANDLER_SLOT_TIMEOUT_SECONDS` in `resembl/server.py`) and the
result cache is released on server close and invalidated by the
database version counter (`7c10336`, `_result_cache_release` and
`_db_version` in `resembl/server.py`; the database-file
fingerprint pair that accompanied it was removed in `d190380` as
ineffective — see below); and the on-disk JSON
manifest readers skip deeply nested manifests (`99c4f3a`,
`_export_manifest_read` / `_db_backup_reuse` / `db_verify_backup` in
`resembl/core.py`). Those land in R3, B1, B2, B5, and Mitigations below.
A later 2026-11-09 pass re-checked the commits after `db64fab`:
`rm`, `clean`, and `collection delete` now take a verified pre-delete
backup (`db_backup_for_delete` in `resembl/core.py`), which a failed
backup aborts before deletion (`8dcee74`, `resembl/cli.py` `rm` /
`clean` / `collection_delete_cmd`); an unparseable `RESEMBL_NOW` or
`RESEMBL_SEED` now fails fast on the read/retry paths too, not only on
stamping writes (`ed0705b`, `resembl/models.py` `_retry_sleep`,
`service/service.go` `New` and `isISO8601Instant`); `parse_instant`
treats ISO `-00:00` as not-an-instant (`9d6f2f0`,
`resembl/models.py`); the CLI warm-server client logs a refusal that
arrives as a `200 {"error": ...}` envelope before falling back
(`1f06887`, `resembl/cli.py` `_server_request`); the Go
`resembl-service` drains in-flight queries on SIGINT/SIGTERM
(`1f06887`, `service/cmd/resembl-service/main.go`); and
`db_verify_backup` caps the `integrity_check` read at three rows
(`eed3273`, `resembl/core.py`). Those land in R17, B2, B3, B8, B9,
Assets, and Mitigations below.
A later 2026-11-09 pass re-checked the commits after `9d6f2f0`:
`merge` now takes the same verified pre-merge backup as the
destructive commands (`a4dba24`, `db_backup_for_delete` in
`resembl/core.py`, `resembl/cli.py` `merge`, with `--force`,
`--backup-dir`, and `--no-backup`; a failed backup aborts with
exit 1 before the merge), and the `backup` CLI now also exits 1
when the published copy fails verification, not only when its
manifest could not be written (`a4dba24`, `resembl/cli.py` `backup`);
`db_backup` now folds the published copy to a single-file restore
point (`3d77755`, `PRAGMA journal_mode=DELETE` and
`wal_checkpoint(TRUNCATE)` in `resembl/core.py` `db_backup`), so it
verifies on its own without a `-wal`/`-shm` sidecar; the result
cache's database-file fingerprint pair is removed (`d190380`,
`resembl/server.py` `_db_version` now returns the bare
`data_version` integer — the pair never caught a replaced file, so
a replaced database file under a running server is now documented
as requiring a restart), and the scheme-id cache is removed
(`d190380`, `_scheme_id` in `resembl/server.py` is uncached,
`_SCHEME_CACHE_MAX = 0`); the Go service now propagates a
per-request correlation id to the engine (`26e95cf`,
`service/service.go` mints it and `service/http_engine.go`
`LocalEngine` forwards it as `X-Request-Id`; the Python server
adopts a bounded, printable caller-supplied `X-Request-Id` and
echoes it, `_correlation_id` in `resembl/server.py`), which is the
investigative path `serve -v` and the Go operator log share;
`minhash_pack` now rejects permutation counts outside 2..4096 at
pack time (`1f210e7`, `resembl/scoring.py` `minhash_pack`), and
`MinHash.__init__` rejects `num_perm >= 2**32` and out-of-range
`hashvalues` (`9aeab2b`, `resembl/minhash.py`); `RESEMBL_SEED` is
now parsed as decimal in both the Python sampler and the Go
service (`9aeab2b`, `resembl/diagnostics.py` `sample_rng` base-10
`int`, `service/service.go` `New` and `newRequestID` base-10
`ParseInt`), so hex/octal spellings fail fast; the YARA escape also
escapes C1 controls U+0080-U+009F (`429f11d`, `_yara_string_escape`
in `resembl/core.py`); the Go `resembl-service` retires pooled
engine connections on shutdown via `CloseIdleConnections`
(`9aacafb`, `service/cmd/resembl-service/main.go`); `ResemblLSH`
`insert`/`insert_batch` skip fingerprints `minhash_pack` rejects
(`resembl/lsh.py`); the service container image now ships `LICENSE`
and `NOTICE` at `/licenses/` (`7ea24f8`, `service/Dockerfile`,
`service/.dockerignore`); and the CSV formula
guard now strips interleaved BOM/whitespace before the prefix
check (`e6314c0`, `_csv_safe` in `resembl/cli.py`); the backup
manifest `config`/`runtime_manifest` fields are now plain-basename
only, so a forged manifest cannot make `verify-backup` read/hash a
file outside the backup directory (`4678fd1`, `db_verify_backup` in
`resembl/core.py`); `db_backup` now also copies the live runtime
manifest (`RESEMBL_RUNTIME_MANIFEST`) verbatim to a
`<backup-stem>.runtime-manifest.json` sidecar (mode `0600`, SHA-256
as `runtime_sha256`, cross-checked by `verify-backup`, `_write_sidecar`
and `_db_backup_runtime_read` in `resembl/core.py`), selects and prunes
retained backups by modification time rather than filename
(`_db_backup_candidates` in `resembl/core.py`), and includes the
`minhash` payload in the content hash so a reindex does not make a
stale backup look current; `pinned_snapshot.index_digest` now raises
`ValueError` on a non-finite `REAL` value instead of leaking a
`json.dumps` encoder message, and serializes a `bool` as its integer
form (`_snapshot_value` in `resembl/pinned_snapshot.py`); `serve` now
fails startup on a malformed/unreadable `config.toml` instead of
binding with built-in defaults (`_find_defaults_load` in
`resembl/server.py` calls `_read_config_dict` in
`resembl/config.py`, which raises `ConfigError`); `make backup` /
`make backup-drill BACKUP_DIR=<off-host-dir>` schedule the operator
backup and prove the newest restores (`Makefile`, `docs/restore.md`);
the Lambda error path omits an empty string body for `HEAD` instead
of shipping an empty base64 body (`service/internal/lambdaproxy/proxy.go`);
`service/custody.go` `syncDir` is best-effort on Windows; and the
Go image pins `GOPROXY=off` (`service/Dockerfile`). Those land in
R4, R6, R14, R17, B1, B2, B6, B9, the env table, Assets, and Mitigations
below.

Scope: what can be attacked in resembl, and what stands in the way. Point
vulnerabilities and their fixes are not tracked here; each risk names the code
that carries it so a review pass can re-verify it.

resembl is a single-user developer tool. It has no accounts, no
multi-tenancy, and no authorization concept anywhere in the codebase. Every
"user" in this model is a human or process already holding the same OS
account as the tool, except for the loopback HTTP boundary, which is the one
place where a peer need not be the same user. The experimental Go service in
`service/` is the one exception: it is a portable HTTP boundary built to be
deployed in front of a pinned index, with an `Authorizer` interface
(`service/service.go:23`) that the application must supply. Its default
binary (`cmd/resembl-service`) and Lambda bootstrap
(`cmd/resembl-lambda/main.go`) are deny-all by default; see B9 below.

## Risk summary

Ranked by exploitability against the shipped defaults, then by impact.

| # | Risk | Boundary | Impact | Control today |
| - | ---- | -------- | ------ | ------------- |
| R1 | `POST /find` is unauthenticated and answers any local caller, so the corpus is readable a page at a time | B1 | Full corpus disclosure to any same-host process; memory pressure on the server | `top_n` capped at 1000 per request (`_MAX_TOP_N` in `resembl/server.py:662`, enforced in `_parse_find_request` at `resembl/server.py:469`, lower bound at `resembl/server.py:426`), which bounds one response but not a sequence of them; the config layer leaves the local `top_n` unbounded (`resembl/config.py:63`), and the served default is clamped to the same cap (`resembl/server.py:1950`) |
| R2 | The find server has no authentication and no rate limit; a non-loopback bind is refused unless the operator explicitly allows it | B1 | R1 plus sustained exhaustion | Non-loopback `--host` refused without `--allow-unauthenticated` (`6cbddc6`, `serve` in `resembl/cli.py`); Go `resembl-service` requires `-token-file` for a non-loopback `-listen` (`6cbddc6`, `isLoopbackListen` in `service/cmd/resembl-service/main.go`); `Host` check on a loopback bind, which answers `403` to a rebound name (`_host_allowed` in `resembl/server.py:1301`, `_loopback_allowed_hosts` in `resembl/server.py:672`); loopback default only (`resembl/cli.py:578`) |
| R3 | The port file is an unauthenticated channel: both clients trust its contents and forward the query text to whatever loopback port it names | B5 | Query text (attacker-supplied source code) sent to an attacker-controlled loopback listener | The standalone client `resembl-find` refuses a port-file port outside 1-65535 before dialing it (`6406aee`, `_main` in `resembl/find_client.py`), so a planted out-of-range value is no longer dialed; the in-process `find` client guards the narrower ephemeral range 1024-65535 (`cdabd39`, `_server_request` in `resembl/cli.py`), so a planted port naming a privileged local service (22, 3306, 6379) is no longer dialed there; a forged port inside the admitted range still reaches an attacker listener, and the cache directory the server did not create is still unchecked (`serve` in `resembl/server.py:2116` makes its own directory `0o700`) |
| R4 | Nothing is recorded per request at the default log level, so queries are unattributable unless `serve -v` is running | B1 | No way to investigate a suspected scrape or a hostile client | `serve -v` logs every request at DEBUG with its peer and outcome (`log_message`/`log_request` in `resembl/server.py:1760`); the only default-level per-request trace is a slow-request WARNING (`resembl/server.py:1790`) and exception logs on failure (`resembl/server.py:1521`). The CLI warm-server client now logs a `200 {"error": ...}` refusal at WARNING before it falls back to the in-process path (`1f06887`, `resembl/cli.py` `_server_request`), so a server that only ever refused with envelopes is no longer silent in the operator log. A per-request correlation id (`26e95cf`: Go mints it, `LocalEngine` forwards it as `X-Request-Id`, the Python server adopts a bounded printable caller-supplied id and echoes it, `_correlation_id` in `resembl/server.py`) joins a Go request to the engine's handling in the operator log, but it is a join key, not an audit trail: no per-request record exists at the default level on either server |
| R5 | `RESEMBL_DATABASE_URL` / `DATABASE_URL`, `RESEMBL_CACHE_DIR`, and `RESEMBL_CONFIG_DIR` are trusted verbatim from the environment, including credentials and a remote host | B3 | Database pointed at an attacker-chosen host; config read from an attacker-chosen directory | none, by design (`docs/adr/006-paths-module-owns-environment.md`, `resembl/paths.py:57`, `resembl/paths.py:81`, `resembl/paths.py:97`) |
| R6 | `merge` opens an arbitrary source URL, including a credentialed remote database, and inserts its rows | B6 | Rows from a hostile source enter the corpus; credentials sent to a chosen host | Fingerprints recomputed, never deserialized (`db_merge` in `resembl/core.py`); `minhash_pack` rejects permutation counts outside 2..4096 at pack time and `MinHash.__init__` rejects `num_perm >= 2**32` and out-of-range `hashvalues` (`1f210e7`, `resembl/scoring.py` `minhash_pack`; `9aeab2b`, `resembl/minhash.py`); the source URL is masked for display in the error path (`db_url_mask` in `resembl/paths.py`), and callers get only a stable message naming the masked source — driver/SQL detail stays in the operator log, not the return value (`c1eb6cb`, `db_merge` in `resembl/core.py`); a failed `db_merge` now also disposes the source engine instead of leaving its pool live; `merge` now also takes a verified pre-merge backup before merging (`a4dba24`, `db_backup_for_delete` in `resembl/core.py`, `resembl/cli.py` `merge`), aborting with exit 1 on failure unless `--no-backup` is given, so a bad source that corrupts in place can be undone — but the backup is SQLite-only and forgeable like R17 |
| R7 | `import` holds every matching path in memory and spawns a worker process per CPU over the set, and `reindex` does the same over every row of the corpus, with no count or size cap on either | B2 | Process, memory, and CPU exhaustion from a hostile directory | Chunked database writes only (`resembl/cli.py:1010`, `resembl/cli.py:1049`) |
| R8 | The in-process result cache keeps the 128 most recent find results resident | B1 | Snippet checksums and names (the result payload) sit in process memory beyond the request | 128-entry LRU whose key is a SHA-256 digest of the query, so the query text itself is not retained (`resembl/server.py:618`) |
| R9 | The port-file name is a 12-hex-digit SHA-256 of the raw, unmasked database URL | B5, B7 | A truncated hash of a credentialed URL is written to disk under the cache dir | none (`server_port_path` in `resembl/paths.py`, upgraded from SHA-1 in `c1eb6cb`; stale SHA-1 files are ignored) |
| R10 | `find-batch --file` reads the whole query file into a list before chunking it into requests | B2 | Memory exhaustion from an oversized input file | Chunking on the wire only (`resembl/cli.py:1571`, `resembl/cli.py:526`) |
| R11 | A release tag is not statically analyzed or dependency-reviewed when it is cut; only the SBOM runs on the tag | B8 | A tagged release ships whatever the tag's tree contains, with no pre-release SAST or CVE gate on the tag itself | SAST on push and pull request to `main` plus a weekly cron, Python and Go (`.github/workflows/codeql.yml:4`), dependency review on pull requests to `main` only (`.github/workflows/dependency-review.yml:11`), SBOM on `main` and on `v*` (`.github/workflows/sbom.yml:9`); `service.yml` runs `gofmt`/`go vet`/`go test -race` on `service/` push/PR |
| R12 | The vulnerability reporting channel the documentation names is not enabled on the repository | Outside the process | A reporter following `SECURITY.md` finds no private channel and is pushed toward a public issue | none; the one working route is a public issue, which the same document forbids for an unfixed vulnerability |
| R13 | The Go service's only shipped credential is a single shared bearer token, with no per-caller identity, no audit trail, and no rotation without a restart | B9 | Any holder can query the whole index; a leaked token cannot be revoked short of restart, and there is no trail of who used it | `TokenAuthority` (`service/token_authority.go:54`) with a constant-time digest compare; credential read once at startup (`service/token_authority.go:15`); `service/README.md` documents that rotation requires restart. The binary default is deny-all (`service/service.go:36`). A per-request correlation id (`26e95cf`, `service/service.go` `newRequestID`) is minted and forwarded to the engine as `X-Request-Id`, but it identifies a request, not a caller — all holders still share the one token |
| R14 | `POST /find-pinned` is unauthenticated, so any local caller can trigger a bounded 1M-row / 64 MiB index hash while holding a SQLite writer fence | B1, B9 | A caller that reaches the endpoint can stall writers for the scan duration; the fence is held from identity through the query (`_handle_find_pinned` in `resembl/server.py:1529`) | Bounds in `resembl/pinned_snapshot.py:9` (`MAX_SNAPSHOT_ROWS=1_000_000`, `MAX_SNAPSHOT_BYTES=64MiB`); a stored non-finite `REAL` now raises `ValueError` from `_snapshot_value` in `resembl/pinned_snapshot.py` (clean refusal, not a `json.dumps` encoder message) and a `bool` serializes as its integer form; `service/README.md` requires the warm engine to stay isolated on loopback behind the Go service. No authentication on the Python route itself |
| R15 | `/metrics` and `/health` are unauthenticated on both Python and Go servers and disclose request counts, cache hit rate, latency, and uptime. The Python `GET /health` additionally reports database reachability and probe latency; the Go `GET /healthz` is liveness only | B1, B9 | Process internals (traffic volume, hit rate, latency) readable by any local caller; aids timing attacks against R1/R13 | None beyond the loopback default on Python (`resembl/cli.py:767`); Python `GET /metrics` (`_handle_metrics` in `resembl/server.py:1467`) exposes `resembl_server_up`, `resembl_server_uptime_seconds`, `resembl_server_requests_total` by path and status, `resembl_server_request_duration_seconds` latency and `resembl_server_result_cache_total` hit/miss, and `GET /health` (`_handle_health` in `resembl/server.py:1413`) returns database status plus `latency_ms`; Go `GET /metrics` (`service/service.go:212`) exposes `resembl_service_up`, `resembl_service_uptime_seconds`, `resembl_service_requests_total` by path and status, and `resembl_service_request_duration_seconds` histogram, and `GET /healthz` (`service/service.go:203`) is liveness only, no dependency I/O |
| R16 | The Go service's custody slots are held after an uncertain backend outcome and never auto-released, so a client that times out can permanently consume capacity | B9 | A hostile or flaky client can exhaust the 1..64 concurrency slots (`service/service.go:123`) and deny service to legitimate callers until an operator repairs slots | Slots channel with `busy` 503 on overflow (`service/service.go:261`); `FileCustody` (`service/custody.go:24`) leaves the slot occupied across restarts by design; an operator must verify the engine stopped before repairing |
| R17 | A tampered backup file or manifest is accepted as a restore candidate with only a count and content-hash comparison against a manifest anyone who can write the directory can forge | B2, B6 | A corrupted or hostile backup restores a wrong corpus | `db_backup` now folds the published copy to a single-file restore point (`PRAGMA journal_mode=DELETE` and `wal_checkpoint(TRUNCATE)`, `3d77755`, `db_backup` in `resembl/core.py`), so it verifies on its own; `verify-backup` opens the backup read-only (capping the `integrity_check` read at three rows, `eed3273`, `db_verify_backup` in `resembl/core.py`), checks expected tables, runs `PRAGMA integrity_check`, fails on a `-wal`/`-shm` sidecar beside the backup (`e95b19d`, `db_verify_backup` in `resembl/core.py`), and compares the live snippet count and corpus `content_hash` (now including the `minhash` payload) against the per-backup manifest (`db_verify_backup` in `resembl/core.py`); a missing manifest is an issue, not a failure, and a count/hash mismatch is a hard failure. When the manifest records a `config_sha256` or `runtime_sha256`, the `<backup-stem>.config.toml` and `<backup-stem>.runtime-manifest.json` sidecars must also exist and hash to them (`8616290` for config; runtime sidecar per `docs/restore.md`, `_db_backup_runtime_read` in `resembl/core.py`), the manifest's `config`/`runtime_manifest` values are plain basenames only (`4678fd1`, `db_verify_backup` in `resembl/core.py`, closing the traversal/hash-oracle), and retained backups are selected/pruned by mtime not filename (`_db_backup_candidates` in `resembl/core.py`), but the manifest itself is unauthenticated, so a forged set still passes `verify-backup`. New backups are stronger: `db_backup` (`resembl/core.py`) re-verifies the published file and removes it on failure, re-verifies a retained backup before reusing it and writes a fresh self-verified copy instead of reporting success for a corrupt one (`0e258ed`, `db_backup` in `resembl/core.py`), writes backup, manifest, and both sidecars mode `0600` (`_write_sidecar` in `resembl/core.py`), and the CLI exits 1 when the manifest could not be written or the published copy fails verification (`resembl/cli.py`, `backup` command; `a4dba24`). `rm`, `clean`, and `collection delete` now take that verified backup first via `db_backup_for_delete` (`8dcee74`, `db_backup_for_delete` in `resembl/core.py`, `resembl/cli.py` `rm`/`clean`/`collection_delete_cmd`), and `merge` now takes the same pre-merge backup (`a4dba24`, `resembl/cli.py` `merge`, `--force` / `--backup-dir` / `--no-backup`) before rewriting names/tags/collections in place; a failed backup aborts the deletion or merge with exit 1 rather than proceeding unverified. `make backup` / `make backup-drill BACKUP_DIR=<off-host-dir>` (`Makefile`, `docs/restore.md`) schedule the off-host copy and prove the newest restores, failing loudly when `BACKUP_DIR` is unset |
| R18 | The Lambda bootstrap reads `RESEMBL_ENGINE_URL` verbatim once surrounding whitespace is trimmed, so an operator-supplied engine address reaches the loopback-only engine adapter unchecked beyond its loopback refusal | B9 | A misconfigured or hostile environment points the adapter at an attacker-chosen loopback listener; with `DenyAll` no query is dispatched today, so the exposure is liveness plus any future authorizer inheriting the address | `NewLocalEngine` loopback refusal (`service/http_engine.go:29`); the bootstrap trims the value and treats a blank one as unset, defaulting to `http://127.0.0.1:8081` (`6963a53`, `service/cmd/resembl-lambda/main.go`, `strings.TrimSpace`); the bootstrap is deny-all with no token path (`service/cmd/resembl-lambda/main.go`) |

## Attack surface

Every entry point below is present in the code at the cited location.

### Network listeners

| Entry point | Location | Notes |
| ----------- | -------- | ----- |
| `POST /find` | `do_POST` in `resembl/server.py:1316` | Unauthenticated, read-only. |
| `POST /find-pinned`, `GET /runtime-generation` | `_handle_find_pinned` in `resembl/server.py` | Experimental, unauthenticated like the other routes; the Go service in `service/` is the intended caller. `/find-pinned` reads the whole SQLite index to hash it, bounded by `resembl/pinned_snapshot.py` (1,000,000 rows, 64 MiB) while holding a writer fence, so a caller that reaches it can stall writers. Keep it on loopback behind the service. |
| `POST /find-batch` | `do_POST` in `resembl/server.py:1316` | Unauthenticated, up to 1000 queries per request (`MAX_BATCH_QUERIES` in `resembl/server.py:652`, enforced at `resembl/server.py:1653`). |
| `POST /score`, `POST /band-keys` | `_score_payload`, `_band_keys_payload` in `resembl/server.py` | Unauthenticated, read no database. At most 1000 listings per request in the 8 MiB body; each is tokenized and fingerprinted, so the list length bounds the work. |
| `GET /scheme`, the `scheme` of `/score` and `/band-keys` | `_scheme_payload`, `_scheme_id` in `resembl/server.py` | Unauthenticated, reads no database. Each distinct parameter set fingerprints one fixed probe listing; the ids are computed per request and deliberately uncached (`_SCHEME_CACHE_MAX = 0`, `d190380`), so a client cycling `normalize`, `bands` and `rows` cannot grow process memory. |
| `X-Request-Id` header (Python and Go) | `_correlation_id` in `resembl/server.py` (`26e95cf`), `requestIDFromContext` in `service/service.go`, `LocalEngine.find` in `service/http_engine.go` | A caller-supplied id is adopted only when bounded to 64 chars and free of control characters; otherwise the server remints. The Go service forwards its minted id so one id spans both hops and the `X-Request-Id` response header echoes the id the logs record. |
| `POST /v1/find`, `POST /v1/find-batch` (Go) | `service/service.go:175` (`ServeHTTP`) | Experimental portable boundary in `service/`. Requires an `Authorizer` (`service/service.go:23`); the shipped binary defaults to deny-all (`service/service.go:36`, `cmd/resembl-service/main.go`) unless `-token-file` supplies a bearer credential (`service/token_authority.go:54`). 8 MiB body cap (`service/service.go:277`), concurrency 1..64 (`service/service.go:123`), per-request timeout. `/v1/find-batch` answers `501` on the shipped pinned binary because `PinnedSQLiteEngine` implements `Find` only (`service/pinned_engine.go:32`), so the handler's `BatchEngine` assertion fails (`service/service.go:324`); a `LocalEngine` backend does implement `FindBatch` (`service/http_engine.go:37`). Not a deployable public service (`service/README.md:3`). |
| `GET /health` (Python) | `_handle_health` in `resembl/server.py:1413` | Unauthenticated; probes the served database with `SELECT 1` and returns `status`, `mode`, `database` reachability, `uptime_seconds`, and `latency_ms` (on success). A stateless server answers `ok` with no database check. |
| `GET /metrics` (Python) | `_handle_metrics` in `resembl/server.py:1467` | Unauthenticated; exposes `resembl_server_up`, `resembl_server_uptime_seconds`, `resembl_server_requests_total` by path and status, `resembl_server_request_duration_seconds` histogram, `resembl_server_result_cache_total` hit/miss, and per-path query errors. No user-controlled label values, so cardinality is bounded by fixed paths and status codes (`_metric_path_label` in `resembl/server.py:935`). |
| `GET /healthz` (Go) | `service/service.go:203` | Local process liveness only; no dependency I/O, no authentication. Under the Lambda bootstrap the same liveness answers an API Gateway event, and every query stays refused. |
| `GET /metrics` (Go) | `service/service.go:212` | Unauthenticated; exposes `resembl_service_up`, `resembl_service_uptime_seconds`, `resembl_service_requests_total` by path (`/v1/find`, `/v1/find-batch`, `/healthz`, `/metrics`, other) and status, and `resembl_service_request_duration_seconds` latency histogram. No user-controlled label values, so cardinality is bounded by fixed paths and status codes. |
| API Gateway HTTP API v2 event (Lambda) | `service/internal/lambdaproxy/proxy.go` (`Handle`), `service/cmd/resembl-lambda/main.go` | The Lambda bootstrap wraps the same `http.Handler` behind `DenyAll`; `lambdaproxy.Handle` translates an API Gateway v2 event into a request, caps input at 8 MiB and output at 4 MiB before base64, rejects duplicate or malformed headers, and never treats gateway identity claims as a grant (`service/internal/lambdaproxy/README.md`). `GET /healthz` answers liveness; every query is refused until an owner-backed authorizer is supplied. |
| `resembl serve --stateless` | `_stateless_serve` in `resembl/server.py` | Holds no database, so the listener exposes no snippet corpus: only `/score`, `/band-keys`, `/scheme`, `/health` and `/metrics` answer, the find paths answer `501`. Still unauthenticated; a non-loopback bind is refused without `--allow-unauthenticated` (`6cbddc6`, `serve` in `resembl/cli.py`) and needs a network gate in front of it when allowed. |
| `resembl serve` bind | `serve` in `resembl/cli.py` | `--host` is free-form; a non-loopback host is refused without `--allow-unauthenticated` (`6cbddc6`, `serve` in `resembl/cli.py`), and with the flag only prints a warning and proceeds. The Go `resembl-service` likewise requires `-token-file` for a non-loopback `-listen` (`6cbddc6`, `isLoopbackListen` in `service/cmd/resembl-service/main.go`). `--port` accepts `0`, which lets the OS assign a port. |
| Any other verb | `__getattr__` and `send_error` in `resembl/server.py:1197` | The handler resolves any unhandled `do_*` name to the JSON `405` envelope (`__getattr__` in `resembl/server.py:1209`) and answers stdlib errors, including malformed request lines, with the same JSON body through `_respond` (`resembl/server.py:1712`), so no response bypasses the hardening headers. The only `501` left is the deliberate one: a `--stateless` server answering a database path (`_stateless_refused` in `resembl/server.py:1352`). |

`GET`, `PUT`, `DELETE`, `PATCH`, `HEAD` and `OPTIONS` are implemented and
answer `405` with the same JSON error envelope (`_method_not_allowed` in
`resembl/server.py:1372`); a `HEAD` carries those headers without a body,
and the Lambda adapter strips the body from `HEAD` answers too
(`6dfaa9b`, `service/internal/lambdaproxy/proxy.go`).
A `POST` to any other path answers `404` (`do_POST` in
`resembl/server.py:1324`). An oversized body answers `413`
`{"error": "request too large"}` (`_read_body` in
`resembl/server.py:1274`, `do_POST` in `resembl/server.py:1334`), matching
the Go service and the Lambda proxy for the same 8 MiB cap; previously it
answered `400`, indistinguishable from a malformed body (`2cd23c9`).

### CLI surface

`resembl` (`resembl/cli.py:113`) exposes 21 commands plus four
sub-applications (`config` 5, `name` 2, `tag` 2, `collection` 6) registered at
`resembl/cli.py:122`. The ones that take untrusted input from outside the
tool's own process:

- `import <path>` walks the tree and reads every `.asm`/`.txt`
  (`resembl/cli.py:975`, `resembl/cli.py:1010`).
- `reindex` recomputes the MinHash of every row in the corpus on a worker
  pool defaulted to one process per CPU (`resembl/cli.py:1404`,
  `resembl/cli.py:1420`).
- `merge <source>` opens a file path or a full database URL
  (`resembl/cli.py:1754`, `resembl/core.py:2036`), and now takes the
  same verified pre-merge backup as the destructive commands
  (`a4dba24`, `db_backup_for_delete` in `resembl/core.py`, `--force` /
  `--backup-dir` / `--no-backup` on `resembl/cli.py` `merge`; a failed
  backup aborts with exit 1 before the merge).
- `find`, `find-batch`, `search`, `compare` take query text, patterns, and
  file paths (`resembl/cli.py:1446`, `resembl/cli.py:1543`,
  `resembl/cli.py:1630`, `resembl/cli.py:1654`).
- `export <file>`, `export-yara <file>` write to a caller-chosen path
  (`resembl/cli.py:896`, `resembl/cli.py:926`).
- `add`, `show`, `rm`, `name *`, `tag *`, `collection *` write to the
  database. `rm`, `clean`, and `collection delete` first take a verified
  pre-delete backup (`db_backup_for_delete` in `resembl/core.py`) unless
  `--no-backup` is given; a failed backup aborts the deletion (`8dcee74`,
  `resembl/cli.py` `rm`/`clean`/`collection_delete_cmd`), on non-SQLite
  backends the `ValueError` from `db_backup` refuses the deletion.
- `backup <dir>` writes a consistent SQLite copy of the corpus plus a
  per-backup manifest (`<backup-stem>.json`, mode `0600`), a config
  sidecar (`<backup-stem>.config.toml`, mode `0600`, copied verbatim
  from the live `config.toml`), and a runtime-manifest sidecar
  (`<backup-stem>.runtime-manifest.json`, mode `0600`, copied verbatim
  from `RESEMBL_RUNTIME_MANIFEST` when set, `_db_backup_runtime_read`
  in `resembl/core.py`) into a caller-chosen directory, pruning to
  the newest `--keep` (default 7) by modification time
  (`_db_backup_candidates` in `resembl/core.py`)
  (`resembl/cli.py`, `db_backup` in `resembl/core.py`); the copy is
  folded to `journal_mode=DELETE` with a `wal_checkpoint(TRUNCATE)`
  before publishing (`3d77755`) so it is a single-file restore point;
  `verify-backup <path>` opens a backup read-only, checks the expected tables, and
  compares the live snippet count and content hash against that
  manifest, cross-checking the config and runtime sidecars when the
  manifest records their hashes
  (`resembl/cli.py`, `db_verify_backup` in
  `resembl/core.py`).
- `make backup BACKUP_DIR=<off-host-dir>` and
  `make backup-drill BACKUP_DIR=<off-host-dir>` are operator scheduled
  entry points (`Makefile`, `docs/restore.md`): `backup` runs
  `resembl backup "$(BACKUP_DIR)" --keep ... --force` and fails loudly
  when `BACKUP_DIR` is unset; `backup-drill` copies the newest
  `assembly-*.db` to a scratch path, runs `verify-backup` there, and
  appends `(date, filename, result)` to
  `$(BACKUP_DIR)/.restore-drill.log`. Both fail when `BACKUP_DIR` is
  unset or no backup exists.

`list` (`resembl/cli.py:1198`) prints every snippet in the corpus to stdout
unless `--range` narrows it, and `verify` (`resembl/cli.py:1361`) counts
snippets and buckets and checks index consistency across the whole corpus, so
both walk the corpus with no row cap; `stats` (`resembl/cli.py:1336`) and
`clean` (`resembl/cli.py:1739`) read or drop the LSH index, and `clean` is
the command that destroys derived state (while `rm` and `collection delete`
destroy rows and collections; both now take the verified pre-delete backup
above, `8dcee74`). None of the four is a network
surface, but each is an availability or disclosure path when the corpus is
large.

The global callback options apply to every command: `--quiet/-q`,
`--verbose/-v`, `--no-color`, `--format`, `--tz`, and an eager `--version`
(`resembl/cli.py:781`). `--force` suppresses a confirmation prompt on `add`,
`export`, `export-yara`, `import`, `rm`, and `reindex`
(`resembl/cli.py:898`); `import -j` and `reindex -j` set the worker count
directly (`resembl/cli.py:1044`, `resembl/cli.py:1406`).

`resembl-find` (`resembl/find_client.py:84`, `resembl/find_client.py:187`) is
a second entry point: it reads a query from `--query` or an arbitrary `--file`
path (`resembl/find_client.py:101`) and POSTs it to the loopback server.

### Environment and configuration

| Input | Location | Trust |
| ----- | -------- | ----- |
| `RESEMBL_DATABASE_URL`, then `DATABASE_URL` | `resembl/paths.py:33`, `resembl/paths.py:57` | Trusted verbatim; may carry a password. |
| `RESEMBL_CACHE_DIR` | `resembl/paths.py:81` | Trusted verbatim; selects where the port file is written and read. |
| `RESEMBL_RUNTIME_MANIFEST` | `_handle_find_pinned` in `resembl/server.py` | Trusted verbatim; selects the file `resembl/runtime_attestation.py` checks loaded code against. The supervisor must keep it and the interpreter environment write-protected. |
| `RESEMBL_ENGINE_URL` (Lambda only) | `service/cmd/resembl-lambda/main.go:17` | Trusted verbatim once surrounding whitespace is trimmed, and a blank value counts as unset (`6963a53`, `strings.TrimSpace`); selects the loopback engine the adapter dials. `NewLocalEngine` refuses a non-loopback host. |
| `RESEMBL_CONFIG_DIR` | `resembl/paths.py:97` | Trusted verbatim; selects the config file. |
| `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` | `resembl/paths.py:100`, `resembl/paths.py:84` | Trusted when the override above is unset. |
| `RESEMBL_SEED` | `resembl/diagnostics.py:35`, `resembl/diagnostics.py:61` | Trusted verbatim; seeds the one generator every MinHash sample in a run draws from, so it changes the fingerprints that run produces. Parsed as decimal: an unparseable value fails fast (`ValueError` in `sample_rng`, `resembl/diagnostics.py`, `9aeab2b` base-10 `int`; in `_retry_sleep`, `resembl/models.py`, `ed0705b`; and at startup in the Go `New`/`newRequestID`, `service/service.go`, `9aeab2b` base-10 `ParseInt`), so hex/octal spellings are rejected, not silently reinterpreted. |
| `RESEMBL_NOW` | `resembl/models.py:37`, `resembl/models.py:48` | Trusted verbatim; replaces the created-at clock for every row and for backup filenames/manifests. An unparseable value fails fast on every read path (`timestamp_now`/`monotonic_now`/`_retry_sleep` in `resembl/models.py`, `ed0705b`); the Go service validates it at startup in `New` via `isISO8601Instant` (`service/service.go`, `ed0705b`) and `parse_instant` treats ISO `-00:00` as not-an-instant (`9d6f2f0`). |
| `~/.config/resembl/config.toml` | `resembl/paths.py:106` | Parsed as TOML (`resembl/config.py:270`), then every value coerced to its field's type and range-checked (`resembl/config.py:157`, `resembl/config.py:74`). Written mode `0600` (`save_config` in `resembl/config.py`, `6273fa0`). `serve` fails startup on a malformed/unreadable file (`_find_defaults_load` in `resembl/server.py` via `_read_config_dict` in `resembl/config.py`, `ConfigError`); interactive commands keep the report-and-default behavior of `load_config`. |

The config file holds six fields (`resembl/config.py:114`):
`lsh_threshold`, `num_permutations`, `top_n`, `ngram_size`, `jaccard_weight`,
and `format`. The first five change what the tool reports; `format` changes
rendering only.

Every path in this table is resolved in `resembl/paths.py`, so the CLI, the
server, and the standalone client cannot disagree about where they read from
(`docs/adr/006-paths-module-owns-environment.md`).

### Files the tool writes

The port file (`serve` in `resembl/server.py:1854`), the config file
(`resembl/config.py:222`), exported YARA rules (`resembl/core.py:1221`), the
database itself, and database backups with their per-backup manifests
(`db_backup` in `resembl/core.py:2129`). Each is published with
write-temp-then-rename (`resembl/config.py:245`, `resembl/server.py:2125`,
`resembl/core.py:2279`).

## Trust boundaries

**B1, process to loopback socket.** The largest boundary in the codebase, and
the only one a peer can cross without the same OS account. Everything the
server accepts is treated as untrusted: body size
(`resembl/server.py:544`, `resembl/server.py:689`), content type
(`resembl/server.py:705`), JSON object shape and nesting depth
(`resembl/server.py:692`, `resembl/server.py:695`), field types
(`resembl/server.py:624`, `resembl/server.py:635`), and the range of
`threshold`, `jaccard_weight`, `ngram_size`, and `num_permutations`
(`resembl/server.py:325`, `resembl/server.py:332`, `resembl/server.py:349`,
`resembl/server.py:344`).

**B2, files and arguments to library code.** Snippet text is untrusted
input. It is lexed and fingerprinted (`resembl/scoring.py:954`), and on
export it is embedded in generated YARA rules (`resembl/core.py:1028`).
Backup manifests are untrusted input here: their `config` and
`runtime_manifest` fields are plain basenames only, enforced in
`db_verify_backup` (`resembl/core.py`, `4678fd1`).

**B3, environment to runtime.** The variables above that name paths are read at call time and
are never validated beyond being non-empty. `RESEMBL_SEED` and
`RESEMBL_NOW` are the sharp two: neither is a path, and both change results
without changing what the operator can see (`resembl/diagnostics.py:61`,
`resembl/models.py:48`); both now fail fast when unparseable (`ed0705b`).

**B4, application to database.** The ORM parameterizes every query, so
untrusted text is not concatenated into SQL. Three places build a `LIKE`
pattern from caller input: the checksum-prefix resolver escapes the wildcard
characters (`resembl/cli.py:726`), the model's JSON-name containment lookup
escapes them (`resembl/models.py:180`), while the name search does not
(`resembl/core.py:1612`). The result set is capped by `limit`, so the
unescaped wildcards widen the match set rather than the returned page.

**B5, cache directory to process.** The port file is read as an integer and used to open an HTTP connection (`resembl/find_client.py:121`, `resembl/cli.py:429`). Its contents are authenticated by nothing. The file names a port only: both clients dial `127.0.0.1` (`resembl/find_client.py:152`, `resembl/cli.py:436`), so a forged port reaches a listener the forger must already be running on the loopback interface. The standalone client narrows this by refusing a port outside 1-65535 before dialing (`6406aee`, `_main` in `resembl/find_client.py`), so a forged port that cannot be bound at all is rejected; the in-process `find` client guards the narrower ephemeral range 1024-65535 (`cdabd39`, `_server_request` in `resembl/cli.py`), so a forged port naming a privileged service is rejected there; a forged port inside the admitted range still reaches an attacker listener on both.

**B6, merge source to local corpus.** Rows from another database are
inserted after structural checks, not after a trust decision about the source
(`resembl/core.py:2036`). `merge` takes a verified pre-merge backup
first (`db_backup_for_delete` in `resembl/core.py`, `a4dba24`)
unless `--no-backup` is given, so a merge that corrupts the corpus
is restorable; the backup does not authenticate the source.

**B7, secrets to code.** The database URL may embed a password. It is masked for display (`resembl/paths.py:63`, used at `resembl/find_client.py:124`) but used unmasked to derive the port-file name (`resembl/paths.py:118`) and to open the connection. The in-process result cache keys on the engine URL rendered with the password hidden (`6273fa0`, `_find_one` in `resembl/server.py`, `render_as_string(hide_password=True)`), so credentials do not persist in the cache keys.

**B9, Go service to pinned engine.** The Go service (`service/service.go:175`)
is the only boundary that authenticates a caller: it calls
`Authorizer.Allow` (`service/service.go:244`) before dispatch and fails closed
on error or missing authority. The shipped binary is deny-all by default and
only enables `TokenAuthority` when `-token-file` is given
(`cmd/resembl-service/main.go`); that token is a single shared 256-bit hex
credential, read once at startup, with no per-caller identity and no
revocation short of restart (`service/token_authority.go:15`,
`service/README.md:9`). The Lambda bootstrap (`cmd/resembl-lambda/main.go`)
is deny-all with no token path; its adapter (`service/internal/lambdaproxy/proxy.go`) caps input at 8 MiB and output at 4 MiB, rejects duplicate or malformed headers (including a comma-bearing `Authorization` and a `Cookie` with cookies), and never treats API Gateway identity claims as a grant (`service/internal/lambdaproxy/README.md`). The engine behind the service is reached over
loopback only: `NewLocalEngine` (`service/http_engine.go:29`) refuses
non-loopback hosts, refuses redirects, and caps responses at 8 MiB. The pinned
path adds three pins: index digest, artifact digest, and generation
(`service/pinned_engine.go:29`), verified against the Python `/find-pinned`
endpoint (`_handle_find_pinned` in `resembl/server.py:1529`) which holds a
SQLite writer fence from identity through the query and re-checks the
runtime manifest before and after (`resembl/server.py:1568`,
`resembl/server.py:1593`). That manifest check
(`resembl/runtime_attestation.py:20`) is file integrity under a trusted
supervisor, not remote attestation (`service/README.md:13`). Custody slots
(`service/custody.go:24`) are claimed before dispatch and retained after an
uncertain backend outcome, including across restarts; the operator must
verify the engine stopped before repairing a slot (shutdown now drains
in-flight queries for up to 30s, `1f06887`, so a clean SIGTERM/SIGINT
no longer orphans them).

**B8, build to release.** The published wheel carries the runtime surface
above plus whatever the release pipeline admits. An SBOM workflow exports a
CycloneDX 1.5 inventory of the runtime graph on a push to `main` and on a
`v*` tag (`.github/workflows/sbom.yml:9`). CodeQL runs the Python and Go SAST suites (`language: [ 'python', 'go' ]` in `.github/workflows/codeql.yml:29`); the Go service workflow (`.github/workflows/service.yml`) runs `gofmt`, `go vet`, `go test -race`, and the image build. CodeQL runs the Python and Go suites
on a push to `main`, a pull request to `main`, and a weekly cron
(`.github/workflows/codeql.yml:4`), and dependency review runs on pull
requests to `main` only, failing on moderate severity
(`.github/workflows/dependency-review.yml:50`). None of the two has a
`tags:` trigger, so a release is not statically analyzed and not
dependency-reviewed at the moment it is cut; R11 records that as a gap rather
than a control. The Pygments floor sits above the ReDoS advisory
(`pyproject.toml:34`), and a tag build records a provenance attestation over
the sdist and the wheel (`.github/workflows/build.yml:102`, the `attest` job,
tag-only by its `if:` at `.github/workflows/build.yml:104`), so a consumer can
check an artifact against this repository and the commit that built it. The
root `.dockerignore` excludes local databases, benchmark data, and build
output from the `service/Dockerfile` build context, which is the repository
root (`docker build -f service/Dockerfile .`), so sensitive corpus data never
ships in the service image context (`.dockerignore`). No
packaging step pulls code at install time; nothing in `resembl/` executes a
network fetch at import.

## Assets

| Asset | Why it matters | Held in |
| ----- | -------------- | ------- |
| Snippet corpus | Proprietary reverse-engineering data; the reason the tool exists | `snippet` table, served over B1 |
| Database credentials | Reach to a shared Postgres/MySQL corpus | `RESEMBL_DATABASE_URL` / `DATABASE_URL`, process environment |
| LSH index | Minutes of CPU to rebuild; its loss is an availability hit, not a data loss | `lsh_bucket` table |
| Warm server availability | The performance reason to run `serve` at all | `serve` in `resembl/server.py:1854` |
| Port file | Discovery of the running server, and a redirect target | cache dir |
| Exported YARA rules | Downstream detection artifacts | caller-chosen output file |
| Config file | Thresholds and match settings, applied with no on-screen report that they are in force | `config.toml`, mode `0600` on write (`save_config` in `resembl/config.py`, `6273fa0`) |
| Release artifacts | The only thing a consumer installs; a substituted one runs with the operator's database credentials | PyPI, attested per B8 |
| Lambda engine address | Selects the loopback engine the adapter dials; a hostile value points it at an attacker listener once an authorizer is supplied | `RESEMBL_ENGINE_URL` (`service/cmd/resembl-lambda/main.go:17`) |
| Go service bearer token | Single shared credential gating the whole pinned index; rotation requires restart, so a leak persists until then | `-token-file`, read once into memory (`service/token_authority.go:15`) |
| Custody slots | Persistent capacity claims; a held slot after an uncertain outcome is a denial of service until an operator repairs it | Custody directory, mode 0700 (`service/custody.go:24`) |
| Runtime manifest | The trust anchor for `/find-pinned`; if writable by an attacker, it can attest tampered loaded code | `RESEMBL_RUNTIME_MANIFEST` path (`_handle_find_pinned` in `resembl/server.py:1529`) |
| Database backups | A full, restorable copy of the corpus; a tampered backup restores a wrong corpus (R17). The pre-delete backups taken by `rm`/`clean`/`collection delete` and the pre-merge backup taken by `merge` (`a4dba24`, default `.resembl-pre-delete-backups`, `db_backup_for_delete` in `resembl/core.py`, `resembl/cli.py` `merge`) are the same asset with the same forgeability; `--no-backup` removes the restore point entirely | caller-chosen backup directory (`db_backup` in `resembl/core.py`), backup, per-backup manifest `<backup-stem>.json`, `<backup-stem>.config.toml` config sidecar, and `<backup-stem>.runtime-manifest.json` runtime sidecar, all mode `0600` (sidecars via `_write_sidecar` in `resembl/core.py`) |

## Threats per boundary

### B1, loopback socket

*Information disclosure.* Any process on the host can enumerate the corpus,
one request at a time. Every find parameter is range-checked, `top_n`
included: a request outside `1..1000` rows is refused with a `400`
(`_parse_find_request` in `resembl/server.py:426`, `_MAX_TOP_N` at
`resembl/server.py:662`, the upper bound at `resembl/server.py:469`). That
bounds one response, not the corpus: repeated requests with a permissive
`threshold` and `top_n = 1000` still page the whole table out of an
unauthenticated endpoint, and the config layer deliberately leaves the local
`top_n` unbounded (`resembl/config.py:63`) because only the request path
needs a bound. A config above that bound is clamped to it as the served
default (`resembl/server.py:1950`), so it bounds the rows a request
omitting `top_n` receives too.

A request may also name a `threshold`, `ngram_size`, or `num_permutations`
that differs from the one the server's index was built for. That is refused
with a `400` (`resembl/server.py:502`), because honouring it would rebuild
the shared `lsh_bucket` table from inside a request thread while other
threads read it.

A browser on the same host is a second peer, and the binding does not stop
it. The server sets no CORS headers, which blocks a cross-origin `fetch`
from *reading* the answer, and on a loopback bind it also checks the `Host`
header and answers `403` to anything but the names it was bound to
(`_host_allowed` in `resembl/server.py:1301`, `_loopback_allowed_hosts` in
`resembl/server.py:672`), so a page that rebinds
its own hostname onto `127.0.0.1` and sends a same-origin request with no
preflight is refused. The `Host` a rebound page carries is still its own
name, which is what the check reads. The auto-assigned port
(`resembl/cli.py:580`) remains enumerable by a page that probes the port
range, and a non-loopback bind is refused without `--allow-unauthenticated`
(`6cbddc6`, `serve` in `resembl/cli.py`); when allowed, it is reached by
whatever name resolves to it. A verb the handler does not implement is answered with the JSON `405` envelope instead: `__getattr__` resolves any unhandled `do_*` name to the shared not-allowed answer (`resembl/server.py:1209`), and `send_error` answers stdlib errors, including malformed request lines, through `_respond` (`resembl/server.py:1712`), so no response skips the hardening header block. Earlier revisions of this model carried the stdlib's header-less HTML `501` as a live finding; the code no longer emits it.

*Denial of service.* `ThreadingHTTPServer` spawns a thread per connection, and `_FindServer` bounds that with a 96-slot semaphore (`_MAX_HANDLER_THREADS`, `resembl/server.py:121`, acquired in `process_request` at `resembl/server.py:1872`): excess connections wait in the listen backlog rather than pinning a thread. The acquire is bounded to 30 seconds (`_HANDLER_SLOT_TIMEOUT_SECONDS`, `7c10336`), so a stalled handler cannot wedge the accept loop and `shutdown`; a timed-out slot closes the queued connection. Each thread holds a 30-second idle timeout (`resembl/server.py:1104`). The engine pool is 32 plus 64 overflow (`resembl/server.py:1885`). A client that opens connections faster than requests complete therefore exhausts pool connections and file descriptors, but not handler threads; there is still no per-caller rate limit or request accounting, so the backlog and the pool are the policy.

*Repudiation.* `log_message`/`log_request` write at DEBUG
(`resembl/server.py:1760`, `resembl/server.py:1776`), which is quiet unless
the server was started with `serve -v`, so a default deployment records
that a query was made, from where, or what was asked for nothing. Failures
are logged with `logger.exception` (`resembl/server.py:1521`), and a
request slower than 1s (`_SLOW_REQUEST_SECONDS` at
`resembl/server.py:923`) is logged at WARNING (`resembl/server.py:1790`);
those are the only per-request traces at default level.

*Elevation of privilege.* None available: the server executes no request-
derived SQL text and writes nothing.

*Host header.* Checked on a loopback bind, as above; a non-loopback bind
accepts any name it is reached by, which is the right rule for an address
that is not private to the host.

### B2, files and arguments

*Tampering.* `merge` accepts any source. Its rows, including names, tags,
and collections, are written into the local corpus.

*Tampering / information disclosure.* A backup in the backup directory is
trusted on the strength of its filename and a snippet-count plus
content-hash manifest. `verify-backup`
(`db_verify_backup` in `resembl/core.py`) opens the file read-only, checks
expected tables, runs `PRAGMA integrity_check`, fails on a `-wal`/`-shm`
sidecar beside the backup (`e95b19d`), and compares the live
count and `content_hash` (which now includes the `minhash` payload, so
a reindex makes a stale backup stale) against the manifest: a tampered
backup whose manifest is edited to match passes (R17). Newer backups
also carry a `<backup-stem>.config.toml` sidecar, copied verbatim from
the live `config.toml` and hashed as `config_sha256` in the manifest,
and a `<backup-stem>.runtime-manifest.json` runtime sidecar, copied
verbatim from `RESEMBL_RUNTIME_MANIFEST` when set and hashed as
`runtime_sha256` (`_db_backup_runtime_read` in `resembl/core.py`);
when the manifest records either hash, `db_verify_backup` requires
the sidecar to exist and hash to it, and a missing or mismatched
sidecar is a hard failure (`8616290` for config, runtime sidecar per
`docs/restore.md`). Backup, manifest, and sidecars are written mode
`0600` (`_write_sidecar` in `resembl/core.py`) but the manifest is
otherwise unauthenticated, so anyone who can write the backup
directory can forge all of them. The manifest's `config` and
`runtime_manifest` values are plain basenames only: `db_verify_backup`
rejects a value whose `os.path.basename` differs (`4678fd1`), so a
forged manifest cannot make `verify-backup` read/hash a file outside
the backup directory (the prior path traversal / hash oracle is
closed). `db_backup` selects and prunes retained backups by
modification time (`_db_backup_candidates` in `resembl/core.py`),
not filename order, so a hand-supplied `assembly-*.db` with a late
name cannot make pruning delete the genuinely newest restore point.
`db_backup` re-verifies the just-published file and deletes it on
failure, so a backup that `backup` reported as success has verified
at write time; it also re-verifies a retained backup before reusing
it and writes a fresh self-verified copy instead of reporting
success for a corrupt one (`0e258ed`). That does not make a later
`verify-backup` of a hand-supplied set trustworthy.

*Denial of service.* `import` has no cap on file count or file size: the
walk materialises every matching path into a list (`resembl/cli.py:1010`),
sizes a spawn-based worker pool from the file count and CPU count
(`resembl/cli.py:1044`), and the chunked writes (`resembl/cli.py:1058`) keep
per-batch memory flat but not total work. `reindex` has the same shape over
the corpus rather than a tree (`resembl/cli.py:1420`). `find-batch --file`
reads the whole file into a list before chunking the requests
(`resembl/cli.py:1571`). `list` and `verify` walk the entire corpus with no
row cap.

### B3, environment

*Spoofing.* A process that can set `RESEMBL_CACHE_DIR` for the tool
controls where the port file is read from and can therefore redirect
`find`'s query text to a loopback listener it owns (R3).
`RESEMBL_CONFIG_DIR` does the same for thresholds and permutation counts,
which changes match results silently rather than failing.

*Tampering.* `RESEMBL_SEED` (`resembl/diagnostics.py:61`) changes the generator
every MinHash sample in the process draws from, and so the fingerprints that
process produces, and `RESEMBL_NOW` (`resembl/models.py:48`) changes every
recorded creation time. Neither is a path, so neither is visible in the
resolved-path listing a reader would check, and neither reports itself in the
output.

### B5, cache directory

*Tampering / spoofing.* `resembl/find_client.py:121` and
`resembl/cli.py:429` read the port file, parse an integer, and connect. A
local process that can write the cache directory controls the port every
subsequent query is sent to. The server does create its side defensively: the
cache directory is made with mode `0o700` (`serve` in
`resembl/server.py:2116`) and the
port file is created `0o600` rather than with the process umask
(`resembl/server.py:2125`). That covers the server's own creation path only.
`os.makedirs(..., exist_ok=True)` does not repair the mode of a directory that
already exists, and no code path reads the mode back, so a cache directory
created earlier by another tool, another user, or an operator's own `mkdir`
keeps whatever mode it had and both clients then trust a file inside it.

*Tampering.* `port_file_cleanup` only removes the file while it still names
the port the caller failed on (`resembl/paths.py:150`), so one process
cannot retire another server's advertisement by racing the exit path. That
control does not extend to a third party rewriting the file's contents.

### B6, merge source

*Tampering.* Rows of unknown provenance enter the corpus and are indexed.
The fingerprint path is the sharpest edge and is closed: a blob that is not
in the `RMLH` format raises `ValueError` (`minhash_unpack` in
`resembl/scoring.py:1014`), a permutation count outside 2..4096 is
rejected at pack time (`minhash_pack` in `resembl/scoring.py`, `1f210e7`),
`MinHash.__init__` rejects `num_perm >= 2**32` and out-of-range
`hashvalues` (`resembl/minhash.py`, `9aeab2b`), and merge recomputes from
the source row's own code rather than deserializing (`db_merge` in
`resembl/core.py:2770`).

*Information disclosure.* A `postgresql+pg8000://user:pass@host/db` source
sends those credentials to the named host (`db_merge` in
`resembl/core.py:2770`). The URL is masked for display (`db_url_mask` in
`resembl/paths.py:68`) but that is a display control, not a network one.

*Information disclosure, the failure path.* When the source will not open
or the merge fails, `db_merge` logs the exception with `logger.exception`
and returns only a stable message naming the masked source (`c1eb6cb`,
`db_merge` in `resembl/core.py`); driver/SQL/port detail no longer reaches
the caller. The source URL is masked (`db_url_mask` in
`resembl/paths.py`, `0afeba7`), which closes the credential-in-error
case. Earlier revisions of this model recorded the raw driver text as a
live leak here; the code no longer emits it.

### B9, Go service to pinned engine

Authentication is a single shared bearer token, so any holder is indistinguishable from any other holder, and the engine behind it (find-pinned) authenticates no one at all. A token holder can query the whole index; a local process that reaches the Python port directly bypasses the token entirely (R14). The token is compared in constant time (service/token_authority.go:54), but there is no per-caller identity, no audit trail, and no revocation short of restart, so a leaked token is a standing credential.

Custody slots are claimed before dispatch and retained after an uncertain backend outcome (R16, service/custody.go:24). A client that triggers timeouts without the backend stopping can permanently consume the 1 to 64 slots (service/service.go:123). The slots channel returns busy 503 on overflow (service/service.go:261), which bounds concurrent work but not the permanent loss of slots.

The runtime manifest is the trust anchor for find-pinned. If an attacker can write the manifest or the interpreter files it covers, they can attest tampered loaded code. The supervisor must keep the manifest and interpreter environment write-protected (resembl/runtime_attestation.py:3, service/README.md:13). The verify check hashes every loaded module file before and after the query (`_handle_find_pinned` in `resembl/server.py:1568`, `resembl/server.py:1593`), bounded by MAX_FILES 10000 and MAX_BYTES 512MiB (resembl/runtime_attestation.py:16).

Results are not cacheable (Cache-Control: no-store, `service/service.go`). The Python engine behind it is unauthenticated, so the Go service authentication is the only gate on that path. When the engine rejects a request, the Go adapter (`LocalEngine.find` in `service/http_engine.go`) surfaces the engine's `error` detail (or the raw body, capped at 1 MiB) in its own error, which `service/service.go` logs at the operator boundary and answers to the caller only as the generic `engine unavailable` — so the detail reaches the operator log, not the caller response.

Every request carries a correlation id (26e95cf). ServeHTTP mints one (service/service.go newRequestID), echoes it as X-Request-Id, and LocalEngine (service/http_engine.go) forwards it to the engine, where the Python server adopts a bounded printable caller-supplied id (_correlation_id in resembl/server.py, max 64 chars, control characters rejected) or falls back to its own minted id. It joins the two logs for one request; it does not identify the caller and is not an audit trail.

### B8, build to release

*Repudiation of provenance.* The attestation recorded by the `attest` job
(`.github/workflows/build.yml:102`) covers only the artifacts that job built
and only on a tag, so an artifact published under this project's name that
did not come from that job has nothing to contradict it. A consumer has to
check the attestation, which no document in this repository tells them to do.

*Tampering by omission.* Because neither CodeQL nor dependency review has a
tag trigger (R11), a vulnerability introduced between the last `main` push
analysis and the tag is shipped without being scanned, and the release
workflow does not re-run either.

## Mitigations

| Control | Where | Covers |
| ------- | ----- | ------ |
| Loopback bind default | `resembl/cli.py:578` | Reduces B1 exposure to same-host processes |
| Non-loopback bind refusal | `serve` in `resembl/cli.py` (`6cbddc6`) | Refuses a non-loopback `--host` without `--allow-unauthenticated`; with the flag, only warns and proceeds, so the operator's explicit opt-in is the control |
| Go non-loopback token requirement | `isLoopbackListen` in `service/cmd/resembl-service/main.go` (`6cbddc6`) | Requires `-token-file` for a non-loopback `-listen`, so a routable bind cannot be unauthenticated by accident |
| Loopback `Host` check, 403 | `_host_allowed` in `resembl/server.py:1301`, `_loopback_allowed_hosts` in `resembl/server.py:672` | A page that rebinds its own hostname onto the loopback port reading a served query |
| Request body cap, 8 MiB / 413 envelope | `_MAX_BODY_BYTES` in `resembl/server.py:644`, `_read_body` in `resembl/server.py:1274` | B1 memory exhaustion; oversized bodies now answer `413` (`2cd23c9`), indistinguishable-from-malformed no longer |
| JSON depth and shape guard | `_read_body` in `resembl/server.py:1254` | `RecursionError` from a nested body killing the handler thread with no response |
| Batch query cap, 1000 | `MAX_BATCH_QUERIES` in `resembl/server.py:652` | B1 work per request |
| Find parameter range checks | `_parse_find_request` in `resembl/server.py:371` | NaN, out-of-range, and degenerate fingerprints |
| Query field check, string and non-blank | `_query_field_error` in `resembl/server.py:705` | A non-string query crashing the lexer into a `500` with its message, and a blank one answering `200` with no matches |
| `top_n` bounds, 1 to 1000 | `resembl/server.py:426`, `resembl/server.py:469`, clamped default at `resembl/server.py:1950` | One response returning the whole corpus (upper bound), or a `200` with an empty `matches` read as "no duplicates" (lower bound). A sequence of requests is not bounded |
| Index-parameter match check | `resembl/server.py:502` | A request rebuilding the shared LSH index mid-serve |
| Per-count MinHash template cap, 8 | `resembl/scoring.py:72` | An unbounded dict grown by cycling permutation counts |
| Scheme ids uncached | `_scheme_id` in `resembl/server.py` (`d190380`, `_SCHEME_CACHE_MAX = 0`) | A client cycling `normalize`/`bands`/`rows` growing process memory with distinct parameter sets |
| `MinHash`/`minhash_pack` count bounds | `resembl/minhash.py` (`9aeab2b` rejects `2**32` in `MinHash`; `1f210e7` rejects counts outside 2..`MAX_NUM_PERM` in `minhash_pack`) | An over-wide count reaching the stored packed format |
| Result-cache key is a query digest | `resembl/server.py:618` | Query text retained by the result cache, and an entry count standing in for a memory bound |
| Result-cache key hides the database password | `_find_one` in `resembl/server.py` (`6273fa0`) | Credentials retained in the long-lived cache keys |
| Per-key compute lock | `resembl/server.py:121` | A burst of identical queries multiplying one find across connections |
| Content-Type check, 415 | `_content_type_ok` in `resembl/server.py:1292` | B1 request shape |
| Backup published only after verification | A truncated or page-flipped copy must not be handed to a restore drill as the newest restore point | `db_backup` in `resembl/core.py` re-verifies with `db_verify_backup` and removes the file and manifest on failure; a retained backup is re-verified before reuse and replaced by a fresh self-verified copy when it no longer verifies (`0e258ed`, `db_backup` in `resembl/core.py`); `backup` CLI in `resembl/cli.py` exits 1 when `manifest_written` is false or `verified` is false (`a4dba24`); retained backups are selected and pruned by mtime (`_db_backup_candidates` in `resembl/core.py`), so a late-named hand-supplied file cannot hide or evict the genuinely newest restore point; the content hash includes the `minhash` payload, so a reindex makes a stale backup compare stale |
| Verify-backup WAL sidecar refusal | A backup that verifies with its `-wal` beside it but restores empty alone | `db_verify_backup` in `resembl/core.py` fails when a `-wal`/`-shm` sidecar is present (`e95b19d`) |
| Verified pre-delete / pre-merge backup before destructive commands | A `rm`/`clean`/`collection delete`/`merge` with no restore point; a silent corruption or sourceless deletion or hostile merge cannot be undone | `db_backup_for_delete` in `resembl/core.py` (`8dcee74`): takes `db_backup` and raises unless `verified` and `manifest_written` are both true; `resembl/cli.py` `rm`/`clean`/`collection_delete_cmd`/`merge` catch `(ValueError, OSError)` and exit 1 before deleting or merging, with `--no-backup` as the explicit opt-out (`merge` gains `--force` / `--backup-dir` / `--no-backup`, `a4dba24`) |
| Backup published as a single-file restore point | A backup that verifies only with its `-wal` beside it but restores empty alone | `db_backup` in `resembl/core.py` (`3d77755`) runs `PRAGMA journal_mode=DELETE` and `wal_checkpoint(TRUNCATE)` on the published copy |
| MinHash pack-time range rejection | A 1-perm or over-cap fingerprint packed into a blob its own reader rejects as implausible, leaving producer and consumer disagreeing about what is storable | `minhash_pack` in `resembl/scoring.py` (`1f210e7`) raises `ValueError` outside 2..4096; `MinHash.__init__` in `resembl/minhash.py` (`9aeab2b`) rejects `num_perm >= 2**32` and out-of-range `hashvalues` |
| Request correlation id | A Go-service request and the engine's handling of it cannot be joined in the operator log | `service/service.go` (`26e95cf`) mints it and `LocalEngine` (`service/http_engine.go`) forwards it as `X-Request-Id`; the Python server adopts a bounded printable caller-supplied id and echoes it (`_correlation_id` in `resembl/server.py`) |
| `RESEMBL_SEED` parsed as decimal | A hex/octal spelling silently changing the sampled fingerprints | `sample_rng` in `resembl/diagnostics.py` (`9aeab2b`, base-10 `int`) and `New`/`newRequestID` in `service/service.go` (base-10 `ParseInt`) |
| Unparseable `RESEMBL_NOW`/`RESEMBL_SEED` fails fast | A typo silently pinning the clock on read/retry paths, or silently falling back to OS randomness | `resembl/models.py` `_retry_sleep` (`ed0705b`) raises `ValueError` like `timestamp_now`/`monotonic_now`; `service/service.go` `New` validates both at startup (`ed0705b`, `isISO8601Instant`); `parse_instant` treats `-00:00` as not-an-instant (`9d6f2f0`); both parse `RESEMBL_SEED` as base-10 only (`9aeab2b`), so `0x…`/`0o…` spellings are rejected |
| Warm-server refusal logged before fallback | A server that only ever answered with `200 {"error": ...}` failing silently in the operator log | `resembl/cli.py` `_server_request` (`1f06887`) logs the envelope at WARNING before returning `None` for the in-process fallback |
| Go service drains on shutdown | A SIGTERM/SIGINT mid-request orphaning custody slots and engine calls | `service/cmd/resembl-service/main.go` (`1f06887`) `signal.NotifyContext` + `http.Server.Shutdown` with a 30s bound; `ListenAndServe` returns `http.ErrServerClosed` on drain |
| Go engine idle connections retired on shutdown | Pooled engine sockets held until process exit, accumulating when servers are built/discarded in-process | `service/cmd/resembl-service/main.go` (`9aacafb`) `engine.CloseIdleConnections()` after drain and on construction failure |
| Caller-supplied X-Request-Id adopted end to end | A per-hop minted id breaking the investigative trail from a service log line to the engine records that explain it | `_correlation_id` in `resembl/server.py` (`26e95cf`, `_MAX_REQUEST_ID_CHARS=64`, printable-only, fallback to minted id), forwarded by `LocalEngine` in `service/http_engine.go` from the request context `service/service.go` mints |
| MinHash pack rejects implausible perm counts | Emitting a blob the reader rejects, leaving producer and consumer disagreeing about what is storable | `minhash_pack` in `resembl/scoring.py` (`1f210e7`, 2..`MAX_NUM_PERM`), with `ResemblLSH.insert`/`insert_batch` in `resembl/lsh.py` skipping such a fingerprint with a warning |
| Capped `integrity_check` read in verify-backup | A heavily corrupt backup reporting one row per damaged page and `fetchall()` materializing the whole list | `resembl/core.py` `db_verify_backup` (`eed3273`) reads at most three rows, the number ever rendered |
| LSH metadata read is fail-closed | A transient `database is locked` read must not trigger a rebuild that overwrites a live index's metadata mid-build | `lsh_meta_get` in `resembl/lsh.py` returns `None` only for a missing table; any other `OperationalError` is logged and re-raised |
| CSV export formula guard covers Unicode leading characters | A formula hiding behind any Unicode space, BOM, or invisible character is as live in a spreadsheet as a bare `=1+1` | `_csv_safe` in `resembl/cli.py` strips whitespace and BOM/zero-width characters before checking `_CSV_FORMULA_PREFIXES` (`e6314c0` covers interleaved BOM/whitespace) |
| YARA export escapes code points above U+00FF and C1 controls | A hex escape on a surrogate or high code point parses as two hex digits plus literal text, corrupting the emitted pattern; a C1 control (U+0080-U+009F) passed through raw and was written to the rule file as its two UTF-8 bytes, the same raw-control leak the C0 escapes exist to stop | `_yara_string_escape` in `resembl/core.py` writes UTF-8 bytes for code points above U+00FF and `?` for lone surrogates, and escapes C1 controls (`429f11d`) |
| Engine error detail stays in the operator log | A caller must not learn internals the engine puts in its `error` field, while operators must see why a request failed | `LocalEngine.find` in `service/http_engine.go` folds the engine's `error` into its error; `service/service.go` logs it and answers the caller only `engine unavailable` |
| Generic 500 body, details to log | `resembl/server.py:1525` | B1 error-text disclosure of SQL, bind parameters, and paths |
| Response hardening headers | `_respond` in `resembl/server.py:1712` | Browser reinterpretation of a snippet-derived response; caching of that response. Every response, including an unhandled verb (`resembl/server.py:1209`) and a stdlib error (`resembl/server.py:1197`), goes through this block |
| Handler-thread bound, 96 | `resembl/server.py:121`, `resembl/server.py:1872` | A stalled-client flood accumulating unbounded handler threads; excess connections stay in the listen backlog |
| Handler-slot wait timeout, 30s | `_HANDLER_SLOT_TIMEOUT_SECONDS` in `resembl/server.py` (`7c10336`), `process_request` | A stalled handler wedging the accept loop in an unbounded acquire, so `serve_forever` and `shutdown` never run; a timed-out slot closes the queued connection instead |
| Result-cache release on server close | `_result_cache_release` in `resembl/server.py` (`7c10336`), `server_close` | A stopped generation's payloads (up to 32 MiB) outliving the engine that produced them in the process-wide cache until LRU eviction |
| Result-cache version guard is the counter alone | `_db_version` in `resembl/server.py` (`d190380`) | A replaced database file restarting `data_version` at the same value and serving payloads computed for the previous corpus — the old file-fingerprint pair never caught it either, so a replaced file under a running server now requires a restart |
| Correlation id adopted only when bounded and printable | `_correlation_id` in `resembl/server.py` (`26e95cf`, `_MAX_REQUEST_ID_CHARS = 64`), `requestIDFromContext` in `service/service.go`, `LocalEngine.find` in `service/http_engine.go` | An unbounded or control-character-bearing `X-Request-Id` inflating the response or forging log lines; the Go service's minted id is forwarded so one id names a request across both hops |
| Log control-character stripping | `_LOG_CONTROL_CHARS` in `resembl/server.py` (`709d31b`), `safeLogPath` in `service/service.go` | A crafted request path/method forging log lines (log injection) via CR/LF in `logger.exception`/`log.Printf` output and the 404 `unknown path` body |
| Bounded client response reads, 16 MiB | `_MAX_RESPONSE_BYTES` in `resembl/find_client.py`, `_MAX_SERVER_RESPONSE_BYTES` in `resembl/cli.py` (`709d31b`) | A port-file swap or compromised loopback server pinning unbounded memory via `response.read()` with no size |
| Loopback proxy bypass | `ProxyHandler({})` in `resembl/cli.py` (`456e464`) and `resembl/find_client.py`, opened via `OpenerDirector.open` (`599923a`): passing the opener as `urlopen`'s context left the default opener's env `ProxyHandler` in charge | A proxied loopback request failing or sending the corpus query to a third party via `http_proxy` |
| On-disk manifest RecursionError guard | `_export_manifest_read`, `_db_backup_reuse`, `db_verify_backup` in `resembl/core.py` (`99c4f3a`) | A deeply nested manifest aborting export/backup/verify instead of being treated as unreadable |
| Loopback bind 127.0.0.0/8 | `_is_loopback_bind` in `resembl/cli.py` (`709d31b`) | A loopback bind (127.0.0.2, 127.1.2.3, …) misclassified as non-loopback and demanding `--allow-unauthenticated`, diverging from the server's own Host allow-list |
| Thread timeout, 30s | `resembl/server.py:1104` | Connections parked open by an idle client |
| Engine pool, 32 + 64 | `resembl/server.py:2027` | Pool exhaustion; the overflow is what a client that outruns requests consumes first |
| Slow-request WARNING at default level | `_SLOW_REQUEST_SECONDS` in `resembl/server.py:923`, `resembl/server.py:1790` | A warm server that has stopped being warm leaves no other default-level trace |
| Double-serve check | `serve` in `resembl/server.py:2007` | Two servers advertising one database |
| Atomic file publication | `resembl/server.py:2125`, `resembl/config.py:245`, `resembl/core.py:2279` | Partial-file reads by a concurrent client |
| Durable file publication (fsync) | `resembl/core.py:126` (`_sync_path`), `resembl/core.py:147` (`_sync_dir`) | An OS crash after rename leaving a zero-length or renamed-away backup/export at its published name; `_sync_path` fsyncs the file before publish and `_sync_dir` fsyncs its directory entry after rename, failures ignored on filesystems that refuse them |
| Cache directory created `0o700` | `serve` in `resembl/server.py:2116` | Another local user reading or writing the port file, for a directory the server itself creates |
| Port file created `0o600` | `resembl/server.py:2125` | The same, for the file inside it. Neither checks a directory that already existed |
| Fingerprint magic check | `minhash_unpack` in `resembl/scoring.py:1014` | Deserialization of attacker-controlled bytes from a hostile database or merge source |
| Legacy cache files never deserialized | `resembl/cache.py:8`, `resembl/cache.py:316` | Code execution from a planted cache file |
| Backup filename percent-encoded before SQLite URI | `resembl/core.py:2129` | A `?`/`#` in a backup filename injecting SQLite URI params (e.g. `?mode=rwc`) and escaping the read-only mode |
| Go container runs unprivileged | `service/Dockerfile` (`FROM scratch`, `USER 65532:65532`) | A compromised service process holding root in the image |
| Service image ships license text | `service/Dockerfile` (`COPY LICENSE NOTICE /licenses/`, `7ea24f8`) | The GPL-3.0-only grant in an OCI label alone did not accompany the binary with its license text |
| Docker build context exclusions | `.dockerignore` (root; `service/.dockerignore` mirrors it) | Local databases, benchmark data, and build output shipped in the service image context (`docker build -f service/Dockerfile .` uses the repository root as context, so Docker reads the root file) |
| YARA string escaping | `resembl/core.py:989` | A snippet name breaking out of a generated rule string |
| Config value coercion and range check | `resembl/config.py:157`, `resembl/config.py:74` | A hand-edited config putting a raw TOML type or an out-of-range value into a numeric field |
| `LIKE` metacharacter escaping | `resembl/cli.py:726`, `resembl/models.py:180` | A checksum prefix like `%` resolving to an arbitrary snippet |
| Password masking | `resembl/paths.py:63` | Credentials in a printed URL |
| Config file lock | `resembl/config.py:185` | Lost updates between concurrent CLI processes |
| Dependency CVE floor | `pyproject.toml:34` | The Pygments ReDoS advisory |
| CI static analysis and dependency review | `.github/workflows/codeql.yml:29` (`language: [ 'python', 'go' ]`), `.github/workflows/dependency-review.yml:50` (`fail-on-severity: moderate`) | Python and Go SAST and dependency review of `main` and of pull requests; the Go tree in `service/` also gets `gofmt`/`go vet`/`go test -race` via `.github/workflows/service.yml` (`make service-check`). None of the SAST or dependency review runs on a release tag (R11) |
| Release SBOM | `.github/workflows/sbom.yml:9` | An inventory of the runtime graph attached to the build, on `main` and on `v*` tags |
| Merge error text stays in the operator log | A hostile or misconfigured source must not leak host, file-path, or SQL detail through the merge error | `db_merge` in `resembl/core.py` (`c1eb6cb`) logs with `logger.exception` and returns only a stable message naming the masked source |
| Port-file hash upgraded to SHA-256 | A credential-carrying URL must not be content-addressed with deprecated SHA-1 | `server_port_path` in `resembl/paths.py` (`c1eb6cb`); stale SHA-1 port files are ignored |
| Backup config sidecar cross-check | A backup that promised tuned config must not verify without it | `db_verify_backup` in `resembl/core.py` (`8616290`): when `config_sha256` is present in the manifest, the `<backup-stem>.config.toml` sidecar must exist and hash to it |
| Go Authorizer gate | service/service.go:244 | B9 caller authentication; fails closed on error or missing authority |
| Lambda event adapter caps | `service/internal/lambdaproxy/proxy.go` | B9 Lambda input/output bound (8 MiB in, 4 MiB out), duplicate-header and malformed-event rejection; gateway identity claims never treated as a grant |
| TokenAuthority constant-time compare | service/token_authority.go:54 | B9 credential verification |
| Custody slots channel | service/service.go:261 | B9 concurrency bound, busy 503 on overflow |
| Loopback engine enforcement | service/http_engine.go:29 | B9 engine must be loopback |
| Pinned index digest and writer fence | `_handle_find_pinned` in `resembl/server.py:1529`, `resembl/pinned_snapshot.py:9` (`_snapshot_value` raises `ValueError` on a non-finite `REAL`, serializes `bool` as integer) | B9 index identity and writer fence; bounded scan; a corrupt stored value is a clean refusal, not a `json.dumps` leak |
| `serve` fails startup on malformed config | `_find_defaults_load` in `resembl/server.py` via `_read_config_dict` in `resembl/config.py` (`ConfigError`) | A malformed `config.toml` silently discarding tuned thresholds so every served find answers a different result set than the operator configured |
| Backup manifest sidecar traversal guard | `db_verify_backup` in `resembl/core.py` (`4678fd1`) | A forged manifest `config`/`runtime_manifest` value making `verify-backup` read/hash an arbitrary file outside the backup directory |
| Backup runtime sidecar cross-check | `db_verify_backup` in `resembl/core.py`, `_db_backup_runtime_read` / `_write_sidecar` in `resembl/core.py` | A backup that promised a runtime manifest must not verify without it; a host loss that took the manifest otherwise leaves `/find-pinned` unrestorable |
| Scheduled backup and restore drill | `make backup` / `make backup-drill` in `Makefile`, `docs/restore.md` | A silent gap where no backup job ran and restore was never exercised; both fail loudly when `BACKUP_DIR` is unset |
| Runtime manifest verification | resembl/runtime_attestation.py:20 | B9 file integrity of loaded code under a trusted supervisor |
| Release provenance | `.github/workflows/build.yml:102` (the `attest` job) | An artifact published under this project's name that this repository did not build |

### Threats with no mitigation

Ranked as in the summary. R13, R14, R15, and R16 are the Go service gaps: R13 and R14 have no mitigation beyond the loopback isolation that service/README.md requires, R15 has none beyond the loopback default, and R16 is mitigated only by operator repair of custody slots. R10 (unbounded
`--file` read) has no control at all. R3 (unauthenticated port file) is
narrowed but not closed: the standalone client refuses an out-of-range
port (`6406aee`, 1-65535) and the in-process `find` client refuses a
port outside the ephemeral 1024-65535 range (`cdabd39`), while an
in-range forged port still reaches an attacker listener on both. R1's `top_n` cap bounds one response
but not a sequence of them, R2's is a `Host` check that a non-loopback bind
does not get (a non-loopback bind is refused without `--allow-unauthenticated`,
`6cbddc6`, so the check's absence only matters when the operator explicitly
allows it), and R4's is a log level the operator has to ask for. R5
and R6 are accepted by design and documented in `SECURITY.md` rather than
enforced.
R3's server-side `0o700` closes the case where `serve` created the cache
directory itself and leaves open the case where something else created it
first, which is the case a shared cache directory actually is, and the
clients' port-range checks (`6406aee` / `cdabd39`) close only the
out-of-range and privileged-port sub-cases of the forged-port attack. R6's
recomputed fingerprints are a control, and its error path now masks the
source URL (`0afeba7`) and keeps driver text in the operator log
(`c1eb6cb`), so the B6 failure-path leak is closed.

R11 and R12 have no mitigation and no code that could carry one: R11 is a
workflow-trigger gap and R12 is a repository setting. Both are recorded here
so a maintainer reading the summary sees them next to the code risks, and
neither is counted as mitigated by anything in the table above.

### Single points of failure

The port file carries discovery, redirect trust, and lifecycle for the whole
warm-find path. One file, one parse, three responsibilities, no integrity
check on any of them.

The config file carries every match parameter. One unvalidated directory
override (R5) changes what the tool reports, with no signal in the output
that a non-default configuration is in force, and `RESEMBL_SEED` and
`RESEMBL_NOW` reach the same result by a route that never touches a path at
all.

The result cache (`resembl/server.py:618`) is the only thing standing between
a repeated expensive query and repeated full work. Its SQLite-only version
guard — the bare `data_version` counter since `d190380` — means a
non-SQLite deployment gets no caching at all and no
cross-process invalidation, and a database file replaced underneath a
running server is served stale until the server restarts. A key that is not cached yet is computed once
however many requests are waiting for it: the per-key compute lock
(`resembl/server.py:121`) collapses a burst of identical queries onto one
find, so a caller cannot multiply the work by opening connections, and the
lock registry it uses holds only the keys currently being computed, not every
query ever asked for.

## Abuse cases

Documented scenarios, each with the code path that enables it. Nothing here
was executed or tested against a running server.

**Corpus exfiltration by a local process.** A process on the same host reads
`~/.cache/resembl/server_<hash>.port`, POSTs
`{"query": "mov eax, 1", "threshold": 0.0, "top_n": 1000}` to
`127.0.0.1:<port>/find` under a `Host` of `127.0.0.1:<port>`, and receives
the first 1000 rows; it repeats with varying queries to page out the rest.
Nothing in the request path distinguishes this from `resembl find`. Enabled
by the absence of caller authentication and by a per-request cap rather than a
per-caller one (`_MAX_TOP_N` in `resembl/server.py:662`).

**Query text capture.** The same process, or any process that can write the
cache directory, replaces the port file's contents with a port it is
listening on. Every subsequent `resembl find` ships its query, which is
attacker-supplied disassembly, to that loopback listener. The standalone
client `resembl-find` refuses a port outside 1-65535 before dialing
(`6406aee`, `_main` in `resembl/find_client.py`), so naming an unbindable
port no longer works there; the in-process `find` client refuses a port
outside 1024-65535 (`cdabd39`), so naming a privileged service port no
longer works there; a forged port inside the admitted range still reaches
an attacker listener on both. Enabled by
`resembl/find_client.py:118`, `resembl/cli.py:429`, and the hard-coded
loopback dial at `resembl/find_client.py:152`.

**Result amplification.** `threshold` down, `top_n` at its 1000-row cap,
repeated. Each response is a fresh 1000-row page, served from a
`ThreadingHTTPServer` with no request accounting.

**Browser pointed at the port.** A page on the host issues a verb the handler does not implement. The handler answers the JSON `405` envelope through the same `_respond` path as every other response (`resembl/server.py:1209`,
`resembl/server.py:1712`), with the hardening headers intact; earlier revisions of this model recorded the stdlib's header-less HTML `501` here, and the code no longer emits it. The `Host` check on a loopback bind still refuses the names a rebound page carries (`_host_allowed` in `resembl/server.py:1301`).

**Resource exhaustion through import.** Pointing `resembl import` at a
directory holding a very large number of `.txt` files drives one worker
process per CPU over the whole set, with the path list resident for the run.
The tool's own warning is a confirmation prompt, not a bound, and `--force`
removes it (`resembl/cli.py:898`).

**Whole-corpus export by a local invocation.** `resembl list` prints every
snippet in the corpus to stdout unless `--range` is given, and
`resembl verify` counts the corpus end to end. Redirecting stdout is the
whole of the export, and neither command reports that it is doing more than a
screen's worth of work.

**Port-file forgery against a shared cache directory.** Two local users share
a cache directory that one of them, or an operator, created with a permissive
mode. Neither `serve` nor `find` reads the directory mode back, so the
`0o700` at `serve` in `resembl/server.py:2116` never applies and the second
user's forged
port file is read as if the first user had written it.

**Silent configuration redirection.** Setting `RESEMBL_CONFIG_DIR` for a
`resembl` invocation changes `lsh_threshold`, `ngram_size`, and
`num_permutations`. The tool does not report that it is running under a
non-default configuration, so match results change with no visible signal.
`RESEMBL_SEED` reaches the same place without touching a directory at all.

**Poisoned merge.** A source database with plausible rows and unusable
fingerprints has them recomputed from its own code
(`db_merge` in `resembl/core.py:2770`). The corpus now contains code the
operator never reviewed, ranked by similarity alongside genuine findings.

**Merge error as an information leak (closed).** Pointing `merge` at a
URL whose host answers with a slow or hostile handshake makes the driver
raise; earlier revisions returned that raw text to the caller. `db_merge`
now logs it and returns only a stable message naming the masked source
(`c1eb6cb`, `db_merge` in `resembl/core.py`), so a hostile source cannot
leak host, file-path, or SQL detail through the merge error.

Token holder reuse. A holder of the Go service bearer token (service/token_authority.go:54) can query the whole pinned index repeatedly; the token grants all holders the same access, is read once at startup, and cannot be revoked short of restart, so a leaked token is a standing credential with no per-caller audit trail.

Custody slot exhaustion. A client that triggers uncertain backend outcomes can permanently consume custody slots (service/custody.go:24), denying service until an operator repairs them after verifying the engine stopped.

A scenario that a previous revision of this model named, **warm-server
growth by cycling `num_permutations`**, no longer holds: such a request is
refused with a `400` (`resembl/server.py:502`), and the per-count MinHash
template cache is capped at eight entries (`resembl/scoring.py:72`) even for
callers that reach it in-process.

## Response readiness

`resembl serve` records every request at DEBUG (`resembl/server.py:1760`,
`resembl/server.py:1776`), which is below the default level, so a server left
running as shipped has no trail to investigate from after a suspected
exfiltration. Errors are logged with stack context at any level
(`resembl/server.py`) and a request slower than 1s at WARNING
(`resembl/server.py`); successful queries are logged only under
`serve -v`. A caller-supplied `X-Request-Id` adopted end to end
(`26e95cf`, `_correlation_id` in `resembl/server.py`) lets an operator
pivot from a Go service log line to the engine records that explain it
when the caller supplied one, but does not add a default-level
per-request record (R4).

The reporting channel itself is not ready. `SECURITY.md` directs a reporter
to GitHub's private vulnerability reporting, and the feature is not enabled
on this repository, so the Security tab offers no such option; the only
working route is the public issue tracker the same document forbids for an
unfixed vulnerability. That is R12, and it is a repository setting rather
than code.

No repository document describes the path from a reported vulnerability to a
shipped fix. `SECURITY.md` states the reporting channel; the release
procedure in `CONTRIBUTING.md:296` is a maintainer checklist that nothing
ties a report to.

## Related

- `SECURITY.md` for the reporting channel and supported versions.
- `docs/http_api.md` for the endpoint contract, including the `501` a `--stateless` server answers on a database path.
- `docs/adr/003-checksum-as-pk.md`, `docs/adr/004-database-backed-lsh-index.md`,
  `docs/adr/005-vendored-minhash.md` for the decisions that removed pickle
  deserialization from the data path.
- `docs/adr/006-paths-module-owns-environment.md` for the decision behind
  B3, B5, and B7, the environment surface that R5, R10, and the port-file
  risks all sit on.
- `docs/adr/002-sqlite-primary-store.md` for the default backend, which
  decides where the corpus and the result cache live.
