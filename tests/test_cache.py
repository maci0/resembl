"""Tests for the cache module."""

import os
import pickle
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from resembl.cache import (
    cache_dir_get,
    db_checksum_path_get,
    lsh_cache_invalidate,
    lsh_cache_load,
    lsh_cache_path_get,
    lsh_cache_save,
    lsh_index_build,
)
from resembl.database import db_checksum_get
from resembl.models import Snippet


class TestCache(unittest.TestCase):
    """Tests for caching functionality."""

    def setUp(self):
        """Set up a mock session for each test."""
        self.session = MagicMock()

    def test_lsh_cache_path_get(self):
        """Test the LSH cache path generation."""
        path = lsh_cache_path_get(0.75)
        self.assertTrue(path.endswith("lsh_0.75.pkl"))

    def test_lsh_cache_save_and_load(self):
        """Test saving and loading the LSH cache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                lsh = {"test": "data"}
                self.session.exec.return_value.one.return_value = 1
                self.session.exec.return_value.first.return_value = Snippet(
                    checksum="abc", code="code"
                )

                lsh_cache_save(self.session, lsh, 0.5)
                loaded_lsh = lsh_cache_load(self.session, 0.5)
                self.assertEqual(lsh, loaded_lsh)

    def test_load_nonexistent_cache(self):
        """Test loading a nonexistent cache file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                loaded_lsh = lsh_cache_load(self.session, 0.5)
                self.assertIsNone(loaded_lsh)

    def test_load_corrupted_cache(self):
        """Test loading a corrupted cache file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                lsh = {"test": "data"}
                self.session.exec.return_value.one.return_value = 1
                self.session.exec.return_value.first.return_value = Snippet(
                    checksum="abc", code="code"
                )
                lsh_cache_save(self.session, lsh, 0.5)

                # Corrupt the file
                cache_path = lsh_cache_path_get(0.5)
                with open(cache_path, "wb") as f:
                    f.write(b"corrupted")

                with self.assertRaises(pickle.UnpicklingError):
                    lsh_cache_load(self.session, 0.5)

    def test_cache_invalidation(self):
        """Test that the cache can be invalidated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                lsh = {"test": "data"}
                self.session.exec.return_value.one.return_value = 1
                self.session.exec.return_value.first.return_value = Snippet(
                    checksum="abc", code="code"
                )
                lsh_cache_save(self.session, lsh, 0.5)
                self.assertTrue(os.path.exists(lsh_cache_path_get(0.5)))
                lsh_cache_invalidate()
                self.assertFalse(os.path.exists(lsh_cache_path_get(0.5)))

    def test_lsh_index_build_invalid_params(self):
        """Test that building LSH with invalid params returns None."""
        with self.assertLogs("resembl", level="ERROR"):
            lsh = lsh_index_build(self.session, 2.0, 128)
            self.assertIsNone(lsh)

    def test_db_checksum_get_empty(self):
        """Test getting a checksum from an empty database."""
        self.session.exec.return_value.one.return_value = 0
        checksum = db_checksum_get(self.session)
        self.assertEqual(checksum, "empty")

    def test_cache_dir_respects_env_override(self):
        """Verify that cache_dir_get reads the env var at call time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                self.assertEqual(cache_dir_get(), tmpdir)
                self.assertTrue(db_checksum_path_get().startswith(tmpdir))


if __name__ == "__main__":
    unittest.main()
