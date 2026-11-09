# Changelog

All notable user-visible changes to resembl are recorded here.  The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `db_backup` now logs a warning when the backup directory shares the live database's filesystem device (`st_dev`): a co-located backup protects only against a deleted file, not instance/disk loss, and the silent downgrade is now visible before the disaster.  `make backup-drill` now walks newest-first and proves the newest backup that *passes* `verify-backup`, so a corrupt newest copy cannot block the drill while an older good copy sits unproven; `docs/restore.md` now states the drill covers SQLite only and gives the DuckDB/PostgreSQL/MySQL scratch-copy procedure.  The two tracked `.resembl-pre-delete-backups/*.json` manifests that shipped before the gitignore existed are untracked from the tree.
- `make dist`'s wheel normalization now runs on the uv-managed interpreter instead of the host's bare `python3`, so a build machine without a pinned Python (or with a different one) no longer produces wheel bytes that differ from CI.
- `make sbom` output is now reproducible: the CycloneDX inventory's random `serialNumber` UUID and wall-clock `metadata.timestamp` are normalized from `SOURCE_DATE_EPOCH` and the lockfile digest, so two runs of the same commit produce identical bytes and a diff between releases shows a real dependency change.
- `resembl serve --port` outside 0-65535 now exits 2 with a clean usage message instead of dumping a traceback and exiting 1: the socket layer raises `OverflowError` for an unbindable port, which is not an `OSError`, so the old handler missed it.
- `resembl serve` now fails startup when `config.toml` exists but is malformed or unreadable instead of binding with built-in defaults: `load_config` keeps reporting and defaulting for interactive commands, but a warm server answers every `find` from the config, so a silently discarded tuned threshold would serve a different result set than the operator configured.
- `make check`'s type gate passes again: the five-column content-hash query in `resembl/core.py` carries a scoped `# type: ignore[call-overload]`, since SQLModel's generated `select` overloads cap at four columns and a 5.0 release of SQLAlchemy broke the implicit fallback the tree relied on.
- The clispec schema now declares `serve` as `idempotent` instead of `non_idempotent`: a second start for the same database reports the already-running server and leaves the database unchanged, so a retry is safe.
- CI now lints `benchmarks/` in the pre-commit hook as well as in the Makefile and the pylint workflow (the hook had been left on the old scope when the benchmark scripts moved out of `tests/`, so a finding there could pass locally and fail remotely); the SBOM workflow validates its JSON with the uv-managed interpreter instead of the runner's bare `python3`; and the uv installer fetch refuses non-HTTPS redirects and retries transient failures. No behavior change for contributors beyond the hook now covering `benchmarks/`.
- The clispec schema now declares `clean` in its `confirmation_required` error and adds its `--force` confirmation bypass: the CLI already required a terminal (or `--force`) for `clean`, but the schema document omitted it. The schema also now states that `merge` and `collection delete` proceed without a confirmation prompt, matching their established unattended behavior.
- The Go service's custody claim no longer fails when the platform cannot fsync a directory handle (Windows): the best-effort directory sync now matches the Python `_sync_dir` contract instead of returning the sync error and dropping the slot.
- The `resembl-service` container image now carries the `LICENSE` and `NOTICE` text at `/licenses/`: the image previously shipped only the binary with the GPL-3.0-only grant in an OCI label, so the license text did not accompany the copy as GPL-3.0 requires. `service/.dockerignore` now mirrors the root `.dockerignore`'s cache, docs and test exclusions, so the two contexts cannot drift again.
- The `resembl-service` image now also carries the `org.opencontainers.image.url` label (the repository URL) alongside `source`, so registries and scanners can resolve the project homepage from the image metadata alone.

### Added

- `resembl serve` now honors `RESEMBL_SERVE_ALLOW_UNAUTHENTICATED` as an environment alternative to `--allow-unauthenticated` for a non-loopback bind, and every command honors `RESEMBL_LOG_LEVEL` (an explicit `-v`/`-q` wins; an invalid value aborts the command) — so systemd/container deployments can configure the bind authorization and log level without rewriting the unit file when only the environment changes. `config.toml.example` in the repository root now documents every tuning key with its range and default.
- `make test-one TEST=<file-or-nodeid>` runs one test file or one test via the Makefile (e.g. `make test-one TEST=tests/test_cli.py::TestCLIConfig::test_config_unset`); without `TEST` it names the missing variable instead of running the full suite. Extra pytest flags pass through as `PYTEST_ARGS`.
- `resembl backup` now also copies the `RESEMBL_RUNTIME_MANIFEST` file (when set) verbatim to a `<backup-stem>.runtime-manifest.json` sidecar beside each SQLite backup (mode `0600`), records its SHA-256 in the manifest as `runtime_sha256`, and `verify-backup` cross-checks it — a missing or mismatched sidecar fails verification. A host loss that took the manifest previously left `/find-pinned` unrestorable even when the corpus restored cleanly; see `docs/restore.md`.
- `make backup BACKUP_DIR=<off-host-dir>` schedules the operator backup and `make backup-drill BACKUP_DIR=<off-host-dir>` proves the newest backup restores (scratch copy + `verify-backup` + `.restore-drill.log` record); both fail loudly when `BACKUP_DIR` is unset, closing the silent gap where no backup job ran and restore was never exercised. See `docs/restore.md`.
- `.resembl-pre-delete-backups/` is now ignored by git, and `docs/restore.md` calls out scheduled off-host backups and quarterly restore drills. The pre-delete backup directory held tracked corpus copies and WAL/SHM sidecars `verify-backup` rejects; the ignore keeps a backup that re-enters the tree from shipping corpus data with the repo.
- `rm`, `clean`, and `collection delete` now take a verified pre-delete backup (`db_backup_for_delete`) before deleting; pass `--no-backup` to opt out. A failed backup aborts the deletion instead of proceeding unverified, closing the foot-gun where a destructive command had no restore point.
- A caller-supplied `X-Request-Id` is now adopted end to end: the Go query service forwards the id it minted for the client to the Python engine (`LocalEngine` sets the header from the request context), and `resembl serve` adopts an incoming id (bounded to 64 chars and printable-only, so a log-forging or unbounded header falls back to a minted one) instead of reminting per hop. The Go service's logs, the engine's log records, and the `X-Request-Id` response header now quote one id for a request, so an operator can pivot from a service log line to the engine records that explain it. A client-caused engine rejection (4xx) is now logged at the Go service too, instead of being silent there.
- Two new fuzz targets cover the remaining untrusted-input readers: `fuzz_stored_metadata.py` drives the merge-source `names`/`tags` JSON readers (`_string_list_or_none` / `_source_lists_or_none`) with corrupt, deeply nested and non-list documents, and `fuzz_runtime_manifest.py` drives the `/find-pinned` runtime-manifest reader (`verify_runtime`) with malformed, truncated and deeply nested manifests. Run them with `make fuzz FUZZER=fuzz_stored_metadata.py` (or `fuzz_runtime_manifest.py`); `make fuzz` and the CI smoke run pick them up automatically.

### Changed

- The benchmark scripts moved from `tests/` to the top-level `benchmarks/` package (`benchmarks/benchmark.py`, `benchmarks/benchmark_scale.py`, `benchmarks/generate_data.py`): they are standalone drivers, not tests, and living in `tests/` made the suite import them as `tests.benchmark`. Run them with `uv run --locked python benchmarks/benchmark.py` (or `benchmark_scale.py` / `generate_data.py`) instead of the old `tests/` paths.
- **Breaking:** `resembl serve` now refuses a non-loopback `--host` unless `--allow-unauthenticated` is given; it previously only printed a warning and bound the unauthenticated corpus to the network. Scripts and operators that launch `serve --host 0.0.0.0` without the flag now get exit code 1 instead of a running server. The Go `resembl-service` likewise now requires `-token-file` for a non-loopback `-listen`.
- **Breaking:** `snippet_list` now caps an unbounded call (`end=None`, no explicit `limit`) at 1000 rows so it cannot materialize the whole corpus (the `code` column included) in memory. Calls that relied on getting more than 1000 rows back now need `limit=None` explicitly with `end=None` for a full unbounded list, or `Snippet.stream_all` for a bounded-memory full scan. The unbounded and capped listings also return lightweight `Snippet` objects with empty `code` and `minhash` (only `checksum` and `names` are loaded); callers that need the code use a windowed call (`end` given), `Snippet.get_all`, or `Snippet.stream_all`.
- **Breaking:** `resembl.minhash.MinHash(hashvalues=...)` now raises `ValueError` for a value outside `[0, 2**32)` instead of silently truncating it on pack. Callers that passed larger values and relied on the truncated fingerprint now get an exception.
- **Breaking:** `monotonic_now` now raises `ValueError` for an unparseable `RESEMBL_NOW` instead of silently pinning every duration to `0.0`. A typo in `RESEMBL_NOW` now fails fast on any read path, not only on a stamping write; fix the env var to a valid ISO 8601 instant or unset it.
- `resembl/server.py`: scheme ids are no longer cached. The old `lru_cache` keyed on the canonical JSON of attacker-controlled parameters (`normalize`, `bands`, `rows`), so a client cycling them could grow process memory with the number of distinct parameter sets; each id now costs one probe fingerprint per request and retains no state. The serve result cache's version guard is now the `PRAGMA data_version` counter alone: the file-fingerprint pair did not catch a replaced database file either, so the pair was no stronger than the counter; a database file replaced under a running server requires that server to restart.
- **Breaking:** `resembl backup` now copies the live `config.toml` verbatim to a `<backup-stem>.config.toml` sidecar beside each SQLite backup (mode `0600`), records its SHA-256 in the manifest as `config_sha256` (`null` when no config existed), and `verify-backup` cross-checks the sidecar: a missing or mismatched sidecar fails verification instead of passing on the database copy alone. Backups written before this change have no sidecar; re-running `backup` writes a fresh one. A restore can now recover tuned config, not just the corpus; see `docs/restore.md`.
- **Breaking:** The `serve` port-file name now hashes the database URL with SHA-256 instead of SHA-1; stale `server_<sha1>.port` files from older versions are ignored, so a running server and any `find` client must be restarted once after upgrading (the client and server compute the name independently and will not discover each other across the rename).
- **Breaking:** `resembl serve` now answers `413` with the `{"error": "request too large"}` envelope to a request body larger than 8 MiB, matching the Go service and the Lambda proxy for the same cap; it previously answered `400`, indistinguishable from a malformed body. Clients that branch on `400` for oversized bodies must now handle `413`.
- **Breaking:** `cfg_similarity` now rejects non-integer counts and block sizes (NaN, infinity, bools, floats) with `ValueError` instead of returning `NaN` for a `NaN` block count, and clamps the cosine term so float rounding can no longer push the score just above `1.0`. Callers that passed fractional or non-finite counts and relied on a numeric result now get an exception.
- **Breaking:** `resembl backup` no longer reuses a retained backup that no longer verifies: a corrupt on-disk copy whose manifest hash still matches the live content is dropped and a fresh self-verified backup is written instead of reporting success for an unrestorable file; `verify-backup` now fails a backup with a `-wal`/`-shm` sidecar beside it, since a single-file restore would not carry those pages. A backup directory that previously passed verification while holding a WAL sidecar now fails it.
- **Breaking:** `resembl merge` now takes a verified pre-merge backup (`db_backup_for_delete`, SQLite only) before rewriting names, tags, collections, and fingerprint stamps; pass `--no-backup` to opt out. A failed backup aborts the merge instead of proceeding unverified. On non-SQLite backends the backup raises `ValueError` and the merge is refused, so a merge that previously ran on PostgreSQL/MySQL/DuckDB now needs `--no-backup` to proceed.
- **Breaking:** `resembl.minhash.MinHash(num_perm=...)` now raises `ValueError` for `num_perm == 2**32` instead of allocating its hash array and failing later in `minhash_pack` with a bare `struct.error`. Callers that constructed a 2**32-permutation fingerprint now get the exception at construction.
- **Breaking:** `resembl.scoring.minhash_new` now raises `ValueError` for a non-integer `num_perm` (including `bool`) or a count outside `2..4096` instead of delegating the rejection to `MinHash` or a downstream pack. Callers that relied on the old passthrough now get the exception at the call.
- **Breaking:** `RESEMBL_SEED` is now parsed as decimal only: a leading-zero spelling (`0123`, previously a `ValueError` under base-0 parsing) is now a valid decimal seed, and hex/octal spellings (`0x123`, `0o123`, previously accepted) now raise `ValueError` in the Python reader and fail startup in the Go service. A deployment that pinned a hex seed must rewrite it in decimal.
- **Breaking:** `resembl backup` content hash now includes the `minhash` payload. A backup taken before a reindex (new n-gram, new permutation count, or healed blob) no longer matches the live corpus hash, so it is not reused as current; the next `backup` writes a fresh copy instead of reporting success for a stale restore point.
- **Breaking:** `resembl backup` now exits `1` when the published copy fails verification, not only when its manifest could not be written. A script that treated a `verified: false` result as success must now read the exit code.
- The `snippet_version_list` API gains an optional keyword-only `limit: int | None = None` argument to cap the version-history fetch; `None` keeps the historical unbounded behavior, and `limit <= 0` returns `[]`.

