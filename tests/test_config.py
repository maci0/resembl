"""Unit tests for the resembl config module."""

import os
import sys
import tempfile
import unittest
from typing import ClassVar
from unittest.mock import patch

from resembl.config import (
    DEFAULTS,
    VALUE_BOUNDS,
    ConfigError,
    ResemblConfig,
    load_config,
    remove_config_key,
    save_config,
    update_config,
    validate_value,
)
from resembl.database import create_db_engine
from resembl.paths import (
    DEFAULT_DB_URL,
    config_dir_get,
    config_path_get,
    db_url_get,
    log_level_get,
    serve_allow_unauthenticated_get,
)
from resembl.scoring import MAX_NUM_PERM


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

    def test_load_mistyped_values_are_coerced_or_rejected(self):
        """A hand-edited wrong-typed value must not crash every command.

        ``update`` coerces each value to its field's type: quoted numerics
        ("0.9") and legal cross-type spellings (7.0 for an int field) are
        accepted, while garbage ("oops") is warned about and skipped so the
        field keeps its default.  Unvalidated, a string ``lsh_threshold``
        crashed every ``find`` with a TypeError at the first comparison.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write('lsh_threshold = "0.9"\ntop_n = 7.0\nngram_size = "oops"\n')
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with self.assertLogs("resembl.config", level="WARNING"):
                    config = load_config()
        self.assertEqual(config.lsh_threshold, 0.9)
        self.assertEqual(config.top_n, 7)
        self.assertEqual(config.ngram_size, DEFAULTS["ngram_size"])

    def test_load_non_finite_values_are_rejected(self):
        """TOML's ``nan`` / ``inf`` spellings must not poison numeric fields.

        An int field coerced from ``inf`` raised OverflowError, which the
        coercion guard did not catch — every command crashed with a
        traceback until the file was hand-fixed.  A NaN weight reaching
        scoring would make every similarity score NaN.  Both are warned
        about and skipped, so the field keeps its default.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("top_n = inf\njaccard_weight = nan\nlsh_threshold = 0.8\n")
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with self.assertLogs("resembl.config", level="WARNING"):
                    config = load_config()
        self.assertEqual(config.top_n, DEFAULTS["top_n"])
        self.assertEqual(config.jaccard_weight, DEFAULTS["jaccard_weight"])
        self.assertEqual(config.lsh_threshold, 0.8)  # unrelated keys still apply

    def test_update_rejects_non_finite_floats(self):
        """update() skips non-finite float values instead of applying them."""
        cfg = ResemblConfig()
        with self.assertLogs("resembl.config", level="WARNING"):
            cfg.update({"jaccard_weight": float("nan"), "lsh_threshold": float("inf"), "top_n": 9})
        self.assertEqual(cfg.jaccard_weight, DEFAULTS["jaccard_weight"])
        self.assertEqual(cfg.lsh_threshold, DEFAULTS["lsh_threshold"])
        self.assertEqual(cfg.top_n, 9)

    def test_load_unreadable_config(self):
        """An unreadable config file runs on defaults instead of crashing."""
        import stat

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("top_n = 9\n")
            os.chmod(config_path, 0)
            try:
                # Capability probe, not an OS or uid check: root and Windows
                # both keep the file readable after a mode 0 chmod (Windows
                # has no mode bits, only a read-only attribute), and the
                # test's premise then does not hold.
                try:
                    with open(config_path, encoding="utf-8"):
                        readable = True
                except OSError:
                    readable = False
                if readable:
                    self.skipTest("filesystem does not enforce read permissions")
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
        self.assertEqual(config.top_n, 10)
        # Ensure other defaults are preserved
        self.assertEqual(config.lsh_threshold, DEFAULTS.get("lsh_threshold"))

    def test_remove_config_key(self):
        """Test that remove_config_key correctly removes a key."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                update_config("top_n", 10)
                remove_config_key("top_n")
                config = load_config()
        self.assertEqual(config.top_n, DEFAULTS.get("top_n"))

    def test_update_with_malformed_config(self):
        """update_config fails closed on a malformed file, leaving it untouched.

        A read-modify-write update that treats a malformed file as empty
        overwrites it and silently discards every existing override, so the
        update must raise (an ``OSError`` subclass) and leave the bytes
        alone instead of reporting success.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("this is not valid toml")
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with self.assertRaises(ConfigError):
                    update_config("top_n", 10)
            with open(config_path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "this is not valid toml")

    def test_remove_with_malformed_config(self):
        """remove_config_key fails closed on a malformed file, leaving it untouched."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("this is not valid toml")
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with self.assertRaises(ConfigError):
                    remove_config_key("top_n")
            with open(config_path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "this is not valid toml")

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
        self.assertEqual(config.top_n, 10)
        self.assertEqual(config.lsh_threshold, DEFAULTS.get("lsh_threshold"))

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
                    self.assertEqual(load_config().top_n, 11)
        self.assertEqual(config["top_n"], 11)

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

    def test_save_config_interrupt_cleans_temp_file(self):
        """A Ctrl-C mid-save must not leave the half-written temp file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with patch("tomli_w.dump", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        save_config({"top_n": 10})
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
        silently discarded the first one's change; each writer's distinct
        ``top_n`` value must be the last one the file holds, on its own
        key's behalf.  (flock conflicts are between open file descriptions,
        so same-process threads on separate fds exercise the same
        serialization real concurrent processes hit.)
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
                        update_config("top_n", 10 + i)
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
        self.assertIn(stored.get("top_n"), range(10, 10 + writers))

    def test_config_file_lock_times_out_instead_of_hanging(self):
        """A held config lock must time out, not hang forever.

        Regression: ``_config_file_lock`` used an unbounded ``flock``; a
        crashed holder or a wedged peer would hang a CLI update forever.
        The lock now polls with a 30s deadline and raises ``TimeoutError``.
        """
        from resembl.config import _config_file_lock

        if sys.platform == "win32":
            self.skipTest("msvcrt region locks do not serialize same-process fds")

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                import fcntl

                lock_path = config_path_get() + ".lock"
                os.makedirs(temp_dir, exist_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    # Fake a clock that jumps past the 30s deadline on the
                    # second poll so the test does not wait 30s.
                    seq = iter([0.0, 31.0])
                    with patch("time.monotonic", side_effect=lambda: next(seq, 31.0)):
                        with self.assertRaises(TimeoutError):
                            with _config_file_lock():
                                pass
                finally:
                    os.close(fd)

    def test_config_file_lock_times_out_under_pin(self):
        """A contended lock under ``RESEMBL_NOW`` must time out, not spin.

        The pinned monotonic clock never advances, so a started+30
        deadline would never fire and a contended pinned holder would
        busy-spin forever (``_retry_sleep`` is a no-op under a pin).
        Under a pin the deadline is the start itself, so the first
        contention raises ``TimeoutError`` deterministically.
        """
        from resembl.config import _config_file_lock

        if sys.platform == "win32":
            self.skipTest("msvcrt region locks do not serialize same-process fds")

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "RESEMBL_CONFIG_DIR": temp_dir,
                    "RESEMBL_NOW": "2024-06-01T12:00:00+00:00",
                },
            ):
                import fcntl

                lock_path = config_path_get() + ".lock"
                os.makedirs(temp_dir, exist_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    with self.assertRaises(TimeoutError):
                        with _config_file_lock():
                            pass
                finally:
                    os.close(fd)

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

    def test_config_file_lock_times_out_under_pinned_clock(self):
        """Held config lock still times out with ``RESEMBL_NOW`` set.

        Regression: ``monotonic_now`` pins to 0 under the pin, so a
        ``+30s`` deadline never fires and a contended pinned holder spins
        forever instead of raising ``TimeoutError``.  The worker is a
        daemon thread joined with a timeout so a regression fails here
        instead of hanging the suite.
        """
        from resembl.config import _config_file_lock

        if sys.platform == "win32":
            self.skipTest("msvcrt region locks do not serialize same-process fds")

        import fcntl
        import threading

        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "RESEMBL_CONFIG_DIR": temp_dir,
                "RESEMBL_NOW": "2024-06-01T12:00:00+00:00",
            }
            with patch.dict(os.environ, env):
                lock_path = config_path_get() + ".lock"
                os.makedirs(temp_dir, exist_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    outcome: list[str] = []

                    def attempt() -> None:
                        try:
                            with _config_file_lock():
                                outcome.append("acquired")
                        except TimeoutError:
                            outcome.append("timeout")

                    worker = threading.Thread(target=attempt, daemon=True)
                    worker.start()
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive(), "pinned lock never timed out")
                    self.assertEqual(outcome, ["timeout"])
                finally:
                    os.close(fd)

    def test_config_dir_override_beats_xdg(self):
        """RESEMBL_CONFIG_DIR takes precedence over $XDG_CONFIG_HOME."""
        with tempfile.TemporaryDirectory() as override, tempfile.TemporaryDirectory() as xdg:
            env = {"RESEMBL_CONFIG_DIR": override, "XDG_CONFIG_HOME": xdg}
            with patch.dict(os.environ, env):
                self.assertEqual(config_dir_get(), override)

    def test_load_unknown_key_warns_and_is_ignored(self):
        """A misspelled config key is warned about, not silently skipped."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("top_nn = 9\ntop_n = 7\n")
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with self.assertLogs("resembl.config", level="WARNING") as logs:
                    config = load_config()
            self.assertTrue(any("top_nn" in line for line in logs.output))
            self.assertEqual(config.top_n, 7)

    def test_update_config_rejects_unknown_and_out_of_range(self):
        """Programmatic updates validate like the CLI does."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                with self.assertRaises(ValueError):
                    update_config("bogus_key", 1)
                with self.assertRaises(ValueError):
                    update_config("top_n", -5)
                with self.assertRaises(ValueError):
                    remove_config_key("bogus_key")
                self.assertEqual(os.listdir(temp_dir), [])


