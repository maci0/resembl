# Tutorial: Finding Known Functions in a Firmware Dump

This tutorial walks through a real-world workflow: importing a library of known
functions, analyzing a firmware dump, and generating detection rules.

## Prerequisites

```bash
pip install resembl
# or: uv pip install resembl
```

## Step 1: Build Your Reference Library

Start by importing known functions from your collection of analyzed samples:

```bash
# Import a single function
resembl add "memcpy_optimized" "push ebp
mov ebp, esp
mov ecx, [ebp+10h]
mov esi, [ebp+0Ch]
mov edi, [ebp+08h]
rep movsb
pop ebp
ret"

# Bulk import from .asm files
resembl import ./known_functions/ --recursive --jobs 4
```

## Step 2: Organize with Collections

Group related snippets for easier management:

```bash
# Create collections
resembl collection create "libc" -d "Standard C library functions"
resembl collection create "crypto" -d "Cryptographic routines"

# Add snippets to collections
resembl collection add libc abc123
resembl collection add crypto def456

# View a collection
resembl collection show crypto
```

## Step 3: Tag Important Snippets

Use tags for cross-cutting concerns:

```bash
resembl tag add abc123 "vulnerable"
resembl tag add abc123 "CVE-2024-1234"
```

## Step 4: Analyze a New Firmware Dump

Now, when you encounter a new binary, extract the disassembly and search:

```bash
# Search for similar functions
resembl find "push ebp
mov ebp, esp
sub esp, 20h
mov eax, [ebp+8]
mov ecx, [ebp+0Ch]" --top-n 10

# Compare two specific snippets
resembl compare abc123 xyz789 --diff
```

The `find` command uses MinHash + LSH for fast filtering, then ranks
candidates by a hybrid score combining Jaccard and Levenshtein similarity.

## Step 5: Generate YARA Rules

Generate detection rules from identified functions:

```bash
# Generate a YARA rule
resembl yara abc123 --rule-name suspicious_memcpy
```

This outputs a YARA rule with hex patterns and metadata derived from the
snippet's content.

## Step 6: Export and Share

Export your library for team use:

```bash
# Export as .asm files
resembl export ./export_dir/

# Export as JSON
resembl export ./export_dir/ --format json
```

## Working with Different Architectures

resembl normalizes registers from multiple architectures:

- **x86/x64:** `eax`, `rbx`, `r15d` → `REG`
- **ARM:** `r0`, `x19`, `w0`, `lr` → `REG`
- **MIPS:** `$t0`, `$ra`, `$f1` → `REG`
- **RISC-V:** `a0`, `s1`, `t3`, `fa7` → `REG`

This means a function compiled for ARM and x86 with the same structure will
have high similarity scores.

## Using as a Python Library

```python
from resembl import code_tokenize, snippet_find_matches
from resembl.database import engine
from sqlmodel import Session

# Tokenize and inspect
tokens = code_tokenize("mov eax, [ebp+8]")
print(tokens)  # ['MOV', 'REG', 'REG', 'IMM']

# Search programmatically
with Session(engine) as session:
    results = snippet_find_matches(session, "mov eax, [ebp+8]")
    for match in results["matches"]:
        print(f"{match['names'][0]}: {match['similarity']:.1%}")
```

## Team Setup with PostgreSQL

For shared databases, set the `DATABASE_URL` environment variable:

```bash
export DATABASE_URL="postgresql://user:password@db-host:5432/resembl"
resembl import ./shared_samples/ --recursive
```

All team members point to the same database for a unified library.
