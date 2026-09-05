"""Targeted tests aiming for 100% coverage of core.py, cache.py, database.py.

Each test class targets specific uncovered lines identified via coverage analysis.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from resembl.cache import (
    lsh_cache_load,
    lsh_cache_save,
    lsh_index_build,
)
from resembl.core import (
    NUM_PERMUTATIONS,
    code_create_minhash,
    code_create_minhash_batch,
    collection_add_snippet,
    collection_create,
    collection_delete,
    collection_remove_snippet,
    db_calculate_average_similarity,
    db_merge,
    db_stats,
    snippet_add,
    snippet_delete,
    snippet_export,
    snippet_export_yara,
    snippet_find_matches,
    snippet_get,
    snippet_name_add,
    snippet_name_remove,
    snippet_tag_add,
    snippet_tag_remove,
)
from resembl.database import db_create
from resembl.minhash import MinHash
from resembl.models import Collection


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
# Tag edge cases — covers lines 300-302, 307, 328
# ---------------------------------------------------------------------------


class TestTagEdgeCases(BaseDBTest):
    """Tests for tag add/remove edge cases (empty tags, quiet mode)."""

    def test_tag_add_empty_string(self):
        """Adding an empty tag should return None (line 300-302)."""
        snippet = snippet_add(self.session, "func", "NOP")
        result = snippet_tag_add(self.session, snippet.checksum, "")
        self.assertIsNone(result)

    def test_tag_add_whitespace_only(self):
        """Adding a whitespace-only tag should return None."""
        snippet = snippet_add(self.session, "func", "NOP")
        result = snippet_tag_add(self.session, snippet.checksum, "   ")
        self.assertIsNone(result)

    def test_tag_add_empty_quiet(self):
        """Adding an empty tag in quiet mode should return None without logging."""
        snippet = snippet_add(self.session, "func", "NOP")
        result = snippet_tag_add(self.session, snippet.checksum, "", quiet=True)
        self.assertIsNone(result)

    def test_tag_add_nonexistent_not_quiet(self):
        """Adding a tag with quiet=False to nonexistent snippet should log (line 307)."""
        with self.assertLogs("resembl.core", level="ERROR"):
            result = snippet_tag_add(self.session, "bad_checksum", "tag", quiet=False)
        self.assertIsNone(result)

    def test_tag_remove_nonexistent_not_quiet(self):
        """Removing a tag from nonexistent snippet with quiet=False should log (line 328)."""
        with self.assertLogs("resembl.core", level="ERROR"):
            result = snippet_tag_remove(self.session, "bad_checksum", "tag", quiet=False)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Batch MinHash edge cases — covers lines 420-425
# ---------------------------------------------------------------------------


class TestBatchMinhashEdgeCases(BaseDBTest):
    """Tests for code_create_minhash_batch with short/empty snippets."""

    def test_batch_minhash_empty_code(self):
        """Empty code must yield a MinHash identical to the single path (line 420-421)."""
        results = code_create_minhash_batch([""], normalize=True, ngram_size=3)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], MinHash)
        self.assertEqual(results[0].jaccard(code_create_minhash("")), 1.0)

    def test_batch_minhash_short_tokens(self):
        """Code shorter than ngram_size must match the single path (line 422-425)."""
        results = code_create_minhash_batch(["NOP"], normalize=True, ngram_size=3)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], MinHash)
        self.assertEqual(results[0].jaccard(code_create_minhash("NOP")), 1.0)

    def test_batch_minhash_tokens_equal_to_ngram(self):
        """Code with tokens == ngram_size should use shingles."""
        results = code_create_minhash_batch(["MOV EAX, 1"], normalize=True, ngram_size=3)
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].jaccard(code_create_minhash("MOV EAX, 1", ngram_size=3)),
            1.0,
        )


# ---------------------------------------------------------------------------
# snippet_find_matches with empty DB — covers line 496
# ---------------------------------------------------------------------------


class TestFindMatchesEmpty(BaseDBTest):
    """snippet_find_matches edge cases."""

    def test_find_matches_empty_db(self):
        """Finding matches in an empty DB should return 0 candidates (line 496)."""
        num, matches = snippet_find_matches(self.session, "MOV EAX, 1", top_n=5, threshold=0.5)
        self.assertEqual(num, 0)
        self.assertEqual(len(matches), 0)

    def test_find_failed_build_raises(self):
        """A failed index build raises IndexBuildError instead of faking zero matches."""
        from unittest.mock import patch

        from resembl.core import IndexBuildError

        with (
            patch("resembl.core.lsh_cache_load", return_value=None),
            patch("resembl.core.lsh_index_build", return_value=None),
        ):
            with self.assertRaises(IndexBuildError):
                snippet_find_matches(self.session, "MOV EAX, 1", top_n=5)

    def test_find_failed_migration_reindex_raises(self):
        """A failed migration reindex raises instead of indexing stale blobs.

        ``db_reindex`` signals failure through its result dict (e.g. its
        index clear lost the race for the database lock).  Ignoring the
        signal let ``find`` continue: the subsequent build indexed the
        stale fingerprints and stamped them current, permanently masking
        the pending migration.
        """
        from unittest.mock import patch

        from resembl.core import IndexBuildError

        # Fresh in-memory database: no version stamp, so a migration
        # reindex is always required on the first find.
        with patch(
            "resembl.core.db_reindex",
            return_value={"error": "could not clear the index (locked)"},
        ) as mock:
            with self.assertRaises(IndexBuildError) as ctx:
                snippet_find_matches(self.session, "MOV EAX, 1", top_n=5)
        mock.assert_called_once()
        self.assertIn("could not clear the index (locked)", str(ctx.exception))


# ---------------------------------------------------------------------------
# snippet_export_yara — covers lines 544-580
# ---------------------------------------------------------------------------


class TestExportYara(BaseDBTest):
    """Tests for snippet_export_yara."""

    def test_export_yara_basic(self):
        """Exporting a snippet to YARA should produce a valid rule file (line 544-580)."""
        snippet_add(self.session, "test_func", "MOV EAX, 1; RET")
        with tempfile.NamedTemporaryFile(suffix=".yar", delete=False, mode="w") as f:
            out_path = f.name
        try:
            result = snippet_export_yara(self.session, out_path)
            self.assertEqual(result["num_exported"], 1)
            with open(out_path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("rule resembl_test_func", content)
            self.assertIn("$asm", content)
        finally:
            os.unlink(out_path)

    def test_export_yara_special_chars(self):
        """YARA export should escape special characters in code."""
        snippet_add(self.session, "esc_test", 'MOV EAX, "hello\\nworld"')
        with tempfile.NamedTemporaryFile(suffix=".yar", delete=False, mode="w") as f:
            out_path = f.name
        try:
            result = snippet_export_yara(self.session, out_path)
            self.assertEqual(result["num_exported"], 1)
            with open(out_path, encoding="utf-8") as f:
                content = f.read()
            # Backslashes and quotes should be escaped
            self.assertNotIn('\n"', content.split("$asm")[1].split("nocase")[0])
        finally:
            os.unlink(out_path)

    def test_export_yara_numeric_first_char(self):
        """YARA rule names starting with a digit should be prefixed (line 552-553)."""
        snippet_add(self.session, "123invalid", "RET")
        with tempfile.NamedTemporaryFile(suffix=".yar", delete=False, mode="w") as f:
            out_path = f.name
        try:
            snippet_export_yara(self.session, out_path)
            with open(out_path, encoding="utf-8") as f:
                content = f.read()
            # Should start with "rule resembl_r_"
            self.assertIn("rule resembl_r_", content)
        finally:
            os.unlink(out_path)

    def test_export_yara_empty_db(self):
        """Exporting from empty DB should produce empty file."""
        with tempfile.NamedTemporaryFile(suffix=".yar", delete=False, mode="w") as f:
            out_path = f.name
        try:
            result = snippet_export_yara(self.session, out_path)
            self.assertEqual(result["num_exported"], 0)
            self.assertEqual(result["avg_time_per_snippet"], 0)
        finally:
            os.unlink(out_path)

    def test_export_yara_name_cannot_inject_rule_syntax(self):
        """Names are user-controlled: they must stay inside the quoted string.

        A name containing quotes/backslashes/newlines used to be embedded
        raw in ``description = "..."``, letting stored names inject
        arbitrary rule syntax into the generated YARA file.
        """
        hostile = 'evil", threat = 100 //\nrule injected {condition: false}'
        snippet_add(self.session, hostile, "MOV EAX, 1; RET")
        with tempfile.NamedTemporaryFile(suffix=".yar", delete=False, mode="w") as f:
            out_path = f.name
        try:
            result = snippet_export_yara(self.session, out_path)
            self.assertEqual(result["num_exported"], 1)
            with open(out_path, encoding="utf-8") as f:
                content = f.read()
            # Exactly one rule header exists: the injected newline stayed
            # escaped inside the quoted string instead of starting a line.
            rule_lines = [ln for ln in content.splitlines() if ln.startswith("rule ")]
            self.assertEqual(len(rule_lines), 1)
            self.assertTrue(rule_lines[0].startswith("rule resembl_"))
            # The hostile characters are escaped within the string literal.
            self.assertIn('\\"', content)
            self.assertIn("\\n", content)
        finally:
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# Average similarity — covers line 660
# ---------------------------------------------------------------------------


class TestAverageSimilarity(BaseDBTest):
    """Tests for db_calculate_average_similarity."""

    def test_avg_similarity_one_snippet(self):
        """With one snippet, should return 1.0."""
        snippet_add(self.session, "func", "NOP")
        result = db_calculate_average_similarity(self.session)
        self.assertEqual(result, 1.0)

    def test_avg_similarity_many_snippets(self):
        """With >2 snippets, should return a float between 0 and 1 (line 660)."""
        snippet_add(self.session, "func1", "MOV EAX, 1")
        snippet_add(self.session, "func2", "XOR EBX, EBX")
        snippet_add(self.session, "func3", "PUSH EBP; LEA ECX, [ESP+4]")
        result = db_calculate_average_similarity(self.session, sample_size=2)
        self.assertIsInstance(result, float)
        self.assertGreaterEqual(result, 0.0)
        self.assertLessEqual(result, 1.0)


# ---------------------------------------------------------------------------
# snippet_export — covers lines 735, 740-744
# ---------------------------------------------------------------------------


class TestSnippetExport(BaseDBTest):
    """Tests for snippet_export to directory."""

    def test_export_basic(self):
        """Exporting snippets should write .asm files (line 735-747)."""
        snippet_add(self.session, "func_a", "NOP\nRET")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = snippet_export(self.session, tmpdir)
            self.assertEqual(result["num_exported"], 1)
            files = os.listdir(tmpdir)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].endswith(".asm"))

    def test_export_empty_db(self):
        """Exporting from empty DB should write no files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = snippet_export(self.session, tmpdir)
            self.assertEqual(result["num_exported"], 0)

    def test_export_traversal_names_stay_inside_directory(self):
        """Hostile snippet names must not write outside the export dir."""
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            tempfile.TemporaryDirectory() as sibling,
        ):
            for i, hostile in enumerate(("../evil", "..\\..\\evil", "sub/dir/evil")):
                snippet_add(self.session, hostile, f"MOV EAX, {i}")
            result = snippet_export(self.session, tmpdir)
            # Every file landed as a direct child of the export directory.
            written = os.listdir(tmpdir)
            self.assertEqual(result["num_exported"], len(written))
            self.assertTrue(all(f.endswith(".asm") for f in written))
            # Nothing escaped to a prefix-sharing sibling directory.
            self.assertEqual(os.listdir(sibling), [])


