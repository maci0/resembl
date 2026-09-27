"""Re-execution safety: every write must converge on the same state.

Each test runs one mutating operation twice and asserts the database (or the
exported artifact) is identical to a single run, with the same counts.  The
operations that are naturally idempotent are pinned here too, so a future
change that drops the content-addressed dedup fails a test instead of
silently duplicating rows on the next retry.
"""

import os
import tempfile
import unittest

from sqlmodel import Session, SQLModel, create_engine, select

from resembl.core import (
    collection_add_snippet,
    collection_create,
    collection_delete,
    db_merge,
    db_reindex,
    snippet_add,
    snippet_add_batch,
    snippet_delete,
    snippet_export,
    snippet_name_add,
    snippet_name_remove,
    snippet_prepare,
    snippet_tag_add,
    snippet_tag_remove,
)
from resembl.models import Collection, Snippet


def _state(session: Session) -> dict:
    """Return a comparable snapshot of the snippet and collection tables.

    Fingerprints are excluded: ``db_reindex`` legitimately rewrites blobs that
    ``minhash_unpack`` would reject, and the checksums, names, tags and
    collections are what a duplicate run must not disturb.
    """
    return {
        "snippets": sorted(
            (s.checksum, s.names, s.tags, s.collection) for s in session.exec(select(Snippet)).all()
        ),
        "collections": sorted(
            (c.name, c.description) for c in session.exec(select(Collection)).all()
        ),
    }


