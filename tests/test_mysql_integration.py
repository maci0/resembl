"""MySQL/MariaDB integration test — runs when a server is available.

Set ``RESEMBL_TEST_MYSQL_URL`` (e.g.
``mysql+pymysql://user:pass@host:3306/db``) to enable; the CI workflow
provides a ``mysql:8`` service container.  Exercises the dialect-guarded
paths unique to MySQL: ``INSERT IGNORE`` bucket upserts, ``ON DUPLICATE
KEY UPDATE`` metadata upserts, and the indexable-hex bucket schema.
"""

import os
import unittest

import resembl.models  # noqa: F401  (registers all tables with SQLModel.metadata)

RESEMBL_TEST_MYSQL_URL = os.environ.get("RESEMBL_TEST_MYSQL_URL")


@unittest.skipUnless(RESEMBL_TEST_MYSQL_URL, "RESEMBL_TEST_MYSQL_URL not set")
class TestMySQLIntegration(unittest.TestCase):
    """Full cycle against a real MySQL/MariaDB database."""

    @classmethod
    def setUpClass(cls):
        from sqlmodel import Session, SQLModel, create_engine

        cls.engine = create_engine(RESEMBL_TEST_MYSQL_URL)
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
            db_reindex,
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


if __name__ == "__main__":
    unittest.main()
