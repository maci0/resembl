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
import tempfile
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError, OperationalError
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
    lsh_index_purge,
    lsh_pickle_cache_remove,
)
from .clock import (
    _retry_sleep,
    avg_time_per_item,
    monotonic_now,
    timestamp_normalize,
    timestamp_now,
)
from .lsh import (
    banding_params,
    fingerprint_ngram_clear,
    fingerprint_ngram_set,
    fingerprint_perm_clear,
    fingerprint_perm_set,
    fingerprint_stamps_get,
    fingerprint_stamps_reconcile,
    fingerprint_version_clear,
    fingerprint_version_get,
    fingerprint_version_set,
    index_build_lock,
    lsh_meta_get,
    sql_text_literal,
)
from .models import (
    FINGERPRINT_VERSION,
    Collection,
    LSHBucket,
    Snippet,
    SnippetVersion,
    name_normalize,
)
from .paths import ascii_fold_lower, config_path_get, db_url_mask, runtime_manifest_path_get
from .scoring import (
    NUM_PERMUTATIONS,
    cfg_extract,
    cfg_similarity,
    code_create_minhash,
    code_create_minhash_batch,
    code_tokenize,
    code_tokenize_normalize_lexed,
    get_lexer,
    minhash_ensure_packed,
    minhash_from_tokens,
    minhash_jaccard_batch,
    minhash_num_perm,
    minhash_pack,
    normalize_unicode,
    score_hybrid,
)

# Re-exported for external backward compatibility only; not used in this
# module (`from resembl.core import string_normalize` keeps working).
# isort: split
from .scoring import (  # noqa: F401
    BRANCH_INSTRUCTIONS,
    COMMON_INSTRUCTIONS,
    RARE_INSTRUCTIONS,
    shingle_weight,
    string_checksum,
    string_normalize,
)

#: Statistics over the store live in ``resembl.diagnostics``; re-exported here
#: for the compatibility surface ``from resembl.core import db_stats`` keeps
#: working.
# isort: split
from .diagnostics import (  # noqa: F401
    SEED_ENV_VAR,
    db_calculate_average_similarity,
    db_stats,
)

logger = logging.getLogger(__name__)

# Constants

#: Default LSH similarity threshold for candidate filtering.
LSH_THRESHOLD = 0.5

#: Name of the legacy shared manifest :func:`db_backup` used to write beside
#: a backup file.  It carried the snippet count and the fingerprint version
#: of only the newest backup, so it described every other retained copy
#: wrongly; each backup now gets its own ``<backup-stem>.json`` manifest
#: (see :func:`_db_backup_manifest_name`).  The name stays so
#: :func:`db_verify_backup` can still read a backup that predates per-backup
#: manifests, when the shared file records that backup's own name.
DB_BACKUP_MANIFEST_NAME = ".resembl-backup.json"

#: Name of the config sidecar :func:`db_backup` writes beside a backup.
#: ``config.toml`` (see :func:`resembl.paths.config_path_get`) is the only
#: non-corpus state an operator tunes by hand and cannot regenerate
#: byte-for-byte, so it is copied verbatim next to the database backup it
#: was taken with and hashed into the manifest (``config_sha256``).
#: A missing config is recorded as ``null`` rather than failing the backup.
DB_BACKUP_CONFIG_NAME = "config.toml"

#: Name of the runtime-manifest sidecar :func:`db_backup` writes beside a
#: backup.  ``RESEMBL_RUNTIME_MANIFEST`` names the file ``serve`` requires
#: for ``/find-pinned``; it pins interpreter + module hashes and cannot be
#: regenerated without re-attesting the exact runtime, so a host loss that
#: drops it leaves ``/find-pinned`` unrestorable even when the corpus
#: restores cleanly.  Like ``config.toml`` it is copied verbatim, mode
#: ``0600``, hashed into the manifest (``runtime_sha256``), and
#: cross-checked by :func:`db_verify_backup`.  A missing manifest is
#: recorded as ``null`` rather than failing the backup.
DB_BACKUP_RUNTIME_NAME = "runtime-manifest.json"


