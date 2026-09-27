# Changelog

All notable user-visible changes to resembl are recorded here.  The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `resembl --tz <ZONE>` picks the zone printed timestamps are rendered in
  (`collection list`, `version <checksum>`), e.g. `--tz Europe/Warsaw`.
  Timestamps are stored in UTC and only the display converts, so the printed
  date no longer changes with the host's `TZ`; the default is still the local
  zone.  A fixed offset (`+02:00`) is rejected, since it names one instant of
  the year rather than a zone that follows daylight saving.  `json` and `csv`
  output is unchanged and still carries the stored UTC string.
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
  builds twice and fails if they differ. Building from the sdist needs
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
  client looking for a port file the server never wrote.
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
  returned for its own parse errors: a bad `--threshold`, `--num-perm`,
  `--ngram-size` or `--format` value, a missing query, or a bare `resembl`
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

### Fixed

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
- The served result cache's version guard is read through the pooled
  connection the request uses, so a rebuild under one connection is no longer
  invisible to a request holding another.
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
