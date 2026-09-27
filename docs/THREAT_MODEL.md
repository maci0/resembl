# resembl threat model

Last reviewed: 2026-09-27, against the code at `resembl/` version 2.0.0.

Scope: this file models what can be attacked in resembl. Point
vulnerabilities and their fixes are not tracked here; each risk below names
the code that carries it so a review pass can re-verify it.

resembl is a single-user developer tool. It has no accounts, no
multi-tenancy, and no authorization concept anywhere in the codebase. Every
"user" in this model is a human or process already holding the same OS
account as the tool, except for the loopback HTTP boundary, which is the one
place where a peer need not be the same user.

## Risk summary

Ranked by exploitability against the shipped defaults, then by impact.

| # | Risk | Boundary | Impact | Control today |
| - | ---- | -------- | ------ | ------------- |
| R1 | `top_n` is accepted from the request with no upper bound, so one unauthenticated `POST /find` can return the entire corpus | B1 | Full corpus disclosure to any local process; memory pressure on the server | none (`resembl/server.py:165`, used at `resembl/server.py:279`) |
| R2 | The find server has no authentication, no `Host` check, and no rate limit; the non-loopback bind is a warning, not a block | B1 | R1 plus sustained exhaustion, and browser reachability via DNS rebinding | Loopback default only (`resembl/cli.py:477`, `resembl/cli.py:490`) |
| R3 | The port file is an unauthenticated channel: both clients trust its contents and forward the query text to whatever port it names | B5 | Query text (attacker-supplied source code) redirected to an attacker-controlled listener | none (`resembl/find_client.py:171`, `resembl/cli.py:332`) |
| R4 | The server logs nothing per request, so queries are unattributable after the fact | B1 | No way to investigate a suspected scrape or a hostile client | none (`resembl/server.py:582`) |
| R5 | `RESEMBL_DATABASE_URL` / `DATABASE_URL`, `RESEMBL_CACHE_DIR`, and `RESEMBL_CONFIG_DIR` are trusted verbatim from the environment, including credentials and a remote host | B3 | Database pointed at attacker-chosen host; config read from attacker-chosen directory | none, by design (`resembl/database.py:33`, `resembl/cache.py:76`, `resembl/config.py:114`) |
| R6 | `merge` opens an arbitrary source URL, including a credentialed remote database, and inserts its rows | B6 | Rows from a hostile source enter the corpus; credentials sent to a chosen host | Fingerprints recomputed, never deserialized (`resembl/core.py:1855`) |
| R7 | `import` reads every `.asm`/`.txt` under a directory and lexes it, with no size or count cap | B2 | Memory and CPU exhaustion on a hostile directory | Chunked writes only (`resembl/cli.py:880`) |
| R8 | The in-process result cache keeps the 128 most recent query-and-result pairs resident | B1 | Query text (source under analysis) sits in process memory beyond the request | 128-entry LRU (`resembl/server.py:60`) |
| R9 | The port-file name is a 12-hex-digit SHA1 of the raw, unmasked database URL | B5 | A truncated hash of a credentialed URL is written to disk under the cache dir | none (`resembl/server.py:327`, `resembl/find_client.py:57`) |

## Attack surface

Every entry point below is present in the code at the cited location.

### Network listeners

| Entry point | Location | Notes |
| ----------- | -------- | ----- |
| `POST /find` | `resembl/server.py:430` | Unauthenticated, read-only. |
| `POST /find-batch` | `resembl/server.py:444` | Unauthenticated, up to 1000 queries per request (`resembl/server.py:509`). |
| `resembl serve` bind | `resembl/cli.py:754` | `--host` is free-form; anything but loopback only prints a warning (`resembl/cli.py:490`). |

No other method is served: `GET`, `PUT`, `DELETE`, and `PATCH` answer `405`
(`resembl/server.py:454`). A `POST` to any other path answers `404`
(`resembl/server.py:431`).

### CLI surface

`resembl` (`resembl/cli.py:1962`) exposes about 30 commands. The ones that
take untrusted input from outside the tool's own process:

- `import <directory>` walks the tree and reads every `.asm`/`.txt`
  (`resembl/cli.py:869`).
- `merge <source>` opens a file path or a full database URL
  (`resembl/core.py:1787`).
- `find`, `find-batch`, `search`, `compare` take query text and patterns
  (`resembl/cli.py:1258`, `resembl/cli.py:1351`).
- `export <file>`, `export-yara <file>` write to a caller-chosen path
  (`resembl/cli.py:759`, `resembl/cli.py:789`).
