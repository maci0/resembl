# Ideas for Improvement

A living document of potential enhancements, optimizations, and new features for resembl.

---

## 🔬 Core Algorithm

- [ ] **Weighted shingling** — Give higher weight to rare instruction patterns (e.g., `CPUID`, `RDTSC`) over common ones (`MOV`, `PUSH`) to improve match quality for distinctive code.
- [ ] **Variable n-gram size** — Allow configurable shingle size (currently hard-coded to 3-grams). Larger n-grams may improve precision for longer snippets.
- [ ] **Hybrid scoring** — Combine Jaccard + Levenshtein into a single composite score with configurable weights, rather than ranking by Levenshtein alone.
- [ ] **Architecture-aware normalization** — Detect and handle ARM, MIPS, and RISC-V in addition to x86/x64. The tokenizer currently assumes x86 register names.
- [ ] **Control-flow graph (CFG) similarity** — Extract basic block structure and compare CFGs as an alternative similarity metric for more complex snippets.

## ⚡ Performance

- [ ] **Batch MinHash computation** — Use `numpy` vectorized operations for MinHash generation instead of Python loops.
- [ ] **Async file I/O for import** — Use `asyncio` or thread pools for reading .asm files during bulk import to overlap I/O with hashing.
- [ ] **Incremental LSH index** — Instead of rebuilding the entire LSH index when the cache is stale, support incremental insertion of new entries.
- [ ] **WAL mode for SQLite** — Enable Write-Ahead Logging for better concurrent read performance.

## 🖥️ CLI & UX

- [ ] **Progress bars** — Use `rich.progress` for long-running operations like `import`, `reindex`, and `export`.
- [ ] **Interactive mode** — A REPL-like mode (`resembl shell`) for exploring the database without repeated startup cost.
- [ ] **Diff output for compare** — Show a side-by-side or unified diff of the two snippets alongside the similarity metrics.
- [ ] **Syntax highlighting** — Use `rich.syntax` to highlight assembly code in `show` and `compare` output.
- [ ] **Pager support** — Automatically pipe long output (e.g., `list` with many snippets) through a pager.
- [ ] **Snippet search by name** — `resembl find --name <pattern>` fuzzy search on snippet names, not just code similarity.
- [ ] **`--format` flag** — Support `table`, `json`, `csv`, `tsv` output formats with a single flag instead of `--json`.

## 📦 Features

- [ ] **Tags / labels** — Allow tagging snippets with metadata (e.g., `malware`, `crypto`, `string-ops`) for filtered searches.
- [ ] **Snippet groups / collections** — Organize snippets into named collections (e.g., "libc patterns", "crypto routines").
- [ ] **Import from IDA / Ghidra** — Parse IDA `.lst` or Ghidra XML export files directly, extracting function boundaries automatically.
- [ ] **Export to YARA rules** — Generate YARA-compatible patterns from snippet databases for use in malware scanning.
- [ ] **Database merge** — `resembl merge <other.db>` to combine two snippet databases, deduplicating by checksum.
- [ ] **Snippet versioning** — Track history of code changes for a given snippet name, useful for tracking function evolution across binary versions.
- [ ] **Web UI** — A lightweight Flask/FastAPI dashboard for browsing and searching the database visually.

## 🧪 Testing & Quality

- [ ] **Property-based tests** — Use `hypothesis` to generate random assembly-like strings and verify invariants (e.g., `tokenize(code)` never crashes, checksums are deterministic).
- [ ] **Benchmark suite** — Formalize the existing benchmark script into a `pytest-benchmark` suite with historical tracking.
- [ ] **Test coverage gate** — Enforce minimum coverage threshold in CI (e.g., 85%).
- [ ] **Integration test for `--no-color`** — Verify that no ANSI/Rich markup leaks into `--no-color` output.

## 🏗️ Architecture

- [ ] **Plugin system** — Allow custom tokenizers/normalizers to be registered as plugins for supporting new architectures.
- [ ] **Abstract storage backend** — Decouple from SQLite so the tool can work with PostgreSQL or other backends for team use.
- [ ] **Separate library from CLI** — Publish `resembl-core` as a standalone library and `resembl` as a thin CLI wrapper.
- [ ] **Type-safe config** — Replace the `dict`-based config with a `dataclass` or Pydantic model for validation and autocompletion.

## 📖 Documentation

- [ ] **Man page** — Generate a man page from the CLI help text.
- [ ] **Architecture decision records (ADRs)** — Document key design decisions (e.g., why MinHash over SimHash, why SQLite).
- [ ] **API reference** — Auto-generate docs from docstrings using Sphinx or MkDocs.
- [ ] **Tutorial** — Step-by-step guide: "Finding known functions in a firmware dump."
