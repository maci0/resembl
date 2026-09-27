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
import secrets

from sqlmodel import Session, func, select

from .models import Snippet
from .scoring import (
    code_tokenize,
    minhash_ensure_packed,
    minhash_num_perm,
    require_same_num_perm,
)

logger = logging.getLogger(__name__)

#: Environment variable holding the seed for every sampled value in a run
#: (see :func:`_sample_key`).  Unset, the seed is drawn from the OS, which
#: keeps production sampling as varied as it was; set, the whole run draws
#: the same samples, which is what makes a recorded result replayable.
SEED_ENV_VAR = "RESEMBL_SEED"


def _sample_key() -> str:
    """Return the 32-byte hex key a row sample starts from.

    Drawn from :data:`SEED_ENV_VAR` when it is set, so a run that reports a
    sample-derived number reports the same one on every replay; unset, the
    seed comes from the OS, which is where it came from before this seam
    existed.  The value is a sampling offset over the checksum key space, not
    a secret, so a seeded draw carries no security weight.
    """
    seed = os.environ.get(SEED_ENV_VAR)
    gen = random.Random(int(seed, 0) if seed is not None else secrets.randbits(64))
    return gen.randbytes(32).hex()


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
    """Estimate average Jaccard similarity from a random sample."""
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
    blobs: list[bytes] = []
    for s in sample_snippets:
        try:
            blobs.append(minhash_ensure_packed(s.minhash))
        except ValueError:
            logger.warning(
                "Skipping snippet %s in the similarity sample: corrupt fingerprint.",
                s.checksum,
            )

    num_snippets = len(blobs)
    if num_snippets < 2:
        return 1.0

    # The sample is compared all-pairs (i < j).  At the default sample of 100
    # that is 4,950 pairs; the per-pair ``minhash_jaccard`` path pays two
    # ``struct.unpack`` calls plus a Python-level 128-element loop each, so
    # the whole estimate ran ~633k interpreted iterations.  One packed uint32
    # array is built once and every pair's equality count is then a C-level
    # numpy pass; per-pair values are identical to ``minhash_jaccard``
    # (boolean mean == equal-count / num_perm in float64).
    num_perm = minhash_num_perm(blobs[0])
    for blob in blobs[1:]:
        require_same_num_perm(num_perm, minhash_num_perm(blob))

    import numpy as np

    values = np.frombuffer(b"".join([blob[8:] for blob in blobs]), dtype=">u4").reshape(
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