- `add`, `show`, `rm`, `name *`, `tag *`, `collection *` write to the
  database.

`resembl-find` (`resembl/find_client.py:238`) is a second entry point: it
reads a query from `--query` or an arbitrary `--file` path
(`resembl/find_client.py:140`) and POSTs it to the loopback server.

### Environment and configuration

| Input | Location | Trust |
| ----- | -------- | ----- |
| `RESEMBL_DATABASE_URL`, then `DATABASE_URL` | `resembl/database.py:33` | Trusted verbatim; may carry a password. |
| `RESEMBL_CACHE_DIR` | `resembl/cache.py:76` | Trusted verbatim; selects where the port file is written and read. |
| `RESEMBL_CONFIG_DIR` | `resembl/config.py:114` | Trusted verbatim; selects the config file. |
| `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` | `resembl/config.py:117`, `resembl/cache.py:79` | Trusted when the override above is unset. |
| `~/.config/resembl/config.toml` | `resembl/config.py:123` | Parsed as TOML, then every value coerced to its field's type and range-checked (`resembl/config.py:188`, `resembl/config.py:75`). |

### Files the tool writes

The port file (`resembl/server.py:773`), the config file
(`resembl/config.py:262`), exported YARA rules (`resembl/core.py:960`), and
the database itself. Each is published with write-temp-then-rename
(`resembl/config.py:262`, `resembl/server.py:773`, `resembl/core.py:960`).

## Trust boundaries

**B1, process to loopback socket.** The largest boundary in the codebase, and
the only one a peer can cross without the same OS account. Everything the
server accepts is treated as untrusted: body size
(`resembl/server.py:336`), content type (`resembl/server.py:420`), JSON
object shape (`resembl/server.py:417`), field types
(`resembl/server.py:475`), and the range of `threshold`, `jaccard_weight`,
`ngram_size`, and `num_permutations` (`resembl/server.py:152`).

**B2, files and arguments to library code.** Snippet text is untrusted
input. It is lexed and fingerprinted (`resembl/scoring.py:927`), and on
export it is embedded in generated YARA rules.

**B3, environment to runtime.** The four environment variables above are read
at import time or on first use and are never validated beyond being
non-empty.

**B4, application to database.** The ORM parameterizes every query, so
untrusted text is not concatenated into SQL. Two places build a `LIKE`
pattern from caller input: the checksum-prefix resolver escapes the wildcard
characters (`resembl/cli.py:604`), while the name search does not
(`resembl/core.py:1432`). The result set is capped by `limit`, so the
unescaped wildcards widen the match set rather than the returned page.

**B5, cache directory to process.** The port file is read as an integer and
used to open an HTTP connection (`resembl/find_client.py:171`,
`resembl/cli.py:332`). Its contents are authenticated by nothing.

**B6, merge source to local corpus.** Rows from another database are
inserted after structural checks, not after a trust decision about the
source (`resembl/core.py:1787`).

**B7, secrets to code.** The database URL may embed a password. It is masked
for display (`resembl/database.py:57`, `resembl/find_client.py:41`) but used
unmasked to derive the port-file name (`resembl/server.py:327`) and to open
the connection.

## Assets

| Asset | Why it matters | Held in |
| ----- | -------------- | ------- |
| Snippet corpus | Proprietary reverse-engineering data; the reason the tool exists | `snippet` table, served over B1 |
| Database credentials | Reach to a shared Postgres/MySQL corpus | `RESEMBL_DATABASE_URL` / `DATABASE_URL`, process environment |
| LSH index | Minutes of CPU to rebuild; its loss is an availability hit, not a data loss | `lsh_bucket` table |
| Warm server availability | The performance reason to run `serve` at all | `resembl/server.py:651` |
| Port file | Discovery of the running server, and a redirect target | cache dir |
| Exported YARA rules | Downstream detection artifacts | caller-chosen output file |

## Threats per boundary

### B1, loopback socket

*Information disclosure.* Any process on the host can enumerate the corpus.
A permissive `threshold` with a large `top_n` returns `lsh_candidates`
worth of rows in one response. `top_n` is the only find parameter with no
range check (`resembl/server.py:165`); every other one is bounded, so this is
a gap in a row of otherwise deliberate limits.

A request may also name a `threshold`, `ngram_size`, or `num_permutations`
that differs from the one the server's index was built for. That is refused
with a `400` (`resembl/server.py:246`), because honouring it would rebuild
the shared `lsh_bucket` table from inside a request thread while other
threads read it.

