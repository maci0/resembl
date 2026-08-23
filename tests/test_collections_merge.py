"""Tests for collections, versioning, merge, tags, search, and config dict-compat."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from resembl.config import ResemblConfig
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
from resembl.models import Collection, Snippet, SnippetVersion, timestamp_normalize


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
# Timestamp normalization tests
# ---------------------------------------------------------------------------


class TestTimestampNormalize(unittest.TestCase):
    """Tests for the canonical created_at form used for string ordering."""

    def test_aware_utc_passthrough(self):
        """Already-canonical UTC values round-trip unchanged."""
        self.assertEqual(
            timestamp_normalize("2024-06-01T10:00:00+00:00"), "2024-06-01T10:00:00+00:00"
        )

    def test_offset_converted_to_utc(self):
        """A foreign offset is re-expressed in UTC so ordering stays valid.

        ``SnippetVersion.get_by_checksum`` orders by raw string comparison,
        which matches chronological order only when every value carries the
        same offset; a verbatim +02:00 value would sort wrongly against
        +00:00 rows.
        """
        self.assertEqual(
            timestamp_normalize("2024-06-01T12:00:00+02:00"), "2024-06-01T10:00:00+00:00"
        )

    def test_naive_interpreted_as_utc(self):
        """Naive values (legacy writer format) are stamped as UTC, not shifted."""
        self.assertEqual(timestamp_normalize("2024-06-01T10:00:00"), "2024-06-01T10:00:00+00:00")

    def test_unparseable_returned_verbatim(self):
        """Garbage input is never fabricated into a timestamp."""
        self.assertEqual(timestamp_normalize("not-a-date"), "not-a-date")


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

    def test_merge_normalizes_foreign_created_at(self):
        """Imported collection timestamps are re-expressed in UTC.

        A source database written with a non-UTC offset (or naively) must
        not leak verbatim into the local DB: rows are ordered by string
        comparison elsewhere, which only matches chronological order while
        every stored value carries the same offset.
        """
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        source_engine = create_engine(f"sqlite:///{tmp.name}")
        SQLModel.metadata.create_all(source_engine)
        with Session(source_engine) as src_session:
            src_session.add(
                Collection(
                    name="offset_col",
                    description="d",
                    created_at="2024-06-01T12:00:00+02:00",
                )
            )
            src_session.commit()
        source_engine.dispose()
        try:
            result = db_merge(self.session, tmp.name)
            self.assertEqual(result["added"], 0)
            merged = Collection.get_by_name(self.session, "offset_col")
            self.assertIsNotNone(merged)
            self.assertEqual(merged.created_at, "2024-06-01T10:00:00+00:00")
        finally:
            os.unlink(tmp.name)

    def test_merge_new_snippets(self):
        """Merging a source with unique snippets should add them."""
        source_path = self._create_source_db(
            [
                ("func_a", "MOV EAX, 1", [], None),
                ("func_b", "MOV EBX, 2", [], None),
            ]
        )
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
        source_path = self._create_source_db(
            [
                ("func_a", "MOV EAX, 1", [], None),
            ]
        )
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["added"], 0)
            self.assertEqual(result["skipped"], 1)
        finally:
            os.unlink(source_path)

    def test_alias_only_merge_keeps_fingerprint_stamps(self):
        """A merge that adds no snippets leaves the fingerprint stamps alone.

        The stamps vouch for the whole stored fingerprint population; only
        new rows can change it (merge updates touch names/tags only).
        Clearing them on an alias-only merge would force the next ``find``
        into a full reindex for nothing.
        """
        from resembl.lsh import (
            fingerprint_ngram_get,
            fingerprint_ngram_set,
            fingerprint_perm_get,
            fingerprint_perm_set,
            fingerprint_version_get,
            fingerprint_version_set,
        )
        from resembl.models import FINGERPRINT_VERSION

        fingerprint_version_set(self.session, FINGERPRINT_VERSION)
        fingerprint_ngram_set(self.session, 3)
        fingerprint_perm_set(self.session, 128)
        snippet_add(self.session, "original", "MOV EAX, 1")
        source_path = self._create_source_db([("alias", "MOV EAX, 1", [], None)])
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["added"], 0)
            self.assertEqual(result["updated"], 1)
            self.assertEqual(fingerprint_version_get(self.session), FINGERPRINT_VERSION)
            self.assertEqual(fingerprint_ngram_get(self.session), 3)
            self.assertEqual(fingerprint_perm_get(self.session), 128)
        finally:
            os.unlink(source_path)

    def test_merge_with_additions_clears_fingerprint_stamps(self):
        """A merge that lands new rows still clears the stamps.

        Source blobs are copied verbatim and may carry a foreign format or
        permutation count, so the stamps must go whenever anything was
        added; the next ``find`` then reindexes once and normalizes.
        """
        from resembl.lsh import (
            fingerprint_ngram_get,
            fingerprint_ngram_set,
            fingerprint_perm_get,
            fingerprint_perm_set,
            fingerprint_version_get,
            fingerprint_version_set,
        )
        from resembl.models import FINGERPRINT_VERSION

        fingerprint_version_set(self.session, FINGERPRINT_VERSION)
        fingerprint_ngram_set(self.session, 3)
        fingerprint_perm_set(self.session, 128)
        source_path = self._create_source_db([("new_func", "MOV ECX, 9", [], None)])
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["added"], 1)
            self.assertIsNone(fingerprint_version_get(self.session))
            self.assertIsNone(fingerprint_ngram_get(self.session))
            self.assertIsNone(fingerprint_perm_get(self.session))
        finally:
            os.unlink(source_path)

    def test_merge_adds_new_names(self):
        """Merging should add new names to existing snippets."""
        snippet_add(self.session, "original_name", "MOV EAX, 1")
        source_path = self._create_source_db(
            [
                ("alias_name", "MOV EAX, 1", [], None),
            ]
        )
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

    def test_merge_preserves_primary_name(self):
        """A merge must not reassign the snippet's primary name.

        ``name_list[0]`` is the primary name: it drives display, export
        filenames, and YARA rule names.  New aliases are appended after the
        existing names (same convention as ``snippet_add_batch``), so the
        original primary name keeps its slot instead of being reordered by a
        sorted union.
        """
        snippet_add(self.session, "zeta_primary", "MOV EAX, 2")
        source_path = self._create_source_db(
            [
                ("alpha_alias", "MOV EAX, 2", [], None),
                ("beta_alias", "MOV EAX, 2", [], None),
            ]
        )
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["updated"], 1)
            s = snippet_get(self.session, string_checksum("MOV EAX, 2"))
            self.assertEqual(s.name_list[0], "zeta_primary")
            self.assertEqual(s.name_list[1:], ["alpha_alias", "beta_alias"])
        finally:
            os.unlink(source_path)

    def test_merge_adds_new_tags_independently(self):
        """Merging should add tags even if names didn't change (bug fix verification)."""
        snippet = snippet_add(self.session, "func", "MOV EAX, 1")
        source_path = self._create_source_db(
            [
                ("func", "MOV EAX, 1", ["new_tag"], None),
            ]
        )
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["updated"], 1)
            s = snippet_get(self.session, snippet.checksum)
            self.assertIn("new_tag", s.tag_list)
        finally:
            os.unlink(source_path)

    def test_merge_overlap_batches_local_lookups(self):
        """Merging heavily-overlapping databases must not do one SELECT per overlap.

        The overlap path used ``session.get`` once per matching checksum (an
        N+1 when consolidating two databases that mostly contain the same
        content).  Local rows are now fetched in chunked IN batches; assert
        the local SELECT count stays bounded while merging 120 overlaps.
        """
        import sqlalchemy.event

        # Seed the local database with 120 snippets.
        for i in range(120):
            snippet_add(self.session, f"s{i}", f"PUSH EBP\nMOV EAX, {i}\nPOP EBP\nRET")

        # Source: the same 120 codes under new names (forces merges) + 10 new.
        source_snippets = [
            (f"s{i}_src", f"PUSH EBP\nMOV EAX, {i}\nPOP EBP\nRET", [], None) for i in range(120)
        ] + [(f"t{i}", f"PUSH ECX\nMOV EBX, {i}\nPOP ECX\nRET", [], None) for i in range(10)]
        source_path = self._create_source_db(source_snippets)

        counts = {"select": 0}

        @sqlalchemy.event.listens_for(self.engine, "after_cursor_execute")
        # Fixed SQLAlchemy listener signature; only `statement` is needed.
        def _count_selects(
            conn, cursor, statement, parameters, context, executemany
        ):  # pylint: disable=unused-argument
            if statement.lstrip().upper().startswith("SELECT"):
                counts["select"] += 1

        try:
            result = db_merge(self.session, source_path)
        finally:
            sqlalchemy.event.remove(self.engine, "after_cursor_execute", _count_selects)
            os.unlink(source_path)

        self.assertEqual(result["added"], 10)
        self.assertEqual(result["updated"], 120)
        # 120 overlaps resolved via ~1 chunked IN fetch, not 120 SELECTs.
        self.assertLessEqual(counts["select"], 6)

    def test_merge_heals_corrupt_source_blob_from_code(self):
        """A corrupt source blob is recomputed from its code, never deserialized.

        Source databases are untrusted input: a non-packed fingerprint blob
        (corrupt or hostile pickle) must not be unpickled.  The row is healed
        by recomputing the fingerprint from the source row's code instead of
        skipping it.
        """
        source_path = self._create_source_db(
            [
                ("a", "MOV EAX, 1", [], None),
                ("b", "MOV EBX, 2", [], None),
                ("c", "MOV ECX, 3", [], None),
            ]
        )
        try:
            src_engine = create_engine(f"sqlite:///{source_path}")
            with Session(src_engine) as ss:
                row = ss.exec(select(Snippet)).first()
                row.minhash = b"corrupt-blob"
                corrupted_checksum = row.checksum
                ss.add(row)
                ss.commit()
            src_engine.dispose()

            result = db_merge(self.session, source_path)
            self.assertEqual(result["added"], 3)  # every row imported
            self.assertEqual(result["skipped"], 0)
            healed = Snippet.get_by_checksum(self.session, corrupted_checksum)
            self.assertIsNotNone(healed)
            self.assertTrue(healed.minhash.startswith(b"RMLH"))
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
            id=1,
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

    def test_snippet_get_by_name_special_characters(self):
        """Names with quotes, backslashes, or LIKE wildcards stay findable.

        The stored names are JSON-encoded, so a name containing ``"`` is
        persisted as ``\\"``: a probe built from the raw name could never
        match its own stored form.  ``%`` and ``_`` must match themselves,
        not widen the SQL pattern.
        """
        for name in ('quote"name', "back\\slash", "percent%name", "under_score"):
            snippet_add(self.session, name, f"NOP  # {name}")
            found = Snippet.get_by_name(self.session, name)
            self.assertIsNotNone(found, name)

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
            id=1,
            snippet_checksum=snippet.checksum,
            code="v1",
            minhash=snippet.minhash,
            created_at="2024-01-01T00:00:00+00:00",
        )
        v2 = SnippetVersion(
            id=2,
            snippet_checksum=snippet.checksum,
            code="v2",
            minhash=snippet.minhash,
            created_at="2025-01-01T00:00:00+00:00",
        )
        self.session.add_all([v1, v2])
        self.session.commit()
        versions = SnippetVersion.get_by_checksum(self.session, snippet.checksum)
        self.assertEqual(len(versions), 2)
        # Newest first
        self.assertEqual(versions[0].code, "v2")


