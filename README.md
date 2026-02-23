<p align="center">
  <img src="docs/resembl_mascot.png" alt="resembl mascot" width="200">
</p>

# resembl — Assembly Code Similarity Search

[![codecov](https://codecov.io/gh/maci0/resembl/branch/main/graph/badge.svg)](https://codecov.io/gh/maci0/resembl)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Linting: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![GitHub license](https://img.shields.io/github/license/maci0/resembl)](https://github.com/maci0/resembl/blob/main/LICENSE)
[![GitHub last commit](https://img.shields.io/github/last-commit/maci0/resembl)](https://github.com/maci0/resembl/commits/main)

`resembl` is a command-line tool designed to find similar assembly code snippets within a database. It uses a combination of hashing and fuzzy string matching to provide fast and accurate results, even when the query is a small fragment of a larger function.

This tool is ideal for tasks such as:
- Identifying known functions from a binary dump.
- Finding code that is structurally similar to a given sample, despite minor differences.
- Building a searchable library of assembly code patterns.

## Core Concepts

### Checksum as Primary Key

Instead of a traditional integer ID, each snippet in the database is identified by a **SHA256 checksum**. This checksum is calculated from the snippet's code after it has been normalized (comments and extra whitespace are removed).

This approach provides several advantages:
- **Content-Addressable:** The ID of a snippet is derived directly from its content.
- **Deduplication:** It is impossible to have two entries with the exact same code.
- **Stable IDs:** The identifier for a snippet remains the same, even if the database is rebuilt.

### LSH Caching

To make searches nearly instantaneous, `resembl` caches the LSH index to a file in `~/.cache/resembl/`. The location can be overridden with the `RESEMBL_CACHE_DIR` environment variable. The cache is automatically invalidated and rebuilt whenever the database is modified.

## How It Works

The search process is a two-step pipeline designed for both speed and accuracy:

### 1. Fast Candidate Filtering with MinHash and LSH

To avoid the slow process of comparing a query against every single entry in the database, we first perform a fast filtering step to find a small number of likely candidates.

- **Normalization:** The assembly code is first "normalized" by a lexer. This process simplifies the code to its core structure, for example by replacing all register names (e.g., `EAX`, `EBX`) with a generic `REG` token and all immediate values with `IMM`. This makes the comparison robust against simple register or value changes. This behavior can be disabled with the `--no-normalization` flag on the `find` command.

- **Normalization Details:** The normalization process is designed to create a canonical representation of the assembly code, focusing on the structural logic rather than specific register choices or immediate values. Here’s what it does:
    - **Generalizes Registers:** All general-purpose registers (e.g., `EAX`, `RBX`, `RDI`) are replaced with the generic token `REG`.
    - **Generalizes Immediate Values:** All numerical values (e.g., `0x10`, `42`) are replaced with the token `IMM`.
    - **Generalizes Labels:** All labels (e.g., `loc_123:`, `?_0001:`) are replaced with the token `LABEL`.
    - **Normalizes Memory Operands:** Memory size indicators like `dword ptr`, `byte`, etc., are replaced with `MEM_SIZE`.
    - **Removes Comments:** All comments are stripped out.
    - **Standardizes Formatting:** All tokens are converted to uppercase, and whitespace is normalized.

    **Example:**

    **Before Normalization:**
    ```asm
    ; ---- 10001000 ----
    ?_0001: ; Local function
            push    esi
            mov     esi, dword [esp+0CH]
            push    edi
            mov     edi, dword [esp+0CH]
    ```

    **After Normalization:**
    ```
    LABEL PUSH REG MOV REG MEM_SIZE [ REG + IMM ] PUSH REG MOV REG MEM_SIZE [ REG + IMM ]
    ```

- **MinHash:** Each normalized snippet is converted into a **MinHash**. A MinHash is a compact "fingerprint" of the code. Snippets with similar structures will produce similar MinHash fingerprints.

- **Locality Sensitive Hashing (LSH):** We use a `MinHashLSH` index to store all the MinHashes. This data structure acts like a "bucketing" system. Similar MinHashes are likely to be placed into the same buckets. When you search, we hash your query's MinHash and only retrieve candidates from the buckets it lands in. This is an extremely fast way to narrow down a huge database to a handful of potential matches.

The key idea behind LSH is to hash items so that similar items have a higher probability of ending up in the same "bucket." The banding technique is a method for amplifying this effect, making the process more efficient and reliable for finding collision candidates.

Here’s how it works:

1.  **Divide the Signature:** The MinHash signature (a list of 128 hash values) is divided into several smaller "bands." For example, if we have 128 hashes and we create 32 bands, each band would contain 4 hashes.

2.  **Hash Each Band:** Each band is then hashed separately. If two snippets have a band that is identical (meaning all hash values within that band are the same), they will produce the same hash for that band and be considered candidates for a match.

3.  **Candidate Pairs:** Two snippets are considered a candidate pair if they are identical in at least one band.

This technique is effective because it balances the trade-off between false positives and false negatives. By requiring an entire band to match, we reduce the chance of accidental collisions (false positives). At the same time, by allowing a match in *any* of the bands, we increase the chance of finding truly similar items, even if their MinHash signatures are not identical (avoiding false negatives). This makes the LSH process both fast and effective at finding likely matches in a very large dataset.

### 2. Accurate Ranking with RapidFuzz

The LSH step gives us a small list of candidates, but it's not perfectly accurate. The second step is to precisely rank these candidates.

- **Fuzzy String Matching:** We use the `rapidfuzz` library to calculate the similarity score between the original query and the full code of each candidate snippet. This comparison is more computationally expensive, but since we only run it on a few candidates, it remains very fast.

- **Ranking:** The candidates are then ranked by their similarity score, and the top results are presented to the user.

### Algorithm Details

#### How MinHash Works

The MinHash algorithm is a technique for quickly estimating how similar two sets are. In our case, the "sets" are the normalized assembly code snippets. Here’s a step-by-step breakdown of how it works:

1.  **Shingling:** The normalized code is first broken down into a set of overlapping "shingles" (or n-grams). For example, a 3-shingle of the tokens `['MOV', 'REG', 'IMM']` would be `('MOV', 'REG', 'IMM')`. This creates a set of all unique shingles in the snippet.

2.  **Hashing:** Each unique shingle is then hashed to an integer. This converts the set of shingles into a set of numbers.

3.  **Min-Hashing:** A fixed number of different hash functions (in our case, 128, as defined by `num_permutations`) are applied to each number in the set of hashed shingles. For each hash function, we only keep the *minimum* hash value produced across all shingles.

4.  **Signature:** The collection of these 128 minimum hash values becomes the "MinHash signature" for the snippet.

The key insight is that the similarity of two MinHash signatures is a good estimate of the Jaccard similarity of the original shingle sets. This allows us to compare fingerprints instead of the full code, which is significantly faster.

#### The RapidFuzz Algorithm

After the LSH index provides a list of potential candidates, `resembl` uses the `rapidfuzz` library to perform a more precise similarity calculation. `rapidfuzz` is a high-performance library that implements a variety of string similarity algorithms in C++.

The primary algorithm used is a variation of the **Levenshtein distance**, which measures the number of edits (insertions, deletions, or substitutions) needed to change one string into another. `rapidfuzz` calculates a normalized similarity ratio based on this distance, which gives a score from 0 to 100, where 100 is a perfect match.

By using `rapidfuzz`, `resembl` can accurately score the similarity between the query and candidate snippets, ensuring that the final results are ranked by their true similarity, not just the approximation from the LSH step.

### Lookup Flow

When you run `resembl find`, the tool processes your query in several stages:

1. **Load metadata** – The SQLite database and the cached LSH index are loaded. If the index is missing or stale it is rebuilt from the stored MinHash fingerprints.
2. **Normalize query** – The query assembly is lexed and normalized unless `--no-normalization` is specified.
3. **MinHash lookup** – The query's MinHash is hashed into the LSH buckets to retrieve candidate checksums.
4. **Retrieve candidates** – Snippets matching those checksums are loaded from the database.
5. **Score** – Each candidate is compared to the original query with RapidFuzz and assigned a similarity score.
6. **Display results** – Candidates are sorted by score and the top results are shown or output as JSON.

This pipeline lets `resembl` search large datasets in milliseconds while ranking results accurately.

## Project Structure

```
resembl/
├── resembl/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cache.py
│   ├── cli.py
│   ├── config.py
│   ├── core.py
│   ├── database.py
│   └── models.py
├── docs/
│   ├── custom_database.md
│   ├── flowcharts.md
│   └── user_stories.md
├── fuzzers/
├── tests/
├── .gitignore
├── CONTRIBUTING.md
├── README.md
└── pyproject.toml
```

## Setup and Usage

### 1. Installation

This project is managed with [uv](https://github.com/astral-sh/uv). First, install uv if you haven't already. Then, from the root of the project, run:

```bash
# 1. Create and activate the virtual environment
uv venv
source .venv/bin/activate

# 2. Install dependencies
uv pip install -e .[dev]

# 3. (Recommended for developers) Install pre-commit hooks
uv run pre-commit install
```



### 2. Configuration

You can create a configuration file at `~/.config/resembl/config.toml` to set
default values. Set `RESEMBL_CONFIG_DIR` to override this location.

| Key               | Default | Description |
|-------------------|--------:|-------------|
| `lsh_threshold`   | `0.5`   | Minimum Jaccard similarity used when querying the LSH index. Lower values yield more candidates. |
| `num_permutations`| `128`   | Number of permutations used when building MinHash fingerprints. |
| `top_n`           | `5`     | Number of matches returned by the `find` command. |

**Example `config.toml`:**
```toml
lsh_threshold = 0.8
top_n = 5
```

You can manage this file with the `resembl config` command.

### 3. Usage

The CLI can be invoked through uv or by running the module directly after activating the shell.

**Examples:**
```bash
# Run commands through uv
uv run resembl add my_memcpy "MOV EAX, EBX; ..."

# Or, after activating the virtual environment, you can call it directly
resembl find --query "MOV EAX"

Global options:
```
--quiet      Suppress informational output
--verbose    Increase output verbosity
--no-color   Disable colored output
```

For a detailed breakdown of all commands and features, see the [User Stories](./docs/user_stories.md) or run:
```bash
uv run resembl --help
```

### 4. Example with test_data

This is an example of how to import the provided `test_data` and then search for a snippet within it.

```bash
# import the data
uv run resembl import tests/test_data
```

```bash
# search for a snippet
uv run resembl find --threshold 0.2 --query "push esi; mov esi, dword [esp+0CH]; push edi"
```

```bash
# search for a function
uv run resembl find --file tests/test_data/1000A0A0.asm
```

### 5. Running Tests

To ensure everything is working correctly, you can run the test suite:
```bash
uv run pytest
```

#### Code Coverage

This project uses `pytest-cov` to measure test coverage. A GitHub Actions workflow runs on every pull request to ensure that code quality is maintained. The results are uploaded to [Codecov](https://codecov.io/gh/maci0/resembl).

You can run the coverage report locally with:
```bash
uv run pytest --cov=resembl
```

## Advanced Usage

- **[Using a Custom Database](./docs/custom_database.md)**: Learn how to integrate `resembl` with your own application's database.

## Benchmarking

A simple benchmarking script is included to measure the performance of the `import` and `find` commands.

To run the benchmark, execute the following command from the root of the project:

```bash
uv run python tests/benchmark.py
```

The script will:
1.  Generate 1,000 new assembly files in a `data/` directory.
2.  Measure the time it takes to import all of these files.
3.  Measure the time it takes to run a `find` query against the newly created database.
4.  Clean up the generated files and the benchmark database.

This provides a quick way to assess the performance of the core functionality on a non-trivial dataset.

## Development

For detailed guidelines on contributing to this project, please see our [Contributor Guide](./CONTRIBUTING.md).

### Generating Test Data

The `tests/generate_data.py` script can be used to create a large number of randomized assembly files for performance testing and validation. By default, it generates 1,000 files in a `data/` directory, but this can be configured.

**Usage:**
```bash
# Generate 1000 files in the default 'data/' directory
uv run python tests/generate_data.py

# Generate 500 files in a custom directory
uv run python tests/generate_data.py --num-files 500 --data-dir custom_data/
```

You can then import the generated files into `resembl` using the `import` command:
```bash
uv run resembl import data/
```

## Visual Flowcharts

For a visual representation of the core workflows, see the [Flowcharts](./docs/flowcharts.md).
