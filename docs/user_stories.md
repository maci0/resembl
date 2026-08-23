# User Stories & Personas

This document outlines the features of the `resembl` CLI from a user's perspective.

---

## Personas

### Persona 1: Alex, the Reverse Engineer

*   **Role:** A reverse engineer at a cybersecurity firm, focused on understanding how malware works.
*   **Motivation:** Alex needs to quickly identify known functions and code patterns in a large volume of disassembled code. The goal is to speed up the analysis process and to focus on the novel aspects of a new malware sample.
*   **Key Needs:**
    *   A fast and accurate way to search for similar code snippets.
    *   The ability to build and maintain a personal database of known functions.
    *   The ability to easily share findings with the team.

### Persona 2: Ben, the Security Researcher

*   **Role:** A security researcher at a large tech company, responsible for finding and fixing vulnerabilities in the company's software.
*   **Motivation:** Ben needs to identify code reuse and to track the propagation of vulnerable code across the company's codebase. The goal is to ensure that all instances of a vulnerability are found and patched.
*   **Key Needs:**
    *   A reliable way to compare two snippets of code and to quantify their similarity.
    *   The ability to script and automate the process of comparing large numbers of snippets.
    *   The ability to integrate the tool into a larger vulnerability management workflow.

### Persona 3: Chris, the Database Maintainer

*   **Role:** A senior engineer on a team that uses `resembl` as a shared resource.
*   **Motivation:** Chris is responsible for the health and quality of the team's shared snippet database. The goal is to ensure that the database is well-organized, up-to-date, and free of errors.
*   **Key Needs:**
    *   Tools for bulk-importing and exporting snippets.
    *   The ability to easily manage snippet names and to remove obsolete or incorrect entries.
    *   The ability to monitor the health of the database and to perform maintenance tasks like re-indexing.

---

## User Stories

### Title: Find a similar code snippet

**As a** reverse engineer (Alex),
**I want to** use the `find` command to search for a snippet of assembly code,
**so that I can** quickly identify if it matches a known function in my database.

**Acceptance Criteria:**
- `resembl find --query "..."` returns a list of the most similar snippets.
- The search is robust against changes in register allocation and immediate values.
- The user can specify the number of results with `--top-n`.
- The user can provide the query from a file with `--file`.
- The output can be formatted as JSON with `--format json`.
- Results are ranked by a hybrid score combining Jaccard and Levenshtein similarity.
- A database-backed LSH index speeds up searches (built lazily, kept in sync incrementally).
- The user can disable normalization with `--no-normalization`.

---

### Title: Search for many queries in one run

**As a** security researcher (Ben),
**I want to** use the `find-batch` command to run a whole file of queries in one process,
**so that** I can screen thousands of snippets without paying interpreter startup per query.

**Acceptance Criteria:**
- `resembl find-batch --file <queries.txt>` processes every non-empty line as a query.
- Lines starting with `#` are treated as comments.
- The number of results per query can be limited with `--top-n`, and the LSH threshold overridden with `--threshold`.
- Results can be formatted as JSON or CSV via the global `--format` option.

---

### Title: Serve finds from a warm process

**As a** reverse engineer (Alex),
**I want to** start a `serve` process once so repeated `find` calls skip interpreter startup,
**so that** interactive searches return near-instantly.

**Acceptance Criteria:**
- `resembl serve` binds to loopback (127.0.0.1) by default; the port is auto-assigned when not given.
- `resembl find` transparently talks to the running server and falls back to the in-process path when no server is reachable.
- Binding a non-loopback interface prints a warning that the service is unauthenticated.

---

### Title: Add a new code snippet or alias

**As a** reverse engineer (Alex),
**I want to** use the `add` command to add a new, named assembly function to the central database,
**so that** my team and I can find it later.

**Acceptance Criteria:**
- `resembl add <name> "<code>"` adds a new snippet.
- If the code already exists, the new name is added as an alias to the existing snippet.

---

### Title: Manage snippet names

**As a** database maintainer (Chris),
**I want to** use the `name` command to manage the names associated with a snippet,
**so that I can** keep the database organized and up-to-date.

**Acceptance Criteria:**
- `resembl name add <checksum> <new_name>` adds a new name to an existing snippet.
- `resembl name remove <checksum> <name_to_remove>` removes a name from a snippet.
- The tool prevents removing the last name from a snippet.

