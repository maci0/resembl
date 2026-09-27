# resembl threat model

Last reviewed: 2026-09-27, against the code at `resembl/` version 2.0.0
(`pyproject.toml:3`). Every reference below was re-read in that tree on that
date.

Scope: what can be attacked in resembl, and what stands in the way. Point
vulnerabilities and their fixes are not tracked here; each risk names the code
that carries it so a review pass can re-verify it.

resembl is a single-user developer tool. It has no accounts, no
multi-tenancy, and no authorization concept anywhere in the codebase. Every
"user" in this model is a human or process already holding the same OS
account as the tool, except for the loopback HTTP boundary, which is the one
place where a peer need not be the same user.

## Risk summary

Ranked by exploitability against the shipped defaults, then by impact.

| # | Risk | Boundary | Impact | Control today |
| - | ---- | -------- | ------ | ------------- |
| R1 | `POST /find` is unauthenticated and answers any local caller, so the corpus is readable a page at a time | B1 | Full corpus disclosure to any same-host process; memory pressure on the server | `top_n` capped at 1000 per request (`resembl/server.py:354`, `resembl/server.py:560`), which bounds one response but not a sequence of them; the config layer leaves the local `top_n` unbounded (`resembl/config.py:63`) |
| R2 | The find server has no authentication and no rate limit, and a non-loopback bind is a warning, not a block | B1 | R1 plus sustained exhaustion | `Host` check on a loopback bind, which answers `403` to a rebound name (`resembl/server.py:707`, `resembl/server.py:724`); loopback default only (`resembl/cli.py:578`, `resembl/cli.py:603`) |
| R3 | The port file is an unauthenticated channel: both clients trust its contents and forward the query text to whatever loopback port it names | B5 | Query text (attacker-supplied source code) sent to an attacker-controlled loopback listener | none on a cache directory that already existed (`resembl/find_client.py:118`, `resembl/cli.py:429`); the server's own directory is `0o700` (`resembl/server.py:1133`) |
| R4 | Nothing is recorded per request at the default log level, so queries are unattributable unless `serve -v` is running | B1 | No way to investigate a suspected scrape or a hostile client | `serve -v` logs every request at DEBUG with its peer and outcome (`resembl/server.py:924`, `resembl/server.py:926`) |
| R5 | `RESEMBL_DATABASE_URL` / `DATABASE_URL`, `RESEMBL_CACHE_DIR`, and `RESEMBL_CONFIG_DIR` are trusted verbatim from the environment, including credentials and a remote host | B3 | Database pointed at an attacker-chosen host; config read from an attacker-chosen directory | none, by design (`docs/adr/006-paths-module-owns-environment.md`, `resembl/paths.py:57`, `resembl/paths.py:81`, `resembl/paths.py:97`) |
| R6 | `merge` opens an arbitrary source URL, including a credentialed remote database, and inserts its rows; a source that fails to open has the raw driver error text returned to the caller | B6 | Rows from a hostile source enter the corpus; credentials sent to a chosen host | Fingerprints recomputed, never deserialized (`resembl/core.py:2149`); the error path is not sanitized (`resembl/core.py:2064`) |
| R7 | `import` holds every matching path in memory and spawns a worker process per CPU over the set, and `reindex` does the same over every row of the corpus, with no count or size cap on either | B2 | Process, memory, and CPU exhaustion from a hostile directory | Chunked database writes only (`resembl/cli.py:1010`, `resembl/cli.py:1049`) |
| R8 | The in-process result cache keeps the 128 most recent find results resident | B1 | Snippet checksums and names (the result payload) sit in process memory beyond the request | 128-entry LRU whose key is a SHA-256 digest of the query, so the query text itself is not retained and the entry count is also a memory bound (`resembl/server.py:65`, `resembl/server.py:71`) |
| R9 | The port-file name is a 12-hex-digit SHA1 of the raw, unmasked database URL | B5, B7 | A truncated hash of a credentialed URL is written to disk under the cache dir | none (`resembl/paths.py:118`) |
| R10 | `find-batch --file` reads the whole query file into a list before chunking it into requests | B2 | Memory exhaustion from an oversized input file | Chunking on the wire only (`resembl/cli.py:1571`, `resembl/cli.py:526`) |
| R11 | A release tag is not statically analyzed or dependency-reviewed when it is cut; only the SBOM runs on the tag | B8 | A tagged release ships whatever the tag's tree contains, with no pre-release SAST or CVE gate on the tag itself | SAST on push and pull request to `main` plus a weekly cron (`.github/workflows/codeql.yml:4`), dependency review on pull requests to `main` only (`.github/workflows/dependency-review.yml:11`), SBOM on `main` and on `v*` (`.github/workflows/sbom.yml:9`) |
| R12 | The vulnerability reporting channel the documentation names is not enabled on the repository | Outside the process | A reporter following `SECURITY.md` finds no private channel and is pushed toward a public issue | none; the one working route is a public issue, which the same document forbids for an unfixed vulnerability |