# ---------------------------------------------------------------------------
# Collection error paths — covers lines 800, 835, 841, 858
# ---------------------------------------------------------------------------


class TestCollectionErrorPaths(BaseDBTest):
    """Tests for collection functions error handling in non-quiet mode."""

    def test_collection_delete_nonexistent_not_quiet(self):
        """Deleting a nonexistent collection with quiet=False should log (line 800)."""
        with self.assertLogs("resembl.core", level="ERROR"):
            result = collection_delete(self.session, "nope", quiet=False)
        self.assertFalse(result)

    def test_collection_add_snippet_nonexistent_collection_not_quiet(self):
        """Adding to a nonexistent collection with quiet=False should log (line 835)."""
        snippet = snippet_add(self.session, "func", "NOP")
        with self.assertLogs("resembl.core", level="ERROR"):
            result = collection_add_snippet(self.session, "bad_col", snippet.checksum, quiet=False)
        self.assertIsNone(result)

    def test_collection_add_snippet_nonexistent_snippet_not_quiet(self):
        """Adding a nonexistent snippet with quiet=False should log (line 841)."""
        collection_create(self.session, "col")
        with self.assertLogs("resembl.core", level="ERROR"):
            result = collection_add_snippet(self.session, "col", "bad_checksum", quiet=False)
        self.assertIsNone(result)

    def test_collection_remove_snippet_nonexistent_not_quiet(self):
        """Removing a nonexistent snippet with quiet=False should log (line 858)."""
        with self.assertLogs("resembl.core", level="ERROR"):
            result = collection_remove_snippet(self.session, "bad_checksum", quiet=False)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# snippet_name_add / snippet_name_remove edge cases
