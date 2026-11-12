# HTTP API (`resembl serve`)

`resembl serve` starts a loopback HTTP server that answers find queries from
a warm engine and LSH index, and scores listings a caller sends. `resembl find`
and `resembl find-batch` use it automatically when it is running; the
endpoints are documented here so other clients can use the same server.

The server has no authentication: it is a single-user local tool, not a
service. It binds `127.0.0.1` by default. A non-loopback `--host` is refused
unless `--allow-unauthenticated` is given, in which case it prints a warning
and proceeds. Any process that can reach the port can read the whole snippet
corpus. Do not expose the port; only use `--allow-unauthenticated` behind a
network gate that provides authentication. On a non-loopback bind the
computed endpoints `POST /score`, `POST /band-keys`, `POST /scheme` and
`GET /metrics` answer `403` with `{"error": "endpoint not exposed on
non-loopback bind"}`: a non-loopback bind is reachable by any host that can
route to the port and the server has no authentication, so only `/health`
and the `/find` family (which keeps its existing operator gate) answer
there. Loopback binds answer normally.

A loopback bind additionally checks the request's `Host` header and answers
`403` to anything but `127.0.0.1`, `localhost` or `::1` (with or without the
port). That closes DNS rebinding: a page that points its own hostname at
127.0.0.1 reaches the endpoint same-origin, with no CORS preflight and a
readable response, and the `Host` header still carries the rebound name. A
non-loopback bind accepts any `Host`, since it is reached by whatever name
resolves to it.

`docs/THREAT_MODEL.md` records this boundary and the rest of the surface.

## Discovery