## Attack surface

Every entry point below is present in the code at the cited location.

### Network listeners

| Entry point | Location | Notes |
| ----------- | -------- | ----- |
| `POST /find` | `resembl/server.py:727` | Unauthenticated, read-only. |
| `POST /find-batch` | `resembl/server.py:727` | Unauthenticated, up to 1000 queries per request (`resembl/server.py:550`). |
| `resembl serve` bind | `resembl/cli.py:578` | `--host` is free-form; anything but loopback only prints a warning (`resembl/cli.py:603`). `--port` accepts `0`, which lets the OS assign a port (`resembl/cli.py:580`). |
| Any other verb | `resembl/server.py:771` | Not implemented by the handler, so `BaseHTTPRequestHandler` answers `501` with its own HTML body. That answer is the only one this server emits that bypasses `_respond`, and so the only one carrying no `X-Content-Type-Options`, `X-Frame-Options`, `Content-Security-Policy`, or `Cache-Control` header (`resembl/server.py:894`). |

`GET`, `PUT`, `DELETE`, `PATCH`, `HEAD` and `OPTIONS` are implemented and
answer `405` with the same JSON error envelope (`resembl/server.py:752`); a
`HEAD` carries those headers without a body. A `POST` to any other path
answers `404` (`resembl/server.py:728`).

### CLI surface

`resembl` (`resembl/cli.py:113`) exposes 18 commands plus four
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
| `RESEMBL_CONFIG_DIR` | `resembl/paths.py:97` | Trusted verbatim; selects the config file. |
| `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` | `resembl/paths.py:100`, `resembl/paths.py:84` | Trusted when the override above is unset. |
| `RESEMBL_SEED` | `resembl/core.py:1328`, `resembl/core.py:1368` | Trusted verbatim; seeds the one generator every MinHash sample in a run draws from, so it changes the fingerprints that run produces. |
| `RESEMBL_NOW` | `resembl/models.py:37`, `resembl/models.py:48` | Trusted verbatim; replaces the created-at clock for every row. Refused unless it parses as ISO 8601. |
| `~/.config/resembl/config.toml` | `resembl/paths.py:106` | Parsed as TOML (`resembl/config.py:270`), then every value coerced to its field's type and range-checked (`resembl/config.py:157`, `resembl/config.py:74`). |

The config file holds six fields (`resembl/config.py:114`):
`lsh_threshold`, `num_permutations`, `top_n`, `ngram_size`, `jaccard_weight`,
and `format`. The first five change what the tool reports; `format` changes
rendering only.

Every path in this table is resolved in `resembl/paths.py`, so the CLI, the
server, and the standalone client cannot disagree about where they read from
(`docs/adr/006-paths-module-owns-environment.md`).

### Files the tool writes

The port file (`resembl/server.py:1012`), the config file
(`resembl/config.py:222`), exported YARA rules (`resembl/core.py:1041`), and
the database itself. Each is published with write-temp-then-rename
(`resembl/config.py:245`, `resembl/server.py:1145`, `resembl/core.py:1043`).

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
without changing what the operator can see (`resembl/core.py:1368`,
`resembl/models.py:48`).

**B4, application to database.** The ORM parameterizes every query, so
untrusted text is not concatenated into SQL. Three places build a `LIKE`
pattern from caller input: the checksum-prefix resolver escapes the wildcard
characters (`resembl/cli.py:726`), the model's JSON-name containment lookup
escapes them (`resembl/models.py:180`), while the name search does not
(`resembl/core.py:1612`). The result set is capped by `limit`, so the
unescaped wildcards widen the match set rather than the returned page.

