"""Database models used by resembl."""

import json
import pickle
from collections.abc import Sequence

from datasketch import MinHash
from sqlmodel import Field, Session, SQLModel, select


class Snippet(SQLModel, table=True):  # type: ignore
    """Model representing a stored assembly snippet."""

    checksum: str = Field(primary_key=True)
    names: str  # JSON-encoded list of strings
    code: str
    minhash: bytes

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

    def get_minhash_obj(self) -> MinHash:
        """Return the stored MinHash object for this snippet."""
        return pickle.loads(self.minhash)
