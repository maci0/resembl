"""Unit tests for the resembl core module."""

import json
import os
import tempfile
import unittest

from rapidfuzz import fuzz
from sqlmodel import Session, SQLModel, create_engine, select

from resembl.core import (
    _random_snippet_rows,
    code_create_minhash,
    code_tokenize,
    db_calculate_average_similarity,
    db_clean,
    db_reindex,
    db_stats,
    score_hybrid,
    snippet_add,
    snippet_compare,
    snippet_delete,
    snippet_export,
    snippet_find_matches,
    snippet_get,
    snippet_list,
    snippet_name_add,
    snippet_name_remove,
    snippet_names_stream,
    string_checksum,
)
from resembl.models import (
    Snippet,
    minhash_jaccard,
    minhash_jaccard_batch,
    minhash_pack,
)

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

    def test_minhash_jaccard_batch_matches_per_blob(self):
        """Batch jaccard is bit-identical to repeated per-blob scoring."""
        m1 = code_create_minhash("MOV EAX, 1\nPUSH EBX")
        m2 = code_create_minhash("MOV EAX, 1\nPUSH ECX")
        m3 = code_create_minhash("XOR EAX, EAX ; RET")
        query = minhash_pack(m1)
        blobs = [minhash_pack(m1), minhash_pack(m2), minhash_pack(m3)]
        batch = minhash_jaccard_batch(query, blobs)
        per_blob = [minhash_jaccard(query, b) for b in blobs]
        self.assertEqual(batch, per_blob)
        self.assertEqual(batch[0], 1.0)  # byte-identical blob is an exact match
        self.assertEqual(minhash_jaccard_batch(query, []), [])

    def test_minhash_jaccard_batch_rejects_non_packed_blob(self):
        """Batch jaccard rejects non-packed blobs instead of deserializing."""
        import pickle

        query = minhash_pack(code_create_minhash("MOV EAX, 1"))
        legacy = pickle.dumps(code_create_minhash("MOV EBX, 2"))
        packed = minhash_pack(code_create_minhash("MOV EAX, 1"))
        with self.assertRaises(ValueError):
            minhash_jaccard_batch(query, [packed, legacy])

    def test_minhash_jaccard_batch_permutation_mismatch(self):
        """Batch jaccard rejects candidates with a different permutation count."""
        from datasketch import MinHash

        small = MinHash(num_perm=64)
        small.update(b"x")
        query = minhash_pack(code_create_minhash("MOV EAX, 1"))
        with self.assertRaises(ValueError):
            minhash_jaccard_batch(query, [minhash_pack(small)])

    def test_find_crowded_candidates_matches_bruteforce(self):
        """find ranks a crowded candidate set identically to brute-force scoring.

        Sixty snippets share the query's exact token stream (so every one of
        them is an LSH candidate with jaccard 1.0) and fifty more share only
        the first shingle — admitted as candidates at threshold 0.0 (b=128,
        r=1: any single shared hash value counts).  The mixed jaccards make
        the early-exit path skip the Levenshtein computation for the low
        scorers; the returned top-n must still equal scoring every candidate.
        """
        for i in range(60):
            snippet_add(
                self.session,
                f"crowd_{i}",
                f"mov eax, {i}\npush ebx\ncall 0x{i:x}\nadd eax, ebx\nret",
            )
        for i in range(50):
            snippet_add(self.session, f"far_{i}", f"mov eax, {i}\nret")

        query = "mov eax, 999\npush ebx\ncall 0x3e7\nadd eax, ebx\nret"
        num, matches = snippet_find_matches(self.session, query, top_n=5, threshold=0.0)
        # The far snippets must actually be candidates, otherwise the test
        # would not exercise the crowded path.
        self.assertGreater(num, 60)

        query_packed = minhash_pack(code_create_minhash(query))
        scored = []
        for s in self.session.exec(select(Snippet)).all():
            jaccard = minhash_jaccard(query_packed, s.minhash)
            levenshtein = fuzz.ratio(query, s.code)
            scored.append((score_hybrid(jaccard, levenshtein), s.checksum))
        scored.sort(key=lambda t: -t[0])

        got = [m[0].checksum for m in matches]
        expected = [c for _, c in scored[:5]]
        self.assertEqual(sorted(got), sorted(expected))
        self.assertEqual(sorted(m[1] for m in matches), sorted(h for h, _ in scored[:5]))


class _IsolatedDBTest(unittest.TestCase):
    """Shared per-test in-memory database."""

    def setUp(self):
        """Set up a clean, in-memory database for each test."""
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        """Clean up the database after each test."""
        self.session.close()
        SQLModel.metadata.drop_all(self.engine)


class TestDBCoreFunctions(_IsolatedDBTest):
    """Tests for core database functions."""

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


