# resembl(1) — Assembly Code Similarity Search

## SYNOPSIS

**resembl** [OPTIONS] COMMAND [ARGS...]

## DESCRIPTION

**resembl** is a command-line tool for finding similar assembly code snippets
within a database.  It uses MinHash and Locality-Sensitive Hashing (LSH) for
fast candidate filtering, weighted shingling to prioritize rare instruction
patterns, and hybrid scoring (Jaccard + Levenshtein) for accurate ranking.
The `compare` command also reports control-flow graph similarity.

## GLOBAL OPTIONS

**--quiet, -q**
:   Suppress informational output.

**--verbose, -v**
:   Increase output verbosity.

**--no-color**
:   Disable colored output.

**--format** *table|json|csv*
:   Output format (overrides config).  Any other value is rejected (exit 2);
    an unrecognized format cannot be rendered.

**--version**
:   Print the resembl version and exit.  Read before the database is
    opened, so it works with an unreachable `DATABASE_URL`.

## EXIT STATUS

**0**
:   Success.

**1**
:   The command failed: a missing snippet or collection, an unreadable
    file, an unopenable database, or health issues reported by `verify`.

**2**
:   The command line was rejected: an unknown command or flag, a bad flag
    value (`--format`, `--range`, `--threshold`, a `config` key or value),
    a missing query, or a bare `resembl` with no subcommand.

## COMMANDS

### Snippet Management

**add** *NAME* *CODE*
:   Add a new snippet to the database.

**rm** *CHECKSUM* [--force]
:   Delete a snippet by its checksum (or a unique prefix).

**show** *CHECKSUM*
:   Show details of a snippet.  Accepts a unique checksum prefix.

**list** [--range START-END]
:   List all stored snippets.  An unbounded listing streams in bounded
    memory (only checksum and names are read, in batches), so it is safe
    on databases of any size; use `--range` to page a specific window.

**search** *PATTERN* [--limit N]
:   Search for snippets by matching their names.  A broad pattern over a
    large database could otherwise return hundreds of thousands of rows,
    so results are bounded (default 50; ``N``+ is printed when the limit
    truncates the output).

**find** [--query *QUERY*] [--file *FILE*] [--top-n *N*] [--threshold *T*]
[--no-normalization]
:   Find snippets similar to the given query string (`--query`, `--file`,
    or stdin; `-` reads stdin, and so does a redirected stdin when
    neither `--query` nor `--file` is given).
    Single-line `--query` strings use `;` as a statement separator
    (e.g., `find --query "push eax; pop ebx"`); multi-line input and
    `--file` keep normal NASM semantics where `;` starts a comment.
    If a `serve` process is running for this database, the query is
    answered by it (a few milliseconds); otherwise the in-process path
    runs, building the LSH index lazily on the first search.

**find-batch** *--file QUERIES* [--top-n N] [--threshold T]
:   Find matches for many queries in one process — each line of *file* is
    one query (`#` starts a comment), and the interpreter startup and LSH
    index load are amortized across the whole batch.  Roughly N times
    faster than N separate `find` calls.  JSON/CSV output is a list of
    per-query payloads.

**compare** *CHECKSUM1* *CHECKSUM2*
:   Compare two snippets side-by-side (similarity metrics and a code
    diff).  Accepts checksum prefixes.

### Bulk Operations

**import** *PATH* [--jobs N] [--force]
:   Import `.asm` / `.txt` files from a directory (subdirectories are
    included automatically).  The default worker count is adaptive — one
    worker per ~100 files, capped at the CPU count — so small directories
    stay single-process (spawning each worker costs ~450 ms of interpreter
    startup) while large ones parallelize fully.

