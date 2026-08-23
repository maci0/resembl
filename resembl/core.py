"""Core functions for tokenizing and comparing assembly snippets.

This module provides:
- Assembly code tokenization and normalization (multi-arch)
- MinHash / LSH-based similarity matching
- Snippet CRUD with checksum-based deduplication
- Collection grouping, tagging, and versioning
- Database merge with independent name/tag reconciliation
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, func, select, text

if TYPE_CHECKING:
    from .minhash import MinHash
from .cache import (
    lsh_cache_load,
    lsh_cache_save,
    lsh_index_add,
    lsh_index_add_batch,
    lsh_index_build,
    lsh_index_clear,
    lsh_index_remove,
)
from .lsh import (
    banding_params,
    fingerprint_ngram_clear,
    fingerprint_ngram_get,
    fingerprint_ngram_set,
    fingerprint_perm_clear,
    fingerprint_perm_get,
    fingerprint_perm_set,
    fingerprint_stamps_reconcile,
    fingerprint_version_clear,
    fingerprint_version_get,
    fingerprint_version_set,
    lsh_meta_get,
)
from .models import (
    FINGERPRINT_VERSION,
    Collection,
    LSHBucket,
    Snippet,
    SnippetVersion,
    timestamp_normalize,
)
from .scoring import (
    NUM_PERMUTATIONS,
    _code_tokenize_lexed,
    _minhash_from_tokens,
    _require_same_num_perm,
    _string_normalize_lexed,
    cfg_extract,
    cfg_similarity,
    code_create_minhash,
    code_create_minhash_batch,
    code_tokenize,
    get_lexer,
    minhash_ensure_packed,
    minhash_jaccard_batch,
    minhash_num_perm,
    minhash_pack,
    score_hybrid,
    string_checksum,
)

# Re-exported for external backward compatibility only; not used in this
# module (`from resembl.core import string_normalize` keeps working).
# isort: split
from .scoring import (  # noqa: F401
    BRANCH_INSTRUCTIONS,
    COMMON_INSTRUCTIONS,
    RARE_INSTRUCTIONS,
    minhash_jaccard,
    shingle_weight,
    string_normalize,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default LSH similarity threshold for candidate filtering.
LSH_THRESHOLD = 0.5

#: Bounded retries for the index clear inside ``db_reindex`` (see there):
#: concurrent cold finds of the same database contend on SQLite's exclusive
#: schema lock, and the loser should wait rather than crash.
_REINDEX_CLEAR_RETRIES = 3
#: Linear backoff between clear retries, in seconds.
_REINDEX_CLEAR_RETRY_BACKOFF = 3


class IndexBuildError(RuntimeError):
    """Raised when the LSH index cannot be built or loaded.

    Distinguishes "no matches" from "search impossible": returning zero
    matches here would make a transient database-lock failure during the
    build look identical to a legitimate empty result.
    """


def adaptive_worker_count(num_items: int, cpu_count: int) -> int:
    """Choose a sensible worker count for a parallel job of *num_items* items.

    One worker per CPU is wasteful for small jobs: spawning each ``spawn``
    worker costs the full interpreter + library import (~450 ms and ~50 MB),
    so a 300-item job measured 1.85 s with 32 workers vs 0.84 s with 4.
    The default scales with the work (one worker per ~100 items) and is
    capped at the CPU count, so small jobs stay single-process and large
    ones parallelize fully.
    """
    return max(1, min(cpu_count, num_items // 100 + 1))


def snippet_name_add(
    session: Session, checksum: str, new_name: str, quiet: bool = False
) -> Snippet | None:
    """Add a new name to an existing snippet."""
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    name_list = snippet.name_list
    if new_name in name_list:
        if not quiet:
            logger.error("Name '%s' already exists for this snippet.", new_name)
        return None

    name_list.append(new_name)
    snippet.names = json.dumps(name_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def snippet_name_remove(
    session: Session, checksum: str, name_to_remove: str, quiet: bool = False
) -> Snippet | None:
    """Remove a name from a snippet."""
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    name_list = snippet.name_list
    if name_to_remove not in name_list:
        if not quiet:
            logger.error("Name '%s' not found for this snippet.", name_to_remove)
        return None

    if len(name_list) == 1:
        if not quiet:
            logger.error("Cannot remove the last name from a snippet.")
        return None

    name_list.remove(name_to_remove)
    snippet.names = json.dumps(name_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def snippet_tag_add(
    session: Session, checksum: str, tag: str, quiet: bool = False
) -> Snippet | None:
    """Add a tag to a snippet (idempotent — adding an existing tag is a no-op)."""
    tag = tag.strip()
    if not tag:
        if not quiet:
            logger.error("Tag cannot be empty.")
        return None

    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    tag_list = snippet.tag_list
    if tag in tag_list:
        return snippet  # Idempotent: already tagged

    tag_list.append(tag)
    snippet.tags = json.dumps(tag_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def snippet_tag_remove(
    session: Session, checksum: str, tag: str, quiet: bool = False
) -> Snippet | None:
    """Remove a tag from a snippet (idempotent — removing a missing tag is a no-op)."""
    tag = tag.strip()
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    tag_list = snippet.tag_list
    if tag not in tag_list:
        return snippet  # Idempotent: tag not present

    tag_list.remove(tag)
    snippet.tags = json.dumps(tag_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


# ---------------------------------------------------------------------------
# Snippet CRUD
# ---------------------------------------------------------------------------


def snippet_prepare(
    name: str, code: str, ngram_size: int = 3
) -> tuple[str, str, str, bytes] | None:
    """Compute the checksum and MinHash fingerprint for a snippet.

    Returns ``(checksum, name, code, minhash_bytes)`` or ``None`` for empty
    code.  This is a pure function with no database access, so it is safe to
    run in worker processes when bulk-importing many files.

    The snippet is lexed exactly once: the normalized string (for the
    checksum) and the token list (for the MinHash) are both derived from the
    same token stream.  Lexing with Pygments is the dominant per-snippet
    cost, so this halves it on the import hot path.
    """
    if not code.strip():
        return None
    # Materialize the token stream: it is consumed twice (once for the
    # normalized checksum string, once for the MinHash tokens).
    tokens = list(get_lexer().get_tokens(code))
    normalized = _string_normalize_lexed(tokens)
    checksum = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()
    minhash_bytes = minhash_pack(_minhash_from_tokens(_code_tokenize_lexed(tokens), ngram_size))
    return checksum, name, code, minhash_bytes


def _checksum_chunks(checksums: list[str]) -> list[list[str]]:
    """Split checksums into chunks small enough for one SQL ``IN`` clause.

    900 stays comfortably under SQLite's variable limit (999 by default,
    32766 on modern builds), halving the round trips of the old 500.
    """
    return [checksums[i : i + 900] for i in range(0, len(checksums), 900)]


def _snippets_by_checksums(session: Session, checksums: list[str]) -> dict[str, Snippet]:
    """Fetch snippets by checksum using chunked ``IN`` queries (no N+1)."""
    result: dict[str, Snippet] = {}
    for chunk in _checksum_chunks(list(checksums)):
        for snippet in session.exec(
            select(Snippet).where(Snippet.checksum.in_(chunk))  # type: ignore[attr-defined]
        ).all():
            result[snippet.checksum] = snippet
    return result


def _snippet_minhashes_by_checksums(session: Session, checksums: list[str]) -> dict[str, bytes]:
    """Fetch only ``(checksum, minhash)`` pairs for many checksums.

    The ``code`` column dominates the table, so reading it for every LSH
    candidate would pull megabytes of text through the ORM per query even
    though most candidates are pruned before they are ever Levenshtein-
    scored.  The find hot path reads just the fingerprints here, vectorizes
    the Jaccard pass, and only then fetches full rows for the survivors.
    """
    result: dict[str, bytes] = {}
    for chunk in _checksum_chunks(list(checksums)):
        for row in session.exec(
            select(Snippet.checksum, Snippet.minhash).where(
                Snippet.checksum.in_(chunk)  # type: ignore[attr-defined]
            )
        ).all():
            result[row[0]] = row[1]
    return result


#: Parameterized template for one snippet row (the executemany path).
_SNIPPET_INSERT_SQL = (
    "INSERT INTO snippet (checksum, names, code, minhash, tags, collection) "
    "VALUES (:checksum, :names, :code, :minhash, :tags, :collection)"
)


def _duckdb_sql_literal(value: object) -> str:
    """Render one snippet-column value as a safe DuckDB SQL literal.

    Text is single-quoted with quote doubling — standard SQL escaping, and
    complete for DuckDB because it treats backslash literally inside string
    literals (no ``\\`` escape sequences).  Bytes use ``FROM_HEX``,
    DuckDB's blob-from-hex function (the ``X'...'`` hex literal is not
    supported).  ``None`` becomes ``NULL``.  This is the correctness and
    injection boundary of the DuckDB multi-VALUES fast path: snippet code
    and names are arbitrary user text, so every value must pass through
    here before being interpolated into SQL.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"FROM_HEX('{value.hex()}')"
    return "'" + str(value).replace("'", "''") + "'"