def _fsync_quiet(path: str) -> None:
    """Flush *path* to disk, ignoring filesystems that refuse it.

    A backup renamed into place without an fsync is durable only in the page
    cache: an OS crash can leave a zero-length or short file at the published
    name, or lose the directory entry that names it even though the data
    survived.  Filesystems that do not support fsync (some network mounts,
    Windows directories) must not fail an otherwise complete backup, so
    ``OSError`` is swallowed.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        logger.warning("fsync skipped for %s: could not open: %s", path, e)
        return
    try:
        os.fsync(fd)
    except OSError as e:
        logger.warning("fsync failed for %s: %s", path, e)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _backup_stem(backup_path: str) -> str:
    return os.path.splitext(os.path.basename(backup_path))[0]


def _fchmod_quiet(fd: int, mode: int, path: str) -> None:
    """Set *mode* on the open file *fd*, where the platform supports it.

    ``os.fchmod`` is POSIX-only: on Windows it does not exist, so an
    unconditional call raises ``AttributeError`` and aborts the export,
    sidecar or manifest write on a claimed platform (windows-2025 in
    CI).  On Windows ``os.chmod`` on the path is the equivalent (it
    only toggles the read-only bit there, which is all Windows models),
    so fall back to it by path; where neither exists, skip silently.
    """
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        try:
            fchmod(fd, mode)
        except OSError as e:
            logger.warning("Could not set permissions on %s: %s", path, e)
        return
    try:
        os.chmod(path, mode)
    except OSError as e:
        logger.warning("Could not set permissions on %s: %s", path, e)


def _remove_quietly(path: str) -> None:
    """Delete *path* if present, logging (not raising) a failed removal.

    Cleanup on an error path must not mask the original failure, but a
    removal that fails silently strands the file for a later run to trust,
    so the failure is recorded at warning level with the path and cause.
    """
    try:
        os.remove(path)
    except OSError as e:
        logger.warning("Could not remove %s during cleanup: %s", path, e)


def _db_backup_manifest_name(backup_path: str) -> str:
    """Return the per-backup manifest name for *backup_path*.

    Each backup gets its own ``<backup-stem>.json`` manifest because a
    directory retains several backups at once (``keep`` defaults to 7):
    a single shared manifest always described only the newest backup,
    so ``verify-backup`` on any older retained copy compared its counts
    against counts that were never its own and reported a mismatch on
    every backup but the latest.
    """
    return f"{_backup_stem(backup_path)}.json"


def _db_backup_config_name(backup_path: str) -> str:
    """Return the config sidecar name for a backup path.

    The sidecar is ``<backup-stem>.config.toml`` so a directory retaining
    several backups keeps each backup's config with its own database copy,
    exactly like the per-backup ``<backup-stem>.json`` manifest.
    """
    return f"{_backup_stem(backup_path)}.config.toml"


def _db_backup_runtime_name(backup_path: str) -> str:
    """Return the runtime-manifest sidecar name for a backup path.

    The sidecar is ``<backup-stem>.runtime-manifest.json`` so a directory
    retaining several backups keeps each backup's pinned runtime with its
    own database copy, exactly like the config and manifest sidecars.
    """
    return f"{_backup_stem(backup_path)}.{DB_BACKUP_RUNTIME_NAME}"


#: Hard ceiling on a sidecar / manifest read: ``verify_runtime`` refuses a
#: manifest over 2 MiB, and a config is a few KiB.  Reading any of them
#: unbounded (``f.read()``) loads the whole file before any check, so a
#: planted multi-GiB file beside a backup pins gigabytes on the backup or
#: verify path.  Mirrors ``resembl.runtime_attestation``'s 2 MiB cap.
_MAX_SIDECAR_BYTES = 2 * 1024 * 1024


def _read_bounded_file(path: str, limit: int = _MAX_SIDECAR_BYTES) -> bytes:
    """Read *path* refusing a body over *limit* bytes.

    ``open(...).read()`` buffers the whole file before any size check.
    Read one byte past the limit so an oversize file is detected without
    allocating unboundedly, raising :class:`ValueError` (a caller that
    cannot bound the source fails closed instead of loading it).
    """
    with open(path, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path!r} exceeds {limit} bytes")
    return data


def _db_backup_runtime_read() -> tuple[bytes | None, str | None]:
    """Return ``(raw_bytes, sha256)`` for the live runtime manifest.

    ``(None, None)`` only when ``RESEMBL_RUNTIME_MANIFEST`` is unset (or
    blank, which counts as unset like every other ``RESEMBL_*`` override):
    no pinned runtime is in use, so it is recorded as absent rather than
    failing the backup.  A set-but-missing path raises ``FileNotFoundError``
    so the caller fails loud rather than reporting a complete backup that
    drops pinned state while the operator believes it was captured; any
    other unreadable path likewise raises ``OSError``.
    """
    manifest_path = runtime_manifest_path_get()
    if manifest_path is None:
        return None, None
    data = _read_bounded_file(manifest_path)
    return data, hashlib.sha256(data).hexdigest()


def _write_sidecar(backup_dir: str, name: str, data: bytes) -> str:
    """Write *data* beside a backup as a mode-``0600`` file, durably.

    Used for the config and runtime-manifest sidecars so both publish the
    same way: same-directory temp file, fsync, atomic rename, directory
    fsync.  Returns the sidecar path.
    """
    sidecar_path = os.path.join(backup_dir, name)
    # mkstemp (O_EXCL, random suffix): a predictable ``{name}.{pid}.tmp``
    # name let a local planter symlink the temp at a victim file.
    fd, tmp = tempfile.mkstemp(dir=backup_dir, prefix=f".{name}.", suffix=".tmp")
    try:
        _fchmod_quiet(fd, 0o600, tmp)
        with open(fd, "wb") as f:
            f.write(data)
        _fsync_quiet(tmp)
        os.replace(tmp, sidecar_path)
        _fsync_quiet(backup_dir)
    except OSError:
        _remove_quietly(tmp)
        raise
    return sidecar_path


def _db_backup_config_read() -> tuple[bytes | None, str | None]:
    """Return ``(raw_bytes, sha256)`` for the live ``config.toml``.

    ``(None, None)`` when no config file exists: a missing config means
    defaults, which are regenerable, so it is recorded as absent rather
    than failing the backup.  An unreadable config raises ``OSError`` so
    the caller can decide whether to fail loud.
    """
    try:
        data = _read_bounded_file(config_path_get())
    except FileNotFoundError:
        return None, None
    return data, hashlib.sha256(data).hexdigest()


def _db_content_hash(session: Session) -> str:
    """Return a stable identity for the snippet corpus in *session*.

    The SHA-256 hex digest over ``(checksum, names, tags, collection,
    minhash)`` in primary-key order.  Two databases holding the same snippets
    — however their files happen to be paged — hash identically, which is what
    makes a re-executed backup recognizable as the same logical backup instead
    of a new one.  The metadata columns are part of the identity: an alias,
    tag, or collection change alters the corpus a restore would hand back, so a
    backup taken before it must not be reused after it (that reuse returned a
    stale copy as if it were current).  The fingerprint payload is part of the
    identity too: a reindex (new n-gram, new permutation count, or healing a
    corrupt blob) rewrites ``minhash`` for the same checksums, and a backup
    taken before that rewrite holds different fingerprints than the live
    database — reusing it would hand back a stale restore point while
    reporting success.  Code is keyed by checksum (the checksum is taken over
    the normalized code), so it needs no separate hashing.
    """
    digest = hashlib.sha256()
    # Stream: the old ``.all()`` materialized every row (O(corpus) memory)
    # just to hash it.  Iterating the result keeps this O(1) extra memory.
    rows = session.exec(
        select(  # type: ignore[call-overload]
            Snippet.checksum, Snippet.names, Snippet.tags, Snippet.collection, Snippet.minhash
        ).order_by(Snippet.checksum)
    ).yield_per(1000)
    for checksum, names, tags, collection, minhash in rows:
        for value in (checksum, names, tags, collection or ""):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        digest.update(minhash)
        digest.update(b"\0")
    return digest.hexdigest()


def _db_backup_candidates(backup_dir: str, names: list[str]) -> list[str]:
    """Backup files in *backup_dir*, oldest first by embedded timestamp.

    Order by the timestamp embedded in the name (``assembly-YYYYMMDDTHHMMSS-ffffff.db``),
    not by filesystem mtime: mtime is wall-clock state written by the OS
    when the file is created, so two pinned replays (``RESEMBL_NOW``)
    produce the same names but different mtimes, and prune/reuse would
    pick a different "newest" on each replay.  The embedded timestamp
    comes from the same logical clock as the manifest's ``created_at``
    (``timestamp_now``, which honors ``RESEMBL_NOW``), so a pinned replay
    orders identically.  A renamed copy that loses its embedded timestamp
    sorts oldest (empty key) rather than by mtime, so it is pruned first
    instead of hiding the genuinely newest restore point.
    """

    def _stamp(name: str) -> tuple[str, str]:
        stem = name.removeprefix("assembly-").removesuffix(".db")
        # ``stem`` is ``YYYYMMDDTHHMMSS-ffffff`` for names this module
        # writes; anything else (hand-supplied, legacy) sorts oldest.
        # The digit positions must be ASCII digits too: a length/shape
        # match on a lexicographic sort would let a name like
        # ``assembly-29991231T235959-999999.db`` (all digits, valid shape)
        # sort newest and pin the prune window, while a non-digit payload
        # that happens to match the shape would sort above real stamps
        # ('z' > '9') and evict genuine backups.  Only all-digit stamps
        # count as embedded timestamps.
        if (
            len(stem) == 22
            and stem[8] == "T"
            and stem[15] == "-"
            and all(part.isascii() and part.isdigit() for part in (stem[:8], stem[9:15], stem[16:]))
        ):
            return (stem, name)
        return ("", name)

    return sorted(
        (
            name
            for name in names
            if name.startswith("assembly-")
            and name.endswith(".db")
            and os.path.isfile(os.path.join(backup_dir, name))
        ),
        key=_stamp,
    )


def _db_backup_reuse(backup_dir: str, content_hash: str) -> str | None:
    """Return the newest existing backup of identical content, or ``None``.

    A backup is reusable when its per-backup manifest records the same
    :func:`_db_content_hash` and the backup file is still present.  A
    missing, unreadable, or legacy (pre-hash) manifest never matches, so
    those backups are simply not reused — a fresh one is written instead.
    Candidates are scanned newest-first and the first match wins: a
    duplicate run follows the run that produced it, so the matching
    backup is among the newest — and scanning (rather than trusting one
    filename) keeps the check correct even if name order inverts.
    """
    try:
        names = os.listdir(backup_dir)
    except OSError as e:
        logger.warning("Could not list backup directory %s for reuse check: %s", backup_dir, e)
        return None

    candidates = _db_backup_candidates(backup_dir, names)
    for newest in reversed(candidates):
        manifest_path = os.path.join(backup_dir, _db_backup_manifest_name(newest))
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except OSError, ValueError, RecursionError:
            # RecursionError is not a ValueError: a deeply nested
            # manifest from a hostile backup directory recurses once
            # per level in the decoder and must skip the candidate,
            # not abort the caller — the same guard _string_list_or_none
            # applies to stored JSON columns.
            continue
        if isinstance(manifest, dict) and manifest.get("content_hash") == content_hash:
            return os.path.join(backup_dir, newest)
    return None


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


class _MergeRetryError(Exception):
    """Internal signal: a chunk collided on the snippet primary key.

    Raised inside :func:`db_merge` when a bulk insert hits an
    ``IntegrityError`` because a concurrent merge committed the same
    checksums first.  Caught by the merge loop, which rolls back and
    re-runs the dedup pass on the now-current rows — the same converge
    strategy :func:`snippet_add_batch` uses — instead of surfacing the
    collision as a failed merge.
    """


def adaptive_worker_count(num_items: int, cpu_count: int) -> int:
    """Choose a sensible worker count for a parallel job of *num_items* items.

    One worker per CPU is wasteful for small jobs: spawning each worker
    costs the full interpreter + library import (~450 ms and ~50 MB),
    so a 300-item job measured 1.85 s with 32 workers vs 0.84 s with 4.
    The default scales with the work (one worker per ~100 items) and is
    capped at the CPU count, so small jobs stay single-process and large
    ones parallelize fully.
    """
    return max(1, min(cpu_count, num_items // 100 + 1))


def _merge_names(existing: list[str], incoming: list[str]) -> list[str]:
    """Union two alias lists, comparing and storing names in NFC.

    Order is preserved (the first entry is the primary name, which drives
    display, export filenames and YARA rule names), and a name already
    present under the other canonical spelling is not appended again: a
    macOS-derived name and the composed spelling of the same file are one
    alias.
    """
    merged = list(existing)
    seen = {name_normalize(name) for name in existing}
    for name in incoming:
        normalized = name_normalize(name)
        if normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


def snippet_name_add(
    session: Session, checksum: str, new_name: str, quiet: bool = False
) -> Snippet | None:
    """Add a new name to an existing snippet (idempotent).

    A name already on the snippet is a no-op that returns the snippet, the
    same contract :func:`snippet_tag_add` has.  Reporting a failure there
    made a retried add (a lost response, a double-clicked command, a script
    re-run) exit 1 for an end state it had already produced.
    """
    new_name = name_normalize(new_name)
    if not new_name:
        if not quiet:
            logger.error("Name cannot be empty.")
        return None

    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    name_list = snippet.name_list
    # Compare in NFC: a name typed here and the same name imported from a
    # macOS filesystem (NFD) are one alias, not two.  The query is already
    # NFC.  ASCII is NFC as well, so composing an ASCII alias again cannot
    # change equality.  A non-ASCII alias may still be the decomposed
    # spelling (a row from before names were composed, or from macOS).
    if any(new_name == (name if name.isascii() else name_normalize(name)) for name in name_list):
        return snippet  # Idempotent: already named

    name_list.append(new_name)
    snippet.names = json.dumps(name_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def snippet_name_remove(
    session: Session, checksum: str, name_to_remove: str, quiet: bool = False
) -> Snippet | None:
    """Remove a name from a snippet (idempotent — removing a missing name is a no-op).

    Already-removed is the requested end state, same as
    :func:`snippet_tag_remove` and the add paths: a retried remove (lost
    response, double-click, re-run) must converge with a success
    response instead of exiting 1.  The one-name guard below is different:
    that state was never produced, so it stays a failure.
    """
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    # Names are stored in NFC, so the argument is normalized too: passing the
    # NFD spelling macOS hands back for a file whose name has an accent must
    # still find the row.
    name_list = snippet.name_list
    # A name may have been stored before normalization, or arrived from a
    # filesystem that spells it decomposed; remove the stored spelling.
    target = name_normalize(name_to_remove)
    match = next((n for n in name_list if name_normalize(n) == target), None)
    if match is None:
        return snippet  # Idempotent: name not present

    if len(name_list) == 1:
        if not quiet:
            logger.error("Cannot remove the last name from a snippet.")
        return None

    name_list.remove(match)
    snippet.names = json.dumps(name_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def snippet_tag_add(
    session: Session, checksum: str, tag: str, quiet: bool = False
) -> Snippet | None:
    """Add a tag to a snippet (idempotent — adding an existing tag is a no-op)."""
    # Tags are identity like names, so they use the same normalization:
    # ``normalize_unicode(tag.strip())`` left a leading space behind when a
    # BOM preceded the padding ("\ufeff mytag" -> " mytag"), a spelling
    # no stripped lookup could reach.  ``name_normalize`` loops over BOM
    # and whitespace together and composes to NFC.
    tag = name_normalize(tag)
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
    # Compare in NFC, as names do: a tag stored before normalization (or
    # merged in from a macOS-written database, which hands out NFD)
    # spelled the other way is the same tag, not a second one.  The
    # stored side is normalized unconditionally, unlike the name paths:
    # an ASCII stored tag can still carry a BOM/space residue from before
    # tags shared the name normalization, and that residue must match.
    if any(tag == name_normalize(stored) for stored in tag_list):
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
    tag = name_normalize(tag)
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    tag_list = snippet.tag_list
    # Match in NFC, as snippet_tag_add does: removing the composed
    # spelling of a tag stored decomposed (or vice versa) must find it.
    match = next(
        (stored for stored in tag_list if tag == name_normalize(stored)),
        None,
    )
    if match is None:
        return snippet  # Idempotent: tag not present

    tag_list.remove(match)
    snippet.tags = json.dumps(tag_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


# Snippet CRUD


def snippet_lex(code: str) -> tuple[str, list[str]] | None:
    """Lex *code* once and return ``(checksum, normalized_tokens)``.

    Returns ``None`` for empty code.  The normalized string (for the
    checksum) and the token list (for the MinHash) come from a single fused
    pass over one Pygments token stream.  Callers that need only the
    checksum (the alias path of :func:`snippet_add`) or only the fingerprint
    still pay the lexer exactly once, instead of once per derived value.
    """
    # NFC first: the checksum is the snippet's primary key, so the same string
    # in two composition forms would have to be stored twice and neither
    # query could find the other.  The empty check runs on the normalized
    # form: a leading BOM is not part of the code, so BOM-only code is
    # empty like whitespace-only code.
    normalized_code = normalize_unicode(code)
    if not normalized_code.strip():
        return None
    # Materialize the token stream: it is consumed twice (once for the
    # normalized checksum string, once for the MinHash tokens), and the fused
    # pass derives both from one iteration instead of two.
    tokens = list(get_lexer().get_tokens(normalized_code))
    token_list, normalized = code_tokenize_normalize_lexed(tokens)
    checksum = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()
    return checksum, token_list


def snippet_prepare(
    name: str, code: str, ngram_size: int = 3, num_perm: int = NUM_PERMUTATIONS
) -> tuple[str, str, str, bytes] | None:
    """Compute the checksum and MinHash fingerprint for a snippet.

    Returns ``(checksum, name, code, minhash_bytes)`` or ``None`` for empty
    code.  This is a pure function with no database access, so it is safe to
    run in worker processes when bulk-importing many files.

    The snippet is lexed exactly once: the normalized string (for the
    checksum) and the token list (for the MinHash) are both derived from the
    same token stream.  Lexing with Pygments is the dominant per-snippet
    cost, so this halves it on the import hot path.

    *name* is normalized to NFC here, the ingestion point every other name
    path normalizes at too, so a directory walked on macOS (which spells
    names decomposed) and the same directory walked on Linux (composed)
    yield one alias rather than two.
    """
    name = name_normalize(name)
    lexed = snippet_lex(code)
    if lexed is None:
        return None
    checksum, token_list = lexed
    # Stored in the same form the checksum was taken over, so what the
    # database holds is exactly what was hashed.
    return (
        checksum,
        normalize_unicode(name),
        normalize_unicode(code),
        minhash_pack(minhash_from_tokens(token_list, ngram_size, num_perm=num_perm)),
    )


#: Checksums per SQL ``IN`` clause (and the merge chunk size that feeds one).
#: 900 stays comfortably under SQLite's variable limit (999 by default,
#: 32766 on modern builds), halving the round trips of the old 500.
_CHECKSUM_CHUNK_SIZE = 900


def _checksum_chunks(checksums: list[str]) -> list[list[str]]:
    """Split checksums into chunks small enough for one SQL ``IN`` clause."""
    return [
        checksums[i : i + _CHECKSUM_CHUNK_SIZE]
        for i in range(0, len(checksums), _CHECKSUM_CHUNK_SIZE)
    ]


def _snippets_by_checksums(session: Session, checksums: list[str]) -> dict[str, Snippet]:
    """Fetch snippets by checksum using chunked ``IN`` queries (no N+1)."""
    result: dict[str, Snippet] = {}
    for chunk in _checksum_chunks(list(checksums)):
        for snippet in session.exec(
            select(Snippet).where(Snippet.checksum.in_(chunk))  # type: ignore[attr-defined]
        ).all():
            result[snippet.checksum] = snippet
    return result


def _string_list_or_none(raw: str | None) -> list[str] | None:
    """Parse a stored JSON string-list column (names/tags).

    Returns ``None`` for anything that is not a JSON array: merge sources
    are untrusted, and one row with corrupt metadata must neither abort the
    whole merge nor poison the destination (a bad ``names`` value would
    crash every later read of the snippet).
    """
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError, RecursionError:
        # ``RecursionError`` is not a ``ValueError``: a deeply nested
        # document (``[[[...]]]``) from a hostile merge source recurses
        # once per level in the decoder and must skip the row, not abort
        # the whole merge — the same guard ``_read_body`` applies.
        return None
    return value if isinstance(value, list) and all(isinstance(i, str) for i in value) else None


def _source_lists_or_none(source_snippet: Snippet) -> tuple[list[str], list[str]] | None:
    """Return a merge source's ``(names, tags)``, or ``None`` if either is corrupt.

    Both columns are copied verbatim into the destination, so unreadable
    metadata is rejected (with the same warning the merge paths emit) before
    it poisons the destination.
    """
    names = _string_list_or_none(source_snippet.names)
    tags = _string_list_or_none(source_snippet.tags)
    if names is None or tags is None:
        logger.warning(
            "Skipping source snippet %s: corrupt names/tags metadata.", source_snippet.checksum
        )
        return None
    return names, tags


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


#: Column order every snippet insert below shares, from the parameterized
#: statement to the SQLite driver-level tuple to the DuckDB literal list.
_SNIPPET_COLUMNS = ("checksum", "names", "code", "minhash", "tags", "collection")
_SNIPPET_COLUMN_LIST = ", ".join(_SNIPPET_COLUMNS)

#: Parameterized template for one snippet row (the executemany path).
_SNIPPET_INSERT_SQL = (
    f"INSERT INTO snippet ({_SNIPPET_COLUMN_LIST}) "
    f"VALUES ({', '.join(':' + c for c in _SNIPPET_COLUMNS)})"
)

#: SQLite driver-level variant of ``_SNIPPET_INSERT_SQL`` (qmark placeholders).
#: Handing the DBAPI cursor the executemany directly skips SQLAlchemy's
#: per-row parameter construction, measured 1.9x faster (397k -> 754k rows/s).
_SNIPPET_INSERT_SQL_SQLITE = (
    f"INSERT INTO snippet ({_SNIPPET_COLUMN_LIST}) "
    f"VALUES ({', '.join('?' * len(_SNIPPET_COLUMNS))})"
)


def _duckdb_sql_literal(value: object) -> str:
    """Render one snippet-column value as a safe DuckDB SQL literal.

    Text quoting is delegated to :func:`resembl.lsh.sql_text_literal`, the
    package's single DuckDB string-escaping invariant.  Bytes use
    ``FROM_HEX``, DuckDB's blob-from-hex function (the ``X'...'`` hex
    literal is not supported).  ``None`` becomes ``NULL``.  This is the
    correctness and injection boundary of the DuckDB multi-VALUES fast
    path: snippet code and names are arbitrary user text, so every value
    must pass through here before being interpolated into SQL.

    The wrapper stays in ``core`` rather than moving into ``lsh`` because it
    only serves the snippet INSERT (``_insert_snippet_rows``); ``lsh``'s
    bucket INSERT quotes its own columns inline via ``sql_text_literal``.
    Keeping the ``None``/``bytes`` handling next to the snippet column list
    avoids a second, divergent literal helper in ``lsh``.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"FROM_HEX('{value.hex()}')"
    return sql_text_literal(value)


def _insert_snippet_rows(
    session: Session, rows: list[dict[str, object]], batch_size: int = 500
) -> None:
    """Insert snippet rows with the dialect's fastest strategy.

    SQLite hands the DBAPI cursor the executemany directly — SQLAlchemy's
    per-row parameter construction cost ~half the write time on a bulk insert
    (measured 1.9x faster: 397k -> 754k rows/s).  DuckDB's executemany path
    is ~7x slower than multi-row ``VALUES`` statements, and the snippet insert
    dominates import throughput there (measured 2,665 vs 19,872 rows/s at 500
    rows/statement); values are rendered through :func:`_duckdb_sql_literal`,
    which is the correctness and injection boundary for that fast path.  Other
    dialects keep the parameterized executemany, which is already
    C-accelerated there.
    """
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        # Explicit ``flush`` preserves the autoflush ``session.execute``
        # would have performed before the insert (alias name merges are
        # pending in ``snippet_add_batch``); ``session.connection()`` does
        # not autoflush.
        session.flush()
        conn = session.connection()
        for i in range(0, len(rows), batch_size):
            conn.exec_driver_sql(
                _SNIPPET_INSERT_SQL_SQLITE,
                [tuple(row[c] for c in _SNIPPET_COLUMNS) for row in rows[i : i + batch_size]],
            )
        return
    if dialect != "duckdb":
        for i in range(0, len(rows), batch_size):
            session.execute(text(_SNIPPET_INSERT_SQL), params=rows[i : i + batch_size])
        return
    for i in range(0, len(rows), batch_size):
        values = ",".join(
            "(" + ", ".join(_duckdb_sql_literal(row[c]) for c in _SNIPPET_COLUMNS) + ")"
            for row in rows[i : i + batch_size]
        )
        # exec_driver_sql, not text(): the generated statement has no bind
        # parameters, and text()'s marker scan cannot tell a literal ``$1``
        # or ``:0`` inside user content from a real bind placeholder — a
        # snippet containing either would otherwise raise StatementError.
        session.connection().exec_driver_sql(
            f"INSERT INTO snippet ({_SNIPPET_COLUMN_LIST}) VALUES {values}"
        )


