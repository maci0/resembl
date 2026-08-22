"""Tests for the benchmark.py script."""

import os
import shutil
import subprocess
import unittest

# This must match the DATA_DIR used in the benchmark script.
DATA_DIR = "data"


class TestBenchmarkScript(unittest.TestCase):
    """Tests for the benchmark.py script."""

    def setUp(self):
        """Ensure the benchmark artifacts don't exist before a test."""
        self.db_name = "benchmark.db"
        if os.path.exists(self.db_name):
            os.remove(self.db_name)
        if os.path.exists(DATA_DIR):
            shutil.rmtree(DATA_DIR)

    def tearDown(self):
        """Clean up any leftover artifacts if a test fails."""
        if os.path.exists(self.db_name):
            os.remove(self.db_name)
        if os.path.exists(DATA_DIR):
            shutil.rmtree(DATA_DIR)

    def test_benchmark_script_runs_and_cleans_up(self):
        """
        Test that the benchmark script runs to completion and
        cleans up its generated files and database.
        """
        # Assert that artifacts don't exist initially
        self.assertFalse(os.path.exists(self.db_name))
        self.assertFalse(os.path.exists(DATA_DIR))

        # Run the benchmark script
        env = os.environ.copy()
        env["PYTHONPATH"] = "."
        result = subprocess.run(
            ["python", "tests/benchmark.py"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        # Check that the script executed successfully
        self.assertEqual(
            result.returncode, 0, f"Benchmark script failed with error: {result.stderr}"
        )
        self.assertIn("--- Benchmarking 'import'", result.stdout)
        self.assertIn("--- Benchmarking 'find'", result.stdout)
        self.assertIn("Cleanup complete", result.stdout)

        # Assert that the script cleaned up after itself
        self.assertFalse(os.path.exists(self.db_name), "Benchmark database was not cleaned up.")
        self.assertFalse(os.path.exists(DATA_DIR), "Generated data directory was not cleaned up.")


if __name__ == "__main__":
    unittest.main()
