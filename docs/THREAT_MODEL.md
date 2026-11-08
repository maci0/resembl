# resembl threat model

Last reviewed: 2026-11-09, against the code at version 4.0.0
(`pyproject.toml:3`) plus the post-4.0.0 commits on `main` through
`1c03f6f`. Every reference below was re-read in that tree on that
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
| R2 | The find server has no authentication and no rate limit, and a non-loopback bind is a warning, not a block | B1 | R1 plus sustained exhaustion | `Host` check on a loopback bind, which answers `403` to a rebound name (`_host_allowed` in `resembl/server.py:1301`, `_loopback_allowed_hosts` in `resembl/server.py:672`); loopback default only (`resembl/cli.py:578`, `resembl/cli.py:603`) |
| R3 | The port file is an unauthenticated channel: both clients trust its contents and forward the query text to whatever loopback port it names | B5 | Query text (attacker-supplied source code) sent to an attacker-controlled loopback listener | The standalone client `resembl-find` refuses a port-file port outside the ephemeral range 1024-65535 before dialing it (`6273fa0`, `_main` in `resembl/find_client.py`), so a planted port naming a privileged local service (22, 3306, 6379) is no longer dialed; a forged port inside that range still reaches an attacker listener, the in-process `find` client (`resembl/cli.py:429`) has no equivalent range check, and the cache directory the server did not create is still unchecked (`serve` in `resembl/server.py:2116` makes its own directory `0o700`) |
| R4 | Nothing is recorded per request at the default log level, so queries are unattributable unless `serve -v` is running | B1 | No way to investigate a suspected scrape or a hostile client | `serve -v` logs every request at DEBUG with its peer and outcome (`log_message`/`log_request` in `resembl/server.py:1760`); the only default-level per-request trace is a slow-request WARNING (`resembl/server.py:1790`) and exception logs on failure (`resembl/server.py:1521`) |
| R5 | `RESEMBL_DATABASE_URL` / `DATABASE_URL`, `RESEMBL_CACHE_DIR`, and `RESEMBL_CONFIG_DIR` are trusted verbatim from the environment, including credentials and a remote host | B3 | Database pointed at an attacker-chosen host; config read from an attacker-chosen directory | none, by design (`docs/adr/006-paths-module-owns-environment.md`, `resembl/paths.py:57`, `resembl/paths.py:81`, `resembl/paths.py:97`) |
| R6 | `merge` opens an arbitrary source URL, including a credentialed remote database, and inserts its rows | B6 | Rows from a hostile source enter the corpus; credentials sent to a chosen host | Fingerprints recomputed, never deserialized (`db_merge` in `resembl/core.py`); the source URL is masked for display in the error path (`db_url_mask` in `resembl/paths.py`), and callers get only a stable message naming the masked source — driver/SQL detail stays in the operator log, not the return value (`c1eb6cb`, `db_merge` in `resembl/core.py`) |
| R7 | `import` holds every matching path in memory and spawns a worker process per CPU over the set, and `reindex` does the same over every row of the corpus, with no count or size cap on either | B2 | Process, memory, and CPU exhaustion from a hostile directory | Chunked database writes only (`resembl/cli.py:1010`, `resembl/cli.py:1049`) |
| R8 | The in-process result cache keeps the 128 most recent find results resident | B1 | Snippet checksums and names (the result payload) sit in process memory beyond the request | 128-entry LRU whose key is a SHA-256 digest of the query, so the query text itself is not retained (`resembl/server.py:618`) |
| R9 | The port-file name is a 12-hex-digit SHA-256 of the raw, unmasked database URL | B5, B7 | A truncated hash of a credentialed URL is written to disk under the cache dir | none (`server_port_path` in `resembl/paths.py`, upgraded from SHA-1 in `c1eb6cb`; stale SHA-1 files are ignored) |
| R10 | `find-batch --file` reads the whole query file into a list before chunking it into requests | B2 | Memory exhaustion from an oversized input file | Chunking on the wire only (`resembl/cli.py:1571`, `resembl/cli.py:526`) |
| R11 | A release tag is not statically analyzed or dependency-reviewed when it is cut; only the SBOM runs on the tag | B8 | A tagged release ships whatever the tag's tree contains, with no pre-release SAST or CVE gate on the tag itself | SAST on push and pull request to `main` plus a weekly cron, Python and Go (`.github/workflows/codeql.yml:4`), dependency review on pull requests to `main` only (`.github/workflows/dependency-review.yml:11`), SBOM on `main` and on `v*` (`.github/workflows/sbom.yml:9`); `service.yml` runs `gofmt`/`go vet`/`go test -race` on `service/` push/PR |
| R12 | The vulnerability reporting channel the documentation names is not enabled on the repository | Outside the process | A reporter following `SECURITY.md` finds no private channel and is pushed toward a public issue | none; the one working route is a public issue, which the same document forbids for an unfixed vulnerability |
| R13 | The Go service's only shipped credential is a single shared bearer token, with no per-caller identity, no audit trail, and no rotation without a restart | B9 | Any holder can query the whole index; a leaked token cannot be revoked short of restart, and there is no trail of who used it | `TokenAuthority` (`service/token_authority.go:54`) with a constant-time digest compare; credential read once at startup (`service/token_authority.go:15`); `service/README.md` documents that rotation requires restart. The binary default is deny-all (`service/service.go:36`) |
| R14 | `POST /find-pinned` is unauthenticated, so any local caller can trigger a bounded 1M-row / 64 MiB index hash while holding a SQLite writer fence | B1, B9 | A caller that reaches the endpoint can stall writers for the scan duration; the fence is held from identity through the query (`_handle_find_pinned` in `resembl/server.py:1529`) | Bounds in `resembl/pinned_snapshot.py:9` (`MAX_SNAPSHOT_ROWS=1_000_000`, `MAX_SNAPSHOT_BYTES=64MiB`); `service/README.md` requires the warm engine to stay isolated on loopback behind the Go service. No authentication on the Python route itself |
| R15 | `/metrics` and `/health` are unauthenticated on both Python and Go servers and disclose request counts, cache hit rate, latency, and uptime. The Python `GET /health` additionally reports database reachability and probe latency; the Go `GET /healthz` is liveness only | B1, B9 | Process internals (traffic volume, hit rate, latency) readable by any local caller; aids timing attacks against R1/R13 | None beyond the loopback default on Python (`resembl/cli.py:767`); Python `GET /metrics` (`_handle_metrics` in `resembl/server.py:1467`) exposes `resembl_server_up`, `resembl_server_uptime_seconds`, `resembl_server_requests_total` by path and status, `resembl_server_request_duration_seconds` latency and `resembl_server_result_cache_total` hit/miss, and `GET /health` (`_handle_health` in `resembl/server.py:1413`) returns database status plus `latency_ms`; Go `GET /metrics` (`service/service.go:212`) exposes `resembl_service_up`, `resembl_service_uptime_seconds`, `resembl_service_requests_total` by path and status, and `resembl_service_request_duration_seconds` histogram, and `GET /healthz` (`service/service.go:203`) is liveness only, no dependency I/O |
| R16 | The Go service's custody slots are held after an uncertain backend outcome and never auto-released, so a client that times out can permanently consume capacity | B9 | A hostile or flaky client can exhaust the 1..64 concurrency slots (`service/service.go:123`) and deny service to legitimate callers until an operator repairs slots | Slots channel with `busy` 503 on overflow (`service/service.go:261`); `FileCustody` (`service/custody.go:24`) leaves the slot occupied across restarts by design; an operator must verify the engine stopped before repairing |
| R17 | A tampered backup file or manifest is accepted as a restore candidate with only a count and content-hash comparison against a manifest anyone who can write the directory can forge | B2, B6 | A corrupted or hostile backup restores a wrong corpus | `verify-backup` opens the backup read-only, checks expected tables, runs `PRAGMA integrity_check`, and compares the live snippet count and corpus `content_hash` against the per-backup manifest (`db_verify_backup` in `resembl/core.py`); a missing manifest is an issue, not a failure, and a count/hash mismatch is a hard failure. When the manifest records a `config_sha256`, the `<backup-stem>.config.toml` sidecar must also exist and hash to it (`8616290`, `db_verify_backup` in `resembl/core.py`), but the manifest itself is unauthenticated, so a forged triple still passes `verify-backup`. New backups are stronger: `db_backup` (`resembl/core.py`) re-verifies the published file and removes it on failure, writes backup, manifest, and config sidecar mode `0600`, and the CLI exits 1 when the manifest could not be written (`resembl/cli.py`, `backup` command) — but that does not authenticate a manifest, so a forged pair still passes `verify-backup` |
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
| `GET /scheme`, the `scheme` of `/score` and `/band-keys` | `_scheme_payload`, `_scheme_id` in `resembl/server.py` | Unauthenticated, reads no database. Each distinct parameter set fingerprints one fixed probe listing; the ids are cached in an LRU of `_SCHEME_CACHE_MAX` (64) entries, so a client cycling `normalize`, `bands` and `rows` cannot grow it. |
| `POST /v1/find`, `POST /v1/find-batch` (Go) | `service/service.go:175` (`ServeHTTP`) | Experimental portable boundary in `service/`. Requires an `Authorizer` (`service/service.go:23`); the shipped binary defaults to deny-all (`service/service.go:36`, `cmd/resembl-service/main.go`) unless `-token-file` supplies a bearer credential (`service/token_authority.go:54`). 8 MiB body cap (`service/service.go:277`), concurrency 1..64 (`service/service.go:123`), per-request timeout. `/v1/find-batch` answers `501` on the shipped pinned binary because `PinnedSQLiteEngine` implements `Find` only (`service/pinned_engine.go:32`), so the handler's `BatchEngine` assertion fails (`service/service.go:324`); a `LocalEngine` backend does implement `FindBatch` (`service/http_engine.go:37`). Not a deployable public service (`service/README.md:3`). |
| `GET /health` (Python) | `_handle_health` in `resembl/server.py:1413` | Unauthenticated; probes the served database with `SELECT 1` and returns `status`, `mode`, `database` reachability, `uptime_seconds`, and `latency_ms` (on success). A stateless server answers `ok` with no database check. |
| `GET /metrics` (Python) | `_handle_metrics` in `resembl/server.py:1467` | Unauthenticated; exposes `resembl_server_up`, `resembl_server_uptime_seconds`, `resembl_server_requests_total` by path and status, `resembl_server_request_duration_seconds` histogram, `resembl_server_result_cache_total` hit/miss, and per-path query errors. No user-controlled label values, so cardinality is bounded by fixed paths and status codes (`_metric_path_label` in `resembl/server.py:935`). |
| `GET /healthz` (Go) | `service/service.go:203` | Local process liveness only; no dependency I/O, no authentication. Under the Lambda bootstrap the same liveness answers an API Gateway event, and every query stays refused. |
| `GET /metrics` (Go) | `service/service.go:212` | Unauthenticated; exposes `resembl_service_up`, `resembl_service_uptime_seconds`, `resembl_service_requests_total` by path (`/v1/find`, `/v1/find-batch`, `/healthz`, `/metrics`, other) and status, and `resembl_service_request_duration_seconds` latency histogram. No user-controlled label values, so cardinality is bounded by fixed paths and status codes. |
| API Gateway HTTP API v2 event (Lambda) | `service/internal/lambdaproxy/proxy.go` (`Handle`), `service/cmd/resembl-lambda/main.go` | The Lambda bootstrap wraps the same `http.Handler` behind `DenyAll`; `lambdaproxy.Handle` translates an API Gateway v2 event into a request, caps input at 8 MiB and output at 4 MiB before base64, rejects duplicate or malformed headers, and never treats gateway identity claims as a grant (`service/internal/lambdaproxy/README.md`). `GET /healthz` answers liveness; every query is refused until an owner-backed authorizer is supplied. |
| `resembl serve --stateless` | `_stateless_serve` in `resembl/server.py` | Holds no database, so the listener exposes no snippet corpus: only `/score`, `/band-keys`, `/scheme`, `/health` and `/metrics` answer, the find paths answer `501`. Still unauthenticated; a non-loopback bind needs a network gate in front of it. |
| `resembl serve` bind | `resembl/cli.py:578` | `--host` is free-form; anything but loopback only prints a warning (`resembl/cli.py:603`). `--port` accepts `0`, which lets the OS assign a port (`resembl/cli.py:580`). |
| Any other verb | `__getattr__` and `send_error` in `resembl/server.py:1197` | The handler resolves any unhandled `do_*` name to the JSON `405` envelope (`__getattr__` in `resembl/server.py:1209`) and answers stdlib errors, including malformed request lines, with the same JSON body through `_respond` (`resembl/server.py:1712`), so no response bypasses the hardening headers. The only `501` left is the deliberate one: a `--stateless` server answering a database path (`_stateless_refused` in `resembl/server.py:1352`). |