def snippet_add_batch(
    session: Session,
    prepared_items: list[tuple[str, str, str, bytes]],
    batch_size: int = 500,
    ngram_size: int = 3,
    num_perm: int = NUM_PERMUTATIONS,
    *,
    _add_batch_retry_seen: bool = False,
) -> dict:
    """Insert many prepared snippets in one pass.

    ``prepared_items`` is a list of ``(checksum, name, code, minhash_bytes)``
    tuples as produced by :func:`snippet_prepare`.

    Deduplication is content-addressable: code that already exists in the
    database is not re-inserted; any new names are merged into the existing
    snippet as aliases.  Rows are written in batches with one incremental LSH
    index update at the end, making bulk imports orders of magnitude faster
    than one ``snippet_add`` call per file.

    Returns ``{"added", "aliased", "skipped", "time_elapsed"}``.
    """
    start_time = monotonic_now()

    # Group by checksum: within one batch, identical code is deduplicated and
    # its names are merged.  ``(code, minhash_bytes, names)`` tuples keep the
    # entry strongly typed for the hot loop below.
    by_checksum: dict[str, tuple[str, bytes, list[str]]] = {}
    for checksum, name, code, minhash_bytes in prepared_items:
        # Names reach the database as filenames (NFD on macOS) and as CLI
        # argv, so the alias merge below would otherwise keep both spellings
        # of the same name as two entries.
        name = normalize_unicode(name)
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
            merged = _merge_names(name_list, names)
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
    # Detect an empty database with an O(1) ``LIMIT 1`` probe instead of a
    # full-table COUNT: the stamp reconciliation below needs to know only
    # whether pre-existing (possibly foreign-format) rows exist alongside
    # this batch's new ones, and the COUNT scanned the whole table on every
    # import chunk.
    fresh_database = session.exec(select(Snippet.checksum).limit(1)).first() is None
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
        try:
            _insert_snippet_rows(session, rows, batch_size)
        except IntegrityError:
            # A concurrent batch committed the same checksums between the
            # dedup read above and this bulk insert (two import processes,
            # or a retry racing the original).  The rows now exist, so
            # converge by re-running the dedup pass — the same strategy the
            # index-sync block below already uses for the same race.
            # Bounded to one retry: a persistent IntegrityError (a constraint
            # other than the concurrent-duplicate race) would otherwise
            # recurse forever, and each retry re-reads and re-inserts the
            # same batch.  Like ``db_merge``, the second collision surfaces
            # as an error instead of spinning; ``snippet_add`` already
            # re-raises when the row still does not exist.
            session.rollback()
            if _add_batch_retry_seen:
                raise
            return snippet_add_batch(
                session,
                prepared_items,
                batch_size,
                ngram_size,
                num_perm,
                _add_batch_retry_seen=True,
            )
    if new_snippets or aliased:
        # The index rows join the snippet rows' transaction: a commit that
        # publishes a snippet must publish the buckets that make it findable.
        # Committing the snippets first left a window in which a crash (or a
        # database lock on the sync) stranded them unindexed while lsh_meta
        # still advertised a complete index, and a retried import never
        # repaired that — the re-run saw the checksums already present, took
        # the alias path, and indexed nothing.
        try:
            index_threshold = lsh_index_add_batch(
                session, [(s.checksum, s.minhash) for s in new_snippets], commit=False
            )
            session.commit()
        except IntegrityError:
            # Concurrent batch committed the same checksums first; converge
            # by re-running the dedup pass on the now-current rows.  Same
            # one-retry bound as the insert block above.
            session.rollback()
            if _add_batch_retry_seen:
                raise
            return snippet_add_batch(
                session,
                prepared_items,
                batch_size,
                ngram_size,
                num_perm,
                _add_batch_retry_seen=True,
            )
        if index_threshold is not None:
            lsh_pickle_cache_remove(index_threshold)
        if new_snippets:
            fingerprint_stamps_reconcile(
                session,
                ngram_size=ngram_size,
                num_perm=num_perm,
                fresh_database=fresh_database,
            )

    elapsed = monotonic_now() - start_time
    return {
        "added": len(new_snippets),
        "aliased": aliased,
        "skipped": len(prepared_items) - len(by_checksum),
        "time_elapsed": elapsed,
    }


def snippet_add(
    session: Session, name: str, code: str, ngram_size: int = 3, num_perm: int = NUM_PERMUTATIONS
) -> Snippet | None:
    """Add a new snippet or alias to the database.

    *name* is stored in NFC, so the alias a caller types and the same alias
    arriving from a decomposed filename are one name.
    """
    name = name_normalize(name)
    # Lex once: the checksum is enough to decide the alias path, and only a
    # new snippet needs the fingerprint.  Calling ``string_checksum`` and
    # ``code_create_minhash`` separately lexed the same code twice, doubling
    # the add path's dominant cost.
    lexed = snippet_lex(code)
    if lexed is None:
        return None
    checksum, token_list = lexed
    name = normalize_unicode(name)
    code = normalize_unicode(code)

    existing_snippet = Snippet.get_by_checksum(session, checksum)

    if existing_snippet:
        name_list = existing_snippet.name_list
        merged = _merge_names(name_list, [name] if name else [])
        if len(merged) > len(name_list):
            existing_snippet.names = json.dumps(merged)
            session.add(existing_snippet)
            session.commit()
            session.refresh(existing_snippet)
        return existing_snippet

    minhash_bytes = minhash_pack(minhash_from_tokens(token_list, ngram_size, num_perm=num_perm))

    # Detect an empty database with an O(1) ``LIMIT 1`` probe instead of a
    # full-table COUNT (same reason as ``snippet_add_batch``): the stamp
    # reconciliation below may only publish stamp values that hold for the
    # whole database, so it must know whether pre-existing rows (possibly
    # foreign-format) are present.
    fresh_database = session.exec(select(Snippet.checksum).limit(1)).first() is None

    new_snippet = Snippet(
        checksum=checksum,
        names=json.dumps([name] if name else []),
        code=code,
        minhash=minhash_bytes,
    )
    session.add(new_snippet)
    # Keep the DB-backed LSH index in sync, in the snippet row's transaction:
    # see ``snippet_add_batch`` for why the two must commit together.
    try:
        index_threshold = lsh_index_add(
            session, new_snippet.checksum, new_snippet.minhash, commit=False
        )
        session.commit()
    except IntegrityError:
        # A concurrent add of the same checksum committed first; converge
        # to the alias path instead of failing the retry.
        session.rollback()
        existing = Snippet.get_by_checksum(session, checksum)
        if existing is None:
            raise
        if name:
            merged = _merge_names(existing.name_list, [name])
            if len(merged) > len(existing.name_list):
                existing.names = json.dumps(merged)
                session.add(existing)
                session.commit()
        session.refresh(existing)
        return existing
    if index_threshold is not None:
        lsh_pickle_cache_remove(index_threshold)
    session.refresh(new_snippet)
    fingerprint_stamps_reconcile(
        session,
        ngram_size=ngram_size,
        num_perm=num_perm,
        fresh_database=fresh_database,
    )
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

    This runs on every find, so all three stamps are read in a single round
    trip (:func:`resembl.lsh.fingerprint_stamps_get`).
    """
    stamps = fingerprint_stamps_get(session)
    if stamps.version != FINGERPRINT_VERSION:
        return True
    if stamps.ngram != ngram_size:
        # Stored fingerprints encode their n-gram; a config ngram change
        # silently zeroes matches (measured: 40 candidates at ngram 3, 0 at
        # ngram 5) — reindex once at the new n-gram.
        return True
    if stamps.perm is not None and stamps.perm != num_permutations:
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

    Returns ``(lsh_candidate_count, matches)``.  The count is the number of
    checksums the index returned, which can exceed ``len(matches)``.  A
    non-positive ``top_n`` returns the candidate count with no matches, and
    skips the scoring passes.

    Raises :class:`IndexBuildError` when the fingerprint migration or the
    index build fails, so callers can tell "no matches" from "the search
    could not be answered at all".

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
        reindex_result = db_reindex(
            session,
            ngram_size=ngram_size,
            jobs=adaptive_worker_count(num_snippets, os.cpu_count() or 1),
            num_perm=num_permutations,
            progress=progress,
        )
        if "error" in reindex_result:
            # The migration could not run (e.g. another process holds the
            # index lock).  Proceeding would index the *stale* fingerprints
            # and stamp them as current, silently hiding the pending
            # migration forever; report it like the failed build it is.
            raise IndexBuildError(reindex_result["error"])

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

    # Jaccard is computed directly from the packed fingerprints (no MinHash
    # object construction), vectorized across candidates in one numpy pass
    # over the (N, num_perm) uint32 array — SIMD under the hood.  This is
    # what keeps find fast when a query lands in a crowded band (thousands
    # of candidates at scale).
    #
    # The candidates are walked in bounded chunks (:func:`_
    # find_candidate_jaccards`), so peak memory is a function of the chunk
    # size rather than of the band population: a band holding 100k snippets
    # (routine on a large corpus, and every snippet at a low threshold) used
    # to materialize every candidate's fingerprint, the whole
    # (N, num_perm) vectorized array and the per-candidate Jaccard list at
    # once.  Only the fingerprint columns are read: the ``code`` column
    # dominates the table, and most candidates are pruned before they are
    # ever Levenshtein-scored, so loading full rows for all of them would
    # move megabytes of text through the ORM per query.
    keys, jaccards = _find_candidate_jaccards(
        session, candidate_keys, query_minhash_bytes, num_permutations
    )
    if not keys:
        return 0, []

    # Hybrid score (Jaccard + Levenshtein) with an early exit: since
    # ``hybrid = 100 * w * jaccard + (1 - w) * levenshtein`` and
    # levenshtein <= 100, a candidate whose upper bound (the same expression
    # with levenshtein = 100, computed through ``score_hybrid``) is strictly
    # below the current n-th best hybrid can never enter the top-n: it skips the
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


#: Candidates scored per round trip while Jaccard-scoring a find.  The
#: fingerprints of a whole band do not fit in memory on a crowded query (a
#: low threshold admits every snippet, and each stored fingerprint is ~520
#: bytes, which the vectorized pass doubles into its own array), so the
#: candidates are walked in chunks and only their Jaccard survives the round:
#: the ranking pass needs the scores, not the blobs.  Large enough that the
#: numpy pass still runs wide (SIMD pays off well past a few hundred rows),
#: small enough that peak memory is a function of this constant rather than
#: of the corpus.
_FIND_CANDIDATE_CHUNK = 20_000


def _find_candidate_jaccards(
    session: Session,
    candidate_keys: Sequence[str],
    query_packed: bytes,
    num_permutations: int,
) -> tuple[list[str], list[float]]:
    """Jaccard the query against every candidate, one bounded chunk at a time.

    Returns ``(keys, jaccards)``, positionally aligned and in the order
    *candidate_keys* arrived, so the caller's index-based tie-breaks are
    unchanged by the chunking.

    Chunks exist to bound memory, not to prune: the fingerprints of a chunk
    are fetched, scored and released before the next one is read, so a find
    holds one chunk of blobs (plus the same number of rows in the vectorized
    array) rather than a multiple of the band population.  Candidates that
    cannot be scored are dropped here, exactly as the ranking pass drops
    rows that vanished, so the two halves agree on which candidates exist.
    """
    keys: list[str] = []
    jaccards: list[float] = []
    for start in range(0, len(candidate_keys), _FIND_CANDIDATE_CHUNK):
        chunk = list(candidate_keys[start : start + _FIND_CANDIDATE_CHUNK])
        minhashes = _snippet_minhashes_by_checksums(session, chunk)
        blobs, chunk_keys = _candidate_blobs_scorable(chunk, minhashes, num_permutations)
        if not chunk_keys:
            continue
        keys.extend(chunk_keys)
        jaccards.extend(minhash_jaccard_batch(query_packed, blobs))
    return keys, jaccards


def _candidate_blobs_scorable(
    keys: list[str], minhashes: dict[str, bytes], num_permutations: int
) -> tuple[list[bytes], list[str]]:
    """Return the packed blobs of the candidates this query can score.

    Each blob is normalized (legacy pickles -> packed), and unusable ones are
    dropped with a warning: a single rotten fingerprint must not crash the
    query (a reindex heals it from its code), and a blob written at a
    different permutation count is stale for this query by the same token —
    scoring it would raise inside the batch Jaccard.  A candidate whose row
    has disappeared (deleted between the index query and this fetch) is
    dropped too, the same race the later full-row pass tolerates.

    ``minhash_ensure_packed`` has already validated the header, and the
    compact format's length is exactly ``8 + 4 * num_perm`` — so the length
    alone reports the permutation count.  Parsing the header a second time
    per candidate cost ~10% of a crowded find (tens of thousands of
    candidates); this is the same rejection, one ``len`` compare.
    """
    expected_len = 8 + 4 * num_permutations
    blobs: list[bytes] = []
    valid_keys: list[str] = []
    for key in keys:
        blob_or_none = minhashes.get(key)
        if blob_or_none is None:
            continue
        try:
            blob = minhash_ensure_packed(blob_or_none)
            if len(blob) != expected_len:
                logger.warning("Skipping candidate %s: stale fingerprint permutation count.", key)
                continue
        except ValueError:
            logger.warning("Skipping candidate %s: corrupt fingerprint.", key)
            continue
        blobs.append(blob)
        valid_keys.append(key)
    return blobs, valid_keys


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

    # The snippet row and its ``lsh_bucket`` rows go in one transaction.
    # Committing the snippet first and purging the index in a second one left
    # bucket rows for a checksum that no longer existed whenever the process
    # died in between, and ``lsh_meta`` still marked the index complete, so
    # no later find would repair it.  The index metadata is read before the
    # writes because an unbuilt index has nothing to purge.
    purge_threshold = lsh_index_purge(session, checksum)
    # Version history dies with its snippet (the foreign key without a
    # database-level cascade — DuckDB's FK parser rejects ON DELETE
    # actions — so the cleanup is explicit and in the same transaction).
    session.execute(
        delete(SnippetVersion).where(SnippetVersion.snippet_checksum == checksum)  # type: ignore[arg-type]
    )
    session.delete(snippet)
    session.commit()
    if not quiet:
        logger.info("Snippet with checksum %s deleted.", checksum)

    # The legacy pickle cache is only dropped once the purge is durable.
    if purge_threshold is not None:
        lsh_pickle_cache_remove(purge_threshold)
    return True


def _snippet_primary_name(snippet: Snippet) -> str:
    """Return a snippet's first alias, or a checksum-derived placeholder."""
    return snippet.name_list[0] if snippet.name_list else f"snippet_{snippet.checksum[:16]}"


