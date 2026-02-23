"""
A simple benchmarking script for resembl.

This script measures the performance of two key operations:
1. `import`: How long it takes to bulk-import a large number of files.
2. `find`: How long it takes to search for a snippet in a large database.
"""

import os
import random
import shutil
import subprocess
import sys
import time

# The following is needed for standalone execution, but triggers a lint error.
# pylint: disable=wrong-import-position
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests.generate_data import generate_files

# --- Benchmark Configuration ---
NUM_FILES = 1000
DATA_DIR = "data"


def run_command(command, extra_env=None):
    """Helper function to run a command and return the elapsed time."""
    env = {
        **os.environ,
        "PYTHONPATH": os.path.join(os.getcwd(), "."),
    }
    if extra_env:
        env.update(extra_env)

    start_time = time.monotonic()
    subprocess.run(
        ["python", "-m", "resembl.cli", *command.split()],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    end_time = time.monotonic()
    return end_time - start_time


def main():
    """Main function to run the benchmarks."""
    db_name = "benchmark.db"
    db_url = f"sqlite:///{db_name}"
    extra_env = {"DATABASE_URL": db_url}

    # --- 1. Generate test data ---
    print(f"Generating {NUM_FILES} assembly files for the benchmark...")
    generate_files(data_dir=DATA_DIR, num_files=NUM_FILES)
    print("Generation complete.")

    # --- 2. Benchmark `import` command ---
    print(f"\n--- Benchmarking 'import' on {NUM_FILES} files ---")
    import_time = run_command(f"import --force {DATA_DIR}", extra_env=extra_env)
    print(f"Import took: {import_time:.4f} seconds")

    # --- 3. Benchmark `find` command ---
    print("\n--- Benchmarking 'find' on a single query ---")
    # Get a random file to use as a query
    random_file = random.choice(os.listdir(DATA_DIR))
    query_file_path = os.path.join(DATA_DIR, random_file)

    find_time = run_command(f"find --file {query_file_path}", extra_env=extra_env)
    print(f"Find took: {find_time:.4f} seconds")

    # --- 4. Clean up ---
    print("\nCleaning up generated files and database...")
    if os.path.exists(db_name):
        os.remove(db_name)
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    print("Cleanup complete.")


if __name__ == "__main__":
    main()
