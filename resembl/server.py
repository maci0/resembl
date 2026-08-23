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
import os
import socket
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from sqlalchemy.engine import Engine
from sqlmodel import Session

from .cache import cache_dir_get, lsh_index_build
from .config import ResemblConfig
from .core import LSH_THRESHOLD, db_reindex, snippet_find_matches

#: Version-guarded result cache: key -> (db_version, payload).  SQLite's
#: ``PRAGMA data_version`` increments on every commit, so a cached result is
#: returned only while the database is unchanged — repeated queries (triage
#: workflows re-checking the same hashes) answer in ~0.1 ms instead of
#: ~1.4 ms, never stale.  Non-SQLite backends get no version counter and
#: bypass the cache.  The key ends with the serving engine's URL: one
#: process may run several servers for different databases (tests,
#: embeddings), and ``data_version`` is per-database — without that final
#: component a hit computed for database A could be served to database B
#: whenever both counters happened to carry the same value.
_RESULT_CACHE: OrderedDict[tuple, tuple[int | None, dict]] = OrderedDict()
_RESULT_CACHE_MAX = 128
#: Serializes access to the shared cache: requests run in concurrent
#: handler threads, and OrderedDict is not thread-safe.
_RESULT_CACHE_LOCK = threading.Lock()

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
    """Return a DB-change counter for cache invalidation (SQLite only)."""
    if session.get_bind().dialect.name != "sqlite":
        return None
    from sqlmodel import text

    return int(session.execute(text("PRAGMA data_version")).scalar() or 0)


def _find_one(
    session: Session,
    body: dict,
    query: str,
    params: ResemblConfig | None = None,
) -> dict:
    """Run one find, served from the version-guarded cache when possible.

    *params* supplies the serving server's configured defaults for values
    absent from *body*; direct callers without a server get plain
    :class:`ResemblConfig` defaults.
    """
    params = params if params is not None else _DEFAULT_FIND_PARAMS
    top_n = int(body.get("top_n", params.top_n))
    threshold = body.get("threshold")
    normalize = bool(body.get("normalize", True))
    ngram_size = int(body.get("ngram_size", params.ngram_size))
    num_permutations = int(body.get("num_permutations", params.num_permutations))
    jaccard_weight = float(body.get("jaccard_weight", params.jaccard_weight))
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
        return {"error": f"num_permutations must be between 2 and {MAX_NUM_PERM}"}
    # Same degenerate-fingerprint guard as the CLI's find validation: an
    # ``ngram_size`` below 1 does not crash, it silently makes every snippet
    # match every other one (all shingles collapse to the empty token tuple).
    if ngram_size < 1:
        return {"error": "ngram_size must be at least 1"}
    # The masked URL identifies the served database without retaining
    # credentials in the long-lived cache keys.
    db_id = str(_session_engine(session).url)
    key = (
        query,
        top_n,
        threshold,
        normalize,
        ngram_size,
        num_permutations,
        jaccard_weight,
        db_id,
    )
    version = _db_version(session)
    if version is not None:
        with _RESULT_CACHE_LOCK:
            entry = _RESULT_CACHE.get(key)
            if entry is not None and entry[0] == version:
                _RESULT_CACHE.move_to_end(key)
                return entry[1]
    # Reject unbuildable thresholds up front: the banding needs b >= 2
    # bands, and an unbuildable one would make the find return zero matches
    # silently.  (The thin client cannot run the scipy banding check without
    # losing its ~50 ms startup, so the server is the right place.)
    from .lsh import banding_params

    effective_threshold = threshold if threshold is not None else LSH_THRESHOLD
    try:
        bands, _ = banding_params(effective_threshold, num_permutations)
    except ValueError:
        bands = 1
    if bands < 2:
        return {
            "error": f"threshold {effective_threshold} is too high for "
            f"{num_permutations} permutations (fewer than 2 bands)"
        }
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
    payload = {
        "lsh_candidates": num_candidates,
        "matches": [
            {"checksum": s.checksum, "names": s.name_list, "score": score} for s, score in matches
        ],
    }
    if version is not None:
        with _RESULT_CACHE_LOCK:
            _RESULT_CACHE[key] = (version, payload)
            _RESULT_CACHE.move_to_end(key)
            while len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
                _RESULT_CACHE.popitem(last=False)
    return payload


def server_port_path(db_url: str) -> str:
    """Return the port-file path for a database URL."""
    digest = hashlib.sha1(db_url.encode("utf-8")).hexdigest()[:12]
    return os.path.join(cache_dir_get(), f"server_{digest}.port")


