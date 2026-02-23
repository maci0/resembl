"""Database models used by resembl."""

import json
import pickle
from collections.abc import Sequence
from datetime import datetime, timezone

from datasketch import MinHash
from sqlmodel import Field, Session, SQLModel, select


class Collection(SQLModel, table=True):  # type: ignore
    """A named group of snippets (e.g., 'libc patterns', 'crypto routines')."""

    name: str = Field(primary_key=True)
    description: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def get_all(cls, session: Session) -> Sequence["Collection"]:
        """Return all collections."""
        return session.exec(select(cls)).all()

    @classmethod
    def get_by_name(cls, session: Session, name: str) -> "Collection | None":
        """Retrieve a collection by name."""
        return session.get(cls, name)


class SnippetVersion(SQLModel, table=True):  # type: ignore
    """A historical version of a snippet's code."""

    id: int | None = Field(default=None, primary_key=True)
    snippet_checksum: str = Field(index=True)
    code: str
    minhash: bytes
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def get_by_checksum(cls, session: Session, checksum: str) -> Sequence["SnippetVersion"]:
        """Return all versions for a given snippet, newest first."""
        return session.exec(
            select(cls)
            .where(cls.snippet_checksum == checksum)
            .order_by(cls.created_at.desc())  # type: ignore[attr-defined]
        ).all()


class Snippet(SQLModel, table=True):  # type: ignore
    """Model representing a stored assembly snippet."""

    checksum: str = Field(primary_key=True)
    names: str  # JSON-encoded list of strings
    code: str
    minhash: bytes
    tags: str = Field(default="[]")
    collection: str | None = Field(default=None, index=True)

    @property
    def tag_list(self) -> list[str]:
        """Return the list of tags for the snippet."""
        return json.loads(self.tags)

    @property
    def name_list(self) -> list[str]:
        """Return the list of alias names for the snippet."""
        return json.loads(self.names)

    @classmethod
    def get_by_checksum(cls, session: Session, checksum: str) -> "Snippet | None":
        """Retrieve a snippet by its checksum."""
        return session.get(cls, checksum)

    @classmethod
    def get_by_name(cls, session: Session, name: str) -> "Snippet | None":
        """Return the snippet containing the given name, if any."""
        # Use SQL LIKE to narrow candidates, then verify in Python
        candidates = session.exec(
            select(cls).where(cls.names.like(f'%"{name}"%'))  # type: ignore[attr-defined]
        ).all()
        for snippet in candidates:
            if name in snippet.name_list:
                return snippet
        return None

    @classmethod
    def get_all(cls, session: Session) -> Sequence["Snippet"]:
        """Return all snippets in the database."""
        return session.exec(select(cls)).all()

    @classmethod
    def get_by_collection(cls, session: Session, collection_name: str) -> Sequence["Snippet"]:
        """Return all snippets in a given collection."""
        return session.exec(
            select(cls).where(cls.collection == collection_name)
        ).all()

    def get_minhash_obj(self) -> MinHash:
        """Return the stored MinHash object for this snippet."""
        return pickle.loads(self.minhash)

