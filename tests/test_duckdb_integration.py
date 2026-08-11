"""DuckDB integration test — runs when ``duckdb-engine`` is installed.

DuckDB is embedded (no server), so this runs in the normal test suite once
the ``duckdb`` / ``duckdb-engine`` packages are present (they are dev
dependencies), exercising the full import -> build -> find -> reindex cycle
against a real DuckDB engine.
"""

import os
import tempfile
import unittest

import resembl.models  # noqa: F401  (registers all tables with SQLModel.metadata)

try:
    import duckdb  # noqa: F401
    import duckdb_engine  # noqa: F401

    _HAS_DUCKDB = True
except ImportError:
    _HAS_DUCKDB = False


@unittest.skipUnless(_HAS_DUCKDB, "duckdb / duckdb-engine not installed")
class TestDuckDBIntegration(unittest.TestCase):
    """Full cycle against a real DuckDB database."""

    def setUp(self):
        from sqlmodel import Session, SQLModel, create_engine

        self._db = tempfile.mktemp(suffix=".duckdb")
        self.engine = create_engine(f"duckdb:///{self._db}")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()
        if os.path.exists(self._db):
            os.remove(self._db)

    def test_import_build_find_reindex_cycle(self):
        from resembl.core import (
            db_reindex,
            db_stats,
            snippet_add_batch,
            snippet_find_matches,
            snippet_prepare,
        )
        from resembl.lsh import lsh_meta_get

        items = [
            snippet_prepare(f"f{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3)
            for i in range(50)
        ]
        result = snippet_add_batch(self.session, [x for x in items if x])
        self.assertEqual(result["added"], 50)

        num_candidates, matches = snippet_find_matches(
            self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=5
        )
        self.assertGreater(num_candidates, 0)
        self.assertEqual(len(matches), 5)
        self.assertIsNotNone(lsh_meta_get(self.session))

        result = db_reindex(self.session, jobs=1)
        self.assertEqual(result["num_reindexed"], 50)

        num_candidates, matches = snippet_find_matches(
            self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=5
        )
        self.assertEqual(len(matches), 5)

        stats = db_stats(self.session)
        self.assertEqual(stats["num_snippets"], 50)

    def test_insert_rows_multi_values(self):
        """The DuckDB fast path (multi-row VALUES) inserts rows correctly.

        The index build writes through ``_insert_rows``, which uses
        multi-row VALUES statements on DuckDB (13x faster than executemany)
        instead of the parameterized template.  Verify both the plain build
        variant and the ``ON CONFLICT DO NOTHING`` incremental variant,
        across the 1000-row statement boundary.
        """
        from sqlmodel import func, select

        from resembl.lsh import _insert_rows, lsh_index_clear
        from resembl.models import LSHBucket

        # The index build in the sibling test leaves rows in lsh_bucket;
        # start from a clean table so the counts are order-independent.
        lsh_index_clear(self.session)

        rows = [
            {
                "band": i % 5,
                "bucket": f"{i:040x}",
                "checksum": f"{i:064x}",
            }
            for i in range(2500)
        ]
        # Plain variant (one-time index build).
        _insert_rows(
            self.session,
            "INSERT INTO lsh_bucket (band, bucket, checksum) VALUES "
            "(:band, :bucket, :checksum)",
            rows,
        )
        self.session.commit()
        count = self.session.exec(
            select(func.count(LSHBucket.band))  # type: ignore[attr-defined]
        ).one()
        self.assertEqual(count, 2500)

        # Incremental variant with conflict handling: duplicates are no-ops.
        _insert_rows(
            self.session,
            "INSERT INTO lsh_bucket (band, bucket, checksum) VALUES "
            "(:band, :bucket, :checksum) ON CONFLICT DO NOTHING",
            rows[:100],
        )
        self.session.commit()
        count = self.session.exec(
            select(func.count(LSHBucket.band))  # type: ignore[attr-defined]
        ).one()
        self.assertEqual(count, 2500)

    def test_add_batch_tricky_content_roundtrips(self):
        """The DuckDB multi-VALUES snippet insert survives hostile content.

        Snippet code and names are arbitrary user text; the fast path
        interpolates values into SQL, so quotes, backslashes, and binary
        fingerprint bytes must round-trip exactly (and inject nothing).
        """
        from resembl.core import snippet_add_batch, snippet_get, snippet_prepare

        tricky_code = "mov eax, 'q' \\ ; it's a test\npush 'x'\nret"
        tricky_name = 'it\'s a \\ snippet "with" quotes'
        items = [
            snippet_prepare(tricky_name, tricky_code, 3),
            snippet_prepare("plain", "push ebx\nmov eax, 1\npop ebx\nret", 3),
        ]
        result = snippet_add_batch(self.session, [x for x in items if x])
        self.assertEqual(result["added"], 2)

        stored = snippet_get(self.session, items[0][0])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.code, tricky_code)
        self.assertIn(tricky_name, stored.name_list)
        # Binary payload survived verbatim (blob via FROM_HEX).
        self.assertEqual(stored.minhash, items[0][3])


if __name__ == "__main__":
    unittest.main()
