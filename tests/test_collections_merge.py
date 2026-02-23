"""Tests for collections, versioning, merge, tags, search, and config dict-compat."""

import json
import os
import tempfile
import unittest

from sqlmodel import Session, SQLModel, create_engine, select

from resembl.core import (
    collection_add_snippet,
    collection_create,
    collection_delete,
    collection_list,
    collection_remove_snippet,
    db_merge,
    snippet_add,
    snippet_get,
    snippet_search_by_name,
    snippet_tag_add,
    snippet_tag_remove,
    snippet_version_list,
    string_checksum,
)
from resembl.config import ResemblConfig
from resembl.models import Collection, Snippet, SnippetVersion


class BaseDBTest(unittest.TestCase):
    """Base class providing an in-memory database session per test."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        SQLModel.metadata.drop_all(self.engine)


# ---------------------------------------------------------------------------
# Collection tests
# ---------------------------------------------------------------------------


class TestCollections(BaseDBTest):
    """Tests for collection CRUD operations."""

    def test_create_collection(self):
        """A new collection should be retrievable by name."""
        col = collection_create(self.session, "crypto", description="Crypto routines")
        self.assertEqual(col.name, "crypto")
        self.assertEqual(col.description, "Crypto routines")
        # Confirm it's in the DB
        fetched = Collection.get_by_name(self.session, "crypto")
        self.assertIsNotNone(fetched)

    def test_delete_collection_unassigns_snippets(self):
        """Deleting a collection should unassign its snippets."""
        collection_create(self.session, "libc")
        snippet = snippet_add(self.session, "memcpy", "REP MOVSB")
        collection_add_snippet(self.session, "libc", snippet.checksum)
        # Confirm assignment
        self.assertEqual(snippet_get(self.session, snippet.checksum).collection, "libc")
        # Delete
        result = collection_delete(self.session, "libc")
        self.assertTrue(result)
        # Snippet should still exist but unassigned
        s = snippet_get(self.session, snippet.checksum)
        self.assertIsNotNone(s)
        self.assertIsNone(s.collection)

    def test_delete_nonexistent_collection(self):
        """Deleting a nonexistent collection should return False."""
        self.assertFalse(collection_delete(self.session, "nope", quiet=True))

    def test_collection_list_with_counts(self):
        """collection_list should include snippet counts."""
        collection_create(self.session, "group_a")
        snippet = snippet_add(self.session, "func1", "NOP")
        collection_add_snippet(self.session, "group_a", snippet.checksum)
        result = collection_list(self.session)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "group_a")
        self.assertEqual(result[0]["snippet_count"], 1)

    def test_add_snippet_to_nonexistent_collection(self):
        """Adding to a nonexistent collection should return None."""
        snippet = snippet_add(self.session, "func", "RET")
        result = collection_add_snippet(self.session, "missing", snippet.checksum, quiet=True)
        self.assertIsNone(result)

    def test_add_nonexistent_snippet_to_collection(self):
        """Adding a nonexistent snippet should return None."""
        collection_create(self.session, "col")
        result = collection_add_snippet(self.session, "col", "deadbeef", quiet=True)
        self.assertIsNone(result)

    def test_remove_snippet_from_collection(self):
        """Removing a snippet from its collection should set collection to None."""
        collection_create(self.session, "test_col")
        snippet = snippet_add(self.session, "f", "PUSH EBP")
        collection_add_snippet(self.session, "test_col", snippet.checksum)
        result = collection_remove_snippet(self.session, snippet.checksum)
        self.assertIsNotNone(result)
        self.assertIsNone(result.collection)

    def test_remove_nonexistent_snippet(self):
        """Removing a nonexistent snippet should return None."""
        result = collection_remove_snippet(self.session, "bad", quiet=True)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Tag tests (core functions, not CLI)
# ---------------------------------------------------------------------------


class TestTagCore(BaseDBTest):
    """Tests for tag add/remove core functions."""

    def test_tag_add(self):
        """Adding a tag should persist."""
        snippet = snippet_add(self.session, "func", "XOR EAX, EAX")
        result = snippet_tag_add(self.session, snippet.checksum, "crypto")
        self.assertIsNotNone(result)
        self.assertIn("crypto", result.tag_list)

    def test_tag_add_duplicate(self):
        """Adding the same tag twice should be idempotent."""
        snippet = snippet_add(self.session, "func", "XOR EAX, EAX")
        snippet_tag_add(self.session, snippet.checksum, "crypto")
        result = snippet_tag_add(self.session, snippet.checksum, "crypto")
        # Returns snippet but doesn't double-add
        self.assertIsNotNone(result)
        self.assertEqual(result.tag_list.count("crypto"), 1)

    def test_tag_remove(self):
        """Removing a tag should persist."""
        snippet = snippet_add(self.session, "func", "XOR EAX, EAX")
        snippet_tag_add(self.session, snippet.checksum, "malware")
        result = snippet_tag_remove(self.session, snippet.checksum, "malware")
        self.assertIsNotNone(result)
        self.assertNotIn("malware", result.tag_list)

    def test_tag_remove_nonexistent(self):
        """Removing a tag that doesn't exist should return the snippet unchanged."""
        snippet = snippet_add(self.session, "func", "XOR EAX, EAX")
        result = snippet_tag_remove(self.session, snippet.checksum, "nosuch")
        self.assertIsNotNone(result)

    def test_tag_add_to_nonexistent_snippet(self):
        """Adding a tag to a nonexistent snippet should return None."""
        result = snippet_tag_add(self.session, "nope", "tag", quiet=True)
        self.assertIsNone(result)

    def test_tag_remove_from_nonexistent_snippet(self):
        """Removing a tag from a nonexistent snippet should return None."""
        result = snippet_tag_remove(self.session, "nope", "tag", quiet=True)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Search tests