### Fixed

- `db_backup` no longer prunes or reuses backups by filename order: retention now sorts by file modification time, so a hand-supplied `assembly-*.db` with a late timestamp cannot make pruning delete the genuinely newest restore point, and the reuse scan finds the newest matching copy instead of a stale one. A retained backup with a `-wal`/`-shm` sidecar is dropped and replaced with a fresh single-file backup rather than returned as verified.
- `pinned_snapshot.index_digest` now rejects a non-finite `REAL` value in the snapshot tables with `ValueError` instead of crashing in `json.dumps` with an encoder-internal message, and serializes a `bool` snapshot value as its integer form so the digest matches what the writer stored. A stored `Inf` threshold is now a clean refusal.
- `minhash_pack` now raises `ValueError` for a permutation count outside 2..4096 instead of emitting a blob that `minhash_unpack` rejects as implausible; `ResemblLSH.insert`/`insert_batch` skip such a fingerprint with a warning like any other unbandable item. Callers that packed a 1-perm or over-cap `MinHash` and relied on the unreadable blob now get an exception at pack time.
- `db_merge` now disposes the source engine when opening its session fails, instead of returning the error with the engine's connection pool still live for the life of the process.
- The Python HTTP server and the Go service no longer log a request path (or method) verbatim: control characters (including CR/LF) are stripped before the path reaches `logger.exception`/`log.Printf`, so a crafted URL cannot forge log lines; the 404 `unknown path` error message strips them the same way.
- `resembl serve`'s loopback guard now uses the full `127.0.0.0/8` loopback test (via `ipaddress`) instead of matching only `127.0.0.1`/`localhost`/`::1`, matching the server's own `Host` allow-list; the `resembl-find` thin client and the `resembl` CLI's warm-server request now bound the response body they read (16 MiB ceiling) instead of buffering it unbounded.
- `db_backup` no longer reports a reused backup as verified without re-verifying it: a backup corrupted after it was written is now replaced by a fresh verified copy instead of being handed back as the newest restore point. `docs/restore.md` now covers the DuckDB file-backed backup gap (stop-process copy, DuckDB 2.0 format pinning) and the `synchronous=NORMAL` OS-crash loss window, and the count-based retention flooding risk for logical-corruption restores.
- `db_merge` now skips a source row whose `names`/`tags` column is a deeply nested JSON document instead of aborting the whole merge: the JSON decoder recurses once per level and raises `RecursionError`, which is not a `ValueError`, so the corrupt-metadata guard now catches it and skips the row like any other unreadable metadata.
- The `/find-pinned` runtime-manifest reader now answers every malformed manifest (deeply nested document, truncated JSON, missing or directory path) with `ValueError` instead of letting `RecursionError` or `OSError` escape as a `503` with internal error text in the log.
- The on-disk JSON manifest readers (`_export_manifest_read`, `_db_backup_reuse`, and the manifest loop in `db_verify_backup`) now skip a deeply nested manifest instead of aborting the export, backup or verification: the JSON decoder raises `RecursionError`, which is not a `ValueError`, so the unreadable-manifest guard now catches it and treats the manifest as unreadable, matching the stored-metadata and runtime-manifest readers. A new fuzz target, `fuzz_manifest_readers.py`, drives all three readers with corrupt, deeply nested and non-shape manifests; run it with `make fuzz FUZZER=fuzz_manifest_readers.py`.
- `db_merge` now converges when two merges of the same source run concurrently: a chunk whose bulk insert hits a primary-key collision from the other merge is rolled back and the merge is re-run once, taking the existing-row path for the now-present checksums, instead of failing with a spurious `IntegrityError` and aborting the whole merge.
- CI runners are now pinned to versioned labels (`ubuntu-24.04`, `macos-15`, `windows-2025`) instead of the floating `-latest` labels, so a GitHub OS migration can no longer move the test platform under a green run; `tests/test_ci_images.py` now fails a workflow that reintroduces a `-latest` label.
- `db_merge` no longer returns raw driver/SQL error text to callers; the exception stays in the log and the return value names only the masked source database, so a hostile or misconfigured source cannot leak host, file-path, or SQL details through the merge error.
- `docs/THREAT_MODEL.md` now records the fsync-before-publish durability controls (`_sync_path` / `_sync_dir` in `resembl/core.py`) and the `.dockerignore` build-context exclusions for local databases and benchmark data, and its header now names the current 4.0.0 release instead of 3.12.0.
- `make install` now names the missing `uv` prerequisite (uv 0.12.13 or newer, with an install link) instead of failing with a bare `uv: command not found`.
- `make dist` no longer stamps the wheel with 1980 when GitHub Actions injects `SOURCE_DATE_EPOCH=0`: the epoch falls through to the HEAD commit time, so CI and local builds of the same commit produce identical bytes (an explicit operator value still wins).
- `parse_instant` / `timestamp_normalize` now treat the ISO 8601 `-00:00` unknown-offset form as not-an-instant (left verbatim) instead of silently stamping it as UTC, so an imported `created_at` with an unknown offset is no longer fabricated into a UTC instant.
- `docs/THREAT_MODEL.md` and `SECURITY.md` now record that `db_merge` masks credentials in its source-URL error path (`0afeba7`) and that `serve` answers `413` for oversized bodies (`2cd23c9`), and correct stale `file:line` references (including two out-of-range `resembl/scoring.py:1315` citations) so the model matches the code it describes.
- The service `Dockerfile` now runs `go mod download` before copying sources, so a source-only edit reuses the cached module layer instead of re-fetching every dependency; `service/.dockerignore` now carries the nested `**` database patterns and states that the root `.dockerignore` is the file Docker actually reads for the documented root-context build.
- The CLI warm-server client now logs a server refusal that arrives as a 200 `{"error": ...}` envelope before it falls back to the in-process path, so a server that only ever refused with envelopes is no longer silent in the operator's log.
- The Go `resembl-service` now drains in-flight queries on SIGINT/SIGTERM (`http.Server.Shutdown` with a 30 s bound) instead of being killed mid-request, so a stopped service no longer orphans custody slots and engine calls.

## [4.0.0] - 2026-11-05

### Fixed

- `resembl-find` (the thin client) now rejects a fractional float for an
  integer config field (e.g. `top_n = 2.9`) and falls back to the default,
  matching the `resembl` CLI's `config` loader; previously it silently
  truncated the value and searched with a different result set.
- Fixed documentation that contradicted the code: `docs/custom_database.md` claimed importing `Snippet` creates only the `snippet` table, when importing `resembl.models` registers and creates all six resembl tables; `docs/api_reference.md` now documents `snippet_search_by_name`, `snippet_name_add`, `snippet_tag_add`, `snippet_export_yara`, and `db_verify`, includes the `quiet` parameter on `snippet_delete`/`collection_delete`, and clarifies that `lsh_cache_save`/`lsh_cache_load` stamp metadata only and never load legacy pickle caches; the README project tree now lists `pinned_snapshot.py`, `runtime_attestation.py`, and `status_colors.py`.

- Trimmed whitespace in database and directory environment overrides and rejected boolean-style values in `config.toml` (including `"true"`/`"false"`) and in `resembl-find`, so a padded `RESEMBL_DATABASE_URL` opens the intended database and a mistyped boolean no longer becomes `1` or `0`.

### Added

- `snippet_search_by_name` now scans the name column in bounded keyset
  pages and stops as soon as the requested number of matches is found,
  so a common pattern no longer materializes the whole table's
  (checksum, names) rows before returning its first page.
- Go service `GET /metrics` now exposes `resembl_service_uptime_seconds` and a `resembl_service_request_duration_seconds` histogram (same buckets as the Python server); failure paths that previously answered `503` without a log record (custody unavailable, busy, capacity claim/release, invalid engine response, response write) now log with the request correlation id.
- Two new Atheris fuzz harnesses: `fuzzers/fuzz_yara_escape.py` covers the YARA export escaping (`_yara_string_escape`) and filename sanitizer (`_export_safe_filename`) with round-trip and traversal invariants, and `fuzzers/fuzz_config_file.py` covers the `config.toml` TOML reader (`_read_config_file`) over raw bytes, truncation, invalid UTF-8, and deeply nested documents. Both run via `make fuzz FUZZER=<name>`.
- `make bench` runs the microbenchmarks in `tests/test_bench.py` with `--benchmark-enable`; they are disabled by default so a plain `make test` never measures them.

### Changed

