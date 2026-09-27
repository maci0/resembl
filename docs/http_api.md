# HTTP API (`resembl serve`)

`resembl serve` starts a loopback HTTP server that answers find queries from
a warm engine and LSH index. `resembl find` and `resembl find-batch` use it
automatically when it is running; the endpoints are documented here so other
clients can use the same server.

The server binds `127.0.0.1` and has no authentication: it is a
single-user local tool, not a service. Do not expose the port.

## Discovery

`resembl serve` writes the port it bound to
`server_<dbhash>.port` in the cache dir (`RESEMBL_CACHE_DIR`, else
`$XDG_CACHE_HOME/resembl`, else `~/.cache/resembl`). The hash is the first 12
hex digits of the SHA1 of the **resolved** database URL (the engine's URL as
rendered with the password intact, falling back to `sqlite:///assembly.db`
when `DATABASE_URL` is unset), so one process can serve several databases. The
file is removed when the server exits.

```bash
# any terminal; the digest below must be of the *resolved* URL, so set
# DATABASE_URL even when you rely on the default.
export DATABASE_URL=sqlite:///assembly.db
resembl serve &

port=$(cat ~/.cache/resembl/server_$(printf %s "$DATABASE_URL" | sha1sum | cut -c1-12).port)
curl -s "http://127.0.0.1:$port/find" \
  -H 'Content-Type: application/json' \
  -d '{"query": "push ebx\nmov eax, 5\npop ebx\nret"}'
```

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/find` | Find matches for one query. |
| `POST` | `/find-batch` | Find matches for many queries in one request. |

Both are read-only. Any other method on either path answers `405`; any other
path answers `404`. Every response, success or error, is
`Content-Type: application/json`.

### `POST /find`

Request fields (all optional except `query`; an explicit `null` means "use
the server's configured default", exactly like an omitted field):

| Field | Type | Constraint |
| ----- | ---- | ---------- |
| `query` | string | required |
| `top_n` | integer | default from the server's config |
| `threshold` | number | `0.0` to `1.0`, and high enough to leave at least 2 LSH bands for `num_permutations` |
| `normalize` | boolean | default `true` |
| `ngram_size` | integer | at least `1` |
| `num_permutations` | integer | `2` to `resembl.scoring.MAX_NUM_PERM` |
| `jaccard_weight` | number | `0.0` to `1.0` |

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
| `400` | Unparseable body, a missing or wrongly typed required field, or a parameter outside its documented range. The message names the field. |
| `404` | Unknown path. |
| `405` | A method other than `POST`. |
| `415` | An explicit `Content-Type` other than `application/json`. |
| `500` | An unexpected server-side failure. The message is generic; the details are in the server log. |

## Other response headers

`Cache-Control: no-store` on every response: results follow the database,
which the server cannot watch for external writers. `X-Content-Type-Options`,
`X-Frame-Options`, and `Content-Security-Policy` are set so a browser pointed
at the port cannot reinterpret a response as a page.