def _yara_string_escape(text: str) -> str:
    """Escape *text* for embedding in a quoted YARA string literal.

    User-controlled names and code are embedded in generated rules, so a
    name cannot be allowed to break out of the string literal (or the rule).
    Control characters are escaped as ``\\xNN`` rather than written through:
    YARA reads a string literal as a C string, so a literal NUL truncates the
    pattern where it lands and the rule then matches something the snippet
    never contained, while the other C0 controls (ESC, BEL, ...) survive into
    the rule file as raw bytes.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    parts: list[str] = []
    for ch in escaped:
        code = ord(ch)
        if 0x20 <= code and not 0x7F <= code <= 0x9F and not 0xD800 <= code <= 0xDFFF:
            parts.append(ch)
        elif code < 0x100:
            # YARA ``\xNN`` takes exactly two hex digits: ``f"\x{ord:02x}"``
            # on a surrogate (e.g. U+DCFF) would parse as ``\xdc`` plus the
            # literal ``ff``, corrupting the pattern.
            parts.append(f"\\x{code:02x}")
        else:
            # No two-digit escape names a code point above U+00FF; write
            # its UTF-8 bytes instead.  A lone surrogate has no UTF-8
            # encoding, so it becomes ``?``.
            try:
                parts.extend(f"\\x{byte:02x}" for byte in ch.encode("utf-8"))
            except UnicodeEncodeError:
                parts.append("?")
    return "".join(parts)


def snippet_export_yara(session: Session, output_file: str) -> dict:
    """Export snippets as YARA string matching rules."""
    import tempfile

    start_time = monotonic_now()
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
        # ``newline="\n"`` for the same reason as the .asm export: the
        # generated rule file must be byte-identical on every platform.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for snippet in Snippet.stream_all(session):
                primary_name = _snippet_primary_name(snippet)
                rule_name = _RULE_NAME_INVALID_CHARS.sub("_", primary_name)
                # A name may be empty (e.g. added with an empty alias) — index
                # would crash; prefix instead, as for names starting with a digit.
                # ASCII-only: a YARA identifier must start with an ASCII
                # letter or underscore, and ``rule_name`` is already
                # ASCII-only after the substitution above — a non-ASCII
                # first letter arrives here as "_" in any case, so the
                # isalpha that follows only ever reads ASCII anyway. Keep
                # the ASCII gate explicit so a future substitution that
                # stops collapsing non-ASCII cannot admit one through.
                first = rule_name[:1]
                if not rule_name or (
                    not ("a" <= first <= "z" or "A" <= first <= "Z") and first != "_"
                ):
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
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, output_file)
    except BaseException:
        # The half-written temp must not outlive a failed export.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    end_time = monotonic_now()
    time_elapsed = end_time - start_time

    return {
        "num_exported": num_exported,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": avg_time_per_item(time_elapsed, num_exported),
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

    With ``jobs > 1`` *and* more than ``batch_size`` snippets in the
    database, the CPU-bound tokenization runs in a process pool (bounded
    in-flight batches), turning a long sequential reindex into a parallel
    one.  Below that the pool's spawn cost exceeds the work.

    The fingerprints are invalidated *before* the update: any built index is
    cleared up front, so a crash mid-reindex can never leave a stale index
    behind (the next ``find`` simply rebuilds it from whatever fingerprints
    are stored).  On SQLite the writes are committed periodically so the WAL
    stays bounded — a single transaction spanning the whole reindex would
    grow the WAL to the size of the database and force one huge checkpoint
    at commit.  Every other backend keeps a single final commit (PostgreSQL
    segments its own WAL and pays an fsync per commit), so only SQLite
    commits periodically.  If *progress* is given it is called as
    ``progress(done, total)`` with snippets processed so far.

    The whole run holds the database's index-rebuild lock: a reindex clears
    the LSH index up front and rewrites every fingerprint, so two of them at
    once (the ``serve`` process runs one handler thread per request) would
    interleave their clears and leave ``lsh_meta`` advertising a complete
    index over half-written rows.  See :func:`resembl.lsh.index_build_lock`.
    """
    with index_build_lock(session):
        return _db_reindex(session, ngram_size, batch_size, jobs, num_perm, progress)


