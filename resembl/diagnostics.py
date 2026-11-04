"""Read-only statistics over a resembl store.

Everything here answers a question about the database as it stands
(``resembl stats``, the similarity estimate behind it) and none of it writes:
the sampled-row helpers, the seeded draw behind them, and the two ``db_verb``
reports built on those.  They live apart from :mod:`resembl.core` so a
question about the store does not sit in the middle of the snippet and
collection write paths, which is where the calls to them come from.
"""

from __future__ import annotations

import logging
import os
import random
import threading

from sqlmodel import Session, func, select

from .models import Snippet
from .scoring import (
    code_tokenize,
    minhash_ensure_packed,
    minhash_num_perm,
)

logger = logging.getLogger(__name__)

#: Environment variable holding the seed for every sampled value in a run
#: (see :func:`sample_rng`).  Unset, one seed is drawn from the OS for the
#: whole process and reported, so a run can still be replayed by setting
#: this variable; set, the whole run draws the same samples, which is what
#: makes a recorded result replayable.
SEED_ENV_VAR = "RESEMBL_SEED"

#: The one generator every sample in a run draws from, and the seed it was
#: built from.  One generator per process, not one per draw: a per-draw
#: generator replayed the first value over and over, so every sample in a
#: seeded run covered the same rows (correlated estimates, and a vocabulary
#: count that ignored all but one window of the checksum key space), and an
#: unseeded run drew a fresh OS seed per draw, so a number it reported could
#: not be reproduced at all.
_SAMPLE_RNG: random.Random | None = None
_SAMPLE_SEED: int | None = None

#: Serializes construction of, replacement of, and draws from
#: :data:`_SAMPLE_RNG`.  The lazy singleton was built, tested and published in
#: one unsynchronized check-then-act, so two threads sampling at once could
#: each build a generator and whichever published last silently discarded the
#: other's draws (or kept drawing from a generator whose seed the other thread
#: had already reported as the run's replay seed).  ``random.Random`` is not a
#: thread-safe generator either — ``randbytes`` feeds ``getrandbits``, a
#: multi-step Mersenne-Twister state update that two threads can interleave
#: inside — so the draw itself is taken under the same lock.  Reentrant, so
#: :func:`_sample_key` can hold it across ``sample_rng()``.  No I/O runs
#: under it.
_SAMPLE_LOCK = threading.RLock()


def sample_rng() -> random.Random:
    """Return the process-wide generator every sampled value is drawn from.

    Seeded from :data:`SEED_ENV_VAR` when it is set, so two runs with the
    same seed draw the same sequence of samples.  Unset, one seed is drawn
    from the OS, logged at INFO with the value to replay with, and reused
    for the rest of the process.  The draws are sampling offsets over the
    checksum key space, not secrets, so a logged seed carries no security
    weight.  Reading the variable per call (rather than caching the seed at
    import) keeps the seam usable by a test or an embedding process that
    changes it between calls; a changed value restarts the stream.
    """
    global _SAMPLE_RNG, _SAMPLE_SEED  # pylint: disable=global-statement

    env_seed = os.environ.get(SEED_ENV_VAR)
    if env_seed is not None and not env_seed.strip():
        env_seed = None
    with _SAMPLE_LOCK:
        if env_seed is None:
            if _SAMPLE_RNG is None:
                import secrets

                _SAMPLE_SEED = secrets.randbits(64)
                _SAMPLE_RNG = random.Random(_SAMPLE_SEED)
                logger.info(
                    "Sampling from an OS-drawn seed; replay this run with %s=%d",
                    SEED_ENV_VAR,
                    _SAMPLE_SEED,
                )
            return _SAMPLE_RNG

        seed = int(env_seed, 0)  # an unparseable value aborts the command
        if _SAMPLE_RNG is None or _SAMPLE_SEED != seed:
            _SAMPLE_SEED = seed
            _SAMPLE_RNG = random.Random(seed)
        return _SAMPLE_RNG


def sample_randbytes(n: int) -> bytes:
    """Return *n* bytes from the process-wide generator, drawn atomically.

    ``random.Random`` is not thread-safe: ``randbytes`` feeds ``getrandbits``,
    a multi-step Mersenne-Twister state update that two threads can interleave
    inside, so the draw itself is taken under :data:`_SAMPLE_LOCK`.  This is
    the one entry point every draw — sampling keys and request ids alike —
    goes through.
    """
    with _SAMPLE_LOCK:
        return sample_rng().randbytes(n)


def _sample_key() -> str:
    """Return the 32-byte hex key a row sample starts from.

    The next draw from :func:`sample_rng`, so one seed reproduces the whole
    run's sequence of samples.  Held under :data:`_SAMPLE_LOCK` so the draw
    is atomic: two threads sampling concurrently would otherwise interleave
    inside the generator's state update, and a seeded run would stop being
    reproducible.
    """
    return sample_randbytes(32).hex()