---

### Title: Bulk-import a directory of code snippets

**As a** database maintainer (Chris),
**I want to** use the `import` command to import an entire directory of `.asm` files,
**so that I can** quickly build a searchable library from my existing collection.

**Acceptance Criteria:**
- `resembl import <directory>` imports all `.asm` and `.txt` files.
- The filename (without extension) is used as the snippet name.
- The user is prompted for confirmation before the import begins.
- The user can bypass the confirmation prompt with the `--force` flag.

---

### Title: Export all snippets to a directory

**As a** database maintainer (Chris),
**I want to** use the `export` command to save all snippets to a directory,
**so that I can** easily back up or share my database.

**Acceptance Criteria:**
- `resembl export <directory>` exports all snippets to the specified directory.
- Each snippet is saved as a separate `.asm` file.
- The filename is the primary name of the snippet.
- The user is prompted for confirmation before the export begins.
- The user can bypass the confirmation prompt with the `--force` flag.

---

### Title: Export snippets as YARA rules

**As a** security researcher (Ben),
**I want to** use the `export-yara` command to write all snippets as YARA string patterns,
**so that** I can reuse the database in YARA-based scanning workflows.

**Acceptance Criteria:**
- `resembl export-yara <output_file>` writes one YARA rule per snippet to the given file.
- The rule name is derived from the primary name, sanitized to valid rule characters.
- The user is prompted for confirmation before writing begins.
- The user can bypass the confirmation prompt with the `--force` flag.

---

### Title: Browse and inspect stored snippets

**As a** reverse engineer (Alex),
**I want to** use the `list` and `show` commands to browse the database and inspect a single snippet,
**so that** I can see what is stored without exporting anything.

**Acceptance Criteria:**
- `resembl list` lists all snippets as checksum plus names.
- `resembl list --range 10-30` lists a specific window; a malformed range (or start greater than end) exits with an error.
- `resembl show <checksum>` prints the full code of one snippet; checksum prefixes are accepted.
- Both support JSON and CSV output via the global `--format` option.

---

### Title: Compare two code snippets

**As a** security researcher (Ben),
**I want to** use the `compare` command to see a detailed comparison of two snippets,
**so that I can** understand their structural and code-level similarities.

**Acceptance Criteria:**
- `resembl compare <checksum1> <checksum2>` displays a side-by-side comparison.
- The comparison includes Jaccard similarity, Levenshtein score, hybrid score, CFG similarity, and shared token count.
- The output is color-coded for readability.
- The user can disable colored output with the `--no-color` flag.
- The output can be formatted as JSON with `--format json`.
- Checksum prefixes are accepted for convenience.

---

### Title: Search for snippets by name

**As a** reverse engineer (Alex),
**I want to** use the `search` command to find snippets by their name,
**so that I can** quickly locate a snippet I know the name of.

**Acceptance Criteria:**
- `resembl search <pattern>` finds snippets whose names match the pattern.
- The search is case-insensitive.
- The output can be formatted as JSON with `--format json`.

---

### Title: Tag snippets for cross-cutting concerns

**As a** security researcher (Ben),
**I want to** use the `tag` command to add tags to snippets,
**so that I can** categorize and filter snippets by attributes like vulnerability status.

**Acceptance Criteria:**
- `resembl tag add <checksum> <tag>` adds a tag to a snippet.
- `resembl tag remove <checksum> <tag>` removes a tag.
- Checksum prefixes are accepted.

---

### Title: Organize snippets into collections

**As a** database maintainer (Chris),
**I want to** use the `collection` command to group related snippets,
**so that I can** manage them as logical sets.

**Acceptance Criteria:**
- `resembl collection create <name>` creates a new collection.
- `resembl collection add <collection> <checksum>` adds a snippet to a collection.
- `resembl collection remove <checksum>` removes a snippet from its collection.
- `resembl collection show <name>` displays all snippets in a collection.
- `resembl collection list` lists all collections with snippet counts.
- `resembl collection delete <name>` deletes a collection without removing snippets.

---

### Title: Clean the database and cache

**As a** database maintainer (Chris),
**I want to** use the `clean` command to remove dangling cache files and optimize the database,
**so that I can** ensure the tool is running efficiently.

