"""Utilities for caching and loading the MinHash LSH index.

The LSH index itself lives in the database (``lsh_bucket`` / ``lsh_meta``
tables, see ``resembl.lsh``): building streams rows in batches, queries are
indexed lookups, and single snippet changes update only that snippet's rows.
No large in-memory index is ever pickled to disk anymore.

Legacy pickle cache files (written by pre-SQLite versions) are *not* loaded:
an index cache is untrusted executable content once unpickled, so anyone who
can plant a file under the user's cache directory would otherwise gain code
execution the next time ``find`` runs.  Stale cache files are ignored and
removed by the next write operation; the index simply rebuilds from the
database instead.
"""

import logging
import os
import time
from collections.abc import Callable

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, func, select, text

from .lsh import (
    ResemblLSH,
    band_buckets,
    build_insert_sql,
    fingerprint_version_set,
    insert_rows,
    lsh_index_clear,
    lsh_meta_get,
    lsh_meta_matches,
    lsh_meta_set,
)
from .models import FINGERPRINT_VERSION, Snippet

# Default permutation count when (re)building the index: shared with scoring.
from .scoring import NUM_PERMUTATIONS as DEFAULT_NUM_PERMUTATIONS
from .scoring import minhash_ensure_packed

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = "~/.cache/resembl"

#: Rows buffered per band before a sorted bulk insert during index builds.
#: Every snippet contributes one row to *every* band, so all bands fill
#: uniformly and peak buffering is ``b * _BAND_CHUNK`` pairs — ~150 MiB at
#: the default threshold / 128 permutations (b = 25), vs ~600 MiB at the
#: previous 100k.  Throughput is unaffected: each flush still issues one
#: large executemany per band, and sorting more, smaller runs costs no
#: more comparisons overall than fewer big ones.
_BAND_CHUNK = 25_000

#: Number of snippets fetched per keyset query during an index build.  Kept
#: modest so the buffered row dicts stay bounded regardless of band count.
_BATCH_SIZE = 2_000

#: Bounded retries for a concurrent index build on SQLite (see
#: ``lsh_index_build``): when two CLI processes cold-find the same database,
#: SQLite's single-writer lock makes the loser fail partway through a
#: multi-minute build.  Identical databases build identical indexes, so the
#: loser may retry after the winner finishes.
_BUILD_RETRIES = 3
#: Linear backoff between build retries, in seconds.
_BUILD_RETRY_BACKOFF = 3


