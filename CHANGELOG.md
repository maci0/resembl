# Changelog

All notable user-visible changes to resembl are recorded here.  The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [Semantic Versioning](https://semver.org/).

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