def _insert_snippet_rows(
    session: Session, rows: list[dict[str, object]], batch_size: int = 500
) -> None:
    """Insert snippet rows with the dialect's fastest strategy.

    DuckDB's executemany path is ~7x slower than multi-row ``VALUES``
    statements, and the snippet insert dominates import throughput there
    (measured 2,665 vs 19,872 rows/s at 500 rows/statement).  Values are
    rendered through :func:`_duckdb_sql_literal`, which is the correctness
    and injection boundary for the fast path.  Other dialects keep the
    parameterized executemany, which is already C-accelerated there.
    """
    if not rows:
        return
    if session.get_bind().dialect.name != "duckdb":
        for i in range(0, len(rows), batch_size):
            session.execute(text(_SNIPPET_INSERT_SQL), params=rows[i : i + batch_size])
        return
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        values = ",".join(
            "("
            + ", ".join(
                _duckdb_sql_literal(v)
                for v in (
                    row["checksum"],
                    row["names"],
                    row["code"],
                    row["minhash"],
                    row["tags"],
                    row["collection"],
                )
            )
            + ")"
            for row in chunk
        )
        # exec_driver_sql, not text(): the generated statement has no bind
        # parameters, and text()'s marker scan cannot tell a literal ``$1``
        # or ``:0`` inside user content from a real bind placeholder — a
        # snippet containing either would otherwise raise StatementError.
        session.connection().exec_driver_sql(
            "INSERT INTO snippet (checksum, names, code, minhash, tags, "
            f"collection) VALUES {values}"
        )


def snippet_add_batch(
    session: Session,
    prepared_items: list[tuple[str, str, str, bytes]],
    batch_size: int = 500,
    ngram_size: int = 3,
) -> dict:
    """Insert many prepared snippets in one pass.

    ``prepared_items`` is a list of ``(checksum, name, code, minhash_bytes)``
    tuples as produced by :func:`snippet_prepare`.

    Deduplication is content-addressable: code that already exists in the
    database is not re-inserted; any new names are merged into the existing
    snippet as aliases.  Rows are written in batches with a single LSH cache
    invalidation at the end, making bulk imports orders of magnitude faster
    than one ``snippet_add`` call per file.

    Returns ``{"added", "aliased", "skipped", "time_elapsed"}``.
    """
    start_time = time.monotonic()

    # Group by checksum: within one batch, identical code is deduplicated and
    # its names are merged.  ``(code, minhash_bytes, names)`` tuples keep the
    # entry strongly typed for the hot loop below.
    by_checksum: dict[str, tuple[str, bytes, list[str]]] = {}
    for checksum, name, code, minhash_bytes in prepared_items:
        entry = by_checksum.get(checksum)
        if entry is None:
            entry = (code, minhash_bytes, [])
            by_checksum[checksum] = entry
        if name and name not in entry[2]:
            entry[2].append(name)

    if not by_checksum:
        return {
            "added": 0,
            "aliased": 0,
            "skipped": len(prepared_items),
            "time_elapsed": 0.0,
        }

    # Batch-fetch the full rows for every candidate checksum in one pass of
    # chunked IN queries.  Checksums absent from the map are new.  This
    # replaces the old two-step flow (a checksum-only EXISTS select, then a
    # ``session.get`` per existing row) which issued one round trip per
    # existing snippet — an N+1 that dominated incremental re-imports of
    # mostly-known content at scale.
    existing_map = _snippets_by_checksums(session, list(by_checksum))

    aliased = 0
    new_snippets: list[Snippet] = []
    for checksum, (code, minhash_bytes, names) in by_checksum.items():
        snippet = existing_map.get(checksum)
        if snippet is not None:
            name_list = snippet.name_list
            merged = list(dict.fromkeys(name_list + names))
            if len(merged) > len(name_list):
                snippet.names = json.dumps(merged)
                session.add(snippet)
                aliased += 1
            continue
        new_snippets.append(
            Snippet(
                checksum=checksum,
                names=json.dumps(names),
                code=code,
                minhash=minhash_bytes,
            )
        )

    # Single transaction: per-group commits would repeatedly trigger WAL
    # checkpoints that rewrite the whole database file (quadratic at scale).
    # New rows are bulk-inserted with a raw ``executemany`` — measured ~30x
    # faster than the ORM's per-object ``add_all``, which was the import
    # write-path bottleneck — while alias name merges flush through the ORM.
    # DuckDB swaps in multi-row VALUES statements (its executemany is ~7x
    # slower; see ``_insert_snippet_rows``).  One commit persists everything.
    # Count rows before inserting: the stamp reconciliation below may only
    # publish values that hold for the whole database, so it must know
    # whether pre-existing (possibly foreign-format) rows exist alongside
    # this batch's new ones.
    preexisting_rows = session.exec(
        select(func.count(Snippet.checksum))  # type: ignore[arg-type]
    ).one()
    if new_snippets:
        rows: list[dict[str, object]] = [
            {
                "checksum": s.checksum,
                "names": s.names,
                "code": s.code,
                "minhash": s.minhash,
                "tags": s.tags,
                "collection": s.collection,
            }
            for s in new_snippets
        ]
        _insert_snippet_rows(session, rows, batch_size)
    if new_snippets or aliased:
        session.commit()
        if new_snippets:
            fingerprint_stamps_reconcile(
                session,
                ngram_size=ngram_size,
                num_perm=NUM_PERMUTATIONS,
                fresh_database=preexisting_rows == 0,
            )

    # Keep the DB-backed LSH index in sync if one is already built.
    lsh_index_add_batch(session, [(s.checksum, s.minhash) for s in new_snippets])

    elapsed = time.monotonic() - start_time
    return {
        "added": len(new_snippets),
        "aliased": aliased,
        "skipped": len(prepared_items) - len(by_checksum),
        "time_elapsed": elapsed,
    }


