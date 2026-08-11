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

_INSERT_MYSQL = (
    "INSERT IGNORE INTO lsh_bucket (band, bucket, checksum) "
    "VALUES (:band, :bucket, :checksum)"
)

_INSERT_PLAIN = (
    "INSERT INTO lsh_bucket (band, bucket, checksum) "
    "VALUES (:band, :bucket, :checksum)"
)

#: Upsert variants for the single-row ``lsh_meta`` / ``app_meta`` upserts.
_META_UPSERT_SQLITE_PG = (
    "INSERT INTO lsh_meta (id, threshold, num_perm) VALUES (1, :t, :n) "
    "ON CONFLICT(id) DO UPDATE SET threshold = :t, num_perm = :n"
)
_META_UPSERT_MYSQL = (
    "INSERT INTO lsh_meta (id, threshold, num_perm) VALUES (1, :t, :n) "
    "ON DUPLICATE KEY UPDATE threshold = :t, num_perm = :n"
)
_META_UPSERT_DUCKDB = (
    "INSERT INTO lsh_meta (id, threshold, num_perm) VALUES (1, :t, :n) "
    "ON CONFLICT (id) DO UPDATE SET threshold = :t, num_perm = :n"
)

_VERSION_UPSERT_SQLITE_PG = (
    "INSERT INTO app_meta (key, value) VALUES ('fingerprint_version', :v) "
    "ON CONFLICT(key) DO UPDATE SET value = :v"
)
_VERSION_UPSERT_MYSQL = (
    "INSERT INTO app_meta (key, value) VALUES ('fingerprint_version', :v) "
    "ON DUPLICATE KEY UPDATE value = :v"
)
_VERSION_UPSERT_DUCKDB = (
    "INSERT INTO app_meta (key, value) VALUES ('fingerprint_version', :v) "
    "ON CONFLICT (key) DO UPDATE SET value = :v"
)


def dialect_name(session: Session) -> str:
    """Return the SQLAlchemy dialect name of the session's connection."""
    return session.get_bind().dialect.name


#: Rows per multi-row ``VALUES`` statement on DuckDB.  DuckDB's Python
#: executemany path is pathologically slow (~7k rows/s measured) while
#: multi-row VALUES inserts reach ~93k rows/s — the difference dominates
#: the one-time index build on DuckDB (cold find at 15k snippets: 65s vs
#: ~5s on SQLite).  Statements of 1000 rows keep the SQL string small.
_DUCKDB_VALUES_CHUNK = 1000


def _insert_rows(session: Session, sql: str, rows: list[dict[str, object]]) -> None:
    """Insert *rows* with the dialect's fastest strategy.

    Non-DuckDB dialects use the executemany template *sql* unchanged
    (SQLAlchemy's executemany is C-accelerated there).  DuckDB falls back
    to multi-row ``VALUES`` statements, which measured 13x faster than its
    executemany path.  ``ON CONFLICT`` suffixes present in *sql* (the
    incremental-sync variant) are preserved.

    The row values are ``(band: int, bucket: hex, checksum: hex)`` — band
    is a small integer and bucket/checksum are lowercase-hex strings by
    construction (``bytes.hex()`` / ``sha256`` digests), so interpolating
    them into the SQL is safe: a hex string cannot contain quotes or other
    SQL metacharacters.
    """
    if not rows:
        return
    if dialect_name(session) != "duckdb":
        session.execute(text(sql), params=rows)
        return
    conflict = " ON CONFLICT DO NOTHING" if "ON CONFLICT" in sql else ""
    for start in range(0, len(rows), _DUCKDB_VALUES_CHUNK):
        chunk = rows[start : start + _DUCKDB_VALUES_CHUNK]
        values = ",".join(
            f"({row['band']}, '{row['bucket']}', '{row['checksum']}')" for row in chunk
        )
        # exec_driver_sql, not text(): the statement has no bind parameters,
        # and text()'s marker scan would misread literal ``$n`` / ``:name``
        # sequences inside values as bind placeholders.
        session.connection().exec_driver_sql(
            "INSERT INTO lsh_bucket (band, bucket, checksum) VALUES "
            f"{values}{conflict}"
        )


def _insert_sql(session: Session) -> str:
    """Return the dialect-appropriate upsert-ignore statement.

    SQLite: ``INSERT OR IGNORE``; MySQL/MariaDB: ``INSERT IGNORE``;
    PostgreSQL/DuckDB: ``ON CONFLICT DO NOTHING``.
    """
    dialect = dialect_name(session)
    if dialect == "mysql":
        return _INSERT_MYSQL
    if dialect in ("postgresql", "duckdb"):
        return _INSERT_PG
    return _INSERT_SQLITE


