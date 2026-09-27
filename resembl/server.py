"""A long-lived server that keeps the database warm for instant ``find``.

Every CLI invocation pays ~450 ms of interpreter/library startup; a search
itself is ~1.4 ms.  ``resembl serve`` starts a small HTTP server (stdlib
only) that holds the engine and LSH index warm, and ``find`` automatically
talks to it when it is running — turning the headline warm-find latency from
~450 ms into a few milliseconds.

The server writes a port file (``server_<dbhash>.port`` in the cache dir)
that ``find`` uses to locate it.  Requests run concurrently: each gets its
own SQLAlchemy session against the shared (warm) engine, and SQLite's WAL
mode allows concurrent readers.  The fingerprint migration and the LSH
index build are done once at startup, so serving is read-only in the normal
case.
"""

from __future__ import annotations

import atexit
import functools
import hashlib
import json
import logging
import os
import socket
import sys
import threading
import weakref
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from sqlalchemy.engine import Engine
from sqlmodel import Session

from .cache import lsh_index_build
from .config import ResemblConfig
from .core import (
    LSH_THRESHOLD,
    IndexBuildError,
    db_reindex,
    snippet_find_matches,
    snippet_matches_payload,
)
from .paths import cache_dir_get, db_url_mask, server_port_path

logger = logging.getLogger(__name__)

#: Version-guarded result cache: key -> (db_version, payload).  SQLite's
#: ``PRAGMA data_version`` increments on every commit, so a cached result is
#: returned only while the database is unchanged — repeated queries (triage
#: workflows re-checking the same hashes) answer in ~0.1 ms instead of
#: ~1.4 ms, never stale.  Non-SQLite backends get no version counter and
#: bypass the cache.  The key ends with the serving engine's URL: one
#: process may run several servers for different databases (tests,
#: embedded callers), and the version counter is per-database — without that
#: final component a hit computed for database A could be served to database
#: B whenever both counters happened to carry the same value.  It starts with
#: :func:`_query_cache_key`, not the query itself, so the entry-count bound
#: is also a memory bound.
_RESULT_CACHE: OrderedDict[tuple, tuple[int | None, dict]] = OrderedDict()
_RESULT_CACHE_MAX = 128
#: Serializes access to the shared cache: requests run in concurrent
#: handler threads, and OrderedDict is not thread-safe.
_RESULT_CACHE_LOCK = threading.Lock()


def _query_cache_key(query: str) -> str:
    """Return the result-cache key component standing in for *query*.

    A find's answer is a pure function of the query text and the find
    parameters, so the query alone identifies the cached payload.  The query
    is request-controlled and as large as the body cap (8 MiB), and retaining
    it verbatim made ``_RESULT_CACHE_MAX`` an entry count rather than a
    memory bound: 128 large requests pinned around a gigabyte of key
    strings while their payloads stayed small, and every key stayed resident
    until the LRU evicted it.  The digest is a fixed 64 characters, over the
    exact text the find keys on (not its normalized form, which is a request
    parameter in its own right), and it is the same content-addressing
    primitive :func:`resembl.scoring.string_checksum` already uses for
    snippet checksums.
    """
    return hashlib.sha256(query.encode("utf-8", errors="surrogatepass")).hexdigest()


class _InFlight:
    """The compute lock for one cache key, plus the threads using it.

    Reference-counted so the registry entry can be dropped by the last
    thread to leave; see :func:`_inflight`.
    """

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


#: Per-key compute locks, keyed exactly like :data:`_RESULT_CACHE`.  A
#: miss is recomputed by every concurrent request for that key otherwise:
#: ``serve`` runs one handler thread per connection, so a burst of identical
#: queries (a triage loop fanning out, a client retrying after a slow
#: response) pays the full find N times over and publishes N copies, the
#: last of which wins and evicts the others.  Only the first thread to miss
#: a key computes it; the rest wait here and read the entry it stored.  The
#: registry is keyed the same way as the cache, so it tracks the keys
#: currently being computed and is emptied as they finish, so it cannot
#: outgrow the concurrency the server already allows.
_INFLIGHT: dict[tuple, _InFlight] = {}
_INFLIGHT_LOCK = threading.Lock()


@contextmanager
def _inflight(key: tuple) -> Iterator[None]:
    """Hold *key*'s compute lock, joining any thread already computing it.

    The wait is also why the caller re-reads the database version once it
    holds this: the previous request's find ran while we queued, and the
    version sampled before the wait says nothing about the database as it is
    now.
    """
    with _INFLIGHT_LOCK:
        entry = _INFLIGHT.get(key)
        if entry is None:
            entry = _InFlight()
            _INFLIGHT[key] = entry
        entry.users += 1
    entry.lock.acquire()
    try:
        yield
    finally:
        # Released before the count drops: a thread arriving after the
        # registry entry is gone finds no holder to wait for, so the key it
        # creates is genuinely free.
        entry.lock.release()
        with _INFLIGHT_LOCK:
            entry.users -= 1
            if entry.users == 0:
                del _INFLIGHT[key]


