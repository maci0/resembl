# ADR 006: One Module Owns Every Environment-Derived Path

## Status

Accepted (2026-09-27)

## Context

Four values depend on the environment rather than on a command line:
the database URL, the cache directory, the config directory, and the port file
`resembl serve` writes. Each started in the module that used it:
`resembl.database` read `DATABASE_URL` once at import, `resembl.cache` and
`resembl.config` each held their own `~` default, and `resembl.server` and
`resembl.find_client` each kept a copy of the port-file naming.

Three defects followed from the copies:

- `resembl.database.DATABASE_URL` was read at import, so a process that set
  the variable afterwards queried the first URL it had seen, and a caller had
  no route to the namespaced `RESEMBL_DATABASE_URL` at all.
- The two port-file helpers could disagree about the name, which would leave
  `resembl-find` looking for a file the server never wrote.
- The unprefixed `DATABASE_URL` was the only name honored, so an unrelated
  `DATABASE_URL` exported by a hosting stack picked the backend.

`resembl.find_client` is held to importing only the standard library (and
`resembl.paths`, which is itself standard-library-only, per its module
docstring), so the shared rules had to live in a module that pulls in
neither sqlmodel, nor SQLAlchemy, nor numpy: the client's startup is part
of what the warm `find` path exists to avoid.

## Decision

Move all four into `resembl.paths`, the module that owns
environment-derived paths:

- `db_url_get()` resolves at call time over `DB_URL_ENV_VARS`
  (`RESEMBL_DATABASE_URL` first, the unprefixed `DATABASE_URL` next, an empty
  value counting as unset).
- `cache_dir_get()`, `config_dir_get()`, `config_path_get()` and
  `server_port_path(db_url, cache_dir)` own their defaults and their
  `RESEMBL_*` overrides. `paths.py` also owns `port_file_cleanup()` and
  `console_utf8_reconfigure()`, the shared output-encoding rule both entry
  points apply.

The CLI, the server and the standalone client resolve through `resembl.paths`
instead of keeping copies. The superseded accessors are gone rather than
re-exported; the changelog records the migration for each.

## Consequences

- One place to change when an override rule changes, and the client cannot end
  up with a different answer than the server.
- Reading a value is a call, not an import-time constant, so a test or an
  embedder that changes the environment after `import resembl` gets the value
  it set.
- The public names that moved (`db_url_get`, `db_url_mask`, `cache_dir_get`,
  `config_dir_get`, `config_path_get`, `DEFAULT_DB_URL`, `DEFAULT_CACHE_DIR`,
  `DEFAULT_CONFIG_DIR`, `server_port_path`, `port_file_cleanup`, and since
  the runtime-manifest move `runtime_manifest_path_get` /
  `RUNTIME_MANIFEST_ENV_VAR`) are breaking removals from their old modules;
  this is a major release.
- `resembl.paths` imports only the standard library, so the import-light
  client contract (roughly 50 ms of startup) keeps holding.
- Three environment variables stay with the modules that own their behavior,
  because they are not among the four paths above: `CLOCK_ENV_VAR` in
  `resembl.clock.timestamp_now` (re-exported from `resembl.models` for
  compatibility) fixes the clock a stored stamp is read from,
  `SEED_ENV_VAR` in `resembl.diagnostics` seeds the sampling stream, and
  `RESEMBL_RUNTIME_MANIFEST` names the file `runtime_attestation` checks
  loaded code against for `/find-pinned`; its constant
  (`RUNTIME_MANIFEST_ENV_VAR`) and accessor (`runtime_manifest_path_get()`)
  live in `resembl.paths` so `backup` and `serve` cannot disagree about
  the path. None names one of the four filesystem paths above, so none
  moved.