`GET`, `PUT`, `DELETE`, `PATCH`, `HEAD` and `OPTIONS` are implemented and
answer `405` with the same JSON error envelope (`_method_not_allowed` in
`resembl/server.py:1372`); a `HEAD` carries those headers without a body.
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
  (`resembl/cli.py:1754`, `resembl/core.py:2036`).
- `find`, `find-batch`, `search`, `compare` take query text, patterns, and
  file paths (`resembl/cli.py:1446`, `resembl/cli.py:1543`,
  `resembl/cli.py:1630`, `resembl/cli.py:1654`).
- `export <file>`, `export-yara <file>` write to a caller-chosen path
  (`resembl/cli.py:896`, `resembl/cli.py:926`).
- `add`, `show`, `rm`, `name *`, `tag *`, `collection *` write to the
  database.
- `backup <dir>` writes a consistent SQLite copy of the corpus plus a
  per-backup manifest (`<backup-stem>.json`, mode `0600`) and a config
  sidecar (`<backup-stem>.config.toml`, mode `0600`, copied verbatim
  from the live `config.toml`) into a caller-chosen directory, pruning
  to the newest `--keep` (default 7)
  (`resembl/cli.py`, `db_backup` in `resembl/core.py`); `verify-backup
  <path>` opens a backup read-only, checks the expected tables, and
  compares the live snippet count and content hash against that
  manifest, cross-checking the config sidecar when the manifest records
  its hash (`resembl/cli.py`, `db_verify_backup` in
  `resembl/core.py`).