# ---------------------------------------------------------------------------


class TestSearch(BaseDBTest):
    """Tests for snippet_search_by_name."""

    def test_search_finds_matching_names(self):
        """Searching should find snippets whose names match the pattern."""
        snippet_add(self.session, "memcpy", "REP MOVSB")
        snippet_add(self.session, "memset", "REP STOSB")
        snippet_add(self.session, "strcmp", "CMPSB")
        results = snippet_search_by_name(self.session, "mem")
        self.assertEqual(len(results), 2)

    def test_search_no_match(self):
        """Searching for a nonexistent name should return empty list."""
        snippet_add(self.session, "func", "RET")
        results = snippet_search_by_name(self.session, "zzz")
        self.assertEqual(len(results), 0)


# ---------------------------------------------------------------------------
# DB Merge tests
# ---------------------------------------------------------------------------


class TestDBMerge(BaseDBTest):
    """Tests for the db_merge function."""

    def _create_source_db(self, snippets, collections=None):
        """Helper: create a source DB file and return its path."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        source_url = f"sqlite:///{tmp.name}"
        source_engine = create_engine(source_url)
        SQLModel.metadata.create_all(source_engine)
        with Session(source_engine) as src_session:
            if collections:
                for name, desc in collections:
                    src_session.add(Collection(name=name, description=desc))
            for name, code, tags, col in snippets:
                s = snippet_add(src_session, name, code)
                if tags:
                    for t in tags:
                        snippet_tag_add(src_session, s.checksum, t)
                if col:
                    collection_add_snippet(src_session, col, s.checksum)
        source_engine.dispose()
        return tmp.name

    def test_merge_new_snippets(self):
        """Merging a source with unique snippets should add them."""
        source_path = self._create_source_db([
            ("func_a", "MOV EAX, 1", [], None),
            ("func_b", "MOV EBX, 2", [], None),
        ])
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["added"], 2)
            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["skipped"], 0)
        finally:
            os.unlink(source_path)

    def test_merge_duplicate_snippets_skipped(self):
        """Merging identical snippets should skip them."""
        snippet_add(self.session, "func_a", "MOV EAX, 1")
        source_path = self._create_source_db([
            ("func_a", "MOV EAX, 1", [], None),
        ])
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["added"], 0)
            self.assertEqual(result["skipped"], 1)
        finally:
            os.unlink(source_path)

    def test_merge_adds_new_names(self):
        """Merging should add new names to existing snippets."""
        snippet_add(self.session, "original_name", "MOV EAX, 1")
        source_path = self._create_source_db([
            ("alias_name", "MOV EAX, 1", [], None),
        ])
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["updated"], 1)
            # Check names merged
            checksum = string_checksum("MOV EAX, 1")
            s = snippet_get(self.session, checksum)
            self.assertIn("original_name", s.name_list)
            self.assertIn("alias_name", s.name_list)
        finally:
            os.unlink(source_path)

    def test_merge_adds_new_tags_independently(self):
        """Merging should add tags even if names didn't change (bug fix verification)."""
        snippet = snippet_add(self.session, "func", "MOV EAX, 1")
        source_path = self._create_source_db([
            ("func", "MOV EAX, 1", ["new_tag"], None),
        ])
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["updated"], 1)
            s = snippet_get(self.session, snippet.checksum)
            self.assertIn("new_tag", s.tag_list)
        finally:
            os.unlink(source_path)

    def test_merge_imports_collections(self):
        """Merging should create collections from the source if missing."""
        source_path = self._create_source_db(
            snippets=[("func", "MOV EAX, 1", [], "imported_col")],
            collections=[("imported_col", "From source DB")],
        )
        try:
            db_merge(self.session, source_path)
            col = Collection.get_by_name(self.session, "imported_col")
            self.assertIsNotNone(col)
            self.assertEqual(col.description, "From source DB")
        finally:
            os.unlink(source_path)