def _db_reindex(
    session: Session,
    ngram_size: int,
    batch_size: int,
    jobs: int,
    num_perm: int,
    progress: Callable[[int, int], None] | None,
) -> dict:
    """Run one reindex under the caller's index-rebuild lock."""
    import multiprocessing as _mp
    from collections import deque
    from concurrent.futures import Future, ProcessPoolExecutor

    start_time = monotonic_now()
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
        for snippet, blob in zip(batch, blobs, strict=True):
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
                _retry_sleep(_REINDEX_CLEAR_RETRY_BACKOFF * (attempt + 1))
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

    def reindex_sequentially() -> None:
        """Recompute and apply every batch on this thread, in keyset order."""
        for batch in Snippet.iter_batches(session, batch_size):
            codes = [snippet.code for snippet in batch]
            apply_batch(batch, _reindex_prepare((codes, ngram_size, num_perm)))

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
            # Abort the transaction a failed flush/commit left behind, or
            # every statement of the sequential retry dies with
            # PendingRollbackError instead of the original work redoing.
            session.rollback()
            reindexed = 0
            batches_since_commit = 0
            reindex_sequentially()
    else:
        reindex_sequentially()
    session.commit()
    # Fingerprints are now current — stamp the format version, n-gram size,
    # and permutation count so `find` does not reindex again.
    fingerprint_version_set(session, FINGERPRINT_VERSION)
    fingerprint_ngram_set(session, ngram_size)
    fingerprint_perm_set(session, num_perm)

    end_time = monotonic_now()
    time_elapsed = end_time - start_time

    return {
        "num_reindexed": reindexed,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": avg_time_per_item(time_elapsed, num_snippets),
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


_SNIPPET_LIST_DEFAULT_LIMIT = 1000

#: Sentinel for ``snippet_list``'s ``limit``: distinguishes "not given"
#: (apply :data:`_SNIPPET_LIST_DEFAULT_LIMIT`) from an explicit
#: ``limit=None`` (unbounded, historical behavior).
_SNIPPET_LIST_LIMIT_UNSET = object()


def snippet_list(
    session: Session,
    start: int = 0,
    end: int | None = None,
    limit: int | None = _SNIPPET_LIST_LIMIT_UNSET,  # type: ignore[assignment]
) -> list[Snippet]:
    """List snippets, optionally within a given half-open ``[start, end)`` range.

    *end* of ``None`` lists from *start* to the end, capped by *limit*.
    When neither *end* nor *limit* is given, *limit* defaults to
    :data:`_SNIPPET_LIST_DEFAULT_LIMIT` so an unbounded call cannot
    materialize the whole corpus (``code`` column included) in memory.
    Pass ``limit=None`` explicitly with ``end=None`` only when a full
    unbounded list is intended; prefer :meth:`Snippet.stream_all` for
    bounded-memory full scans.

    Ordered by the checksum primary key, so a paged window selects the same
    rows on every run: an unordered ``OFFSET``/``LIMIT`` over the same
    database can return a different page once the plan changes.

    The unbounded listing reads only ``(checksum, names)`` via keyset
    pagination and reconstructs lightweight ``Snippet`` objects, instead of
    loading every full row (the ``code`` column dominates the table).
    Rendered listings only need the checksum and names; callers that need
    the code still use :meth:`Snippet.get_all` or :meth:`Snippet.stream_all`.
    """
    if start < 0:
        # A window starting before the first row selects no rows: SQLite
        # reads ``OFFSET -1`` as 0, so without this guard the window
        # answered with rows from the start instead of none.
        return []
    if end is not None:
        # ``end < start`` is an empty window, not a negative LIMIT: SQLite
        # reads a negative LIMIT as "unbounded" and would return every row
        # from the offset, while PostgreSQL rejects it outright.
        limit = max(0, end - start)
        if limit == 0:
            return []
        # Windowed reads keep their full-row fetch: the window is bounded
        # and callers may need the code column.
        return list(
            session.exec(
                select(Snippet).order_by(Snippet.checksum).offset(start).limit(limit)
            ).all()
        )
    if limit is _SNIPPET_LIST_LIMIT_UNSET:
        limit = _SNIPPET_LIST_DEFAULT_LIMIT
    if limit is not None and limit <= 0:
        return []
    if limit is not None:
        rows = session.exec(
            select(Snippet.checksum, Snippet.names)
            .order_by(Snippet.checksum)
            .offset(start)
            .limit(limit)
        ).all()
        return [
            Snippet(checksum=checksum, names=names, code="", minhash=b"")
            for checksum, names in rows
        ]
    # Unbounded (limit=None): stream (checksum, names) in keyset batches so
    # memory stays flat and the code column never enters the ORM.
    # ``Snippet`` objects are reconstructed with empty code/minhash — the
    # listing only renders checksum and names (``name_list`` parses ``names``).
    result: list[Snippet] = []
    last: str | None = None
    skipped = 0
    page_size = 1000
    while True:
        stmt = select(Snippet.checksum, Snippet.names).order_by(Snippet.checksum).limit(page_size)
        if last is not None:
            stmt = stmt.where(Snippet.checksum > last)
        rows = session.exec(stmt).all()
        if not rows:
            break
        for checksum, names in rows:
            if skipped < start:
                skipped += 1
                continue
            result.append(Snippet(checksum=checksum, names=names, code="", minhash=b""))
        last = rows[-1][0]
    return result


def _names_stream(
    session: Session, collection_name: str | None, batch_size: int
) -> Iterator[list[tuple[str, str]]]:
    """Yield ``(checksum, names)`` pairs in batches via keyset pagination.

    Shared implementation of :func:`snippet_names_stream` and
    :func:`snippet_collection_names_stream`; only the two rendered columns
    are read so the ``code`` column never enters memory.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    last: str | None = None
    while True:
        stmt = select(Snippet.checksum, Snippet.names).order_by(Snippet.checksum).limit(batch_size)
        if collection_name is not None:
            stmt = stmt.where(Snippet.collection == collection_name)
        if last is not None:
            stmt = stmt.where(Snippet.checksum > last)
        rows = session.exec(stmt).all()
        if not rows:
            return
        yield [(row[0], row[1]) for row in rows]
        last = rows[-1][0]


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
    return _names_stream(session, None, batch_size)


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
    return _names_stream(session, name_normalize(collection_name), batch_size)


def snippet_search_by_name(session: Session, pattern: str, limit: int = 50) -> list[Snippet]:
    """Search for snippets where any name matches the pattern (case-insensitive).

    Names are embedded in a JSON column, so no index can answer a
    substring search over them; the search scans the (checksum, names)
    columns in checksum order and verifies each decoded name in Python.
    An ``ILIKE`` probe was tried for candidate narrowing, but it cannot
    be sound here: the column spells a non-ASCII character as its
    ``\\uXXXX`` escape, and case changes the code point — ``CAFÉ``
    escapes to ``\\u00c9`` while ``café`` stores ``\\u00e9`` — so no
    one probe admits both spellings, and an ASCII pattern that
    casefold-matches a non-ASCII name (``strasse`` for ``straße``, whose
    casefold is ``strasse``) is spelled literally in the column nowhere.
    The ``%pattern%`` form is a full scan on every backend anyway, so
    narrowing bought nothing and cost the matches above.

    The match itself is caseless: both sides are :meth:`str.casefold`
    before the substring test (not ``lower``, which leaves ``ß`` as
    ``ß``).  *pattern* is normalized to NFC first, so a pattern macOS or
    a copy-paste hands over in NFD (``cafe`` plus a combining acute)
    finds the composed name stored in the database.
    *limit* bounds the result, applied after verification so rejected
    rows cannot consume the page.  The page is ordered by the checksum
    primary key, so two runs over the same database return the same
    rows in the same order.

    Only the two decision columns are fetched (``code`` dominates the
    table); the verified survivors' full rows are fetched afterwards,
    so a broad pattern does not drag every match's code through the
    ORM before verification.
    """
    if limit <= 0:
        # ``checksums[:-1]`` would silently drop only the last match, so a
        # non-positive limit must bound the result to zero rows.
        return []
    target = name_normalize(pattern)
    folded_target = target.casefold()
    # Verification is case-insensitive, as the probe is: candidate
    # generation and this check must agree on what "matches" means, or an
    # upper-case pattern finds no row its own probe admitted.  casefold
    # (not lower) is the caseless form: it maps 'ß' to 'ss' and folds the
    # final sigma, so 'STRASSE' finds the row stored as 'straße'.
    # The pattern is already NFC.  ASCII is NFC as well, so composing an
    # ASCII alias again cannot change whether the pattern occurs in it.
    # A non-ASCII alias may still be the decomposed spelling (a row from
    # before names were composed, or from macOS), and a substring of that
    # raw spelling is not a substring of the composed form.
    # Stream in keyset batches (as _names_stream does) and stop as soon as
    # ``limit`` verified matches are found: the old ``.all()`` materialized
    # every (checksum, names) row, decoded every name and built the full
    # match list before slicing to ``limit`` — O(corpus) memory and CPU for
    # a page that needs the first ``limit`` hits.
    checksums: list[str] = []
    last: str | None = None
    while len(checksums) < limit:
        stmt = select(Snippet.checksum, Snippet.names).order_by(Snippet.checksum).limit(2000)
        if last is not None:
            stmt = stmt.where(Snippet.checksum > last)
        rows = session.exec(stmt).all()
        if not rows:
            break
        for checksum, names in rows:
            if any(
                folded_target in (stored if stored.isascii() else name_normalize(stored)).casefold()
                for stored in json.loads(names)
            ):
                checksums.append(checksum)
                if len(checksums) >= limit:
                    break
        last = rows[-1][0]
    if not checksums:
        return []
    snippets_by_checksum = _snippets_by_checksums(session, checksums)
    return [snippets_by_checksum[c] for c in checksums if c in snippets_by_checksum]


#: Characters invalid in filenames on at least one major filesystem:
#: Windows forbids ``< > : " / \ | ? *`` plus control characters, and the
#: others break round-trips between platforms.
#:
#: The surrogate range is here for a reason POSIX needs: a filename is a byte
#: string there, and ``os.fsdecode`` surfaces an undecodable byte as a lone
#: surrogate.  Importing a directory whose entries carry such a byte produced
#: the name ``"bad\udcff"``, and encoding that to UTF-8 for the length check
#: below raised ``UnicodeEncodeError`` and aborted the whole export.  A lone
#: surrogate has no UTF-8 encoding at all, so it can never be part of a
#: portable filename: substituting it with the same ``_`` as every other
#: illegal character is the only answer that keeps the export running.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\ud800-\udfff]')

#: Compiled once at module load; the YARA export loop previously called
#: ``re.sub`` per snippet, paying a cache lookup + compile check per row.
_RULE_NAME_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_]")

#: Windows reserves these stems (case-insensitive, extension ignored):
#: writing "con.asm" targets the console device instead of a file.
_WINDOWS_RESERVED_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)
_WINDOWS_RESERVED_STEMS_CASEFOLDED = frozenset(
    ascii_fold_lower(stem) for stem in _WINDOWS_RESERVED_STEMS
)

#: Maximum encoded length of an exported filename stem, in bytes.  Name
#: limits are per-*byte* on POSIX filesystems (ext4: 255), and the ``.asm``
#: suffix plus the checksum disambiguator add up to 17 more — so the stem is
#: truncated here to stay inside every supported filesystem (and a Windows
#: MAX_PATH budget) regardless of how long the snippet's name is.
#: The temp-file suffix (``.pid-thread.tmp``) adds up to 32 more bytes, so
#: the budget leaves room for that too when the stem is at its maximum.
_EXPORT_STEM_MAX_BYTES = 200

#: Manifest a previous :func:`snippet_export` run left in the export
#: directory: the file names it wrote.  Only names listed here are ever
#: removed on a later run, so a user's own files in the same directory are
#: never touched, and the manifest is replaced wholesale every run (it is
#: bounded by one entry per exported snippet, not a growing log).
_EXPORT_MANIFEST_NAME = ".resembl-export.json"


def _grapheme_clusters(text: str) -> Iterator[str]:
    """Yield *text* split into grapheme clusters (UAX #29 approximation).

    No third-party segmentation is available, so this covers the clusters
    that break on a byte cut: combining marks (categories Mn, Me, Mc),
    variation selectors, emoji skin-tone modifiers, ZWJ-joined sequences,
    and regional-indicator pairs (flags). Used only to keep the
    byte-truncation on a user-perceived character boundary.
    """
    cluster: list[str] = []
    ri_run = 0
    prev_zwj = False
    for ch in text:
        if not cluster:
            cluster.append(ch)
            ri_run = 1 if 0x1F1E6 <= ord(ch) <= 0x1F1FF else 0
            prev_zwj = ch == "\u200d"
            continue
        code = ord(ch)
        is_ri = 0x1F1E6 <= code <= 0x1F1FF
        extend = (
            prev_zwj
            or ch == "\u200d"
            or unicodedata.category(ch) in ("Mn", "Me", "Mc")
            or 0xFE00 <= code <= 0xFE0F
            or 0xE0100 <= code <= 0xE01EF
            or 0x1F3FB <= code <= 0x1F3FF
            or (is_ri and ri_run % 2 == 1)
        )
        if extend:
            cluster.append(ch)
            if is_ri:
                ri_run += 1
            prev_zwj = ch == "\u200d"
        else:
            yield "".join(cluster)
            cluster = [ch]
            ri_run = 1 if is_ri else 0
            prev_zwj = ch == "\u200d"
    if cluster:
        yield "".join(cluster)


def _truncate_utf8_graphemes(text: str, max_bytes: int) -> str:
    """Return *text* truncated to *max_bytes* UTF-8 bytes, on a grapheme boundary."""
    budget = max_bytes
    out: list[str] = []
    for cluster in _grapheme_clusters(text):
        size = len(cluster.encode("utf-8"))
        if size > budget:
            break
        out.append(cluster)
        budget -= size
    return "".join(out)


def _export_safe_filename(name: str) -> str:
    """Sanitize a snippet name into a portable filename stem.

    Replaces characters that are illegal on Windows (and problematic
    elsewhere, including lone surrogates from an undecodable filename byte),
    blocks directory traversal, bounds the UTF-8 encoded length (filesystems
    cap filenames at 255 *bytes*; Windows MAX_PATH much lower), strips trailing
    dots/spaces (Windows silently drops them), and prefixes Windows reserved
    device names so ``con`` cannot target a device.
    """
    cleaned = _INVALID_FILENAME_CHARS.sub("_", name.replace("..", "_"))
    cleaned = os.path.basename(cleaned)
    cleaned = unicodedata.normalize("NFC", cleaned)
    if len(cleaned.encode("utf-8")) > _EXPORT_STEM_MAX_BYTES:
        # Truncate by whole grapheme clusters so the cut never splits a
        # multi-code-point sequence: NFC first composes e + combining acute
        # into one code point, but sequences NFC does not compose (emoji
        # ZWJ chains, regional-indicator flag pairs, variation selectors,
        # emoji modifiers, combining marks that stay decomposed) are still
        # several code points.  Cutting raw bytes drops a partial code
        # point but leaves a dangling ZWJ or a lone regional indicator,
        # which renders as a broken glyph.  Walking code points and only
        # keeping whole clusters keeps the stem valid and on a grapheme
        # boundary; a trailing cluster that does not fit in the byte
        # budget is dropped whole, not split.
        cleaned = _truncate_utf8_graphemes(cleaned, _EXPORT_STEM_MAX_BYTES)
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        return ""
    # ASCII-only fold: the reserved stems are ASCII, and ``casefold`` maps
    # the long s to s, so a spelling with the long s would need to match
    # here — reserved device names are ASCII protocol tokens, not names.
    if ascii_fold_lower(cleaned.split(".", 1)[0]) in _WINDOWS_RESERVED_STEMS_CASEFOLDED:
        cleaned = f"_{cleaned}"
    return cleaned


def _export_manifest_read(export_dir: str) -> set[str]:
    """Return the file names a previous export wrote, or an empty set.

    A missing, unreadable, or malformed manifest yields no names, which
    disables pruning for that run: the export still writes every file, and a
    damaged manifest can never turn into an unbounded delete.
    """
    try:
        with open(os.path.join(export_dir, _EXPORT_MANIFEST_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except OSError, ValueError, RecursionError:
        # RecursionError is not a ValueError: a deeply nested manifest
        # from a hostile export directory recurses once per level in
        # the decoder and must disable pruning, not abort the export —
        # the same guard _string_list_or_none applies to stored JSON.
        return set()
    if not isinstance(data, list):
        return set()
    return {name for name in data if isinstance(name, str)}


def _export_stale_remove(export_dir: str, previous: set[str], written: set[str]) -> int:
    """Delete the files a previous export wrote that this run did not, and count them.

    Only names the previous manifest recorded are considered, only ones this
    run did not rewrite, and only plain file names: a name carrying a
    separator or a ``..`` component, or one that is not a file anymore, is
    skipped rather than resolved, so a hand-edited or hostile manifest cannot
    reach outside the export directory.
    """
    removed = 0
    for name in sorted(previous - written):
        if name in {_EXPORT_MANIFEST_NAME, "", ".", ".."} or os.path.basename(name) != name:
            continue
        path = os.path.join(export_dir, name)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            logger.warning("Could not remove stale export file %s: %s", path, e)
    return removed


def _export_manifest_write(export_dir: str, written: set[str]) -> bool:
    """Record *written* as the file list the next export run may prune.

    Published through a same-directory temp and one rename (the pattern
    :func:`snippet_export_yara` uses for its rules), so a crash or a
    concurrent reader never sees a half-written manifest.  Returns whether
    the manifest landed: the files are on disk either way, but a missing
    manifest disables stale-file pruning on the next run, which the caller
    reports instead of silently accepting a non-convergent directory.
    """
    # mkstemp (O_EXCL, random suffix): predictable name let a planter
    # symlink the temp at a victim file, truncating it on write.
    fd, tmp_path = tempfile.mkstemp(
        dir=export_dir, prefix=f".{_EXPORT_MANIFEST_NAME}.", suffix=".tmp"
    )
    try:
        _fchmod_quiet(fd, 0o600, tmp_path)
        with open(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(sorted(written), f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, os.path.join(export_dir, _EXPORT_MANIFEST_NAME))
        _fsync_quiet(export_dir)
    except OSError as e:
        logger.warning("Could not write the export manifest: %s", e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False
    return True


def snippet_export(session: Session, export_dir: str) -> dict:
    """Export all snippets to a directory.

    Convergent on a re-run: the directory ends up holding exactly the files
    the database produces.  Files a previous run wrote for snippets that have
    since been renamed or deleted are removed (see :func:`_export_stale_remove`
    for the manifest that records them), so an exported directory is a valid
    input to a later ``import`` rather than one that re-adds retired
    snippets.  Files the export did not write are never touched.
    """
    start_time = monotonic_now()
    num_exported = 0

    os.makedirs(export_dir, exist_ok=True)
    # Read before the writes: the manifest names what the *previous* run
    # produced, which is what this run may prune.
    previous_exports = _export_manifest_read(export_dir)

    abs_export_dir = os.path.realpath(export_dir)
    # Keys are case-folded so names differing only by case cannot silently
    # overwrite each other on a case-insensitive filesystem.  Folding is done
    # here rather than by os.path.normcase(), which only folds on Windows:
    # macOS' default volume is case-insensitive too, so there the guard did
    # nothing and the second snippet's file landed on the first one.
    used_paths: set[str] = set()
    # Plain file names, for the manifest (the keys above are folded paths,
    # which would record two spellings of one file on a case-folding
    # filesystem).
    written: set[str] = set()

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
        # ASCII-only fold for the same reason as the reserved-stem check:
        # a filesystem compares bytes, and ``casefold`` would glue two
        # names a filesystem keeps distinct.
        used_key = ascii_fold_lower(file_path)
        if used_key in used_paths:
            # 12 hex chars (48 bits) keeps the disambiguator collision-free
            # even with hundreds of thousands of same-named snippets (the
            # previous 8 chars collided at ~30 pairs per 500k).
            file_path = os.path.join(abs_export_dir, f"{safe_name}-{snippet.checksum[:12]}.asm")
            used_key = ascii_fold_lower(file_path)
        used_paths.add(used_key)
        written.add(os.path.basename(file_path))

        # ``newline="\n"``: the default text-mode translate rewrites every
        # "\n" to ``os.linesep``, so the same database would export CRLF on
        # Windows and LF elsewhere, and the two exports would diff against
        # each other.  Assembly sources are LF by convention and re-import
        # identically either way.  Written to a sibling temp and renamed
        # (same pattern as ``db_backup``): ``open(file_path, "w")`` truncates
        # the live export first, so a crash mid-export leaves a truncated
        # file that looks like a complete export artifact.
        # mkstemp (O_EXCL, random suffix): predictable pid/thread name
        # let a planter symlink the temp at a victim file.
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=abs_export_dir, prefix=".resembl-export-", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(snippet.code)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, file_path)
            # Directory fsync is deferred to once after the loop (below):
            # one fsync of the directory entry per file costs O(n) durable
            # flushes for a single directory, dominating the per-file
            # fsync at scale.  A single ``_fsync_quiet`` after the loop makes
            # every rename in this export durable with one flush, the same
            # contract as batching the group-commit elsewhere.
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        num_exported += 1

    # One durable flush for every directory entry this export created.
    _fsync_quiet(abs_export_dir)

    # Every file is on disk: drop what a previous run wrote for snippets the
    # database no longer holds, then record this run's files for the next one.
    # Both steps are bookkeeping over files this export owns, and neither can
    # fail the export (a failed write inside the loop already raised).
    num_removed = _export_stale_remove(export_dir, previous_exports, written)
    manifest_written = _export_manifest_write(export_dir, written)

    end_time = monotonic_now()
    time_elapsed = end_time - start_time

    result = {
        "num_exported": num_exported,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": avg_time_per_item(time_elapsed, num_exported),
        "manifest_written": manifest_written,
    }
    if num_removed:
        result["num_removed"] = num_removed
    return result


def _usable_band_count(threshold: float, num_perm: int) -> int | None:
    """Return the band count *threshold* / *num_perm* bands into, else None.

    None when :class:`~resembl.lsh.ResemblLSH` would refuse those parameters
    (a threshold outside ``[0.0, 1.0]``, fewer than two permutations, or a
    banding left with a single band), so a caller reading stored LSH metadata
    can tell "this index is unusable" from "this index has N bands" without
    running a numerical search on values no index can be built from.
    """
    if not 0.0 <= threshold <= 1.0 or num_perm < 2:
        return None
    bands, _rows = banding_params(threshold, num_perm)
    return bands if bands >= 2 else None


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
        b = _usable_band_count(threshold, num_perm)
        if b is None:
            # The metadata names parameters no index can be built from (a
            # hand-edited or corrupt row: a permutation count below 2, a
            # threshold outside [0.0, 1.0], or a banding that collapses to a
            # single band).  ``banding_params`` is not a safe way to find that
            # out — a zero permutation count raises numpy's "need at least
            # one array to concatenate" — and no bucket count derived from it
            # would mean anything, so report the row instead of the rows.
            issues.append(
                f"stored LSH parameters (threshold {threshold}, {num_perm} permutations) "
                f"have no usable banding — run `resembl reindex --force`"
            )
        else:
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
    """Drop the LSH index and vacuum the database.

    Legacy pickle cache files are not touched here; they are removed by the
    next index write (:func:`resembl.cache.lsh_pickle_cache_remove`), not by
    ``clean``.
    """
    start_time = monotonic_now()

    vacuum_success = False
    # The whole operation is destructive to the index, so it runs under the
    # same per-database lock a rebuild takes (:func:`resembl.lsh.index_build_lock`).
    # Without it a `clean` landing between a rebuild's clear and its final
    # stamp deletes rows the rebuild already inserted: the rebuild then stamps
    # `lsh_meta` as complete over an index that is missing them, and no later
    # find ever repairs it.  The vacuum belongs inside the lock for the same
    # reason — it rewrites the whole file the rebuild is writing into.
    with index_build_lock(session):
        # 1. Wipe the DB-backed index (bucket rows and the metadata row).
        lsh_index_clear(session)

        # 2. Vacuum the database to reclaim space (SQLite only).
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("VACUUM"))
            session.commit()
            vacuum_success = True

    end_time = monotonic_now()
    time_elapsed = end_time - start_time

    return {
        "time_elapsed": time_elapsed,
        "vacuum_success": vacuum_success,
    }


def db_backup_for_delete(
    session: Session, backup_dir: str = ".resembl-pre-delete-backups", *, keep: int = 7
) -> dict:
    """Take a verified backup before a destructive operation.

    Returns the verified result; raises ValueError/OSError when the backup
    cannot be written or verified, so callers must not proceed with the
    deletion. The default directory lives beside the working directory,
    not inside the live database file, and is a single restore point.
    """
    result = db_backup(session, backup_dir, keep=keep)
    if result.get("verified") is not True or result.get("manifest_written") is not True:
        raise ValueError(
            f"pre-delete backup did not verify: {result.get('backup')!r} "
            f"{'; '.join(result.get('verify_issues') or [])}".strip()
        )
    return result


def db_backup(session: Session, backup_dir: str, *, keep: int = 7) -> dict:
    """Write a consistent, restorable copy of the database into *backup_dir*.

    SQLite-only: the copy is made through SQLite's online backup API
    (:func:`sqlite3.Connection.backup`), which checkpoints the WAL and
    produces a single-file database the current binary can open — unlike a
    raw file copy of ``assembly.db`` taken mid-write, which pairs the main
    file with a stale or missing ``-wal`` sidecar and can restore as an
    empty or corrupt database.  The destination must not be the live file
    nor its ``-wal``/``-shm`` sidecars: backing up over the source would
    destroy the data the backup exists to protect.

    The backup is published by writing to a temporary file in the same
    directory and renaming it into place, exactly like
    :func:`snippet_export_yara`, so a failed backup never leaves a truncated
    file that looks complete.  The published backup is immediately
    re-verified with :func:`db_verify_backup` and removed (with
    ``manifest_written: False``) if verification fails — an
    unverifiable copy that a later drill finds by name order would be
    trusted as a restore point.  A per-backup manifest (see
    :func:`_db_backup_manifest_name`) records the snippet count and
    fingerprint version of the source at backup time; the manifest is the
    contract a restore is verified against (:func:`db_verify_backup`).
    It also copies ``config.toml`` and the ``RESEMBL_RUNTIME_MANIFEST``
    file (when set) verbatim beside the backup, modes ``0600``, with
    SHA-256 hashes recorded in the manifest (``config_sha256`` /
    ``runtime_sha256``; ``null`` when that file did not exist).
    Non-SQLite backends have no offline copy here —
    their own backup tooling (``pg_dump``, ``mysqldump``) owns that — so this
    raises :class:`ValueError` naming the backend.

    ``keep`` counts backup files (not manifests): the oldest beyond *keep*
    are removed after the new one is durably in place, so a pruning failure
    can never cost the backup just taken.  A ``keep`` below 1 raises
    :class:`ValueError`: zero would delete the backup this call just wrote.
    Pruning never deletes the backup this call just wrote, even when the
    directory already holds more than *keep* files: the new backup is
    marked ``preserve`` across the prune, so an oversized directory
    cannot prune the newest restore point out from under the caller.

    Re-execution is convergent: when the newest retained backup already
    holds this database's current content (see :func:`_db_content_hash`),
    the existing backup is returned and nothing is written or pruned, so a
    retried run — a double-scheduled cron job, a script re-run, a caller
    that lost the first response — cannot consume a second retention
    slot or delete an older restorable copy.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")
    bind = session.get_bind()
    # ``get_bind`` on a Session always returns an Engine in this codebase;
    # mypy models it as ``Engine | Connection``, so narrow by attribute.
    if not hasattr(bind, "dialect") or bind.dialect.name != "sqlite":
        backend = getattr(getattr(bind, "dialect", None), "name", "unknown")
        raise ValueError(
            f"db_backup supports SQLite only (backend is {backend!r}); "
            "use the backend's own backup tooling"
        )
    import sqlite3
    import tempfile

    os.makedirs(backup_dir, exist_ok=True)
    backup_dir = os.path.realpath(backup_dir)
    # Idempotency: a backup of unchanged content already retained is not
    # written again.  The check runs before the copy, which is also the
    # expensive part — a duplicate run costs one checksum scan.
    content_hash = _db_content_hash(session)
    reused = _db_backup_reuse(backup_dir, content_hash)

    def _drop_reused(reused_path: str) -> None:
        for _stale in (
            reused_path,
            os.path.join(backup_dir, _db_backup_manifest_name(reused_path)),
            os.path.join(backup_dir, _db_backup_config_name(reused_path)),
            os.path.join(backup_dir, _db_backup_runtime_name(reused_path)),
        ):
            _remove_quietly(_stale)

    # A reused backup with a WAL/SHM sidecar must never be served as a
    # restore point: ``db_verify_backup`` refuses such a copy because a
    # single-file restore would not carry the sidecar's pages.
    if reused is not None and any(
        os.path.exists(reused + _sidecar) for _sidecar in ("-wal", "-shm")
    ):
        _drop_reused(reused)
        reused = None
    if reused is not None and not db_verify_backup(session, reused)["ok"]:
        # A retained backup whose manifest still matches the live content
        # hash can nonetheless be corrupt now (bit rot, a truncated
        # transfer, a half-overwritten copy).  Reusing it would report
        # success for a directory whose newest restore point no longer
        # restores — the exact failure db_backup's fresh path refuses by
        # deleting an unverifiable copy.  Drop the trio and fall through
        # to write a fresh, self-verified backup.
        _drop_reused(reused)
        reused = None
    if reused is not None:
        # A reused backup was verified when it was written, but its file can
        # be corrupted afterwards (bit rot, a truncated copy, a hand edit).
        # Only report success when it still verifies now — otherwise fall
        # through and write a fresh backup instead of handing the caller a
        # restore point that would fail at restore time.
        check = db_verify_backup(session, reused)
        if check["ok"]:
            try:
                num_snippets = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]
            except Exception as e:
                # As in the fresh-backup path below: never fabricate a 0 count,
                # which db_verify_backup reads as an empty database.
                logger.warning("Could not count snippets for the reused backup: %s", e)
                session.rollback()
                num_snippets = None
            return {
                "backup": reused,
                "num_snippets": num_snippets,
                "snippet_count_known": num_snippets is not None,
                "fingerprint_version": fingerprint_version_get(session),
                "num_removed": 0,
                "manifest_written": True,
                "verified": True,
            }
        logger.warning(
            "Reused backup %s failed verification (%s); writing a fresh backup.",
            reused,
            "; ".join(check["issues"]),
        )
    # Stamped in UTC, not local time: the name is a sort key for prune
    # (legacy local stamps sort 01:30 EDT and 01:30 EST identically across
    # a fall-back, so the hour that was taken second can sort oldest and
    # be deleted).  The stamp comes from the same logical clock as the
    # manifest's ``created_at`` (``timestamp_now``, which honors
    # ``RESEMBL_NOW``): a second, hardwired wall-clock read made a
    # pinned replay's rows match byte-for-byte while its backup
    # filenames no longer did.  When no override is set, ``timestamp_now``
    # is still one read, converted to the backup stamp's
    # "%Y%m%dT%H%M%S-microsecond" shape and made distinct from any
    # backup already present (microsecond is a prune sort key, not a
    # uniqueness guarantee at a pinned instant).
    _moment = datetime.fromisoformat(timestamp_now())
    _base_stamp = _moment.strftime("%Y%m%dT%H%M%S")
    _micro = _moment.microsecond
    backup_name = f"assembly-{_base_stamp}-{_micro:06d}.db"
    while os.path.exists(os.path.join(backup_dir, backup_name)):
        # Carry into the next second instead of overflowing the fixed-width
        # microsecond field: a 7-digit field sorts after every 6-digit one
        # of the same second, so prune could delete the newest backup first.
        _moment += timedelta(microseconds=1)
        _base_stamp = _moment.strftime("%Y%m%dT%H%M%S")
        _micro = _moment.microsecond
        backup_name = f"assembly-{_base_stamp}-{_micro:06d}.db"
    final_path = os.path.join(backup_dir, backup_name)
    # ``backup`` refuses a destination that is the source connection's own
    # file, and a same-directory temp file makes the collision explicit
    # instead of depending on the driver's refusal.  Compare against the
    # live file and its sidecars, which a path-only check would miss.
    live_path = ""
    try:
        row = session.execute(text("PRAGMA database_list")).first()
        if row is not None and len(row) > 2 and row[2]:
            live_path = os.path.realpath(str(row[2]))
    except Exception as exc:
        # The collision guard degrades to "no live path known" — a failed
        # probe must not be silent, or a backup that clobbered nothing
        # only gets reported if it coincidentally collides.
        logger.warning("db_backup: could not probe live database path: %s", exc)
        live_path = ""
    if live_path:
        live_dir = os.path.dirname(live_path)
        live_base = os.path.basename(live_path)
        # The backup target lives beside the live file only when the caller
        # pointed backup_dir at the database's own directory; the temp file
        # below is never the live file itself, so this guards the rename
        # target, not the copy.
        if live_dir == backup_dir and (
            live_base == backup_name or live_base.startswith("assembly-")
        ):
            # Different names, same directory: safe.  Only an exact name
            # collision would overwrite the live file.
            if live_base == backup_name:
                raise ValueError("backup destination is the live database file")
        # Failure-domain concentration (docs/restore.md): a backup on the
        # same filesystem as the live database protects only against a
        # deleted/corrupted file, not instance/disk loss — both die
        # together.  Warn loudly (not refuse: tests and in-memory-to-file
        # flows legitimately co-locate, and some deployments accept the
        # risk) so an operator who meant off-host storage notices the
        # silent downgrade before the disaster does.
        try:
            if os.stat(live_path).st_dev == os.stat(backup_dir).st_dev:
                logger.warning(
                    "db_backup: backup directory %s is on the same filesystem "
                    "as the live database %s (st_dev=%s); instance/disk loss "
                    "destroys both — point BACKUP_DIR at different storage "
                    "(docs/restore.md)",
                    backup_dir,
                    live_path,
                    os.stat(backup_dir).st_dev,
                )
        except OSError as e:
            logger.warning("db_backup: could not check backup filesystem: %s", e)
    dbapi_connection = bind.raw_connection()  # type: ignore[union-attr]
    if dbapi_connection is None:
        raise ValueError("db_backup: no database connection available")
    try:
        sqlite_connection = dbapi_connection.dbapi_connection
        if not isinstance(sqlite_connection, sqlite3.Connection):
            raise ValueError("db_backup: the SQLite driver did not yield a sqlite3 connection")
        tmp_fd, tmp_path = tempfile.mkstemp(dir=backup_dir, prefix=".assembly-", suffix=".tmp")
        os.close(tmp_fd)
        try:
            # Remove the empty placeholder: sqlite3.connect creates the file
            # itself, and ``backup`` requires a path it may create.
            os.unlink(tmp_path)
            destination = sqlite3.connect(tmp_path)
            try:
                sqlite_connection.backup(destination)
                # The online backup copies the source's journal mode
                # (WAL for every live database), so the published file
                # would carry -wal/-shm sidecars on the next read-only
                # open and fail db_verify_backup's single-file check.
                # Fold it to DELETE and checkpoint so the backup is a
                # true single-file restore point.  Verify the mode actually
                # changed: a checkpoint that cannot complete (e.g. another
                # connection still holds a read mark) silently leaves WAL
                # sidecars beside the published copy, and the later
                # single-file verify fails the backup for it.  Fail loud
                # here instead of publishing a copy restore cannot ship.
                destination.execute("PRAGMA journal_mode=DELETE")
                if destination.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise RuntimeError("db_backup: could not switch backup to journal_mode=DELETE")
                destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                destination.close()
            # fsync before publish: an OS crash after ``os.replace`` but
            # before the file's data hits disk leaves a backup that looks
            # complete but restores empty/corrupt — ack'd-but-not-durable.
            _fsync_quiet(tmp_path)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError as e:
                logger.warning("Could not set permissions on backup %s: %s", tmp_path, e)
            os.replace(tmp_path, final_path)
            _fsync_quiet(backup_dir)
        except BaseException:
            # The temp database and any WAL sidecars from the backup
            # connection must not survive a failed publish: a ``-wal``
            # left beside the destination name would make the next
            # verify-backup fail with "wal sidecar present" for a file
            # that was never published.
            _remove_quietly(tmp_path)
            _remove_quietly(tmp_path + "-wal")
            _remove_quietly(tmp_path + "-shm")
            raise
    finally:
        try:
            dbapi_connection.close()
        except Exception:
            # The borrowed DBAPI connection is checked out of SQLAlchemy's
            # pool; a close failure here is not worth failing the backup,
            # but swallowing it silently hid connection leaks — log it.
            logger.warning("db_backup: borrowed connection close failed", exc_info=True)
    session.rollback()

    count_known = True
    try:
        num_snippets = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]
    except Exception as e:
        # The backup file itself is complete; only the count is unknown.  A
        # 0 would be read as "empty database" by db_verify_backup and fail a
        # healthy backup, so say what happened instead of fabricating it.
        # Record the absence (None, not 0) so verify knows the count was
        # never taken and skips the cross-check rather than failing it.
        logger.warning("Could not count snippets for the backup manifest: %s", e)
        session.rollback()
        num_snippets = None
        count_known = False
    # Config sidecar: copy the live config.toml verbatim beside the backup
    # so a restore can recover tuned settings, not just the corpus.  A
    # missing config is recorded as absent (defaults are regenerable); an
    # unreadable one fails loud so a backup that silently dropped tuned
    # state is never reported as complete.
    config_data, config_hash = _db_backup_config_read()
    config_name = _db_backup_config_name(backup_name)
    config_path = os.path.join(backup_dir, config_name)
    if config_data is not None:
        try:
            _write_sidecar(backup_dir, config_name, config_data)
        except OSError:
            _remove_quietly(final_path)
            raise
    runtime_data, runtime_hash = _db_backup_runtime_read()
    runtime_name = _db_backup_runtime_name(backup_name)
    runtime_path = os.path.join(backup_dir, runtime_name)
    if runtime_data is not None:
        try:
            _write_sidecar(backup_dir, runtime_name, runtime_data)
        except OSError:
            _remove_quietly(final_path)
            _remove_quietly(config_path)
            raise
    manifest = {
        "backup": backup_name,
        "num_snippets": num_snippets,
        "fingerprint_version": fingerprint_version_get(session),
        "content_hash": content_hash,
        "created_at": timestamp_now(),
        "config_sha256": config_hash,
        "config": config_name if config_data is not None else None,
        "runtime_sha256": runtime_hash,
        "runtime_manifest": runtime_name if runtime_data is not None else None,
    }
    manifest_name = _db_backup_manifest_name(backup_name)
    manifest_path = os.path.join(backup_dir, manifest_name)
    # mkstemp (O_EXCL, random suffix): predictable pid name let a planter
    # symlink the temp at a victim file, truncating it on write.
    fd, tmp_manifest = tempfile.mkstemp(dir=backup_dir, prefix=f".{manifest_name}.", suffix=".tmp")
    try:
        _fchmod_quiet(fd, 0o600, tmp_manifest)
        with open(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_manifest, manifest_path)
        _fsync_quiet(backup_dir)
    except OSError:
        _remove_quietly(tmp_manifest)
        # The backup file is complete but unverifiable: a restore checked
        # against a manifest that was never written would pass on contents
        # alone. Say so in the result instead of leaving the caller to
        # discover it at verify time.
        removed = _db_backup_prune(backup_dir, keep, preserve=backup_name)
        # An unverifiable backup must not sit silently where a later
        # verify-backup (or a restore drill) finds it by name order and
        # trusts it.  Remove the unmanifested file too: re-running the
        # backup will write a fresh one, and the caller was already told
        # via ``manifest_written``.
        _remove_quietly(final_path)
        # A manifest-less backup must not keep its sidecars either:
        # a later verify would find a sidecar no manifest vouches for.
        _remove_quietly(config_path)
        _remove_quietly(runtime_path)
        return {
            "backup": final_path,
            "num_snippets": manifest["num_snippets"],
            "snippet_count_known": count_known,
            "fingerprint_version": manifest["fingerprint_version"],
            "num_removed": removed,
            "manifest_written": False,
            "verified": False,
        }

    # Re-verify the published backup before reporting success: a copy
    # whose contents disagree with its manifest (a truncated publish, a
    # page flipped on write) must not be handed to a restore drill as
    # the newest restore point.  Deleting it here keeps ``db_backup``
    # honest — success means it verified, failure means no file was
    # left to trust.
    check = db_verify_backup(session, final_path)
    if not check["ok"]:
        logger.warning(
            "New backup %s failed verification (%s); removing it.",
            final_path,
            "; ".join(check["issues"]),
        )
        _remove_quietly(final_path)
        _remove_quietly(manifest_path)
        _remove_quietly(config_path)
        _remove_quietly(runtime_path)
        return {
            "backup": final_path,
            "num_snippets": manifest["num_snippets"],
            "snippet_count_known": count_known,
            "fingerprint_version": manifest["fingerprint_version"],
            "num_removed": 0,
            "manifest_written": False,
            "verified": False,
            "verify_issues": check["issues"],
        }

    removed = _db_backup_prune(backup_dir, keep, preserve=backup_name)
    return {
        "backup": final_path,
        "num_snippets": manifest["num_snippets"],
        "snippet_count_known": count_known,
        "fingerprint_version": manifest["fingerprint_version"],
        "num_removed": removed,
        "manifest_written": True,
        "verified": True,
    }