class TestDBMergeFailure(unittest.TestCase):
    """Error paths of ``db_merge``: partial state stays usable and healable."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        SQLModel.metadata.drop_all(self.engine)

    def _raw_row(self, i: int) -> dict:
        """Build one raw source row dict (no lexing — cheap at 5000+ rows)."""
        from resembl.minhash import MinHash
        from resembl.scoring import minhash_pack

        return {
            "checksum": f"{i:064x}",
            "names": json.dumps([f"snippet_{i}"]),
            "code": f"MOV EAX, {i}",
            "minhash": minhash_pack(MinHash(num_perm=128)),
        }

    def _create_raw_source_db(self, rows) -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        engine = create_engine(f"sqlite:///{tmp.name}")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as src_session:
            for kwargs in rows:
                src_session.add(Snippet(**kwargs))
            src_session.commit()
        engine.dispose()
        return tmp.name

    def test_merge_failure_rolls_back_and_clears_stamps(self):
        """A crash between committed chunks leaves a usable, healable database.

        ``flush_new_rows`` commits each filled chunk, so a failure midway
        strands earlier chunks in the destination.  The error handler must
        roll back the aborted transaction (the caller's session keeps
        working) and clear the fingerprint stamps whenever rows landed:
        the copied blobs may be foreign-format, and an unchanged stamp
        would vouch for them — every later find would skip those snippets
        as stale instead of healing them with one reindex.
        """
        from sqlmodel import func

        from resembl.lsh import (
            fingerprint_ngram_get,
            fingerprint_ngram_set,
            fingerprint_perm_set,
            fingerprint_version_get,
            fingerprint_version_set,
        )
        from resembl.models import FINGERPRINT_VERSION

        rows = [self._raw_row(i) for i in range(5001)]
        source_path = self._create_raw_source_db(rows)
        fingerprint_version_set(self.session, FINGERPRINT_VERSION)
        fingerprint_ngram_set(self.session, 3)
        fingerprint_perm_set(self.session, 128)

        import resembl.core as core_mod

        real_insert = core_mod._insert_snippet_rows
        calls = {"n": 0}

        def flaky_insert(session, insert_rows, batch_size=500):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated disk full")
            real_insert(session, insert_rows, batch_size)

        try:
            with patch.object(core_mod, "_insert_snippet_rows", side_effect=flaky_insert):
                result = db_merge(self.session, source_path)
            self.assertIn("error", result)
            # The first chunk (merge_flush_size rows) was committed before
            # the failure; it must still be readable through this session.
            count = self.session.exec(select(func.count(Snippet.checksum))).one()
            self.assertEqual(count, 5000)
            # Rows landed, so the stamps must have been cleared: the next
            # find reindexes once over the partial merge.
            self.assertIsNone(fingerprint_version_get(self.session))
            self.assertIsNone(fingerprint_ngram_get(self.session))
        finally:
            os.unlink(source_path)

    def test_merge_skips_corrupt_metadata_rows(self):
        """Source rows whose names/tags are not JSON arrays are skipped.

        The columns are copied verbatim into the destination; one poisoned
        row would otherwise crash every later read of the merged snippet
        (and abort future merges touching it) with a raw JSONDecodeError.
        """
        good = self._raw_row(1)
        bad_names = self._raw_row(2)
        bad_names["names"] = "not json"
        bad_tags = self._raw_row(3)
        bad_tags["tags"] = "{nope"
        source_path = self._create_raw_source_db([good, bad_names, bad_tags])

        # A local row sharing the corrupt-names checksum exercises the
        # existing-row branch of the merge as well as the new-row one.
        self.session.add(
            Snippet(
                checksum=f"{2:064x}",
                names=json.dumps(["local_2"]),
                code="MOV EAX, 999",
                minhash=self._raw_row(2)["minhash"],
            )
        )
        self.session.commit()
        try:
            result = db_merge(self.session, source_path)
            self.assertEqual(result["added"], 1)
            self.assertEqual(result["skipped"], 2)
            # Every stored names column still parses: reads cannot crash
            # on metadata, and the pre-existing row kept its own names.
            stored = {
                checksum: json.loads(names)
                for checksum, names in self.session.exec(
                    select(Snippet.checksum, Snippet.names)
                ).all()
            }
            self.assertIn("local_2", stored[f"{2:064x}"])
        finally:
            os.unlink(source_path)


if __name__ == "__main__":
    unittest.main()