#: One single-connection probe engine per served database, used only by
#: :func:`_db_version`.  Weakly keyed so a disposed engine takes its probe
#: (and the SQLite handle behind it) with it.
_VERSION_PROBES: weakref.WeakKeyDictionary[Engine, Engine] = weakref.WeakKeyDictionary()
#: The probe is one shared connection, so reading it is serialized.
_VERSION_PROBE_LOCK = threading.Lock()

#: Find parameters used *only* when ``_find_one`` is called without a
#: serving server (tests, direct API use): they mirror
#: :class:`ResemblConfig`'s defaults.  Real requests take their defaults
#: from the per-instance ``find_defaults`` of the server that owns the
#: handler thread (see :func:`serve`) — module globals would let a second
#: ``serve`` call retarget an older, still-serving server's threads, exactly
#: the bug ``_FindHandler.engine`` avoids for the engine itself.
_DEFAULT_FIND_PARAMS = ResemblConfig()


def _session_engine(session: Session) -> Engine:
    """Return the engine behind *session* (every caller binds an ``Engine``)."""
    return cast(Engine, session.get_bind())


def _db_version(session: Session) -> int | None:
    """Return a DB-change counter for cache invalidation (SQLite only).

    ``PRAGMA data_version`` is a *per-connection* counter: every connection
    starts its own count and only advances when it observes a commit made by
    a different connection, so the same number means nothing across
    connections.  Probing on the request's pooled connection therefore let a
    connection that had already seen the last commit serve a cached payload
    computed before it to a connection that had not — the guard silently
    stopped guarding as soon as the pool held more than one live connection.
    One shared probe connection per database (see :data:`_VERSION_PROBES`)
    makes the counter advance in step with the file for the whole cache.

    ``None`` means "no counter": callers bypass the cache.
    """
    engine = _session_engine(session)
    if engine.dialect.name != "sqlite":
        return None
    url = engine.url
    # A private in-memory database is per-connection, so a probe would read
    # an empty database of its own and report a constant version.  Those
    # engines (tests, embedded callers) go uncached rather than wrong.
    if url.database in (None, "", ":memory:"):
        return None
    from sqlalchemy.pool import StaticPool
    from sqlmodel import create_engine, text

    with _VERSION_PROBE_LOCK:
        probe = _VERSION_PROBES.get(engine)
        if probe is None:
            # StaticPool keeps every checkout on the same connection; the
            # probe is threaded, so the sqlite3 handle must be shareable.
            probe = create_engine(
                url, poolclass=StaticPool, connect_args={"check_same_thread": False}
            )
            _VERSION_PROBES[engine] = probe
        with probe.connect() as conn:
            return int(conn.execute(text("PRAGMA data_version")).scalar() or 0)


def _version_probe_release(engine: Engine) -> None:
    """Drop and dispose the version probe cached for *engine*, if any.

    The probe is a second engine over the same database, holding its own
    SQLite connection (see :func:`_db_version`).  :class:`_FindServer`'s
    ``server_close`` disposes the engine it owns, but the probe is only
    weakly referenced from the engine, so it survived that dispose and kept
    the handle open until the process collected both.  A process that
    starts and stops servers repeatedly — an embedded caller, a test
    harness — then accumulated one open handle and pool per generation, and
    on Windows the last handle keeps the database file from being removed
    or replaced.  Releasing the probe where the engine is released makes
    the handle's lifetime the engine's lifetime.
    """
    with _VERSION_PROBE_LOCK:
        probe = _VERSION_PROBES.pop(engine, None)
    if probe is not None:
        probe.dispose()


class BadRequestError(ValueError):
    """A caller-supplied parameter is unusable; answered as 400 by handlers.

    Raised by :func:`_parse_find_request` so one code path owns the
    validation messages: every bad request answers the same JSON envelope
    (``{"error": ...}``) with a 400 status, whichever endpoint raised it.
    """


@dataclass(frozen=True)
class _FindRequest:
    """Validated find parameters for one request (see :func:`_parse_find_request`)."""

    top_n: int
    threshold: float | None
    normalize: bool
    ngram_size: int
    num_permutations: int
    jaccard_weight: float
    #: ``threshold`` when the caller sent one, else the server's configured
    #: LSH threshold — the index the server built at startup.  Range-checked
    #: and passed to the banding feasibility test.
    effective_threshold: float


