"""Database engine and helpers."""

import os

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, func, select

from .models import Snippet

# Default to assembly.db, but allow overriding for testing
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///assembly.db")

engine = create_engine(DATABASE_URL, echo=False)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable WAL mode for better performance."""
    if DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


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
