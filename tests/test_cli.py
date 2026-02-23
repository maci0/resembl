"""Integration tests for the resembl CLI."""

import os
import random
import shlex
import subprocess
import tempfile
import unittest

import tomli
from sqlmodel import Session, SQLModel, create_engine, select

from resembl.core import snippet_add
from resembl.models import Snippet


class BaseCLITest(unittest.TestCase):
    """Base class for CLI tests with common setup and helper methods."""

    def setUp(self):
        """Set up a clean, in-memory database for each test."""
        self.db_name = f"test_{random.randint(0, 100000)}.db"
        self.engine = create_engine(f"sqlite:///{self.db_name}")
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            snippet_add(session, "test_snippet", "MOV EAX, 1")

    def tearDown(self):
        """Clean up the database after each test."""
        self.engine.dispose()
        if os.path.exists(self.db_name):
            os.remove(self.db_name)

    def run_command(self, command, input_data=None, extra_env=None):
        """Helper function to run a command and return the output."""
        env = {
            **os.environ,
            "PYTHONPATH": os.path.join(os.getcwd(), "."),
            "DATABASE_URL": f"sqlite:///{self.db_name}",
        }
        if extra_env:
            env.update(extra_env)

        result = subprocess.run(
            ["python", "-m", "resembl.cli", *shlex.split(command)],
            shell=False,
            capture_output=True,
            text=True,
            input=input_data,
            env=env,
            check=False,
        )
        return result


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
        self.assertIn("Number of snippets: 1", result.stdout)

    def test_find_command(self):
        """Test the find command."""
        result = self.run_command("find --query 'MOV EAX, 1'")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Top Matches", result.stdout)
        self.assertIn("test_snippet", result.stdout)

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

    def test_compare_missing_snippet(self):
        """Comparing unknown checksums should fail."""
        result = self.run_command("compare deadbeef cafebabe")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not be found", result.stderr)

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
        self.assertIn("Snippets re-indexed: 1", result.stdout)

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

            with open(exported_file, "r", encoding="utf-8") as f:
                content = f.read()
                self.assertEqual(content, "MOV EAX, 1")

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
            env = {"HOME": home}
            self.run_command("config set lsh_threshold 0.7", extra_env=env)
            self.run_command("config set top_n 10", extra_env=env)

            config_path = os.path.join(home, ".config", "resembl", "config.toml")
            with open(config_path, "rb") as f:
                data = tomli.load(f)

            self.assertEqual(data.get("lsh_threshold"), 0.7)
            self.assertEqual(data.get("top_n"), 10)

    def test_config_dir_env_override(self):
        """RESEMBL_CONFIG_DIR should override the default config path."""
        with tempfile.TemporaryDirectory() as cfgdir:
            self.run_command(
                "config set top_n 7", extra_env={"RESEMBL_CONFIG_DIR": cfgdir}
            )
            config_path = os.path.join(cfgdir, "config.toml")
            with open(config_path, "rb") as f:
                data = tomli.load(f)
            self.assertEqual(data.get("top_n"), 7)

            result = self.run_command(
                "config path", extra_env={"RESEMBL_CONFIG_DIR": cfgdir}
            )
            self.assertIn(config_path, result.stdout)

    def test_cache_dir_env_override(self):
        """RESEMBL_CACHE_DIR should control where cache files are stored."""
        with tempfile.TemporaryDirectory() as cache_dir:
            self.run_command(
                "find --query 'MOV EAX, 1'",
                extra_env={"RESEMBL_CACHE_DIR": cache_dir},
            )
            cache_file = os.path.join(cache_dir, "lsh_0.50.pkl")
            self.assertTrue(os.path.exists(cache_file))

    def test_config_set_and_list(self):
        """`config list` should show values set via `config set`."""
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home}
            self.run_command("config set lsh_threshold 0.6", extra_env=env)
            list_result = self.run_command("config list", extra_env=env)
            self.assertIn("lsh_threshold = 0.6", list_result.stdout)
            self.assertIn("top_n = 5", list_result.stdout)

    def test_config_set_invalid_key(self):
        """`config set` should reject invalid keys."""
        result = self.run_command("config set invalid_key 123")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid configuration key", result.stderr)

    def test_config_get(self):
        """`config get` should retrieve a specific value."""
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home}
            self.run_command("config set top_n 15", extra_env=env)
            result = self.run_command("config get top_n", extra_env=env)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "15")

    def test_config_get_nonexistent_key(self):
        """`config get` on a nonexistent key should show the default."""
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home}
            result = self.run_command("config get top_n", extra_env=env)
            self.assertEqual(result.returncode, 0)
            self.assertIn("5", result.stdout)  # Default value

    def test_config_unset(self):
        """`config unset` should remove a key from the config file."""
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home}
            self.run_command("config set top_n 20", extra_env=env)
            result = self.run_command("config unset top_n", extra_env=env)
            self.assertEqual(result.returncode, 0)

            # Verify that the key is gone
            list_result = self.run_command("config list", extra_env=env)
            self.assertNotIn("top_n = 20", list_result.stdout)
            self.assertIn("top_n = 5", list_result.stdout)  # Shows default


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


class TestCLIImport(BaseCLITest):
    """Tests for the import command."""

    def test_import_counts_only_new_snippets(self):
        """Re-importing the same files should report 0 new snippets."""
        with tempfile.TemporaryDirectory() as import_dir:
            file_path = os.path.join(import_dir, "existing.asm")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("PUSH EBP; MOV EBP, ESP; POP EBP; RET")

            # First import – should report 1 new snippet
            result = self.run_command(f"import --force --json {import_dir}")
            self.assertEqual(result.returncode, 0)
            import json

            data = json.loads(result.stdout)
            self.assertEqual(data["num_imported"], 1)

            # Second import of the same file – should report 0 new snippets
            result = self.run_command(f"import --force --json {import_dir}")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["num_imported"], 0)


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
            snippets = session.exec(
                select(Snippet).where(Snippet.names == '[""]')
            ).all()
            self.assertEqual(len(snippets), 2)

    def test_add_multiple_snippets_with_same_name(self):
        """Test that multiple snippets can be added with the same name."""
        self.run_command("add same_name 'MOV EDI, 6'")
        self.run_command("add same_name 'MOV EBP, 7'")
        with Session(self.engine) as session:
            snippets = session.exec(
                select(Snippet).where(Snippet.names == '["same_name"]')
            ).all()
            self.assertEqual(len(snippets), 2)


if __name__ == "__main__":
    unittest.main()