def snippet_add(session: Session, name: str, code: str, ngram_size: int = 3) -> Snippet | None:
    """Add a new snippet or alias to the database."""
    if not code.strip():
        return None
    checksum = string_checksum(code)

    existing_snippet = Snippet.get_by_checksum(session, checksum)

    if existing_snippet:
        # Code exists, add new name as an alias
        name_list = existing_snippet.name_list
        if name and name not in name_list:
            name_list.append(name)
            existing_snippet.names = json.dumps(name_list)
            session.add(existing_snippet)
            session.commit()
            session.refresh(existing_snippet)
        return existing_snippet

    # Snippet with this code does not exist, create a new one
    minhash_obj = code_create_minhash(code, ngram_size=ngram_size)
    minhash_bytes = minhash_pack(minhash_obj)

    # Count rows before inserting: the stamp reconciliation below may only
    # publish stamp values that hold for the whole database, so it must know
    # whether pre-existing rows (possibly foreign-format) are present.
    preexisting_rows = session.exec(
        select(func.count(Snippet.checksum))  # type: ignore[arg-type]
    ).one()

    new_snippet = Snippet(
        checksum=checksum,
        names=json.dumps([name]),
        code=code,
        minhash=minhash_bytes,
    )
    session.add(new_snippet)
    session.commit()
    session.refresh(new_snippet)
    fingerprint_stamps_reconcile(
        session,
        ngram_size=ngram_size,
        num_perm=NUM_PERMUTATIONS,
        fresh_database=preexisting_rows == 0,
    )
    # Keep the DB-backed LSH index in sync if one is already built.
    lsh_index_add(session, new_snippet.checksum, new_snippet.minhash)
    return new_snippet


def fingerprints_need_reindex(session: Session, ngram_size: int, num_permutations: int) -> bool:
    """Return True when stored fingerprints predate the requested parameters.

    Stored blobs carry a format-version stamp, an n-gram stamp, and a
    permutation-count stamp; a mismatch against any of them would silently
    mix fingerprint formats in query results, so the database must be
    reindexed once first.  When the stamps are missing (legacy databases)
    one stored blob is probed: requesting the *default* count does not prove
    the blobs match it (a database written while ``num_permutations`` was
    configured differently would otherwise crash the next default-count find
    instead of healing itself).  Reindexing current-format blobs is
    idempotent (identical fingerprints).  Shared by ``find`` and ``serve``
    startup.
    """
    if fingerprint_version_get(session) != FINGERPRINT_VERSION:
        return True
    if fingerprint_ngram_get(session) != ngram_size:
        # Stored fingerprints encode their n-gram; a config ngram change
        # silently zeroes matches (measured: 40 candidates at ngram 3, 0 at
        # ngram 5) — reindex once at the new n-gram.
        return True
    stamped_perm = fingerprint_perm_get(session)
    if stamped_perm is not None and stamped_perm != num_permutations:
        # Every fingerprint writer stamps its count, so this is exact — no
        # reliance on which single blob the legacy probe below samples.
        return True
    # Probe one blob: a non-default stored count, or a corrupt blob, means
    # the database must be reindexed before queries can mix formats safely.
    blob = session.exec(select(Snippet.minhash).limit(1)).first()
    if blob is None:
        return False
    try:
        return minhash_num_perm(blob) != num_permutations
    except ValueError:
        return True  # corrupt blob — a reindex heals it


def snippet_find_matches(
    session: Session,
    query_string: str,
    top_n: int = 3,
    threshold: float | None = None,
    normalize: bool = True,
    ngram_size: int = 3,
    num_permutations: int = NUM_PERMUTATIONS,
    jaccard_weight: float = 0.4,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, list[tuple[Snippet, float]]]:
    """Find and rank matches for a query string.

    ``progress`` is forwarded to the lazy index build and to a one-time
    automatic reindex (see below) when either is triggered.
    """
    # Deferred like the lexer: Levenshtein scoring is only reached by
    # find/compare paths, so light commands never pay rapidfuzz's import.
    from rapidfuzz import fuzz

    if threshold is None:
        threshold = LSH_THRESHOLD

    # Fingerprint-format migration: if the stored blobs were written by an
    # older algorithm, recompute them once so queries never silently mix
    # fingerprint formats.
    if fingerprints_need_reindex(session, ngram_size, num_permutations):
        num_snippets = session.exec(
            select(func.count(Snippet.checksum))  # type: ignore[arg-type]
        ).one()
        db_reindex(
            session,
            ngram_size=ngram_size,
            jobs=adaptive_worker_count(num_snippets, os.cpu_count() or 1),
            num_perm=num_permutations,
            progress=progress,
        )

    lsh = lsh_cache_load(session, threshold, num_permutations)
    if not lsh:
        lsh = lsh_index_build(session, threshold, num_permutations, progress=progress)
        if lsh:
            lsh_cache_save(session, threshold, num_permutations)

    if lsh is None:
        # The build failed (logged by ``lsh_index_build``): report an error
        # instead of a silent zero-match result.
        raise IndexBuildError(
            "could not build the LSH index (another process may be writing "
            "to this database, or the parameters are invalid); retry once "
            "the database is idle"
        )

    query_minhash = code_create_minhash(
        query_string, normalize, ngram_size=ngram_size, num_perm=num_permutations
    )
    query_minhash_bytes = minhash_pack(query_minhash)
    candidate_keys = lsh.query(query_minhash_bytes)

    if not candidate_keys:
        return 0, []
    if top_n <= 0:
        return len(candidate_keys), []

    # Fetch only the fingerprint columns for every candidate first — the
    # ``code`` column dominates the table, and most candidates are pruned
    # before they are ever Levenshtein-scored, so loading full rows for all
    # of them would move megabytes of text through the ORM per query.
    keys = list(candidate_keys)
    minhashes = _snippet_minhashes_by_checksums(session, keys)

    # Jaccard is computed directly from the packed fingerprints (no MinHash
    # object construction), vectorized across all candidates in one numpy
    # pass over the (N, 128) uint32 array — SIMD under the hood.  This is
    # what keeps find fast when a query lands in a crowded band (thousands
    # of candidates at scale).
    # Normalize each candidate's blob (legacy pickles -> packed) and skip
    # corrupt ones: a single rotten fingerprint must not crash the query —
    # it is excluded from scoring (a reindex heals it from its code).
    # Blobs written at a different permutation count are stale for this
    # query by the same token; scoring them would raise inside the batch
    # Jaccard, so they are skipped here.
    normalized: list[bytes] = []
    valid_keys: list[str] = []
    for k in keys:
        # A stale bucket row may reference a snippet deleted between the
        # index query and this fetch (same race the full-row pass below
        # tolerates); skipping it keeps the query alive until the index
        # catches up.
        blob_or_none = minhashes.get(k)
        if blob_or_none is None:
            continue
        try:
            blob = minhash_ensure_packed(blob_or_none)
            if minhash_num_perm(blob) != num_permutations:
                logger.warning("Skipping candidate %s: stale fingerprint permutation count.", k)
                continue
        except ValueError:
            logger.warning("Skipping candidate %s: corrupt fingerprint.", k)
            continue
        normalized.append(blob)
        valid_keys.append(k)
    keys = valid_keys
    if not keys:
        return 0, []
    jaccards = minhash_jaccard_batch(query_minhash_bytes, normalized)

    # Hybrid score (Jaccard + Levenshtein) with an early exit: since
    # ``hybrid = 40 * jaccard + 0.6 * levenshtein`` and levenshtein <= 100,
    # a candidate whose upper bound ``40 * jaccard + 60`` is strictly below
    # the current n-th best hybrid can never enter the top-n — it skips the
    # ``fuzz.ratio`` call and the full-row fetch entirely.  Candidates are
    # processed in descending jaccard order so the bound only shrinks: once
    # one candidate is pruned, the rest of the list is provably pruned too,
    # and no further rows are fetched at all.  Full rows are loaded in small
    # chunks, and the heap keeps the top-n by (hybrid, insertion index),
    # ties evicting the largest index as a stable sort would, with a final
    # sort that replicates that stable sort, so the returned matches are
    # identical to scoring and fetching everything.
    order = sorted(range(len(keys)), key=lambda i: jaccards[i], reverse=True)
    scored: list[tuple[float, int, Snippet]] = []
    for start in range(0, len(order), 64):
        batch = order[start : start + 64]
        # Best possible hybrid in this batch cannot beat the current top-n.
        if (
            len(scored) >= top_n
            and score_hybrid(jaccards[batch[0]], 100.0, jaccard_weight) < scored[0][0]
        ):
            break
        full_rows = _snippets_by_checksums(session, [keys[i] for i in batch])
        for i in batch:
            snippet = full_rows.get(keys[i])
            if snippet is None:
                continue  # deleted concurrently between the two fetches
            jaccard = jaccards[i]
            # Upper bound on hybrid = the score at the maximum Levenshtein
            # (100), computed through score_hybrid itself so the bound is
            # bit-identical to what a real lev=100 candidate would score;
            # hand-inlined arithmetic can round a few ulps lower and then
            # prune a genuine top-n candidate.  Candidates strictly below
            # the current n-th best are provably out and skip the
            # fuzz.ratio call.
            if len(scored) >= top_n and score_hybrid(jaccard, 100.0, jaccard_weight) < scored[0][0]:
                continue
            levenshtein = fuzz.ratio(query_string, snippet.code)
            hybrid = score_hybrid(jaccard, levenshtein, jaccard_weight)
            # The negated index makes the heap root the *worst* entry under
            # the final ranking (descending hybrid, ascending index): among
            # candidates tied at the lowest score, the largest index is the
            # one a stable sort would drop.  With plain ``+i`` the root
            # would be the smallest tied index and an eviction would remove
            # exactly the candidate that should have won the tie.
            heapq.heappush(scored, (hybrid, -i, snippet))
            if len(scored) > top_n:
                heapq.heappop(scored)
    scored.sort(key=lambda t: (-t[0], -t[1]))
    top_matches = [(snippet, hybrid) for hybrid, _idx, snippet in scored[:top_n]]

    return len(candidate_keys), top_matches