**B5, cache directory to process.** The port file is read as an integer and
used to open an HTTP connection (`resembl/find_client.py:121`,
`resembl/cli.py:429`). Its contents are authenticated by nothing. The file
names a port only: both clients dial `127.0.0.1`
(`resembl/find_client.py:152`, `resembl/cli.py:436`), so a forged port
reaches a listener the forger must already be running on the loopback
interface.

**B6, merge source to local corpus.** Rows from another database are
inserted after structural checks, not after a trust decision about the source
(`resembl/core.py:2036`).

**B7, secrets to code.** The database URL may embed a password. It is masked
for display (`resembl/paths.py:63`, used at `resembl/find_client.py:124`)
but used unmasked to derive the port-file name (`resembl/paths.py:118`) and
to open the connection. The in-process result cache keys on the rendered
engine URL (`resembl/server.py:433`), which SQLAlchemy renders with the
password replaced by `***`, so credentials do not persist in the cache keys.

**B8, build to release.** The published wheel carries the runtime surface
above plus whatever the release pipeline admits. An SBOM workflow exports a
CycloneDX 1.5 inventory of the runtime graph on a push to `main` and on a
`v*` tag (`.github/workflows/sbom.yml:9`). CodeQL runs the Python SAST suite
on a push to `main`, a pull request to `main`, and a weekly cron
(`.github/workflows/codeql.yml:4`), and dependency review runs on pull
requests to `main` only, failing on moderate severity
(`.github/workflows/dependency-review.yml:11`). None of the two has a
`tags:` trigger, so a release is not statically analyzed and not
dependency-reviewed at the moment it is cut; R11 records that as a gap rather
than a control. The Pygments floor sits above the ReDoS advisory
(`pyproject.toml:34`), and a tag build records a provenance attestation over
the sdist and the wheel (`.github/workflows/build.yml:93`, the `attest` job,
tag-only by its `if:` at `.github/workflows/build.yml:96`), so a consumer can
check an artifact against this repository and the commit that built it. No
packaging step pulls code at install time; nothing in `resembl/` executes a
network fetch at import.

## Assets

| Asset | Why it matters | Held in |
| ----- | -------------- | ------- |
| Snippet corpus | Proprietary reverse-engineering data; the reason the tool exists | `snippet` table, served over B1 |
| Database credentials | Reach to a shared Postgres/MySQL corpus | `RESEMBL_DATABASE_URL` / `DATABASE_URL`, process environment |
| LSH index | Minutes of CPU to rebuild; its loss is an availability hit, not a data loss | `lsh_bucket` table |
| Warm server availability | The performance reason to run `serve` at all | `resembl/server.py:997` |
| Port file | Discovery of the running server, and a redirect target | cache dir |
| Exported YARA rules | Downstream detection artifacts | caller-chosen output file |
| Config file | Thresholds and match settings, applied with no on-screen report that they are in force | `config.toml` |
| Release artifacts | The only thing a consumer installs; a substituted one runs with the operator's database credentials | PyPI, attested per B8 |

## Threats per boundary

### B1, loopback socket

*Information disclosure.* Any process on the host can enumerate the corpus,
one request at a time. Every find parameter is range-checked, `top_n`
included: a request outside `1..1000` rows is refused with a `400`
(`resembl/server.py:354`, `_MAX_TOP_N` at `resembl/server.py:560`, the lower
bound at `resembl/server.py:363`). That bounds one response, not the corpus:
repeated requests with a permissive `threshold` and `top_n = 1000` still page
the whole table out of an unauthenticated endpoint, and the config layer
deliberately leaves the local `top_n` unbounded (`resembl/config.py:63`)
because only the request path needs a bound. A config above that bound is
clamped to it as the served default (`resembl/server.py:560`), so it bounds
the rows a request omitting `top_n` receives too.

A request may also name a `threshold`, `ngram_size`, or `num_permutations`
that differs from the one the server's index was built for. That is refused
with a `400` (`resembl/server.py:392`), because honouring it would rebuild
the shared `lsh_bucket` table from inside a request thread while other
threads read it.