#: Maximum accepted request body (8 MiB — orders of magnitude above any real
#: find/batch payload).  A bound keeps a local process from making the server
#: allocate unbounded memory per request, and negative or non-numeric
#: Content-Length values are rejected instead of hanging the handler thread
#: reading until EOF.
_MAX_BODY_BYTES = 8 * 1024 * 1024


def _port_file_cleanup(port_file: str, port: int) -> None:
    """Remove the port file only when it still advertises our own *port*.

    The double-serve check in :func:`serve` is not atomic across processes:
    two ``serve`` invocations started close together both pass it (neither
    has written yet), both bind, and the last writer owns the advertisement.
    An unconditional delete on exit would then orphan the surviving server
    for every ``find`` client, so an exiting process removes the file only
    while its content is still its own bound port.
    """
    try:
        with open(port_file, encoding="utf-8") as f:
            if f.read().strip() != str(port):
                return
        os.remove(port_file)
    except OSError:
        pass


class _FindHandler(BaseHTTPRequestHandler):
    """Serves ``POST /find``; one session per request (concurrent reads)."""

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
        """Read and parse the JSON request body; None if malformed."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, KeyError):
            return None

    def do_POST(self) -> None:  # pylint: disable=invalid-name; http.server API
        body = self._read_body()
        if body is None:
            self._respond(400, {"error": "bad request body"})
            return
        if self.path == "/find":
            self._handle_find(body)
            return
        if self.path == "/find-batch":
            self._handle_find_batch(body)
            return
        self.send_error(404, "Not Found")

    def _handle_find(self, body: dict) -> None:
        try:
            query = body["query"]
        except KeyError as exc:
            self._respond(400, {"error": f"bad request: {exc}"})
            return
        try:
            with Session(self.engine) as session:
                payload = _find_one(session, body, query, self.find_defaults)
        except Exception as exc:  # pragma: no cover - defensive
            self._respond(500, {"error": str(exc)})
            return
        self._respond(200, payload)

    def _handle_find_batch(self, body: dict) -> None:
        """Process many queries in one request (results keyed by query)."""
        try:
            queries = body["queries"]
        except KeyError as exc:
            self._respond(400, {"error": f"bad request: {exc}"})
            return
        results: list[dict] = []
        try:
            with Session(self.engine) as session:
                for query in queries:
                    try:
                        if not isinstance(query, str):
                            raise ValueError("query must be a string")
                        results.append(
                            {"query": query, **_find_one(session, body, query, self.find_defaults)}
                        )
                    except Exception as exc:  # isolate per-query failures
                        results.append({"query": query, "error": str(exc)})
        except Exception as exc:
            # Malformed container or session/pool failure — answer 500 rather
            # than dropping the connection with a handler-thread traceback.
            self._respond(500, {"error": str(exc)})
            return
        self._respond(200, {"results": results})

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(
        self, format: str, *args: Any  # noqa: A002  # pylint: disable=redefined-builtin
    ) -> None:
        # Quiet by default; the CLI prints its own status line.
        return


class _FindServer(ThreadingHTTPServer):
    """The serving :class:`ThreadingHTTPServer`; owns and disposes the engine.

    ``server_close`` releases the engine's pooled DB connections instead of
    leaving them to interpreter exit: a stopped server generation must not
    pin up to ``pool_size + max_overflow`` SQLite handles in a process that
    starts and stops servers repeatedly (tests, embeddings).
    """

    engine: Any
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
    port_file = server_port_path(db_url)
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
            db_reindex(
                session,
                jobs=adaptive_worker_count(num_snippets, os.cpu_count() or 1),
                ngram_size=cfg.ngram_size,
                num_perm=cfg.num_permutations,
            )
        # Build the index only if it is missing or was built with different
        # parameters — rebuilding an already-current index on every restart
        # would make serve startup pay the full build (~2 min at 500k) each
        # time, which bites under process managers that restart often.
        from .lsh import lsh_meta_get, lsh_meta_matches

        meta = lsh_meta_get(session)
        if not lsh_meta_matches(meta, cfg.lsh_threshold, cfg.num_permutations):
            lsh_index_build(session, cfg.lsh_threshold, cfg.num_permutations)

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
    tmp_port_path = f"{port_file}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(port_file), exist_ok=True)
        # Publish via write-temp-then-rename: ``open(port_file, "w")``
        # truncates in place, so a find client racing this write could read
        # an empty or half-written port number and wrongly conclude no
        # server is running.  ``os.replace`` flips the whole advertisement
        # atomically (POSIX rename semantics; also atomic on Windows), and
        # a same-directory temp keeps the rename on one filesystem.
        with open(tmp_port_path, "w", encoding="utf-8") as f:
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
    cleanup = functools.partial(_port_file_cleanup, port_file, port)
    httpd.set_atexit_cleanup(cleanup)
    atexit.register(cleanup)
    return httpd