def snippet_matches_payload(num_candidates: int, matches: list[tuple[Snippet, float]]) -> dict:
    """Serialize find results into the shared payload/wire shape.

    ``find``, ``find-batch``, and the ``serve`` endpoints must emit exactly
    this structure (the thin client and CLI renderers read these keys), so
    it is built in one place rather than re-typed per caller.
    """
    return {
        "lsh_candidates": num_candidates,
        "matches": [
            {"checksum": s.checksum, "names": s.name_list, "score": score} for s, score in matches
        ],
    }


def snippet_delete(session: Session, checksum: str, quiet: bool = False) -> bool:
    """Delete a snippet by its checksum."""
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return False

    session.delete(snippet)
    session.commit()
    if not quiet:
        logger.info("Snippet with checksum %s deleted.", checksum)

    # Keep the DB-backed LSH index in sync if one is already built.
    lsh_index_remove(session, checksum)
    return True


def _snippet_primary_name(snippet: Snippet) -> str:
    """Return a snippet's first alias, or a checksum-derived placeholder."""
    return snippet.name_list[0] if snippet.name_list else f"snippet_{snippet.checksum[:16]}"


def _yara_string_escape(text: str) -> str:
    """Escape *text* for embedding in a quoted YARA string literal.

    User-controlled names and code are embedded in generated rules, so a
    name cannot be allowed to break out of the string literal (or the rule).
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return escaped.replace("\r", "\\r").replace("\n", "\\n")


def snippet_export_yara(session: Session, output_file: str) -> dict:
    """Export snippets as YARA string matching rules."""
    import tempfile

    start_time = time.monotonic()
    num_exported = 0

    # Stream into a same-directory temp and publish with one atomic rename
    # (same pattern as ``config.save_config``): a failure mid-export (disk
    # full, permissions) must not leave a truncated, uncompilable rule file
    # at the requested path looking like a complete artifact.  A previously
    # exported file there also stays intact instead of being truncated.
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(os.path.abspath(output_file)), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for snippet in Snippet.stream_all(session):
                primary_name = _snippet_primary_name(snippet)
                rule_name = re.sub(r"[^a-zA-Z0-9_]", "_", primary_name)
                # A name may be empty (e.g. added with an empty alias) — index
                # would crash; prefix instead, as for names starting with a digit.
                if not rule_name or (not rule_name[0].isalpha() and rule_name[0] != "_"):
                    rule_name = "r_" + rule_name
                rule_name = f"resembl_{rule_name}_{snippet.checksum[:8]}"

                name_escaped = _yara_string_escape(primary_name)
                code_escaped = _yara_string_escape(snippet.code)

                yara_rule = f"""rule {rule_name} {{
    meta:
        description = "Resembl exported snippet: {name_escaped}"
        checksum = "{snippet.checksum}"
    strings:
        $asm = "{code_escaped}" nocase ascii wide
    condition:
        $asm
}}

