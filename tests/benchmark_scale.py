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
    --db PATH        SQLite database file (default: ``scale_benchmark.db``)
    --db-url URL     Full DATABASE_URL override (DuckDB, PostgreSQL, ...);
                     SQLite-only metrics are skipped for non-SQLite backends
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
        [sys.executable, "-m", "resembl.cli", *command],
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
        total, count = conn.execute("SELECT SUM(LENGTH(minhash)), COUNT(*) FROM snippet").fetchone()
    finally:
        conn.close()
    return (total / count) if count else 0.0


def main() -> None:
    """Run the scaling benchmark."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-files", type=int, default=DEFAULT_NUM_FILES)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument(
        "--db-url",
        default=None,
        help="Full DATABASE_URL (e.g. duckdb:///file.db or "
        "postgresql+pg8000://user:pass@host/db).  Overrides --db; "
        "SQLite-only metrics (DB size, MinHash footprint) are skipped.",
    )
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed for reproducible data + query selection.",
    )
    args = parser.parse_args()

    num_files = args.num_files
    is_sqlite = args.db_url is None
    db_url = args.db_url or f"sqlite:///{args.db}"
    env = {"DATABASE_URL": db_url}

    if is_sqlite and os.path.exists(args.db):
        os.remove(args.db)
    if os.path.exists(args.data_dir):
        shutil.rmtree(args.data_dir)

    print(f"Generating {num_files} assembly files...")
    generate_files(data_dir=args.data_dir, num_files=num_files, seed=args.seed)

    import_args = ["--quiet", "import", "--force"]
    if args.jobs:
        import_args += ["--jobs", str(args.jobs)]
    import_args.append(args.data_dir)

    print(f"\n--- Import: {num_files} files ---")
    t = run_command(import_args, env)
    print(f"Import took: {t:.3f}s  ({num_files / t:,.0f} files/s)")
    if is_sqlite:
        print(f"DB size: {db_size_mb(args.db):.2f} MB")
        print(f"Avg MinHash bytes/snippet: {minhash_bytes_per_snippet(args.db):.1f}")
    else:
        print("DB size / MinHash footprint: n/a (non-SQLite backend)")

    if args.seed is not None:
        random.seed(args.seed)
    query_file = os.path.join(args.data_dir, random.choice(os.listdir(args.data_dir)))

    print("\n--- Find (cold cache: LSH rebuild + save) ---")
    t_cold = run_command(["--quiet", "find", "--file", query_file], env)
    print(f"Cold find took: {t_cold:.3f}s")

    print("\n--- Find (warm cache) ---")
    t_warm = run_command(["--quiet", "find", "--file", query_file], env)
    print(f"Warm find took: {t_warm:.3f}s")

    # Warm find through a `serve` process, measured with the stdlib-only thin
    # client — the headline "instant warm finds" number end to end.
    with open(query_file, encoding="utf-8", errors="replace") as f:
        query_text = f.read()
    serve_env = {
        **os.environ,
        **env,
        "PYTHONPATH": os.path.join(os.getcwd(), "."),
    }
    serve_proc = subprocess.Popen(
        [sys.executable, "-m", "resembl.cli", "--quiet", "serve"],
        env=serve_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        from resembl.server import server_port_path

        port_file = server_port_path(db_url)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                with open(port_file, encoding="utf-8") as f:
                    int(f.read().strip())
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        else:
            raise RuntimeError("serve did not write its port file in time")
        n_runs = 5
        t0 = time.monotonic()
        for _ in range(n_runs):
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "resembl.find_client",
                    "--query",
                    query_text,
                ],
                capture_output=True,
                env=serve_env,
                check=False,
            )
        avg_ms = (time.monotonic() - t0) / n_runs * 1000
        print(f"Thin-client warm find via serve: {avg_ms:.1f} ms avg ({n_runs} runs)")
    finally:
        serve_proc.terminate()
        try:
            serve_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            serve_proc.kill()
            serve_proc.wait(timeout=5)

    print("\n--- Reindex ---")
    t_reindex = run_command(["--quiet", "reindex", "--force"], env)
    print(f"Reindex took: {t_reindex:.3f}s")

    if not args.keep:
        print("\nCleaning up...")
        if is_sqlite and os.path.exists(args.db):
            os.remove(args.db)
        if os.path.exists(args.data_dir):
            shutil.rmtree(args.data_dir)
    print("Done.")


if __name__ == "__main__":
    main()
