"""Utilities for caching and loading the MinHash LSH index.

The LSH index itself lives in the database (``lsh_bucket`` / ``lsh_meta``
tables, see ``resembl.lsh``): building streams rows in batches, queries are
indexed lookups, and single snippet changes update only that snippet's rows.
No large in-memory index is ever pickled to disk anymore.

A legacy pickle cache (the pre-SQLite format) is still supported: files
written by older versions load transparently, and the first write operation
after an upgrade migrates to the database-backed index.
"""

import logging
import os
import pickle
import zlib
from collections.abc import Callable

from sqlmodel import Session, func, select, text

from .database import db_checksum_get
from .lsh import (
    ResemblLSH,
    _insert_sql,
    band_buckets,
    lsh_index_clear,
    lsh_meta_get,
    lsh_meta_set,
)
from .models import Snippet, minhash_ensure_packed

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = "~/.cache/resembl"

#: Default number of permutations used when (re)building the index.
DEFAULT_NUM_PERMUTATIONS = 128

#: Magic prefix for the current cache format.  New cache files are written as
#: this magic + zlib-compressed pickle; files without the prefix are treated as
#: legacy plain pickles and still load correctly (or raise UnpicklingError
#: when corrupted, matching previous behavior).
CACHE_MAGIC = b"RESEMBL-CACHE-V2"

#: Rows buffered per band before a sorted bulk insert during index builds.
_BAND_CHUNK = 100_000

#: Number of snippets fetched per keyset query during an index build.  Kept
#: modest so the buffered row dicts stay bounded regardless of band count.
_BATCH_SIZE = 2_000