"""
                f.write(yara_rule)
                num_exported += 1
        os.replace(tmp_path, output_file)
    except BaseException:
        # The half-written temp must not outlive a failed export.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    end_time = time.monotonic()
    time_elapsed = end_time - start_time

    return {
        "num_exported": num_exported,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": ((time_elapsed / num_exported) if num_exported > 0 else 0),
    }


def _reindex_prepare(args: tuple[list[str], int, int]) -> list[bytes]:
    """Worker: recompute packed fingerprints for a batch of codes.

    Pure function (no database access) so it can run in a process pool.
    """
    codes, ngram_size, num_perm = args
    return [
        minhash_pack(m)
        for m in code_create_minhash_batch(codes, ngram_size=ngram_size, num_perm=num_perm)
    ]


def db_reindex(
    session: Session,
    ngram_size: int = 3,
    batch_size: int = 500,
    jobs: int = 1,
    num_perm: int = NUM_PERMUTATIONS,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Recalculate the MinHash for every snippet in the database.

    With ``jobs > 1`` the CPU-bound tokenization runs in a process pool
    (bounded in-flight batches), turning a long sequential reindex into a
    parallel one.

    The fingerprints are invalidated *before* the update: any built index is
    cleared up front, so a crash mid-reindex can never leave a stale index
    behind (the next ``find`` simply rebuilds it from whatever fingerprints
    are stored).  On SQLite the writes are committed periodically so the WAL
    stays bounded — a single transaction spanning the whole reindex would
    grow the WAL to the size of the database and force one huge checkpoint
    at commit.  PostgreSQL segments its own WAL and pays an fsync per
    commit, so it keeps a single final commit.  If *progress* is given it is
    called as ``progress(done, total)`` with snippets processed so far.
    """
    import multiprocessing as _mp
    from collections import deque
    from concurrent.futures import Future, ProcessPoolExecutor

    start_time = time.monotonic()
    num_snippets = session.exec(
        select(func.count(Snippet.checksum))  # type: ignore[arg-type]
    ).one()

    if num_snippets == 0:
        fingerprint_version_set(session, FINGERPRINT_VERSION)
        fingerprint_ngram_set(session, ngram_size)
        fingerprint_perm_set(session, num_perm)
        return {"num_reindexed": 0, "time_elapsed": 0, "avg_time_per_snippet": 0}

    reindexed = 0
    parallel = jobs > 1 and num_snippets > batch_size
    is_sqlite = session.get_bind().dialect.name == "sqlite"
    # Commit every N batches on SQLite (WAL stays bounded); never on PG.
    commit_interval = 10 if is_sqlite else 0
    batches_since_commit = 0

    def apply_batch(batch: list[Snippet], blobs: list[bytes]) -> None:
        nonlocal reindexed, batches_since_commit
        for snippet, blob in zip(batch, blobs):
            snippet.minhash = blob
        reindexed += len(batch)
        if progress is not None:
            progress(reindexed, num_snippets)
        # Flush the batch's writes, then drop exactly these objects so the
        # identity map stays bounded.  (Not ``expunge_all``: in the parallel
        # path later batches are already loaded and still queued in flight —
        # detaching them here made their subsequent ``minhash`` writes
        # invisible to ``flush``, so every batch but the first was silently
        # left with its old fingerprint while the run reported full success.)
        session.flush()
        for snippet in batch:
            session.expunge(snippet)
        batches_since_commit += 1
        if commit_interval and batches_since_commit >= commit_interval:
            session.commit()
            batches_since_commit = 0

    # Fingerprints are about to change — drop any built index now so a crash
    # mid-reindex cannot leave a stale one behind.  The clear takes an
    # exclusive lock; when another process is concurrently building the index
    # (two CLI processes cold-finding the same database), retry briefly
    # instead of surfacing a raw "database is locked".
    for attempt in range(_REINDEX_CLEAR_RETRIES):
        try:
            lsh_index_clear(session)
            break
        except OperationalError:
            session.rollback()
            if attempt + 1 < _REINDEX_CLEAR_RETRIES:
                time.sleep(_REINDEX_CLEAR_RETRY_BACKOFF * (attempt + 1))
    else:
        logger.error(
            "Could not clear the index (another process may be writing to "
            "this database); retry once it is idle."
        )
        # Signal the failure explicitly: a success-shaped zero report made
        # ``resembl reindex`` print "Re-indexing Complete" and exit 0 after
        # doing nothing at all.
        return {
            "error": (
                "could not clear the index (another process may be writing "
                "to this database); retry once it is idle"
            ),
            "num_reindexed": 0,
            "time_elapsed": 0,
            "avg_time_per_snippet": 0,
        }

    if parallel:
        ctx = _mp.get_context("spawn")
        try:
            with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as executor:
                in_flight: deque[tuple[list[Snippet], Future[list[bytes]]]] = deque()
                max_in_flight = jobs * 2
                for batch in Snippet.iter_batches(session, batch_size):
                    codes = [snippet.code for snippet in batch]
                    in_flight.append(
                        (
                            batch,
                            executor.submit(_reindex_prepare, (codes, ngram_size, num_perm)),
                        )
                    )
                    if len(in_flight) >= max_in_flight:
                        pending_batch, future = in_flight.popleft()
                        apply_batch(pending_batch, future.result())
                while in_flight:
                    pending_batch, future = in_flight.popleft()
                    apply_batch(pending_batch, future.result())
        except Exception as exc:
            # Pool unavailable (e.g. spawned from a stdin script) — redo the
            # work sequentially; correctness must never depend on the pool.
            # Re-applying identical fingerprints is idempotent, so reset the
            # counter and process every batch again.  The triggering failure
            # (pool startup or a mid-run worker/DB error) is logged with its
            # traceback instead of being swallowed by the fixed message.
            logger.warning(
                "Parallel reindex failed (%s); retrying sequentially.",
                exc,
                exc_info=True,
            )
            reindexed = 0
            batches_since_commit = 0
            for batch in Snippet.iter_batches(session, batch_size):
                codes = [snippet.code for snippet in batch]
                apply_batch(batch, _reindex_prepare((codes, ngram_size, num_perm)))
    else:
        for batch in Snippet.iter_batches(session, batch_size):
            codes = [snippet.code for snippet in batch]
            apply_batch(batch, _reindex_prepare((codes, ngram_size, num_perm)))
    session.commit()
    # Fingerprints are now current — stamp the format version, n-gram size,
    # and permutation count so `find` does not reindex again.
    fingerprint_version_set(session, FINGERPRINT_VERSION)
    fingerprint_ngram_set(session, ngram_size)
    fingerprint_perm_set(session, num_perm)

    end_time = time.monotonic()
    time_elapsed = end_time - start_time

    return {
        "num_reindexed": reindexed,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": time_elapsed / num_snippets,
    }


def snippet_get(session: Session, checksum: str) -> Snippet | None:
    """Return a snippet by its checksum."""
    return Snippet.get_by_checksum(session, checksum)


def snippet_compare(session: Session, checksum1: str, checksum2: str) -> dict | None:
    """Compare two snippets and return similarity metrics."""
    from rapidfuzz import fuzz

    snippet1 = snippet_get(session, checksum1)
    snippet2 = snippet_get(session, checksum2)

    if not snippet1 or not snippet2:
        return None

    def _minhash_or_recomputed(snippet: Snippet) -> MinHash:
        """Return the stored fingerprint, or one recomputed from the code.

        Stored blobs are never deserialized in a non-packed format (that
        would be arbitrary code execution on hostile data), so a corrupt or
        pre-versioning fingerprint raises ``ValueError`` from unpacking.
        Recomputing from the snippet's own code keeps ``compare`` working on
        such databases — identical semantics to the self-healing reindex.
        """
        try:
            return snippet.get_minhash_obj()
        except ValueError:
            return code_create_minhash(snippet.code)

    m1 = _minhash_or_recomputed(snippet1)
    m2 = _minhash_or_recomputed(snippet2)
    jaccard_similarity = m1.jaccard(m2)

    levenshtein_score = fuzz.ratio(snippet1.code, snippet2.code)
    hybrid = score_hybrid(jaccard_similarity, levenshtein_score)

    tokens1 = set(code_tokenize(snippet1.code, normalize=True))
    tokens2 = set(code_tokenize(snippet2.code, normalize=True))
    shared_tokens = len(tokens1.intersection(tokens2))

    # CFG structural comparison
    cfg1 = cfg_extract(snippet1.code)
    cfg2 = cfg_extract(snippet2.code)
    cfg_sim = cfg_similarity(cfg1, cfg2)

    return {
        "snippet1": {
            "checksum": snippet1.checksum,
            "names": snippet1.name_list,
            "token_count": len(tokens1),
        },
        "snippet2": {
            "checksum": snippet2.checksum,
            "names": snippet2.name_list,
            "token_count": len(tokens2),
        },
        "comparison": {
            "jaccard_similarity": jaccard_similarity,
            "levenshtein_score": levenshtein_score,
            "hybrid_score": hybrid,
            "cfg_similarity": cfg_sim,
            "shared_normalized_tokens": shared_tokens,
        },
    }