A browser on the same host is a second peer, and the binding does not stop
it. The server sets no CORS headers, which blocks a cross-origin `fetch`
from *reading* the answer, and on a loopback bind it also checks the `Host`
header and answers `403` to anything but the names it was bound to
(`resembl/server.py:707`, `resembl/server.py:724`), so a page that rebinds
its own hostname onto `127.0.0.1` and sends a same-origin request with no
preflight is refused. The `Host` a rebound page carries is still its own
name, which is what the check reads. The auto-assigned port
(`resembl/cli.py:580`) remains enumerable by a page that probes the port
range, and a non-loopback bind is unrestricted, since it is reached by
whatever name resolves to it. A verb the handler does not implement is
answered `501` with a header-less HTML body by the stdlib base class
(`resembl/server.py:771`), the one response that skips the hardening header
block at `resembl/server.py:894` and the only one a browser can be pointed
at and have markup returned.

*Denial of service.* `ThreadingHTTPServer` spawns a thread per connection
with no cap; each thread holds a 30-second idle timeout
(`resembl/server.py:653`). The engine pool is 32 plus 64 overflow
(`resembl/server.py:1032`). A client that opens connections faster than
requests complete exhausts threads, pool connections, and file descriptors
before any of the three limits is a policy decision. There is no request
accounting of any kind.

*Repudiation.* `log_message` writes at DEBUG (`resembl/server.py:924`,
`resembl/server.py:926`), which is quiet unless the server was started with
`serve -v`, so a default deployment records that a query was made, from
where, or what was asked for nothing. Failures are logged with
`logger.exception` (`resembl/server.py:811`), which is the only per-request
trace that exists at any level.

*Elevation of privilege.* None available: the server executes no request-
derived SQL text and writes nothing.

*Host header.* Checked on a loopback bind, as above; a non-loopback bind
accepts any name it is reached by, which is the right rule for an address
that is not private to the host.

### B2, files and arguments

*Tampering.* `merge` accepts any source. Its rows, including names, tags,
and collections, are written into the local corpus.

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

*Tampering.* `RESEMBL_SEED` (`resembl/core.py:1368`) changes the generator
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
cache directory is made with mode `0o700` (`resembl/server.py:1133`) and the
port file is created `0o600` rather than with the process umask
(`resembl/server.py:1142`). That covers the server's own creation path only.
`os.makedirs(..., exist_ok=True)` does not repair the mode of a directory that
already exists, and no code path reads the mode back, so a cache directory
created earlier by another tool, another user, or an operator's own `mkdir`
keeps whatever mode it had and both clients then trust a file inside it.

*Tampering.* `port_file_cleanup` only removes the file while it still names
the port the caller failed on (`resembl/server.py:603`), so one process
cannot retire another server's advertisement by racing the exit path. That
control does not extend to a third party rewriting the file's contents.

### B6, merge source

*Tampering.* Rows of unknown provenance enter the corpus and are indexed.
The fingerprint path is the sharpest edge and is closed: a blob that is not
in the `RMLH` format raises `ValueError` (`resembl/scoring.py:1315`), and
merge recomputes from the source row's own code rather than deserializing
(`resembl/core.py:2149`).

*Information disclosure.* A `postgresql+pg8000://user:pass@host/db` source
sends those credentials to the named host (`resembl/core.py:2060`). The URL
is masked for display (`resembl/paths.py:63`) but that is a display control,
not a network one.

*Information disclosure, the failure path.* When the source will not open,
`db_merge` returns the raw exception text to the caller
(`resembl/core.py:2064`) rather than a fixed message. The CLI prints it, so
whatever the driver puts in the exception, including the host and port it
dialled, reaches the terminal and any log that captures it. This is the only
error path in the codebase that returns unsanitized text to a caller; the
HTTP handlers replaced theirs with a fixed body (`resembl/server.py:728`,
`resembl/server.py:752`).

### B8, build to release