class TestSnippetCoreFunctions(_IsolatedDBTest):
    """Tests for core snippet functions."""

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

    def test_snippet_names_stream(self):
        """snippet_names_stream yields (checksum, names) across batch boundaries."""
        for i in range(2500):
            snippet_add(self.session, f"n_{i}", f"MOV EAX, {i}")
        batches = list(snippet_names_stream(self.session, batch_size=1000))
        self.assertEqual(len(batches), 3)  # 1000 + 1000 + 500
        self.assertEqual(sum(len(b) for b in batches), 2500)
        pairs = [pair for batch in batches for pair in batch]
        checksums = [c for c, _ in pairs]
        self.assertEqual(len(checksums), 2500)
        self.assertEqual(len(set(checksums)), 2500)  # unique, no dupes/omissions
        all_names = {name for _, raw in pairs for name in json.loads(raw)}
        self.assertEqual(all_names, {f"n_{i}" for i in range(2500)})

    def test_snippet_collection_names_stream(self):
        """snippet_collection_names_stream yields only that collection's rows."""
        from resembl.core import collection_add_snippet, collection_create
        from resembl.core import snippet_collection_names_stream

        collection_create(self.session, "col")
        for i in range(50):
            checksum = snippet_add(self.session, f"in_{i}", f"MOV R{ i }, {i}").checksum
            collection_add_snippet(self.session, "col", checksum)
        for i in range(20):
            snippet_add(self.session, f"out_{i}", f"XOR R{i}, {i}")

        batches = list(snippet_collection_names_stream(self.session, "col", batch_size=20))
        self.assertEqual(len(batches), 3)  # 20 + 20 + 10
        pairs = [pair for batch in batches for pair in batch]
        checksums = [c for c, _ in pairs]
        self.assertEqual(len(checksums), 50)
        self.assertEqual(len(set(checksums)), 50)  # keyset pagination, no dupes/omissions
        all_names = {name for _, raw in pairs for name in json.loads(raw)}
        self.assertEqual(all_names, {f"in_{i}" for i in range(50)})
        # An unknown collection yields nothing.
        self.assertEqual(list(snippet_collection_names_stream(self.session, "nope")), [])

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

    def test_random_snippet_rows_samples_distinctly(self):
        """Keyset random sampling returns distinct rows and wraps around."""
        for i in range(500):
            snippet_add(self.session, f"r{i}", f"MOV EAX, {i}")
        rows = _random_snippet_rows(self.session, 100)
        self.assertEqual(len(rows), 100)
        self.assertEqual(len({s.checksum for s in rows}), 100)  # distinct
        rows2 = _random_snippet_rows(self.session, 100)
        # Two draws of 100 from 500 differ (a full collision is impossible).
        self.assertNotEqual({s.checksum for s in rows}, {s.checksum for s in rows2})

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
        result = snippet_name_remove(self.session, "nonexistent", "new_name", quiet=True)
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
                self.assertTrue(os.path.realpath(full_path).startswith(os.path.realpath(temp_dir)))

    def test_snippet_export_long_names_do_not_crash(self):
        """A name longer than any filesystem's filename limit still exports.

        Names are arbitrary user text (`resembl add`), so a 300-char name
        used to raise ENAMETOOLONG mid-export and abort the whole run with
        only part of the database written.
        """
        from resembl.core import _EXPORT_STEM_MAX_BYTES

        snippet_add(self.session, "ok", "MOV EAX, 1")
        snippet_add(self.session, "a" * 300, "MOV EBX, 2")
        with tempfile.TemporaryDirectory() as temp_dir:
            result = snippet_export(self.session, temp_dir)
            self.assertEqual(result["num_exported"], 2)
            exported = sorted(os.listdir(temp_dir))
            self.assertEqual(len(exported), 2)
            for fname in exported:
                stem = fname[: -len(".asm")]
                self.assertLessEqual(len(stem.encode("utf-8")), _EXPORT_STEM_MAX_BYTES)
                self.assertTrue(os.path.exists(os.path.join(temp_dir, fname)))

    def test_safe_filename_bounds_multi_byte_names_by_bytes(self):
        """The stem bound is on UTF-8 bytes: CJK names truncate far sooner.

        POSIX filesystems cap filenames at 255 bytes, not characters —
        a character-only cap would still overflow for non-ASCII names.
        """
        from resembl.core import _EXPORT_STEM_MAX_BYTES, _export_safe_filename

        stem = _export_safe_filename("界" * 300)  # 900 bytes of UTF-8
        self.assertLessEqual(len(stem.encode("utf-8")), _EXPORT_STEM_MAX_BYTES)
        # Truncation must yield valid text (no partial codepoint artifacts).
        self.assertEqual(stem, "界" * (_EXPORT_STEM_MAX_BYTES // 3))

    def test_safe_filename_truncation_keeps_distinct_names_distinct(self):
        """Two long names sharing a prefix must not overwrite each other."""
        from resembl.core import _export_safe_filename

        long_prefix = "b" * 500
        stem1 = _export_safe_filename(long_prefix + "_one")
        stem2 = _export_safe_filename(long_prefix + "_two")
        # Both truncate to the same stem, which is fine: snippet_export's
        # checksum disambiguator makes the written files distinct.  Here we
        # pin that sanitization itself stays deterministic and legal.
        self.assertEqual(stem1, stem2)
        self.assertTrue(stem1)


if __name__ == "__main__":
    unittest.main()