# ---------------------------------------------------------------------------


class TestNameOperations(BaseDBTest):
    """Tests for snippet_name_add and snippet_name_remove."""

    def test_name_add_duplicate(self):
        """Adding an existing name should return None."""
        snippet = snippet_add(self.session, "original", "NOP")
        result = snippet_name_add(self.session, snippet.checksum, "original", quiet=True)
        self.assertIsNone(result)

    def test_name_remove_last_name(self):
        """Removing the last name must be refused, keeping the snippet named."""
        snippet = snippet_add(self.session, "only_name", "NOP")
        result = snippet_name_remove(self.session, snippet.checksum, "only_name")
        # The removal is rejected (None) and the name survives.
        self.assertIsNone(result)
        refreshed = snippet_get(self.session, snippet.checksum)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.name_list, ["only_name"])

    def test_name_add_nonexistent_snippet(self):
        """Adding a name to a nonexistent snippet should return None."""
        result = snippet_name_add(self.session, "bad_checksum", "name")
        self.assertIsNone(result)

    def test_name_remove_nonexistent_snippet(self):
        """Removing a name from a nonexistent snippet should return None."""
        result = snippet_name_remove(self.session, "bad_checksum", "name")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Merge edge cases — covers lines 911-913, 964-965
# ---------------------------------------------------------------------------