def cache_dir_get() -> str:
    """Return the cache directory, respecting override environment variables.

    ``RESEMBL_CACHE_DIR`` wins outright.  Otherwise ``$XDG_CACHE_HOME`` is
    honored when set (freedesktop base-directory spec), falling back to the
    historical ``~/.cache/resembl`` default.
    """
    override = os.environ.get("RESEMBL_CACHE_DIR")
    if override:
        return os.path.expanduser(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return os.path.join(xdg, "resembl")
    return os.path.expanduser(DEFAULT_CACHE_DIR)


def db_checksum_path_get() -> str:
    """Return the path to the DB checksum file."""
    return os.path.join(cache_dir_get(), "db_checksum.txt")


def lsh_cache_path_get(threshold: float) -> str:
    """Return the path to the LSH cache file for a given threshold."""
    return os.path.join(cache_dir_get(), f"lsh_{threshold:.2f}.pkl")


def lsh_index_build(
    session: Session,
    threshold: float,
    num_perm: int,
    progress: Callable[[int, int], None] | None = None,
) -> ResemblLSH | None:
    """Build the LSH index from snippets in the database.

    Bucket rows are computed directly from the packed fingerprints (no
    MinHash object construction) and bulk-inserted in batches, so memory
    stays bounded even for very large databases.  Any previous index is
    replaced.

    The write is split into many small transactions instead of one giant
    one: a single transaction spanning the whole build grows the WAL to the
    size of the index (hundreds of MB at scale) and forces one huge
    checkpoint at commit, while periodic commits let WAL autocheckpoint keep
    the log bounded.  A crash mid-build leaves only a partial index, which
    is invisible until ``lsh_meta`` is set and is wiped on the next build,
    so atomicity is not required here.

    On SQLite the ``checksum`` secondary index is dropped for the duration
    of the build — bulk-loading into it row-by-row would double the insert
    cost — and recreated in a single bulk pass afterwards.  The page cache
    is also raised (the default 8 MiB thrashes on random b-tree inserts),
    and rows are inserted band-major sorted by bucket so the primary key
    grows by sequential append.

    If *progress* is given, it is called as ``progress(done, total)`` with
    the number of snippets processed so far and the total, roughly once per
    batch — let long-running CLI builds report status instead of appearing
    hung.

    A concurrent build of the same database (two CLI processes cold-finding
    together) makes one of them fail on SQLite's single-writer lock partway
    through the build.  Since identical databases build identical indexes,
    the whole build is retried a bounded number of times with a brief
    backoff — the loser waits for the winner instead of crashing with a raw
    ``database is locked`` traceback.
    """
    try:
        lsh = ResemblLSH(session, threshold, num_perm)
    except ValueError as e:
        logger.error(
            "Error: Invalid LSH parameters. The threshold (%s) may be too high "
            "for the number of permutations (%s).",
            threshold,
            num_perm,
        )
        logger.error("  -> Original error: %s", e)
        return None

    for attempt in range(_BUILD_RETRIES):
        try:
            _build_once(lsh, threshold, num_perm, progress)
            return lsh
        except OperationalError:
            session.rollback()  # abort the failed transaction before retrying
            if attempt + 1 < _BUILD_RETRIES:
                logger.warning(
                    "Index build hit a database lock (another process may be "
                    "writing); retrying in %ds…",
                    _BUILD_RETRY_BACKOFF * (attempt + 1),
                )
                time.sleep(_BUILD_RETRY_BACKOFF * (attempt + 1))
    logger.error(
        "Index build failed repeatedly (another process may be writing to "
        "this database); retry once it is idle."
    )
    return None


def _build_once(
    lsh: ResemblLSH,
    threshold: float,
    num_perm: int,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Run one full index build inside ``lsh_index_build``'s retry loop."""
    session = lsh.session

    lsh_index_clear(session)

    is_sqlite = session.get_bind().dialect.name == "sqlite"
    if is_sqlite:
        session.execute(text("DROP INDEX IF EXISTS ix_lsh_bucket_checksum"))
        # Random bucket inserts into a large b-tree thrash the default 8 MiB
        # page cache; a few hundred MiB keeps the active index pages resident
        # during the build.  Soft upper bound — SQLite does not preallocate.
        session.execute(text("PRAGMA cache_size=-262144"))
        session.commit()

    # The build's rows are unique by construction; DuckDB's ON CONFLICT
    # handling is ~7x slower than a plain insert and dominates the build.
    insert_sql = build_insert_sql(session)
    # Rows are buffered per band and inserted band-major, sorted by bucket, so
    # the (band, bucket, checksum) primary key grows by sequential append
    # instead of random probe — random inserts into a deep b-tree decay badly
    # (measured ~4x slower by the tail of a 12.5M-row build).
    band_rows: list[list[tuple[str, str]]] = [[] for _ in range(lsh.b)]

    def flush_band(band: int) -> None:
        pairs = sorted(band_rows[band])
        insert_rows(
            session,
            insert_sql,
            [{"band": band, "bucket": bucket, "checksum": checksum} for bucket, checksum in pairs],
        )
        session.commit()
        band_rows[band].clear()

    total_snippets = session.exec(
        select(func.count(Snippet.checksum))  # type: ignore[arg-type]
    ).one()
    processed = 0
    skipped_corrupt = 0
    for batch in Snippet.iter_minhash_batches(session, _BATCH_SIZE):
        for checksum, minhash in batch:
            try:
                packed = minhash_ensure_packed(minhash)
            except ValueError:
                # A corrupt fingerprint (disk rot, a bad merge source) must
                # not brick the whole build — every find would crash.  Skip
                # the snippet; a reindex recomputes its fingerprint from the
                # code and heals it.
                skipped_corrupt += 1
                logger.warning("Skipping snippet %s: corrupt fingerprint.", checksum)
                continue
            try:
                buckets = band_buckets(packed, num_perm, lsh.b, lsh.r)
            except ValueError:
                # A well-formed blob written at a different permutation count
                # is stale for this build (mixed-perm databases from config
                # flips between writes).  Skip it like a corrupt blob instead
                # of crashing the build; a reindex at the requested count
                # recomputes it from the snippet's code.
                skipped_corrupt += 1
                logger.warning(
                    "Skipping snippet %s: fingerprint permutation count does not match %d.",
                    checksum,
                    num_perm,
                )
                continue
            for band, bucket in enumerate(buckets):
                band_rows[band].append((bucket, checksum))
        processed += len(batch)
        if progress is not None:
            progress(processed, total_snippets)
        for band in range(lsh.b):
            if len(band_rows[band]) >= _BAND_CHUNK:
                flush_band(band)
    for band in range(lsh.b):
        if band_rows[band]:
            flush_band(band)
    if skipped_corrupt:
        logger.warning(
            "Index build skipped %d snippet(s) with unusable fingerprints "
            "(corrupt, or written at a different permutation count); "
            "`resembl reindex --force` recomputes them from their code.",
            skipped_corrupt,
        )

    if is_sqlite:
        session.execute(
            text("CREATE INDEX IF NOT EXISTS ix_lsh_bucket_checksum ON lsh_bucket (checksum)")
        )
        session.commit()

    lsh_meta_set(session, threshold, num_perm)
    fingerprint_version_set(session, FINGERPRINT_VERSION)


def lsh_index_add(session: Session, checksum: str, minhash_bytes: bytes) -> bool:
    """Incrementally add one snippet to the DB-backed index, if one is built."""
    meta = lsh_meta_get(session)
    if meta is None:
        return False
    lsh = ResemblLSH(session, meta[0], meta[1])
    lsh.insert(checksum, minhash_bytes)
    _remove_pickle_cache(meta[0])
    return True


def lsh_index_add_batch(session: Session, items: list[tuple[str, bytes]]) -> int:
    """Incrementally add many snippets to the DB-backed index, if one is built."""
    meta = lsh_meta_get(session)
    if meta is None or not items:
        return 0
    lsh = ResemblLSH(session, meta[0], meta[1])
    added = lsh.insert_batch(items)
    _remove_pickle_cache(meta[0])
    return added


def lsh_index_remove(session: Session, checksum: str) -> bool:
    """Incrementally remove one snippet from the DB-backed index, if built."""
    meta = lsh_meta_get(session)
    if meta is None:
        return False
    lsh = ResemblLSH(session, meta[0], meta[1])
    lsh.remove(checksum)
    _remove_pickle_cache(meta[0])
    return True


def _remove_pickle_cache(threshold: float) -> None:
    """Delete the legacy pickle cache file for *threshold* (if any).

    Best-effort: callers run this after their database writes are already
    committed, so a failure here (e.g. a read-only file left by another
    user) must be logged and skipped instead of crashing an operation that
    actually succeeded.
    """
    cache_dir = cache_dir_get()
    if not os.path.exists(cache_dir):
        return
    for name in (lsh_cache_path_get(threshold), db_checksum_path_get()):
        try:
            if os.path.isfile(name):
                os.remove(name)
        except OSError as e:
            logger.warning("Could not remove legacy cache file %s: %s", name, e)


def lsh_cache_save(
    session: Session,
    threshold: float,
    num_perm: int = DEFAULT_NUM_PERMUTATIONS,
) -> None:
    """Record that the DB-backed index is current for these parameters.

    The bucket rows already live in the database; saving only stamps the
    metadata row and removes any legacy pickle cache file.  Cache files were
    historically pickles — loading one is arbitrary code execution — so no
    cache content is ever written to disk.
    """
    lsh_meta_set(session, threshold, num_perm)
    _remove_pickle_cache(threshold)


def lsh_cache_load(
    session: Session,
    threshold: float,
    num_perm: int = DEFAULT_NUM_PERMUTATIONS,
) -> ResemblLSH | None:
    """Load the LSH index if it is still valid.

    The database-backed index is used when its metadata matches the requested
    parameters.  Legacy pickle cache files are deliberately NOT loaded —
    unpickling a file is arbitrary code execution, and the cache directory is
    not a trust boundary (see the module docstring).  A stale or missing
    index returns ``None`` so callers rebuild from the database.
    """
    meta = lsh_meta_get(session)
    if lsh_meta_matches(meta, threshold, num_perm):
        return ResemblLSH(session, threshold, num_perm)
    return None
