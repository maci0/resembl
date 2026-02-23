"""Unit tests for the resembl core module."""

import os
import tempfile
import unittest

from sqlmodel import Session, SQLModel, create_engine, select

from resembl.core import (
    code_create_minhash,
    code_tokenize,
    db_calculate_average_similarity,
    db_clean,
    db_reindex,
    db_stats,
    snippet_add,
    snippet_compare,
    snippet_delete,
    snippet_export,
    snippet_find_matches,
    snippet_get,
    snippet_list,
    snippet_name_add,
    snippet_name_remove,
    string_checksum,
)
from resembl.models import Snippet

# Use an in-memory SQLite database for testing
DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(DATABASE_URL)


class TestResembl(unittest.TestCase):
    """Tests for core snippet operations."""

    def setUp(self):
        """Set up a clean database for each test."""
        SQLModel.metadata.create_all(engine)
        self.session = Session(engine)

    def tearDown(self):
        """Clean up the database after each test."""
        self.session.close()
        SQLModel.metadata.drop_all(engine)

    def test_add_and_get_snippet(self):
        """Test adding a snippet and retrieving it by its checksum."""
        name = "test_snippet"
        code = "MOV EAX, 1"
        checksum = string_checksum(code)

        snippet_add(self.session, name, code)

        retrieved = snippet_get(self.session, checksum)
        self.assertIsNotNone(retrieved)
        self.assertIn(name, retrieved.name_list)
        self.assertEqual(retrieved.code, code)
        self.assertEqual(retrieved.checksum, checksum)

    def test_add_alias_to_existing_code(self):
        """Test that adding a snippet with identical code adds an alias."""
        name1 = "snippet_one"
        name2 = "snippet_two"
        code = "MOV EBX, 2"

        snippet_add(self.session, name1, code)
        result = snippet_add(self.session, name2, code)
        self.assertIsNotNone(result)

        snippets = self.session.exec(select(Snippet)).all()
        self.assertEqual(len(snippets), 1)
        self.assertIn(name1, snippets[0].name_list)
        self.assertIn(name2, snippets[0].name_list)

    def test_normalization(self):
        """Test the normalization function."""
        code1 = "MOV EAX, [ESP+4] ; load first argument"
        code2 = "mov eax, [esp+4]"
        minhash1 = code_create_minhash(code1)
        minhash2 = code_create_minhash(code2)
        self.assertGreater(minhash1.jaccard(minhash2), 0.99)

    def test_find_matches(self):
        """Test finding top matches for a query."""
        snippet1_name = "string_copy"
        snippet1_code = """
        string_copy:
            lodsb
            stosb
            test al, al
            jnz string_copy
        """
        snippet1_checksum = string_checksum(snippet1_code)
        snippet_add(self.session, snippet1_name, snippet1_code)

        snippet2_name = "sum_array"
        snippet2_code = """
        sum_loop:
            add eax, [esi]
            esi, 4
            dec ecx
            jnz sum_loop
        """
        snippet_add(self.session, snippet2_name, snippet2_code)

        query = """
        copy_loop:
            lodsb
            stosb
            test al, al
            jnz copy_loop
        """
        _num_candidates, matches = snippet_find_matches(self.session, query, top_n=1)

        self.assertEqual(len(matches), 1)
        # The key of the match should be the checksum
        self.assertEqual(matches[0][0].checksum, snippet1_checksum)

    def test_large_and_unicode_snippets(self):
        """Ensure very large and unicode-heavy snippets are handled."""
        large_code = "\n".join(["MOV EAX, EBX"] * 1000)
        unicode_code = "MOV EAX, 1 ; π≈3.14"

        snippet_add(self.session, "big", large_code)
        snippet_add(self.session, "unicode", unicode_code)

        checksum_large = string_checksum(large_code)
        checksum_unicode = string_checksum(unicode_code)

        self.assertIsNotNone(snippet_get(self.session, checksum_large))
        self.assertIsNotNone(snippet_get(self.session, checksum_unicode))

    def test_find_no_matches(self):
        """Test that find returns an empty list when no matches are found."""
        snippet_add(self.session, "test", "MOV EAX, 1")
        _num, matches = snippet_find_matches(self.session, "JMP 0x42")
        self.assertEqual(len(matches), 0)

    def test_empty_query(self):
        """Test that an empty query returns no matches."""
        snippet_add(self.session, "test", "MOV EAX, 1")
        _num, matches = snippet_find_matches(self.session, "")
        self.assertEqual(len(matches), 0)

    def test_add_empty_snippet(self):
        """Test that adding an empty snippet does nothing."""
        result = snippet_add(self.session, "empty", "")
        self.assertIsNone(result)
        self.assertIsNone(snippet_get(self.session, string_checksum("")))

    def test_get_by_name_not_found(self):
        """Test getting a snippet by a name that does not exist."""
        retrieved = Snippet.get_by_name(self.session, "non_existent")
        self.assertIsNone(retrieved)

    def test_get_tokens_no_normalize(self):
        """Test getting tokens without normalization."""
        tokens = code_tokenize("mov eax, 1", normalize=False)
        self.assertEqual(tokens, ["MOV", "EAX", "1"])

    def test_find_matches_no_candidates(self):
        """Test finding matches with no candidates."""
        _num, matches = snippet_find_matches(self.session, "MOV EAX, 1")
        self.assertEqual(len(matches), 0)