class TestConfigValueRanges(unittest.TestCase):
    """A value inside its field's type but outside its range is a fault.

    Type coercion alone let ``ngram_size = 0`` through, and the reindex it
    drives produces degenerate fingerprints (every shingle collapses to the
    empty token, so every snippet matches every other) with no error at any
    point.  ``lsh_threshold = 5.0`` and ``top_n = -1`` fail at use instead.
    """

    #: (key, value) pairs the range check must refuse.
    OUT_OF_RANGE: ClassVar[list[tuple[str, float]]] = [
        ("lsh_threshold", 5.0),
        ("lsh_threshold", -0.1),
        ("lsh_threshold", 0.99),
        ("num_permutations", 1),
        ("num_permutations", MAX_NUM_PERM + 1),
        ("top_n", 0),
        ("top_n", -3),
        ("ngram_size", 0),
        ("ngram_size", 1000000),
        ("jaccard_weight", 1.5),
        ("jaccard_weight", -0.5),
    ]

    def test_validate_value_refuses_out_of_range(self):
        """Each out-of-range value comes back with a reason, not applied."""
        for key, value in self.OUT_OF_RANGE:
            with self.subTest(key=key, value=value):
                reason = validate_value(key, value)
                self.assertIsNotNone(reason)
                assert reason is not None
                # The reason names the bound that was crossed, so a range
                # table that silently moves a bound cannot pass with a
                # generic "invalid" message.
                bound = VALUE_BOUNDS[key]
                expected = (
                    f"at least {bound.low:g}"
                    if value < bound.low
                    else f"{'at most' if bound.high_inclusive else 'below'} {bound.high:g}"
                )
                self.assertIn(expected, reason)

    def test_validate_value_accepts_defaults(self):
        """The shipped defaults are inside their own ranges."""
        for key, value in DEFAULTS.items():
            with self.subTest(key=key):
                self.assertIsNone(validate_value(key, value))

    def test_load_config_keeps_out_of_range_values_out(self):
        """A hand-edited out-of-range value is warned about and ignored."""
        for key, value in self.OUT_OF_RANGE:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temp_dir:
                config_path = os.path.join(temp_dir, "config.toml")
                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(f"{key} = {value!r}\n")
                with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": temp_dir}):
                    with self.assertLogs("resembl.config", level="WARNING"):
                        config = load_config()
                self.assertEqual(getattr(config, key), DEFAULTS[key])

    def test_update_keeps_out_of_range_values_out(self):
        """update() refuses a bad range instead of applying it."""
        cfg = ResemblConfig()
        with self.assertLogs("resembl.config", level="WARNING"):
            cfg.update({"ngram_size": 0, "top_n": -1, "jaccard_weight": 2.0, "lsh_threshold": 0.8})
        self.assertEqual(cfg.ngram_size, DEFAULTS["ngram_size"])
        self.assertEqual(cfg.top_n, DEFAULTS["top_n"])
        self.assertEqual(cfg.jaccard_weight, DEFAULTS["jaccard_weight"])
        # An in-range value in the same update still applies.
        self.assertEqual(cfg.lsh_threshold, 0.8)

    def test_update_rejects_booleans_and_boolean_strings(self):
        """A bool or "true"/"false" string must not become 1/0.

        ``int(True)`` silently turned a hand-edited ``top_n = true`` into
        1, so the run searched with a threshold the user never typed
        instead of keeping its default.
        """
        cfg = ResemblConfig()
        with self.assertLogs("resembl.config", level="WARNING"):
            cfg.update({"top_n": True, "jaccard_weight": "true", "lsh_threshold": "false"})
        self.assertEqual(cfg.top_n, DEFAULTS["top_n"])
        self.assertEqual(cfg.jaccard_weight, DEFAULTS["jaccard_weight"])
        self.assertEqual(cfg.lsh_threshold, DEFAULTS["lsh_threshold"])

    def test_validate_value_rejects_unknown_key_and_wrong_type(self):
        """An unknown key or a cross-type value reports a reason, not None."""
        self.assertEqual(validate_value("bogus_key", 1), "unknown configuration key: 'bogus_key'")
        for bad in (1.5, "5", True):
            with self.subTest(value=bad):
                self.assertEqual(validate_value("top_n", bad), "expected a int")
        self.assertIsNone(validate_value("top_n", 5))
        self.assertIsNone(validate_value("format", "json"))
        self.assertEqual(validate_value("format", "yaml"), "expected one of: table, json, csv")