`list` (`resembl/cli.py:1198`) prints every snippet in the corpus to stdout
unless `--range` narrows it, and `verify` (`resembl/cli.py:1361`) counts
snippets and buckets and checks index consistency across the whole corpus, so
both walk the corpus with no row cap; `stats` (`resembl/cli.py:1336`) and
`clean` (`resembl/cli.py:1739`) read or drop the LSH index, and `clean` is
the only command that destroys derived state. None of the four is a network
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
| `RESEMBL_SEED` | `resembl/diagnostics.py:35`, `resembl/diagnostics.py:61` | Trusted verbatim; seeds the one generator every MinHash sample in a run draws from, so it changes the fingerprints that run produces. |
| `RESEMBL_NOW` | `resembl/models.py:37`, `resembl/models.py:48` | Trusted verbatim; replaces the created-at clock for every row and for backup filenames/manifests. Refused unless it parses as ISO 8601. |
| `~/.config/resembl/config.toml` | `resembl/paths.py:106` | Parsed as TOML (`resembl/config.py:270`), then every value coerced to its field's type and range-checked (`resembl/config.py:157`, `resembl/config.py:74`). Written mode `0600` (`save_config` in `resembl/config.py`, `6273fa0`). |

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

**B3, environment to runtime.** The variables above are read at call time and
are never validated beyond being non-empty. `RESEMBL_SEED` and
`RESEMBL_NOW` are the sharp two: neither is a path, and both change results
without changing what the operator can see (`resembl/diagnostics.py:61`,
`resembl/models.py:48`).

