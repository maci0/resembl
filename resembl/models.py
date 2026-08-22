"""Database models used by resembl."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Column, Integer, Text
from sqlmodel import Field, Session, SQLModel, select

from .scoring import (  # noqa: F401  (re-exported; `from resembl.models import minhash_pack` keeps working)
    minhash_ensure_packed,
    minhash_jaccard,
    minhash_jaccard_batch,
    minhash_new,
    minhash_num_perm,
    minhash_pack,
    minhash_unpack,
)

if TYPE_CHECKING:
    from datasketch import MinHash


class Collection(SQLModel, table=True):  # type: ignore
    """A named group of snippets (e.g., 'libc patterns', 'crypto routines')."""

    name: str = Field(primary_key=True, max_length=128)
    description: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @classmethod
    def get_all(cls, session: Session) -> Sequence["Collection"]:
        """Return all collections."""
        return session.exec(select(cls)).all()

    @classmethod
    def get_by_name(cls, session: Session, name: str) -> "Collection | None":
        """Retrieve a collection by name."""
        return session.get(cls, name)


class SnippetVersion(SQLModel, table=True):  # type: ignore
    """A historical version of a snippet's code.

    The primary key is a plain integer set by the caller.  DuckDB (as of
    1.5.x) does not implement any auto-increment form (SERIAL / IDENTITY
    both raise "Constraint not implemented"), so a portable schema cannot
    rely on database-generated ids.  This model has no production writer
    yet; the versioning feature should assign ids explicitly when it lands.
    """

    id: int | None = Field(
        default=None,
        sa_column=Column(Integer, primary_key=True, autoincrement=False),
    )
    snippet_checksum: str = Field(index=True, max_length=64)
    code: str = Field(sa_column=Column(Text, nullable=False))
    minhash: bytes
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @classmethod
    def get_by_checksum(
        cls, session: Session, checksum: str
    ) -> Sequence["SnippetVersion"]:
        """Return all versions for a given snippet, newest first."""
        return session.exec(
            select(cls)
            .where(cls.snippet_checksum == checksum)
            .order_by(cls.created_at.desc())  # type: ignore[attr-defined]
        ).all()


class Snippet(SQLModel, table=True):  # type: ignore
    """Model representing a stored assembly snippet."""

    checksum: str = Field(primary_key=True, max_length=64)
    names: str = Field(sa_column=Column(Text, nullable=False))  # JSON-encoded list
    code: str = Field(sa_column=Column(Text, nullable=False))
    minhash: bytes
    tags: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    collection: str | None = Field(default=None, index=True, max_length=128)

    @property
    def name_list(self) -> list[str]:
        """Return the list of alias names for the snippet."""
        return json.loads(self.names)

    @property
    def tag_list(self) -> list[str]:
        """Return the list of tags for the snippet."""
        return json.loads(self.tags)

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
    def stream_all(
        cls, session: Session, batch_size: int = 1000
    ) -> Iterator["Snippet"]:
        """Yield all snippets in batches, bounding memory for large databases."""
        yield from session.exec(select(cls)).yield_per(batch_size)

    @classmethod
    def iter_batches(
        cls, session: Session, batch_size: int = 1000
    ) -> Iterator[list["Snippet"]]:
        """Yield ``Snippet`` lists via keyset pagination on the checksum PK.

        Unlike a streaming cursor (``yield_per``), each batch fully consumes
        its query before being yielded, so callers can safely write to the
        same session/connection between batches — required by SQLite, which
        otherwise raises ``database is locked`` when a write happens while a
        read cursor is still open on the connection.
        """
        last: str | None = None
        while True:
            stmt = select(cls).order_by(cls.checksum).limit(batch_size)
            if last is not None:
                stmt = stmt.where(cls.checksum > last)
            batch = list(session.exec(stmt).all())
            if not batch:
                return
            yield batch
            last = batch[-1].checksum

    @classmethod
    def iter_minhash_batches(
        cls, session: Session, batch_size: int = 1000
    ) -> Iterator[list[tuple[str, bytes]]]:
        """Yield ``(checksum, minhash)`` pairs via keyset pagination.

        Reads only the two columns the LSH build needs instead of full rows:
        the ``code`` column dominates the table, so loading it during an
        index build would pull the whole database through the ORM for
        nothing.  Same pagination semantics as :meth:`iter_batches` (each
        batch is fully consumed before yielding, so callers may write to the
        same connection between batches).
        """
        last: str | None = None
        while True:
            stmt = (
                select(cls.checksum, cls.minhash)
                .order_by(cls.checksum)
                .limit(batch_size)
            )
            if last is not None:
                stmt = stmt.where(cls.checksum > last)
            batch = session.exec(stmt).all()
            if not batch:
                return
            yield [(row[0], row[1]) for row in batch]
            last = batch[-1][0]

    @classmethod
    def get_by_collection(
        cls, session: Session, collection_name: str
    ) -> Sequence["Snippet"]:
        """Return all snippets in a given collection."""
        return session.exec(select(cls).where(cls.collection == collection_name)).all()

    def get_minhash_obj(self) -> MinHash:
        """Return the stored MinHash object for this snippet."""
        return minhash_unpack(self.minhash)


class LSHBucket(SQLModel, table=True):  # type: ignore
    """SQLite-backed LSH index entry (one row per band bucket hit).

    The index is a banded Locality-Sensitive Hash: every snippet contributes
    one row per band whose bucket hash matches.  ``find`` queries only touch
    the buckets the query lands in, so lookups stay fast regardless of how
    many snippets are indexed, and no full in-memory index needs to be
    pickled to disk.

    ``bucket`` is a fixed-width lowercase hex encoding of the band (20 bytes
    -> 40 chars), which every supported database can index — a raw ``BLOB``
    column cannot be part of a primary key on MySQL/MariaDB.
    """

    __tablename__ = "lsh_bucket"  # noqa: N815

    band: int = Field(primary_key=True)
    bucket: str = Field(primary_key=True, max_length=40)
    checksum: str = Field(primary_key=True, max_length=64, index=True)


class LSHMeta(SQLModel, table=True):  # type: ignore
    """Single-row table recording the parameters of the built LSH index.

    ``id`` is always 1.  A present row means the ``lsh_bucket`` table holds a
    complete index built with ``threshold`` / ``num_perm``; if a caller asks
    for different parameters, the index is rebuilt.
    """

    __tablename__ = "lsh_meta"  # noqa: N815

    id: int = Field(default=1, primary_key=True)
    threshold: float
    num_perm: int


#: Version of the fingerprint algorithm.  Bumped whenever stored MinHash
#: blobs or bucket keys would differ from a re-computation (e.g. the
#: weighted-shingling fix, or the switch to hex-encoded bucket keys).  The
#: value is stamped into ``app_meta`` by index builds/reindexes; a mismatch
#: makes ``find`` reindex the database once instead of silently matching old
#: fingerprints against new query fingerprints.
FINGERPRINT_VERSION = 3


class AppMeta(SQLModel, table=True):  # type: ignore
    """Small key-value store for application metadata (e.g. fingerprint version)."""

    __tablename__ = "app_meta"  # noqa: N815

    key: str = Field(primary_key=True, max_length=64)
    value: str = Field(sa_column=Column(Text, nullable=False))