def _random_snippet_rows(session: Session, limit: int) -> list[Snippet]:
    """Return up to *limit* uniformly random snippet rows via the checksum PK.

    ``ORDER BY random() LIMIT n`` evaluates the random function for every
    row and keeps the top-N — linear in the table size (measured ~21 ms at
    200k rows, ~100 s at a billion).  Checksums are content hashes, uniform
    over the 64-hex key space, so a contiguous run starting at a random key
    is a uniform sample, and the PK index makes it O(limit) regardless of
    table size (~0.6 ms measured).  Keys near the end of the key space wrap
    around via a second indexed query.  The starting key comes from
    :func:`_sample_key`, so the sample is reproducible from a seed.
    """
    key = _sample_key()
    rows = list(
        session.exec(
            select(Snippet).where(Snippet.checksum >= key).order_by(Snippet.checksum).limit(limit)
        ).all()
    )
    if len(rows) < limit:
        rows += list(
            session.exec(
                select(Snippet)
                .where(Snippet.checksum < key)
                .order_by(Snippet.checksum)
                .limit(limit - len(rows))
            ).all()
        )
    return rows


def db_calculate_average_similarity(session: Session, sample_size: int = 100) -> float:
    """Estimate average Jaccard similarity from a random sample.

    Raises ``ValueError`` for a *sample_size* below 1: a zero size samples
    nothing and would report perfect similarity (1.0) for an empty sample,
    and a negative size is passed to SQL ``LIMIT`` as-is, where SQLite
    treats ``LIMIT -1`` as "no limit" and loads the whole table.
    """
    if sample_size < 1:
        raise ValueError(f"sample_size must be at least 1, got {sample_size!r}")
    count = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]
    if count < 2:
        return 1.0

    if count > sample_size:
        # Random sample directly in SQL — no need to load the whole table.
        sample_snippets = _random_snippet_rows(session, sample_size)
    else:
        sample_snippets = list(Snippet.get_all(session))

    # Normalize and validate each sampled fingerprint, skipping corrupt ones
    # (disk rot) — one bad blob must not crash `stats` on a large database.
    # Stale-perm blobs are skipped the same way, matching the index build
    # and find paths: a mixed-perm database (config flip between writes)
    # must not crash the similarity estimate too.
    blobs: list[tuple[str, bytes]] = []
    for s in sample_snippets:
        try:
            blobs.append((s.checksum, minhash_ensure_packed(s.minhash)))
        except ValueError:
            logger.warning(
                "Skipping snippet %s in the similarity sample: corrupt fingerprint.",
                s.checksum,
            )

    if not blobs:
        return 1.0

    # Keep only blobs matching the first blob's permutation count; a
    # well-formed blob at a different count is stale for this sample.
    num_perm = minhash_num_perm(blobs[0][1])
    packed_blobs: list[bytes] = []
    for checksum, blob in blobs:
        if minhash_num_perm(blob) != num_perm:
            logger.warning(
                "Skipping snippet %s in the similarity sample: "
                "fingerprint permutation count does not match %d.",
                checksum,
                num_perm,
            )
            continue
        packed_blobs.append(blob)

    num_snippets = len(packed_blobs)
    if num_snippets < 2:
        return 1.0

    import numpy as np

    values = np.frombuffer(b"".join([blob[8:] for blob in packed_blobs]), dtype=">u4").reshape(
        num_snippets, num_perm
    )
    total_similarity = 0.0
    for i in range(num_snippets - 1):
        # Per-pair value equals ``minhash_jaccard``: equal-count / num_perm,
        # each division rounded to float64 exactly as Python's int/int `/`.
        row_jaccards = (values[i + 1 :] == values[i]).sum(axis=1) / num_perm
        total_similarity += float(row_jaccards.sum())

    return total_similarity / (num_snippets * (num_snippets - 1) // 2)


def db_stats(session: Session) -> dict:
    """Return a dictionary of database statistics."""
    num_snippets = session.exec(
        select(func.count(Snippet.checksum))  # type: ignore[arg-type]
    ).one()
    if num_snippets == 0:
        return {
            "num_snippets": 0,
            "avg_snippet_size": 0,
            "vocabulary_size": 0,
            "avg_jaccard_similarity": 0.0,
        }

    # Aggregate the average snippet size in SQL instead of loading every row.
    avg_size = session.exec(select(func.avg(func.length(Snippet.code)))).one()
    avg_snippet_size = float(avg_size or 0.0)

    # Vocabulary: tokenize a bounded random sample so the command stays
    # constant-time at scale (tokenizing every code took ~1 min at 500k).
    # For small databases the sample is the whole corpus (exact).
    sample_codes = [s.code for s in _random_snippet_rows(session, 2000)]
    all_tokens: set[str] = set()
    for code in sample_codes:
        all_tokens.update(code_tokenize(code))

    return {
        "num_snippets": num_snippets,
        "avg_snippet_size": avg_snippet_size,
        # Estimated from up to 2000 sampled snippets on large databases.
        "vocabulary_size": len(all_tokens),
        "avg_jaccard_similarity": db_calculate_average_similarity(session),
    }
