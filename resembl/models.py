"""Database models used by resembl."""

from __future__ import annotations

import json
import os
import unicodedata
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
#: printed back by ``collection list`` and ``version list``, so two runs of
#: the same inputs never produce the same rows or the same output.  Setting
#: this variable is what makes a recorded run replay byte-for-byte.
CLOCK_ENV_VAR = "RESEMBL_NOW"


def name_normalize(name: str) -> str:
    """Return *name* in NFC, the one form names are stored and compared in.

    A name is identity: it is the primary key of a collection, the alias a
    user types back into ``resembl name``/``collection``, and the stem of an
    exported file.  macOS hands out decomposed (NFD) spellings of the names
    it is given while Linux and Windows compose them (NFC), so the same
    ``café.asm`` reaches a database as two different strings and every
    byte-wise lookup against it misses.  Normalizing at ingestion and again
    at lookup keeps the two platforms talking about one name; rows written
    before this existed still resolve, because the lookup compares
    normalized spellings.
    """
    return unicodedata.normalize("NFC", name)


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
        """Return all collections."""
        return session.exec(select(cls)).all()

    @classmethod
    def get_by_name(cls, session: Session, name: str) -> Collection | None:
        """Retrieve a collection by name."""
        return session.get(cls, name_normalize(name))


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
        # *name*, or a name containing ``"``, ``\`` or any non-ASCII
        # character can never match its own row.  ``json.dumps`` is what
        # writes the column and it escapes backslash, quote and everything
        # outside ASCII as ``\uXXXX``, so the probe is built from its own
        # output rather than a hand-rolled subset: the hand-rolled version
        # handled ``"`` and ``\`` but left ``é`` as a literal while the row
        # held ``é``, so every accented name was unfindable.  The
        # LIKE-escaping that follows doubles the backslashes JSON just
        # introduced and protects ``%`` / ``_`` so they match themselves
        # instead of widening the probe.
        #
        # A name is probed in both canonical forms, because a row written
        # before names were normalized (or imported from a macOS filesystem,
        # which hands out NFD) stores the other one.
        for spelling in dict.fromkeys((name_normalize(name), unicodedata.normalize("NFD", name))):
            encoded = json.dumps(spelling)[1:-1]
            literal = encoded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            candidates = session.exec(
                select(cls.checksum, cls.names).where(
                    cls.names.like(f'%"{literal}"%', escape="\\")  # type: ignore[attr-defined]
                )
            ).all()
            for checksum, names in candidates:
                # Compare normalized spellings, so either probe above can
                # reach a row stored in the form the other one used.
                if name_normalize(spelling) in [
                    name_normalize(stored) for stored in json.loads(names)
                ]:
                    return session.get(cls, checksum)
        return None

    @classmethod
    def get_all(cls, session: Session) -> Sequence[Snippet]:
        """Return all snippets in the database."""
        return session.exec(select(cls)).all()

    @classmethod
    def stream_all(cls, session: Session, batch_size: int = 1000) -> Iterator[Snippet]:
        """Yield all snippets in batches, bounding memory for large databases."""
        yield from session.exec(select(cls)).yield_per(batch_size)

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
        """Return all snippets in a given collection."""
        return session.exec(
            select(cls).where(cls.collection == name_normalize(collection_name))
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
