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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sqlmodel import Session

from .cache import cache_dir_get, lsh_index_build
from .core import LSH_THRESHOLD, NUM_PERMUTATIONS, db_reindex, snippet_find_matches
from .lsh import fingerprint_version_get
from .models import FINGERPRINT_VERSION


def server_port_path(db_url: str) -> str:
    """Return the port-file path for a database URL."""
    digest = hashlib.sha1(db_url.encode("utf-8")).hexdigest()[:12]
    return os.path.join(cache_dir_get(), f"server_{digest}.port")


class _FindHandler(BaseHTTPRequestHandler):
    """Serves ``POST /find``; one session per request (concurrent reads)."""

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
                num_candidates, matches = snippet_find_matches(
                    session,
                    query,
                    top_n=int(body.get("top_n", 5)),
                    threshold=body.get("threshold"),
                    normalize=bool(body.get("normalize", True)),
                    ngram_size=int(body.get("ngram_size", 3)),
                )
        except Exception as exc:  # pragma: no cover - defensive
            self._respond(500, {"error": str(exc)})
            return
        self._respond(
            200,
            {
                "lsh_candidates": num_candidates,
                "matches": [
                    {"checksum": s.checksum, "names": s.name_list, "score": score}
                    for s, score in matches
                ],
            },
        )

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
                    num_candidates, matches = snippet_find_matches(
                        session,
                        query,
                        top_n=int(body.get("top_n", 5)),
                        threshold=body.get("threshold"),
                        normalize=bool(body.get("normalize", True)),
                        ngram_size=int(body.get("ngram_size", 3)),
                    )
                except Exception as exc:  # isolate per-query failures
                    results.append({"query": query, "error": str(exc)})
                    continue
                results.append(
                    {
                        "query": query,
                        "lsh_candidates": num_candidates,
                        "matches": [
                            {
                                "checksum": s.checksum,
                                "names": s.name_list,
                                "score": score,
                            }
                            for s, score in matches
                        ],
                    }
                )
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

    engine = create_db_engine(db_url)
    with Session(engine) as session:
        # One-time migration + index build, before any request is served.
        if fingerprint_version_get(session) != FINGERPRINT_VERSION:
            db_reindex(session, jobs=max(1, os.cpu_count() or 1))
        lsh_index_build(session, LSH_THRESHOLD, NUM_PERMUTATIONS)

    _FindHandler.engine = engine
    httpd = ThreadingHTTPServer((host, port), _FindHandler)
    port_file = server_port_path(db_url)
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
