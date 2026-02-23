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
:   Output format (overrides config).

## COMMANDS

### Snippet Management

**add** *NAME* *CODE*
:   Add a new snippet to the database.

**delete** *CHECKSUM*
:   Delete a snippet by its checksum.

**get** *CHECKSUM*
:   Show details of a snippet.  Accepts a unique checksum prefix.

**list**
:   List all stored snippets.

**search** *PATTERN*
:   Search for snippets by matching their names.

**find** *QUERY* [--top-n N] [--threshold T] [--no-normalization]
:   Find snippets similar to the given query string.

**compare** *CHECKSUM1* *CHECKSUM2* [--diff]
:   Compare two snippets side-by-side.  Accepts checksum prefixes.

### Bulk Operations

**import** *PATH* [--recursive] [--jobs N]
:   Import `.asm` files from a directory.

**export** *DIRECTORY* [--format json|asm]
:   Export all snippets to a directory.

**yara** *CHECKSUM* [--rule-name NAME]
:   Generate a YARA rule from a snippet.

### Database

**reindex**
:   Recalculate MinHash fingerprints for all snippets.

**stats**
:   Show database statistics.

**clean**
:   Vacuum the database and clear caches.

**merge** *PATH*
:   Merge snippets from another resembl database file, deduplicating by checksum.

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

**RESEMBL_CACHE_DIR**
:   Override the default cache directory (`~/.cache/resembl`).

**DATABASE_URL**
:   SQLAlchemy database URL. Defaults to `sqlite:///assembly.db`.
    Set to a PostgreSQL URL (e.g., `postgresql://user:pass@host/db`)
    for team use.

## CONFIGURATION

Settings are stored in `~/.config/resembl/config.toml`:

| Key              | Type  | Default | Description                        |
|------------------|-------|---------|------------------------------------|
| lsh_threshold    | float | 0.5     | Minimum LSH Jaccard similarity     |
| num_permutations | int   | 128     | MinHash permutation count          |
| top_n            | int   | 5       | Default number of results          |
| ngram_size       | int   | 3       | Token n-gram size for shingling    |
| jaccard_weight   | float | 0.4     | Weight of Jaccard in hybrid score  |
| format           | str   | table   | Default output format              |

## EXAMPLES

```bash
# Add a snippet
resembl add "memcpy" "mov ecx, [esp+8] ; rep movsb"

# Find similar snippets
resembl find "mov ecx, [esp+8]" --top-n 10

# Import a directory of .asm files
resembl import ./samples --recursive --jobs 4

# Generate a YARA rule
resembl yara abc123 --rule-name suspicious_memcpy

# Create and use a collection
resembl collection create "crypto" -d "Cryptographic routines"
resembl collection add crypto abc123

# Use with PostgreSQL
DATABASE_URL=postgresql://user:pass@host/db resembl list
```

## SEE ALSO

Project repository: <https://github.com/maci0/resembl>