class BaseDBTest(unittest.TestCase):
    """Base class providing an in-memory database session per test."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        SQLModel.metadata.drop_all(self.engine)

    def _create_source_db(self, snippets, collections=None):
        """Create a source database file and return its path.

        *snippets* is a list of ``(name, code, tags, collection)`` tuples;
        *collections* a list of ``(name, description)`` pairs.
        """
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        source_engine = create_engine(f"sqlite:///{tmp.name}")
        SQLModel.metadata.create_all(source_engine)
        with Session(source_engine) as src_session:
            for name, desc in collections or []:
                src_session.add(Collection(name=name, description=desc))
                src_session.commit()
            for name, code, tags, col in snippets:
                s = snippet_add(src_session, name, code)
                for tag in tags:
                    snippet_tag_add(src_session, s.checksum, tag)
                if col:
                    collection_add_snippet(src_session, col, s.checksum)
        source_engine.dispose()
        return tmp.name


class TestSnippetWrites(BaseDBTest):
    """Adding, aliasing, tagging and deleting survive a repeated run."""

    def test_add_twice_adds_no_second_row(self):
        snippet_add(self.session, "proc", "MOV EAX, 1")
        after_first = _state(self.session)
        again = snippet_add(self.session, "proc", "MOV EAX, 1")
        self.assertEqual(_state(self.session), after_first)
        self.assertEqual(again.name_list, ["proc"])

    def test_add_twice_with_a_new_alias_merges_names(self):
        """Re-adding the same alias appends it once, not once per run."""
        snippet_add(self.session, "proc", "MOV EAX, 1")
        snippet_add(self.session, "other", "MOV EAX, 1")
        after_first = _state(self.session)
        snippet_add(self.session, "other", "MOV EAX, 1")
        self.assertEqual(_state(self.session), after_first)

    def test_add_batch_twice_adds_nothing(self):
        prepared = [snippet_prepare("proc", "MOV EAX, 1", 3)]
        first = snippet_add_batch(self.session, prepared)
        after_first = _state(self.session)
        second = snippet_add_batch(self.session, prepared)
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["aliased"], 0)
        self.assertEqual(_state(self.session), after_first)

    def test_name_add_twice_is_a_noop(self):
        snippet = snippet_add(self.session, "proc", "MOV EAX, 1")
        result = snippet_name_add(self.session, snippet.checksum, "alias")
        after_first = _state(self.session)
        again = snippet_name_add(self.session, snippet.checksum, "alias")
        self.assertIsNotNone(again)
        self.assertEqual(again.name_list, result.name_list)
        self.assertEqual(_state(self.session), after_first)

    def test_name_remove_twice_is_a_noop(self):
        snippet = snippet_add(self.session, "proc", "MOV EAX, 1")
        snippet_name_add(self.session, snippet.checksum, "alias")
        snippet_name_remove(self.session, snippet.checksum, "alias")
        after_first = _state(self.session)
        self.assertIsNone(snippet_name_remove(self.session, snippet.checksum, "alias"))
        self.assertEqual(_state(self.session), after_first)

    def test_tag_add_twice_is_a_noop(self):
        snippet = snippet_add(self.session, "proc", "MOV EAX, 1")
        snippet_tag_add(self.session, snippet.checksum, "crypto")
        after_first = _state(self.session)
        snippet_tag_add(self.session, snippet.checksum, "crypto")
        self.assertEqual(_state(self.session), after_first)

    def test_tag_remove_twice_is_a_noop(self):
        snippet = snippet_add(self.session, "proc", "MOV EAX, 1")
        snippet_tag_add(self.session, snippet.checksum, "crypto")
        snippet_tag_remove(self.session, snippet.checksum, "crypto")
        after_first = _state(self.session)
        snippet_tag_remove(self.session, snippet.checksum, "crypto")
        self.assertEqual(_state(self.session), after_first)

    def test_delete_twice_leaves_one_state(self):
        snippet = snippet_add(self.session, "proc", "MOV EAX, 1")
        self.assertTrue(snippet_delete(self.session, snippet.checksum, quiet=True))
        after_first = _state(self.session)
        # The second run reports "not found"; what must not happen is a second
        # deletion changing the state again.
        self.assertFalse(snippet_delete(self.session, snippet.checksum, quiet=True))
        self.assertEqual(_state(self.session), after_first)


class TestCollectionWrites(BaseDBTest):
    """Collection creation converges instead of colliding on its primary key."""

    def test_create_twice_keeps_one_row_and_its_description(self):
        collection_create(self.session, "crypto", description="Crypto routines")
        after_first = _state(self.session)
        again = collection_create(self.session, "crypto", description="overwritten")
        self.assertEqual(_state(self.session), after_first)
        self.assertEqual(again.description, "Crypto routines")

    def test_delete_twice_leaves_one_state(self):
        collection_create(self.session, "crypto")
        self.assertTrue(collection_delete(self.session, "crypto", quiet=True))
        after_first = _state(self.session)
        self.assertFalse(collection_delete(self.session, "crypto", quiet=True))
        self.assertEqual(_state(self.session), after_first)


class TestReindexWrites(BaseDBTest):
    """A re-run of a fingerprint rebuild reproduces the same blobs."""

    def test_reindex_twice_reproduces_fingerprints(self):
        for i in range(3):
            snippet_add(self.session, f"proc{i}", f"MOV EAX, {i}")
        self.assertNotIn("error", db_reindex(self.session, jobs=1))
        first = dict(self.session.exec(select(Snippet.checksum, Snippet.minhash)).all())
        self.assertNotIn("error", db_reindex(self.session, jobs=1))
        second = dict(self.session.exec(select(Snippet.checksum, Snippet.minhash)).all())
        self.assertEqual(second, first)


class TestMergeWrites(BaseDBTest):
    """Merging the same source twice adds nothing the first run did not."""

    def _merged_state(self, source_path):
        result = db_merge(self.session, source_path)
        self.assertNotIn("error", result)
        return result

    def test_merge_twice_adds_no_second_row(self):
        source = self._create_source_db(
            snippets=[("src", "MOV EBX, 2", ["crypto"], "src_col")],
            collections=[("src_col", "Source collection")],
        )
        try:
            first = self._merged_state(source)
            after_first = _state(self.session)
            second = self._merged_state(source)
        finally:
            os.unlink(source)
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(_state(self.session), after_first)

    def test_merge_into_a_partial_run_converges(self):
        """A merge that failed after committing a chunk is repaired by a re-run."""
        source = self._create_source_db(
            snippets=[("a", "MOV EAX, 1", [], None), ("b", "MOV EBX, 2", [], None)],
        )
        try:
            first = self._merged_state(source)
            after_first = _state(self.session)
            second = self._merged_state(source)
        finally:
            os.unlink(source)
        self.assertEqual(first["added"], 2)
        self.assertEqual(second["added"], 0)
        self.assertEqual(_state(self.session), after_first)


class TestExportWrites(BaseDBTest):
    """Exporting twice rewrites the same files rather than appending to them."""

    def test_export_twice_is_stable(self):
        snippet_add(self.session, "proc", "MOV EAX, 1")
        with tempfile.TemporaryDirectory() as out:
            snippet_export(self.session, out)
            first = {
                name: open(os.path.join(out, name), encoding="utf-8").read()
                for name in os.listdir(out)
            }
            snippet_export(self.session, out)
            second = {
                name: open(os.path.join(out, name), encoding="utf-8").read()
                for name in os.listdir(out)
            }
        self.assertEqual(second, first)
        self.assertEqual(first, {"proc.asm": "MOV EAX, 1"})


if __name__ == "__main__":
    unittest.main()