def _db_backup_prune(backup_dir: str, keep: int, *, preserve: str | None = None) -> int:
    """Remove backup files beyond the newest *keep* in *backup_dir*.

    Only ``assembly-*.db`` files this module writes are candidates, so
    pruning can never reach a file the user placed in the directory by
    hand.  The newest are kept by name sort, which is chronological for
    the timestamped names :func:`db_backup` writes.  A missing directory
    or unreadable entry is not an error: the backup is already in place,
    and pruning is the only thing being skipped.  *preserve* names a
    backup file that must survive this prune even when *keep* would
    remove it; ``db_backup`` passes the backup it just wrote, so a
    directory already over *keep* by hand-supplied ``assembly-*.db``
    files can never make pruning delete the newest restore point.
    """
    try:
        names = os.listdir(backup_dir)
    except OSError as e:
        logger.warning("Could not list backup directory %s for pruning: %s", backup_dir, e)
        return 0

    candidates = _db_backup_candidates(backup_dir, names)
    # Keep exactly ``keep`` files, newest by embedded stamp.  ``preserve``
    # (the backup just written) must survive even when its stamp sorts
    # oldest (clock skew, or hand-supplied far-future names): skip it in
    # the removal window AND still honor ``keep`` by extending the window
    # past it — otherwise a preserved oldest file leaves ``keep + 1``
    # backups retained, defeating retention.
    if preserve is not None and preserve in candidates:
        others = [n for n in candidates if n != preserve]
        to_remove = others[: max(0, len(others) - max(0, keep - 1))]
    else:
        to_remove = candidates[: max(0, len(candidates) - keep)]
    removed = 0
    for name in to_remove:
        if preserve is not None and name == preserve:
            continue
        try:
            os.remove(os.path.join(backup_dir, name))
            removed += 1
        except OSError as e:
            logger.warning("Could not remove old backup %s: %s", name, e)
            continue
        # A pruned backup's manifest describes no file that remains, so
        # remove it too rather than leaving the directory accumulating
        # manifests whose backups are gone.
        try:
            os.remove(os.path.join(backup_dir, _db_backup_manifest_name(name)))
        except OSError as e:
            # A missing manifest strands bookkeeping, not data; verify-backup
            # ignores manifests that name no backup, but an unremovable one
            # is worth an operator-visible record.
            logger.warning("Could not remove manifest for pruned backup %s: %s", name, e)
        _remove_quietly(os.path.join(backup_dir, _db_backup_config_name(name)))
        _remove_quietly(os.path.join(backup_dir, _db_backup_runtime_name(name)))
    return removed


