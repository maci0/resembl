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

import hashlib
import json
import os
import socket
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sqlmodel import Session

from .cache import cache_dir_get, lsh_index_build
from .core import LSH_THRESHOLD, NUM_PERMUTATIONS, db_reindex, snippet_find_matches
from .lsh import fingerprint_version_get
from .models import FINGERPRINT_VERSION

#: Version-guarded result cache: key -> (db_version, payload).  SQLite's
#: ``PRAGMA data_version`` increments on every commit, so a cached result is
#: returned only while the database is unchanged — repeated queries (triage
#: workflows re-checking the same hashes) answer in ~0.1 ms instead of
#: ~1.4 ms, never stale.  Non-SQLite backends get no version counter and
#: bypass the cache.
_RESULT_CACHE: "OrderedDict[tuple, tuple[int | None, dict]]" = OrderedDict()
_RESULT_CACHE_MAX = 128
#: Serializes access to the shared cache: requests run in concurrent
#: handler threads, and OrderedDict is not thread-safe.
_RESULT_CACHE_LOCK = threading.Lock()


def _db_version(session: Session) -> int | None:
    """Return a DB-change counter for cache invalidation (SQLite only)."""
    if session.get_bind().dialect.name != "sqlite":
        return None
    from sqlmodel import text

    return int(session.execute(text("PRAGMA data_version")).scalar() or 0)


def _find_one(session: Session, body: dict, query: str) -> dict:
    """Run one find, served from the version-guarded cache when possible."""
    key = (
        query,
        int(body.get("top_n", 5)),
        body.get("threshold"),
        bool(body.get("normalize", True)),
        int(body.get("ngram_size", 3)),
    )
    version = _db_version(session)
    if version is not None:
        with _RESULT_CACHE_LOCK:
            entry = _RESULT_CACHE.get(key)
            if entry is not None and entry[0] == version:
                _RESULT_CACHE.move_to_end(key)
                return entry[1]
    num_candidates, matches = snippet_find_matches(session, query, *key[1:])
    payload = {
        "lsh_candidates": num_candidates,
        "matches": [
            {"checksum": s.checksum, "names": s.name_list, "score": score}
            for s, score in matches
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

    engine: Any = None  # set by serve()

    def _read_body(self) -> dict | None:
        """Read and parse the JSON request body; None if malformed."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, KeyError):
            return None

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
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
                payload = _find_one(session, body, query)
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
        with Session(self.engine) as session:
            for query in queries:
                try:
                    results.append({"query": query, **_find_one(session, body, query)})
                except Exception as exc:  # isolate per-query failures
                    results.append({"query": query, "error": str(exc)})
        self._respond(200, {"results": results})

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Quiet by default; the CLI prints its own status line.
        return


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
        # One-time migration + index build, before any request is served.
        # The migration worker count scales with the database (spawning a
        # worker per CPU for a small database costs more than the work).
        if fingerprint_version_get(session) != FINGERPRINT_VERSION:
            from sqlmodel import func, select

            from .core import adaptive_worker_count
            from .models import Snippet

            num_snippets = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]
            db_reindex(
                session,
                jobs=adaptive_worker_count(num_snippets, os.cpu_count() or 1),
            )
        # Build the index only if it is missing or was built with different
        # parameters — rebuilding an already-current index on every restart
        # would make serve startup pay the full build (~2 min at 500k) each
        # time, which bites under process managers that restart often.
        from .lsh import lsh_meta_get

        meta = lsh_meta_get(session)
        if (
            meta is None
            or abs(meta[0] - LSH_THRESHOLD) > 1e-9
            or meta[1] != NUM_PERMUTATIONS
        ):
            lsh_index_build(session, LSH_THRESHOLD, NUM_PERMUTATIONS)

    _FindHandler.engine = engine
    httpd = ThreadingHTTPServer((host, port), _FindHandler)
    os.makedirs(os.path.dirname(port_file), exist_ok=True)
    with open(port_file, "w", encoding="utf-8") as f:
        f.write(str(httpd.server_address[1]))

    def _cleanup() -> None:
        try:
            os.remove(port_file)
        except OSError:
            pass

    import atexit

    atexit.register(_cleanup)
    return httpd