**export** *DIRECTORY* [--force]
:   Export all snippets to a directory as `.asm` files (one per snippet,
    named after the snippet's primary name).

**export-yara** *OUTPUT_FILE* [--force]
:   Export all snippets to *OUTPUT_FILE* as YARA string-matching rules.

### Database

**serve** [--host HOST] [--port PORT]
:   Start a warm server for this database (default: loopback, auto-assigned
    port).  `find` then talks to it over localhost in a few milliseconds
    instead of paying ~450 ms of interpreter startup per query; the port file
    is written to the cache directory and removed on exit.  Stop with
    Ctrl+C.  For repeated queries, the thin client
    `resembl-find --query "…"` (or `python -m resembl.find_client`) is the
    fastest path.  Requests run concurrently (each gets its own session);
    the fingerprint migration and index build happen once at startup, and a
    restart skips rebuilding an index that is already current.  Starting a
    second server for the same database is refused (as is an occupied
    `--port`), with a clean error rather than a traceback.
    `resembl serve -v` logs the configuration the server started with (the
    served database with its password masked, the port file, and the find
    defaults every request inherits).
    The HTTP endpoints themselves (`POST /find`, `POST /find-batch`, JSON
    requests, `{"error": ...}` bodies) are documented in
    [http_api.md](http_api.md).

**reindex** [--jobs N] [--force]
:   Recalculate MinHash fingerprints for all snippets.
    Accepts `--jobs N` to run the CPU-bound recomputation in parallel
    (default: one worker per CPU core).
    After a fingerprint-format change, the first `find` reindexes
    automatically once (a format version is stamped in the database);
    `reindex --force` is only needed to force it early.

**stats**
:   Show database statistics.

**verify**
:   Check database health: snippet/bucket counts, fingerprint format
    version, and any pending work (a missing index or stale fingerprints
    are healed by the next `find`; a bucket/snippet mismatch means
    `reindex --force` should run).  Exits 1 when issues are found.

**clean**
:   Drop the LSH index (bucket rows and metadata), then vacuum the
    database.  Legacy pickle cache files are removed by the next index
    write, not by `clean`.

**merge** *PATH*
:   Merge snippets from another resembl database into the current one,
    deduplicating by checksum.  *PATH* is a database file or a full
    `DATABASE_URL` (any backend with its driver installed, e.g.
    `duckdb:///file.db`).

### Naming & Tags

**name add** *CHECKSUM* *NAME*
:   Add an alias to a snippet.  Accepts checksum prefixes.

**name remove** *CHECKSUM* *NAME*
:   Remove an alias from a snippet.  Accepts checksum prefixes.

**tag add** *CHECKSUM* *TAG*
:   Add a tag to a snippet.  Accepts checksum prefixes.

**tag remove** *CHECKSUM* *TAG*
:   Remove a tag from a snippet.  Accepts checksum prefixes.

### Collections

**collection create** *NAME* [--description TEXT]
:   Create a new snippet collection.

**collection delete** *NAME*
:   Delete a collection (snippets are kept).

**collection list**
:   List all collections.

**collection show** *NAME*
:   Show snippets in a collection.

**collection add** *COLLECTION* *CHECKSUM*
:   Add a snippet to a collection.  Accepts checksum prefixes.

**collection remove** *CHECKSUM*
:   Remove a snippet from its collection.  Accepts checksum prefixes.

### Version History

**version** *CHECKSUM*
:   Show the version history for a snippet.  Accepts checksum prefixes.
    (The tool's own version comes from the global `--version` option.)

### Configuration

**config list**
:   Show current configuration.

**config get** *KEY*
:   Get a configuration value.

**config set** *KEY* *VALUE*
:   Set a configuration value.

**config unset** *KEY*
:   Reset a key to its default.

**config path**
:   Print the config file path.

## ENVIRONMENT

**RESEMBL_CONFIG_DIR**
:   Override the default config directory (`~/.config/resembl`).

**XDG_CONFIG_HOME / XDG_CACHE_HOME**
:   Honored when the `RESEMBL_*` overrides above are unset: the config
    directory becomes `$XDG_CONFIG_HOME/resembl`, the cache directory
    `$XDG_CACHE_HOME/resembl`.

**RESEMBL_CACHE_DIR**
:   Override the default cache directory (`~/.cache/resembl`).
    The LSH index itself lives in the database; this directory holds only
    small auxiliary files: legacy pickle cache files written by older
    versions (removed on write) and the `serve` port file used by `find`
    and `resembl-find` to locate a running server.

**RESEMBL_DATABASE_URL**
:   SQLAlchemy database URL. Defaults to `sqlite:///assembly.db`.
    Set to a PostgreSQL URL (e.g., `postgresql+pg8000://user:pass@host/db`)
    for team use.  The `pg8000` and `pymysql` drivers are installed with the
    package; a `duckdb:///` URL needs the extra: `uv pip install
    "resembl[duckdb]"`.

**DATABASE_URL**
:   Same as `RESEMBL_DATABASE_URL`, read only when that one is unset, so a
    `DATABASE_URL` already exported by another tool still works. Prefer the
    namespaced name: `DATABASE_URL` is a common name and a stray one
    redirects resembl at a different database. An empty value of either
    variable counts as unset.

**RESEMBL_SEED**
:   Integer seed for the values a command samples rather than reads
    (`stats` vocabulary and average-similarity estimates). Unset, the seed
    is drawn from the OS. Set it, and the same database reports the same
    estimates on every run, so a number can be reproduced after the fact. An
    unparseable value aborts the command.

**RESEMBL_NOW**
:   ISO 8601 instant to stamp `created_at` with, instead of the wall clock.
    Every row written during the run and every timestamp printed back by
    `collection list` and `version list` takes this value, so a run replayed
    with the same inputs produces the same database and the same output. An
    unparseable value aborts the command.

## CONFIGURATION

Settings are stored in `~/.config/resembl/config.toml`:

| Key              | Type  | Default | Range             | Description                     |
|------------------|-------|---------|-------------------|---------------------------------|
| lsh_threshold    | float | 0.5     | 0.0 <= v < 0.99   | Minimum LSH Jaccard similarity  |
| num_permutations | int   | 128     | 2 <= v <= 4096    | MinHash permutation count       |
| top_n            | int   | 5       | v >= 1            | Default number of results       |
| ngram_size       | int   | 3       | v >= 1            | Token n-gram size for shingling |
| jaccard_weight   | float | 0.4     | 0.0 <= v <= 1.0   | Weight of Jaccard in hybrid score |
| format           | str   | table   | table/json/csv    | Default output format           |

`config set` refuses a value outside these ranges. A hand-edited
`config.toml` holding one is reported on stderr and ignored, and the default
is used instead, so a bad value cannot silently produce a degenerate index
or an empty result set.

## EXAMPLES

```bash
# Add a snippet (';' starts a comment in add, like any NASM source:
# put each statement on its own line for multi-statement code)
resembl add "memcpy" "mov ecx, [esp+8]"

# Find similar snippets
resembl find --query "mov ecx, [esp+8]" --top-n 10

# Or pipe the query in
cat routine.asm | resembl find --format json

# Import a directory of .asm files
resembl import ./samples --jobs 4

# Export all snippets as YARA string rules
resembl export-yara rules.yar --force

# Create and use a collection
resembl collection create "crypto" -d "Cryptographic routines"
resembl collection add crypto abc123

# Use with PostgreSQL
DATABASE_URL=postgresql+pg8000://user:pass@host/db resembl list
```

## SEE ALSO

Project repository: <https://github.com/maci0/resembl>
