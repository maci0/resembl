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

from sqlalchemy import event, func
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from resembl.cache import lsh_index_build
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
from resembl.lsh import banding_params
from resembl.models import Collection, LSHBucket, Snippet
from resembl.scoring import NUM_PERMUTATIONS


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


class TestIndexSyncAtomicity(unittest.TestCase):
    """A snippet and its LSH bucket rows become visible in the same commit.

    The bucket rows are unique by ``(band, bucket, checksum)``, so writing
    them twice is free; what a second *write* cannot repair is a snippet
    published without them.  Committing the snippet row first and the index
    rows second leaves a gap in which the process can die, and because a
    retried import of the same files takes the already-present alias path it
    never re-indexes them: ``lsh_meta`` still advertises a complete index
    while those snippets are invisible to every find, until someone runs a
    manual rebuild.  Each snapshot below is what a *separate* connection sees
    at one of the writer's commit boundaries, so it pins the boundary itself.
    """

    BANDS = banding_params(0.5, NUM_PERMUTATIONS)[0]

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.url = f"sqlite:///{self.db_path}"
        self.engine = create_engine(self.url)
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.snapshots: list[tuple[int, int]] = []
        event.listen(self.session, "after_commit", self._snapshot)

    def tearDown(self):
        event.remove(self.session, "after_commit", self._snapshot)
        self.session.close()
        self.engine.dispose()
        SQLModel.metadata.drop_all(self.engine)
        os.unlink(self.db_path)

    def _snapshot(self, _session: Session) -> None:
        """Record (snippets, buckets) as an outside connection sees them."""
        probe_engine = create_engine(self.url)
        try:
            with Session(probe_engine) as probe:
                try:
                    buckets = probe.exec(
                        select(func.count(LSHBucket.checksum))  # type: ignore[arg-type]
                    ).one()
                except OperationalError:
                    # No index has been built yet, so there is nothing to lag.
                    return
                snippets = probe.exec(
                    select(func.count(Snippet.checksum))  # type: ignore[arg-type]
                ).one()
                self.snapshots.append((snippets, buckets))
        finally:
            probe_engine.dispose()

    def _assert_index_never_lags(self) -> None:
        self.assertTrue(self.snapshots, "the write never committed")
        for snippets, buckets in self.snapshots:
            self.assertEqual(
                buckets,
                snippets * self.BANDS,
                f"{snippets} committed snippet(s) with {buckets} bucket row(s)",
            )

    def _seed_and_build(self) -> None:
        """One snippet plus a built index, so the incremental sync runs."""
        snippet_add(self.session, "seed", "MOV EBX, 2")
        lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS)
        self.snapshots.clear()

    def test_add_commits_the_snippet_with_its_bucket_rows(self):
        self._seed_and_build()
        snippet_add(self.session, "proc", "MOV EAX, 1")
        self._assert_index_never_lags()

    def test_add_batch_commits_the_snippets_with_their_bucket_rows(self):
        self._seed_and_build()
        snippet_add_batch(
            self.session,
            [
                item
                for item in (
                    snippet_prepare(f"p{i}", "MOV EAX, 1\n" * (i + 1), 3) for i in range(3)
                )
                if item
            ],
        )
        self._assert_index_never_lags()


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

    #: The export's own bookkeeping, not part of the exported snippets.
    MANIFEST = ".resembl-export.json"

    @staticmethod
    def _read_export(out: str) -> dict[str, str]:
        """Read the exported ``.asm`` files (the manifest is metadata)."""
        return {
            name: open(os.path.join(out, name), encoding="utf-8").read()
            for name in sorted(os.listdir(out))
            if name.endswith(".asm")
        }

    def test_export_twice_is_stable(self):
        snippet_add(self.session, "proc", "MOV EAX, 1")
        with tempfile.TemporaryDirectory() as out:
            snippet_export(self.session, out)
            first = self._read_export(out)
            snippet_export(self.session, out)
            second = self._read_export(out)
        self.assertEqual(second, first)
        self.assertEqual(first, {"proc.asm": "MOV EAX, 1"})

    def test_rerun_drops_files_of_deleted_and_renamed_snippets(self):
        """A re-run leaves the directory holding exactly what the database holds.

        Without this, ``export`` → ``rm`` → ``export`` left the deleted
        snippet's file behind, and a directory exported as an input to a
        later ``import`` re-added what had been retired.
        """
        snippet_add(self.session, "alpha", "MOV EAX, 1")
        beta = snippet_add(self.session, "beta", "MOV EBX, 2")
        with tempfile.TemporaryDirectory() as out:
            snippet_export(self.session, out)
            self.assertEqual(
                self._read_export(out), {"alpha.asm": "MOV EAX, 1", "beta.asm": "MOV EBX, 2"}
            )
            snippet_delete(self.session, beta.checksum, quiet=True)
            result = snippet_export(self.session, out)
            self.assertEqual(self._read_export(out), {"alpha.asm": "MOV EAX, 1"})
        self.assertEqual(result["num_removed"], 1)

    def test_rerun_prunes_a_rename_but_not_a_file_it_did_not_write(self):
        """Pruning is limited to the entries of the export's own manifest."""
        alpha = snippet_add(self.session, "alpha", "MOV EAX, 1")
        snippet_add(self.session, "beta", "MOV EBX, 2")
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "notes.txt"), "w", encoding="utf-8") as f:
                f.write("hand written")
            snippet_export(self.session, out)
            # Renaming the primary name changes the exported file name, so the
            # old one is stale: a re-run must not leave both.
            snippet_name_add(self.session, alpha.checksum, "gamma", quiet=True)
            snippet_name_remove(self.session, alpha.checksum, "alpha", quiet=True)
            result = snippet_export(self.session, out)
            self.assertEqual(
                self._read_export(out),
                {"beta.asm": "MOV EBX, 2", "gamma.asm": "MOV EAX, 1"},
            )
        self.assertEqual(result["num_removed"], 1)

    def test_unreadable_manifest_disables_pruning(self):
        """A damaged manifest never turns into a delete."""
        snippet_add(self.session, "alpha", "MOV EAX, 1")
        with tempfile.TemporaryDirectory() as out:
            snippet_export(self.session, out)
            with open(os.path.join(out, self.MANIFEST), "w", encoding="utf-8") as f:
                f.write("{not json")
            result = snippet_export(self.session, out)
            self.assertNotIn("num_removed", result)
            self.assertEqual(self._read_export(out), {"alpha.asm": "MOV EAX, 1"})


if __name__ == "__main__":
    unittest.main()
