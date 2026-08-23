"""Unit tests for the resembl config module."""

import os
import sys
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

    def test_load_unknown_format_falls_back_to_default(self):
        """A hand-edited out-of-enum ``format`` is ignored, keeping the default.

        Every renderer switches on exactly table/json/csv; an unknown stored
        value must not put commands into an undefined render branch.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write('format = "yaml"\ntop_n = 9\n')
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with self.assertLogs("resembl.config", level="WARNING"):
                    config = load_config()
        self.assertEqual(config.format, "table")
        self.assertEqual(config.top_n, 9)  # unrelated keys still apply

    def test_load_unreadable_config(self):
        """An unreadable config file runs on defaults instead of crashing."""
        import stat

        if os.geteuid() == 0:  # root ignores directory/file write bits
            self.skipTest("running as root")
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("top_n = 9\n")
            os.chmod(config_path, 0)
            try:
                with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                    with self.assertLogs("resembl.config", level="ERROR"):
                        config = load_config()
            finally:
                os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)
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

    def test_update_without_locking_api_falls_back_to_unlocked(self):
        """A platform with neither fcntl nor msvcrt must still update config.

        The lock is best-effort: on a runtime offering neither locking API
        (some non-CPython builds), the historical unlocked behavior applies
        instead of every config write crashing with ImportError.  A ``None``
        entry in ``sys.modules`` makes the import raise ImportError, which
        simulates both modules being absent.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with patch.dict(sys.modules, {"fcntl": None, "msvcrt": None}):
                    config = update_config("top_n", 11)
                    self.assertEqual(load_config().get("top_n"), 11)
        self.assertEqual(config.get("top_n"), 11)

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

    def test_concurrent_update_config_keeps_every_change(self):
        """Concurrent read-modify-write cycles must not drop each other's key.

        ``update_config`` reads the whole file, mutates the dict, and writes
        it back.  Without the cross-process file lock, two overlapping
        writers both read the same starting state and the second write
        silently discarded the first one's change; every writer's key must
        survive when all of them run at once.  (flock conflicts are between
        open file descriptions, so same-process threads on separate fds
        exercise the same serialization real concurrent processes hit.)
        """
        import threading

        if sys.platform == "win32":
            self.skipTest("msvcrt region locks do not serialize same-process fds")

        writers = 16

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                barrier = threading.Barrier(writers)
                failures: list[Exception] = []

                def writer(i: int) -> None:
                    try:
                        barrier.wait(timeout=10)
                        update_config(f"key_{i}", i)
                    except Exception as exc:  # pragma: no cover - surfaced below
                        failures.append(exc)

                threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                self.assertEqual(failures, [])
                with open(os.path.join(temp_dir, "config.toml"), "rb") as f:
                    import tomllib

                    stored = tomllib.load(f)
        for i in range(writers):
            self.assertEqual(stored.get(f"key_{i}"), i, f"key_{i} was lost to a racing writer")

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
