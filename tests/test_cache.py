"""Tests for the cache module."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from resembl.cache import (
    cache_dir_get,
    db_checksum_path_get,
    lsh_cache_load,
    lsh_cache_path_get,
    lsh_cache_save,
    lsh_index_build,
)


def _unpickle_canary() -> None:
    """Called only if a hostile pickle blob is ever deserialized."""
    raise AssertionError("hostile pickle blob was deserialized")


class TestCache(unittest.TestCase):
    """Tests for caching functionality."""

    def setUp(self):
        """Set up a mock session for each test."""
        self.session = MagicMock()
        # LSH metadata lookup (SQLite-backed index) returns "no index built".
        self.session.exec.return_value.one_or_none.return_value = None

    def test_lsh_cache_path_get(self):
        """Test the LSH cache path generation."""
        path = lsh_cache_path_get(0.75)
        self.assertTrue(path.endswith("lsh_0.75.pkl"))

    def test_load_nonexistent_cache(self):
        """Test loading a nonexistent cache file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                loaded_lsh = lsh_cache_load(self.session, 0.5)
                self.assertIsNone(loaded_lsh)

    def test_load_corrupted_cache_ignored(self):
        """A corrupted legacy cache file is ignored (no exception, no load)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                cache_path = lsh_cache_path_get(0.5)
                with open(cache_path, "wb") as f:
                    f.write(b"corrupted")

                self.assertIsNone(lsh_cache_load(self.session, 0.5))

    def test_legacy_pickle_cache_file_is_not_unpickled(self):
        """A legacy pickle cache in the cache dir must not execute on load."""
        import pickle

        class _Canary:
            def __reduce__(self):  # detonates only if the blob is unpickled
                return _unpickle_canary, ()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                with open(lsh_cache_path_get(0.5), "wb") as f:
                    f.write(b"RESEMBL-CACHE-V2" + pickle.dumps(_Canary()))
                self.assertIsNone(lsh_cache_load(self.session, 0.5))

    def test_save_removes_stale_legacy_cache_files(self):
        """Saving the index state removes any legacy cache file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                with open(lsh_cache_path_get(0.5), "wb") as f:
                    f.write(b"stale")
                lsh_cache_save(self.session, 0.5)
                self.assertFalse(os.path.exists(lsh_cache_path_get(0.5)))

    def test_lsh_index_build_invalid_params(self):
        """Test that building LSH with invalid params returns None."""
        with self.assertLogs("resembl", level="ERROR"):
            lsh = lsh_index_build(self.session, 2.0, 128)
            self.assertIsNone(lsh)

    def test_cache_dir_respects_env_override(self):
        """Verify that cache_dir_get reads the env var at call time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                self.assertEqual(cache_dir_get(), tmpdir)
                self.assertTrue(db_checksum_path_get().startswith(tmpdir))


if __name__ == "__main__":
    unittest.main()