def _as_int(value: object, name: str) -> int:
    """Return *value* as an exact integer, or raise :class:`BadRequestError`.

    A count has to be a whole number, and ``int()`` alone does not check
    that: it truncates ``5.9`` to 5 and turns a client's ``2.9`` permutations
    into a valid 2, so the request is answered as though it had said
    something it did not.  Integral floats and numeric strings stay legal
    (``128.0``, ``"128"``); a fractional value, a boolean, NaN or infinity is
    a bad request.
    """
    if isinstance(value, bool):
        raise BadRequestError(f"{name} must be a whole number, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise BadRequestError(f"{name} must be a whole number, got {value!r}")
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise BadRequestError(f"{name} must be a whole number, got {value!r}") from exc
    raise BadRequestError(f"{name} must be a whole number, got {value!r}")


def _parse_find_request(body: dict, params: ResemblConfig) -> _FindRequest:
    """Validate *body*'s find parameters, or raise :class:`BadRequestError`.

    *params* supplies the serving server's configured defaults for values
    absent from *body*; direct callers without a server get plain
    :class:`ResemblConfig` defaults.
    """
    # An explicit JSON ``null`` means "not provided", exactly like an absent
    # key: dict.get's default only fires on missing keys, so a null would
    # crash int()/float() (and flip ``normalize`` to False) instead of using
    # the configured default.
    provided = {k: v for k, v in body.items() if v is not None}
    try:
        top_n = _as_int(provided.get("top_n", params.top_n), "top_n")
        # Coerce inside the same guard as the numeric fields: a JSON string
        # threshold ("0.9") must answer the clean bad-request payload, not
        # raise TypeError at the range check below and surface as a 500
        # echoing internal error text.
        threshold_raw = provided.get("threshold", params.lsh_threshold)
        threshold = float(threshold_raw) if threshold_raw is not None else None
        normalize = bool(provided.get("normalize", True))
        ngram_size = _as_int(provided.get("ngram_size", params.ngram_size), "ngram_size")
        num_permutations = _as_int(
            provided.get("num_permutations", params.num_permutations), "num_permutations"
        )
        jaccard_weight = float(provided.get("jaccard_weight", params.jaccard_weight))
    except BadRequestError:
        # A whole-number count already failed :func:`_as_int`; its message
        # names the value, so keep it instead of the generic one below.
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        # A non-numeric parameter is a bad request, answered with a clean 400
        # instead of letting int()/float() raise inside the handler, which
        # would surface as a 500 echoing the exception text.  JSON's
        # ``Infinity`` / ``1e400`` parse to float infinity and ``int()`` on
        # those raises OverflowError (not TypeError/ValueError) — without it
        # in this tuple such a request leaked a 500 with internal error text.
        raise BadRequestError(
            "top_n, ngram_size, num_permutations must be whole numbers; "
            "threshold and jaccard_weight must be numbers"
        ) from exc
    # Range-check the threshold up front: :class:`ResemblLSH` rejects values
    # outside [0.0, 1.0] anyway, so without this every out-of-range request
    # surfaced as a 500 leaking that internal error instead of a clean one.
    # (NaN fails both comparisons and is rejected too.)
    effective_threshold = threshold if threshold is not None else LSH_THRESHOLD
    if not 0.0 <= effective_threshold <= 1.0:
        raise BadRequestError(f"threshold {effective_threshold} is not in [0.0, 1.0]")
    # Same range rule as the threshold: score_hybrid documents the weight as a
    # 0-1 balance, and an unvalidated NaN/Infinity weight made every hybrid
    # score NaN — corrupting the top-n ranking comparisons (NaN never
    # compares) and serializing as a bare ``NaN`` token, which no external
    # JSON parser accepts.  (NaN fails both comparisons and is rejected too.)
    if not 0.0 <= jaccard_weight <= 1.0:
        raise BadRequestError(f"jaccard_weight {jaccard_weight} is not in [0.0, 1.0]")
    # Bound the request-supplied permutation count before anything derives
    # state from it: fingerprint construction and banding allocate memory
    # proportional to *num_permutations*, and ``minhash_new`` caches one
    # MinHash template per distinct count for the life of the process.
    # Unbounded, a script cycling values grows the warm server without
    # limit (and a single absurd value tries a multi-GB allocation).
    # The cap is the same one used to validate stored blobs
    # (``scoring.MAX_NUM_PERM``, "real configurations use 64-128").
    from .scoring import MAX_NUM_PERM

    if not 2 <= num_permutations <= MAX_NUM_PERM:
        raise BadRequestError(f"num_permutations must be between 2 and {MAX_NUM_PERM}")
    # Same degenerate-fingerprint guard as the CLI's find validation: an
    # ``ngram_size`` below 1 does not crash, it silently makes every snippet
    # match every other one (all shingles collapse to the empty token tuple).
    if ngram_size < 1:
        raise BadRequestError("ngram_size must be at least 1")
    # The response is bounded too, not just the work: a top_n large enough to
    # name the whole corpus turns one unauthenticated request into a complete
    # export of the snippet table.
    if top_n > _MAX_TOP_N:
        raise BadRequestError(f"top_n must be at most {_MAX_TOP_N}")
    # Reject an unbuildable threshold up front: the banding needs b >= 2
    # bands, and an unbuildable one would make the find return zero matches
    # silently.  (The thin client cannot run the banding search without
    # losing its ~50 ms startup, so the server is the right place.)
    from .lsh import banding_params

    try:
        bands, _ = banding_params(effective_threshold, num_permutations)
    except ValueError:
        bands = 1
    if bands < 2:
        raise BadRequestError(
            f"threshold {effective_threshold} is too high for "
            f"{num_permutations} permutations (fewer than 2 bands)"
        )
    # The served index is built once, at startup, for a single
    # (threshold, ngram_size, num_permutations) triple; a request naming a
    # different one cannot be answered from it.  Honouring it would rebuild
    # the whole index from inside a request thread — dropping the shared
    # ``lsh_bucket`` table and repopulating it, or reindexing every
    # fingerprint — while the other handler threads query that same table.
    # Two such requests interleave into an index that ``lsh_meta`` advertises
    # as complete while most of its rows are missing, so every later find
    # silently returns a fraction of its matches and nothing ever rebuilds
    # it.  Answering 400 keeps the warm process read-only; the parameters
    # change with a restart.
    from .lsh import lsh_meta_matches

    if ngram_size != params.ngram_size or not lsh_meta_matches(
        (params.lsh_threshold, params.num_permutations), effective_threshold, num_permutations
    ):
        raise BadRequestError(
            "this server searches with threshold "
            f"{params.lsh_threshold}, ngram_size {params.ngram_size} and "
            f"num_permutations {params.num_permutations}; restart it with "
            "those settings to search differently"
        )
    return _FindRequest(
        top_n=top_n,
        threshold=threshold,
        normalize=normalize,
        ngram_size=ngram_size,
        num_permutations=num_permutations,
        jaccard_weight=jaccard_weight,
        effective_threshold=effective_threshold,
    )


def _find_one(
    session: Session,
    body: dict,
    query: str,
    params: ResemblConfig | None = None,
) -> dict:
    """Run one find, served from the version-guarded cache when possible.

    Raises :class:`BadRequestError` for unusable parameters; handlers turn
    that into the 400 error envelope.
    """
    params = params if params is not None else _DEFAULT_FIND_PARAMS
    request = _parse_find_request(body, params)
    top_n = request.top_n
    threshold = request.threshold
    normalize = request.normalize
    ngram_size = request.ngram_size
    num_permutations = request.num_permutations
    jaccard_weight = request.jaccard_weight
    # The masked URL identifies the served database without retaining
    # credentials in the long-lived cache keys.
    db_id = str(_session_engine(session).url)
    key = (
        _query_cache_key(query),
        top_n,
        threshold,
        normalize,
        ngram_size,
        num_permutations,
        jaccard_weight,
        db_id,
    )
    version = _db_version(session)
    if version is None:
        # No version counter for this backend: the cache cannot tell a stale
        # entry from a current one, so the find always runs.
        return _find_uncached(
            session,
            query,
            top_n,
            threshold,
            normalize,
            ngram_size,
            num_permutations,
            jaccard_weight,
        )
    cached = _result_cache_get(key, version)
    if cached is not None:
        return cached
    with _inflight(key):
        # Re-sampled, not carried over: the wait above let the thread ahead
        # run a full find, and a write committed during it.  A payload
        # computed for the older version would be filed under it and thrown
        # away on the next read, and a cached payload for that version may
        # describe rows the database has since changed.
        version = _db_version(session)
        if version is None:  # pragma: no cover - the dialect cannot change mid-call
            return _find_uncached(
                session,
                query,
                top_n,
                threshold,
                normalize,
                ngram_size,
                num_permutations,
                jaccard_weight,
            )
        cached = _result_cache_get(key, version)
        if cached is not None:
            return cached
        payload = _find_uncached(
            session,
            query,
            top_n,
            threshold,
            normalize,
            ngram_size,
            num_permutations,
            jaccard_weight,
        )
        _result_cache_put(key, version, payload)
        return payload


def _find_uncached(
    session: Session,
    query: str,
    top_n: int,
    threshold: float | None,
    normalize: bool,
    ngram_size: int,
    num_permutations: int,
    jaccard_weight: float,
) -> dict:
    """Run one find and serialize it, bypassing the result cache."""
    num_candidates, matches = snippet_find_matches(
        session,
        query,
        top_n=top_n,
        threshold=threshold,
        normalize=normalize,
        ngram_size=ngram_size,
        num_permutations=num_permutations,
        jaccard_weight=jaccard_weight,
    )
    return snippet_matches_payload(num_candidates, matches)


def _result_cache_get(key: tuple, version: int) -> dict | None:
    """Return the cached payload for *key* if it was built at *version*."""
    with _RESULT_CACHE_LOCK:
        entry = _RESULT_CACHE.get(key)
        if entry is None or entry[0] != version:
            return None
        _RESULT_CACHE.move_to_end(key)
        return entry[1]


def _result_cache_put(key: tuple, version: int, payload: dict) -> None:
    """Store *payload* for *key* at *version*, evicting past the cap."""
    with _RESULT_CACHE_LOCK:
        _RESULT_CACHE[key] = (version, payload)
        _RESULT_CACHE.move_to_end(key)
        while len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
            _RESULT_CACHE.popitem(last=False)


#: Maximum accepted request body (8 MiB — orders of magnitude above any real
#: find/batch payload).  A bound keeps a local process from making the server
#: allocate unbounded memory per request, and negative or non-numeric
#: Content-Length values are rejected instead of hanging the handler thread
#: reading until EOF.
_MAX_BODY_BYTES = 8 * 1024 * 1024

#: Maximum number of queries one ``/find-batch`` request may carry.  The
#: endpoint runs one find per entry on a single connection, so an unbounded
#: list turns one request into unbounded work; callers with more split the
#: input (the CLI's ``find-batch`` reads a file of any size, in chunks).
_MAX_BATCH_QUERIES = 1000

#: Largest ``top_n`` one request may ask for.  Every other find parameter is
#: range-checked, and without this one ``POST /find`` answers the whole corpus:
#: ``top_n`` is the only parameter that grows the response itself (one row per
#: match) rather than the work behind it, so an unauthenticated request naming
#: a huge value walks every LSH candidate, Levenshtein-scores it, and returns
#: it.  A caller that wants more splits its query.  ``--top-n`` is not capped
#: to match, since its config range has no upper bound: a larger value is
#: answered 400 and the CLI falls back to its in-process path.
_MAX_TOP_N = 1000

#: Host names a loopback bind answers to.  A request carrying any other
#: ``Host`` reached a loopback port the browser resolved by rebinding an
#: attacker-controlled name onto 127.0.0.1, so the header, not the peer
#: address, is what identifies the origin of such a request.
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")


def _loopback_allowed_hosts(host: str, port: int) -> frozenset[str]:
    """Return the ``Host`` values a server bound to *host* may answer.

    Empty for any bind that is not loopback: a server on a routable interface
    is reached by whatever name resolves to it, which the server cannot know
    in advance, and a check would refuse legitimate clients instead of
    protecting them.  A loopback bind is only ever addressed by the names
    below, so the check costs nothing there and closes the rebinding path.
    """
    is_loopback = host.lower() == "localhost"
    if not is_loopback:
        try:
            import ipaddress

            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        return frozenset()
    allowed: set[str] = set()
    for name in _LOOPBACK_NAMES:
        # HTTP/1.1 carries the port in the header (all but 80), and an IPv6
        # literal is bracketed there; the bare form covers a client that omits
        # the port.
        literal = f"[{name}]" if ":" in name else name
        allowed.update({f"{literal}:{port}", literal, f"{name}:{port}"})
    return frozenset(allowed)


#: C0 and DEL, dropped from a logged request line so a crafted path cannot
#: end the record and append forged lines to the log.
_LOG_CONTROL_CHARS = dict.fromkeys([*range(0x20), 0x7F])


def port_file_cleanup(port_file: str, port: int) -> None:
    """Remove the port file only when it still advertises our own *port*.

    Shared by the server's exit path (:func:`serve`) and any client that
    verified the advertised port is dead (``cli._server_request``): the
    double-serve check in :func:`serve` is not atomic across processes, two
    ``serve`` invocations started close together both pass it (neither has
    written yet), both bind, and the last writer owns the advertisement.
    An unconditional delete on exit would then orphan the surviving server
    for every ``find`` client, so the file is removed only while its
    content is still the known *port*.
    """
    try:
        with open(port_file, encoding="utf-8") as f:
            if f.read().strip() != str(port):
                return
        os.remove(port_file)
    except OSError:
        pass


class _FindHandler(BaseHTTPRequestHandler):
    """Serves ``POST /find`` and ``POST /find-batch``; one session per request
    (concurrent reads).  Also carries this server's 403, 404, 405 and 415
    answers, which need no database."""

    # HTTP/1.1 enables keep-alive: well-behaved clients reuse the connection
    # instead of opening a fresh one per request, which cut measured
    # connection-reset errors under concurrent load from ~24 to ~3 (the
    # resets come from connection churn, not request logic — a churned
    # close under GIL contention surfaces as RST).  The idle timeout bounds
    # how long a keep-alive connection can hold its handler thread.
    protocol_version = "HTTP/1.1"
    timeout = 30

    @property
    def engine(self) -> Any:
        """The request engine, owned by the serving :class:`ThreadingHTTPServer`.

        It lives on the server *instance*, not on this handler class: two
        ``serve()`` calls in one process must never retarget an older,
        still-serving server's handler threads to the newer engine.
        """
        return self.server.engine  # type: ignore[attr-defined]

    @property
    def find_defaults(self) -> ResemblConfig:
        """This server's configured find defaults (see :func:`serve`)."""
        # Lives on the server instance, like ``engine`` above.
        return self.server.find_defaults  # type: ignore[attr-defined]

    def _read_body(self) -> dict | None:
        """Read and parse the JSON request body; None if malformed.

        A well-formed JSON document that is not an object (``[]``, ``5``,
        ``"x"``) is malformed for this API: the handlers index it by
        ``body["query"]`` / ``body["queries"]``, which a list or a number
        answers with a ``TypeError`` that would surface as a 500.

        Deeply nested documents are malformed too: the JSON decoder
        recurses once per nesting level, so a body of a few hundred
        kilobytes of ``[[[...]]]`` raised ``RecursionError``, which is not
        a ``ValueError`` and so escaped this guard and the handler's own
        ``except`` alike, killing the thread without a response.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, KeyError, RecursionError):
            return None
        return body if isinstance(body, dict) else None

    def _content_type_ok(self) -> bool:
        """Whether the request declares a JSON body.

        A missing ``Content-Type`` is accepted (the body is parsed as JSON
        either way); an explicit non-JSON media type is refused rather than
        silently parsed.
        """
        declared = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return declared in ("", "application/json")

    def _host_allowed(self) -> bool:
        """Whether the request's ``Host`` names this loopback server.

        A browser on the host is a second peer, and the loopback bind does not
        stop it: a page that rebinds its own name onto 127.0.0.1 sends a
        same-origin request (no CORS preflight, and the response is readable
        because the origin matches) to an unauthenticated endpoint.  The
        ``Host`` header still carries the rebound name, so refusing anything
        but the names the server answers to closes that path.  A non-loopback
        bind is unrestricted (see :func:`_loopback_allowed_hosts`).
        """
        allowed = self.server.allowed_hosts  # type: ignore[attr-defined]
        if not allowed:
            return True
        return (self.headers.get("Host") or "").strip().lower() in allowed

    def do_POST(self) -> None:  # pylint: disable=invalid-name; http.server API
        if not self._host_allowed():
            self._respond(403, {"error": "host not allowed"})
            return
        if self.path not in ("/find", "/find-batch"):
            self._respond(404, {"error": f"unknown path: {self.path}"})
            return
        if not self._content_type_ok():
            self._respond(415, {"error": "content type must be application/json"})
            return
        body = self._read_body()
        if body is None:
            self._respond(400, {"error": "bad request body"})
            return
        if self.path == "/find":
            self._handle_find(body)
            return
        self._handle_find_batch(body)

    def _method_not_allowed(self) -> None:
        """Answer a non-POST request with the same JSON error envelope."""
        if not self._host_allowed():
            self._respond(403, {"error": "host not allowed"})
            return
        self._respond(
            405,
            {"error": f"method not allowed: {self.command} (use POST)"},
            extra_headers={"Allow": "POST"},
        )

    def do_GET(self) -> None:  # pylint: disable=invalid-name; http.server API
        self._method_not_allowed()

    def do_PUT(self) -> None:  # pylint: disable=invalid-name; http.server API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # pylint: disable=invalid-name; http.server API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # pylint: disable=invalid-name; http.server API
        self._method_not_allowed()

    def _handle_find(self, body: dict) -> None:
        try:
            query = body["query"]
        except KeyError:
            self._respond(400, {"error": "bad request: 'query' is required"})
            return
        # Same type rule as /find-batch's per-query check: a non-string query
        # (dict, number, list) would otherwise crash the lexer downstream and
        # answer 500 with the internal exception text instead of a clean 400.
        if not isinstance(query, str):
            self._respond(400, {"error": "bad request: query must be a string"})
            return
        try:
            with Session(self.engine) as session:
                payload = _find_one(session, body, query, self.find_defaults)
        except BadRequestError as exc:
            # Parameter validation: the caller's request, not the server.
            self._respond(400, {"error": str(exc)})
            return
        except Exception:  # pragma: no cover - defensive
            # A long-lived serve process prints nothing per request (see
            # ``log_message``): without this record, 500s are completely
            # unobserved and an on-call operator has no trace to debug.
            logger.exception("POST %s failed", self.path)
            # The full exception text stays in the log only: driver and ORM
            # messages carry SQL fragments, bind parameters, and file paths,
            # which must not reach the client.
            self._respond(500, {"error": "internal server error"})
            return
        self._respond(200, payload)

    def _handle_find_batch(self, body: dict) -> None:
        """Process many queries in one request (results keyed by query)."""
        try:
            queries = body["queries"]
        except KeyError:
            self._respond(400, {"error": "bad request: 'queries' is required"})
            return
        # A bare string would iterate per character below, silently turning
        # one malformed request into one single-character find per letter.
        if not isinstance(queries, list):
            self._respond(400, {"error": "bad request: queries must be a list"})
            return
        if len(queries) > _MAX_BATCH_QUERIES:
            self._respond(
                400,
                {"error": f"queries must hold at most {_MAX_BATCH_QUERIES} entries"},
            )
            return
        # Validated once for the whole batch: a parameter error concerns the
        # request, not any one query, so repeating it in every entry (as the
        # per-query path did) told the caller nothing extra and cost N finds.
        try:
            _parse_find_request(body, self.find_defaults)
        except BadRequestError as exc:
            self._respond(400, {"error": str(exc)})
            return
        results: list[dict] = []
        try:
            with Session(self.engine) as session:
                for query in queries:
                    # Same type rule as /find's boundary check, answered
                    # without raising so the controlled message reaches the
                    # client while unexpected failures cannot.
                    if not isinstance(query, str):
                        results.append({"query": query, "error": "query must be a string"})
                        continue
                    try:
                        results.append(
                            {"query": query, **_find_one(session, body, query, self.find_defaults)}
                        )
                    except BadRequestError as exc:
                        # A query the caller controls (empty string, say) is a
                        # bad request, not a server fault: answered per query
                        # so the rest of the batch still completes.
                        results.append({"query": query, "error": str(exc)})
                    except Exception as exc:  # isolate per-query failures
                        logger.warning("find-batch query %.200r failed: %s", query, exc)
                        # Like the 500 path below: the exception text (SQL,
                        # paths) is for the log, not the wire.
                        results.append(
                            {"query": query, "error": "internal error while processing this query"}
                        )
        except Exception:
            # Malformed container or session/pool failure — answer 500 rather
            # than dropping the connection with a handler-thread traceback.
            logger.exception("POST %s failed", self.path)
            self._respond(500, {"error": "internal server error"})
            return
        self._respond(200, {"results": results})

    def _respond(
        self,
        status: int,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        # The payload is always JSON; forbid content-type sniffing so a
        # response can never be reinterpreted as HTML/script by a browser
        # pointed at the endpoint.
        self.send_header("X-Content-Type-Options", "nosniff")
        # Defense in depth for a browser pointed at the endpoint: deny
        # framing and script/style/object sources outright, and keep cached
        # copies of snippet-derived responses out of intermediary caches.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(
        self,
        format: str,  # noqa: A002  # pylint: disable=redefined-builtin
        *args: Any,
    ) -> None:
        # Quiet at the default level: the CLI prints its own status line.  At
        # DEBUG (``resembl serve -v``) every request is recorded with its peer
        # and outcome, which is the only trail a served query leaves: without
        # it a scrape through this unauthenticated endpoint leaves nothing to
        # investigate.  The request line is request-controlled, so control
        # characters are stripped from the rendered record: a raw one lets a
        # crafted path write a second, forged log line.
        peer = self.client_address[0]
        record = (format % args).translate(_LOG_CONTROL_CHARS)
        logger.debug("%s %s", peer, record)


class _FindServer(ThreadingHTTPServer):
    """The serving :class:`ThreadingHTTPServer`; owns and disposes the engine.

    ``server_close`` releases the engine's pooled DB connections instead of
    leaving them to interpreter exit: a stopped server generation must not
    pin up to ``pool_size + max_overflow`` SQLite handles in a process that
    starts and stops servers repeatedly (tests, embedded callers).
    """

    engine: Any
    #: ``Host`` values this generation answers to; empty means unrestricted
    #: (a non-loopback bind — see :func:`_loopback_allowed_hosts`).
    allowed_hosts: frozenset[str] = frozenset()
    #: This generation's exit-hook callback, set by :func:`serve`.
    #: ``server_close`` runs it (retiring the port file) and unregisters it,
    #: so a process cycling servers accumulates neither stale advertisements
    #: nor exit handlers.  It is a per-generation ``functools.partial``
    #: because ``atexit.unregister`` matches by callable alone — sharing one
    #: bare function would let one close drop every server's registration.
    _atexit_cleanup: functools.partial[None]

    #: Per-server find defaults; every instance overwrites this class-level
    #: fallback in :func:`serve` (see there for why it cannot be a module
    #: global).
    find_defaults: ResemblConfig = _DEFAULT_FIND_PARAMS

    def set_atexit_cleanup(self, cleanup: functools.partial[None]) -> None:
        """Attach this generation's exit hook (called by :func:`serve`).

        Kept as a method so callers never poke the protected attribute from
        outside the class.
        """
        self._atexit_cleanup = cleanup

    def server_close(self) -> None:
        super().server_close()
        engine = getattr(self, "engine", None)
        if engine is not None:
            # The result cache's per-database version probe holds a second
            # connection to this same database; releasing it with the engine
            # it was created for keeps the handle from outliving the
            # generation that opened it (see :func:`_version_probe_release`).
            _version_probe_release(engine)
            # Checked-out connections still finish their request and are
            # closed on return; idle pooled connections close now.
            engine.dispose()
        cleanup = getattr(self, "_atexit_cleanup", None)
        if cleanup is not None:
            # This generation is stopping: retire its advertisement now (only
            # while it still names our own port) and release its exit hook so
            # repeated serve/close cycles do not accumulate handlers.
            cleanup()
            atexit.unregister(cleanup)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Swallow routine client-disconnection failures; delegate the rest.

        A client that hangs up mid-response (or mid-keep-alive read) surfaces
        as ``ConnectionError`` from ``_respond`` — routine under connection
        churn, and the default handler would print a full traceback per
        disconnect.  Anything unexpected still reaches the default handler
        so real bugs stay visible.
        """
        if isinstance(sys.exc_info()[1], ConnectionError | TimeoutError):
            return
        super().handle_error(request, client_address)


def serve(db_url: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Start the find server for *db_url* and return the bound HTTP server.

    The fingerprint migration (if any) and the LSH index build run once at
    startup so serving is read-only; the port file is written on startup and
    removed on exit.
    """
    from .database import create_db_engine

    # Refuse to double-serve: if a port file exists for this database and a
    # server is actually listening on it, another ``serve`` is already
    # running.  Starting a second one would silently orphan the first — both
    # bind different auto-ports, and find clients use the port file, which
    # the last starter overwrites.  (A stale port file whose port is dead is
    # ignored and replaced.)
    port_file = server_port_path(db_url, cache_dir_get())
    try:
        with open(port_file, encoding="utf-8") as f:
            existing_port = int(f.read().strip())
    except (OSError, ValueError):
        existing_port = None  # no port file, or malformed
    if existing_port is not None:
        try:
            with socket.create_connection(("127.0.0.1", existing_port), timeout=1):
                raise ValueError(
                    "another serve process is already running for this "
                    f"database (port {existing_port})"
                )
        except OSError:
            pass  # stale port file — nothing listening; we'll replace it

    # Larger than the default pool: requests run one thread per connection,
    # and the default (5 + 10 overflow) was exhausted under concurrent load,
    # timing requests out after 30s.  SQLite in WAL mode handles many
    # concurrent readers fine.
    engine = create_db_engine(db_url, pool_size=32, max_overflow=64)
    try:
        with Session(engine) as session:
            # Honor the CLI config: the server answers with the same threshold /
            # permutation count as in-process find, so clients using the same
            # config get warm cache hits instead of per-request rebuilds.  The
            # values are carried on the server *instance* (like ``engine``):
            # module globals would leak into any older server still serving
            # another database in this process.
            from .config import load_config

            cfg = load_config()
            find_defaults = ResemblConfig(
                lsh_threshold=cfg.lsh_threshold,
                top_n=cfg.top_n,
                num_permutations=cfg.num_permutations,
                ngram_size=cfg.ngram_size,
                jaccard_weight=cfg.jaccard_weight,
            )

            # One-time migration + index build, before any request is served.
            # The migration worker count scales with the database (spawning a
            # worker per CPU for a small database costs more than the work).
            from .core import adaptive_worker_count, fingerprints_need_reindex

            if fingerprints_need_reindex(session, cfg.ngram_size, cfg.num_permutations):
                from sqlmodel import func, select

                from .models import Snippet

                num_snippets = session.exec(
                    select(func.count(Snippet.checksum))  # type: ignore[arg-type]
                ).one()
                reindex_result = db_reindex(
                    session,
                    jobs=adaptive_worker_count(num_snippets, os.cpu_count() or 1),
                    ngram_size=cfg.ngram_size,
                    num_perm=cfg.num_permutations,
                )
                if "error" in reindex_result:
                    # Serving over the unmigrated fingerprints would let the
                    # index build below restamp them as current, silently
                    # masking the migration; fail the startup instead (the
                    # enclosing `except BaseException` disposes the engine).
                    raise IndexBuildError(reindex_result["error"])
            # Build the index only if it is missing or was built with different
            # parameters — rebuilding an already-current index on every restart
            # would make serve startup pay the full build (~2 min at 500k) each
            # time, which bites under process managers that restart often.
            from .lsh import lsh_meta_get, lsh_meta_matches

            meta = lsh_meta_get(session)
            if not lsh_meta_matches(meta, cfg.lsh_threshold, cfg.num_permutations):
                lsh_index_build(session, cfg.lsh_threshold, cfg.num_permutations)
    except BaseException:
        # A warm-up failure (e.g. a database error during the migration
        # reindex or index build) must not leak the engine either: it holds
        # up to pool_size + max_overflow SQLite handles once warmed, and this
        # process keeps running (embedded callers, test harnesses) after
        # serve raises.  Same contract as the bind-failure dispose below.
        engine.dispose()
        raise

    # A failed bind (port already in use) must not leak the engine: it holds
    # up to pool_size + max_overflow SQLite handles once warmed, and this
    # process keeps running (embedded callers, test harnesses) after serve
    # raises.
    try:
        httpd = _FindServer((host, port), _FindHandler)
    except BaseException:
        engine.dispose()
        raise
    # Per-instance shared state (see _FindHandler.engine): each server
    # generation carries its own engine and find defaults.
    httpd.engine = engine
    httpd.find_defaults = find_defaults
    httpd.allowed_hosts = _loopback_allowed_hosts(host, int(httpd.server_address[1]))
    tmp_port_path = f"{port_file}.{os.getpid()}.tmp"
    try:
        # 0o700 at creation: the cache dir holds the advertisement, and both
        # clients trust its contents to name the port they send their query
        # to, so nothing in it should be readable or writable by another
        # account on a shared host.  An existing directory keeps whatever
        # permissions the user gave it.
        os.makedirs(os.path.dirname(port_file), mode=0o700, exist_ok=True)
        # Publish via write-temp-then-rename: ``open(port_file, "w")``
        # truncates in place, so a find client racing this write could read
        # an empty or half-written port number and wrongly conclude no
        # server is running.  ``os.replace`` flips the whole advertisement
        # atomically (POSIX rename semantics; also atomic on Windows), and
        # a same-directory temp keeps the rename on one filesystem.  The
        # temp is created 0o600 rather than with the process umask, so the
        # advertised port is not world-readable even before the rename.
        fd = os.open(tmp_port_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(str(httpd.server_address[1]))
        os.replace(tmp_port_path, port_file)
    except BaseException:
        # A failed advertisement must not leak the bound server and its
        # warm engine pool (same reasoning as the bind-failure dispose
        # above): ``server_close`` disposes the engine.  The temp is
        # removed only here — after a successful replace it no longer
        # exists, and deleting it then would unlink the live port file.
        try:
            os.remove(tmp_port_path)
        except OSError:
            pass
        httpd.server_close()
        raise

    # The advertisement's lifecycle is owned by the server instance: closing
    # it retires the file (while it still names our port) and releases this
    # registration, so repeated serve/close cycles do not accumulate exit
    # handlers.  The hook remains only as a backstop for callers that never
    # close the returned server.
    port = int(httpd.server_address[1])
    # The configuration this generation actually serves with, on one line at
    # DEBUG (`resembl serve -v`): the served database (password masked), where
    # the advertisement lives, and the find defaults every request that omits
    # a parameter inherits.  A long-lived process otherwise gives an operator
    # no way to tell which config file it read at startup.
    logger.debug(
        "serving %s on %s:%d (port file %s), find defaults %s",
        db_url_mask(db_url),
        host,
        port,
        port_file,
        ", ".join(f"{k}={v}" for k, v in find_defaults.items()),
    )
    cleanup = functools.partial(port_file_cleanup, port_file, port)
    httpd.set_atexit_cleanup(cleanup)
    atexit.register(cleanup)
    return httpd
