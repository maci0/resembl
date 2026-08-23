"""Database engine and helpers."""

from __future__ import annotations

import os

from sqlalchemy import Engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry
from sqlmodel import SQLModel, create_engine

# Imported for its side effect: defining the SQLModel classes registers the
# tables that ``db_create`` creates (an empty metadata creates nothing).
from . import models  # noqa: F401

# Default to assembly.db, but allow overriding for testing or PostgreSQL use.
# Examples:
#   sqlite:///assembly.db        (default, local file)
#   sqlite:///:memory:           (in-memory, for tests)
#   postgresql://user:pass@host/db  (PostgreSQL for teams)
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///assembly.db")


def create_db_engine(
    url: str | None = None,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> Engine:
    """Create a SQLAlchemy engine for the given URL.

    If *url* is ``None``, the ``DATABASE_URL`` environment variable is
    used (falling back to ``sqlite:///assembly.db``).

    SQLite-specific pragmas (WAL mode, synchronous=NORMAL) are applied
    automatically when the URL starts with ``sqlite``.

    ``pool_size`` / ``max_overflow`` override the SQLAlchemy defaults —
    the ``serve`` process passes a larger pool because it serves one
    request thread per connection and the default (5 + 10 overflow) was
    exhausted under concurrent load.
    """
    db_url = url or DATABASE_URL
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
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # 30s busy wait: concurrent writers (two CLI processes, or an
            # import while a find builds the index) serialize instead of
            # failing immediately with "database is locked".
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return eng


_engine: Engine | None = None


def get_engine() -> Engine:
    """Return the module-level engine, creating it on first use.

    Creating the engine opens a SQLite connection and applies the WAL
    pragmas (~30-50 ms), so it is deferred until a command actually touches
    the database — ``--help``, ``version`` and similar never pay for it.
    """
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def db_create() -> None:
    """Create database tables if they do not already exist."""
    SQLModel.metadata.create_all(get_engine())