def _build_insert_sql(session: Session) -> str:
    """Return the insert used by one-time index builds.

    Build rows are unique by construction (one row per band per snippet,
    checksums are primary keys), so a plain ``INSERT`` is correct — and
    DuckDB's ``ON CONFLICT`` handling is ~7x slower than a plain insert,
    which dominates the one-time build there.  The incremental sync path
    keeps the conflict-ignore variants (idempotency matters there).
    """
    if dialect_name(session) == "duckdb":
        return _INSERT_PLAIN
    return _insert_sql(session)


def _meta_upsert_sql(session: Session) -> str:
    """Return the dialect-appropriate single-row upsert for ``lsh_meta``."""
    dialect = dialect_name(session)
    if dialect == "mysql":
        return _META_UPSERT_MYSQL
    if dialect == "duckdb":
        return _META_UPSERT_DUCKDB
    return _META_UPSERT_SQLITE_PG


def _version_upsert_sql(session: Session) -> str:
    """Return the dialect-appropriate upsert for the ``app_meta`` key."""
    dialect = dialect_name(session)
    if dialect == "mysql":
        return _VERSION_UPSERT_MYSQL
    if dialect == "duckdb":
        return _VERSION_UPSERT_DUCKDB
    return _VERSION_UPSERT_SQLITE_PG


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
    session.execute(text(_meta_upsert_sql(session)), {"t": threshold, "n": num_perm})
    session.commit()


def lsh_meta_clear(session: Session) -> None:
    """Remove the metadata row (the index must be rebuilt)."""
    session.execute(text("DELETE FROM lsh_meta WHERE id = 1"))
    session.commit()


def fingerprint_version_get(session: Session) -> int | None:
    """Return the stored fingerprint-format version, or ``None`` if unset."""
    row = session.execute(
        text("SELECT value FROM app_meta WHERE key = 'fingerprint_version'")
    ).one_or_none()
    return int(row[0]) if row is not None else None


def fingerprint_version_set(session: Session, version: int) -> None:
    """Stamp the fingerprint-format version (current algorithm)."""
    session.execute(
        text(_version_upsert_sql(session)),
        {"v": str(version)},
    )
    session.commit()


def fingerprint_version_clear(session: Session) -> None:
    """Remove the version stamp (blobs may be stale; force a reindex)."""
    session.execute(text("DELETE FROM app_meta WHERE key = 'fingerprint_version'"))
    session.commit()


def band_buckets(packed: bytes, num_perm: int, b: int, r: int) -> list[str]:
    """Compute the bucket key for each band of a packed fingerprint.

    ``packed`` is a ``minhash_pack`` blob (magic + num_perm + uint32 values).
    Exactly ``b`` bands of ``r`` consecutive hash values are used (matching
    datasketch's ``hashranges`` — when ``b * r < num_perm`` the tail values
    are ignored, exactly as datasketch does).  Each band is a big-endian
    uint32 run straight out of the packed blob, so band keys are produced by
    plain bytes slicing (C-level) rather than per-band ``struct`` packing —
    ~3.6x faster, which matters because the index build calls this once per
    band per snippet (12.5M+ times at 500k snippets).

    Keys are returned as fixed-width lowercase hex strings (20 bytes ->
    40 chars).  Every supported database can index a string column, whereas
    a raw ``BLOB`` column cannot be part of a primary key on MySQL/MariaDB.

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
    return [packed[base + i * step : base + (i + 1) * step].hex() for i in range(b)]


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

    def _buckets(self, packed: bytes) -> list[str]:
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
            _insert_rows(self.session, _insert_sql(self.session), chunk)
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
    # CREATE/DROP TABLE need an exclusive lock, and SQLite fails *immediately*
    # (busy_timeout cannot help) when the session still holds a shared read
    # snapshot from an earlier statement — the usual cause of "database is
    # locked" when two processes cold-build the same database concurrently.
    # Ending the transaction first lets the exclusive lock wait via the busy
    # timeout, so the loser blocks on the winner instead of crashing.
    session.commit()
    table_ensure(session)
    session.execute(text("DROP TABLE IF EXISTS lsh_bucket"))
    session.commit()
    table_ensure(session)  # recreate the (now empty) table
    lsh_meta_clear(session)
