"""Test utilities for the resembl test suite."""

from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine


@contextmanager
def temp_session():
    """Create a temporary, in-memory database session for testing."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        SQLModel.metadata.drop_all(engine)