def _random_snippet_rows(session: Session, limit: int) -> list[Snippet]:
    """Return up to *limit* uniformly random snippet rows via the checksum PK.

    ``ORDER BY random() LIMIT n`` evaluates the random function for every
    row and keeps the top-N — linear in the table size (measured ~21 ms at
    200k rows, ~100 s at a billion).  Checksums are content hashes, uniform
    over the 64-hex key space, so a contiguous run starting at a random key
    is a uniform sample, and the PK index makes it O(limit) regardless of
    table size (~0.6 ms measured).  Keys near the end of the key space wrap
    around via a second indexed query.
    """
    import secrets

    key = secrets.token_hex(32)
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
        _require_same_num_perm(num_perm, minhash_num_perm(blob))

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


def snippet_list(session: Session, start: int = 0, end: int = 0) -> list[Snippet]:
    """List snippets, optionally within a given range."""
    if end > 0:
        return list(session.exec(select(Snippet).offset(start).limit(end - start)).all())
    return list(Snippet.get_all(session))


def snippet_names_stream(
    session: Session, batch_size: int = 2000
) -> Iterator[list[tuple[str, str]]]:
    """Yield ``(checksum, names)`` pairs in batches via keyset pagination.

    Reads only the two columns the ``list`` command renders.  The ``code``
    column dominates the table, so loading full rows to list a large
    database would pull the whole corpus through the ORM (~1 GB at 500k
    snippets) — this keeps the unbounded listing flat in memory regardless
    of database size.  Each batch is fully consumed before the next is
    fetched (same keyset semantics as :meth:`Snippet.iter_batches`).
    """
    last: str | None = None
    while True:
        stmt = select(Snippet.checksum, Snippet.names).order_by(Snippet.checksum).limit(batch_size)
        if last is not None:
            stmt = stmt.where(Snippet.checksum > last)
        rows = session.exec(stmt).all()
        if not rows:
            return
        yield [(row[0], row[1]) for row in rows]
        last = rows[-1][0]


def snippet_collection_names_stream(
    session: Session, collection_name: str, batch_size: int = 2000
) -> Iterator[list[tuple[str, str]]]:
    """Yield ``(checksum, names)`` pairs for one collection's snippets in batches.

    Keyset pagination on the checksum PK keeps every query bounded, and only
    the two rendered columns are read: ``collection show`` used to load every
    member's full row through :meth:`Snippet.get_by_collection` — the ``code``
    column dominates the table, so showing a large collection pulled its whole
    corpus through the ORM just to render names.  Each batch is fully consumed
    before the next is fetched (same keyset semantics as
    :func:`snippet_names_stream`).
    """
    last: str | None = None
    while True:
        stmt = (
            select(Snippet.checksum, Snippet.names)
            .where(Snippet.collection == collection_name)
            .order_by(Snippet.checksum)
            .limit(batch_size)
        )
        if last is not None:
            stmt = stmt.where(Snippet.checksum > last)
        rows = session.exec(stmt).all()
        if not rows:
            return
        yield [(row[0], row[1]) for row in rows]
        last = rows[-1][0]


def snippet_search_by_name(session: Session, pattern: str, limit: int = 50) -> list[Snippet]:
    """Search for snippets where any name matches the pattern (case-insensitive).

    The JSON structure means names are embedded in the string, so a
    ``ILIKE '%pattern%'`` matches anywhere in the names list.  Case-
    insensitivity comes from ``ilike`` rather than plain ``LIKE``: SQLite
    happens to fold case in ``LIKE``, but PostgreSQL and DuckDB do not,
    which made the same search behave differently across backends.
    *limit* bounds the result (and the fetch) so a broad pattern on a
    large database returns a useful page instead of everything.
    """
    query_pattern = f"%{pattern}%"
    return list(
        session.exec(
            select(Snippet)
            .where(Snippet.names.ilike(query_pattern))  # type: ignore[attr-defined]
            .limit(limit)
        ).all()
    )


#: Characters invalid in filenames on at least one major filesystem:
#: Windows forbids ``< > : " / \ | ? *`` plus control characters, and the
#: others break round-trips between platforms.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Windows reserves these stems (case-insensitive, extension ignored):
#: writing "con.asm" targets the console device instead of a file.
_WINDOWS_RESERVED_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

#: Maximum encoded length of an exported filename stem, in bytes.  Name
#: limits are per-*byte* on POSIX filesystems (ext4: 255), and the ``.asm``
#: suffix plus the checksum disambiguator add up to 17 more — so the stem is
#: truncated here to stay inside every supported filesystem (and a Windows
#: MAX_PATH budget) regardless of how long the snippet's name is.
_EXPORT_STEM_MAX_BYTES = 230


def _export_safe_filename(name: str) -> str:
    """Sanitize a snippet name into a portable filename stem.

    Replaces characters that are illegal on Windows (and problematic
    elsewhere), blocks directory traversal, bounds the UTF-8 encoded length
    (filesystems cap filenames at 255 *bytes*; Windows MAX_PATH much lower),
    strips trailing dots/spaces (Windows silently drops them), and prefixes
    Windows reserved device names so ``con`` cannot target a device.
    """
    cleaned = _INVALID_FILENAME_CHARS.sub("_", name.replace("..", "_"))
    cleaned = os.path.basename(cleaned)
    if len(cleaned.encode("utf-8")) > _EXPORT_STEM_MAX_BYTES:
        # Cut by bytes, then drop any partial multi-byte character left at
        # the end ("ignore") so the stem stays a valid string.
        cleaned = cleaned.encode("utf-8")[:_EXPORT_STEM_MAX_BYTES].decode("utf-8", errors="ignore")
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        return ""
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_STEMS:
        cleaned = f"_{cleaned}"
    return cleaned


