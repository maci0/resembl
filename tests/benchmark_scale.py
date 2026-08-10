"""
A scaling benchmark for resembl.

Unlike ``benchmark.py`` (a quick smoke benchmark), this script is designed
for larger datasets and reports the metrics that matter at scale:

- Bulk ``import`` wall time (CLI, process-pool path).
- First ``find`` (cold LSH cache -> index rebuild + save).
- Subsequent ``find`` (warm LSH cache).
- ``reindex`` wall time.
- Database file size and stored-MinHash footprint.

Usage::

    uv run python tests/benchmark_scale.py --num-files 5000

Optional flags:

    --data-dir DIR   Directory for generated files (default: ``scale_data``)
    --db PATH        Database file (default: ``scale_benchmark.db``)
    --jobs N         Worker processes for import (default: CPU count)
    --keep           Do not clean up generated files / database afterwards
"""

import argparse
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.generate_data import generate_files  # noqa: E402

DEFAULT_NUM_FILES = 5000
DEFAULT_DATA_DIR = "scale_data"
DEFAULT_DB = "scale_benchmark.db"


def run_command(command: list[str], extra_env: dict | None = None) -> float:
    """Run a CLI command and return the elapsed wall time in seconds."""
    env = {**os.environ, "PYTHONPATH": os.path.join(os.getcwd(), ".")}
    if extra_env:
        env.update(extra_env)
    start = time.monotonic()
    subprocess.run(
        ["python", "-m", "resembl.cli", *command],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return time.monotonic() - start


def db_size_mb(db_path: str) -> float:
    """Return the database file size in MB."""
    return os.path.getsize(db_path) / (1024 * 1024) if os.path.exists(db_path) else 0.0


def minhash_bytes_per_snippet(db_path: str) -> float:
    """Average stored MinHash byte size (compact format should be ~520 B)."""
    conn = sqlite3.connect(db_path)
    try:
        total, count = conn.execute(
            "SELECT SUM(LENGTH(minhash)), COUNT(*) FROM snippet"
        ).fetchone()
    finally:
        conn.close()
    return (total / count) if count else 0.0


def main() -> None:
    """Run the scaling benchmark."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-files", type=int, default=DEFAULT_NUM_FILES)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    num_files = args.num_files
    db_url = f"sqlite:///{args.db}"
    env = {"DATABASE_URL": db_url}

    if os.path.exists(args.db):
        os.remove(args.db)
    if os.path.exists(args.data_dir):
        shutil.rmtree(args.data_dir)

    print(f"Generating {num_files} assembly files...")
    generate_files(data_dir=args.data_dir, num_files=num_files)

    import_args = ["--quiet", "import", "--force"]
    if args.jobs:
        import_args += ["--jobs", str(args.jobs)]
    import_args.append(args.data_dir)

    print(f"\n--- Import: {num_files} files ---")
    t = run_command(import_args, env)
    print(f"Import took: {t:.3f}s  ({num_files / t:,.0f} files/s)")
    print(f"DB size: {db_size_mb(args.db):.2f} MB")
    print(f"Avg MinHash bytes/snippet: {minhash_bytes_per_snippet(args.db):.1f}")

    query_file = os.path.join(
        args.data_dir, random.choice([f for f in os.listdir(args.data_dir)])
    )

    print("\n--- Find (cold cache: LSH rebuild + save) ---")
    t_cold = run_command(["--quiet", "find", "--file", query_file], env)
    print(f"Cold find took: {t_cold:.3f}s")

    print("\n--- Find (warm cache) ---")
    t_warm = run_command(["--quiet", "find", "--file", query_file], env)
    print(f"Warm find took: {t_warm:.3f}s")

    print("\n--- Reindex ---")
    t_reindex = run_command(["--quiet", "reindex", "--force"], env)
    print(f"Reindex took: {t_reindex:.3f}s")

    if not args.keep:
        print("\nCleaning up...")
        if os.path.exists(args.db):
            os.remove(args.db)
        if os.path.exists(args.data_dir):
            shutil.rmtree(args.data_dir)
    print("Done.")


if __name__ == "__main__":
    main()
