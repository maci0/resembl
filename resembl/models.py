"""Database models used by resembl."""

from __future__ import annotations

import json
import operator
import pickle
import struct
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlmodel import Field, Session, SQLModel, select

if TYPE_CHECKING:
    from datasketch import MinHash

#: Magic prefix for the compact MinHash byte format.  A stored fingerprint
#: either starts with this prefix (``struct``-packed uint32 hash values,
#: self-describing) or is a legacy ``pickle`` blob produced by older versions.
MINHASH_MAGIC = b"RMLH"

#: Upper bound on the permutation count accepted when unpacking a stored
#: fingerprint.  Real configurations use 64–128; anything near this limit is
#: corrupt or hostile.  The bound also keeps ``struct`` format strings and
#: ``MinHash`` allocations sane on malformed input.
_MAX_NUM_PERM = 1 << 12

#: Cached MinHash templates keyed by num_perm, used to skip datasketch's
#: per-construction permutation regeneration (~260 µs — the dominant cost of
#: building a fingerprint).  Permutations depend only on (num_perm, seed),
#: so cloning a template produces identical fingerprints.
_MINHASH_TEMPLATES: dict[int, object] = {}


def minhash_new(num_perm: int = 128) -> MinHash:
    """Return a fresh all-max MinHash without regenerating permutations.

    datasketch's constructor draws the permutation arrays from a numpy
    random stream on every call (~260 µs at 128 perms — most of the import
    worker's CPU).  Cloning a cached template (deepcopy of two small numpy
    arrays) is ~15x faster and yields identical permutations (seed 1), so
    fingerprints are byte-for-byte the same.
    """
    import copy

    from datasketch import MinHash

    template = _MINHASH_TEMPLATES.get(num_perm)
    if template is None:
        template = MinHash(num_perm=num_perm)
        _MINHASH_TEMPLATES[num_perm] = template
    return copy.deepcopy(template)


def minhash_num_perm(data: bytes) -> int:
    """Return the permutation count encoded in a packed fingerprint header.

    Validates the header (magic, 4-byte count in range) and raises
    ``ValueError`` on malformed input — callers that unpack untrusted blobs
    (legacy databases, ``merge`` sources, corrupted files) must never see raw
    ``struct.error`` or pathological counts.
    """
    if len(data) < 8:
        raise ValueError("Corrupt MinHash payload: shorter than the 8-byte header.")
    num_perm = struct.unpack(">I", data[4:8])[0]
    if num_perm < 2 or num_perm > _MAX_NUM_PERM:
        raise ValueError(
            f"Corrupt MinHash payload: implausible permutation count {num_perm}."
        )
    expected = 8 + 4 * num_perm
    if len(data) != expected:
        raise ValueError(
            f"Corrupt MinHash payload: expected {expected} bytes, got {len(data)}."
        )
    return num_perm


def minhash_pack(m: MinHash) -> bytes:
    """Serialize a MinHash into a compact, self-describing byte string.

    The format is ``MINHASH_MAGIC`` + big-endian uint32 ``num_perm`` +
    ``num_perm`` big-endian uint32 hash values (512 bytes for the default
    128 permutations — several times smaller than a pickle).
    """
    digest = m.digest()
    num_perm = len(digest)
    return MINHASH_MAGIC + struct.pack(f">I{num_perm}I", num_perm, *digest)


def minhash_unpack(data: bytes) -> MinHash:
    """Deserialize a MinHash stored with :func:`minhash_pack`.

    Falls back to ``pickle.loads`` for legacy pickled fingerprints, so
    databases created by older versions keep working unchanged.  Malformed
    packed payloads raise ``ValueError`` (never low-level ``struct`` errors).
    """
    if data.startswith(MINHASH_MAGIC):
        from datasketch import MinHash

        num_perm = minhash_num_perm(data)
        values = struct.unpack(f">{num_perm}I", data[8 : 8 + 4 * num_perm])
        return MinHash(num_perm=num_perm, hashvalues=list(values))
    return pickle.loads(data)


def minhash_jaccard(packed_a: bytes, packed_b: bytes) -> float:
    """Jaccard similarity of two stored MinHash byte blobs (0.0–1.0).

    Fast path: when both blobs use the compact packed format, similarity is
    computed directly from the uint32 arrays, bypassing the ``MinHash``
    constructor — which dominates the cost when scoring thousands of
    candidates (the constructor is ~300 µs per object).  Falls back to
    object-based comparison for legacy pickled blobs.

    The metric matches :meth:`datasketch.MinHash.jaccard` exactly: the
    fraction of positions whose hash values are equal (element-wise), not a
    set intersection — the two differ on degenerate fingerprints where hash
    values repeat (e.g. short or empty snippets).
    """
    if not (packed_a.startswith(MINHASH_MAGIC) and packed_b.startswith(MINHASH_MAGIC)):
        return minhash_unpack(packed_a).jaccard(minhash_unpack(packed_b))
    # Byte-identical blobs are exact matches — a single C-level memcmp that
    # is ~100x faster than the element-wise loop.  This is the common
    # self-match / exact-duplicate case in candidate scoring.
    if packed_a == packed_b:
        return 1.0
    # The two blobs may encode different permutation counts; reject the
    # mismatch the same way datasketch's MinHash.jaccard does.
    num_perm_a = minhash_num_perm(packed_a)
    num_perm_b = minhash_num_perm(packed_b)
    if num_perm_a != num_perm_b:
        raise ValueError(
            "Cannot compute Jaccard for MinHash blobs with different "
            f"permutation counts ({num_perm_a} vs {num_perm_b})."
        )
    a = struct.unpack(f">{num_perm_a}I", packed_a[8 : 8 + 4 * num_perm_a])
    b = struct.unpack(f">{num_perm_b}I", packed_b[8 : 8 + 4 * num_perm_b])
    # ``map(operator.eq, ...)`` iterates with C-level callbacks instead of a
    # Python ``for``/generator — ~1.6x faster over 128 hash values.
    return sum(map(operator.eq, a, b)) / num_perm_a


def minhash_ensure_packed(data: bytes) -> bytes:
    """Return *data* in the compact packed format, converting legacy pickles."""
    if data.startswith(MINHASH_MAGIC):
        # Validate rather than trust: blobs from merged databases or legacy
        # files may be corrupt, and every downstream use (banding, Jaccard,
        # the query path) assumes a well-formed header.
        minhash_num_perm(data)
        return data
    return minhash_pack(minhash_unpack(data))


class Collection(SQLModel, table=True):  # type: ignore
    """A named group of snippets (e.g., 'libc patterns', 'crypto routines')."""

    name: str = Field(primary_key=True)
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
    """A historical version of a snippet's code."""

    id: int | None = Field(default=None, primary_key=True)
    snippet_checksum: str = Field(index=True)
    code: str
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

    checksum: str = Field(primary_key=True)
    names: str  # JSON-encoded list of strings
    code: str
    minhash: bytes
    tags: str = Field(default="[]")
    collection: str | None = Field(default=None, index=True)

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
    """

    __tablename__ = "lsh_bucket"  # noqa: N815

    band: int = Field(primary_key=True)
    bucket: bytes = Field(primary_key=True)
    checksum: str = Field(primary_key=True, index=True)


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