*Repudiation of provenance.* The attestation recorded by the `attest` job
(`.github/workflows/build.yml:93`) covers only the artifacts that job built
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
| Loopback `Host` check, 403 | `resembl/server.py:707`, `resembl/server.py:724` | A page that rebinds its own hostname onto the loopback port reading a served query |
| Request body cap, 8 MiB | `resembl/server.py:689` | B1 memory exhaustion |
| JSON depth and shape guard | `resembl/server.py:692` | `RecursionError` from a nested body killing the handler thread with no response |
| Batch query cap, 1000 | `resembl/server.py:550`, `resembl/server.py:831` | B1 work per request |
| Find parameter range checks | `resembl/server.py:317` | NaN, out-of-range, and degenerate fingerprints |
| Query field check, string and non-blank | `resembl/server.py:624` | A non-string query crashing the lexer into a `500` with its message, and a blank one answering `200` with no matches |
| `top_n` bounds, 1 to 1000 | `resembl/server.py:354`, `resembl/server.py:363`, `resembl/server.py:560` | One response returning the whole corpus (upper bound), or a `200` with an empty `matches` read as "no duplicates" (lower bound). A sequence of requests is not bounded |
| Index-parameter match check | `resembl/server.py:394` | A request rebuilding the shared LSH index mid-serve |
| Per-count MinHash template cap, 8 | `resembl/scoring.py:72` | An unbounded dict grown by cycling permutation counts |
| Result-cache key is a query digest | `resembl/server.py:71` | Query text retained by the result cache, and an entry count standing in for a memory bound |
| Per-key compute lock | `resembl/server.py:117` | A burst of identical queries multiplying one find across connections |
| Content-Type check, 415 | `resembl/server.py:705` | B1 request shape |
| Generic 500 body, details to log | `resembl/server.py:815` | B1 error-text disclosure of SQL, bind parameters, and paths |
| Response hardening headers | `resembl/server.py:900` | Browser reinterpretation of a snippet-derived response; caching of that response. Not applied to the stdlib `501` path (`resembl/server.py:771`) |
| Thread timeout, 30s | `resembl/server.py:653` | Connections parked open by an idle client |
| Engine pool, 32 + 64 | `resembl/server.py:1032` | Pool exhaustion; the overflow is what a client that outruns requests consumes first |
| Disconnect failures swallowed | `resembl/server.py:983` | Traceback spam from connection churn, not a security control |
| Double-serve check | `resembl/server.py:1012` | Two servers advertising one database |
| Atomic file publication | `resembl/server.py:1145`, `resembl/config.py:245`, `resembl/core.py:1043` | Partial-file reads by a concurrent client |
| Cache directory created `0o700` | `resembl/server.py:1133` | Another local user reading or writing the port file, for a directory the server itself creates |
| Port file created `0o600` | `resembl/server.py:1142` | The same, for the file inside it. Neither checks a directory that already existed |
| Fingerprint magic check | `resembl/scoring.py:1315` | Deserialization of attacker-controlled bytes from a hostile database or merge source |
| Legacy cache files never deserialized | `resembl/cache.py:8`, `resembl/cache.py:316` | Code execution from a planted cache file |
| YARA string escaping | `resembl/core.py:989` | A snippet name breaking out of a generated rule string |
| Config value coercion and range check | `resembl/config.py:157`, `resembl/config.py:74` | A hand-edited config putting a raw TOML type or an out-of-range value into a numeric field |
| `LIKE` metacharacter escaping | `resembl/cli.py:726`, `resembl/models.py:180` | A checksum prefix like `%` resolving to an arbitrary snippet |
| Password masking | `resembl/paths.py:63` | Credentials in a printed URL |
| Config file lock | `resembl/config.py:185` | Lost updates between concurrent CLI processes |
| Dependency CVE floor | `pyproject.toml:34` | The Pygments ReDoS advisory |
| CI static analysis and dependency review | `.github/workflows/codeql.yml:4`, `.github/workflows/dependency-review.yml:11` | Static analysis and dependency review of `main` and of pull requests. Neither runs on a release tag (R11) |
| Release SBOM | `.github/workflows/sbom.yml:9` | An inventory of the runtime graph attached to the build, on `main` and on `v*` tags |
| Release provenance | `.github/workflows/build.yml:93` (the `attest` job) | An artifact published under this project's name that this repository did not build |

### Threats with no mitigation

