"""PostgreSQL integration test — runs when a server is available.

Set ``RESEMBL_TEST_PG_URL`` (e.g. ``postgresql+pg8000://user:pass@host/db``)
to enable; otherwise the tests are skipped so the suite stays green without
a PostgreSQL server.

Exercises the dialect-guarded code paths that SQLite never hits: the
``ON CONFLICT DO NOTHING`` bucket upserts, the single-commit index build
(no sqlite page-cache pragma), and the full import/build/find cycle.
"""

import os
import unittest

import resembl.models  # noqa: F401  (registers all tables with SQLModel.metadata)

RESEMBL_TEST_PG_URL = os.environ.get("RESEMBL_TEST_PG_URL")


@unittest.skipUnless(RESEMBL_TEST_PG_URL, "RESEMBL_TEST_PG_URL not set")
class TestPostgresIntegration(unittest.TestCase):
    """Full cycle against a real PostgreSQL database."""

    @classmethod
    def setUpClass(cls):
        from sqlmodel import Session, SQLModel, create_engine

        cls.engine = create_engine(RESEMBL_TEST_PG_URL)
        SQLModel.metadata.create_all(cls.engine)
        cls.session = Session(cls.engine)

    @classmethod
    def tearDownClass(cls):
        from sqlmodel import SQLModel

        cls.session.close()
        SQLModel.metadata.drop_all(cls.engine)
        cls.engine.dispose()

    def test_import_build_find_cycle(self):
        from resembl.core import (
            snippet_add_batch,
            snippet_find_matches,
            snippet_prepare,
        )
        from resembl.lsh import lsh_meta_get

        items = [
            snippet_prepare(f"f{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3) for i in range(50)
        ]
        result = snippet_add_batch(self.session, [x for x in items if x])
        self.assertEqual(result["added"], 50)

        # The build runs with the PG dialect (single commit, no sqlite pragma).
        num_candidates, matches = snippet_find_matches(
            self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=5
        )
        self.assertGreater(num_candidates, 0)
        self.assertEqual(len(matches), 5)
        self.assertIsNotNone(lsh_meta_get(self.session))


if __name__ == "__main__":
    unittest.main()
