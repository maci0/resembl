"""Database engine and helpers."""

from __future__ import annotations

import os

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, func, select

from .models import Snippet

# Default to assembly.db, but allow overriding for testing or PostgreSQL use.
# Examples:
#   sqlite:///assembly.db        (default, local file)
#   sqlite:///:memory:           (in-memory, for tests)
#   postgresql://user:pass@host/db  (PostgreSQL for teams)
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///assembly.db")


def create_db_engine(url: str | None = None):
    """Create a SQLAlchemy engine for the given URL.

    If *url* is ``None``, the ``DATABASE_URL`` environment variable is
    used (falling back to ``sqlite:///assembly.db``).

    SQLite-specific pragmas (WAL mode, synchronous=NORMAL) are applied
    automatically when the URL starts with ``sqlite``.
    """
    db_url = url or DATABASE_URL
    eng = create_engine(db_url, echo=False)

    if db_url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return eng


# Module-level default engine (used by the CLI)
engine = create_db_engine()


def db_create() -> None:
    """Create database tables if they do not already exist."""
    SQLModel.metadata.create_all(engine)


def db_checksum_get(session: Session) -> str:
    """Return a checksum representing the current database state."""
    # Pylint mis-identifies `func.count` as non-callable in SQLModel
    count = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]  # pylint: disable=not-callable
    if count == 0:
        return "empty"

    # `desc` is a SQLAlchemy method generated at runtime
    last_snippet = session.exec(
        select(Snippet).order_by(Snippet.checksum.desc())  # type: ignore[attr-defined]  # pylint: disable=no-member
    ).first()
    assert last_snippet is not None
    return f"{count}-{last_snippet.checksum}"
