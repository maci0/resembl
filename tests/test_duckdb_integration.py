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

    @classmethod
    def setUpClass(cls):
        from sqlmodel import Session, SQLModel, create_engine

        cls._db = tempfile.mktemp(suffix=".duckdb")
        cls.engine = create_engine(f"duckdb:///{cls._db}")
        SQLModel.metadata.create_all(cls.engine)
        cls.session = Session(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.engine.dispose()
        if os.path.exists(cls._db):
            os.remove(cls._db)

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


if __name__ == "__main__":
    unittest.main()
