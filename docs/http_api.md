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

Both are read-only, and the path is matched verbatim: a query string makes
`/find?x=1` an unknown path, answered `404`. `GET`, `PUT`, `DELETE` and
`PATCH` are answered `405` on any path (with an `Allow: POST` header), so the
path is only checked for `POST`. Every response the server produces, success
or error, is `Content-Type: application/json`; a verb with no handler at all
(`HEAD`, `OPTIONS`, …) is answered by the stdlib base class with `501` and an
HTML body.

A connection is closed after 30 s idle, which bounds how long a keep-alive
connection holds its handler thread. The client side gives up sooner: 5 s for
a single `find`, 60 s for a `find-batch` chunk, after which `resembl` falls
back to the in-process path.

### `POST /find`

Request fields (all optional except `query`; an explicit `null` means "use
the server's configured default", exactly like an omitted field):

| Field | Type | Constraint |
| ----- | ---- | ---------- |
| `query` | string | required |
| `top_n` | integer | at most 1000, else `400`. The default is the server's config |
| `threshold` | number | `0.0` to `1.0`, and high enough to leave at least 2 LSH bands for `num_permutations`. Must match the server's configured `lsh_threshold` (compared with a `1e-6` tolerance, since MySQL and DuckDB store it single-precision) |
| `normalize` | boolean | default `true` |
| `ngram_size` | integer | at least `1`. Must equal the server's configured `ngram_size` |
| `num_permutations` | integer | `2` to `resembl.scoring.MAX_NUM_PERM`. Must equal the server's configured `num_permutations` |
| `jaccard_weight` | number | `0.0` to `1.0` |

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
| `queries` | list of string | required, at most 1000 entries |

A parameter error is answered once, as a `400` for the whole request. A
*query* that cannot be processed fails alone: its entry carries an `error`
key and the other queries still return results, so the response is always
`200` for a well-formed request.

```json
{"results": [
  {"query": "push ebx", "lsh_candidates": 7, "matches": []},
  {"query": "mov eax", "error": "query must be a string"}
]}
```

## Errors

Every error is `{"error": "<message>"}` with a `4xx` or `5xx` status:

| Status | When |
| ------ | ---- |
| `400` | Unparseable body, a body nested deeper than the JSON decoder's recursion limit, a missing or wrongly typed required field, a parameter outside its documented range, or a `threshold` / `ngram_size` / `num_permutations` other than the ones the server's index was built for. The message names the field. |
| `403` | A loopback bind and a `Host` header that does not name it. |
| `404` | Unknown path. |
| `405` | A method other than `POST`. |
| `415` | An explicit `Content-Type` other than `application/json`. |
| `500` | An unexpected server-side failure. The message is generic; the details are in the server log. |

## Other response headers

`Cache-Control: no-store` on every response: results follow the database,
which the server cannot watch for external writers. `X-Content-Type-Options`,
`X-Frame-Options`, and `Content-Security-Policy` are set so a browser pointed
at the port cannot reinterpret a response as a page.

## Logging

The server logs nothing at the default level. Started with `-v` (`resembl
serve -v`) it records every request at DEBUG: peer address, request line and
outcome, with control characters stripped from the request line so a crafted
path cannot forge a second record. That is the only trail a served query
leaves, so it is the first thing to raise when investigating a suspected
scrape.