**B4, application to database.** The ORM parameterizes every query, so
untrusted text is not concatenated into SQL. Three places build a `LIKE`
pattern from caller input: the checksum-prefix resolver escapes the wildcard
characters (`resembl/cli.py:726`), the model's JSON-name containment lookup
escapes them (`resembl/models.py:180`), while the name search does not
(`resembl/core.py:1612`). The result set is capped by `limit`, so the
unescaped wildcards widen the match set rather than the returned page.

**B5, cache directory to process.** The port file is read as an integer and used to open an HTTP connection (`resembl/find_client.py:121`, `resembl/cli.py:429`). Its contents are authenticated by nothing. The file names a port only: both clients dial `127.0.0.1` (`resembl/find_client.py:152`, `resembl/cli.py:436`), so a forged port reaches a listener the forger must already be running on the loopback interface. The standalone client narrows this by refusing a port outside 1024-65535 before dialing (`6273fa0`, `_main` in `resembl/find_client.py`), so a forged port naming a privileged service is rejected; a forged port inside the ephemeral range and the in-process `find` client remain unchecked.

**B6, merge source to local corpus.** Rows from another database are
inserted after structural checks, not after a trust decision about the source
(`resembl/core.py:2036`).

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
verify the engine stopped before repairing a slot.

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
| Database backups | A full, restorable copy of the corpus; a tampered backup restores a wrong corpus (R17) | caller-chosen backup directory (`db_backup` in `resembl/core.py`), backup, per-backup manifest `<backup-stem>.json`, and `<backup-stem>.config.toml` config sidecar, all mode `0600` |

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
range, and a non-loopback bind is unrestricted, since it is reached by
whatever name resolves to it. A verb the handler does not implement is answered with the JSON `405` envelope instead: `__getattr__` resolves any unhandled `do_*` name to the shared not-allowed answer (`resembl/server.py:1209`), and `send_error` answers stdlib errors, including malformed request lines, through `_respond` (`resembl/server.py:1712`), so no response skips the hardening header block. Earlier revisions of this model carried the stdlib's header-less HTML `501` as a live finding; the code no longer emits it.