class TestDBCoreFunctions(unittest.TestCase):
    """Tests for core database functions."""

    def setUp(self):
        """Set up a clean, in-memory database for each test."""
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        """Clean up the database after each test."""
        self.session.close()
        SQLModel.metadata.drop_all(self.engine)

    def test_db_reindex(self):
        """Test reindexing the database."""
        snippet_add(self.session, "test", "MOV EAX, 1")
        result = db_reindex(self.session)
        self.assertEqual(result["num_reindexed"], 1)

    def test_db_stats(self):
        """Test getting database statistics."""
        snippet_add(self.session, "test", "MOV EAX, 1")
        stats = db_stats(self.session)
        self.assertEqual(stats["num_snippets"], 1)

    def test_db_clean(self):
        """Test cleaning the database."""
        result = db_clean(self.session)
        self.assertTrue(result["vacuum_success"])

    def test_db_reindex_empty_db(self):
        """Test reindexing an empty database."""
        result = db_reindex(self.session)
        self.assertEqual(result["num_reindexed"], 0)

    def test_db_stats_empty_db(self):
        """Test getting stats for an empty database."""
        stats = db_stats(self.session)
        self.assertEqual(stats["num_snippets"], 0)


class TestSnippetCoreFunctions(unittest.TestCase):
    """Tests for core snippet functions."""

    def setUp(self):
        """Set up a clean, in-memory database for each test."""
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        """Clean up the database after each test."""
        self.session.close()
        SQLModel.metadata.drop_all(self.engine)

    def test_snippet_name_add(self):
        """Test adding a name to a snippet."""
        snippet = snippet_add(self.session, "test", "MOV EAX, 1")
        self.assertIsNotNone(snippet)
        snippet_name_add(self.session, snippet.checksum, "new_name")
        retrieved = snippet_get(self.session, snippet.checksum)
        self.assertIn("new_name", retrieved.name_list)

    def test_snippet_name_remove(self):
        """Test removing a name from a snippet."""
        snippet = snippet_add(self.session, "test", "MOV EAX, 1")
        self.assertIsNotNone(snippet)
        snippet_add(self.session, "test2", "MOV EAX, 1")
        snippet_name_remove(self.session, snippet.checksum, "test")
        retrieved = snippet_get(self.session, snippet.checksum)
        self.assertNotIn("test", retrieved.name_list)

    def test_snippet_delete(self):
        """Test deleting a snippet."""
        snippet = snippet_add(self.session, "test", "MOV EAX, 1")
        self.assertIsNotNone(snippet)
        snippet_delete(self.session, snippet.checksum)
        retrieved = snippet_get(self.session, snippet.checksum)
        self.assertIsNone(retrieved)

    def test_snippet_compare(self):
        """Test comparing two snippets."""
        s1 = snippet_add(self.session, "s1", "MOV EAX, 1")
        s2 = snippet_add(self.session, "s2", "MOV EAX, 2")
        self.assertIsNotNone(s1)
        self.assertIsNotNone(s2)
        result = snippet_compare(self.session, s1.checksum, s2.checksum)
        self.assertIsNotNone(result)
        self.assertIn("comparison", result)

    def test_snippet_list(self):
        """Test listing snippets."""
        snippet_add(self.session, "test1", "MOV EAX, 1")
        snippet_add(self.session, "test2", "MOV EAX, 2")
        snippets = snippet_list(self.session)
        self.assertEqual(len(snippets), 2)
        snippets = snippet_list(self.session, start=1, end=2)
        self.assertEqual(len(snippets), 1)

    def test_snippet_export(self):
        """Test exporting snippets."""
        snippet_add(self.session, "test", "MOV EAX, 1")
        with tempfile.TemporaryDirectory() as temp_dir:
            result = snippet_export(self.session, temp_dir)
            self.assertEqual(result["num_exported"], 1)
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "test.asm")))

    def test_get_average_similarity(self):
        """Test getting the average similarity."""
        snippet_add(self.session, "s1", "MOV EAX, 1")
        snippet_add(self.session, "s2", "MOV EAX, 2")
        similarity = db_calculate_average_similarity(self.session)
        self.assertIsInstance(similarity, float)

    def test_snippet_name_add_nonexistent_snippet(self):
        """Test adding a name to a non-existent snippet."""
        result = snippet_name_add(self.session, "nonexistent", "new_name")
        self.assertIsNone(result)

    def test_snippet_name_add_existing_name(self):
        """Test adding a name that already exists."""
        snippet = snippet_add(self.session, "test", "MOV EAX, 1")
        self.assertIsNotNone(snippet)
        result = snippet_name_add(self.session, snippet.checksum, "test")
        self.assertIsNone(result)

    def test_snippet_name_remove_nonexistent_snippet(self):
        """Test removing a name from a non-existent snippet."""
        result = snippet_name_remove(self.session, "nonexistent", "test")
        self.assertIsNone(result)

    def test_snippet_name_remove_nonexistent_name(self):
        """Test removing a name that does not exist."""
        snippet = snippet_add(self.session, "test", "MOV EAX, 1")
        self.assertIsNotNone(snippet)
        result = snippet_name_remove(self.session, snippet.checksum, "nonexistent")
        self.assertIsNone(result)

    def test_snippet_name_remove_last_name(self):
        """Test that the last name cannot be removed from a snippet."""
        snippet = snippet_add(self.session, "test", "MOV EAX, 1")
        self.assertIsNotNone(snippet)
        result = snippet_name_remove(self.session, snippet.checksum, "test")
        self.assertIsNone(result)

    def test_snippet_delete_nonexistent_snippet(self):
        """Test deleting a non-existent snippet."""
        result = snippet_delete(self.session, "nonexistent")
        self.assertFalse(result)

    def test_snippet_compare_nonexistent_snippet(self):
        """Test comparing a non-existent snippet."""
        s1 = snippet_add(self.session, "s1", "MOV EAX, 1")
        self.assertIsNotNone(s1)
        result = snippet_compare(self.session, s1.checksum, "nonexistent")
        self.assertIsNone(result)

    def test_get_average_similarity_empty_db(self):
        """Test getting average similarity for an empty database."""
        similarity = db_calculate_average_similarity(self.session)
        self.assertEqual(similarity, 1.0)

    def test_snippet_name_add_quiet(self):
        """Test the quiet flag in snippet_name_add."""
        result = snippet_name_add(self.session, "nonexistent", "new_name", quiet=True)
        self.assertIsNone(result)

    def test_snippet_name_remove_quiet(self):
        """Test the quiet flag in snippet_name_remove."""
        result = snippet_name_remove(
            self.session, "nonexistent", "new_name", quiet=True
        )
        self.assertIsNone(result)

    def test_snippet_export_empty_db(self):
        """Test exporting an empty database."""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = snippet_export(self.session, temp_dir)
            self.assertEqual(result["num_exported"], 0)

    def test_snippet_export_sanitizes_names(self):
        """Verify that path-traversal snippet names are sanitized on export."""
        snippet_add(self.session, "../../evil", "MOV EAX, 0xDEAD")
        with tempfile.TemporaryDirectory() as temp_dir:
            result = snippet_export(self.session, temp_dir)
            self.assertEqual(result["num_exported"], 1)
            # The file should be inside temp_dir, not outside
            for fname in os.listdir(temp_dir):
                full_path = os.path.join(temp_dir, fname)
                self.assertTrue(
                    os.path.realpath(full_path).startswith(os.path.realpath(temp_dir))
                )


if __name__ == "__main__":
    unittest.main()