A browser on the same host is a second peer, and the binding does not stop
it. The server sets no CORS headers, which blocks a cross-origin `fetch`
from *reading* the answer, but a page whose own origin is the one the
browser resolves to `127.0.0.1` (DNS rebinding) sends a same-origin request
with no preflight at all and reads the response. The handler inspects only
`Content-Length` and `Content-Type` (`resembl/server.py:409`,
`resembl/server.py:427`); it validates neither the `Host` nor the `Origin`
header, and the auto-assigned port (`resembl/cli.py:479`) is enumerable by a
page that probes the port range.

*Denial of service.* `ThreadingHTTPServer` spawns a thread per connection
with no cap; each thread holds a 30-second idle timeout
(`resembl/server.py:376`). The engine pool is 32 plus 64 overflow
(`resembl/server.py:686`). A client that opens connections faster than
requests complete exhausts threads, pool connections, and file descriptors
before any of the three limits is a policy decision.

*Repudiation.* `log_message` is a no-op (`resembl/server.py:582`), so nothing
records that a query was made, from where, or what was asked. Failures are
logged with `logger.exception` (`resembl/server.py:489`), which is the only
per-request trace that exists.

*Elevation of privilege.* None available: the server executes no request-
derived SQL text and writes nothing.

*Host header.* Unvalidated, as above. A `Host` check is the standard
rebounding defence and is the cheapest missing control on this boundary.

### B2, files and arguments

*Tampering.* `merge` accepts any source. Its rows, including names, tags,
and collections, are written into the local corpus.

*Denial of service.* `import` has no cap on file count or file size; the
chunked writes keep memory flat (`resembl/cli.py:880`) but not total work.
`find-batch --file` reads the whole file into a list before chunking the
requests (`resembl/cli.py:1378`).

### B3, environment

*Spoofing.* A process that can set `RESEMBL_CACHE_DIR` for the tool
controls where the port file is read from and can therefore redirect
`find`'s query text to a listener it owns (R3). `RESEMBL_CONFIG_DIR` does the
same for thresholds and permutation counts, which changes match results
silently rather than failing.

### B5, cache directory

*Tampering / spoofing.* `resembl/find_client.py:171` and `resembl/cli.py:332`
read the port file, parse an integer, and connect. A local process that can
write the cache directory controls the port every subsequent query is sent
to. Both files assume the cache directory is private to the user; nothing in
the code creates or checks its permissions.

*Tampering.* `port_file_cleanup` only removes the file while it still names
the port the caller failed on (`resembl/server.py:345`), so one process
cannot retire another server's advertisement by racing the exit path. That
control does not extend to a third party rewriting the file's contents.

### B6, merge source

*Tampering.* Rows of unknown provenance enter the corpus and are indexed.
The fingerprint path is the sharpest edge and is closed: a blob that is not
in the `RMLH` format raises `ValueError`
(`resembl/scoring.py:1253`), and merge recomputes from the source row's own
code rather than deserializing (`resembl/core.py:1855`).

*Information disclosure.* A `postgresql+pg8000://user:pass@host/db` source
sends those credentials to the named host. The URL is masked for display
(`resembl/find_client.py:41`) but that is a display control, not a network
one.

## Mitigations