*Denial of service.* `ThreadingHTTPServer` spawns a thread per connection, and `_FindServer` bounds that with a 96-slot semaphore (`_MAX_HANDLER_THREADS`, `resembl/server.py:121`, acquired in `process_request` at `resembl/server.py:1872`): excess connections wait in the listen backlog rather than pinning a thread. Each thread holds a 30-second idle timeout (`resembl/server.py:1104`). The engine pool is 32 plus 64 overflow (`resembl/server.py:1885`). A client that opens connections faster than requests complete therefore exhausts pool connections and file descriptors, but not handler threads; there is still no per-caller rate limit or request accounting, so the backlog and the pool are the policy.

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
expected tables, runs `PRAGMA integrity_check`, and compares the live
count and `content_hash` against the manifest: a tampered backup whose
manifest is edited to match passes (R17). Newer backups also carry a
`<backup-stem>.config.toml` sidecar, copied verbatim from the live
`config.toml` and hashed as `config_sha256` in the manifest; when the
manifest records that hash, `db_verify_backup` requires the sidecar to
exist and hash to it, and a missing or mismatched sidecar is a hard
failure (`8616290`). Backup, manifest, and sidecar are written mode
`0600` (`db_backup` in `resembl/core.py`) but the manifest is otherwise
unauthenticated, so anyone who can write the backup directory can forge
all three. `db_backup` re-verifies the just-published file and deletes
it on failure, so a backup that `backup` reported as success has
verified at write time; that does not make a later `verify-backup` of a
hand-supplied triple trustworthy.

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
`resembl/scoring.py:1014`), and merge recomputes from the source row's own
code rather than deserializing (`db_merge` in `resembl/core.py:2770`).

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
| Non-loopback warning | `resembl/cli.py:603` | Informs the operator; does not prevent the bind |
| Loopback `Host` check, 403 | `_host_allowed` in `resembl/server.py:1301`, `_loopback_allowed_hosts` in `resembl/server.py:672` | A page that rebinds its own hostname onto the loopback port reading a served query |
| Request body cap, 8 MiB / 413 envelope | `_MAX_BODY_BYTES` in `resembl/server.py:644`, `_read_body` in `resembl/server.py:1274` | B1 memory exhaustion; oversized bodies now answer `413` (`2cd23c9`), indistinguishable-from-malformed no longer |
| JSON depth and shape guard | `_read_body` in `resembl/server.py:1254` | `RecursionError` from a nested body killing the handler thread with no response |
| Batch query cap, 1000 | `MAX_BATCH_QUERIES` in `resembl/server.py:652` | B1 work per request |
| Find parameter range checks | `_parse_find_request` in `resembl/server.py:371` | NaN, out-of-range, and degenerate fingerprints |
| Query field check, string and non-blank | `_query_field_error` in `resembl/server.py:705` | A non-string query crashing the lexer into a `500` with its message, and a blank one answering `200` with no matches |
| `top_n` bounds, 1 to 1000 | `resembl/server.py:426`, `resembl/server.py:469`, clamped default at `resembl/server.py:1950` | One response returning the whole corpus (upper bound), or a `200` with an empty `matches` read as "no duplicates" (lower bound). A sequence of requests is not bounded |
| Index-parameter match check | `resembl/server.py:502` | A request rebuilding the shared LSH index mid-serve |
| Per-count MinHash template cap, 8 | `resembl/scoring.py:72` | An unbounded dict grown by cycling permutation counts |
| Result-cache key is a query digest | `resembl/server.py:618` | Query text retained by the result cache, and an entry count standing in for a memory bound |
| Result-cache key hides the database password | `_find_one` in `resembl/server.py` (`6273fa0`) | Credentials retained in the long-lived cache keys |
| Per-key compute lock | `resembl/server.py:121` | A burst of identical queries multiplying one find across connections |
| Content-Type check, 415 | `_content_type_ok` in `resembl/server.py:1292` | B1 request shape |
| Backup published only after verification | A truncated or page-flipped copy must not be handed to a restore drill as the newest restore point | `db_backup` in `resembl/core.py` re-verifies with `db_verify_backup` and removes the file and manifest on failure; `backup` CLI in `resembl/cli.py` exits 1 when `manifest_written` is false |
| LSH metadata read is fail-closed | A transient `database is locked` read must not trigger a rebuild that overwrites a live index's metadata mid-build | `lsh_meta_get` in `resembl/lsh.py` returns `None` only for a missing table; any other `OperationalError` is logged and re-raised |
| CSV export formula guard covers Unicode leading characters | A formula hiding behind any Unicode space, BOM, or invisible character is as live in a spreadsheet as a bare `=1+1` | `_csv_safe` in `resembl/cli.py` strips whitespace and BOM/zero-width characters before checking `_CSV_FORMULA_PREFIXES` |
| YARA export escapes code points above U+00FF | A hex escape on a surrogate or high code point parses as two hex digits plus literal text, corrupting the emitted pattern | `_yara_string_escape` in `resembl/core.py` writes UTF-8 bytes for code points above U+00FF and `?` for lone surrogates |
| Engine error detail stays in the operator log | A caller must not learn internals the engine puts in its `error` field, while operators must see why a request failed | `LocalEngine.find` in `service/http_engine.go` folds the engine's `error` into its error; `service/service.go` logs it and answers the caller only `engine unavailable` |
| Generic 500 body, details to log | `resembl/server.py:1525` | B1 error-text disclosure of SQL, bind parameters, and paths |
| Response hardening headers | `_respond` in `resembl/server.py:1712` | Browser reinterpretation of a snippet-derived response; caching of that response. Every response, including an unhandled verb (`resembl/server.py:1209`) and a stdlib error (`resembl/server.py:1197`), goes through this block |
| Handler-thread bound, 96 | `resembl/server.py:121`, `resembl/server.py:1872` | A stalled-client flood accumulating unbounded handler threads; excess connections stay in the listen backlog |
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
| Docker build context exclusions | `.dockerignore` (root and `service/.dockerignore`) | Local databases, benchmark data, and build output shipped in the service image context (`docker build -f service/Dockerfile .` uses the repository root as context) |
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
| Pinned index digest and writer fence | `_handle_find_pinned` in `resembl/server.py:1529`, `resembl/pinned_snapshot.py:9` | B9 index identity and writer fence; bounded scan |
| Runtime manifest verification | resembl/runtime_attestation.py:20 | B9 file integrity of loaded code under a trusted supervisor |
| Release provenance | `.github/workflows/build.yml:102` (the `attest` job) | An artifact published under this project's name that this repository did not build |