# ---------------------------------------------------------------------------
# Snippet versioning tests
# ---------------------------------------------------------------------------


class TestVersioning(BaseDBTest):
    """Tests for snippet version history."""

    def test_version_list_empty(self):
        """A snippet with no versions should return empty list."""
        snippet = snippet_add(self.session, "func", "RET")
        versions = snippet_version_list(self.session, snippet.checksum)
        self.assertEqual(len(versions), 0)

    def test_version_list_after_manual_insert(self):
        """Manually inserted versions should be retrievable."""
        snippet = snippet_add(self.session, "func", "RET")
        v = SnippetVersion(
            snippet_checksum=snippet.checksum,
            code="old code",
            minhash=snippet.minhash,
        )
        self.session.add(v)
        self.session.commit()
        versions = snippet_version_list(self.session, snippet.checksum)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["snippet_checksum"], snippet.checksum)


# ---------------------------------------------------------------------------
# ResemblConfig dict-compat tests
# ---------------------------------------------------------------------------


class TestResemblConfig(unittest.TestCase):
    """Tests for ResemblConfig dict-like interface."""

    def test_get_existing_key(self):
        cfg = ResemblConfig()
        self.assertEqual(cfg.get("top_n"), 5)

    def test_get_missing_key_returns_default(self):
        cfg = ResemblConfig()
        self.assertEqual(cfg.get("nonexistent", 42), 42)

    def test_contains(self):
        cfg = ResemblConfig()
        self.assertIn("lsh_threshold", cfg)
        self.assertNotIn("nonexistent", cfg)

    def test_getitem_setitem(self):
        cfg = ResemblConfig()
        cfg["top_n"] = 20
        self.assertEqual(cfg["top_n"], 20)

    def test_items(self):
        cfg = ResemblConfig()
        items = cfg.items()
        keys = [k for k, v in items]
        self.assertIn("lsh_threshold", keys)
        self.assertIn("top_n", keys)

    def test_update_from_dict(self):
        cfg = ResemblConfig()
        cfg.update({"top_n": 15, "format": "json"})
        self.assertEqual(cfg.get("top_n"), 15)
        self.assertEqual(cfg.get("format"), "json")

    def test_update_from_config(self):
        cfg1 = ResemblConfig(top_n=100)
        cfg2 = ResemblConfig()
        cfg2.update(cfg1)
        self.assertEqual(cfg2.get("top_n"), 100)

    def test_clear(self):
        cfg = ResemblConfig(top_n=99, lsh_threshold=0.9)
        cfg.clear()
        self.assertEqual(cfg.get("top_n"), 5)
        self.assertEqual(cfg.get("lsh_threshold"), 0.5)

    def test_to_dict(self):
        cfg = ResemblConfig()
        d = cfg.to_dict()
        self.assertIsInstance(d, dict)
        self.assertEqual(d["top_n"], 5)


# ---------------------------------------------------------------------------
# Model method tests
# ---------------------------------------------------------------------------


class TestModelMethods(BaseDBTest):
    """Tests for model class methods not covered elsewhere."""

    def test_snippet_get_by_name(self):
        """Snippet.get_by_name should find snippets by their alias."""
        snippet_add(self.session, "my_function", "PUSH EBP; MOV EBP, ESP")
        found = Snippet.get_by_name(self.session, "my_function")
        self.assertIsNotNone(found)

    def test_snippet_get_by_collection_empty(self):
        """get_by_collection should return empty for nonexistent collection."""
        results = Snippet.get_by_collection(self.session, "none")
        self.assertEqual(len(results), 0)

    def test_collection_get_all(self):
        """Collection.get_all should return all collections."""
        collection_create(self.session, "a")
        collection_create(self.session, "b")
        all_cols = Collection.get_all(self.session)
        self.assertEqual(len(all_cols), 2)

    def test_snippet_version_get_by_checksum(self):
        """SnippetVersion.get_by_checksum should return versions newest first."""
        snippet = snippet_add(self.session, "func", "NOP")
        v1 = SnippetVersion(
            snippet_checksum=snippet.checksum,
            code="v1",
            minhash=snippet.minhash,
            created_at="2024-01-01T00:00:00",
        )
        v2 = SnippetVersion(
            snippet_checksum=snippet.checksum,
            code="v2",
            minhash=snippet.minhash,
            created_at="2025-01-01T00:00:00",
        )
        self.session.add_all([v1, v2])
        self.session.commit()
        versions = SnippetVersion.get_by_checksum(self.session, snippet.checksum)
        self.assertEqual(len(versions), 2)
        # Newest first
        self.assertEqual(versions[0].code, "v2")


if __name__ == "__main__":
    unittest.main()
