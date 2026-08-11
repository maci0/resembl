"""A long-lived server that keeps the database warm for instant ``find``.

Every CLI invocation pays ~450 ms of interpreter/library startup; a search
itself is ~1.4 ms.  ``resembl serve`` starts a small HTTP server (stdlib
only) that holds the engine, session, and LSH index warm, and ``find``
automatically talks to it when it is running — turning the headline warm-find
latency from ~450 ms into a few milliseconds.

The server writes a port file (``server_<dbhash>.port`` in the cache dir)
that ``find`` uses to locate it.  Queries are serialized with a lock
(SQLAlchemy sessions are not thread-safe).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from sqlmodel import Session

from .cache import cache_dir_get
from .core import snippet_find_matches


def server_port_path(db_url: str) -> str:
    """Return the port-file path for a database URL."""
    digest = hashlib.sha1(db_url.encode("utf-8")).hexdigest()[:12]
    return os.path.join(cache_dir_get(), f"server_{digest}.port")


class _FindHandler(BaseHTTPRequestHandler):
    """Serves ``POST /find`` against the shared session."""

    session: Session = None  # type: ignore[assignment]  # set by serve()
    lock: threading.Lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        if self.path != "/find":
            self.send_error(404, "Not Found")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            query = body["query"]
        except (ValueError, KeyError) as exc:
            self._respond(400, {"error": f"bad request: {exc}"})
            return
        try:
            with self.lock:
                num_candidates, matches = snippet_find_matches(
                    self.session,
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


def serve(db_url: str, host: str = "127.0.0.1", port: int = 0) -> HTTPServer:
    """Start the find server for *db_url* and return the bound HTTP server.

    The port file is written on startup and removed on exit.
    """
    from .database import create_db_engine

    engine = create_db_engine(db_url)
    _FindHandler.session = Session(engine)
    httpd = HTTPServer((host, port), _FindHandler)
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