def snippet_export(session: Session, export_dir: str) -> dict:
    """Export all snippets to a directory."""
    start_time = time.monotonic()
    num_exported = 0

    os.makedirs(export_dir, exist_ok=True)

    abs_export_dir = os.path.realpath(export_dir)
    # Keys are os.path.normcase()-normalized so names differing only by
    # case cannot silently overwrite each other on case-insensitive
    # filesystems (macOS defaults, Windows).
    used_paths: set[str] = set()

    for snippet in Snippet.stream_all(session):
        # Use the first name as the primary name, sanitized for safety.
        primary_name = _snippet_primary_name(snippet)
        safe_name = _export_safe_filename(primary_name)
        if not safe_name:
            safe_name = snippet.checksum[:12]

        file_path = os.path.join(abs_export_dir, f"{safe_name}.asm")

        # Final guard: the sanitized name cannot contain separators, so every
        # written file must be a DIRECT child of the export directory.  A
        # plain ``startswith`` prefix test would accept sibling directories
        # sharing the prefix (``/out/export-evil`` vs ``/out/export``).
        resolved_path = os.path.realpath(file_path)
        if os.path.dirname(resolved_path) != abs_export_dir:
            logger.warning(
                "Skipping snippet '%s': resolved path is outside export directory.",
                primary_name,
            )
            continue

        # Avoid silently overwriting when several snippets share a name
        # (compared case-insensitively — see used_paths above).
        used_key = os.path.normcase(file_path)
        if used_key in used_paths:
            # 12 hex chars (48 bits) keeps the disambiguator collision-free
            # even with hundreds of thousands of same-named snippets (the
            # previous 8 chars collided at ~30 pairs per 500k).
            file_path = os.path.join(abs_export_dir, f"{safe_name}-{snippet.checksum[:12]}.asm")
            used_key = os.path.normcase(file_path)
        used_paths.add(used_key)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(snippet.code)
        num_exported += 1

    end_time = time.monotonic()
    time_elapsed = end_time - start_time

    return {
        "num_exported": num_exported,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": (time_elapsed / num_exported if num_exported > 0 else 0),
    }


def db_verify(session: Session) -> dict:
    """Report the database's health: index, fingerprints, and pending work.

    Returns a dict with counts, ``warnings`` (self-healing states: a missing
    index or stale fingerprints, both repaired by the next ``find``) and
    ``issues`` (a bucket/snippet mismatch — a genuinely stale index that
    ``reindex --force`` should resolve).  Callers typically exit non-zero
    only when ``issues`` is non-empty.
    """
    num_snippets = session.exec(
        select(func.count(Snippet.checksum))  # type: ignore[arg-type]
    ).one()
    warnings: list[str] = []
    issues: list[str] = []

    stored_version = fingerprint_version_get(session)
    if stored_version != FINGERPRINT_VERSION:
        warnings.append("fingerprints are from an older format — the next `find` reindexes once")

    meta = lsh_meta_get(session)
    num_buckets = 0
    expected_buckets: int | None = None
    if meta is None:
        warnings.append("no LSH index — the next `find` builds it")
    else:
        threshold, num_perm = meta
        b, _r = banding_params(threshold, num_perm)
        expected_buckets = num_snippets * b
        table_missing = False
        try:
            # Count only band 0: every snippet contributes exactly one row
            # per band, and staleness (missing or extra rows) affects all
            # bands uniformly — so band0 * b is the exact total for a
            # consistent index, and the scan is 1/b of the full count
            # (~100ms -> ~4ms at 500k, minutes -> seconds at billions).
            band0 = session.exec(
                select(func.count(LSHBucket.checksum)).where(  # type: ignore[arg-type]
                    LSHBucket.band == 0
                )
            ).one()
            num_buckets = band0 * b
        except OperationalError:
            # lsh_bucket missing while its meta row says an index exists —
            # e.g. a crash inside the drop/recreate window, or a manual drop.
            # The next `find` rebuilds, so this is a warning, not an issue;
            # the bucket-count comparison below must not run either (a zero
            # count against a nonzero expectation would raise exactly the
            # stale-index issue this state never produces).
            warnings.append("lsh_bucket table is missing — the next `find` rebuilds the index")
            num_buckets = 0
            table_missing = True
        if not table_missing and num_snippets > 0 and num_buckets != expected_buckets:
            issues.append(
                f"index may be stale ({num_buckets} bucket rows, expected "
                f"{expected_buckets}) — run `resembl reindex --force`"
            )

    return {
        "num_snippets": num_snippets,
        "num_buckets": num_buckets,
        "expected_buckets": expected_buckets,
        "fingerprint_version": stored_version,
        "warnings": warnings,
        "issues": issues,
    }


def db_clean(session: Session) -> dict:
    """Clean the LSH cache and vacuum the database."""
    start_time = time.monotonic()

    # 1. Wipe the legacy cache files and the DB-backed index.
    lsh_index_clear(session)

    # 2. Vacuum the database to reclaim space (SQLite only).
    vacuum_success = False
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("VACUUM"))
        session.commit()
        vacuum_success = True

    end_time = time.monotonic()
    time_elapsed = end_time - start_time

    return {
        "time_elapsed": time_elapsed,
        "vacuum_success": vacuum_success,
    }


# ---------------------------------------------------------------------------
# Collection Functions
# ---------------------------------------------------------------------------


def collection_create(session: Session, name: str, description: str = "") -> Collection:
    """Create a new snippet collection."""
    collection = Collection(name=name, description=description)
    session.add(collection)
    session.commit()
    session.refresh(collection)
    return collection


def collection_delete(session: Session, name: str, quiet: bool = False) -> bool:
    """Delete a collection and unassign all its snippets."""
    collection = Collection.get_by_name(session, name)
    if not collection:
        if not quiet:
            logger.error("Collection '%s' not found.", name)
        return False

    # Unassign snippets in one bulk UPDATE: the per-row loop loaded every
    # member's full row (the ``code`` column dominates the table) through the
    # ORM just to null one column, and issued one UPDATE per row on flush —
    # quadratic round-trip pressure for a large collection.
    session.execute(
        update(Snippet)
        .where(Snippet.collection == name)  # type: ignore[arg-type]
        .values(collection=None)
    )

    session.delete(collection)
    session.commit()
    return True


def collection_list(session: Session) -> list[dict]:
    """List all collections with snippet counts.

    Counts come from one indexed ``GROUP BY`` over the ``collection``
    column instead of loading every member's full row per collection —
    the old per-collection ``get_by_collection`` pulled the whole corpus
    (code column included) through the ORM just to count it.
    """
    collections = Collection.get_all(session)
    counts = dict(
        session.exec(
            select(
                Snippet.collection,
                func.count(Snippet.checksum),  # type: ignore[arg-type]
            )
            .where(Snippet.collection.is_not(None))  # type: ignore[union-attr]
            .group_by(Snippet.collection)
        ).all()
    )
    return [
        {
            "name": col.name,
            "description": col.description,
            "snippet_count": counts.get(col.name, 0),
            "created_at": col.created_at,
        }
        for col in collections
    ]


def collection_add_snippet(
    session: Session, collection_name: str, checksum: str, quiet: bool = False
) -> Snippet | None:
    """Add a snippet to a collection."""
    collection = Collection.get_by_name(session, collection_name)
    if not collection:
        if not quiet:
            logger.error("Collection '%s' not found.", collection_name)
        return None

    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    snippet.collection = collection_name
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def collection_remove_snippet(
    session: Session, checksum: str, quiet: bool = False
) -> Snippet | None:
    """Remove a snippet from its collection."""
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    snippet.collection = None
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


# ---------------------------------------------------------------------------
# Version Functions
# ---------------------------------------------------------------------------


