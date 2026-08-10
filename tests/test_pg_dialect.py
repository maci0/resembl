"""Dialect-dispatch tests for the PostgreSQL code paths.

A live PostgreSQL server is not available in every environment, so the
dialect *selection* is unit-tested here with a fake postgresql dialect, and
the full integration test (``test_pg_integration.py``) runs against a real
server when ``RESEMBL_TEST_PG_URL`` is set.
"""

import unittest


class _FakeDialect:
    def __init__(self, name: str):
        self.name = name


class _FakeBind:
    def __init__(self, name: str):
        self.dialect = _FakeDialect(name)


class _FakeSession:
    def __init__(self, name: str):
        self._bind = _FakeBind(name)

    def get_bind(self):
        return self._bind


class TestDialectDispatch(unittest.TestCase):
    """The PG-specific upsert SQL is selected by the connection dialect."""

    def test_insert_sql_selects_pg_variant(self):
        from resembl.lsh import _insert_sql

        pg = _insert_sql(_FakeSession("postgresql"))
        self.assertIn("ON CONFLICT DO NOTHING", pg)
        self.assertNotIn("INSERT OR IGNORE", pg)

    def test_insert_sql_selects_sqlite_variant(self):
        from resembl.lsh import _insert_sql

        sqlite = _insert_sql(_FakeSession("sqlite"))
        self.assertIn("INSERT OR IGNORE", sqlite)
        self.assertNotIn("ON CONFLICT", sqlite)

    def test_sqlite_gate_used_by_build_and_reindex(self):
        """Both the build and reindex gate sqlite-only behavior on the same check.

        This pins the gate expression; the full behavior is exercised by the
        integration test against a real server.
        """
        import inspect

        from resembl import cache, core

        build_src = inspect.getsource(cache.lsh_index_build)
        self.assertIn('dialect.name == "sqlite"', build_src)
        reindex_src = inspect.getsource(core.db_reindex)
        self.assertIn('dialect.name == "sqlite"', reindex_src)
