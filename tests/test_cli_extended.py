"""CLI integration tests for collection, version, merge, and search commands."""

import json
import os
import tempfile
import unittest

from sqlmodel import Session

from resembl.core import collection_create, snippet_add, snippet_tag_add
from tests.test_cli import BaseCLITest


class TestCLICollections(BaseCLITest):
    """Integration tests for the collection command group."""

    def test_collection_create(self):
        """Creating a collection should succeed."""
        result = self.run_command("collection create test_col --description 'Test collection'")
        self.assertEqual(result.returncode, 0)
        self.assertIn("test_col", result.stdout)

    def test_collection_list(self):
        """Listing collections should show created ones."""
        with Session(self.engine) as session:
            collection_create(session, "my_col", description="A test")
        result = self.run_command("collection list")
        self.assertEqual(result.returncode, 0)
        self.assertIn("my_col", result.stdout)

    def test_collection_show(self):
        """Showing a collection should list its snippets."""
        with Session(self.engine) as session:
            collection_create(session, "group")
            from resembl.core import collection_add_snippet
            from resembl.models import Snippet
            s = Snippet.get_by_name(session, "test_snippet")
            collection_add_snippet(session, "group", s.checksum)
        result = self.run_command("collection show group")
        self.assertEqual(result.returncode, 0)
        self.assertIn("test_snippet", result.stdout)

    def test_collection_delete(self):
        """Deleting a collection should succeed."""
        with Session(self.engine) as session:
            collection_create(session, "to_delete")
        result = self.run_command("collection delete to_delete")
        self.assertEqual(result.returncode, 0)

    def test_collection_add_snippet(self):
        """Adding a snippet to a collection via CLI."""
        with Session(self.engine) as session:
            collection_create(session, "target_col")
            from resembl.models import Snippet
            s = Snippet.get_by_name(session, "test_snippet")
            checksum = s.checksum
        result = self.run_command(f"collection add target_col {checksum}")
        self.assertEqual(result.returncode, 0)

    def test_collection_remove_snippet(self):
        """Removing a snippet from its collection via CLI."""
        with Session(self.engine) as session:
            collection_create(session, "my_col")
            from resembl.core import collection_add_snippet
            from resembl.models import Snippet
            s = Snippet.get_by_name(session, "test_snippet")
            collection_add_snippet(session, "my_col", s.checksum)
            checksum = s.checksum
        result = self.run_command(f"collection remove {checksum}")
        self.assertEqual(result.returncode, 0)

    def test_collection_list_quiet(self):
        """--quiet should suppress collection list output."""
        with Session(self.engine) as session:
            collection_create(session, "quiet_col")
        result = self.run_command("--quiet collection list")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")


class TestCLIMerge(BaseCLITest):
    """Integration tests for the merge command."""

    def _create_source_db(self):
        """Create a source DB with a unique snippet."""
        from sqlmodel import SQLModel, create_engine
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        src_engine = create_engine(f"sqlite:///{tmp.name}")
        SQLModel.metadata.create_all(src_engine)
        with Session(src_engine) as session:
            snippet_add(session, "source_func", "PUSH EBP; MOV EBP, ESP; POP EBP")
        src_engine.dispose()
        return tmp.name

    def test_merge_command(self):
        """Merging a source DB should report results."""
        source_path = self._create_source_db()
        try:
            result = self.run_command(f"merge {source_path}")
            self.assertEqual(result.returncode, 0)
            self.assertIn("Merge Complete", result.stdout)
        finally:
            os.unlink(source_path)

    def test_merge_json_format(self):
        """Merging with --format json should produce valid JSON."""
        source_path = self._create_source_db()
        try:
            result = self.run_command(f"--format json merge {source_path}")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertIn("added", data)
        finally:
            os.unlink(source_path)

    def test_merge_nonexistent_file(self):
        """Merging a nonexistent file should fail."""
        result = self.run_command("merge /tmp/nonexistent_db.db")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error", result.stderr)


class TestCLIVersion(BaseCLITest):
    """Integration tests for the version command."""

    def test_version_command(self):
        """version should return results (possibly empty)."""
        with Session(self.engine) as session:
            from resembl.models import Snippet
            s = Snippet.get_by_name(session, "test_snippet")
            checksum = s.checksum
        result = self.run_command(f"version {checksum}")
        self.assertEqual(result.returncode, 0)


class TestCLISearch(BaseCLITest):
    """Integration tests for the search command."""

    def test_search_by_name(self):
        """search command should find snippets by name pattern."""
        with Session(self.engine) as session:
            snippet_add(session, "memcpy_impl", "REP MOVSB")
            snippet_add(session, "strcmp_impl", "CMPSB")
        result = self.run_command("search mem")
        self.assertEqual(result.returncode, 0)
        self.assertIn("memcpy_impl", result.stdout)
        self.assertNotIn("strcmp_impl", result.stdout)


class TestCLIFormatFlag(BaseCLITest):
    """Integration tests for --format json/csv."""

    def test_stats_json(self):
        """stats --format json should produce valid JSON."""
        result = self.run_command("--format json stats")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertIn("num_snippets", data)

    def test_list_json(self):
        """list --format json should produce valid JSON."""
        result = self.run_command("--format json list")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertIsInstance(data, list)

    def test_list_csv(self):
        """list --format csv should produce CSV output."""
        result = self.run_command("--format csv list")
        self.assertEqual(result.returncode, 0)
        # CSV output should have header row
        lines = result.stdout.strip().split("\n")
        self.assertGreaterEqual(len(lines), 1)

    def test_find_json(self):
        """find --format json should produce valid JSON with matches key."""
        result = self.run_command("--format json find --query 'MOV EAX, 1'")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertIn("matches", data)
        self.assertIsInstance(data["matches"], list)


class TestCLITagEdgeCases(BaseCLITest):
    """Edge-case tests for tag commands."""

    def test_tag_add_idempotent(self):
        """Adding the same tag twice should succeed both times (idempotent)."""
        with Session(self.engine) as session:
            from resembl.models import Snippet
            s = Snippet.get_by_name(session, "test_snippet")
            checksum = s.checksum
        # First add
        result1 = self.run_command(f"tag add {checksum} 'crypto'")
        self.assertEqual(result1.returncode, 0)
        # Second add (should be idempotent)
        result2 = self.run_command(f"tag add {checksum} 'crypto'")
        self.assertEqual(result2.returncode, 0)


class TestCLIShowCommand(BaseCLITest):
    """Tests for the show command."""

    def test_show_by_checksum(self):
        """show should display snippet details."""
        with Session(self.engine) as session:
            from resembl.models import Snippet
            s = Snippet.get_by_name(session, "test_snippet")
            checksum = s.checksum
        result = self.run_command(f"show {checksum}")
        self.assertEqual(result.returncode, 0)
        self.assertIn("test_snippet", result.stdout)

    def test_show_by_partial_checksum(self):
        """show should work with checksum prefix."""
        with Session(self.engine) as session:
            from resembl.models import Snippet
            s = Snippet.get_by_name(session, "test_snippet")
            prefix = s.checksum[:8]
        result = self.run_command(f"show {prefix}")
        self.assertEqual(result.returncode, 0)

    def test_show_nonexistent(self):
        """show with invalid checksum should fail."""
        result = self.run_command("show ffffffffffffffff")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
