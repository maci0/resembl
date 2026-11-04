"""Database engine and helpers."""

from __future__ import annotations

import threading

from sqlalchemy import Engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry
from sqlmodel import SQLModel, create_engine

# Imported for its side effect: defining the SQLModel classes registers the
# tables that ``db_create`` creates (an empty metadata creates nothing).
from . import models  # noqa: F401
from .paths import db_url_get

# Default to assembly.db, but allow overriding for testing or PostgreSQL use.
# Examples:
#   sqlite:///assembly.db        (default, local file)
#   sqlite:///:memory:           (in-memory, for tests)
#   postgresql://user:pass@host/db  (PostgreSQL for teams)


def create_db_engine(
    url: str | None = None,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> Engine:
    """Create a SQLAlchemy engine for the given URL.

    If *url* is ``None``, the URL comes from :func:`db_url_get`
    (``RESEMBL_DATABASE_URL``, then ``DATABASE_URL``, then
    ``sqlite:///assembly.db``).

    SQLite-specific pragmas (WAL mode, synchronous=NORMAL) are applied
    automatically when the URL starts with ``sqlite``.

    ``pool_size`` / ``max_overflow`` override the SQLAlchemy defaults —
    the ``serve`` process passes a larger pool because it serves one
    request thread per connection and the default (5 + 10 overflow) was
    exhausted under concurrent load.
    """
    db_url = url or db_url_get()
    kwargs: dict[str, object] = {"echo": False}
    if pool_size is not None:
        kwargs["pool_size"] = pool_size
    if max_overflow is not None:
        kwargs["max_overflow"] = max_overflow
    eng = create_engine(db_url, **kwargs)

    if db_url.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        # Fixed SQLAlchemy connect-listener signature.
        def _set_sqlite_pragma(
            dbapi_connection: DBAPIConnection,
            connection_record: ConnectionPoolEntry,  # pylint: disable=unused-argument
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                # 30s busy wait: concurrent writers (two CLI processes, or an
                # import while a find builds the index) serialize instead of
                # failing immediately with "database is locked".
                cursor.execute("PRAGMA busy_timeout=30000")
                # Enforce the foreign keys declared in models.py (SQLite
                # leaves them off per-connection by default).  Without this
                # the constraints are decorative and orphan rows — a snippet
                # pointing at a missing collection, a version or bucket row
                # for a deleted snippet — persist silently.
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return eng


# Mutable lazy singleton, not a constant: pylint's const-rgx would have it
# UPPER_CASE because the initializer is a literal.
_engine: Engine | None = None  # pylint: disable=invalid-name
#: Serializes first-time construction: the ``serve`` process runs one handler
#: thread per request, and an unsynchronized check-then-act let two threads
#: each build an engine.  The loser's pool (up to ``pool_size +
#: max_overflow`` SQLite handles) had no owner left to dispose it, so the
#: process leaked one pool per race.  The steady state (the engine exists)
#: stays lock-free.
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    """Return the module-level engine, creating it on first use.

    Creating the engine opens a SQLite connection and applies the WAL
    pragmas (~30-50 ms), so it is deferred until a command actually touches
    the database — ``--help``, ``version`` and similar never pay for it.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:  # double-checked: loser re-probes before building
                _engine = create_db_engine()
    return _engine


def db_create() -> None:
    """Create database tables if they do not already exist."""
    SQLModel.metadata.create_all(get_engine())
    _ensure_indexes(get_engine())


def _ensure_indexes(eng: Engine) -> None:
    """Create indexes that create_all alone never adds to existing databases.

    create_all is check-first: on a database created by an older version it
    creates missing tables but never alters existing ones, so an index added
    to a model after the fact would silently never reach the databases that
    need it.  These CREATE INDEX IF NOT EXISTS statements are the
    reversible, deploy-safe migration for indexes: they are no-ops on a
    fresh database (the model already declares them) and fill in the gap on
    an existing one.  Each one is chosen from a query this codebase
    demonstrably runs — see the comments inline.

    ix_lsh_bucket_checksum is also dropped for the duration of an index
    build and recreated afterwards; if that build is interrupted the index
    would otherwise stay missing until the next successful build, slowing
    every DELETE FROM lsh_bucket WHERE checksum = ? (snippet delete) to a
    full scan.
    """
    from sqlalchemy import text as _text

    statements = (
        # SnippetVersion.get_by_checksum / snippet_version_list:
        # WHERE snippet_checksum = ? ORDER BY created_at DESC.  The
        # single-column index on snippet_checksum alone still sorts every
        # version row for a popular snippet.
        (
            "CREATE INDEX IF NOT EXISTS ix_snippetversion_checksum_created "
            "ON snippetversion (snippet_checksum, created_at DESC)"
        ),
        # Snippet.get_by_collection: WHERE collection = ? ORDER BY checksum.
        # The existing ix_snippet_collection index does not cover the
        # checksum ordering; a composite (collection, checksum) does.
        (
            "CREATE INDEX IF NOT EXISTS ix_snippet_collection_checksum "
            "ON snippet (collection, checksum)"
        ),
        # ResemblLSH.remove / lsh_index_purge: DELETE FROM lsh_bucket
        # WHERE checksum = ?.  Declared on the model, but dropped during
        # index builds — re-ensure it for databases whose build was
        # interrupted.
        ("CREATE INDEX IF NOT EXISTS ix_lsh_bucket_checksum ON lsh_bucket (checksum)"),
    )
    with eng.begin() as conn:
        for stmt in statements:
            conn.execute(_text(stmt))