- Fingerprint stamp reconciliation now batches stamp reads in a single
  query rather than three separate lookups per write.
- The Go service now requires Go 1.26 everywhere: `service/go.mod`, `service/Dockerfile`, the Makefile and the docs named 1.25 while the Dockerfile built with 1.26, so a contributor with Go 1.25 got a toolchain switch instead of a named error.
- **Breaking:** Snippet checksums are now taken over a whitespace-collapsed
  normalized string. Before, `mov eax,1` and `mov  eax,  1` tokenized and
  fingerprinted identically but hashed to different checksums, so the same
  snippet was stored twice. Stored rows keep their old checksums (they stay
  findable by fingerprint); re-importing a snippet after upgrading writes it
  under its new checksum instead of deduplicating against the old row, and
  the duplicate appears in `find` until it is deleted by hand.
- The root `.dockerignore` (the context for `service/Dockerfile`, built from
  the repository root) now also excludes local databases and benchmark
  data, matching `service/.dockerignore`: sensitive corpus data stays
  out of the build context even though only `service/` is COPY'd.
- The Go service CI job now smoke-tests the built image by running
  `resembl-service -h` and checking its usage output, so a broken
  entrypoint or COPY path fails in CI instead of on an operator's host.

### Fixed

- A `config.toml` containing invalid UTF-8 or deeply nested TOML now yields the documented empty-config-plus-error answer instead of an uncaught `UnicodeDecodeError` or `RecursionError` traceback: `resembl/config.py:_read_config_file` and the thin client's `_load_config` catch both, matching their never-raise contract.
- The Go service no longer masks a warm-engine `4xx` (bad parameter, index mismatch) as `503 "engine unavailable"`: it relays the engine's status and `{"error": ...}` text, so a client-caused rejection is the caller's `400`/`409` again; engine `5xx` and transport failures still answer `503`.
- The Lambda event adapter now answers its own refusals (bad path/header, oversized body, oversized response) with the same `{"error": ...}` JSON envelope and `application/json` content type as the HTTP service, instead of an empty body.
- The Python server now rejects a boolean or numeric-string `threshold` / `jaccard_weight` (`true`, `"0.5"`) with `400`, matching the strictness the integer fields (`top_n`, `ngram_size`, `num_permutations`) and `/score`'s `jaccard_floor` already enforced instead of silently coercing them via `float()`.
- `docs/THREAT_MODEL.md` now records the Go service `/v1/find-batch` `501` on the pinned binary (the handler's `BatchEngine` assertion against `PinnedSQLiteEngine`, which implements `Find` only), the Go latency histogram and uptime gauges in `/metrics`, the Python `GET /health` database probe, and that the Go tree gets `gofmt`/`go vet`/`go test -race` via `service.yml` plus CodeQL SAST for both Python and Go.
- CLI logging now writes ISO-8601 timestamps (`%(asctime)s %(levelname)s %(name)s %(message)s`) instead of the bare `LEVEL:name:message` default, so log aggregators can parse and order records; unhandled server errors now log with traceback instead of relying on the stdlib default.

- Backups, manifests, config writes, and exports are now fsynced before
  being published (and the directory entry fsynced after the rename), so
  an OS crash cannot leave a half-written backup or export at its final
  name; a backup whose manifest cannot be written now fails instead of
  silently succeeding; `snippet_export` writes each file atomically instead
  of truncating the live export file first. Added `docs/restore.md`, a
  restore runbook covering instance loss, logical corruption, stale WAL
  sidecars, and periodic verify-backup scheduling.
- Backup files are now written mode `0600` (matching their manifests),
  and `resembl backup` exits 1 when the backup's manifest could not be
  written, so a complete-but-unverifiable backup is never reported as
  success; the restore runbook now requires removing stale WAL sidecars
  after the copy and recording each restore drill.
- `docs/http_api.md` and `docs/THREAT_MODEL.md` no longer describe the
  stdlib's header-less HTML `501` as the answer to an unimplemented verb;
  the handler answers the JSON `405` envelope for any unhandled verb
  (`resembl/server.py:1180` and `resembl/server.py:1168`), and the
  threat model now records the 96 handler-thread bound
  (`resembl/server.py:117`) and the Lambda event adapter
  (`service/internal/lambdaproxy/`, R18) instead of the removed claims.
  `SECURITY.md` now points at R13-R18.
- `CONTRIBUTING.md` now matches `AGENTS.md` on trunk-based development
  (commit straight to `main`, branch only for work that must not land
  yet) instead of requiring a feature branch for every contribution;
  the PR checklist no longer references an `upstream` remote or a pull
  request template that does not exist.
- `AGENTS.md` no longer claims the `SBOM` workflow attaches its inventory
  to a release; it uploads a 30-day artifact on `main` and on `v*` tags.
  It also states the actual `pygments>=2.21.0` floor from
  `pyproject.toml` instead of `2.20.0`.
- `resembl backup` reuse is now on the full corpus identity (names, tags, collection, not checksums alone), so an alias/tag change after a backup writes a fresh one instead of reusing the stale copy.
- `resembl backup` now re-verifies the published backup against its manifest (via `db_verify_backup`) before reporting success, and removes an unverifiable copy instead of leaving it as the newest restore point a drill would trust. Backup pruning never deletes the backup just written, even when the directory already holds more than `--keep` files.
- `resembl backup` is now idempotent: re-running it on unchanged content
  returns the existing backup instead of writing a new copy and pruning
  the oldest retained one, so a retried or double-scheduled run can no
  longer drain the retention window and delete a restorable copy.
  Backups written before this change carry no content hash and are
  never reused, so the first run after upgrading always writes a fresh
  backup.
- `resembl backup` now stamps its filename from the same logical clock
  as every other run output (`RESEMBL_NOW`), instead of a second
  hardwired wall-clock read: a replay pinned with `RESEMBL_NOW` now
  reproduces its backup filenames and manifests byte-for-byte, not
  just its database rows. Distinct backups at the same pinned instant
  keep distinct names.
- `AGENTS.md` no longer carries the historical 3.7.0 exception that
  released the Python 3.14 floor migration as a minor; the section now
  states the SemVer rule without that one-time exemption.
- `make service-check` now names the missing Go 1.25+ toolchain and where
  to install it, instead of failing with a bare `gofmt: command not found`
  on a machine without Go.
- `CONTRIBUTING.md` lists `service-check` among the `make help` targets
  and shows the `make test PYTEST_ARGS=...` form of the single-test loop.
- `mypy`, `pylint` and `black` gates pass again: `serve`'s return type
  no longer hides the server's `runtime_generation`, a long `config`
  line is split, and the semaphore acquire in `serve` carries its
  scoped `consider-using-with` disable.

### Changed

- **Breaking:** `score_hybrid` now raises `ValueError` for a non-finite
  input or one outside its domain (Jaccard and `jaccard_weight` outside
  0–1, Levenshtein outside 0–100) instead of returning an out-of-range
  score. Clamp or validate the inputs before calling it; callers that
  relied on the old lenient result now get an exception.
- **Breaking:** the `Found N candidates via LSH.` line in `resembl-find`
  now goes to stderr rather than stdout, so pipes that read stdout get
  only match rows (or the JSON document). Scripts that parsed that line
  from stdout must read stderr instead.
- `resembl.server.serve` accepts `db_url=None` for stateless mode, and
  the `-h` / `--help` short form is now available on the `config`,
  `name`, `tag` and `collection` subcommand groups as well as the top
  level. `resembl-find` accepts `--version`, `--quiet` / `-q` and
  `--verbose` / `-v`.
- `Collection.get_all`, `Snippet.get_all`,
  `Snippet.get_by_collection` and `SnippetVersion.get_by_checksum`
  gained an optional `limit: int | None = None` parameter. Existing
  calls are unchanged.
- The PostgreSQL and MySQL service containers in the test and coverage
  workflows are now pinned by digest, so CI tests against the exact
  database image that was reviewed rather than whatever the tag points
  at on the day. Dependabot's `docker` ecosystem watches those images
  (and the service Dockerfile's) for new upstream releases.
- `packaging` is declared as an explicit dev dependency: the fuzz-extra
  marker test imports it directly and no longer relies on it arriving
  transitively via `black` or `duckdb-engine`.

### Fixed

- `snippet_name_add` and `collection_create` reject an empty or
  whitespace-only name (after stripping and NFC-normalizing). Previously
  an empty alias could be stored and a whitespace-only collection name
  normalized to an empty primary key.
- A fractional value (e.g. `2.9`) for an integer config field is now
  rejected with a warning and the default kept, instead of being
  truncated by `int()` to `2`. Integral floats such as `128.0` remain
  accepted.
- Concurrent `snippet_add` / `snippet_add_batch` / `collection_create`
  calls for the same checksum or name now converge on the existing row
  instead of raising `IntegrityError`.
- A relative `XDG_CACHE_HOME` / `XDG_CONFIG_HOME` is now ignored, per
  the freedesktop base-directory spec, instead of being resolved
  relative to the working directory.
- `resembl collection create` prints the `ValueError` message for an
  empty name and exits 1 instead of propagating a traceback.
- `snippet_list` with a negative `start` and `snippet_search_by_name`
  with a non-positive `limit` now return an empty result instead of
  leaking rows from the start of the table (SQLite reads a negative
  `OFFSET` as 0 and a negative `LIMIT` as unbounded).
- `update_config` and `remove_config_key` now raise `ValueError` for an
  unknown key or a value outside the range `validate_value` allows,
  instead of persisting a misconfiguration the CLI would refuse; a
  present but malformed config file now raises `ConfigError` (an
  `OSError`) instead of being silently overwritten with defaults.
- `snippet_add` dedupes aliases by normalized name, so an alias that
  differs only in case or Unicode composition no longer creates a
  duplicate entry.
- Piped and HTTP text is decoded as UTF-8 instead of the locale
  encoding, and tags and stdin are case-folded and NFC-normalized
  before storage and lookup.
- Degenerate numeric inputs are guarded in the MinHash, config and
  stats paths: negative CFG block sizes raise `ValueError` instead of
  aliasing histogram buckets, and non-finite config values are
  rejected instead of propagating NaN into scores.
- `cfg_similarity` and average-similarity sampling skip stale
  fingerprints instead of counting them as zero-similarity pairs.
- The server now surfaces a startup index-build failure instead of
  serving an empty result set, and startup manifest and custody/release
  failures are no longer silently ignored.
- API error envelopes and method handling are unified across the Python
  and Go servers: unhandled verbs answer the JSON `405` envelope
  instead of the stdlib's header-less HTML `501`.
- The result cache is isolated from caller mutation, the singleflight
  race is fixed, and server threads are bounded with leaked connections
  closed.
- `tzdata` is now installed on Windows only (`sys_platform == 'win32'`)
  instead of on every platform; Linux and macOS continue to read the
  system zoneinfo database.
- Blank environment variables (`RESEMBL_NOW`, `RESEMBL_SEED`,
  `DATABASE_URL`, `RESEMBL_CACHE_DIR`, `RESEMBL_CONFIG_DIR`) are
  treated as unset, and unknown configuration keys are logged as
  ignored instead of silently accepted.
- A SQLite URI injection through a crafted database URL is blocked.
- `db_backup` filenames are stamped in UTC instead of local time, and
  backup verification now checks integrity and content hash against
  the backup's own manifest.
- `resembl-find` bound-checks its config values and rejects a
  non-positive batch size instead of sending an out-of-range value to
  the server.

### Added

- `resembl backup DIR` writes a consistent, restorable copy of the default
  SQLite database (via the online backup API, with a manifest holding the
  snippet count and fingerprint version, pruned to the newest 7 by default),
  and `resembl verify-backup PATH` checks a backup opens, holds every table,
  and matches its manifest. A restore is copying the file back to
  `assembly.db` and running `verify`; RPO is the time since the last backup.
  See `docs/man.md`.
- `resembl serve` answers `POST /score`, which scores one listing against
  listings in the request with the find blend and an optional Jaccard floor,
  and `POST /band-keys`, which returns the LSH band keys of listings in the
  request. Neither reads the database. See `docs/http_api.md`.
- `/score` and `/band-keys` answer a `scheme` object, and `GET /scheme` answers
  the one a `/score` with the server's defaults uses: the fingerprint version,
  hash, permutation seed, `num_permutations`, `ngram_size`, `normalize` and,
  for band keys, `bands` and `rows`, plus an `id` derived from all of them and
  from a probe listing's real fingerprint. Store the `id` with band keys and
  recompute keys whose `id` differs from the server's.
- `resembl serve --stateless` opens no database and writes no port file. It
  answers `/score`, `/band-keys`, `/scheme`, `/health` (`"mode": "stateless"`,
  always `200`) and `/metrics`; `/find`, `/find-batch`, `/find-pinned` and
  `GET /runtime-generation` answer `501`.
- `GET /health` carries `"mode": "database"` on a server with a database.

### Fixed

- The sdist now includes `tests/test_data` and `docs` via `MANIFEST.in`, so
  the README example (`resembl import tests/test_data`) works from an sdist
  install; the wheel is unchanged. `service/Dockerfile` copies `go.sum` and
  `service/internal`, and carries OCI labels for source and license.

## [3.12.0] - 2026-11-04

### Added

- `resembl-service` takes `-concurrency` and `-timeout` flags and requires
  `-resource`. The flag reference is in `service/README.md`.
- `docs/http_api.md` documents `POST /find-pinned` and `GET /runtime-generation`,
  and `docs/man.md` documents `RESEMBL_RUNTIME_MANIFEST`.
- `make service-check` and a `Go service` workflow run gofmt, `go vet` and
  `go test -race` on `service/`.

### Changed

- `resembl-service` exits at startup without `-resource` instead of serving
  under the name `unconfigured`.
- `/v1/find-batch` on the pinned service answers `501` instead of `503`, since
  the pinned protocol has no batch form.

## [3.11.0] - 2026-11-04

### Added

- Add an experimental portable Go query service with an optional scoped service
  credential and pinned SQLite runtime checks; embedded Python interfaces remain available.

### Fixed

- Preserve control flow when a branch or return follows a label on the same line.

## [3.10.2] - 2026-11-02

### Changed

- Refresh dependency locks and use Python 3.15.2 and uv 0.13.6 for development.

## [3.10.1] - 2026-11-01

### Fixed

- Reject malformed packed fingerprint headers during scalar and batch similarity scoring.
- Query large LSH indexes on SQLite without exceeding its compound query limit.

## [3.10.0] - 2026-11-01

### Changed

- Name query and alias performance: normalize query once per lookup and skip NFC on ASCII aliases during lookup, search, and add.

## [3.9.0] - 2026-10-27

### Changed

- Document that, by default, DuckDB before 2.0 cannot read database files
  DuckDB 2.0 creates, and that an existing older-format file keeps its storage
  version.

## [3.8.0] - 2026-10-25

### Changed

- Default normal development, standalone use and release builds to Python 3.15.1.
  Retain Python 3.14 compatibility and the existing Python floor for consumers.
- Run optional Atheris fuzzing on Python 3.15.1 in a separate environment.
  The qualified wheel is Atheris 3.1.0+relumea.2 for CPython 3.15 normal GIL
  on Linux x86_64. It needs GLIBC 2.38 and GLIBCXX 3.4.32. Free-threading,
  PyPy, ARM, macOS, Windows, and Python 3.16 are not qualified: they resolve
  the published sdist from that release instead of that wheel, and
  Python 3.15 does not fall back to unported upstream Atheris. Python below
  3.15 keeps the upstream Atheris package.
- Test normal runtime behavior on Python 3.14 and 3.15, and smoke all fuzz targets
  on Python 3.15.1 with the maintained Atheris binding.

### Fixed

- Validate the MinHash magic before using an otherwise plausible count and length;
  malformed stored fingerprints consistently follow the existing recovery path.

## [3.7.0] - 2026-10-25

### Changed

- **Breaking:** Require Python 3.14 or newer; upgrade older environments before installing.
- Refresh all locked dependencies to their latest stable releases and use Python 3.14 exception syntax.
- Validate equal-length MinHash comparisons with Python 3.14 strict mapping.

## [3.6.1] - 2026-10-24

### Changed

- Shorten repeated LSH and ranking explanations in the README.

## [3.6.0] - 2026-10-21

### Changed

- Select Python 3.14.8 for contributor environments and the current CI lane.
  DuckDB 1.5.6 advertises, tests and publishes wheels through Python 3.14;
  Python 3.15 remains an unqualified source-build path. Retain the 3.13 lane.
- Refresh locked dependencies and the setuptools 84.2.0 build backend; use
  uv 0.13.3 and immutable release commits for CI actions.

### Fixed

- Print `config path` verbatim so long paths remain usable by scripts.

## [3.5.0] - 2026-10-20

### Changed

- Use the shared Relumea terminal palette for human output; preserve no-color and structured formats.

- `resembl schema` and `resembl-find schema` declare `type` on every global
  argument, and a one-character alias as `short`.

## [3.4.0] - 2026-10-18

### Changed

- Refresh contributor environment tooling to virtualenv 21.14.11. Runtime
  dependencies, supported Python versions and command behavior are unchanged.

## [3.3.1] - 2026-10-18

### Changed

- Refresh locked runtime and development dependencies, including SQLModel
  0.0.48 and its newly supported SQLAlchemy 2.1 line. Minimum supported
  dependency versions are unchanged; automated updates can now include
  SQLAlchemy 2.1.
- CI uses uv 0.13.1 and the current cache action; repository hygiene hooks
  use pre-commit-hooks 6.0.0.
- Database integration and coverage checks use PostgreSQL 18 and MySQL 26.7.

### Fixed

- Windows server discovery preserves the configured database URL spelling, so
  SQLAlchemy URL escaping cannot hide a running server from thin clients.
- CLI startup works in runtime-only installations without a separate Click
  package; error hooks use the exceptions supplied by Typer itself.
- The Black commit hook receives the project path when scanning the whole tree.
- macOS contributor setup selects current GNU Make so recipe pipeline failures
  stop the build; the old system Make ignores those shell flags.

## [3.3.0] - 2026-10-15

### Added

- `resembl schema` prints a clispec 0.3 document for this CLI. It does not
  read the config file and it does not open a database. `resembl-find schema`
  prints the same kind of document for the thin client.

### Changed

- `-o` / `--output` selects `auto`, `text`, or `json` and is placed before the
  subcommand. `auto` stays text when stdout is a pipe. `--format` still selects
  `table`, `json`, or `csv`.
- JSON listings from `list`, `search`, `collection list`, `collection show`,
  `version`, and `find-batch` are one document with `items` and `total`.
- With `-o json`, a failure finishes with one stderr object carrying `kind`,
  `message`, and `hint`. `resembl-find --json` does the same, and `-o text`
  keeps the human line.
- `export`, `export-yara`, `import`, `rm`, and `reindex` ask on a terminal.
  `--force` proceeds when there is no terminal.

## [3.2.1] - 2026-10-14

### Fixed

- Source builds and SBOM generation work when `/bin/sh` is Dash. The
  Makefile now tests whether the recipe shell supports `pipefail` instead
  of assuming support; shells that support it retain pipeline fail-fast.

## [3.2.0] - 2026-10-03

### Fixed

- `resembl --tz <ZONE>` works on Windows. Windows ships no IANA time zone
  database, and the `tzdata` package that supplies one was declared a
  development-only dependency, so every named zone was refused with "is not a
  known IANA time zone" even though the flag is documented for every supported
  platform. `tzdata` is now a runtime dependency; Linux and macOS are
  unaffected, since `zoneinfo` reads the system database first and consults
  the package only when the system cannot answer.

### Changed

- The declared `sqlalchemy` minimum is 2.0.54, the release the lockfile
  already installs. It stays below 2.1, which `sqlmodel` does not allow yet.
- Building from source needs uv 0.12.13 or newer instead of exactly 0.12.13.

## [3.1.4] - 2026-09-29

### Changed

- Dependencies refreshed to their latest releases, with the declared minimums
  raised to match (among them `sqlmodel` 0.0.47, `numpy` 2.5.3, `rich` 15 and
  `typer` 0.27.2), and the build backend pinned to `setuptools==84.0.0`.
  `sqlalchemy` stays on 2.0.x: every `sqlmodel` release so far requires
  `sqlalchemy<2.1`.

## [3.1.3] - 2026-09-29

### Fixed

- The icon and favicon eye rings end in rounded caps at the head outline. In
  3.1.2 their ends stuck out past the silhouette as small white spikes on a
  dark background.

## [3.1.2] - 2026-09-29

### Fixed

- The mascot, icon and favicon have transparent backgrounds, so the README no
  longer shows them as a white square on a dark theme. Only the white around
  the art was removed; the eyes, the icon's line work and the brand green
  (`#10911A`) are unchanged. The two PNGs are palette-optimized, the icon
  from 105 kB to 19 kB and the mascot from 196 kB to 39 kB.

## [3.1.1] - 2026-09-29

### Changed

- Documentation only. The README opener says what `resembl` does, the "How It
  Works" summary names the hybrid score (estimated Jaccard plus Levenshtein)
  that ranks the LSH candidates, and CONTRIBUTING lists four plain principles.
  README, CONTRIBUTING and the API reference, custom database and man page
  guides no longer use em dashes or filler.

## [3.1.0] - 2026-09-28

### Fixed

- Every command writes LF-terminated output on Windows too. The output streams
  were switched to UTF-8 but kept `newline=None`, which rewrites each `"\n"` to
  `os.linesep`, so the same report arrived CRLF-terminated there and
  LF-terminated everywhere else; a script reading it had to know the platform.
- `serve` releases the version probe it keeps per database when the engine
  behind it is disposed. The probe is a second connection, and it used to
  outlive the engine until collection, keeping the database file open: on
  Windows the file could then not be removed or replaced, and an embedded
  caller leaked one handle per server generation.
- Styled output reaches a redirected stream on Windows. rich renders a legacy
  Windows console through the Win32 API whenever its file is a standard stream,
  which a piped stdout is, so the escapes went to the console's screen buffer
  and a log, a file or a test's captured output carried no color at all, even
  with `FORCE_COLOR=1`. The Win32 path is now used only for a console.
- `serve` no longer waits on a reverse-DNS lookup of the address it binds
  before it advertises its port. `http.server` filled `server_name` with
  `socket.getfqdn(...)`, an unbounded resolver call that took 35 seconds on
  a host with a slow resolver, and a `find` client that reads the port file
  saw a server that had not started yet. Nothing reads `server_name`; the
  bound address is what it holds now.
- `export` no longer lets two snippets whose names differ only by case write
  to one file. The guard compared the file names with `os.path.normcase`,
  which only folds case on Windows, while macOS' default volume is
  case-insensitive too, so the second snippet's file landed on the first
  one. The names are folded on every platform now, so a directory exported
  on Linux carries the same file names it does on macOS and Windows.

## [3.0.0] - 2026-09-27

### Fixed

- A snippet name, alias or collection name containing a non-ASCII character
  is now findable. The `names` column is written by `json.dumps`, which
  stores `é` as `é`, so the lookup probe built from the raw
  character matched nothing: `resembl find --name`, `name remove` and
  `collection` operations on an accented name silently reported the name as
  absent. The probe is now built from the column's own encoding, and the
  fetched rows are verified against the decoded names, so `%` and `_` in a
  search pattern no longer match rows that do not contain them.
- Names are stored and compared in one normalization form (NFC). macOS
  hands out decomposed (NFD) spellings of a filename while Linux and Windows
  compose them, so the same `café.asm` reached a database as two aliases
  under `resembl add`/`import`, was exported under two filenames, and merged
  from a macOS database into a duplicate collection. Names written before
  this still resolve: a lookup probes both canonical spellings.
- `export-yara` escapes control characters in a snippet's name and code as
  `\xNN`. A snippet carrying a NUL byte (or ESC, BEL, ...) wrote a raw
  control byte into the rule file, where YARA's C-string literal truncated
  the pattern, so the exported rule matched something the snippet never
  contained. A tab is escaped too.

### Added

- `resembl serve` answers `GET /health` (readiness, one `SELECT 1` against
  the served database) and `GET /metrics` (Prometheus text format: requests
  by path and status, request latency histogram, result-cache hit rate, and
  the per-query failures a `200` `/find-batch` otherwise hides).  A warm
  server had no endpoint a supervisor could probe and no number an operator
  could read.
- Every log record the serve process makes now leads with a per-request
  correlation id, and a request slower than a second is recorded at WARNING
  even without `-v`, naming the id, the path, the status and the elapsed
  time.

- A release now carries a provenance attestation.  Pushing a `vX.Y.Z` tag
  runs a Sigstore-backed attestation over the sdist and the wheel, so
  `gh attestation verify` can tie a downloaded artifact to this repository
  and the commit that built it.  Only a tag run asks for the token the
  attestation is signed with.
- `make sbom` writes `build/sbom.cdx.json`, the CycloneDX 1.5 inventory of
  the runtime dependency graph, from the same `uv export` the `SBOM`
  workflow runs, so the inventory a release is assessed with can be
  produced and read before the tag is cut.
- The test suite runs on macOS and Windows, not only Linux.  Both are
  documented install platforms, and the portability surface they cover
  (path separators and reserved filenames in `export`, `spawn` process
  pools, `msvcrt` config locking, text-mode line endings, the `os.replace`
  port-file publication) was previously asserted in the docs and exercised
  by no CI job at all.
- The wheel and sdist now declare the operating systems they run on
  (Linux, macOS, Windows) and Python 3.14 in their package metadata, the
  platforms the docs describe installing on and the CI matrix runs, so an
  indexer reading the metadata reports what is actually tested instead of
  nothing at all.
- `resembl import` takes a single `.asm` / `.txt` file as readily as a
  directory, so pointing it at one file no longer answers "Directory not
  found".
- `resembl --tz <ZONE>` picks the zone printed timestamps are rendered in
  (`collection list`, `version <checksum>`), e.g. `--tz Europe/Warsaw`.
  Timestamps are stored in UTC and only the display converts, so the printed
  date no longer changes with the host's `TZ`; the default is still the local
  zone.  A fixed offset (`+02:00`) is rejected, since it names one instant of
  the year rather than a zone that follows daylight saving.  `json` and `csv`
  output is unchanged and still carries the stored UTC string.
- `make hygiene` checks the trailing whitespace and the missing final newline
  that the `pre-commit` hooks refuse to commit. No CI workflow ran those two
  hooks, so a green `make check` did not mean the commit would be accepted;
  `make check` now runs the target first.
- `resembl --version` prints the installed version and exits `0`; it is
  answered before the database is opened, so it works against a missing or
  unreachable database URL.
- `resembl find` reads its query from stdin when neither `--query` nor
  `--file` is given, so a snippet can be piped in.
- DuckDB is reachable by users through a new `duckdb` extra
  (`uv pip install "resembl[duckdb]"`).  The compiled driver used to be a
  development dependency only, so a `duckdb:///file.db` URL needed a manual
  install.
- `resembl config set` refuses a value outside the range its setting works
  in, and names the range it wanted.
- `docs/http_api.md` documents the `serve` endpoints: the request fields of
  `/find` and `/find-batch`, the `200` response shapes, and the
  `400`/`404`/`405`/`415`/`500` error envelope.
- Two fuzz harnesses cover the surfaces that parse untrusted input:
  `fuzzers/fuzz_minhash_blob.py` for the stored-fingerprint byte format, and
  `fuzzers/fuzz_find_request.py` for the `serve` request body and find
  parameters.

### Changed

- The threat model and the security policy were re-checked line by line
  against the tree.  Corrected citations that had drifted, and recorded the
  release path honestly: a `v*` tag is not statically analyzed and not
  dependency-reviewed when it is cut, since neither CodeQL nor dependency
  review has a tag trigger, so `docs/THREAT_MODEL.md` now names that as an
  open risk rather than as a control.
- `SECURITY.md` no longer reports GitHub private vulnerability reporting as
  the reporting channel.  The feature is not enabled on this repository, so
  the Security tab does not offer it; the document now says so, and says
  that the published 2.0.0 release predates the `serve` controls described
  in it.
- `docs/man.md` states the mode `serve` gives the cache directory and the
  port file, and that neither is re-checked once created.
- The portability CI job runs on macOS and Windows only.  The Linux job
  already runs the same suite, so a third leg bought no platform while
  lengthening every push and pull request.
- Dependabot watches the GitHub Actions versions the workflows pin.  Each
  action is pinned to a major tag on purpose, and nothing was proposing the
  bump when the next major landed.
- `make dist` clears `build/` and `resembl.egg-info/` before building.
  setuptools stages the wheel in `build/lib`, so a module deleted from the
  source tree survived in the artifact until someone cleaned the directory by
  hand; the sdist is also unpacked under `.scratch/` rather than `mktemp -d`'s
  tmpfs.
- Pylint's `consider-using-with` is no longer disabled for the whole project.
  A file handle or a child process a module opens without a context manager
  now fails the gate; the test modules and the scale benchmark that keep a
  temp file open across a whole test body scope the message to themselves,
  each with its reason.
- Every `uv run` in the `Makefile` and in the `pre-commit` hooks passes
  `--locked`, so no check re-resolves `uv.lock` and rewrites it under the
  contributor when `pyproject.toml` has drifted.  Plain `uv run` gated the
  code against a lockfile no CI run and no other contributor ever saw;
  `uv lock` (or `make install`) is now the way forward.  The commands
  `README.md` and `CONTRIBUTING.md` spell out carry `--locked` for the same
  reason, and a fuzzer run names the `fuzz` extra on the invocation, since
  `uv run` syncs the environment against the lockfile before it starts.  The
  same flag is on every `uv run` in the `Test Suite`, `Code Coverage` and
  `Pylint` workflows, which were the last commands that could still
  re-resolve the lockfile inside a run.
- The `SBOM` workflow runs `make sbom` instead of a second copy of its
  `uv export` line.  The copy had drifted: it lacked `--no-dev`, so the
  inventory attached to a release listed the development dependencies
  (`pytest`, `mypy`, `datasketch`, ...) alongside the runtime graph, while
  its own comment and `CONTRIBUTING.md` both described it as the runtime
  set.  The report also lands in `build/` rather than the repository root.
- The `Code Coverage` workflow runs `make coverage`, the target added for
  it, so the number a contributor reads locally and the one Codecov stores
  come from one invocation; the XML report is named explicitly for the
  upload instead of being left to the action's root-level search.
- `make dist-verify` keeps the sha256 sums of its first build under
  `.scratch/` rather than in the repository root, so a failed comparison no
  longer leaves a file behind in the tree for a contributor to find and
  delete.
- The Release Drafter workflow skips a pull request opened from a fork
  instead of failing on it.  Its token is read-only there whatever the
  workflow declares, so the write the job exists for could only error; a
  same-repository pull request is drafted and commented on as before.
- **Breaking:** Snippet code, names and tags are stored in Unicode
  Normalization Form C, and every checksum, fingerprint and query is taken
  over that same form.  The same string could previously arrive in two
  compositions (`café` as one code point, or `café` as a base letter plus
  a combining acute) and be stored as two snippets that no query could
  tell apart, or as two aliases on one snippet.  macOS is the usual
  source: it hands back NFD filenames.  A database whose snippets contain
  non-ASCII text must be re-imported, because their checksums are now
  taken over the normalized form; ASCII text is unaffected, since NFC is
  the identity on ASCII and the stored checksum of such a snippet does not
  change.  `normalize_unicode` is exported for callers that compare
  against the stored form themselves.
- The `Test Suite` and `Pylint` workflows run on pull requests to `main` and
  on pushes to `main`, not on every push to every branch.  The two events carry
  different refs and so are different concurrency groups, which meant every
  push to a feature branch ran the full suite twice, each run standing up its
  own PostgreSQL and MySQL containers.  Every other workflow here was already
  scoped to `main`.
- No workflow leaves the GitHub token in `.git/config` after checkout
  (`persist-credentials: false`); none of them pushes, so the credential was
  only readable by a later step.
- `Build` and `SBOM` artifacts are kept for 30 days rather than the 90-day
  default, so superseded builds of every push do not accumulate.
- `make lint` is green again.  `tests/test_asmatch.py` reached into
  `resembl.core._FIND_CANDIDATE_CHUNK` without the module-level
  `protected-access` disable the other private-internals test modules carry,
  so `pylint` exited 4 and `make check` stopped at the lint step.
- `docs/THREAT_MODEL.md` and `SECURITY.md` re-verified against the current
  tree: every file reference now resolves in `resembl/`, the environment
  overrides are documented where they are read (`resembl/paths.py`), and two
  gaps are named that were not, `find-batch --file` reading its whole input
  into memory, and `resembl import` sizing a worker pool from a directory
  listing with no file-count cap. `SECURITY.md` also states the
  `RESEMBL_DATABASE_URL` precedence over `DATABASE_URL` and that the config
  directory is separate from the cache directory.
- `find` and `serve` Jaccard-score their LSH candidates in bounded chunks, so
  a query landing in a crowded band no longer pulls every candidate's
  fingerprint, the whole vectorized array and the score list into memory at
  once; peak memory is now a function of the chunk size instead of the band
  population.  Rankings are unchanged.
- The sdist and wheel now carry a `NOTICE` alongside `LICENSE`. `resembl`'s
  MinHash and LSH banding code derives from MIT-licensed datasketch, whose
  terms require the copyright and permission notice to travel with every
  copy; nothing shipped it before, so a downstream consumer could not trace
  that grant back to its origin. Nothing about installation, the CLI or the
  `serve` responses changes.
- `RESEMBL_SEED` now governs one generator for the whole run instead of one
  generator per draw. A seeded run used to replay its first sampling offset
  for every draw, so every `stats` estimate in that run was computed from the
  same rows; the same seed now replays the same sequence of distinct draws.
  Unset, one seed is drawn from the OS, logged at INFO with the value to
  replay with, and reused for the rest of the process, where before each
  draw drew a fresh one and the run reported numbers nothing could reproduce.
- The find server bounds `top_n` at 1000 per request, like every other find
  parameter. It was the one unbounded field, so one unauthenticated `POST
  /find` with a low `threshold` and a large `top_n` returned the whole
  matching corpus in a single response. A larger value now answers `400`;
  `resembl find` falls back to its in-process path, which is unchanged.
- The find server refuses a request whose `Host` header does not name it, on a
  loopback bind. It answers `403` to anything but `127.0.0.1`, `localhost` and
  `::1` (with or without the port). A page that rebinds its own hostname onto
  127.0.0.1 reached `/find` same-origin, with no CORS preflight and a readable
  response, and the server had no other caller check; the `Host` header still
  carries the rebound name. A non-loopback bind is unrestricted, since it is
  reached by whatever name resolves to it.
- The cache directory `serve` creates is `0o700` and the port file it writes is
  `0o600`, instead of both following the process umask. Both `find` clients
  trust the advertisement to name the port they send the query to, so nothing
  in that directory needs to be readable by another account on a shared host.
  An existing cache directory keeps the permissions the user gave it.
- `resembl serve -v` now records every request at DEBUG (peer address, request
  line, outcome, with control characters stripped so a crafted path cannot
  forge a second record). At the default level the server still logs nothing.
- Full-corpus reads are ordered by the checksum primary key: `snippet list`
  (including a `--range` window), `name search`, `collection show`,
  `stats`, `export` and `merge` all read rows in one defined order, where
  before the row order was whatever plan the backend picked, so output and
  float sums could differ between two runs over the same database.
- **Breaking:** `resembl import` on a directory that holds no `.asm` / `.txt`
  file now exits `1` and says so, instead of exiting `0` and reporting a
  successful import of zero snippets.  A script that imported a directory that
  had been emptied now sees a failure; point it at a directory that holds
  snippets, or tolerate exit `1`.
- `resembl list` on an empty database, a `--range` past the last snippet,
  and a `resembl search` that matches no name each say what happened and
  what to run next, where the listing used to print nothing or a bare
  count.
- **Breaking:** `resembl collection create` on a name that already exists now
  reports the collection as already there and exits `0`, where it failed with
  the database's `IntegrityError` and its SQL text at exit `1`. The stored
  description is left alone, so re-running a create never rewrites a
  collection. A caller that treated exit `1` as "the create failed" now has to
  read the collection back to tell the two apart.
- **Breaking:** `resembl name add` on a name the snippet already carries is now
  a no-op that exits `0`, where it exited `1`, like `resembl tag add` already
  was. A script that re-ran the command after a failure used to see exit 1 for
  an end state it had already produced, and a script that branched on that exit
  code now has to.
- The sdist and wheel are now built by `make dist`, which pins the build clock
  to the commit's `SOURCE_DATE_EPOCH` and normalizes the sdist archive
  metadata. Two builds of one commit produce identical bytes; `make dist-verify`
  builds twice and fails if they differ, and CI runs it on every push, so an
  artifact that cannot be reproduced or installed no longer reaches a release
  on the strength of the test suite alone. Building from the sdist needs
  `setuptools==80.9.0`, the now-pinned build backend.
- **Breaking:** `resembl.database.db_url_get`, `resembl.database.db_url_mask`,
  `resembl.database.DEFAULT_DB_URL`, `resembl.cache.cache_dir_get`,
  `resembl.cache.DEFAULT_CACHE_DIR`, `resembl.config.config_dir_get`,
  `resembl.config.config_path_get` and `resembl.config.DEFAULT_CONFIG_DIR`
  moved to the new `resembl.paths` module, which owns every path resembl
  derives from the environment.  `resembl.server.server_port_path` and
  `resembl.find_client.server_port_path` collapse into one
  `resembl.paths.server_port_path(db_url, cache_dir)`.  The CLI, `serve` and
  the thin find client resolve these through the shared module instead of
  keeping copies, so a change to the override rules can no longer leave the
  client looking for a port file the server never wrote.  Recorded as
  [ADR 006](docs/adr/006-paths-module-owns-environment.md).
- **Breaking:** `resembl.database.DATABASE_URL` is gone.  It was read once at
  import and only from the unprefixed `DATABASE_URL`, so a process that set
  the variable after importing resembl kept querying the first URL it saw,
  and a caller could not reach the namespaced `RESEMBL_DATABASE_URL` at all.
  Use `resembl.paths.db_url_get()`, which reads the namespaced name first
  and the unprefixed one next, at call time;
  `resembl.database.create_db_engine(None)` already resolves through it.
- **Breaking:** `resembl.cache.lsh_index_remove` (and its `resembl.core`
  re-export) is renamed `resembl.cache.lsh_index_purge`, and its contract
  changes with the name: it returns the built index's threshold, or `None`
  when no index is built, where the old function returned `bool`; and it
  leaves the delete uncommitted, so the caller commits the bucket rows
  together with the row they belong to.  A caller that committed inside the
  old function now owns the transaction: read the threshold and commit, then
  call `resembl.cache.lsh_pickle_cache_remove(threshold)` to drop the legacy
  pickle cache, as `snippet_delete` does.
- **Breaking:** `resembl.scoring.token_is_label` is removed.  It had no caller
  left, in this package or out of it, and was never documented, so there is
  no replacement: the label filter it duplicated lives inside the lexing
  pipeline behind `code_tokenize_normalize_lexed`.  A consumer that
  classified a token itself has to keep its own copy of the check.
- **Breaking:** `resembl.models.timestamp_normalize` (also reachable as
  `resembl.core.timestamp_normalize`) takes `str | None` and returns
  `str | None`, where it took and returned `str`.  A NULL `created_at` is
  now passed through instead of raising, which is what lets `db_merge`
  accept a source row with no readable timestamp.  A caller that assigned
  the result to a `str` has to handle `None`; the only input that now
  returns `None` is one the old call raised `TypeError` on.
- **Breaking:** `resembl serve` now answers `400` to a request whose
  `threshold`, `ngram_size` or `num_permutations` is not the value the
  running server's index was built for.  Before, the request rebuilt the
  shared `lsh_bucket` table inside a handler thread while other requests read
  it, which could leave `lsh_meta` advertising a complete index over missing
  rows and silently return a fraction of the matches.  A `threshold` too high
  to leave 2 bands is refused the same way, instead of answering with zero
  matches.  Migration: send the fields the server is configured with, or
  leave them out so they default to the server's `config.toml`, or restart
  the server with the settings the client sends.  The three fields come from
  the same `config.toml` the CLI and `resembl-find` read, so a matching
  client needs no change.
- **Breaking:** a configuration value outside the range its setting works in
  is now reported on stderr and ignored, and the default is used.  Before it
  was used as written, so a hand-edited `lsh_threshold = 0.995` queried with
  that threshold and now queries with the `0.5` default; a `top_n = 0` that
  returned nothing now falls back to `5`.  The accepted ranges are in the
  README table.  Move an out-of-range value inside its range.
- `DATABASE_URL` is namespaced: `RESEMBL_DATABASE_URL` is read first and the
  unprefixed name next, so an existing deployment keeps working and an
  unrelated `DATABASE_URL` in the environment no longer picks the backend.
- **Breaking:** a rejected command line exits `2`, not `1`, matching what
  typer already returned for its own parse errors: a bad `--threshold` or
  `--format` value, a missing query, or a bare `resembl`
  with no subcommand.  A script that treated `1` as "you typed it wrong"
  has to read `2` now; `1` still means the command failed.
- **Breaking:** an unsupported `--format` value, and an unsupported `format`
  in the config file, are refused instead of falling through to an unknown
  renderer. A `format` a hand-edited `config.toml` carried used to be written
  through to the writer; a config naming a renderer that no longer exists now
  exits `2` at startup, so the value has to be one of the documented names.
- **Breaking:** log records go to stderr rather than stdout, so a warning
  raised mid-run (a config key ignored, a parallel import falling back) no
  longer interleaves into the JSON or CSV a script is parsing. A consumer that
  captured stdout and parsed it as one document has to read stderr for the
  warnings and keep stdout for the result.
- **Breaking:** a `--format csv` result set with no rows now writes nothing,
  instead of writing a JSON `[]` into the CSV stream. A reader that parsed the
  empty result as a one-line file has to treat a zero-byte file as the empty
  result.
- The `pg8000` and `pymysql` DBAPI drivers are runtime dependencies, so a
  plain install can use the `postgresql+pg8000://` and `mysql+pymysql://`
  URLs the CLI advertises.
- Package metadata declares the SPDX expression `GPL-3.0-only` and ships
  the license text through `license-files`; `GPLv3` was not a valid SPDX id,
  so indexers and scanners could not match the license.
- The terminal UI is drawn from a single brand-green palette rather than per
  element colors.
- **Breaking:** a refused find parameter on `resembl serve` now answers `400`
  with a `{"error": "..."}` body, on `POST /find` and `POST /find-batch`
  alike.  Before, `POST /find` answered `200` and put the error in the
  payload, so a client that read `error` out of a successful response has to
  check the status code instead.  The message also lost its `bad request: `
  prefix.  `resembl find` and `resembl-find` speak both shapes.
- `POST /find-batch` refuses more than 1000 queries with `400`; a client with
  a larger set splits it across requests (the CLI's `find-batch` already
  chunks its input file).
- An explicit `Content-Type` other than `application/json` is refused with
  `415` rather than parsed anyway.
- An unknown path answers the JSON error envelope with `404` instead of the
  stdlib's HTML 404 page, and a method other than `POST` answers `405`
  instead of the stdlib's HTML 501.
- The shared `setup-uv` composite action now exports the `python-version` it
  is given as `UV_PYTHON`, which is what its input documents and what the
  pylint matrix leg was setting by hand, so a workflow that asks for an
  interpreter gets it instead of only naming it in the cache key.  The test
  and coverage workflows dropped their `actions/setup-python` step, which
  downloaded a second CPython that uv never used, and the release-drafter
  and SBOM workflows gained the concurrency group the other five have.

### Fixed

- `resembl find --file` and `resembl find-batch --file` read the file as
  UTF-8, the encoding `resembl import` already reads with.  Both went through
  click's default instead, which is the platform's locale: on a Windows shell
  with a legacy code page a UTF-8 snippet decoded to mojibake (`café` read as
  `cafÃ©`), so the query no longer matched the snippet the same file imports
  as, and a query file that was not in the local code page was refused
  outright.

- Output is written as UTF-8 whatever the host's locale says, and a character
  the stream cannot represent is replaced rather than raising.  A snippet
  named `日本語` printed under a cp1252 stdout raised
  `UnicodeEncodeError` and lost the whole report: `list`, `find`, `search`,
  `compare` and `collection list` all tracebacked instead of rendering.
  Windows terminals, which already use their own Unicode writer, are
  untouched.

- `resembl search` normalizes its pattern to NFC, the form names are stored
  in.  A pattern carrying an NFD spelling of an accented name (`café` as a
  base letter plus a combining acute, the spelling macOS and a copy-paste
  hand over) matched no row even though the name was in the database.

- `POST /find` answers `400` for a blank `query`, and `POST /find-batch`
  carries the same message on that query's entry.  A blank query
  fingerprints to an empty token set, so the endpoint answered `200` with an
  empty `matches`, which a client reads as "this snippet has no duplicates"
  rather than as a request that searched for nothing.  `resembl find` and
  `resembl-find` already refused such a query.

- `resembl serve` clamps a configured `top_n` above 1000 to 1000 as its served
  default, and reports the clamp once at startup.  The config layer leaves
  `top_n` unbounded while the request path caps it, so a large configured
  value became a default the endpoint refused: every request that omitted
  `top_n`, which is what a plain third-party client sends, was answered `400`
  over a value it never sent.  An explicit `top_n` above the cap is still a
  `400`, and the in-process `resembl find` still returns the configured
  number of rows.

- `resembl export` converges on a re-run.  A second export into a directory an
  earlier one wrote left the files of snippets that had since been renamed or
  deleted in place, so the directory was not a faithful copy of the database
  and re-importing it brought retired snippets back.  The export now records
  the files it wrote in `DIRECTORY/.resembl-export.json` and a later run
  removes the ones the database no longer produces; only those entries are
  ever removed, so other files in the directory are untouched, and a missing
  or damaged manifest just disables the pruning.  The removed count is
  reported in the `json` / `csv` output as `num_removed` and in the table.

- `resembl verify` no longer crashes on an `lsh_meta` row naming parameters no
  index can be built from (a permutation count below 2, a threshold outside
  `[0.0, 1.0]`).  The stored values went straight into the banding search,
  which raised numpy's "need at least one array to concatenate" out of a
  health check; the row is now reported as an issue, so the command still
  exits 1 and still points at `reindex --force`.

- `resembl.lsh.band_buckets` refuses a banding that does not fit the
  fingerprint (`b * r > num_perm`).  Slicing past the end of the blob returned
  a short, or empty, slice, so every over-long band hashed to the same empty
  bucket key and an index built that way would answer every query with every
  snippet.  No index built by `banding_params` was ever affected.

- `serve` answers `400` for a fractional `top_n`, `ngram_size` or
  `num_permutations` instead of truncating it: `2.9` permutations used to
  become a valid 2 and `5.9` results became 5, so the response answered as if
  the client had sent a number it did not.  An integral float (`3.0`) and a
  decimal string (`"3"`) are still accepted.

- `POST /find` and `POST /find-batch` refuse a `top_n` below `1` with a `400`
  instead of answering `200` with an empty `matches` list.  The cap of 1000
  was the only bound on the field, so a request naming `0` or a negative
  value silently truncated the ranking to nothing, which a client reads as
  "this snippet has no duplicates".  `top_n` now carries the same `1` lower
  bound the configuration layer applies to the CLI.

- `HEAD` and `OPTIONS` are answered `405` with the same JSON error envelope as
  `GET`, `PUT`, `DELETE` and `PATCH`, rather than falling through to the
  stdlib base class and returning an HTML `501`.  A `HEAD` carries the status
  and headers a `GET` would produce and no body bytes.  A client that parses
  every response as JSON, or a liveness probe checking the port, no longer
  gets a body it cannot decode.

- Collection names are normalized in both directions.  `collection_create`
  stored the name in NFC while `collection add` and `collection delete`
  looked it up raw, so the NFD spelling macOS hands back for an accented
  name missed the row and reported "Collection not found"; `db merge`
  compared the incoming name against un-normalized local names and could
  abort on a primary-key collision.  All three now compare on the stored
  form.
- `cfg_extract` drops `#` comments, not only `;` ones.  NASM spells a line
  comment both ways and the tokenizer the rest of the module uses drops
  both, so a commented-out `#label:` line was counted as a label and split
  its block, changing every CFG-derived score.
- `find` and `find-batch` reject a `--top-n` below 1, and `serve` answers
  `400` for one, the way `config set top_n` already did.  A zero or
  negative override bypassed that check and truncated the ranking to
  nothing, which reads as a clean "no matches" beside a full candidate
  count.
- `resembl serve -v` works.  `-v` was only a global option, so the
  documented spelling was rejected with "No such option"; the per-request
  DEBUG log is the only trail a served query leaves, so serve now takes
  the flag itself.
- `--format csv` writes `find-batch`'s `matches` cell as JSON.  It was
  handed the nested list of match dicts, which `csv` stringified with
  `str()`, so the cell held a Python repr no JSON or CSV reader could
  parse back.  The CSV formula guard is applied to the strings inside it,
  so a name smuggled into a nested payload is still neutralized.
- `snippet_list` clamps a `LIMIT` of `end - start` to zero.  A window
  asked for with `end < start` reached SQLite as a negative `LIMIT`, which
  it reads as "unbounded" and answers with every row from the offset, and
  which PostgreSQL rejects outright.
- `resembl clean` no longer races an index rebuild of the same database.
  It dropped every `lsh_bucket` row without taking the per-database lock a
  rebuild holds, so a clean landing between the rebuild's own clear and its
  final `lsh_meta` stamp left it advertising a complete index over rows it
  had already deleted: every later find answered with a fraction of its
  matches and nothing rebuilt it.  The clean and its vacuum now run under
  that lock, as a rebuild and an index build already do.
- The sampled figures `resembl stats` reports (vocabulary, average
  similarity) are reproducible again when several threads sample at once.
  The process-wide generator was built, tested and published in one
  unsynchronized check-then-act, so two callers arriving together could
  each build one and the loser kept re-drawing the first values of a
  generator whose seed the other had already reported as the run's replay
  seed.  Construction, replacement and every draw now happen under one
  lock.
- `resembl export` no longer aborts on a filename that contains a byte no
  encoding can decode.  POSIX filenames are byte strings, so importing a
  directory whose entries carry one produced a name with a lone surrogate
  in it, and encoding that name to measure its length raised
  `UnicodeEncodeError` and killed the whole export.  Such a name now
  exports like any other name with an unportable character in it.

- `resembl serve` holds one copy of a query instead of the query itself, and
  answers a burst of identical queries with one find rather than one per
  request.  The in-process result cache kept the full request text as the
  cache key, so its 128-entry cap was not a memory bound: a hundred and
  twenty-eight large requests pinned about a gigabyte of key strings.  The
  key is now a SHA-256 digest of the query, the same content addressing
  snippet checksums use.  Concurrent requests for a key that is not cached
  yet each ran the same find simultaneously, every one of them holding a
  database connection and an LSH query for its full duration; the first
  request to miss a key now computes it and the rest read what it stored.
  A cached answer is still returned only while the database is unchanged.
- A process that drives the CLI repeatedly in one interpreter (a test
  harness, an embedding script) no longer leaks a database session and a
  checked-out connection per invocation.  The main callback registered
  `state.session.close` as an exit hook on every run, so each registration
  held a strong reference to its own session, the earlier ones were never
  released, and the engine's connection pool drained until it stalled.  One
  stable exit hook now covers the session currently in use, and a new
  invocation closes the previous one first.
- `--format csv` output no longer carries a stray carriage return on Windows.
  The `csv` module terminates records with `\r\n` and the writers target
  `sys.stdout`, a text stream that rewrites every `\n` to `os.linesep`, so
  each record ended `\r\r\n` there and every CSV reader parsed the extra `\r`
  as a field.  Records now end in a bare `\n` on every platform, matching the
  `json` renderer.
- `resembl collection show --format csv` writes LF on every platform too.  It
  builds its writer inline rather than through the shared CSV helper, so it
  kept the `csv` module's `\r\n` default and was the one CSV render that still
  differed across platforms.
- **Breaking:** `resembl clean` reports the LSH index it dropped instead of a
  cache it never touched.  `clean` drops the index rows and vacuums; the legacy
  pickle cache is removed by the next index write, not by `clean`.  A script
  that read the dropped cache path out of the report, or that ran `clean` to
  reclaim the cache directory's disk, has to run an index write instead.
- `resembl export` and `resembl export-yara` write LF on every platform.
  Text mode rewrote each `\n` to `os.linesep`, so the same database exported
  CRLF on Windows and LF elsewhere and the two trees diffed against each
  other; the generated rule file and the `.asm` files are now byte-identical
  wherever they are produced.
- `make dist-verify` works on macOS, which ships `shasum -a 256` instead of
  coreutils' `sha256sum`.  `make hygiene` no longer needs ripgrep, which is
  not a system tool there.
- `serve` answers every HTTP method with its JSON error envelope.  A method
  the handler did not implement (`HEAD`, `OPTIONS`, ...) fell through to
  `BaseHTTPRequestHandler`'s `501`, an HTML page, so a client parsing
  responses as JSON got a decode error instead of the status, on exactly the
  methods it probes first.  `HEAD` on `/find` now answers `405` with the
  headers and no body; every other verb answers `405` with `Allow: POST`.
- `serve` answers `404`, not `405`, for an unknown path on any method.
  `GET /nope` reported a method problem for a path that does not exist.
- `serve` ignores a query string on the request path, so `/find?trace=1` is
  the `/find` endpoint instead of a `404` for a documented request.
- `serve` rejects a non-boolean `normalize` with a `400`.  A client sending
  the string `"false"` asked for normalization off and got a `200` with the
  matches for the normalized corpus, because `bool("false")` is true.
- `resembl verify` exits 1 on a stale index in every output format.  The
  documented exit status was only applied to the table render, so
  `verify --format json` reported the same issues and still exited 0, and a
  script gating on it never saw the failure.
- **Breaking:** `resembl.core.snippet_list` (and its `resembl` re-export)
  takes `end: int | None = None` where it took `end: int = 0`, and a window
  is now selected by any `end` that is given rather than by `end > 0`.  The
  old `0` was an "unbounded" sentinel, so a caller that passed one
  explicitly to mean "every row" (`snippet_list(session, 0, 0)`,
  `snippet_list(session, 5, 0)`, or a `0` carried by a variable) is now
  answered with the empty half-open window `[start, end)`, and `end < start`
  computes a negative `LIMIT`.  Migration: drop the argument
  (`snippet_list(session)`) or pass `end=None` where the old `0` stood.
- `resembl list --range 0-0` no longer lists the whole database.  An
  explicit `0-0` window meant "no rows", and the sentinel `0` that answered
  it with the whole database is gone; `--range` without an `end` still lists
  everything.
- The token-type classification cache is published instead of mutated in
  place.  `serve` runs one handler thread per request and every request
  lexes, so the cache was written by several threads while others read it,
  with no lock on either side.  Writers now build a fresh dict and rebind the
  name, so a reader that loaded the old one keeps answering from a complete
  snapshot.  Nothing observable changes for a single-threaded caller.
- `snippet_delete` purges the checksum's `lsh_bucket` rows in the same
  transaction as the snippet row.  Committing the two separately left bucket
  rows for a snippet that no longer existed whenever the process died in
  between, and `lsh_meta` still marked the index complete, so no later find
  repaired it.
- `add`, `import`, and `merge` write a snippet's `lsh_bucket` rows in the same
  transaction as the snippet row itself.  Committing the rows separately left
  a window in which a killed process (or a database lock on the index sync)
  stranded committed snippets outside the index while `lsh_meta` still
  advertised a complete one, so every find silently missed them.  Re-running
  the import did not repair it either: the checksums were already present, so
  the retry took the alias path and indexed nothing, leaving a permanently
  under-indexed database until someone ran `reindex --force`.
- A snippet whose `created_at` is `NULL` no longer breaks `merge` or its
  timestamp rendering.
- The served result cache's version guard is read through one shared probe
  connection per database, so a rebuild under one connection is no longer
  invisible to a request holding another.
- Closing a `serve` generation now releases the result cache's version probe
  for its database.  The probe was only weakly referenced from the served
  engine, so its connection stayed open until the process collected both: a
  process starting and stopping servers repeatedly accumulated one open
  handle and connection pool per generation, and on Windows the last handle
  kept the database file from being removed or replaced.
- `resembl-find` reports a mistyped number in `config.toml` and falls back to
  the default instead of dying with a `ValueError` traceback before the query
  is sent.
- A `POST /find` or `POST /find-batch` body nested deeper than the JSON
  decoder's recursion limit is now answered with `400` and the standard
  error envelope.  It used to raise `RecursionError`, which escaped the
  body's parse guard and the handler's own error handling, so the
  connection died with no response at all.
- A `POST /find` or `POST /find-batch` request that omits `threshold` now
  searches at the threshold the server's index was built for, instead of
  answering `400` on any server configured with an `lsh_threshold` other
  than `0.5`.  An omitted (or `null`) field has always meant "use the
  server's configured default"; it resolved to the module default instead,
  so the endpoint blamed the client's omission for a mismatch it had
  created.  `resembl find` and `resembl-find` send the value from
  `config.toml` and are unaffected.
- `serve` no longer caches a find result whose payload retains more than
  256 KiB of checksum and name text.  The result cache was bounded by entry
  count, and one request chooses its own `top_n` and the name lists behind
  the matches, so a client cycling large finds pinned an unbounded multiple
  of that cap.  An oversized result is simply computed again next time; the
  answer is unchanged.

## [2.0.0] - 2026-09-15

### Changed

- **Breaking:** Python 3.11 and 3.12 are no longer supported; the minimum
  is now Python 3.13, matching what the locked dependencies (notably
  `numpy` 2.5) already require.
- Dependencies refreshed to their latest releases: `pylint` 4, `datasketch`
  2, `mypy` 2, plus the rest of the locked tree.
- **Breaking:** three names are gone from the `resembl.core` re-export block:
  `code_tokenize_lexed` and `string_normalize_lexed` (both dead; the live
  lexing pipeline behind them is `resembl.scoring.code_tokenize_lexed` and
  `resembl.scoring.string_normalize_lexed`), and `minhash_jaccard`, which is
  still public and is importable from `resembl.models` and documented in
  `docs/api_reference.md`. A caller that reached it through `resembl.core`
  has to import it from `resembl.models` (or `resembl.scoring`); the function
  and its signature are unchanged, only the name it was re-exported under.

## [1.2.0] - 2026-09-15

### Changed

- Performance: fingerprinting — the work behind `import`, `reindex`, `find`
  and `stats` — is roughly twice as fast.  The NASM lexer's rule order was
  corrected so a space no longer fails fourteen regexes before matching
  (~39% fewer regex attempts, 1.55x faster lexing); the cached MinHash
  template no longer regenerates its permutation table on every clone; and
  a snippet is lexed once per `add` instead of twice.
- Performance: SQLite bulk writes go straight to the DBAPI cursor instead
  of through SQLAlchemy's per-row parameter construction, so `import`,
  `reindex` and `merge` write faster (LSH index rows 1.73x, snippet rows
  1.9x).  Stored fingerprints are unchanged, so no reindex is required.

## [1.1.0] - 2026-09-13

### Fixed

- `--no-color` now also suppresses the ANSI codes typer paints into `--help`.
  typer forces color whenever `GITHUB_ACTIONS`, `FORCE_COLOR` or `PY_COLORS` is
  set, even into a pipe, so the flag previously left the help panel colored.
- MySQL/MariaDB: the `app_meta` statements quote their `key` column.  `key` is
  reserved in MySQL, so every insert, select and delete on that table was a
  syntax error there; SQLite, PostgreSQL and DuckDB accept the unquoted name.

## [1.0.0] - 2026-09-12

First stable release.  The 0.x line was developed without a changelog; this
entry covers the changes a 0.x consumer needs to know about, not the full
0.1.0..1.0.0 history (which is in git).

### Changed

- **Breaking:** the private `resembl.scoring._minhash_from_tokens` helper is
  now the public `resembl.scoring.minhash_from_tokens`, alongside the other
  public banding parameters and the `num_permutations` cap.  Code importing
  the underscore-prefixed name must switch to the public one.
- **Breaking:** legacy pickle cache files are no longer read.  Unpickling a
  file is arbitrary code execution and the cache directory is not a trust
  boundary, so stale cache files are ignored and removed on the next write;
  the LSH index rebuilds from the database instead.
- The MinHash implementation is vendored (bit-compatible with `datasketch`,
  in `resembl.minhash`) and the `scipy` dependency is gone.
- The pygments lexer and rapidfuzz are imported lazily, off the startup path.
- Config, data, and cache locations honor the XDG base-directory spec.

### Added

- `resembl-find`, a standalone client for a running `resembl serve`, shipped
  as its own console script (`resembl.find_client`).
- DuckDB support alongside SQLite, PostgreSQL, and MySQL.
- YARA export, written atomically.

### Fixed

- LSH writes reject oversized keys and foreign blobs.
- CSV output and server error responses are hardened against malformed input.
- `serve` treats explicit null find parameters as absent and retires its port
  file on close.
- Non-finite numeric values are rejected in config and server input.
- Config updates are serialized with a cross-process lock.
- Elapsed-time measurement uses `time.monotonic`.

## [0.1.0] - 2026-08-20

Initial release: content-addressed snippet store, database-backed LSH index,
and the `resembl find` matching pipeline.

[Unreleased]: https://github.com/maci0/resembl/compare/v4.0.0...HEAD
[4.0.0]: https://github.com/maci0/resembl/compare/v3.12.0...v4.0.0
[3.12.0]: https://github.com/maci0/resembl/compare/v3.11.0...v3.12.0
[3.11.0]: https://github.com/maci0/resembl/compare/v3.10.2...v3.11.0
[3.10.1]: https://github.com/maci0/resembl/compare/v3.10.0...v3.10.1
[3.10.0]: https://github.com/maci0/resembl/compare/v3.9.0...v3.10.0
[3.9.0]: https://github.com/maci0/resembl/compare/v3.8.0...v3.9.0
[3.8.0]: https://github.com/maci0/resembl/compare/v3.7.0...v3.8.0
[3.7.0]: https://github.com/maci0/resembl/compare/v3.6.1...v3.7.0
[3.6.1]: https://github.com/maci0/resembl/compare/v3.6.0...v3.6.1
[3.6.0]: https://github.com/maci0/resembl/compare/v3.5.0...v3.6.0
[3.5.0]: https://github.com/maci0/resembl/compare/v3.4.0...v3.5.0
[3.4.0]: https://github.com/maci0/resembl/compare/v3.3.1...v3.4.0
[3.3.1]: https://github.com/maci0/resembl/compare/v3.3.0...v3.3.1
[3.3.0]: https://github.com/maci0/resembl/compare/v3.2.1...v3.3.0
[3.2.1]: https://github.com/maci0/resembl/compare/v3.2.0...v3.2.1
[3.2.0]: https://github.com/maci0/resembl/compare/v3.1.4...v3.2.0
[3.1.4]: https://github.com/maci0/resembl/compare/v3.1.3...v3.1.4
[3.1.3]: https://github.com/maci0/resembl/compare/v3.1.2...v3.1.3
[3.1.2]: https://github.com/maci0/resembl/compare/v3.1.1...v3.1.2
[3.1.1]: https://github.com/maci0/resembl/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/maci0/resembl/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/maci0/resembl/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/maci0/resembl/compare/v1.2.0...v2.0.0
[1.2.0]: https://github.com/maci0/resembl/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/maci0/resembl/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/maci0/resembl/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/maci0/resembl/releases/tag/v0.1.0

[3.10.2]: https://github.com/maci0/resembl/releases/tag/v3.10.2