| Control | Where | Covers |
| ------- | ----- | ------ |
| Loopback bind default | `resembl/cli.py:477` | Reduces B1 exposure to same-host processes |
| Non-loopback warning | `resembl/cli.py:490` | Informs the operator; does not prevent the bind |
| Request body cap, 8 MiB | `resembl/server.py:336` | B1 memory exhaustion |
| Batch query cap, 1000 | `resembl/server.py:509` | B1 work per request |
| Find parameter range checks | `resembl/server.py:152` | NaN, out-of-range, and degenerate fingerprints. `top_n` is not covered |
| Index-parameter match check | `resembl/server.py:246` | A request rebuilding the shared LSH index mid-serve |
| Per-count MinHash template cap, 8 | `resembl/scoring.py:71` | An unbounded dict grown by cycling permutation counts |
| Content-Type and JSON-object checks | `resembl/server.py:417` | B1 request shape |
| Generic 500 body, details to log | `resembl/server.py:493` | B1 error-text disclosure of SQL, bind parameters, and paths |
| Response hardening headers | `resembl/server.py:565` | Browser reinterpretation of a snippet-derived response |
| Double-serve check | `resembl/server.py:666` | Two servers advertising one database |
| Atomic file publication | `resembl/server.py:773`, `resembl/config.py:262`, `resembl/core.py:960` | Partial-file reads by a concurrent client |
| Fingerprint magic check | `resembl/scoring.py:1253` | Deserialization of attacker-controlled bytes from a hostile database or merge source |
| Legacy cache files never deserialized | `resembl/cache.py:8`, `resembl/cache.py:352` | Code execution from a planted cache file |
| YARA string escaping | `resembl/core.py:908` | A snippet name breaking out of a generated rule string |
| Config value coercion | `resembl/config.py:188` | A hand-edited config putting a raw TOML type into a numeric field |
| `LIKE` metacharacter escaping | `resembl/cli.py:604` | A checksum prefix like `%` resolving to an arbitrary snippet |
| Password masking | `resembl/database.py:57`, `resembl/find_client.py:41` | Credentials in a printed URL |
| Config file lock | `resembl/config.py:207` | Lost updates between concurrent CLI processes |
| Dependency CVE floor | `pyproject.toml:21` | The Pygments ReDoS advisory |
| CI security analysis | `.github/workflows/codeql.yml`, `.github/workflows/dependency-review.yml`, `.github/workflows/sbom.yml` | Static analysis, dependency review, and an SBOM of every release |

### Threats with no mitigation

Ranked as in the summary. R1 (unbounded `top_n`), R3 (unauthenticated port
file), and R4 (no request log) have no control at all. R2's mitigation is a
warning string. R5 and R6 are accepted by design and documented in
`SECURITY.md` rather than enforced.

### Single points of failure

The port file carries discovery, redirect trust, and lifecycle for the whole
warm-find path. One file, one parse, three responsibilities, no integrity
check on any of them.

The result cache (`resembl/server.py:60`) is the only thing standing between
a repeated expensive query and repeated full work. Its SQLite-only version
guard means a non-SQLite deployment gets no caching at all and no
cross-process invalidation.

## Abuse cases

Documented scenarios, each with the code path that enables it. Nothing here
was executed or tested against a running server.

**Corpus exfiltration by a local process.** A process on the same host reads
`~/.cache/resembl/server_<hash>.port`, POSTs
`{"query": "", "threshold": 0.0, "top_n": 1000000}` to
`127.0.0.1:<port>/find`, and receives the corpus. Nothing in the request
path distinguishes this from `resembl find`. Enabled by
`resembl/server.py:165` (no `top_n` bound) and `resembl/server.py:430` (no
caller authentication).

**Query text capture.** The same process, or any process that can write the
cache directory, replaces the port file's contents with its own port. Every
subsequent `resembl find` ships its query, which is attacker-supplied
disassembly, to the attacker's listener. Enabled by
`resembl/find_client.py:171` and `resembl/cli.py:332`.

**Result amplification.** `threshold` down, `top_n` up, repeated. Each
response is a fresh, complete copy of the corpus, served from a
`ThreadingHTTPServer` with no request accounting.

**Silent configuration redirection.** Setting `RESEMBL_CONFIG_DIR` for a
`resembl` invocation changes `lsh_threshold`, `ngram_size`, and
`num_permutations`. The tool does not report that it is running under a
non-default configuration, so match results change with no visible signal.

**Poisoned merge.** A source database with plausible rows and unusable
fingerprints has them recomputed from its own code
(`resembl/core.py:1855`). The corpus now contains code the operator never
reviewed, ranked by similarity alongside genuine findings.

A scenario that a previous revision of this model named, **warm-server
growth by cycling `num_permutations`**, no longer holds: such a request is
refused with a `400` (`resembl/server.py:246`), and the per-count MinHash
template cache is capped at eight entries (`resembl/scoring.py:71`) even for
callers that reach it in-process.

## Response readiness

`resembl serve` records nothing per request (`resembl/server.py:582`), so
there is no trail to investigate from after a suspected exfiltration. Errors
are logged with stack context (`resembl/server.py:489`); successful queries
are not logged at all.

No repository document describes the path from a reported vulnerability to a
shipped fix. `SECURITY.md` states the reporting channel; the rest of the path
is unstated.

## Related

- `SECURITY.md` for the reporting channel and supported versions.
- `docs/http_api.md` for the endpoint contract.
- `docs/adr/003-checksum-as-pk.md`, `docs/adr/004-database-backed-lsh-index.md`,
  `docs/adr/005-vendored-minhash.md` for the decisions that removed pickle
  deserialization from the data path.