def cache_dir_get() -> str:
    """Return the cache directory, respecting the RESEMBL_CACHE_DIR env var."""
    return os.path.expanduser(os.environ.get("RESEMBL_CACHE_DIR", DEFAULT_CACHE_DIR))


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
    """
    try:
        lsh = ResemblLSH(session, threshold, num_perm)
    except ValueError as e:
        logger.error(
            "Error: Invalid LSH parameters. The threshold (%s) may be too high for the number of permutations (%s).",
            threshold,
            num_perm,
        )
        logger.error("  -> Original error: %s", e)
        return None

    lsh_index_clear(session)

    is_sqlite = session.get_bind().dialect.name == "sqlite"
    if is_sqlite:
        session.execute(text("DROP INDEX IF EXISTS ix_lsh_bucket_checksum"))
        # Random bucket inserts into a large b-tree thrash the default 8 MiB
        # page cache; a few hundred MiB keeps the active index pages resident
        # during the build.  Soft upper bound — SQLite does not preallocate.
        session.execute(text("PRAGMA cache_size=-262144"))
        session.commit()

    insert_sql = text(_insert_sql(session))
    # Rows are buffered per band and inserted band-major, sorted by bucket, so
    # the (band, bucket, checksum) primary key grows by sequential append
    # instead of random probe — random inserts into a deep b-tree decay badly
    # (measured ~4x slower by the tail of a 12.5M-row build).
    band_rows: list[list[tuple[bytes, str]]] = [[] for _ in range(lsh.b)]

    def flush_band(band: int) -> None:
        pairs = sorted(band_rows[band])
        session.execute(
            insert_sql,
            [
                {"band": band, "bucket": bucket, "checksum": checksum}
                for bucket, checksum in pairs
            ],
        )
        session.commit()
        band_rows[band].clear()

    total_snippets = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]
    processed = 0
    for batch in Snippet.iter_minhash_batches(session, _BATCH_SIZE):
        for checksum, minhash in batch:
            packed = minhash_ensure_packed(minhash)
            for band, bucket in enumerate(band_buckets(packed, num_perm, lsh.b, lsh.r)):
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

    if is_sqlite:
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_lsh_bucket_checksum "
                "ON lsh_bucket (checksum)"
            )
        )
        session.commit()

    lsh_meta_set(session, threshold, num_perm)
    return lsh


def lsh_index_insert(lsh: ResemblLSH, snippet: Snippet) -> None:
    """Insert a single snippet into an existing LSH index.

    Works with both the DB-backed :class:`ResemblLSH` and legacy datasketch
    ``MinHashLSH`` objects.  Skips insertion if the key already exists
    (idempotent).
    """
    try:
        if isinstance(lsh, ResemblLSH):
            lsh.insert(snippet.checksum, minhash_ensure_packed(snippet.minhash))
        else:
            lsh.insert(snippet.checksum, snippet.get_minhash_obj())
    except ValueError:
        # Key already exists in the LSH — safe to ignore.
        pass


def lsh_index_insert_batch(lsh: ResemblLSH, snippets: list[Snippet]) -> int:
    """Insert multiple snippets into an existing LSH index.

    Returns the number of newly inserted entries.
    """
    if isinstance(lsh, ResemblLSH):
        return lsh.insert_batch(
            [(s.checksum, minhash_ensure_packed(s.minhash)) for s in snippets]
        )
    inserted = 0
    for snippet in snippets:
        try:
            lsh.insert(snippet.checksum, snippet.get_minhash_obj())
            inserted += 1
        except ValueError:
            pass
    return inserted


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
    """Delete the legacy pickle cache file for *threshold* (if any)."""
    cache_dir = cache_dir_get()
    if not os.path.exists(cache_dir):
        return
    for name in (lsh_cache_path_get(threshold), db_checksum_path_get()):
        if os.path.isfile(name):
            os.remove(name)


def lsh_cache_save(
    session: Session,
    lsh: ResemblLSH,
    threshold: float,
    num_perm: int = DEFAULT_NUM_PERMUTATIONS,
) -> None:
    """Persist the LSH index state.

    For the DB-backed index this only records the metadata row (the bucket
    rows are already in the database) and removes any legacy pickle cache.
    Other objects (legacy datasketch indexes, test doubles) are still saved
    as zlib-compressed pickles, as before.
    """
    cache_dir = cache_dir_get()
    os.makedirs(cache_dir, exist_ok=True)

    if isinstance(lsh, ResemblLSH):
        lsh_meta_set(session, threshold, num_perm)
        _remove_pickle_cache(threshold)
        return

    lsh_cache_path = lsh_cache_path_get(threshold)
    payload = CACHE_MAGIC + zlib.compress(
        pickle.dumps(lsh, protocol=pickle.HIGHEST_PROTOCOL)
    )
    with open(lsh_cache_path, "wb") as f:
        f.write(payload)

    with open(db_checksum_path_get(), "w", encoding="utf-8") as f:
        f.write(db_checksum_get(session))


def lsh_cache_load(
    session: Session,
    threshold: float,
    num_perm: int = DEFAULT_NUM_PERMUTATIONS,
) -> ResemblLSH | None:
    """Load the LSH index if it is still valid.

    The database-backed index is used when its metadata matches the requested
    parameters.  Otherwise the legacy pickle cache is consulted (validated
    against the current database checksum), and a rebuilt index takes over on
    the next save.
    """
    meta = lsh_meta_get(session)
    if meta is not None and abs(meta[0] - threshold) < 1e-9 and meta[1] == num_perm:
        return ResemblLSH(session, threshold, num_perm)

    lsh_cache_path = lsh_cache_path_get(threshold)
    checksum_path = db_checksum_path_get()
    if not os.path.exists(lsh_cache_path) or not os.path.exists(checksum_path):
        return None

    with open(checksum_path, "r", encoding="utf-8") as f:
        cached_checksum = f.read()

    current_checksum = db_checksum_get(session)

    if cached_checksum != current_checksum:
        return None  # Cache is stale

    with open(lsh_cache_path, "rb") as f:
        data = f.read()

    if data.startswith(CACHE_MAGIC):
        return pickle.loads(zlib.decompress(data[len(CACHE_MAGIC) :]))
    # Legacy plain pickle (pre-compression format).
    return pickle.loads(data)


def lsh_cache_invalidate() -> None:
    """Delete all cached LSH files.

    Only touches the legacy pickle cache files — the database-backed index is
    kept in sync incrementally (see ``lsh_index_add`` / ``lsh_index_remove``).
    """
    cache_dir = cache_dir_get()
    if os.path.exists(cache_dir):
        for f in os.listdir(cache_dir):
            path = os.path.join(cache_dir, f)
            if os.path.isfile(path):
                os.remove(path)