def snippet_version_list(session: Session, checksum: str) -> list[dict]:
    """Return version history for a snippet."""
    versions = SnippetVersion.get_by_checksum(session, checksum)
    return [
        {
            "id": v.id,
            "snippet_checksum": v.snippet_checksum,
            "created_at": v.created_at,
        }
        for v in versions
    ]


# ---------------------------------------------------------------------------
# Merge Functions
# ---------------------------------------------------------------------------


def db_merge(session: Session, source_db_path: str) -> dict:
    """Merge snippets from a source database into the current one.

    *source_db_path* is a SQLite file path, or a full database URL (e.g.
    ``duckdb:///file.db`` or ``postgresql+pg8000://user:pass@host/db``) —
    any backend with its driver installed can be a source.

    Deduplicates by checksum:
    - New snippets (unique checksum) are inserted.
    - Existing snippets gain any new names and tags from the source.
    - Collections from the source are created if they don't exist.

    Returns a dict with counts of added, updated, and skipped snippets.
    """
    from .database import create_db_engine

    start_time = time.monotonic()
    # The source may be any backend: a full URL (e.g. duckdb:///file.db,
    # postgresql+pg8000://...) is used as-is; otherwise it is a SQLite path.
    source_url = source_db_path if "://" in source_db_path else f"sqlite:///{source_db_path}"

    try:
        source_engine = create_db_engine(source_url)
        source_session = Session(source_engine)
    except Exception as e:
        logger.error("Failed to open source database: %s", e)
        return {"error": str(e)}

    added = 0
    updated = 0
    skipped = 0
    added_minhashes: list[tuple[str, bytes]] = []
    new_rows: list[dict[str, object]] = []

    try:
        # Import collections first.  Local collections are snapshotted once
        # (a per-source ``get_by_name`` was one destination round trip per
        # imported collection); new names are added to the snapshot as they
        # are created so duplicates stay impossible without re-querying.
        local_collections = {col.name: col for col in Collection.get_all(session)}
        for col in source_session.exec(select(Collection)).all():
            if col.name not in local_collections:
                new_col = Collection(
                    name=col.name,
                    description=col.description,
                    created_at=timestamp_normalize(col.created_at),
                )
                session.add(new_col)
                local_collections[col.name] = new_col

        # Source snippets that already exist locally are merged; new ones are
        # bulk-inserted.  Existence is decided by one chunked IN query per
        # chunk of source, so memory stays O(chunk) regardless of the LOCAL
        # database size — the old design preloaded every local checksum into
        # a set (O(local DB) memory, a wall when consolidating against
        # billions of snippets) and then fetched overlapping rows a second
        # time.  The chunk's returned rows ARE the merge rows, so there is
        # no second fetch, and the chunked INs replace both the preload and
        # the per-overlap ``session.get`` (the N+1 fixed earlier).
        merge_chunk: list[Snippet] = []

        # New rows are flushed to the database in chunks so a mostly-new
        # merge stays flat in memory regardless of source size.
        merge_flush_size = 5000

        def flush_new_rows() -> None:
            """Bulk-insert buffered new rows and sync the index, then clear."""
            if not new_rows:
                return
            _insert_snippet_rows(session, new_rows)
            session.commit()
            lsh_index_add_batch(session, added_minhashes)
            new_rows.clear()
            added_minhashes.clear()

        def record_new(src_snippet: Snippet) -> None:
            nonlocal added, skipped
            try:
                packed = minhash_ensure_packed(src_snippet.minhash)
            except ValueError:
                # A non-packed fingerprint (legacy format or corrupt) is
                # never deserialized — that would execute attacker-controlled
                # pickle code from a hostile merge source.  Recompute it from
                # the source row's code instead; only a row with no usable
                # code either is skipped.
                logger.warning(
                    "Recomputing fingerprint for source snippet %s (unsupported format).",
                    src_snippet.checksum,
                )
                try:
                    packed = minhash_pack(code_create_minhash(src_snippet.code))
                except Exception:
                    logger.warning(
                        "Skipping source snippet %s: corrupt fingerprint and "
                        "no recomputable code.",
                        src_snippet.checksum,
                    )
                    skipped += 1
                    return
            new_rows.append(
                {
                    "checksum": src_snippet.checksum,
                    "names": src_snippet.names,
                    "code": src_snippet.code,
                    "minhash": packed,
                    "tags": src_snippet.tags,
                    "collection": src_snippet.collection,
                }
            )
            added += 1
            added_minhashes.append((src_snippet.checksum, packed))
            if len(new_rows) >= merge_flush_size:
                flush_new_rows()

        def flush_merge_chunk() -> None:
            nonlocal updated, skipped
            if not merge_chunk:
                return
            local_rows = _snippets_by_checksums(session, [s.checksum for s in merge_chunk])
            for src_snippet in merge_chunk:
                existing = local_rows.get(src_snippet.checksum)
                if existing is None:
                    # Not in the local database (or vanished concurrently) —
                    # treat as new, matching the old fallback.
                    record_new(src_snippet)
                    continue
                changed = False

                # Merge names and tags order-preservingly: existing entries
                # keep their positions (name_list[0] is the primary name — it
                # drives display, export filenames, and YARA rule names) and
                # source-only entries are appended.  Same convention as
                # ``snippet_add_batch``'s alias merge; a sorted union would
                # silently reassign the primary name on every merge.
                existing_names = existing.name_list
                merged_names = list(dict.fromkeys(existing_names + src_snippet.name_list))
                if merged_names != existing_names:
                    existing.names = json.dumps(merged_names)
                    changed = True

                # Merge tags (independent of names)
                existing_tags = existing.tag_list
                merged_tags = list(dict.fromkeys(existing_tags + src_snippet.tag_list))
                if merged_tags != existing_tags:
                    existing.tags = json.dumps(merged_tags)
                    changed = True

                if changed:
                    session.add(existing)
                    updated += 1
                else:
                    skipped += 1

                # Assign collection if the existing snippet doesn't have one
                if not existing.collection and src_snippet.collection:
                    existing.collection = src_snippet.collection
                    session.add(existing)
            merge_chunk.clear()

        # Import snippets (streaming, so memory stays bounded for big sources)
        for src_snippet in Snippet.stream_all(source_session):
            merge_chunk.append(src_snippet)
            if len(merge_chunk) >= 900:
                flush_merge_chunk()
        flush_merge_chunk()

        # Persist any remaining new rows.  ``record_new`` flushes in chunks
        # (see below) so a mostly-new merge never accumulates the whole new
        # content in memory — same bounded-memory design as import.
        flush_new_rows()
        # Persist the session.add'ed merge updates (aliases, tags, collections).
        session.commit()
        # The source blobs were copied verbatim and may be from an older
        # fingerprint format or permutation count — drop the stamps so the
        # next `find` reindexes once and normalizes everything.
        fingerprint_version_clear(session)
        fingerprint_ngram_clear(session)
        fingerprint_perm_clear(session)
    except Exception as e:
        logger.error("Merge failed: %s", e, exc_info=True)
        return {"error": str(e)}
    finally:
        source_session.close()
        # Return the pooled connections to the OS — the source engine is
        # never reused after this call.
        source_engine.dispose()

    end_time = time.monotonic()
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "total_source": added + updated + skipped,
        "time_elapsed": end_time - start_time,
    }