def db_verify_backup(
    session: Session, backup_path: str, *, max_age_days: float | None = None
) -> dict:  # pylint: disable=unused-argument
    """Check that *backup_path* opens and holds the data its manifest promised.

    Opens the backup as a read-only SQLite database, runs
    ``PRAGMA integrity_check`` (a flipped or zeroed page that still
    answers ``COUNT(*)``
    must not pass as restor-able), confirms every table the schema
    creates is present, and compares the live snippet count and —
    when the manifest records one — the corpus ``content_hash``
    against the manifest :func:`db_backup` wrote beside it.  A backup
    whose own manifest disagrees with its contents is reported, not
    trusted: a copy that silently lost rows (a raw file copy paired
    with a stale WAL, a truncated transfer, a bit-rotted page) must
    fail verification rather than pass on the strength of having
    been written.

    Returns ``{"ok": bool, "num_snippets": int, "issues": [...]}``; ``ok``
    is false — and the caller should treat the backup as unrestorable —
    whenever the file cannot be opened, a table is missing, the
    integrity check fails, or the counts/hashes disagree.  A missing
    manifest is an issue but not a failure: the file may predate
    manifests, and its contents are what matter.

    *max_age_days* (when given) makes a backup older than that many
    days a hard failure.  Retention is by count, not by age, so
    without an age bound a directory whose newest copy is weeks old
    still verifies and the RPO gap stays invisible.  The age is
    measured from the manifest ``created_at`` against the logical
    clock (:func:`resembl.clock.timestamp_now`, honoring
    ``RESEMBL_NOW``), not from filesystem mtime, so the check replays
    deterministically.  Legacy manifests without ``created_at`` are
    skipped rather than failed.
    """
    del session
    import sqlite3
    from urllib.parse import quote

    issues: list[str] = []
    if not os.path.isfile(backup_path):
        return {"ok": False, "num_snippets": 0, "issues": [f"backup not found: {backup_path}"]}
    # A backup is a single-file restore point: a restore copies only the
    # ``.db`` file (docs/restore.md).  A ``-wal``/``-shm`` sidecar beside
    # it (a previous restore attempt, a hand-copied live pair) would be
    # replayed by the read-only open below, so verify would measure
    # main+wal while a restore ships main alone — a backup that verifies
    # here and restores empty.  Flag the sidecar as a hard failure so the
    # copy that cannot survive a restore is never trusted.
    issues.extend(
        f"wal sidecar present: {os.path.basename(backup_path + _sidecar)} "
        "would be replayed on verify but not copied on restore"
        for _sidecar in ("-wal", "-shm")
        if os.path.exists(backup_path + _sidecar)
    )
    try:
        # Percent-encode so '?'/'#' in a filename cannot inject URI params
        # (e.g. "?mode=rwc") and escape the read-only mode.  Normalize to
        # forward slashes first: a SQLite URI uses '/' as the separator on
        # every platform, and the Windows backslash spelling percent-encoded
        # (``file:C%3A%5C...``) is not the absolute form SQLite opens there.
        _uri_path = os.path.abspath(backup_path).replace(os.sep, "/")
        # ``errors="surrogateescape"``: a filename carrying an undecodable
        # byte arrives as a lone surrogate (``os.fsdecode``); plain
        # ``quote`` raises UnicodeEncodeError on it, crashing verify
        # instead of reporting the file unopenable.  surrogateescape
        # encodes the raw byte as %FF, which SQLite decodes back to it.
        connection = sqlite3.connect(
            f"file:{quote(_uri_path, safe='/', encoding='utf-8', errors='surrogateescape')}"
            "?mode=ro",
            uri=True,
        )
    except sqlite3.Error:
        return {"ok": False, "num_snippets": 0, "issues": ["cannot open backup"]}
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected_tables = {
            "snippet",
            "collection",
            "lsh_bucket",
            "lsh_meta",
            "app_meta",
            "snippetversion",
        }
        missing = sorted(expected_tables - tables)
        if missing:
            issues.append(f"missing tables: {', '.join(missing)}")
        num_snippets = 0
        content_hash = None
        if "snippet" in tables:
            row = connection.execute("SELECT COUNT(*) FROM snippet").fetchone()
            num_snippets = int(row[0]) if row else 0
            # Corpus identity: SHA-256 over (checksum, names, tags, collection,
            # minhash) in PK order (the same digest _db_content_hash computes).
            # Catches a backup whose row count matches its manifest but whose
            # rows are not the rows promised (half-overwritten,
            # truncated-then-padded copy, or fingerprints rewritten by a
            # reindex the manifest predates).
            digest = hashlib.sha256()
            # Streamed, not fetchall(): a large backup would otherwise be
            # materialized whole just to hash it (same fix as _db_content_hash).
            for row in connection.execute(
                "SELECT checksum, names, tags, collection, minhash FROM snippet ORDER BY checksum"
            ):
                for value in row[:-1]:
                    digest.update(str(value or "").encode("utf-8"))
                    digest.update(b"\0")
                digest.update(row[-1] or b"")
                digest.update(b"\0")
            content_hash = digest.hexdigest()
        # integrity_check: a flipped or zeroed page that still answers
        # COUNT(*) must not pass as restorable.
        # Only the first three rows are ever rendered, so cap the read:
        # a heavily corrupt database reports one row per damaged page
        # and fetchall() would materialize the whole list to slice it.
        integrity_rows: list[str] = []
        for row in connection.execute("PRAGMA integrity_check"):
            integrity_rows.append(str(row[0]))
            if len(integrity_rows) >= 3:
                break
        if integrity_rows != ["ok"]:
            issues.append(f"integrity_check failed: {'; '.join(integrity_rows)}")
    except sqlite3.Error:
        return {"ok": False, "num_snippets": 0, "issues": ["cannot read backup"]}
    finally:
        connection.close()

    manifest_dir = os.path.dirname(os.path.abspath(backup_path))
    per_backup_path = os.path.join(manifest_dir, _db_backup_manifest_name(backup_path))
    shared_path = os.path.join(manifest_dir, DB_BACKUP_MANIFEST_NAME)
    manifest_snippets: int | None = None
    manifest_hash: str | None = None
    manifest_config_hash: str | None = None
    manifest_config_name: str | None = None
    manifest_runtime_hash: str | None = None
    manifest_runtime_name: str | None = None
    manifest_created: str | None = None
    manifest_present = False
    for manifest_path in (per_backup_path, shared_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
        except OSError, ValueError, RecursionError:
            # RecursionError is not a ValueError: a deeply nested
            # manifest from a hostile backup directory recurses once
            # per level in the decoder and must skip the candidate,
            # not abort verification — the same guard
            # _string_list_or_none applies to stored JSON columns.
            continue
        # The shared manifest records only the newest backup's own name,
        # so treat it as this file's contract only when it names this
        # file; otherwise it describes a different backup in the directory.
        if os.path.basename(manifest_path) == DB_BACKUP_MANIFEST_NAME:
            if not isinstance(data, dict) or data.get("backup") != os.path.basename(backup_path):
                continue
        if isinstance(data, dict) and isinstance(data.get("num_snippets"), int):
            manifest_snippets = data["num_snippets"]
            manifest_hash = (
                data.get("content_hash") if isinstance(data.get("content_hash"), str) else None
            )
            manifest_config_hash = (
                data.get("config_sha256") if isinstance(data.get("config_sha256"), str) else None
            )
            manifest_config_name = (
                data.get("config") if isinstance(data.get("config"), str) else None
            )
            manifest_runtime_hash = (
                data.get("runtime_sha256") if isinstance(data.get("runtime_sha256"), str) else None
            )
            manifest_runtime_name = (
                data.get("runtime_manifest")
                if isinstance(data.get("runtime_manifest"), str)
                else None
            )
            manifest_created = (
                data.get("created_at") if isinstance(data.get("created_at"), str) else None
            )
            manifest_present = True
        elif isinstance(data, dict) and data.get("num_snippets") is None:
            # The count was never taken at backup time (the count query
            # failed but the file is complete): record it as not
            # cross-checked, not as a mismatch against a fabricated 0.
            issues.append("backup manifest records no snippet count; count not cross-checked")
            break
        break
    if not manifest_present:
        issues.append("no readable backup manifest; snippet count not cross-checked")
    if manifest_snippets is not None and manifest_snippets != num_snippets:
        issues.append(
            f"snippet count mismatch: backup holds {num_snippets}, "
            f"manifest records {manifest_snippets}"
        )
    if manifest_hash is not None and content_hash is not None and manifest_hash != content_hash:
        issues.append("content hash mismatch: backup contents differ from manifest")
    # Stale-backup check: retention is by count, not age, so a directory
    # whose newest copy predates the RPO target still verifies on
    # contents alone while the restore point silently ages out.  Measure
    # age from the manifest's ``created_at`` against the logical clock
    # (``timestamp_now``, honoring ``RESEMBL_NOW``) so the check replays
    # deterministically and never reads filesystem mtime.  A legacy
    # manifest without ``created_at`` is skipped rather than failed.
    if max_age_days is not None and manifest_created is not None:
        try:
            _created = datetime.fromisoformat(manifest_created)
        except ValueError:
            _created = None
        if _created is not None:
            if _created.tzinfo is None:
                _created = _created.replace(tzinfo=UTC)
            _now = datetime.fromisoformat(timestamp_now())
            if _now.tzinfo is None:
                _now = _now.replace(tzinfo=UTC)
            _age_days = (_now - _created).total_seconds() / 86400
            if _age_days > max_age_days:
                issues.append(
                    f"backup is {_age_days:.1f} days old, exceeds max_age_days={max_age_days}"
                )
    # Config sidecar cross-check: when the manifest records a config hash,
    # the sidecar must exist and hash to it.  A missing or mismatched
    # sidecar is a hard failure (the backup promised tuned state it no
    # longer holds); legacy manifests without the field are skipped.
    if manifest_config_hash is not None:
        sidecar_name = manifest_config_name or _db_backup_config_name(backup_path)
        # The manifest is untrusted (it travels with the backup): a
        # ``config`` value carrying a separator or ``..`` would make
        # verify read/hash an arbitrary file outside the backup
        # directory (path traversal / hash oracle).  Only a plain file
        # name beside the backup is a valid sidecar; anything else is
        # a mismatch, not a path to follow.
        if sidecar_name in {"", ".", ".."} or os.path.basename(sidecar_name) != sidecar_name:
            issues.append("config mismatch: backup config sidecar missing")
        else:
            sidecar_path = os.path.join(manifest_dir, sidecar_name)
            try:
                sidecar_data = _read_bounded_file(sidecar_path)
                if hashlib.sha256(sidecar_data).hexdigest() != manifest_config_hash:
                    issues.append("config mismatch: backup config differs from manifest")
            except OSError, ValueError:
                issues.append("config mismatch: backup config sidecar missing")
    # Runtime-manifest sidecar cross-check: when the manifest records a
    # runtime hash, the sidecar must exist and hash to it, exactly like
    # the config sidecar.  A missing or mismatched sidecar is a hard
    # failure: the backup promised pinned state it no longer holds, and
    # /find-pinned would be unrestorable.  Legacy manifests without the
    # field are skipped.  The manifest is untrusted, so a
    # ``runtime_manifest`` value carrying a traversal must not be
    # followed outside the backup directory either (same guard as
    # the config sidecar above).
    if manifest_runtime_hash is not None:
        runtime_sidecar_name = manifest_runtime_name or _db_backup_runtime_name(backup_path)
        if (
            runtime_sidecar_name in {"", ".", ".."}
            or os.path.basename(runtime_sidecar_name) != runtime_sidecar_name
        ):
            issues.append("runtime mismatch: backup runtime manifest sidecar missing")
        else:
            runtime_sidecar_path = os.path.join(manifest_dir, runtime_sidecar_name)
            try:
                runtime_sidecar_data = _read_bounded_file(runtime_sidecar_path)
                if hashlib.sha256(runtime_sidecar_data).hexdigest() != manifest_runtime_hash:
                    issues.append("runtime mismatch: backup runtime manifest differs from manifest")
            except OSError, ValueError:
                issues.append("runtime mismatch: backup runtime manifest sidecar missing")
    # A missing manifest alone does not fail verification: the file may
    # predate manifests, and its contents are what matter.  A missing table,
    # a failed integrity_check, or a count/hash mismatch does.
    hard_failures = [
        i
        for i in issues
        if "mismatch" in i
        or "missing tables" in i
        or "integrity_check" in i
        or "wal sidecar" in i
        or "exceeds max_age_days" in i
    ]
    return {"ok": not hard_failures, "num_snippets": num_snippets, "issues": issues}


# Collection Functions


def collection_create(session: Session, name: str, description: str = "") -> Collection:
    """Create a snippet collection, or return the existing one of that name.

    Idempotent: ``name`` is the primary key, so a second execution used to
    raise an ``IntegrityError`` and leave the caller reporting a failure for
    a collection that already exists — the state after the first run was
    already the requested one.  Setup scripts and any retried create now
    converge, and the stored row (its description and ``created_at``) is left
    untouched rather than overwritten, so re-running a create cannot rewrite
    a description a human has since edited.
    """
    # The name is the primary key, so it is normalized before the lookup: a
    # create with the NFD spelling of a name stored in NFC would miss the
    # existing row and then collide with it on insert.
    name = name_normalize(name)
    if not name:
        raise ValueError("Collection name cannot be empty.")
    existing = Collection.get_by_name(session, name)
    if existing is not None:
        return existing
    collection = Collection(name=name, description=normalize_unicode(description))
    session.add(collection)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent create of the same name committed first.
        session.rollback()
        existing = Collection.get_by_name(session, name)
        if existing is None:
            raise
        return existing
    session.refresh(collection)
    return collection


def collection_delete(session: Session, name: str, quiet: bool = False) -> bool:
    """Delete a collection and unassign all its snippets."""
    # Normalized for the same reason as ``collection_add_snippet``: the row
    # is stored NFC, and the bulk UPDATE below matches on that same form.
    name = name_normalize(name)
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
    # The name is the primary key and is stored NFC by ``collection_create``,
    # so it is normalized before the lookup: the NFD spelling macOS hands
    # back for an accented name would otherwise miss the row.
    collection_name = name_normalize(collection_name)
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


# Version Functions


def snippet_version_list(
    session: Session, checksum: str, *, limit: int | None = None
) -> list[dict]:
    """Return version history for a snippet.

    Only ``(id, snippet_checksum, created_at)`` are fetched: :meth:`SnippetVersion.get_by_checksum`
    loads the full row (``code`` + ``minhash``) ORM objects, but the caller
    renders only the three metadata columns — a large version history would
    drag every version's code through the ORM for nothing.

    *limit* caps the fetch so a caller rendering a bounded history does
    not materialize an unbounded version list; ``None`` (the default)
    keeps the historical unbounded behavior.
    """
    stmt = (
        select(
            SnippetVersion.id,
            SnippetVersion.snippet_checksum,
            SnippetVersion.created_at,
        )
        .where(SnippetVersion.snippet_checksum == checksum)
        .order_by(SnippetVersion.created_at.desc(), SnippetVersion.id.desc())  # type: ignore[attr-defined, union-attr]
    )
    if limit is not None:
        if limit <= 0:
            return []
        stmt = stmt.limit(limit)
    rows = session.exec(stmt).yield_per(1000)
    return [
        {"id": vid, "snippet_checksum": s_checksum, "created_at": created_at}
        for vid, s_checksum, created_at in rows
    ]


# Merge Functions


def _fingerprint_stamps_clear(session: Session) -> None:
    """Drop all three fingerprint stamps so the next ``find`` reindexes once."""
    fingerprint_version_clear(session)
    fingerprint_ngram_clear(session)
    fingerprint_perm_clear(session)


def db_merge(session: Session, source_db_path: str, *, _merge_retry_seen: bool = False) -> dict:
    """Merge snippets from a source database into the current one.

    *source_db_path* is a SQLite file path, or a full database URL (e.g.
    ``duckdb:///file.db`` or ``postgresql+pg8000://user:pass@host/db``) —
    any backend with its driver installed can be a source.

    Deduplicates by checksum:
    - New snippets (unique checksum) are inserted.
    - Existing snippets gain any new names and tags from the source.
    - Collections from the source are created if they don't exist.
    - Source rows with corrupt names/tags metadata (not a JSON array) are
      skipped with a warning, like rows with unusable fingerprints.

    Returns a dict with counts of added, updated, and skipped snippets.
    """
    from .database import create_db_engine

    start_time = monotonic_now()
    # The source may be any backend: a full URL (e.g. duckdb:///file.db,
    # postgresql+pg8000://...) is used as-is; otherwise it is a SQLite path.
    # Build the URL from the filesystem path through SQLAlchemy's URL
    # constructor rather than string concatenation: on Windows a path such
    # as ``C:\Users\x\db`` pasted into ``sqlite:///{path}`` keeps its
    # backslashes (and the drive colon is percent-encoded), so SQLite opens
    # a different file than the one the user named.  ``URL.create`` takes
    # the native path verbatim and renders the triple-slash absolute/relative
    # form correctly on every platform.
    if "://" in source_db_path:
        source_url = source_db_path
    else:
        from sqlalchemy.engine.url import URL

        source_url = str(URL.create("sqlite", database=source_db_path))

    source_engine = None
    try:
        source_engine = create_db_engine(source_url)
        source_session = Session(source_engine)
    except Exception as e:
        if source_engine is not None:
            source_engine.dispose()
        # Mask credentials: a URL source may embed a password, and the
        # exception/log path must not persist it (same rule as db_url_mask
        # in the CLI/server open paths). Plain paths pass through intact.
        # The driver error text (SQL, host, credentials) stays in the log
        # only; the caller gets a stable message naming the masked source.
        masked = db_url_mask(source_db_path)
        logger.exception("Failed to open source database %r: %s", masked, e)
        return {"error": f"failed to open source database {masked}"}

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
        local_collections = {name_normalize(col.name): col for col in Collection.get_all(session)}
        for col in source_session.exec(select(Collection).order_by(Collection.name)).all():
            # The name is the collection's primary key on both sides, so the
            # comparison has to be on the form both stores: a source row whose
            # name arrived in a different composition would otherwise miss the
            # local row and be inserted as a second collection under a name
            # that is the same to any reader.
            merged_name = name_normalize(col.name)
            if merged_name not in local_collections:
                new_col = Collection(
                    name=merged_name,
                    description=col.description,
                    # A source row with no readable timestamp (a NULL column in
                    # a hand-built or older database) still has to land in a
                    # NOT NULL column, and leaving it empty would sort it
                    # ahead of every real collection.  The import moment is
                    # the only instant this tool can honestly record.
                    created_at=timestamp_normalize(col.created_at) or timestamp_now(),
                )
                session.add(new_col)
                local_collections[merged_name] = new_col

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
            try:
                _insert_snippet_rows(session, new_rows)
                # Same atomicity as ``snippet_add_batch``: the new snippets
                # and the buckets that make them findable commit together,
                # so a merge interrupted between the two cannot leave
                # either half behind.
                index_threshold = lsh_index_add_batch(session, added_minhashes, commit=False)
                session.commit()
            except IntegrityError:
                # A concurrent merge of the same source committed the same
                # checksums first.  Rolling back undoes this chunk; the
                # colliding rows already live locally, so drop the count
                # and let the outer handler re-run the merge from the
                # start, where they take the idempotent existing-row path.
                session.rollback()
                raise _MergeRetryError from None
            if index_threshold is not None:
                lsh_pickle_cache_remove(index_threshold)
            new_rows.clear()
            added_minhashes.clear()

        def record_new(source_snippet: Snippet) -> None:
            nonlocal added, skipped
            if _source_lists_or_none(source_snippet) is None:
                skipped += 1
                return
            # A source row may name a collection the source has no row for
            # (a legacy database written before referential integrity was
            # enforced).  The destination's foreign key would reject the
            # insert, so materialize the collection row first — exactly as
            # the collections pass above does for rows it can see.
            if source_snippet.collection:
                coll_name = name_normalize(source_snippet.collection)
                if coll_name not in local_collections:
                    new_col = Collection(
                        name=coll_name,
                        description="",
                        created_at=timestamp_now(),
                    )
                    session.add(new_col)
                    local_collections[coll_name] = new_col
                    logger.warning(
                        "Source snippet %s names missing collection %s; created it on merge.",
                        source_snippet.checksum,
                        coll_name,
                    )
            try:
                packed = minhash_ensure_packed(source_snippet.minhash)
            except ValueError:
                # A non-packed fingerprint (legacy format or corrupt) is
                # never deserialized — that would execute attacker-controlled
                # pickle code from a hostile merge source.  Recompute it from
                # the source row's code instead; only a row with no usable
                # code either is skipped.
                logger.warning(
                    "Recomputing fingerprint for source snippet %s (unsupported format).",
                    source_snippet.checksum,
                )
                try:
                    packed = minhash_pack(code_create_minhash(source_snippet.code))
                except Exception as recompute_exc:
                    logger.warning(
                        "Skipping source snippet %s: corrupt fingerprint and "
                        "no recomputable code (%s).",
                        source_snippet.checksum,
                        recompute_exc,
                    )
                    skipped += 1
                    return
            new_rows.append(
                {
                    "checksum": source_snippet.checksum,
                    "names": source_snippet.names,
                    "code": source_snippet.code,
                    "minhash": packed,
                    "tags": source_snippet.tags,
                    "collection": source_snippet.collection,
                }
            )
            added += 1
            added_minhashes.append((source_snippet.checksum, packed))
            if len(new_rows) >= merge_flush_size:
                flush_new_rows()

        def flush_merge_chunk() -> None:
            nonlocal updated, skipped
            if not merge_chunk:
                return
            local_rows = _snippets_by_checksums(session, [s.checksum for s in merge_chunk])
            for source_snippet in merge_chunk:
                existing = local_rows.get(source_snippet.checksum)
                if existing is None:
                    # Not in the local database (or vanished concurrently) —
                    # treat as new, matching the old fallback.
                    record_new(source_snippet)
                    continue
                src_lists = _source_lists_or_none(source_snippet)
                if src_lists is None:
                    skipped += 1
                    continue
                src_names, src_tags = src_lists
                changed = False

                # Merge names and tags order-preservingly: existing entries
                # keep their positions (name_list[0] is the primary name — it
                # drives display, export filenames, and YARA rule names) and
                # source-only entries are appended.  Same convention as
                # ``snippet_add_batch``'s alias merge; a sorted union would
                # silently reassign the primary name on every merge.
                existing_names = existing.name_list
                merged_names = _merge_names(existing_names, src_names)
                if merged_names != existing_names:
                    existing.names = json.dumps(merged_names)
                    changed = True

                # Merge tags (independent of names), deduplicated in
                # NFC as names are: a source row written on macOS (NFD)
                # and the composed tag already stored are one tag, and
                # an unnormalized union would store both spellings.
                existing_tags = existing.tag_list
                merged_tags = list(existing_tags)
                seen_tags = {name_normalize(t) for t in merged_tags}
                for src_tag in src_tags:
                    normalized_tag = name_normalize(src_tag)
                    if normalized_tag in seen_tags:
                        continue
                    seen_tags.add(normalized_tag)
                    merged_tags.append(normalized_tag)
                if merged_tags != existing_tags:
                    existing.tags = json.dumps(merged_tags)
                    changed = True

                if changed:
                    session.add(existing)
                    updated += 1
                else:
                    skipped += 1

                # Assign collection if the existing snippet doesn't have one
                if not existing.collection and source_snippet.collection:
                    # Same missing-collection guard as record_new: the
                    # destination foreign key rejects a dangling name.
                    coll_name = name_normalize(source_snippet.collection)
                    if coll_name not in local_collections:
                        new_col = Collection(
                            name=coll_name,
                            description="",
                            created_at=timestamp_now(),
                        )
                        session.add(new_col)
                        local_collections[coll_name] = new_col
                    existing.collection = coll_name
                    session.add(existing)
            merge_chunk.clear()

        # Import snippets (streaming, so memory stays bounded for big sources)
        for source_snippet in Snippet.stream_all(source_session):
            merge_chunk.append(source_snippet)
            if len(merge_chunk) >= _CHECKSUM_CHUNK_SIZE:
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
        # next `find` reindexes once and normalizes everything.  Only new
        # rows can change the stored population: an alias/tag-only merge
        # (added == 0) leaves every fingerprint untouched, and clearing the
        # stamps there would force a pointless full reindex on the next find.
        if added:
            _fingerprint_stamps_clear(session)
    except _MergeRetryError:
        # A concurrent merge committed this chunk's checksums first.
        # The rows now exist locally, so a re-run takes the existing-row
        # path (alias/tag/collection merge) and converges.  Bounded to
        # one retry: a second collision means a pathological writer
        # loop, which must surface as an error instead of spinning.
        session.rollback()
        if _merge_retry_seen:
            if added:
                _fingerprint_stamps_clear(session)
            return {"error": "merge collided with a concurrent writer"}
        return db_merge(session, source_db_path, _merge_retry_seen=True)
    except Exception as e:
        logger.exception("Merge failed for %r: %s", db_url_mask(source_db_path), e)
        # Chunks already through ``flush_new_rows`` are committed (it commits
        # as each chunk fills), so a failure can leave a partial merge behind.
        # End the aborted transaction so the caller's session stays usable,
        # and when any source rows landed, clear the fingerprint stamps: the
        # copied blobs may be foreign-format, and an unchanged stamp would
        # vouch for them — every later find would skip those snippets as
        # stale instead of healing them with one reindex.
        # The raw exception text (SQL, file paths, driver internals) stays
        # in the log; the caller gets a stable message naming the source.
        session.rollback()
        if added:
            _fingerprint_stamps_clear(session)
        return {"error": f"merge failed for {db_url_mask(source_db_path)}"}
    finally:
        source_session.close()
        # Return the pooled connections to the OS — the source engine is
        # never reused after this call.
        source_engine.dispose()

    end_time = monotonic_now()
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "total_source": added + updated + skipped,
        "time_elapsed": end_time - start_time,
    }
