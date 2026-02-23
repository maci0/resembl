"""Utilities for caching and loading the MinHash LSH index."""

import logging
import os
import pickle

from datasketch import MinHashLSH
from sqlmodel import Session

from .database import db_checksum_get
from .models import Snippet

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = "~/.cache/resembl"


def cache_dir_get() -> str:
    """Return the cache directory, respecting the RESEMBL_CACHE_DIR env var."""
    return os.path.expanduser(os.environ.get("RESEMBL_CACHE_DIR", DEFAULT_CACHE_DIR))


def db_checksum_path_get() -> str:
    """Return the path to the DB checksum file."""
    return os.path.join(cache_dir_get(), "db_checksum.txt")


def lsh_cache_path_get(threshold: float) -> str:
    """Return the path to the LSH cache file for a given threshold."""
    return os.path.join(cache_dir_get(), f"lsh_{threshold:.2f}.pkl")


def lsh_index_build(session: Session, threshold: float, num_perm: int) -> MinHashLSH | None:
    """Build the LSH index from snippets in the database."""
    try:
        lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    except ValueError as e:
        logger.error(
            "Error: Invalid LSH parameters. The threshold (%s) may be too high for the number of permutations (%s).",
            threshold,
            num_perm,
        )
        logger.error("  -> Original error: %s", e)
        return None

    snippets = Snippet.get_all(session)
    for snippet in snippets:
        lsh.insert(snippet.checksum, snippet.get_minhash_obj())
    return lsh


def lsh_index_insert(lsh: MinHashLSH, snippet: Snippet) -> None:
    """Insert a single snippet into an existing LSH index.

    Skips insertion if the key already exists (idempotent).
    """
    try:
        lsh.insert(snippet.checksum, snippet.get_minhash_obj())
    except ValueError:
        # Key already exists in the LSH — safe to ignore.
        pass


def lsh_index_insert_batch(lsh: MinHashLSH, snippets: list[Snippet]) -> int:
    """Insert multiple snippets into an existing LSH index.

    Returns the number of newly inserted entries.
    """
    inserted = 0
    for snippet in snippets:
        try:
            lsh.insert(snippet.checksum, snippet.get_minhash_obj())
            inserted += 1
        except ValueError:
            pass
    return inserted


def lsh_cache_save(session: Session, lsh: MinHashLSH, threshold: float) -> None:
    """Save the LSH index and the current DB checksum to the cache."""
    cache_dir = cache_dir_get()
    os.makedirs(cache_dir, exist_ok=True)

    lsh_cache_path = lsh_cache_path_get(threshold)
    with open(lsh_cache_path, "wb") as f:
        pickle.dump(lsh, f)

    with open(db_checksum_path_get(), "w", encoding="utf-8") as f:
        f.write(db_checksum_get(session))


def lsh_cache_load(session: Session, threshold: float) -> MinHashLSH | None:
    """Load the LSH index from cache if it is still valid."""
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
        return pickle.load(f)


def lsh_cache_invalidate() -> None:
    """Delete all cached LSH files."""
    cache_dir = cache_dir_get()
    if os.path.exists(cache_dir):
        for f in os.listdir(cache_dir):
            path = os.path.join(cache_dir, f)
            if os.path.isfile(path):
                os.remove(path)
