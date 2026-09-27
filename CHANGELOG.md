# Changelog

All notable user-visible changes to resembl are recorded here.  The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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

- `make dist` clears `build/` and `resembl.egg-info/` before building.
  setuptools stages the wheel in `build/lib`, so a module deleted from the
  source tree survived in the artifact until someone cleaned the directory by
  hand; the sdist is also unpacked under `.scratch/` rather than `mktemp -d`'s
  tmpfs.
- Every `uv run` in the `Makefile` and in the `pre-commit` hooks passes
  `--locked`, so no check re-resolves `uv.lock` and rewrites it under the
  contributor when `pyproject.toml` has drifted.  Plain `uv run` gated the
  code against a lockfile no CI run and no other contributor ever saw;
  `uv lock` (or `make install`) is now the way forward.
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
- `resembl import` on a directory that holds no `.asm` / `.txt` file now
  exits `1` and says so, instead of reporting a successful import of zero
  snippets.  A script that imported a directory that had been emptied now
  sees a failure; point it at a directory that holds snippets.
- `resembl list` on an empty database, a `--range` past the last snippet,
  and a `resembl search` that matches no name each say what happened and
  what to run next, where the listing used to print nothing or a bare
  count.
- `resembl collection create` on a name that already exists now reports the
  collection as already there and exits 0, instead of failing with the
  database's `IntegrityError` and its SQL text. The stored description is
  left alone, so re-running a create never rewrites a collection.
- `resembl name add` on a name the snippet already carries is now a no-op
  that exits 0, like `resembl tag add` already was. A script that re-ran
  the command after a failure used to see exit 1 for an end state it had
  already produced.
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
- A rejected command line exits `2`, not `1`, matching what typer already
  returned for its own parse errors: a bad `--threshold` or `--format` value, a
  missing query, or a bare `resembl`
  with no subcommand.  A script that treated `1` as "you typed it wrong"
  has to read `2` now; `1` still means the command failed.
- An unsupported `--format` value, and an unsupported `format` in the config
  file, are refused instead of falling through to an unknown renderer.
- Log records go to stderr rather than stdout, so a warning raised mid-run
  (a config key ignored, a parallel import falling back) no longer
  interleaves into the JSON or CSV a script is parsing.
- A `--format csv` result set with no rows now writes nothing, instead of a
  JSON `[]` in the CSV stream.
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
- `resembl clean` reports the LSH index it dropped instead of a cache it never
  touched.  `clean` drops the index rows and vacuums; the legacy pickle cache
  is removed by the next index write, not by `clean`.
- `resembl export` and `resembl export-yara` write LF on every platform.
  Text mode rewrote each `\n` to `os.linesep`, so the same database exported
  CRLF on Windows and LF elsewhere and the two trees diffed against each
  other; the generated rule file and the `.asm` files are now byte-identical
  wherever they are produced.
- `make dist-verify` works on macOS, which ships `shasum -a 256` instead of
  coreutils' `sha256sum`.  `make hygiene` no longer needs ripgrep, which is
  not a system tool there.
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

## [2.0.0] - 2026-09-15

### Changed

- **Breaking:** Python 3.11 and 3.12 are no longer supported; the minimum
  is now Python 3.13, matching what the locked dependencies (notably
  `numpy` 2.5) already require.
- Dependencies refreshed to their latest releases: `pylint` 4, `datasketch`
  2, `mypy` 2, plus the rest of the locked tree.
- Removed three names from the `resembl.core` re-export block:
  `code_tokenize_lexed` and `string_normalize_lexed` (both dead) and
  `minhash_jaccard`, which is still public and is importable from
  `resembl.models` and documented in `docs/api_reference.md`.

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

[Unreleased]: https://github.com/maci0/resembl/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/maci0/resembl/compare/v1.2.0...v2.0.0
[1.2.0]: https://github.com/maci0/resembl/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/maci0/resembl/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/maci0/resembl/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/maci0/resembl/releases/tag/v0.1.0