class TestDatabaseUrlEnv(unittest.TestCase):
    """Which environment variable names the database, and in what order."""

    def test_namespaced_name_wins(self):
        """RESEMBL_DATABASE_URL beats the unprefixed DATABASE_URL."""
        env = {
            "RESEMBL_DATABASE_URL": "sqlite:///namespaced.db",
            "DATABASE_URL": "sqlite:///generic.db",
        }
        with patch.dict(os.environ, env):
            self.assertEqual(db_url_get(), "sqlite:///namespaced.db")

    def test_generic_name_still_works(self):
        """The unprefixed name is honored when the namespaced one is unset."""
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///generic.db"}, clear=False):
            os.environ.pop("RESEMBL_DATABASE_URL", None)
            self.assertEqual(db_url_get(), "sqlite:///generic.db")

    def test_empty_values_fall_through_to_the_default(self):
        """An exported-but-empty variable means unset, not "use the empty URL"."""
        with patch.dict(
            os.environ,
            {"RESEMBL_DATABASE_URL": "", "DATABASE_URL": ""},
            clear=False,
        ):
            self.assertEqual(db_url_get(), DEFAULT_DB_URL)

    def test_padded_url_is_trimmed(self):
        """Surrounding whitespace is not part of the URL the engine parses.

        A URL exported as ``" sqlite:///x.db "`` reached SQLAlchemy verbatim
        and failed with an obscure driver error instead of opening the
        database the operator named, so the whitespace is stripped the way
        the blank check already treated it.
        """
        with patch.dict(os.environ, {"RESEMBL_DATABASE_URL": " sqlite:///x.db "}):
            self.assertEqual(db_url_get(), "sqlite:///x.db")

    def test_default_when_unset(self):
        """With neither variable set the URL is the local assembly.db."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(db_url_get(), DEFAULT_DB_URL)

    def test_read_at_call_time_not_import_time(self):
        """A URL exported after import is the one the engine is built from.

        The URL used to be captured in a module constant at import time, so
        an embedder (or a test) that set the variable afterwards silently got
        whatever the environment held when resembl was first imported.
        """
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(db_url_get(), DEFAULT_DB_URL)
            os.environ["RESEMBL_DATABASE_URL"] = "sqlite:///late.db"
            try:
                self.assertEqual(db_url_get(), "sqlite:///late.db")
                engine = create_db_engine()
                self.assertEqual(str(engine.url), "sqlite:///late.db")
            finally:
                os.environ.pop("RESEMBL_DATABASE_URL", None)


class TestConfigDirPlatformSpelling(unittest.TestCase):
    """The ~/.config default is returned with the platform separator."""

    def test_default_is_platform_spelling(self):
        """expanduser keeps '/' verbatim, so normalize before returning.

        On Windows the raw expanduser result mixes separators
        (``C:\\Users\\u/.config\\resembl``); normalizing keeps the returned
        path string-equal to an ``os.path.join`` expectation on every
        platform.
        """
        with tempfile.TemporaryDirectory() as home:
            env = {
                "HOME": home,
                "USERPROFILE": home,
                "XDG_CONFIG_HOME": "",
                "RESEMBL_CONFIG_DIR": "",
            }
            with patch.dict(os.environ, env):
                result = config_dir_get()
            expected = os.path.join(home, ".config", "resembl")
            self.assertEqual(result, expected)
            if os.sep == "\\":
                self.assertNotIn("/", result)


class TestServeAndLogEnv(unittest.TestCase):
    """RESEMBL_SERVE_ALLOW_UNAUTHENTICATED and RESEMBL_LOG_LEVEL."""

    def test_serve_allow_unset_is_false(self):
        """Unset means no opt-in."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(serve_allow_unauthenticated_get())

    def test_serve_allow_blank_counts_as_unset(self):
        """A blank value counts as unset, like every other override."""
        with patch.dict(os.environ, {"RESEMBL_SERVE_ALLOW_UNAUTHENTICATED": "  "}):
            self.assertFalse(serve_allow_unauthenticated_get())

    def test_serve_allow_nonblank_opts_in(self):
        """Any non-blank value opts in."""
        with patch.dict(os.environ, {"RESEMBL_SERVE_ALLOW_UNAUTHENTICATED": "1"}):
            self.assertTrue(serve_allow_unauthenticated_get())

    def test_log_level_unset_is_none(self):
        """Unset means no override."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(log_level_get())

    def test_log_level_blank_counts_as_unset(self):
        """Blank counts as unset."""
        with patch.dict(os.environ, {"RESEMBL_LOG_LEVEL": ""}):
            self.assertIsNone(log_level_get())

    def test_log_level_is_uppercased(self):
        """The name is upper-cased for the caller to validate."""
        with patch.dict(os.environ, {"RESEMBL_LOG_LEVEL": "debug"}):
            self.assertEqual(log_level_get(), "DEBUG")

    def test_log_level_dotless_i_is_not_info(self):
        """ASCII-only fold: dotless \u0131 must not validate as INFO."""
        with patch.dict(os.environ, {"RESEMBL_LOG_LEVEL": "\u0131nfo"}):
            level = log_level_get()
            self.assertEqual(level, "\u0131NFO")
            self.assertNotIn(level, ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