### Threats with no mitigation

Ranked as in the summary. R13, R14, R15, and R16 are the Go service gaps: R13 and R14 have no mitigation beyond the loopback isolation that service/README.md requires, R15 has none beyond the loopback default, and R16 is mitigated only by operator repair of custody slots. R10 (unbounded
`--file` read) has no control at all. R3 (unauthenticated port file) is
narrowed but not closed: the standalone client refuses an out-of-range
port (`6273fa0`) while an in-range forged port and the in-process
`find` client remain unchecked. R1's `top_n` cap bounds one response
but not a sequence of them, R2's is a `Host` check that a non-loopback bind
does not get, and R4's is a log level the operator has to ask for. R5
and R6 are accepted by design and documented in `SECURITY.md` rather than
enforced.
R3's server-side `0o700` closes the case where `serve` created the cache
directory itself and leaves open the case where something else created it
first, which is the case a shared cache directory actually is, and the
standalone client's port-range check (`6273fa0`) closes only the
privileged-port sub-case of the forged-port attack. R6's
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
guard means a non-SQLite deployment gets no caching at all and no
cross-process invalidation. A key that is not cached yet is computed once
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
client `resembl-find` refuses a port outside 1024-65535 before dialing
(`6273fa0`, `_main` in `resembl/find_client.py`), so naming a privileged
service port no longer works there; a forged port inside the ephemeral
range, and the in-process `find` client, remain unchecked. Enabled by
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
(`resembl/server.py:1521`) and a request slower than 1s at WARNING
(`resembl/server.py:1790`); successful queries are logged only under
`serve -v`.

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
