"""Integration tests for the resembl CLI."""

# pylint: disable=protected-access  # tests exercise private internals
# pylint: disable=consider-using-with  # a test keeps a temp file, a temp
# directory or a child process open for the whole test body on purpose

import json
import os
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlmodel import Session, SQLModel, create_engine, select

from resembl import cli
from resembl.cli import _format_created_at, _zone_get
from resembl.core import snippet_add
from resembl.models import Snippet


class BaseCLITest(unittest.TestCase):
    """Base class for CLI tests with common setup and helper methods."""

    def setUp(self):
        """Set up a clean database for each test."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_name = os.path.join(tmp.name, "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_name}")
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            snippet_add(session, "test_snippet", "MOV EAX, 1")

    def tearDown(self):
        """Clean up the database after each test."""
        self.engine.dispose()

    def run_command(self, command, input_data=None, extra_env=None):
        """Helper function to run a command and return the output."""
        env = {
            **os.environ,
            "PYTHONPATH": os.path.join(os.getcwd(), "."),
            "DATABASE_URL": f"sqlite:///{self.db_name}",
        }
        if extra_env:
            env.update(extra_env)

        # ``encoding="utf-8"``: the CLI puts its own stdout and stderr into
        # UTF-8 on every platform (resembl.paths.console_utf8_reconfigure), so
        # its bytes are UTF-8 whatever the host locale says.  Letting
        # subprocess decode them with the locale (cp1252 on the Windows
        # runners) raised in its reader thread, which leaves ``stdout`` as
        # None instead of failing in the test that asked for it.
        #
        # The backslashes are doubled before the split for the same reason a
        # Windows path reaches this helper at all: POSIX shlex reads ``\`` as
        # an escape, so ``export --force C:\out`` arrived at the CLI as
        # ``C:out`` and the export landed somewhere else.  No command here
        # carries a backslash the CLI is meant to receive as an escape, and
        # on Linux and macOS there are none to double.
        return subprocess.run(
            [sys.executable, "-m", "resembl.cli", *shlex.split(command.replace("\\", "\\\\"))],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=input_data,
            env=env,
            check=False,
        )


class TestCLICommands(BaseCLITest):
    """Tests for core CLI commands like stats, find, add, etc."""

    def test_help_message(self):
        """Test that the --help message is displayed correctly."""
        result = self.run_command("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage", result.stdout)

    def test_stats_command(self):
        """Test the stats command."""
        result = self.run_command("stats")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Database Statistics", result.stdout)
        self.assertIn("1", result.stdout)

    def test_find_command(self):
        """Test the find command."""
        result = self.run_command("find --query 'MOV EAX, 1'")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Top Matches", result.stdout)
        self.assertIn("test_snippet", result.stdout)

    def test_find_rejects_unbuildable_threshold(self):
        """A threshold leaving <2 bands fails cleanly, not with 0 results.

        Thresholds in [0.981, 0.99) pass the range check but give b=1, which
        used to make the index build fail and find return zero matches
        silently.
        """
        result = self.run_command("find --query 'MOV EAX, 1' --threshold 0.985")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("too high", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        # find-batch gets the same guard.
        queries_file = tempfile.mktemp(suffix=".txt")
        with open(queries_file, "w", encoding="utf-8") as f:
            f.write("MOV EAX, 1\n")
        try:
            batch = self.run_command(f"find-batch --file {queries_file} --threshold 0.985")
        finally:
            os.remove(queries_file)
        self.assertNotEqual(batch.returncode, 0)
        self.assertIn("too high", batch.stderr)

    def test_find_command_with_stdin(self):
        """Test the find command with stdin."""
        result = self.run_command("find --file -", input_data="MOV EAX, 1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Top Matches", result.stdout)
        self.assertIn("test_snippet", result.stdout)

    def test_find_invalid_threshold(self):
        """Invalid threshold values should return an error."""
        result = self.run_command("find --threshold 2.0 --query 'x'")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("threshold", result.stderr)

    def test_find_rejects_invalid_config_num_permutations(self):
        """An out-of-range num_permutations from config never reaches the find.

        The value used to travel all the way into snippet_find_matches,
        which runs the full auto-reindex side effect first and only then
        crashed with a raw datasketch ValueError traceback.  It is now
        refused where it is read (config.validate_value): the command runs
        on the default permutation count and says which value it ignored.
        """
        with tempfile.TemporaryDirectory() as cfgdir:
            config_path = os.path.join(cfgdir, "config.toml")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("num_permutations = 1\n")
            env = {"RESEMBL_CONFIG_DIR": cfgdir}
            result = self.run_command("find --query 'MOV EAX, 1'", extra_env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("num_permutations", result.stderr)
            self.assertIn("test_snippet", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

            queries_file = os.path.join(cfgdir, "queries.txt")
            with open(queries_file, "w", encoding="utf-8") as f:
                f.write("MOV EAX, 1\n")
            batch = self.run_command(f"find-batch --file {queries_file}", extra_env=env)
            self.assertEqual(batch.returncode, 0, batch.stderr)
            self.assertIn("num_permutations", batch.stderr)
            self.assertNotIn("Traceback", batch.stderr)

    def test_compare_missing_snippet(self):
        """Comparing unknown checksums should fail."""
        result = self.run_command("compare deadbeef cafebabe")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No snippet found matching", result.stderr)

    def test_add_command(self):
        """Test the add command."""
        result = self.run_command("add new_snippet 'MOV EBX, 2'")
        self.assertEqual(result.returncode, 0)
        self.assertIn("now has names", result.stdout)

        with Session(self.engine) as session:
            snippet = Snippet.get_by_name(session, "new_snippet")
            self.assertIsNotNone(snippet)

    def test_reindex_command(self):
        """`reindex` should run when confirmation is provided."""
        result = self.run_command("reindex", input_data="y\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Re-indexing Complete", result.stdout)

    def test_delete_by_checksum_with_confirmation(self):
        """Removing a snippet by checksum after confirmation should update the database."""
        with Session(self.engine) as session:
            snippet = Snippet.get_by_name(session, "test_snippet")
            self.assertIsNotNone(snippet)
            checksum = snippet.checksum

        rm_result = self.run_command(f"rm {checksum}", input_data="y\n")
        self.assertEqual(rm_result.returncode, 0)

        with Session(self.engine) as session:
            snippet = Snippet.get_by_checksum(session, checksum)
            self.assertIsNone(snippet)

    def test_export_command(self):
        """Test the export command."""
        with tempfile.TemporaryDirectory() as export_dir:
            result = self.run_command(f"export {export_dir}", input_data="y\n")
            self.assertEqual(result.returncode, 0)
            self.assertIn("Export Complete", result.stdout)

            exported_file = os.path.join(export_dir, "test_snippet.asm")
            self.assertTrue(os.path.exists(exported_file))

            with open(exported_file, encoding="utf-8") as f:
                content = f.read()
                self.assertEqual(content, "MOV EAX, 1")

    def test_export_sanitizes_portability_unsafe_names(self):
        """Names illegal on Windows filesystems must still export everywhere.

        Reserved device stems (con, aux), reserved characters (':'), and
        trailing dots would crash or misbehave on Windows; the sanitizer
        must produce a writable file for each snippet.
        """
        with Session(self.engine) as session:
            for name, code in (
                ("con", "MOV AL, 1"),
                ("aux", "MOV BL, 2"),
                ("my:name", "MOV CL, 3"),
                ("trailing.", "MOV DL, 4"),
            ):
                self.assertIsNotNone(snippet_add(session, name, code))

        with tempfile.TemporaryDirectory() as export_dir:
            result = self.run_command(f"export --force {export_dir}")
            self.assertEqual(result.returncode, 0)

            exported = sorted(f for f in os.listdir(export_dir) if f.endswith(".asm"))
            self.assertEqual(len(exported), 5)  # 4 here + setUp's test_snippet
            for fname in exported:
                stem = fname[: -len(".asm")]
                self.assertNotIn(stem.lower(), ("con", "aux"))
                self.assertFalse(fname.endswith("."), fname)

    def test_export_case_insensitive_name_collision(self):
        """Names differing only by case must not silently overwrite.

        macOS and Windows filesystems are case-insensitive: exporting two
        snippets named 'Memcpy' and 'memcpy' used to write one file.
        """
        with Session(self.engine) as session:
            self.assertIsNotNone(snippet_add(session, "Memcpy", "MOV AX, 1"))
            self.assertIsNotNone(snippet_add(session, "memcpy", "MOV BX, 2"))

        with tempfile.TemporaryDirectory() as export_dir:
            result = self.run_command(f"export --force {export_dir}")
            self.assertEqual(result.returncode, 0)

            contents = {
                fname: open(os.path.join(export_dir, fname), encoding="utf-8").read()
                for fname in os.listdir(export_dir)
                if fname.endswith(".asm")  # the export manifest is not a snippet
            }
            self.assertEqual(len(contents), 3)  # 2 here + setUp's test_snippet
            # No two exported names may fold together: 'Memcpy.asm' and
            # 'memcpy.asm' are one file on macOS and Windows, so the export
            # has to disambiguate on a case-sensitive volume too, and this is
            # the assertion a Linux-only run can see that with.
            self.assertEqual(len({fname.casefold() for fname in contents}), 3)
            self.assertIn("MOV AX, 1", contents.values())
            self.assertIn("MOV BX, 2", contents.values())

    def test_name_add_and_remove(self):
        """Test the name add and remove commands."""
        with Session(self.engine) as session:
            snippet = Snippet.get_by_name(session, "test_snippet")
            self.assertIsNotNone(snippet)
            checksum = snippet.checksum

        result = self.run_command(f"name add {checksum} new_name")
        self.assertEqual(result.returncode, 0)

        with Session(self.engine) as session:
            snippet = Snippet.get_by_checksum(session, checksum)
            self.assertIn("new_name", snippet.name_list)

        result = self.run_command(f"name remove {checksum} new_name")
        self.assertEqual(result.returncode, 0)

        with Session(self.engine) as session:
            snippet = Snippet.get_by_checksum(session, checksum)
            self.assertNotIn("new_name", snippet.name_list)

    def test_failed_mutations_exit_nonzero_in_every_mode(self):
        """A failed name/tag mutation exits 1 regardless of --quiet/--format.

        The exit code used to be swallowed in quiet mode and JSON/CSV mode,
        so scripts running `--quiet tag add ...` saw success after a no-op.
        Output may be suppressed; the failure signal may not.
        """
        cases = (
            "name add deadbeef x",
            "name remove deadbeef x",
            "tag add deadbeef x",
            "tag remove deadbeef x",
        )
        for command in cases:
            for flag in ("--quiet", "--format json", "--format csv"):
                result = self.run_command(f"{flag} {command}")
                self.assertNotEqual(result.returncode, 0, f"'{flag} {command}' reported success")

    def test_rm_with_force_flag(self):
        """Removing with --force should skip confirmation."""
        with Session(self.engine) as session:
            snippet = Snippet.get_by_name(session, "test_snippet")
            self.assertIsNotNone(snippet)
            checksum = snippet.checksum

        rm_result = self.run_command(f"rm --force {checksum}")
        self.assertEqual(rm_result.returncode, 0)

        with Session(self.engine) as session:
            snippet = Snippet.get_by_checksum(session, checksum)
            self.assertIsNone(snippet)


class TestCLIConfig(BaseCLITest):
    """Tests for the `config` subcommand and environment variable overrides."""

    def test_config_set_preserves_existing_settings(self):
        """`config set` should update a single key without losing others."""
        with tempfile.TemporaryDirectory() as home:
            # Empty XDG_CONFIG_HOME pins the documented ~/.config default
            # branch even on hosts exporting an XDG base dir.  HOME and
            # USERPROFILE both point at the temp home because expanduser reads
            # the first on POSIX and the second on Windows: with only HOME the
            # child wrote into the runner's real home, so this test read a file
            # that did not exist and left a config behind for later tests.
            env = {"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": ""}
            self.run_command("config set lsh_threshold 0.7", extra_env=env)
            self.run_command("config set top_n 10", extra_env=env)

            config_path = os.path.join(home, ".config", "resembl", "config.toml")
            with open(config_path, "rb") as f:
                data = tomllib.load(f)

            self.assertEqual(data.get("lsh_threshold"), 0.7)
            self.assertEqual(data.get("top_n"), 10)

    def test_config_dir_env_override(self):
        """RESEMBL_CONFIG_DIR should override the default config path."""
        with tempfile.TemporaryDirectory() as cfgdir:
            self.run_command("config set top_n 7", extra_env={"RESEMBL_CONFIG_DIR": cfgdir})
            config_path = os.path.join(cfgdir, "config.toml")
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            self.assertEqual(data.get("top_n"), 7)

            result = self.run_command("config path", extra_env={"RESEMBL_CONFIG_DIR": cfgdir})
            self.assertIn(config_path, result.stdout)

    def test_cache_dir_env_override(self):
        """RESEMBL_CACHE_DIR should control where legacy cache files are stored.

        The LSH index itself lives in the database; the env var applies to the
        legacy pickle cache files.
        """
        with tempfile.TemporaryDirectory() as cache_dir:
            self.run_command(
                "find --query 'MOV EAX, 1'",
                extra_env={"RESEMBL_CACHE_DIR": cache_dir},
            )
            # The index is database-backed: no pickle file should be created.
            cache_file = os.path.join(cache_dir, "lsh_0.50.pkl")
            self.assertFalse(os.path.exists(cache_file))

    def test_config_set_and_list(self):
        """`config list` should show values set via `config set`."""
        with tempfile.TemporaryDirectory() as home:
            # Empty XDG_CONFIG_HOME pins the documented ~/.config default
            # branch even on hosts exporting an XDG base dir.  HOME and
            # USERPROFILE both point at the temp home because expanduser reads
            # the first on POSIX and the second on Windows: with only HOME the
            # child wrote into the runner's real home, so this test read a file
            # that did not exist and left a config behind for later tests.
            env = {"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": ""}
            self.run_command("config set lsh_threshold 0.6", extra_env=env)
            list_result = self.run_command("config list", extra_env=env)
            self.assertIn("0.6", list_result.stdout)
            self.assertIn("lsh_threshold", list_result.stdout)
            self.assertIn("top_n", list_result.stdout)

    def test_config_set_invalid_key(self):
        """`config set` should reject invalid keys."""
        result = self.run_command("config set invalid_key 123")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid configuration key", result.stderr)

    def test_config_get_invalid_key(self):
        """`config get` on an unknown key fails instead of printing `None`.

        Printing the string "None" with exit 0 made a typo'd key look like a
        stored value to any script reading the output.
        """
        result = self.run_command("config get not_a_key")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid configuration key", result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_config_set_invalid_value(self):
        """`config set` should reject values that don't fit the key's type."""
        result = self.run_command("config set top_n abc")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid value", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_config_set_rejects_non_finite_float(self):
        """`config set` must reject nan/inf instead of persisting them.

        "nan" coerces to a float cleanly; persisted, it made every find
        score NaN (and emit invalid JSON) until the value was removed by
        hand.
        """
        with tempfile.TemporaryDirectory() as cfgdir:
            env = {"RESEMBL_CONFIG_DIR": cfgdir}
            for bad in ("nan", "inf"):
                result = self.run_command(f"config set jaccard_weight {bad}", extra_env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("finite", result.stderr)
            config_path = os.path.join(cfgdir, "config.toml")
            self.assertFalse(os.path.exists(config_path))

    def test_config_set_rejects_out_of_range_value(self):
        """`config set` must reject a value outside the range the code works in.

        Type coercion alone let ``ngram_size 0`` through: the reindex it
        drives builds degenerate fingerprints (every snippet matches every
        other) and nothing reports an error at any later point.
        """
        with tempfile.TemporaryDirectory() as cfgdir:
            env = {"RESEMBL_CONFIG_DIR": cfgdir}
            for key, bad in (
                ("ngram_size", "0"),
                ("top_n", "0"),
                ("lsh_threshold", "5.0"),
                ("jaccard_weight", "2"),
            ):
                result = self.run_command(f"config set {key} {bad}", extra_env=env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f"Invalid value for '{key}'", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(os.path.exists(os.path.join(cfgdir, "config.toml")))

    def test_config_set_invalid_format(self):
        """`config set format` should reject values outside the render enum."""
        result = self.run_command("config set format yaml")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid value for 'format'", result.stderr)
        self.assertIn("table, json, csv", result.stderr)

    def test_config_get(self):
        """`config get` should retrieve a specific value."""
        with tempfile.TemporaryDirectory() as home:
            # Empty XDG_CONFIG_HOME pins the documented ~/.config default
            # branch even on hosts exporting an XDG base dir.  HOME and
            # USERPROFILE both point at the temp home because expanduser reads
            # the first on POSIX and the second on Windows: with only HOME the
            # child wrote into the runner's real home, so this test read a file
            # that did not exist and left a config behind for later tests.
            env = {"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": ""}
            self.run_command("config set top_n 15", extra_env=env)
            result = self.run_command("config get top_n", extra_env=env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "15")

    def test_config_get_nonexistent_key(self):
        """`config get` on a nonexistent key should show the default."""
        with tempfile.TemporaryDirectory() as home:
            # Empty XDG_CONFIG_HOME pins the documented ~/.config default
            # branch even on hosts exporting an XDG base dir.  HOME and
            # USERPROFILE both point at the temp home because expanduser reads
            # the first on POSIX and the second on Windows: with only HOME the
            # child wrote into the runner's real home, so this test read a file
            # that did not exist and left a config behind for later tests.
            env = {"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": ""}
            result = self.run_command("config get top_n", extra_env=env)
            self.assertEqual(result.returncode, 0)
            self.assertIn("5", result.stdout)  # Default value

    def test_config_unset(self):
        """`config unset` should remove a key from the config file."""
        with tempfile.TemporaryDirectory() as home:
            # Empty XDG_CONFIG_HOME pins the documented ~/.config default
            # branch even on hosts exporting an XDG base dir.  HOME and
            # USERPROFILE both point at the temp home because expanduser reads
            # the first on POSIX and the second on Windows: with only HOME the
            # child wrote into the runner's real home, so this test read a file
            # that did not exist and left a config behind for later tests.
            env = {"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": ""}
            self.run_command("config set top_n 20", extra_env=env)
            result = self.run_command("config unset top_n", extra_env=env)
            self.assertEqual(result.returncode, 0)

            # Verify that the key is gone
            list_result = self.run_command("config list", extra_env=env)
            self.assertNotIn("20", list_result.stdout)
            self.assertIn("top_n", list_result.stdout)  # Shows default


class TestCLIOptions(BaseCLITest):
    """Tests for global command-line options like --quiet and --no-color."""

    def test_quiet_option(self):
        """Test that --quiet suppresses informational output."""
        result = self.run_command("--quiet stats")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_no_color_flag(self):
        """Test that the --no-color flag disables colored output."""
        with Session(self.engine) as session:
            snippet_add(session, "snippet2", "MOV EBX, 2")
            s1 = Snippet.get_by_name(session, "test_snippet")
            s2 = Snippet.get_by_name(session, "snippet2")

        result = self.run_command(f"--no-color compare {s1.checksum} {s2.checksum}")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("\033[1m", result.stdout)

    def test_version_flag_needs_no_subcommand(self):
        """`--version` answers on its own, before the database is opened."""
        result = self.run_command("--version", extra_env={"DATABASE_URL": "not-a-url"})
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout.strip(), r"^resembl \S+")

    def test_unknown_format_is_a_usage_error(self):
        """An unrenderable --format must not fall through to a table render."""
        result = self.run_command("--format yaml stats")
        self.assertEqual(result.returncode, 2)
        self.assertIn("table, json, csv", result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_invalid_range_is_a_usage_error(self):
        """A malformed --range reports the flag and exits 2, not 1."""
        result = self.run_command("list --range 5")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--range", result.stderr)

    def test_find_reads_a_piped_query(self):
        """A redirected stdin is the query when --query/--file are absent."""
        result = self.run_command("find", input_data="MOV EAX, 1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Top Matches", result.stdout)
        self.assertIn("test_snippet", result.stdout)

    def test_find_without_a_query_is_a_usage_error(self):
        """No query anywhere exits 2 (the command line is wrong, not the run)."""
        result = self.run_command("find", input_data="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("No query provided", result.stderr)

    def test_logs_stay_off_stdout(self):
        """A warning raised mid-run must not corrupt a machine-readable stream.

        The config loader warns about values it ignores; with the log on
        stdout that warning landed inside the JSON a script parses.
        """
        with tempfile.TemporaryDirectory() as cfgdir:
            with open(os.path.join(cfgdir, "config.toml"), "w", encoding="utf-8") as f:
                f.write('lsh_threshold = "nonsense"\n')
            env = {"RESEMBL_CONFIG_DIR": cfgdir}
            result = self.run_command("--format json stats", extra_env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("lsh_threshold", result.stderr)
        self.assertIn("num_snippets", json.loads(result.stdout))


class TestCLIImport(BaseCLITest):
    """Tests for the import command."""

    def test_import_nonexistent_directory_fails_loudly(self):
        """Importing from a missing directory errors instead of a silent no-op success."""
        result = self.run_command("import /nonexistent/resembl_dir --force")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Directory not found", result.stderr)

    def test_import_counts_only_new_snippets(self):
        """Re-importing the same files should report 0 new snippets."""
        with tempfile.TemporaryDirectory() as import_dir:
            file_path = os.path.join(import_dir, "existing.asm")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("PUSH EBP; MOV EBP, ESP; POP EBP; RET")

            # First import - should report 1 new snippet
            result = self.run_command(f"--format json import --force {import_dir}")
            self.assertEqual(result.returncode, 0)
            import json

            data = json.loads(result.stdout)
            self.assertEqual(data["num_imported"], 1)

            # Second import of the same file - should report 0 new snippets
            result = self.run_command(f"--format json import --force {import_dir}")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["num_imported"], 0)

    def test_import_jobs_zero_imports_sequentially(self):
        """`--jobs 0` means sequential in-process import, not "skip everything".

        A jobs count below 1 used to fall into the empty-directory branch and
        mark every readable file as skipped, importing nothing.
        """
        import json

        with tempfile.TemporaryDirectory() as import_dir:
            for i in range(3):
                file_path = os.path.join(import_dir, f"s{i}.asm")
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(f"MOV R{i}, {i}\nRET")

            result = self.run_command(f"--format json import --force --jobs 0 {import_dir}")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["num_imported"], 3)
            self.assertNotIn("skipped", data)

    def test_import_matches_extension_case_insensitively(self):
        """UPPERCASE extensions must import on every platform.

        The old glob patterns were folded by os.path.normcase on Windows
        but matched verbatim on Linux/macOS, so a directory of FOO.ASM
        files imported everything there and zero files here.
        """
        import json

        with tempfile.TemporaryDirectory() as import_dir:
            for fname, code in (
                ("UPPER.ASM", "MOV AX, 1; RET"),
                ("mixed.Txt", "MOV BX, 2; RET"),
            ):
                with open(os.path.join(import_dir, fname), "w", encoding="utf-8") as f:
                    f.write(code)

            result = self.run_command(f"--format json import --force --jobs 0 {import_dir}")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["num_imported"], 2)
            self.assertNotIn("skipped", data)

    def test_import_directory_without_snippets_fails_loudly(self):
        """A directory holding no .asm/.txt files is a wrong path, not a 0-snippet import."""
        with tempfile.TemporaryDirectory() as import_dir:
            with open(os.path.join(import_dir, "readme.md"), "w", encoding="utf-8") as f:
                f.write("not assembly")

            result = self.run_command(f"--format json import --force {import_dir}")
            self.assertEqual(result.returncode, 1)
            self.assertIn("No .asm or .txt files found", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_import_single_file(self):
        """A single .asm file is importable directly, not answered with 'Directory not found'."""
        import json

        with tempfile.TemporaryDirectory() as import_dir:
            file_path = os.path.join(import_dir, "one.asm")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("PUSH EBP; MOV EBP, ESP; POP EBP; RET")

            result = self.run_command(f"--format json import --force {file_path}")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["num_imported"], 1)

            listed = self.run_command("--format json list")
            names = [name for row in json.loads(listed.stdout) for name in row["names"]]
            self.assertIn("one", names)

    def test_list_reversed_range_fails_cleanly(self):
        """`--range start-end` with start > end errors instead of a
        backend-dependent negative LIMIT."""
        result = self.run_command("list --range 4-2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid --range", result.stderr)

    def test_unresolved_checksum_exits_nonzero(self):
        """Commands taking a checksum must fail loudly when it doesn't resolve.

        `version`, `collection add`, and `collection remove` printed the
        error but exited 0 (unlike show/rm/name/tag), so scripts saw success.
        """
        for command in (
            "version deadbeef00",
            "collection add mycoll deadbeef00",
            "collection remove deadbeef00",
        ):
            result = self.run_command(command)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertIn("No snippet found", result.stderr)


class TestCLIAddSnippet(BaseCLITest):
    """Tests focused on edge cases for the `add` command."""

    def test_add_snippet_with_no_name(self):
        """Test that a snippet can be added with no name."""
        self.run_command("add '' 'MOV ECX, 3'")
        with Session(self.engine) as session:
            snippet = Snippet.get_by_name(session, "")
            self.assertIsNotNone(snippet)

    def test_add_multiple_snippets_with_no_name(self):
        """Test that multiple snippets can be added with no name."""
        self.run_command("add '' 'MOV EDX, 4'")
        self.run_command("add '' 'MOV ESI, 5'")
        with Session(self.engine) as session:
            snippets = session.exec(select(Snippet).where(Snippet.names == '[""]')).all()
            self.assertEqual(len(snippets), 2)

    def test_add_multiple_snippets_with_same_name(self):
        """Test that multiple snippets can be added with the same name."""
        self.run_command("add same_name 'MOV EDI, 6'")
        self.run_command("add same_name 'MOV EBP, 7'")
        with Session(self.engine) as session:
            snippets = session.exec(select(Snippet).where(Snippet.names == '["same_name"]')).all()
            self.assertEqual(len(snippets), 2)


class TestFormatCreatedAt(unittest.TestCase):
    """Tests for local-zone rendering of stored UTC timestamps."""

    TOKYO = ZoneInfo("Asia/Tokyo")
    WARSAW = ZoneInfo("Europe/Warsaw")
    UTC = ZoneInfo("UTC")

    def test_utc_value_rendered_in_target_zone(self):
        """A stored UTC instant is shown at the viewer's wall-clock time."""
        self.assertEqual(
            _format_created_at("2024-06-01T15:00:00+00:00", "%Y-%m-%d %H:%M", self.TOKYO),
            "2024-06-02 00:00",
        )

    def test_date_shifts_across_zones(self):
        """The calendar date must come from the display zone, not the stored one.

        Slicing the raw string (the old behavior) shows 2024-06-01 even for a
        viewer in Tokyo, whose wall clock already reads June 2nd.
        """
        self.assertEqual(
            _format_created_at("2024-06-01T15:00:00+00:00", "%Y-%m-%d", self.UTC),
            "2024-06-01",
        )
        self.assertEqual(
            _format_created_at("2024-06-01T15:00:00+00:00", "%Y-%m-%d", self.TOKYO),
            "2024-06-02",
        )

    def test_naive_interpreted_as_utc(self):
        """Legacy naive values are treated as UTC, never as local time."""
        self.assertEqual(
            _format_created_at("2024-06-01T23:30:00", "%Y-%m-%dT%H:%M%z", self.TOKYO),
            "2024-06-02T08:30+0900",
        )

    def test_unparseable_shown_verbatim(self):
        """Garbage from a foreign database is displayed without crashing."""
        self.assertEqual(_format_created_at("not-a-date", "%Y-%m-%d", self.TOKYO), "not-a-date")

    def test_null_renders_empty(self):
        """A NULL created_at column has no instant to show, and must not crash."""
        self.assertEqual(_format_created_at(None, "%Y-%m-%d", self.TOKYO), "")

    def test_spring_forward_gap_is_skipped(self):
        """A UTC instant inside a zone's DST gap renders at the shifted wall time.

        2024-03-31 01:30Z is 03:30 in Warsaw: local 02:00-03:00 does not exist
        that day, so no correct conversion can place the instant there.
        """
        self.assertEqual(
            _format_created_at("2024-03-31T01:30:00+00:00", "%Y-%m-%d %H:%M %z", self.WARSAW),
            "2024-03-31 03:30 +0200",
        )

    def test_fall_back_repeated_hour_keeps_offset(self):
        """A UTC instant inside a zone's repeated hour carries that hour's offset.

        2024-10-27 00:30Z is 02:30 in Warsaw, the first of the two 02:30s
        (CEST, +0200), not the second (CET, +0100) an offset-free guess would
        produce.
        """
        self.assertEqual(
            _format_created_at("2024-10-27T00:30:00+00:00", "%Y-%m-%d %H:%M %z", self.WARSAW),
            "2024-10-27 02:30 +0200",
        )

    def test_default_zone_is_system_local(self):
        """Without an explicit zone the system local zone is used."""
        value = "2024-06-01T00:00:00+00:00"
        expected = datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M %z")
        self.assertEqual(_format_created_at(value, "%Y-%m-%d %H:%M %z"), expected)


