"""Database engine and helpers."""

from __future__ import annotations

import os
import re
import threading

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
DEFAULT_DB_URL = "sqlite:///assembly.db"

#: Environment variables consulted for the database URL, most specific first.
#: ``RESEMBL_DATABASE_URL`` is the namespaced name every other resembl
#: override uses (``RESEMBL_CONFIG_DIR``, ``RESEMBL_CACHE_DIR``) and wins
#: outright.  The unprefixed ``DATABASE_URL`` stays supported and is read
#: next: it is the name other Python and hosting stacks already export, so
#: dropping it would break a working deployment.  An empty value counts as
#: unset in both, matching the directory overrides, rather than handing
#: SQLAlchemy an empty URL to fail on obscurely.
DB_URL_ENV_VARS = ("RESEMBL_DATABASE_URL", "DATABASE_URL")

#: Matches the credentials component of ``scheme://user:password@host/...``.
#: The password run stops at the first character a URL may not carry bare;
#: percent-encoded passwords round-trip untouched.
_DB_URL_CREDENTIALS = re.compile(r"(://[^:/?#\s]+:)([^@/\s]+)(@)")


def db_url_get() -> str:
    """Return the database URL configured in the environment.

    Read at call time, not at import time: an embedder that sets
    ``RESEMBL_DATABASE_URL`` after importing resembl (and a test that points
    the engine at a temporary database) gets the value it set, instead of
    whatever the environment happened to hold when the module was first
    imported.
    """
    for var in DB_URL_ENV_VARS:
        url = os.environ.get(var)
        if url:
            return url
    return DEFAULT_DB_URL


def db_url_mask(url: str) -> str:
    """Return *url* with any embedded password replaced by ``***``.

    Display helper for messages that echo a database URL (connection
    failures, merge progress): URLs like
    ``postgresql+pg8000://user:pass@host/db`` must not print the password.
    URLs without credentials are returned unchanged.
    """
    return _DB_URL_CREDENTIALS.sub(r"\1***\3", url)


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
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # 30s busy wait: concurrent writers (two CLI processes, or an
            # import while a find builds the index) serialize instead of
            # failing immediately with "database is locked".
            cursor.execute("PRAGMA busy_timeout=30000")
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