Ranked as in the summary. R3 (unauthenticated port file) and R10 (unbounded
`--file` read) have no control at all. R1's `top_n` cap bounds one response
but not a sequence of them, R2's is a `Host` check that a non-loopback bind
does not get, and R4's is a log level the operator has to ask for. R5 and R6
are accepted by design and documented in `SECURITY.md` rather than enforced.
R3's server-side `0o700` closes the case where `serve` created the cache
directory itself and leaves open the case where something else created it
first, which is the case a shared cache directory actually is. R6's
recomputed fingerprints are a control; its error path is not, and returns
raw driver text to the caller.

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

The result cache (`resembl/server.py:65`) is the only thing standing between
a repeated expensive query and repeated full work. Its SQLite-only version
guard means a non-SQLite deployment gets no caching at all and no
cross-process invalidation. A key that is not cached yet is computed once
however many requests are waiting for it: the per-key compute lock
(`resembl/server.py:117`) collapses a burst of identical queries onto one
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
per-caller one (`resembl/server.py:354`).

**Query text capture.** The same process, or any process that can write the
cache directory, replaces the port file's contents with a port it is
listening on. Every subsequent `resembl find` ships its query, which is
attacker-supplied disassembly, to that loopback listener. Enabled by
`resembl/find_client.py:118`, `resembl/cli.py:429`, and the hard-coded
loopback dial at `resembl/find_client.py:152`.

**Result amplification.** `threshold` down, `top_n` at its 1000-row cap,
repeated. Each response is a fresh 1000-row page, served from a
`ThreadingHTTPServer` with no request accounting.

**Browser pointed at the port.** A page on the host issues a verb the handler
does not implement. The stdlib base class answers `501` with an HTML body and
none of the `X-Content-Type-Options`, `X-Frame-Options`, or
`Content-Security-Policy` headers the server sets on every response it builds
itself (`resembl/server.py:771`, `resembl/server.py:894`). This is a
markup-returning surface on a port a page is known to be able to reach, and
it is the one response shape the `Host` check also does not reach, since the
check lives in `do_POST` and the shared method handlers
(`resembl/server.py:723`).

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
`0o700` at `resembl/server.py:1133` never applies and the second user's forged
port file is read as if the first user had written it.

**Silent configuration redirection.** Setting `RESEMBL_CONFIG_DIR` for a
`resembl` invocation changes `lsh_threshold`, `ngram_size`, and
`num_permutations`. The tool does not report that it is running under a
non-default configuration, so match results change with no visible signal.
`RESEMBL_SEED` reaches the same place without touching a directory at all.

**Poisoned merge.** A source database with plausible rows and unusable
fingerprints has them recomputed from its own code
(`resembl/core.py:2149`). The corpus now contains code the operator never
reviewed, ranked by similarity alongside genuine findings.

**Merge error as an information leak.** Pointing `merge` at a URL whose host
answers with a slow or hostile handshake makes the driver raise; the raw
text of that error is returned and printed (`resembl/core.py:2064`).

A scenario that a previous revision of this model named, **warm-server
growth by cycling `num_permutations`**, no longer holds: such a request is
refused with a `400` (`resembl/server.py:392`), and the per-count MinHash
template cache is capped at eight entries (`resembl/scoring.py:72`) even for
callers that reach it in-process.

## Response readiness

`resembl serve` records every request at DEBUG (`resembl/server.py:924`,
`resembl/server.py:926`), which is below the default level, so a server left
running as shipped has no trail to investigate from after a suspected
exfiltration. Errors are logged with stack context at any level
(`resembl/server.py:811`); successful queries are logged only under
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
- `docs/http_api.md` for the endpoint contract, including the `501` path
  that the handler does not implement.
- `docs/adr/003-checksum-as-pk.md`, `docs/adr/004-database-backed-lsh-index.md`,
  `docs/adr/005-vendored-minhash.md` for the decisions that removed pickle
  deserialization from the data path.
- `docs/adr/006-paths-module-owns-environment.md` for the decision behind
  B3, B5, and B7, the environment surface that R5, R10, and the port-file
  risks all sit on.
- `docs/adr/002-sqlite-primary-store.md` for the default backend, which
  decides where the corpus and the result cache live.