class TestMergeEdgeCases(BaseDBTest):
    """Additional merge tests for collection assignment and coverage."""

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
                    src_session.commit()
            for name, code, tags, col in snippets:
                s = snippet_add(src_session, name, code)
                if tags:
                    for t in tags:
                        snippet_tag_add(src_session, s.checksum, t)
                if col:
                    collection_add_snippet(src_session, col, s.checksum)
        source_engine.dispose()
        return tmp.name

    def test_merge_assigns_collection_to_existing(self):
        """Merging should assign source collection to existing snippet without one."""
        snippet = snippet_add(self.session, "func", "MOV EAX, 1")
        # Verify no collection
        self.assertIsNone(snippet.collection)

        source_path = self._create_source_db(
            snippets=[("func", "MOV EAX, 1", [], "src_col")],
            collections=[("src_col", "Source collection")],
        )
        try:
            db_merge(self.session, source_path)
            s = snippet_get(self.session, snippet.checksum)
            self.assertEqual(s.collection, "src_col")
        finally:
            os.unlink(source_path)

    def test_merge_does_not_overwrite_existing_collection(self):
        """Merging should NOT overwrite an existing snippet's collection."""
        collection_create(self.session, "my_col")
        snippet = snippet_add(self.session, "func", "MOV EAX, 1")
        collection_add_snippet(self.session, "my_col", snippet.checksum)

        source_path = self._create_source_db(
            snippets=[("func", "MOV EAX, 1", [], "other_col")],
            collections=[("other_col", "Other collection")],
        )
        try:
            db_merge(self.session, source_path)
            s = snippet_get(self.session, snippet.checksum)
            self.assertEqual(s.collection, "my_col")  # Should keep original
        finally:
            os.unlink(source_path)

    def test_merge_invalid_source(self):
        """Merging an invalid path (directory) should trigger error handling (lines 911-913)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # A directory can't be a SQLite DB — triggers the exception path
            result = db_merge(self.session, tmpdir)
            self.assertIn("error", result)


# ---------------------------------------------------------------------------
# Cache functions — index build corrupt-fingerprint handling
# ---------------------------------------------------------------------------


class TestCacheBuildCorruptFingerprints(BaseDBTest):
    """A corrupt stored fingerprint must not abort an index build."""

    def test_build_skips_corrupt_fingerprint_with_warning(self):
        """Corrupt blobs are skipped with a warning; good ones still index."""
        from resembl import cache as cache_mod

        good = snippet_add(self.session, "good", "MOV EAX, 1")
        bad = snippet_add(self.session, "bad", "XOR EBX, EBX")
        bad.minhash = b"garbage"
        with self.assertLogs(cache_mod.logger, level="WARNING"):
            lsh = lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS)
        self.assertIsNotNone(lsh)
        keys = lsh.query(good.minhash)
        self.assertIn(good.checksum, keys)
        self.assertNotIn(bad.checksum, keys)


# ---------------------------------------------------------------------------
# Database module — covers line 49
# ---------------------------------------------------------------------------


class TestDatabaseModule(unittest.TestCase):
    """Tests for database.py functions."""

    def test_db_create_runs(self):
        """db_create should create all tables in the configured engine (line 49)."""
        from unittest.mock import patch

        from sqlalchemy import inspect

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(f"sqlite:///{os.path.join(temp_dir, 'test.db')}")
            try:
                # Route the module-level engine into the temp DB so the
                # default `sqlite:///assembly.db` is never created in CWD.
                with patch("resembl.database._engine", engine):
                    db_create()
                tables = set(inspect(engine).get_table_names())
                self.assertGreater(len(tables), 0)
                # The tables every command depends on must exist.
                self.assertIn("snippet", tables)
                self.assertIn("lsh_bucket", tables)
                self.assertIn("lsh_meta", tables)
            finally:
                engine.dispose()


# ---------------------------------------------------------------------------
# db_stats — covers stat retrieval
# ---------------------------------------------------------------------------


class TestDBStats(BaseDBTest):
    """Tests for db_stats."""

    def test_stats_empty_db(self):
        """Stats on empty DB should return zeros."""
        stats = db_stats(self.session)
        self.assertEqual(stats["num_snippets"], 0)
        self.assertEqual(stats["avg_snippet_size"], 0)
        self.assertEqual(stats["vocabulary_size"], 0)

    def test_stats_with_data(self):
        """Stats with data should return correct counts."""
        snippet_add(self.session, "func1", "NOP")
        snippet_add(self.session, "func2", "RET")
        stats = db_stats(self.session)
        self.assertEqual(stats["num_snippets"], 2)
        self.assertGreater(stats["avg_snippet_size"], 0)
        self.assertGreater(stats["vocabulary_size"], 0)


# ---------------------------------------------------------------------------
# snippet_delete edge cases
# ---------------------------------------------------------------------------


class TestSnippetDeleteEdge(BaseDBTest):
    """Edge cases for snippet_delete."""

    def test_delete_nonexistent(self):
        """Deleting a nonexistent snippet should return False."""
        result = snippet_delete(self.session, "bad_checksum", quiet=True)
        self.assertFalse(result)

    def test_delete_with_collection(self):
        """Deleting a snippet assigned to a collection should succeed."""
        collection_create(self.session, "col")
        snippet = snippet_add(self.session, "func", "NOP")
        collection_add_snippet(self.session, "col", snippet.checksum)
        result = snippet_delete(self.session, snippet.checksum)
        self.assertTrue(result)
        self.assertIsNone(snippet_get(self.session, snippet.checksum))


# ---------------------------------------------------------------------------
# snippet_add deduplication
# ---------------------------------------------------------------------------


class TestSnippetAddDedup(BaseDBTest):
    """Tests for snippet_add deduplication behavior."""

    def test_add_duplicate_code(self):
        """Adding the same code should return existing snippet with merged names."""
        s1 = snippet_add(self.session, "name_a", "MOV EAX, 1")
        s2 = snippet_add(self.session, "name_b", "MOV EAX, 1")
        self.assertEqual(s1.checksum, s2.checksum)
        refreshed = snippet_get(self.session, s1.checksum)
        self.assertIn("name_a", refreshed.name_list)
        self.assertIn("name_b", refreshed.name_list)


# ---------------------------------------------------------------------------
# LSH cache lifecycle
# ---------------------------------------------------------------------------


class TestLSHCacheLifecycle(BaseDBTest):
    """Tests for LSH cache save/load flow."""

    def test_build_and_save_cache(self):
        """Building and saving cache should produce loadable index."""
        snippet_add(self.session, "func", "MOV EAX, 1")
        self.assertIsNotNone(lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS))
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RESEMBL_CACHE_DIR": tmpdir}):
                lsh_cache_save(self.session, 0.5)
                loaded = lsh_cache_load(self.session, 0.5)
                self.assertIsNotNone(loaded)


if __name__ == "__main__":
    unittest.main()