`resembl serve` writes the port it bound to
`server_<dbhash>.port` in the cache directory (resolved as
[man.md](man.md#environment) describes). The hash is the first 12
hex digits of the SHA-256 of the **resolved** database URL (the engine's URL as
rendered with the password intact, falling back to `sqlite:///assembly.db`
when no database URL is set in the environment), so one process can serve several databases. The
file is removed when the server exits.

Set `RESEMBL_DATABASE_URL` even when you rely on the default (the digest must
be of the *resolved* URL), start `resembl serve`, then print the port file's
path with Python (stdlib only) so the same command works on Linux, macOS and
Windows where `sha256sum`/`cut`/`$()` do not exist:

```bash
export RESEMBL_DATABASE_URL=sqlite:///assembly.db
resembl serve &
python3 -c "from resembl.paths import db_url_get, cache_dir_get, server_port_path; print(server_port_path(db_url_get(), cache_dir_get()))"
# On PowerShell, quote the -c argument with single quotes instead.
```

On PowerShell, set the variable with
`$env:RESEMBL_DATABASE_URL = "sqlite:///assembly.db"` and run the same
`python3 -c` command. Read the port from the printed file's contents and
post to `http://127.0.0.1:<port>/find` with any HTTP client (for example
`curl -s "http://127.0.0.1:<port>/find" -H 'Content-Type: application/json' -d '{"query": "push ebx\nmov eax, 5\npop ebx\nret"}'`).

## Stateless mode

`resembl serve --stateless` opens no database: no migration, no LSH index
build, no port file, and `resembl find` never discovers it. It answers
`/score`, `/band-keys`, `/scheme`, `/health` and `/metrics` on a loopback
bind, which read no
database; `/find`, `/find-batch`, `/find-pinned` and `GET /runtime-generation`
answer `501`. On a non-loopback bind `--stateless` still refuses the four
computed endpoints with `403` (see "HTTP API" above). The fingerprint defaults still come from `config.toml`. Replicas
share no state, so a load balancer can spread `/score` and `/band-keys` over
any number of them, and `/health` reports only that the process answers.

```bash
resembl serve --stateless --host 0.0.0.0 --port 8080 --allow-unauthenticated
```

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/find` | Find matches for one query. |
| `POST` | `/find-batch` | Find matches for many queries in one request. |
| `POST` | `/find-pinned` | Experimental, for `service/`: a find that succeeds only while the SQLite index digest and the runtime manifest match the request. |
| `POST` | `/score` | Score one listing against caller-supplied listings. Reads no database. |
| `POST` | `/band-keys` | LSH band keys of caller-supplied listings. Reads no database. |
| `GET` | `/runtime-generation` | Experimental: the process generation that `/find-pinned` requires. |
| `GET` | `/scheme` | The fingerprint scheme a `/score` with the server defaults uses. Reads no database. |
| `GET` | `/health` | Whether this process can still serve a find, or, under `--stateless`, answer at all. |
| `GET` | `/metrics` | Request, latency and cache counters (Prometheus text format). |

The `POST` endpoints are read-only. `Content-Length` accepts ASCII decimal
digits with optional surrounding HTTP whitespace; signs and separators are
rejected with `400`. The path is matched with any query
string removed, so `/find?x=1` is `/find` (parameters are read from the
body). `PUT`, `DELETE`, `PATCH`, `HEAD` and `OPTIONS` are answered `405`
on any known path (with an `Allow` header naming the verb that path
serves), as is a `GET` to any `POST` path and a `POST` to any `GET` path
(`/health`, `/scheme`, `/metrics`, `/runtime-generation`). A `HEAD`
answer carries those headers and no body bytes, as `HEAD` requires. Any
other verb is answered `405` with the same JSON error envelope. Every
response, success or error, is `Content-Type: application/json`, except
`/metrics`.

A connection is closed after 30 s idle, which bounds how long a keep-alive
connection holds its handler thread. The client side gives up sooner: 5 s for
a single `find`, 60 s for a `find-batch` chunk, after which `resembl` falls
back to the in-process path.

### `POST /find`

Request fields (all optional except `query`; an explicit `null` means "use
the server's configured default", exactly like an omitted field):

| Field | Type | Constraint |
| ----- | ---- | ---------- |
| `query` | string | required, and not blank. A blank one fingerprints to an empty token set and would answer `200` with an empty `matches`, which reads as "no matches" rather than as a bad request |
| `top_n` | integer | `1` to `1000`, else `400`. A non-positive one would answer `200` with an empty `matches`, which reads as "no matches" rather than as a bad request. A fractional value is rejected, not truncated. The default is the server's config, capped at `1000` (a config above it is reported once at startup, since a cap above the endpoint's own bound would refuse every request that omitted the field) |
| `threshold` | number | `0.0` to `1.0`, and high enough to leave at least 2 LSH bands for `num_permutations`. Must match the server's configured `lsh_threshold` (compared with a `1e-6` tolerance, since MySQL and DuckDB store it single-precision) |
| `normalize` | boolean | default `true` |
| `ngram_size` | integer | at least `1`, whole. Must equal the server's configured `ngram_size` |
| `num_permutations` | integer | `2` to `resembl.scoring.MAX_NUM_PERM`, whole. Must equal the server's configured `num_permutations` |
| `jaccard_weight` | number | `0.0` to `1.0` |

The three integer fields accept a JSON integer, an integral number
(`3.0`) or a decimal string (`"3"`); a fractional one is a `400`.

The LSH index is built once, at startup, for the server's configured
`threshold`, `ngram_size` and `num_permutations`, and is shared by every
request. A request naming different values answers `400`: serving it would
mean rebuilding that shared index inside a request thread while other
requests read it. Restart the server with the wanted settings (they come
from the same `config.toml` the CLI and the thin client read, so a matching
client needs no changes).

Unknown fields are ignored. `Content-Type` must be `application/json` when
present, and the body must be a JSON object no larger than 8 MiB.

Response `200`:

```json
{
  "lsh_candidates": 42,
  "matches": [
    {"checksum": "ab12…", "names": ["main", "alias"], "score": 97.5}
  ]
}
```

`matches` holds at most `top_n` entries, ordered by descending `score`.

### `POST /find-batch`

Same find parameters, plus:

| Field | Type | Constraint |
| ----- | ---- | ---------- |
| `queries` | list of string | required, at most 1000 entries, each one non-blank |

A parameter error is answered once, as a `400` for the whole request. A
*query* that cannot be processed fails alone: its entry carries an `error`
key and the other queries still return results, so the response is always
`200` for a well-formed request.

```json
{"results": [
  {"query": "push ebx", "lsh_candidates": 7, "matches": []},
  {"query": 42, "error": "query must be a string"},
  {"query": "   ", "error": "query must not be empty"}
]}
```

### `POST /score`

Scores `source` against each of `candidates` the way a find scores a
candidate snippet: `score_hybrid` of the MinHash Jaccard and
`rapidfuzz.fuzz.ratio`, 0 to 100, unrounded. The listings come from the
request, not from the database, so a caller can rank its own corpus.

| Field | Type | Constraint |
| ----- | ---- | ---------- |
| `source` | string | required, not blank |
| `candidates` | list of string | required, at most 1000 entries |
| `jaccard_floor` | number | `0.0` to `1.0`, default `0.0` |

The `/find` parameters `normalize`, `ngram_size`, `num_permutations` and
`jaccard_weight` apply with the same rules and defaults; the others are
validated and unused.

```json
{"scores": [97.5, null, 0.0],
 "scheme": {"id": "08905d3f77ba73b598d2bbb30c501682", "version": 3, "hash": "sha1-le32", "seed": 1,
            "num_permutations": 128, "ngram_size": 3, "normalize": true}}
```

`scores` holds one entry per candidate, in request order. A candidate whose
Jaccard is below `jaccard_floor` is `null`, and its text ratio is not
computed. A blank candidate scores `0.0`, or `null` under a positive floor.
`scheme` is the [fingerprint scheme](#fingerprint-scheme) the scores were
computed under.

### `POST /band-keys`

The LSH band keys of each listing's fingerprint, as `lsh.band_buckets`
computes them for the index: band `i` covers hash values `i * rows` to
`(i + 1) * rows - 1`, and its key is their lowercase hex, `8 * rows`
characters.

| Field | Type | Constraint |
| ----- | ---- | ---------- |
| `texts` | list of string | required, at most 1000 entries, each one non-blank |
| `bands` | integer | required, at least `1` |
| `rows` | integer | required, at least `1`; `bands * rows` at most `num_permutations` |

The `/find` fingerprint parameters apply as on `/score`.

```json
{"keys": [["0a1b…", "93c4…"], ["0a1b…", "77e0…"]],
 "scheme": {"id": "e4bd8fdd0017dea8499109ae47eacda3", "version": 3, "hash": "sha1-le32", "seed": 1,
            "num_permutations": 128, "ngram_size": 3, "normalize": true,
            "bands": 2, "rows": 8}}
```

### `GET /scheme`

`{"scheme": {...}}`: the scheme a `/score` that names no fingerprint parameter
uses on this server, which carries no band layout. A client can read it at
startup to see which settings the server computes with; the scheme of a
`/band-keys` answer adds `bands` and `rows`, so its `id` differs.

### Fingerprint scheme

Scores and band keys are comparable only when both sides were computed under
one fingerprint scheme. `/score` and `/band-keys` answer the scheme they used,
so a caller that stores band keys can store the scheme `id` with them and
refuse to compare keys whose ids differ.

```json
{"id": "82f09df515ac6a71875fc6bb6276ae41", "version": 3, "hash": "sha1-le32", "seed": 1,
 "num_permutations": 128, "ngram_size": 3, "normalize": true,
 "bands": 64, "rows": 2}
```

| Field | Meaning |
| ----- | ------- |
| `id` | 32 hex digits: SHA-256 over the other fields and the fingerprint (and, with `bands`, the band keys) of a fixed probe listing. |
| `version` | `resembl.models.FINGERPRINT_VERSION`, bumped whenever stored fingerprints or band keys would differ from a recomputation (algorithm, tokenization, normalization). |
| `hash`, `seed` | The MinHash element hash and the permutation stream's seed (`resembl.scoring.MINHASH_SEED`). |
| `num_permutations`, `ngram_size`, `normalize` | The request's fingerprint parameters. |
| `bands`, `rows` | `/band-keys` only: the band layout. |

Any change to a field changes `id`. Because the probe listing is fingerprinted
for real, a change to tokenization, normalization or shingle weighting also
changes `id` when no field changed. Two ids that differ mean the keys behind
them must not be compared; recompute the stored side.

### `POST /find-pinned`

Experimental, for the Go query service in `service/`. It takes the
`/find` body plus three fields, all required:

| Field | Type | Constraint |
| ----- | ---- | ---------- |
| `index_sha256` | string | 64 lowercase hex digits: the digest of the SQLite snippet, LSH and metadata rows |
| `artifact_sha256` | string | SHA-256 of the runtime manifest named by `RESEMBL_RUNTIME_MANIFEST` |
| `generation` | string | the value `GET /runtime-generation` returned for this process |

The server verifies the manifest against its executable and every loaded
module file, takes a SQLite writer fence (`BEGIN IMMEDIATE`), recomputes the
index digest, runs the find under the same fence, verifies the manifest again,
and echoes the three values with the result:

```json
{"index_sha256": "...", "artifact_sha256": "...", "generation": "...", "result": {"lsh_candidates": 0, "matches": []}}
```

| Status | When |
| ------ | ---- |
| `400` | A malformed `index_sha256`, a missing or blank `query`, or a find parameter error. |
| `409` | The index digest differs from `index_sha256`. |
| `503` | `RESEMBL_RUNTIME_MANIFEST` is unset, `artifact_sha256` or `generation` is missing or stale, the manifest check failed, the database is not SQLite, or the index needs a rebuild. The index is never rebuilt under the proof. |

The digest scan is limited to one million rows and 64 MiB of encoded data.
This is file integrity under a trusted supervisor, not remote attestation; see
`service/README.md`.

### `GET /runtime-generation`

`{"generation": "<32 hex digits>"}`: a random id fixed when this server
process starts. A restarted server has a new one, so a pin taken from an
earlier process is refused by `/find-pinned`.

### `GET /health`

For a supervisor, a load balancer or a shell check. It runs one `SELECT 1`
against the served database and nothing else: the fingerprint migration and
the LSH index build happen once at startup, so re-running them here would
report a slow server as an unhealthy one, and the engine is the only
dependency that can stop a find from being answered.

```json
{"status": "ok", "mode": "database", "database": "ok", "uptime_seconds": 1284.31, "latency_ms": 0.21}
```

A database that cannot be reached answers `503` with
`{"status": "degraded", "mode": "database", "database": "unavailable", "error": "OperationalError",
"uptime_seconds": ...}`: `error` is the exception's *type*, never its message,
which carries SQL and file paths. A failed probe is a normal answer, not an
incident, so it is logged at WARNING once per poll rather than raising a
traceback.

A `--stateless` server has no database to probe: answering is its readiness,
and the reply is always `200`:

```json
{"status": "ok", "mode": "stateless", "uptime_seconds": 1284.31}
```

### `GET /metrics`

Counters for the served request path, in the Prometheus text exposition
format (`Content-Type: text/plain; version=0.0.4`). The only label is the
request path, and a path outside `/find`, `/find-batch`, `/score`,
`/band-keys`, `/scheme`, `/health` and `/metrics` is folded into `other`, so a crafted
path cannot grow the series count.

| Metric | Type | Meaning |
| ------ | ---- | ------- |
| `resembl_server_up` | gauge | `1` while this process answers requests. |
| `resembl_server_uptime_seconds` | gauge | Seconds since the process started serving. |
| `resembl_server_requests_total{path,status}` | counter | Requests answered. |
| `resembl_server_request_duration_seconds{path}` | histogram | Latency to the response headers, in the fixed buckets `0.001` … `30` plus `+Inf`. |
| `resembl_server_result_cache_total{result}` | counter | Result-cache lookups, `hit` or `miss`. |
| `resembl_server_result_cache_hit_ratio` | gauge | Share of lookups answered from the version-guarded cache. |
| `resembl_server_query_errors_total{path}` | counter | Queries that failed inside an otherwise-`200` `/find-batch` request. |
| `resembl_server_handler_rejected_total` | counter | Connections closed because every handler slot was held; no HTTP response was written, so this is the saturation signal the request counter cannot see. |

A find is ~1.4 ms uncached, so the `le="0.001"` and `le="0.005"` buckets
separate a cache hit from a cold find. The counters are in-process and reset
when the server restarts; nothing is exported anywhere.

## Errors

Every error is `{"error": "<message>"}` with a `4xx` or `5xx` status:

| Status | When |
| ------ | ---- |
| `400` | Unparseable body, a body nested deeper than the JSON decoder's recursion limit, a missing, wrongly typed or blank required field, a parameter outside its documented range, or a `threshold` / `ngram_size` / `num_permutations` other than the ones the server's index was built for. The message names the field. |
| `403` | A loopback bind and a `Host` header that does not name it. |
| `404` | Unknown path. |
| `405` | A known path addressed with the wrong verb (including `HEAD` and `OPTIONS`, which carry the same headers without a body), with an `Allow` header naming the verb the path serves. |
| `413` | A request body larger than 8 MiB (the same status the Go service answers for the same cap). |
| `415` | An explicit `Content-Type` other than `application/json`. |
| `500` | An unexpected server-side failure. The message is generic; the details are in the server log. |
| `409` | `/find-pinned` only: the index digest does not match. |
| `503` | `/health`: the served database did not answer its probe. `/find-pinned`: see its table. |
| `501` | A `--stateless` server: `/find`, `/find-batch`, `/find-pinned` and `GET /runtime-generation`. The message names the mode. |

## Other response headers

`Cache-Control: no-store` on every response: results follow the database,
which the server cannot watch for external writers. `X-Content-Type-Options`,
`X-Frame-Options`, and `Content-Security-Policy` are set so a browser pointed
at the port cannot reinterpret a response as a page.

## Logging and the investigative path

Every record the server makes leads with a correlation id. A request that
carries its own `X-Request-Id` (the Go query service adopts the client's id
and forwards the id it minted for the client) keeps that id end to end; otherwise the server mints
twelve hex digits per request. A supplied id is adopted only when it is
bounded and printable, and anything else falls back to the minted one. A
`500` traceback, a failed `/find-batch` query, a slow request and the
request's own DEBUG line all quote the same one, so a client report that
carries an id (or a timestamp and a peer) reaches the rest. The id is also
echoed on the response as `X-Request-Id`.

At the default level the server records two things: a `500`, with its
traceback, and a request slower than a second. A served find is ~1.4 ms, so
the slow-request WARNING is the signal that the warm process has stopped
being warm; it names the id, the verb, the path, the status and the elapsed
time.

The signals connect through that one id. A metric anomaly on
`resembl_server_requests_total` or on `resembl_server_request_duration_seconds`
names a path and a status; the access line at DEBUG names the id, the peer,
the status and the latency for the individual request; and the id is echoed
on the response `X-Request-Id`, so a client report carrying it searches the
Go service's log, this server's records and the metric for the same request.
A failed `/find-batch` query is also counted on
`resembl_server_query_errors_total`, because the batch itself still answers
`200` and would otherwise be invisible to anything reading status codes.

Started with `-v` (`resembl serve -v`) it also records every request at
DEBUG: peer address, request line, status and latency. That is the fuller
trail a served query leaves, so it is the first thing to raise when
investigating a suspected scrape. The request line is request-controlled, so
control characters are stripped from the rendered record: a raw one lets a
crafted path write a second, forged log line.

## Tracing

The server is a single process with no upstream to correlate against, so it
emits no distributed trace. The correlation id above is the trace: one id per
request, on every record that request produces, and on the metric series for
its path.
