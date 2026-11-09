"""Database models used by resembl."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, Index, Integer, String, Text
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


# Re-exported for external backward compatibility only; in-tree code
# imports these from ``resembl.clock`` directly, so ``models`` is not the
# clock owner and ``config``/``cache``/``core`` no longer pull the ORM
# tables in to read a monotonic clock or a retry wait. The names stay
# here because ``from resembl.models import timestamp_now`` (and the
# ``created_at`` default factories below) keep working unchanged.
from .clock import (  # noqa: F401
    CLOCK_ENV_VAR,
    avg_time_per_item,
    monotonic_now,
    parse_instant,
    timestamp_normalize,
    timestamp_now,
)


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
    # A leading BOM (a UTF-8 file read as ``utf-8``) is not part of the
    # name, for the same reason as in ``normalize_unicode``.  Surrounding
    # whitespace (including Unicode spaces such as U+00A0, which
    # ``str.strip`` removes) is not part of the name either: every other
    # ingestion path (``snippet_name_add``, ``collection_create``) strips
    # it, so a name that kept it would be stored under a spelling no
    # lookup strips its way to.  Both are stripped in a loop: a BOM can
    # sit on either side of the padding (``"\ufeff café"``), so a single
    # ``strip().lstrip(BOM)`` leaves the padding that followed the BOM.
    stripped = name
    while True:
        next_stripped = stripped.strip().lstrip("\ufeff")
        if next_stripped == stripped:
            break
        stripped = next_stripped
    return unicodedata.normalize("NFC", stripped)


def name_json_escape(text: str) -> str:
    """Return *text* as it appears inside a stored ``names`` / ``tags`` column.

    Both columns hold a JSON list written by :func:`json.dumps`, which escapes
    every non-ASCII character: the name ``café`` is stored with ``é`` written
    as the escape sequence ``\\u00e9``, not as the character itself.  A SQL
    ``LIKE`` probe carrying the character therefore matches no row, which is
    why every name lookup and name search escapes its input this way first.
    """
    return json.dumps(text)[1:-1]


class Collection(SQLModel, table=True):
    """A named group of snippets (e.g., 'libc patterns', 'crypto routines')."""

    name: str = Field(primary_key=True, max_length=128)
    # Explicit Text: SQLModel infers a plain `str` field as VARCHAR, and
    # MySQL renders VARCHAR without a length as VARCHAR(255). A collection
    # description longer than 255 chars would then be silently truncated
    # (or rejected in strict mode) on MySQL while staying whole on SQLite
    # and PostgreSQL. TEXT is storage-equivalent everywhere and has no
    # length cap, so the column cannot lose data on any backend.
    description: str = Field(sa_column=Column(Text, nullable=False))
    created_at: str = Field(sa_column=Column(Text, nullable=False), default_factory=timestamp_now)

    @classmethod
    def get_all(cls, session: Session, limit: int | None = None) -> Sequence[Collection]:
        """Return all collections, in name order.

        Name order rather than the backend's row order: `collection list`
        renders these rows directly, and a merge snapshots them in the same
        order, so an unspecified order is a run-to-run difference in output.

        *limit* caps the fetch symmetrically with :meth:`Snippet.get_all`.
        """
        if limit is not None and limit <= 0:
            return []
        stmt = select(cls).order_by(cls.name)
        if limit is not None:
            stmt = stmt.limit(limit)
        return session.exec(stmt).all()

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

    __table_args__ = (
        Index("ix_snippetversion_checksum_created", "snippet_checksum", "created_at"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(Integer, primary_key=True, autoincrement=False),
    )
    # No single-column index here: the composite
    # (snippet_checksum, created_at) index declared via Index below
    # covers every lookup on this column (its leftmost prefix is
    # snippet_checksum).  The database-level helper `_ensure_indexes`
    # creates that composite on existing databases; the plain
    # single-column index would only duplicate it.
    snippet_checksum: str = Field(
        sa_column=Column(
            String(64),
            ForeignKey("snippet.checksum"),
            nullable=False,
        )
    )
    code: str = Field(sa_column=Column(Text, nullable=False))
    minhash: bytes
    # Explicit Text for the same MySQL VARCHAR(255) reason as
    # Collection.created_at: an ISO-8601 timestamp is short, but a plain
    # `str` field would still render as VARCHAR(255) on MySQL and the
    # column should match its TEXT siblings (code) rather than depend on
    # the value staying under the cap.
    created_at: str = Field(sa_column=Column(Text, nullable=False), default_factory=timestamp_now)

    @classmethod
    def get_by_checksum(
        cls, session: Session, checksum: str, limit: int | None = None
    ) -> Sequence[SnippetVersion]:
        """Return all versions for a given snippet, newest first.

        *limit* caps the fetch so a caller asking for history does not pull
        an unbounded version list into memory; ``None`` (the default) keeps
        the historical unbounded behavior for callers that need every row.
        """
        if limit is not None and limit <= 0:
            return []
        stmt = (
            select(cls)
            .where(cls.snippet_checksum == checksum)
            .order_by(cls.created_at.desc(), cls.id.desc())  # type: ignore[attr-defined, union-attr]
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return session.exec(stmt).all()


class Snippet(SQLModel, table=True):
    """Model representing a stored assembly snippet."""

    __table_args__ = (Index("ix_snippet_collection_checksum", "collection", "checksum"),)

    checksum: str = Field(primary_key=True, max_length=64)
    names: str = Field(sa_column=Column(Text, nullable=False))  # JSON-encoded list
    code: str = Field(sa_column=Column(Text, nullable=False))
    minhash: bytes
    tags: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    # No single-column index here: the composite (collection, checksum)
    # index declared via Index below covers every lookup on this column
    # (its leftmost prefix is collection).  The database-level helper
    # `_ensure_indexes` creates that composite on existing databases;
    # the plain single-column index would only duplicate it.
    collection: str | None = Field(
        default=None,
        sa_column=Column(
            String(128),
            ForeignKey("collection.name"),
            nullable=True,
        ),
    )

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
        # character can never match its own row: encode it the way
        # :func:`name_json_escape` describes, then LIKE-escape the result
        # ('\\' is the escape character, so every stored backslash —
        # including the ones JSON just introduced — must be doubled) and
        # protect ``%`` / ``_`` so they match themselves instead of widening
        # the probe.
        #
        # A name is probed in both canonical forms, because a row written
        # before names were normalized (or imported from a macOS filesystem,
        # which hands out NFD) stores the other one.  Both probes compare
        # against one normalized query, computed once per lookup.
        needle = name_normalize(name)
        for spelling in dict.fromkeys((needle, unicodedata.normalize("NFD", name))):
            literal = (
                name_json_escape(spelling)
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            # Stream candidates: a short/common alias can LIKE-match many
            # rows, and ``.all()`` would materialize every (checksum, names)
            # pair before the first verification.  ``yield_per`` keeps the
            # probe O(batch) in memory; verification order is unchanged
            # (the first verified candidate still wins).
            candidates = session.exec(
                select(cls.checksum, cls.names).where(
                    cls.names.like(f'%"{literal}"%', escape="\\")  # type: ignore[attr-defined]
                )
            ).yield_per(1000)
            # The query is already NFC.  ASCII is NFC as well, so composing
            # an ASCII alias again cannot change equality.  A non-ASCII alias
            # may still be the decomposed spelling (a row from before names
            # were composed, or from macOS), and either probe above can reach
            # a row stored in the form the other one used.
            for checksum, names in candidates:
                if any(
                    needle == (stored if stored.isascii() else name_normalize(stored))
                    for stored in json.loads(names)
                ):
                    return session.get(cls, checksum)
        return None

    @classmethod
    def get_all(cls, session: Session, limit: int | None = None) -> Sequence[Snippet]:
        """Return snippets in the database, in checksum order.

        Ordered by the primary key so the row order is a property of the
        data rather than of the plan the backend happens to pick: every
        caller renders, paginates or float-sums these rows, and none of
        that may differ between two runs over the same database.

        *limit* caps the fetch so a caller paging or sampling does not pull
        the whole corpus (``code`` column included) into memory; ``None``
        (the default) keeps the historical unbounded behavior.  Prefer
        :meth:`stream_all` / :meth:`iter_batches` for full scans.
        """
        if limit is not None and limit <= 0:
            return []
        stmt = select(cls).order_by(cls.checksum)
        if limit is not None:
            stmt = stmt.limit(limit)
        return session.exec(stmt).all()

    @classmethod
    def stream_all(cls, session: Session, batch_size: int = 1000) -> Iterator[Snippet]:
        """Yield all snippets in batches, bounding memory for large databases.

        Checksum-ordered, like :meth:`get_all`; the sort rides the primary
        key index, so a streamed scan stays a merge over the b-tree rather
        than a sort of the whole table.
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        yield from session.exec(select(cls).order_by(cls.checksum)).yield_per(batch_size)

    @staticmethod
    def _keyset_batches(session: Session, batch_size: int, columns: tuple) -> Iterator[list]:
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        # When the full entity is projected, ``exec`` returns ORM objects
        # (no subscript); a column projection returns ``Row`` tuples.
        # The keyset cursor is always the first column, so index 0 holds
        # the checksum in both shapes, but reaching it must not assume
        # ``row[0]`` works on a ``Snippet``.
        entity_projected = any(isinstance(c, type) and issubclass(c, SQLModel) for c in columns)
        last: str | None = None
        while True:
            stmt = select(*columns).order_by(Snippet.checksum).limit(batch_size)
            if last is not None:
                stmt = stmt.where(Snippet.checksum > last)
            batch = list(session.exec(stmt).all())
            if not batch:
                return
            yield batch
            last = (
                batch[-1].checksum
                if entity_projected or hasattr(batch[-1], "checksum")
                else batch[-1][0]
            )

    @classmethod
    def iter_batches(cls, session: Session, batch_size: int = 1000) -> Iterator[list[Snippet]]:
        """Yield ``Snippet`` lists via keyset pagination on the checksum PK.

        Unlike a streaming cursor (``yield_per``), each batch fully consumes
        its query before being yielded, so callers can safely write to the
        same session/connection between batches — required by SQLite, which
        otherwise raises ``database is locked`` when a write happens while a
        read cursor is still open on the connection.
        """
        yield from cls._keyset_batches(session, batch_size, (cls,))

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
        yield from (
            ([(row[0], row[1]) for row in batch])
            for batch in cls._keyset_batches(session, batch_size, (cls.checksum, cls.minhash))
        )

    @classmethod
    def get_by_collection(
        cls, session: Session, collection_name: str, limit: int | None = None
    ) -> Sequence[Snippet]:
        """Return snippets in a given collection, in checksum order.

        *limit* caps the fetch so a large collection is not pulled into
        memory whole; ``None`` (the default) keeps the historical unbounded
        behavior.  Prefer the streamed ``snippet_collection_names_stream``
        for rendering names without loading the ``code`` column.
        """
        if limit is not None and limit <= 0:
            return []
        stmt = (
            select(cls)
            .where(cls.collection == name_normalize(collection_name))
            .order_by(cls.checksum)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return session.exec(stmt).all()

    def get_minhash_obj(self) -> MinHash:
        """Return the stored MinHash object for this snippet."""
        return minhash_unpack(self.minhash)


#: Maximum width of the ``lsh_bucket.bucket`` hex key column.  The bound is
#: chosen with headroom: the composite primary key (utf8mb4) is
#: 640*4 + checksum 64*4 + int, about 2.8 kB against MySQL InnoDB's
#: 3072-byte index-key limit.  Banding configurations that would need wider
#: keys are
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
    checksum: str = Field(
        sa_column=Column(
            String(64),
            ForeignKey("snippet.checksum"),
            nullable=False,
            primary_key=True,
            index=True,
        )
    )


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
