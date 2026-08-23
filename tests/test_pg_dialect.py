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
        build_src += inspect.getsource(cache._build_once)
        self.assertIn('dialect.name == "sqlite"', build_src)
        reindex_src = inspect.getsource(core.db_reindex)
        self.assertIn('dialect.name == "sqlite"', reindex_src)

    def test_insert_sql_variants(self):
        """The bucket upsert is portable across SQLite/MySQL/PG/DuckDB."""
        from resembl.lsh import _insert_sql

        cases = {
            "sqlite": "INSERT OR IGNORE",
            "mysql": "INSERT IGNORE",
            "postgresql": "ON CONFLICT DO NOTHING",
            "duckdb": "ON CONFLICT DO NOTHING",
        }
        for dialect, needle in cases.items():
            sql = _insert_sql(_FakeSession(dialect))
            self.assertIn(needle, sql, dialect)

    def test_meta_and_version_upserts_are_portable(self):
        """Single-row upserts use ON DUPLICATE KEY on MySQL, ON CONFLICT elsewhere."""
        from resembl.lsh import _meta_upsert_sql, _version_upsert_sql

        for fn in (_meta_upsert_sql, _version_upsert_sql):
            self.assertIn("ON DUPLICATE KEY", fn(_FakeSession("mysql")))
            self.assertIn("ON CONFLICT", fn(_FakeSession("sqlite")))
            self.assertIn("ON CONFLICT", fn(_FakeSession("postgresql")))
            self.assertIn("ON CONFLICT", fn(_FakeSession("duckdb")))

    def test_bucket_keys_are_indexable_hex(self):
        """band_buckets returns 40-char hex keys (indexable on MySQL)."""

        from resembl.core import code_create_minhash
        from resembl.lsh import band_buckets

        m = code_create_minhash("push ebx; mov eax, 1; pop ebx; ret")
        packed = m.digest().tobytes()  # 128 uint64s -> use pack instead
        from resembl.models import minhash_pack

        packed = minhash_pack(m)
        keys = band_buckets(packed, 128, 25, 5)
        self.assertEqual(len(keys), 25)
        for key in keys:
            self.assertIsInstance(key, str)
            self.assertEqual(len(key), 40)  # 20 bytes -> 40 hex chars
            int(key, 16)  # valid hex


class TestSchemaPortability(unittest.TestCase):
    """CREATE TABLE DDL must be valid on SQLite, PostgreSQL, and MySQL.

    MySQL rejects BLOB/TEXT columns in primary keys and VARCHAR columns
    without an explicit length — this pins the schema against that.
    """

    def _ddl(self, dialect):
        from sqlalchemy.dialects import mysql, postgresql, sqlite
        from sqlalchemy.schema import CreateTable

        from resembl.models import LSHBucket, Snippet

        dialects = {
            "mysql": mysql.dialect(),
            "postgresql": postgresql.dialect(),
            "sqlite": sqlite.dialect(),
        }
        d = dialects[dialect]
        return (
            str(CreateTable(LSHBucket.__table__).compile(dialect=d)),
            str(CreateTable(Snippet.__table__).compile(dialect=d)),
        )

    def test_mysql_lsh_bucket_has_indexable_string_pk(self):
        bucket_ddl, _ = self._ddl("mysql")
        # No BLOB in the primary key: bucket/checksum are sized VARCHARs.
        # The bucket column is wider than the default band-key size (40
        # chars): higher thresholds / permutation counts grow the key
        # (`8 * r` chars), and PostgreSQL/MySQL reject overlong inserts.
        # 640 keeps the composite PK inside InnoDB's 3072-byte key limit
        # (utf8mb4: 640*4 + checksum 64*4 + int).
        self.assertRegex(bucket_ddl, r"bucket VARCHAR\(\d+\)")
        self.assertIn("VARCHAR(64)", bucket_ddl)
        self.assertNotIn("BLOB", bucket_ddl)

    def test_mysql_long_columns_are_text(self):
        _, snippet_ddl = self._ddl("mysql")
        self.assertIn("TEXT", snippet_ddl)
        # String PK has an explicit length.
        self.assertIn("VARCHAR(64)", snippet_ddl)

    def test_all_dialects_compile(self):
        for dialect in ("mysql", "postgresql", "sqlite"):
            bucket_ddl, snippet_ddl = self._ddl(dialect)
            self.assertTrue(bucket_ddl.strip().startswith("CREATE TABLE lsh_bucket"))
            self.assertTrue(snippet_ddl.strip().startswith("CREATE TABLE snippet"))
