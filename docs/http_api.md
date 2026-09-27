# HTTP API (`resembl serve`)

`resembl serve` starts a loopback HTTP server that answers find queries from
a warm engine and LSH index. `resembl find` and `resembl find-batch` use it
automatically when it is running; the endpoints are documented here so other
clients can use the same server.

The server has no authentication: it is a single-user local tool, not a
service. It binds `127.0.0.1` by default, but `--host` overrides that and a
non-loopback address only prints a warning; the bind proceeds. Any process
that can reach the port can read the whole snippet corpus. Do not expose the
port.

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
`server_<dbhash>.port` in the cache dir (`RESEMBL_CACHE_DIR`, else
`$XDG_CACHE_HOME/resembl`, else `~/.cache/resembl`). The hash is the first 12
hex digits of the SHA1 of the **resolved** database URL (the engine's URL as
rendered with the password intact, falling back to `sqlite:///assembly.db`
when no database URL is set in the environment), so one process can serve several databases. The
file is removed when the server exits.

```bash
# any terminal; the digest below must be of the *resolved* URL, so set
# the database URL even when you rely on the default.
export RESEMBL_DATABASE_URL=sqlite:///assembly.db
resembl serve &

port=$(cat ~/.cache/resembl/server_$(printf %s "$RESEMBL_DATABASE_URL" | sha1sum | cut -c1-12).port)
curl -s "http://127.0.0.1:$port/find" \
  -H 'Content-Type: application/json' \
  -d '{"query": "push ebx\nmov eax, 5\npop ebx\nret"}'
```

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/find` | Find matches for one query. |
| `POST` | `/find-batch` | Find matches for many queries in one request. |
| `GET` | `/health` | Whether this process can still serve a find. |
| `GET` | `/metrics` | Request, latency and cache counters (Prometheus text format). |

The two `POST` endpoints are read-only, and the path is matched verbatim: a
query string makes `/find?x=1` an unknown path, answered `404`. `PUT`,
`DELETE`, `PATCH`, `HEAD` and `OPTIONS` are answered `405` on any path (with
an `Allow: POST` header), as is a `GET` to any path other than `/health` and
`/metrics`. A `HEAD` answer carries those headers and no body bytes, as
`HEAD` requires. Any other verb is answered by the stdlib base class with
`501` and an HTML body. Every response this server produces itself, success or
error, is `Content-Type: application/json`, except `/metrics`.

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

### `GET /health`

For a supervisor, a load balancer or a shell check. It runs one `SELECT 1`
against the served database and nothing else: the fingerprint migration and
the LSH index build happen once at startup, so re-running them here would
report a slow server as an unhealthy one, and the engine is the only
dependency that can stop a find from being answered.

```json
{"status": "ok", "database": "ok", "uptime_seconds": 1284.31, "latency_ms": 0.21}
```

A database that cannot be reached answers `503` with
`{"status": "degraded", "database": "unavailable", "error": "OperationalError",
"uptime_seconds": ...}`: `error` is the exception's *type*, never its message,
which carries SQL and file paths. A failed probe is a normal answer, not an
incident, so it is logged at WARNING once per poll rather than raising a
traceback.

### `GET /metrics`

Counters for the served request path, in the Prometheus text exposition
format (`Content-Type: text/plain; version=0.0.4`). The only label is the
request path, and a path outside `/find`, `/find-batch`, `/health` and
`/metrics` is folded into `other`, so a crafted path cannot grow the series
count.

| Metric | Type | Meaning |
| ------ | ---- | ------- |
| `resembl_server_up` | gauge | `1` while this process answers requests. |
| `resembl_server_uptime_seconds` | gauge | Seconds since the process started serving. |
| `resembl_server_requests_total{path,status}` | counter | Requests answered. |
| `resembl_server_request_duration_seconds{path}` | histogram | Latency to the response headers, in the fixed buckets `0.001` … `30` plus `+Inf`. |
| `resembl_server_result_cache_total{result}` | counter | Result-cache lookups, `hit` or `miss`. |
| `resembl_server_result_cache_hit_ratio` | gauge | Share of lookups answered from the version-guarded cache. |
| `resembl_server_query_errors_total{path}` | counter | Queries that failed inside an otherwise-`200` `/find-batch` request. |

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
| `405` | A method other than `POST` (including `HEAD` and `OPTIONS`, which carry the same headers without a body), or a `GET` to a path other than `/health` and `/metrics`. |
| `415` | An explicit `Content-Type` other than `application/json`. |
| `500` | An unexpected server-side failure. The message is generic; the details are in the server log. |
| `503` | `/health` only: the served database did not answer its probe. |

## Other response headers

`Cache-Control: no-store` on every response: results follow the database,
which the server cannot watch for external writers. `X-Content-Type-Options`,
`X-Frame-Options`, and `Content-Security-Policy` are set so a browser pointed
at the port cannot reinterpret a response as a page.

## Logging

Every record the server makes leads with a correlation id, twelve hex digits
of a fresh id per request. A `500` traceback, a failed `/find-batch` query, a
slow request and the request's own DEBUG line all quote the same one, so a
client report that carries an id (or a timestamp and a peer) reaches the rest.

At the default level the server records two things: a `500`, with its
traceback, and a request slower than a second. A served find is ~1.4 ms, so
the slow-request WARNING is the signal that the warm process has stopped
being warm; it names the id, the verb, the path, the status and the elapsed
time.

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
