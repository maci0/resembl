"""A SQLite-backed MinHash LSH index for resembl.

Why SQLite instead of datasketch's in-memory :class:`~datasketch.MinHashLSH`?

- The in-memory index must be rebuilt and pickled to a cache file on every
  change; for large databases that pickle is hundreds of megabytes and takes
  seconds to write and read back.
- Here the band buckets live in ordinary SQLite tables.  Building the index
  streams rows in batches, queries hit a handful of indexed lookups, and
  single snippet additions/deletions update only that snippet's rows.

The banding math is identical to datasketch (same ``_optimal_param``), so
recall behavior at a given threshold is equivalent.  Bucket keys are derived
directly from the packed uint32 fingerprints (see ``models.minhash_pack``),
which avoids constructing MinHash objects during index builds.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, text

from .models import minhash_num_perm, minhash_pack

if TYPE_CHECKING:
    from datasketch import MinHash


@lru_cache(maxsize=8)
def _banding_params(threshold: float, num_perm: int) -> tuple[int, int]:
    """Return the ``(b, r)`` banding for a ``(threshold, num_perm)`` pair.

    The banding is a pure function of its inputs, but datasketch's
    ``_optimal_param`` evaluates false-positive/false-negative probabilities
    via a scipy numerical integration (~13 ms) — and it was being recomputed
    on every :class:`ResemblLSH` construction, i.e. every query.  Caching the
    result turns a ~13 ms per-query cost into a dict lookup.  The scipy
    dependency is imported lazily so commands that never touch the index
    (list, stats, export, ...) skip the ~200 ms datasketch/scipy startup.
    """
    from datasketch.lsh import _optimal_param  # type: ignore[attr-defined]

    return _optimal_param(threshold, num_perm, 0.5, 0.5)


#: SQL for reading the single LSH metadata row (see ``LSHMeta``).
_META_SELECT = "SELECT threshold, num_perm FROM lsh_meta WHERE id = 1"

_INSERT_SQLITE = (
    "INSERT OR IGNORE INTO lsh_bucket (band, bucket, checksum) "
    "VALUES (:band, :bucket, :checksum)"
)

_INSERT_PG = (
    "INSERT INTO lsh_bucket (band, bucket, checksum) "
    "VALUES (:band, :bucket, :checksum) ON CONFLICT DO NOTHING"
)


def _insert_sql(session: Session) -> str:
    """Return the dialect-appropriate upsert-ignore statement."""
    dialect = session.get_bind().dialect.name
    return _INSERT_PG if dialect == "postgresql" else _INSERT_SQLITE


def table_ensure(session: Session) -> None:
    """Create the LSH tables if they do not exist yet (idempotent)."""
    SQLModel.metadata.create_all(session.get_bind())


def lsh_meta_get(session: Session) -> tuple[float, int] | None:
    """Return ``(threshold, num_perm)`` of the built index, or ``None``."""
    try:
        row = session.execute(text(_META_SELECT)).one_or_none()
    except OperationalError:  # table missing on pre-migration DBs
        return None
    if row is None:
        return None
    return float(row[0]), int(row[1])


def lsh_meta_set(session: Session, threshold: float, num_perm: int) -> None:
    """Record that ``lsh_bucket`` holds a complete index (id = 1)."""
    session.execute(
        text(
            "INSERT INTO lsh_meta (id, threshold, num_perm) VALUES (1, :t, :n) "
            "ON CONFLICT(id) DO UPDATE SET threshold = :t, num_perm = :n"
        ),
        {"t": threshold, "n": num_perm},
    )
    session.commit()


def lsh_meta_clear(session: Session) -> None:
    """Remove the metadata row (the index must be rebuilt)."""
    session.execute(text("DELETE FROM lsh_meta WHERE id = 1"))
    session.commit()


def band_buckets(packed: bytes, num_perm: int, b: int, r: int) -> list[bytes]:
    """Compute the bucket key for each band of a packed fingerprint.

    ``packed`` is a ``minhash_pack`` blob (magic + num_perm + uint32 values).
    Exactly ``b`` bands of ``r`` consecutive hash values are used (matching
    datasketch's ``hashranges`` — when ``b * r < num_perm`` the tail values
    are ignored, exactly as datasketch does).  Each band is a big-endian
    uint32 run straight out of the packed blob, so band keys are produced by
    plain bytes slicing (C-level) rather than per-band ``struct`` packing —
    ~3.6x faster, which matters because the index build calls this once per
    band per snippet (12.5M+ times at 500k snippets).

    Malformed blobs (bad header, wrong permutation count) raise
    ``ValueError`` rather than low-level ``struct`` errors.
    """
    if len(packed) < 8 or not packed.startswith(b"RMLH"):
        raise ValueError("Corrupt MinHash payload: missing RMLH magic.")
    if minhash_num_perm(packed) != num_perm:
        raise ValueError(
            "Corrupt MinHash payload: permutation count does not match the "
            f"expected {num_perm}."
        )
    step = 4 * r
    base = 8
    return [packed[base + i * step : base + (i + 1) * step] for i in range(b)]


class ResemblLSH:
    """A banded MinHash LSH index persisted in the ``lsh_bucket`` table.

    The bucket rows live in the caller's database; this object is just a thin
    facade providing ``insert`` / ``insert_batch`` / ``query`` / ``remove``
    over them.  It is cheap to construct (no data loaded) — it only needs the
    banding parameters derived from ``(threshold, num_perm)``.

    Mutation and query methods accept either a :class:`~datasketch.MinHash`
    object or a ``minhash_pack`` byte blob.
    """

    def __init__(self, session: Session, threshold: float, num_perm: int) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be in [0.0, 1.0]")
        if num_perm < 2:
            raise ValueError("Too few permutation functions")
        self.session = session
        self.num_perm = num_perm
        # Same banding optimization as datasketch's MinHashLSH (cached — see
        # ``_banding_params``; the raw computation is a ~13 ms scipy integral).
        self.b, self.r = _banding_params(threshold, num_perm)
        if self.b < 2:
            raise ValueError("The number of bands are too small (b < 2)")
        table_ensure(session)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _as_packed(value: bytes | MinHash) -> bytes:
        """Normalize a MinHash object or packed blob to packed bytes."""
        if hasattr(value, "digest"):
            return minhash_pack(value)
        return value  # type: ignore[return-value]

    def _buckets(self, packed: bytes) -> list[bytes]:
        return band_buckets(packed, self.num_perm, self.b, self.r)

    # -- mutation ----------------------------------------------------------

    def insert(self, key: str, value: bytes | MinHash) -> None:
        """Insert one fingerprint under *key* (MinHash or packed bytes)."""
        packed = self._as_packed(value)
        params = [
            {"band": band, "bucket": bucket, "checksum": key}
            for band, bucket in enumerate(self._buckets(packed))
        ]
        self.session.execute(text(_insert_sql(self.session)), params=params)
        self.session.commit()

    def insert_batch(self, items: list[tuple[str, bytes | MinHash]]) -> int:
        """Insert many ``(key, fingerprint)`` pairs; returns the row count."""
        rows: list[dict[str, object]] = []
        for key, value in items:
            packed = self._as_packed(value)
            rows.extend(
                {"band": band, "bucket": bucket, "checksum": key}
                for band, bucket in enumerate(self._buckets(packed))
            )
        for i in range(0, len(rows), 10_000):
            chunk = rows[i : i + 10_000]
            # Insert band-major sorted by bucket (see ``lsh_index_build``):
            # the (band, bucket, checksum) primary key then grows by
            # sequential append instead of random probe, which keeps large
            # incremental syncs (importing into an indexed database) fast.
            chunk.sort(key=lambda row: (row["band"], row["bucket"]))
            self.session.execute(text(_insert_sql(self.session)), params=chunk)
            self.session.commit()
        return len(rows)

    def remove(self, checksum: str) -> None:
        """Remove all index rows for *checksum*."""
        self.session.execute(
            text("DELETE FROM lsh_bucket WHERE checksum = :c"), {"c": checksum}
        )
        self.session.commit()

    # -- query -------------------------------------------------------------

    def query(self, value: bytes | MinHash) -> list[str]:
        """Return the keys whose fingerprints share a band bucket with *value*.

        All band lookups run in a single ``UNION ALL`` round trip instead of
        one query per band (~4x faster measured); each branch still uses the
        ``(band, bucket)`` primary-key prefix, and candidates are the union
        of keys across all bands, deduplicated.
        """
        packed = self._as_packed(value)
        buckets = self._buckets(packed)
        sql = " UNION ALL ".join(
            f"SELECT checksum FROM lsh_bucket WHERE band = :b{i} AND bucket = :k{i}"
            for i in range(len(buckets))
        )
        params: dict[str, object] = {}
        for i, bucket in enumerate(buckets):
            params[f"b{i}"] = i
            params[f"k{i}"] = bucket
        return list({row[0] for row in self.session.execute(text(sql), params).all()})


def lsh_index_clear(session: Session) -> None:
    """Wipe the index (buckets + metadata).  The next find rebuilds it.

    The bucket table is dropped rather than drained row-by-row: deleting
    millions of rows would churn the WAL and index pages, while a DROP is
    O(1).  An empty table is recreated immediately so subsequent inserts
    (e.g. during a build) keep working.
    """
    table_ensure(session)
    session.execute(text("DROP TABLE IF EXISTS lsh_bucket"))
    session.commit()
    table_ensure(session)  # recreate the (now empty) table
    lsh_meta_clear(session)
