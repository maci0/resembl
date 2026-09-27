"""Database models used by resembl."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Column, Integer, Text
from sqlmodel import Field, Session, SQLModel, select

# Re-exported for external backward compatibility only
# (`from resembl.models import minhash_pack` keeps working); modules inside
# this package import these from ``resembl.scoring`` directly.
from .scoring import (  # noqa: F401
    minhash_ensure_packed,
    minhash_jaccard,
    minhash_jaccard_batch,
    minhash_new,
    minhash_num_perm,
    minhash_pack,
    minhash_unpack,
)

if TYPE_CHECKING:
    from .minhash import MinHash


#: Environment variable that pins :func:`timestamp_now` to a fixed instant,
#: given as an ISO 8601 string.  The wall clock is otherwise the only source
#: of ``created_at`` values, and those values are written to the database and
#: printed back by ``collection list`` and ``version``, so two runs of
#: the same inputs never produce the same rows or the same output.  Setting
#: this variable is what makes a recorded run replay byte-for-byte.
CLOCK_ENV_VAR = "RESEMBL_NOW"


def timestamp_now() -> str:
    """Return the current instant as a canonical UTC ISO 8601 string.

    Read from ``CLOCK_ENV_VAR`` when that variable is set, so a test or a
    replay gets the same stamp every time; an unparseable value raises
    ``ValueError`` rather than silently falling back to the wall clock, which
    would defeat the point of setting it.
    """
    override = os.environ.get(CLOCK_ENV_VAR)
    if override is None:
        return datetime.now(UTC).isoformat()
    try:
        moment = datetime.fromisoformat(override)
    except ValueError as exc:
        raise ValueError(f"{CLOCK_ENV_VAR}={override!r} is not an ISO 8601 instant") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def timestamp_normalize(value: str | None) -> str | None:
    """Normalize a ``created_at`` string to the canonical stored form.

    Timestamps are stored as aware-UTC ISO 8601 strings, exactly as written
    by :func:`timestamp_now`.  Rows are ordered by string comparison
    (e.g. ``SnippetVersion.get_by_checksum``), which matches chronological
    order only while every value carries the same offset, so timestamps
    imported from foreign databases must be re-expressed in UTC before being
    persisted.  Naive values are interpreted as UTC (the historical writer's
    zone).  A NULL column and unparseable text are returned unchanged:
    a merge never rewrites metadata it cannot read.
    """
    if not isinstance(value, str):
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


class Collection(SQLModel, table=True):
    """A named group of snippets (e.g., 'libc patterns', 'crypto routines')."""

    name: str = Field(primary_key=True, max_length=128)
    description: str = ""
    created_at: str = Field(default_factory=timestamp_now)

    @classmethod
    def get_all(cls, session: Session) -> Sequence[Collection]:
        """Return all collections, in name order.

        Name order rather than the backend's row order: `collection list`
        renders these rows directly, and a merge snapshots them in the same
        order, so an unspecified order is a run-to-run difference in output.
        """
        return session.exec(select(cls).order_by(cls.name)).all()

    @classmethod
    def get_by_name(cls, session: Session, name: str) -> Collection | None:
        """Retrieve a collection by name."""
        return session.get(cls, name)


class SnippetVersion(SQLModel, table=True):
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
    created_at: str = Field(default_factory=timestamp_now)

    @classmethod
    def get_by_checksum(cls, session: Session, checksum: str) -> Sequence[SnippetVersion]:
        """Return all versions for a given snippet, newest first."""
        return session.exec(
            select(cls)
            .where(cls.snippet_checksum == checksum)
            .order_by(cls.created_at.desc())  # type: ignore[attr-defined]
        ).all()


class Snippet(SQLModel, table=True):
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
    def get_by_checksum(cls, session: Session, checksum: str) -> Snippet | None:
        """Retrieve a snippet by its checksum."""
        return session.get(cls, checksum)

    @classmethod
    def get_by_name(cls, session: Session, name: str) -> Snippet | None:
        """Return the snippet containing the given name, if any."""
        # Use SQL LIKE to narrow candidates, then verify in Python.  Only the
        # two decision columns are fetched: the LIKE can match many rows
        # before one verifies, and full rows would pull each match's ``code``
        # (the column that dominates the table) through the ORM for nothing.
        # The winner's full row is fetched via the identity map afterwards.
        #
        # The probe must reproduce the stored (JSON-encoded) spelling of
        # *name*, or a name containing ``"`` or ``\`` can never match its own
        # row: first encode like ``json.dumps`` does, then LIKE-escape the
        # result ('\\' is the escape character, so every stored backslash —
        # including the ones JSON just introduced — must be doubled) and
        # protect ``%`` / ``_`` so they match themselves instead of widening
        # the probe.
        encoded = name.replace("\\", "\\\\").replace('"', '\\"')
        literal = encoded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        candidates = session.exec(
            select(cls.checksum, cls.names).where(
                cls.names.like(f'%"{literal}"%', escape="\\")  # type: ignore[attr-defined]
            )
        ).all()
        for checksum, names in candidates:
            if name in json.loads(names):
                return session.get(cls, checksum)
        return None

    @classmethod
    def get_all(cls, session: Session) -> Sequence[Snippet]:
        """Return all snippets in the database, in checksum order.

        Ordered by the primary key so the row order is a property of the
        data rather than of the plan the backend happens to pick: every
        caller renders, paginates or float-sums these rows, and none of
        that may differ between two runs over the same database.
        """
        return session.exec(select(cls).order_by(cls.checksum)).all()

    @classmethod
    def stream_all(cls, session: Session, batch_size: int = 1000) -> Iterator[Snippet]:
        """Yield all snippets in batches, bounding memory for large databases.

        Checksum-ordered, like :meth:`get_all`; the sort rides the primary
        key index, so a streamed scan stays a merge over the b-tree rather
        than a sort of the whole table.
        """
        yield from session.exec(select(cls).order_by(cls.checksum)).yield_per(batch_size)

    @classmethod
    def iter_batches(cls, session: Session, batch_size: int = 1000) -> Iterator[list[Snippet]]:
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
            stmt = select(cls.checksum, cls.minhash).order_by(cls.checksum).limit(batch_size)
            if last is not None:
                stmt = stmt.where(cls.checksum > last)
            batch = session.exec(stmt).all()
            if not batch:
                return
            yield [(row[0], row[1]) for row in batch]
            last = batch[-1][0]

    @classmethod
    def get_by_collection(cls, session: Session, collection_name: str) -> Sequence[Snippet]:
        """Return all snippets in a given collection, in checksum order."""
        return session.exec(
            select(cls).where(cls.collection == collection_name).order_by(cls.checksum)
        ).all()

    def get_minhash_obj(self) -> MinHash:
        """Return the stored MinHash object for this snippet."""
        return minhash_unpack(self.minhash)


#: Maximum width of the ``lsh_bucket.bucket`` hex key column.  The bound is
#: the widest key a composite primary key may have while staying inside
#: MySQL InnoDB's 3072-byte index-key limit (utf8mb4: 640*4 + checksum
#: 64*4 + int).  Banding configurations that would need wider keys are
#: rejected when the index object is constructed (see
#: :class:`resembl.lsh.ResemblLSH`), so every backend fails identically and
#: early instead of silently writing out-of-spec keys (SQLite) or failing
#: mid-build with a dialect-specific error (PostgreSQL/MySQL).
LSH_BUCKET_KEY_MAX = 640


class LSHBucket(SQLModel, table=True):
    """LSH index entry row (one row per band bucket hit).

    The index is a banded Locality-Sensitive Hash: every snippet contributes
    one row per band whose bucket hash matches.  ``find`` queries only touch
    the buckets the query lands in, so lookups stay fast regardless of how
    many snippets are indexed, and no full in-memory index needs to be
    pickled to disk.

    ``bucket`` is a lowercase hex encoding of the band (``8 * r`` chars,
    where ``r`` is the band row size derived from ``threshold`` /
    ``num_perm``), which every supported database can index — a raw
    ``BLOB`` column cannot be part of a primary key on MySQL/MariaDB.
    The width is bounded by the column rather than fixed: higher
    thresholds / permutation counts grow ``r``, and configurations whose
    keys would exceed the bound are rejected at index construction (see
    :data:`LSH_BUCKET_KEY_MAX`).
    """

    __tablename__ = "lsh_bucket"

    band: int = Field(primary_key=True)
    bucket: str = Field(primary_key=True, max_length=LSH_BUCKET_KEY_MAX)
    checksum: str = Field(primary_key=True, max_length=64, index=True)


class LSHMeta(SQLModel, table=True):
    """Single-row table recording the parameters of the built LSH index.

    ``id`` is always 1.  A present row means the ``lsh_bucket`` table holds a
    complete index built with ``threshold`` / ``num_perm``; if a caller asks
    for different parameters, the index is rebuilt.
    """

    __tablename__ = "lsh_meta"

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


class AppMeta(SQLModel, table=True):
    """Small key-value store for application metadata (e.g. fingerprint version)."""

    __tablename__ = "app_meta"

    key: str = Field(primary_key=True, max_length=64)
    value: str = Field(sa_column=Column(Text, nullable=False))
