"""Unit tests for the resembl config module."""

import os
import tempfile
import unittest
from unittest.mock import patch

from resembl.config import (
    DEFAULTS,
    config_dir_get,
    config_path_get,
    load_config,
    remove_config_key,
    save_config,
    update_config,
)


class TestConfig(unittest.TestCase):
    """Tests for configuration file handling."""

    def test_load_nonexistent_config(self):
        """If no config file exists, load_config should return defaults."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                config = load_config()
        self.assertEqual(config.to_dict(), DEFAULTS)

    def test_load_malformed_config(self):
        """If the config file is malformed, load_config should return defaults."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("this is not valid toml")
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                config = load_config()
        self.assertEqual(config.to_dict(), DEFAULTS)

    def test_update_and_load_config(self):
        """Test that update_config correctly writes a value and load_config reads it."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                update_config("top_n", 10)
                config = load_config()
        self.assertEqual(config.get("top_n"), 10)
        # Ensure other defaults are preserved
        self.assertEqual(config.get("lsh_threshold"), DEFAULTS.get("lsh_threshold"))

    def test_remove_config_key(self):
        """Test that remove_config_key correctly removes a key."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                update_config("top_n", 10)
                remove_config_key("top_n")
                config = load_config()
        self.assertEqual(config.get("top_n"), DEFAULTS.get("top_n"))

    def test_update_with_malformed_config(self):
        """Test that update_config works with a malformed config file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("this is not valid toml")
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                config = update_config("top_n", 10)
        self.assertEqual(config.get("top_n"), 10)
        self.assertEqual(config.get("lsh_threshold"), DEFAULTS.get("lsh_threshold"))

    def test_remove_with_malformed_config(self):
        """Test that remove_config_key works with a malformed config file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("this is not valid toml")
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                config = remove_config_key("top_n")
        self.assertEqual(config, DEFAULTS)

    def test_remove_nonexistent_key(self):
        """Test that remove_config_key does nothing for a nonexistent key."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                # Start with a known config
                update_config("top_n", 10)
                # Attempt to remove a key that isn't there
                remove_config_key("lsh_threshold")
                config = load_config()
        # The original config should be unchanged
        self.assertEqual(config.get("top_n"), 10)
        self.assertEqual(config.get("lsh_threshold"), DEFAULTS.get("lsh_threshold"))

    def test_save_config_creates_directory(self):
        """save_config should create the config directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = os.path.join(temp_dir, "subdir", "resembl")
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": config_dir}):
                save_config({"test": "value"})
            self.assertTrue(os.path.exists(config_dir))
            self.assertTrue(os.path.exists(os.path.join(config_dir, "config.toml")))

    def test_save_config_failure_cleans_temp_file(self):
        """A failed save must not leave the half-written temp file behind."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with self.assertRaises(TypeError):
                    # object() is not TOML-serializable: the dump fails
                    # after the temp file was created.
                    save_config({"top_n": object()})
            self.assertEqual(os.listdir(temp_dir), [])

    def test_save_config_replace_failure_cleans_temp_file(self):
        """A failed atomic rename must not leave the temp file behind either."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with patch("os.replace", side_effect=OSError("replace failed")):
                    with self.assertRaises(OSError):
                        save_config({"top_n": 10})
            self.assertEqual(os.listdir(temp_dir), [])

    def test_config_dir_respects_env(self):
        """config_dir_get should respect RESEMBL_CONFIG_DIR at call time."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                self.assertEqual(config_dir_get(), temp_dir)
                self.assertTrue(config_path_get().startswith(temp_dir))

    def test_config_dir_respects_xdg(self):
        """$XDG_CONFIG_HOME/resembl is used when no RESEMBL_CONFIG_DIR is set."""
        with tempfile.TemporaryDirectory() as xdg:
            # Empty string means "unset" for this resolution.
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg, "RESEMBL_CONFIG_DIR": ""}):
                self.assertEqual(config_dir_get(), os.path.join(xdg, "resembl"))

    def test_config_dir_override_beats_xdg(self):
        """RESEMBL_CONFIG_DIR takes precedence over $XDG_CONFIG_HOME."""
        with tempfile.TemporaryDirectory() as override, tempfile.TemporaryDirectory() as xdg:
            env = {"RESEMBL_CONFIG_DIR": override, "XDG_CONFIG_HOME": xdg}
            with patch.dict(os.environ, env):
                self.assertEqual(config_dir_get(), override)