class TestZoneGet(unittest.TestCase):
    """Tests for the ``--tz`` zone resolver."""

    def test_named_zone_resolves(self):
        self.assertEqual(_zone_get("Europe/Warsaw"), ZoneInfo("Europe/Warsaw"))

    def test_none_keeps_the_local_zone(self):
        self.assertIsNone(_zone_get(None))

    def test_fixed_offset_refused(self):
        """A fixed offset names one instant of the year, so it is not a zone."""
        for name in ("+02:00", "-05:00", "UTC+2"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                _zone_get(name)

    def test_unknown_zone_refused(self):
        with self.assertRaises(ValueError):
            _zone_get("Mars/Olympus_Mons")


class TestTimezoneOption(BaseCLITest):
    """``--tz`` decides the zone printed timestamps are rendered in."""

    def test_tz_shifts_the_printed_date(self):
        """The same stored instant prints a different date per zone."""
        env = {"RESEMBL_NOW": "2024-06-01T23:30:00+00:00"}
        result = self.run_command("collection create zoned", extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        warsaw = self.run_command("--tz Europe/Warsaw collection list", extra_env=env)
        tokyo = self.run_command("--tz Asia/Tokyo collection list", extra_env=env)
        self.assertEqual(warsaw.returncode, 0, warsaw.stderr)
        self.assertEqual(tokyo.returncode, 0, tokyo.stderr)
        self.assertIn("2024-06-02", warsaw.stdout)
        self.assertIn("2024-06-02", tokyo.stdout)

    def test_tz_before_the_stored_date(self):
        """A zone behind UTC can print the previous calendar day."""
        self.run_command(
            "collection create zoned", extra_env={"RESEMBL_NOW": "2024-06-02T00:30:00+00:00"}
        )
        result = self.run_command("--tz America/Los_Angeles collection list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2024-06-01", result.stdout)

    def test_unknown_zone_exits_2(self):
        """An unknown zone is a usage error, not a silent local-zone fallback."""
        result = self.run_command("--tz Mars/Olympus_Mons collection list")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a known IANA time zone", result.stderr)

    def test_fixed_offset_exits_2(self):
        """A fixed offset is refused: it cannot follow a DST transition."""
        result = self.run_command("--tz +02:00 collection list")
        self.assertEqual(result.returncode, 2)
        self.assertIn("fixed offset", result.stderr)


class TestResolveChecksum(unittest.TestCase):
    """Unit tests for the checksum-prefix resolver's LIKE handling."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{tmp.name}/test.db")
        # After tmp.cleanup, so it runs first: Windows refuses to remove a
        # directory holding a database file whose handle is still open.
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

    def test_wildcard_prefix_matches_nothing(self):
        """LIKE metacharacters in a prefix are matched literally.

        Checksums are hex, so "%"/"_" can never be a real prefix: unescaped,
        "%" matched every row and resolved (e.g. for ``rm``) to an arbitrary
        snippet instead of reporting that no snippet matches.
        """
        from resembl import cli

        with Session(self.engine) as session:
            snippet = snippet_add(session, "t1", "MOV EAX, 1")
            old_session = getattr(cli.state, "session", None)
            cli.state.session = session
            try:
                for garbage in ("%", "_", "%%", "_%", "%_"):
                    self.assertIsNone(cli._resolve_checksum(garbage), garbage)
                # A literal underscore inside a longer garbage prefix too.
                self.assertIsNone(cli._resolve_checksum(f"{snippet.checksum[:4]}_%"))
                # Real prefixes keep resolving.
                self.assertEqual(cli._resolve_checksum(snippet.checksum[:8]), snippet.checksum)
            finally:
                cli.state.session = old_session
                if old_session is None:
                    del cli.state.session


class _StubContext:
    """The slice of ``typer.Context`` the main callback reads."""

    invoked_subcommand: str | None = "list"


class _RecordingSession:
    """A stand-in for the session the callback opens, tracking its close."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestSessionLifecycle(unittest.TestCase):
    """The session the main callback opens is released, not just pooled.

    Registering ``state.session.close`` as the exit hook ran once per
    callback, and each registration held a strong reference to its own
    session: a process driving the CLI repeatedly (a test harness, an
    embedding script) kept every earlier session alive, each holding a
    checked-out connection, and grew the exit-hook registry without bound.
    """

    def setUp(self):
        self.addCleanup(cli._close_active_session)
        self.addCleanup(setattr, cli, "_active_session", None)

    def _run_callback(self):
        """Run the main callback against a recording session; return it."""
        session = _RecordingSession()
        with (
            patch.object(cli, "Session", return_value=session),
            patch.object(cli, "db_create"),
            patch("resembl.database.get_engine"),
        ):
            cli.app_callback(_StubContext(), False, False, False, None, False, None)
        return session

    def test_a_new_callback_closes_the_previous_session(self):
        first = self._run_callback()
        self.assertFalse(first.closed)
        second = self._run_callback()
        self.assertTrue(first.closed, "the previous session was never released")
        self.assertIs(cli.state.session, second)

    def test_the_exit_hook_registers_once_per_process(self):
        with patch("atexit.register") as register:
            self._run_callback()
            self._run_callback()
        register.assert_not_called()


if __name__ == "__main__":
    unittest.main()