**Acceptance Criteria:**
- `resembl clean` removes the LSH index (buckets and metadata) and any legacy cache files.
- `resembl clean` vacuums the database to reclaim unused space (SQLite only).
- The LSH index is rebuilt automatically on the next `find`.

---

### Title: Remove a snippet

**As a** database maintainer (Chris),
**I want to** use the `rm` (or `del`) command to remove an obsolete or incorrect snippet,
**so that** the search results remain clean and accurate.

**Acceptance Criteria:**
- `resembl rm <checksum>` removes a snippet from the database.
- Checksum prefixes are accepted.
- The tool prompts for confirmation before deleting.
- The user can bypass the confirmation prompt with the `--force` flag.

---

### Title: Merge a teammate's database

**As a** database maintainer (Chris),
**I want to** use the `merge` command to pull snippets from another resembl database,
**so that** team databases can be combined without losing anyone's entries.

**Acceptance Criteria:**
- `resembl merge <source>` accepts a path to another database file, or a full database URL for non-SQLite sources.
- Snippets already present (same checksum) are skipped, not duplicated.
- For existing snippets, names and tags from the source are merged in.
- The summary reports added, updated, skipped, and total source counts.

---

### Title: Check database health

**As a** database maintainer (Chris),
**I want to** use the `verify` command to check that the index and fingerprints are consistent,
**so that** I can detect staleness or corruption before it affects search results.

**Acceptance Criteria:**
- `resembl verify` reports snippet count, bucket-row count, and fingerprint format version.
- A missing index or stale fingerprints is reported as a warning (healed automatically by the next `find`).
- A bucket/snippet mismatch is reported as an issue, and the command exits with code 1.

---

### Title: View a snippet's version history

**As a** database maintainer (Chris),
**I want to** use the `version` command to see recorded history for a snippet,
**so that** I can audit how an entry came to be.

**Acceptance Criteria:**
- `resembl version <checksum>` lists recorded versions (id and timestamp); checksum prefixes are accepted.
- When no history exists, the tool says so instead of erroring.
- The output can be formatted as JSON or CSV via the global `--format` option.

---

### Title: Get database statistics

**As a** database maintainer (Chris),
**I want to** use the `stats` command to see high-level statistics about the database,
**so that I can** understand the size and complexity of the dataset.

**Acceptance Criteria:**
- `resembl stats` displays the total number of snippets, average snippet size, vocabulary size, and average in-dataset similarity.
- The output can be formatted as JSON with `--format json`.

---

### Title: Re-index the database

**As a** database maintainer (Chris),
**I want to** use the `reindex` command to recalculate all MinHashes,
**so that I can** apply changes to the hashing or normalization algorithm to all existing entries.

**Acceptance Criteria:**
- `resembl reindex` recalculates all hashes.
- The user is prompted for confirmation before starting.
- The tool displays statistics about the re-indexing process.

---

### Title: Manage user configuration

**As a** security researcher (Ben),
**I want to** use the `config` command to manage my default settings,
**so that I can** customize the tool's behavior without using flags for every command.

**Acceptance Criteria:**
- `resembl config path` shows the location of the config file.
- `resembl config list` displays the current settings.
- `resembl config get <key>` prints the effective value of a single setting.
- `resembl config set <key> <value>` sets a new default value.
- The tool reads user overrides for `lsh_threshold`, `top_n`, and other keys from `~/.config/resembl/config.toml`. `$XDG_CONFIG_HOME` is honored when set, and `RESEMBL_CONFIG_DIR` overrides both.

---

### Title: Unset a configuration value

**As a** security researcher (Ben),
**I want to** use the `config unset` command to reset a setting to its default value,
**so that I can** undo a previous customization without having to look up the default.

**Acceptance Criteria:**
- `resembl config unset <key>` removes the key from the config file.
- The setting reverts to its default value.
- `resembl config list` confirms the default is restored.

---

### Title: Perform a quick end-to-end lookup

**As a** reverse engineer (Alex),
**I want to** add a snippet and immediately search for it,
**so that** I can verify the tool and its LSH index are working.

**Acceptance Criteria:**
- `resembl add my_snippet "MOV EAX, EBX"` stores the snippet and updates the LSH index.
- `resembl find --query "MOV EAX, EBX"` returns that snippet among the results.
- Each result includes a similarity score.
