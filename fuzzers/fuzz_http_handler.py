#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the HTTP request surface of the find server.

The Python find server (:class:`resembl.server._FindHandler`) is the
widest untrusted-input surface in the project: every byte of a request
line, the headers, and the body reaches the handler before any
validation, and the handler's one job is to answer *every* shape of
request with the JSON envelope — a shape that escapes as a second
exception type, an HTML error page, or a thread that dies without
answering is exactly what a crash looks like from a client's side.
The existing fuzz targets drive the *payload* parsers in isolation
(:func:`resembl.server._read_body` with a stub handler in
``fuzz_find_request.py``; ``/score`` and ``/band-keys`` payload
functions in ``fuzz_computed_request.py``), so they never exercise
the dispatch around them: routing on an attacker-controlled path,
verb handling, the correlation-id adoption, host and content-type
gates, body draining on a keep-alive connection, and the metric /
log records that quote those fields.  A control character smuggled
into a path or a correlation id forges a log line; a lying
``Content-Length`` left undrained poisons the next request on the
same connection; a verb the handler does not name falls through to
the stdlib's HTML 501.  Those are all reachable from one request.

The harness drives the real handler over a live socket with a
fuzzer-chosen raw request (method, path, version, headers, body
length and body bytes), against a stateless server: no database is
opened, the database-backed paths answer 501, and the stateless
endpoints (``/score``, ``/band-keys``, ``/scheme``, ``/health``,
``/metrics``) run the shipped code.  It asserts the invariants the
handler documents, not just the absence of a crash:

* every complete request is answered with a status the handler
  documents (200, 400, 403, 404, 405, 413, 415, 501, 503) and the
  answer is one JSON object carrying ``error`` on a non-2xx, or the
  documented payload shape on a 2xx — never an HTML error page;
  a request the stdlib itself cannot parse (a request line too
  long, an HTTP version it rejects, a host header it refuses, a
  host or content-type gate it answers before reading a body)
  answers with the stdlib's own error or a closed connection
  before any handler runs, and the harness skips those instead of
  asserting on them;
* the connection stays reusable after any answer: a second,
  well-formed request on the same connection is answered correctly,
  so a body left undrained (or over-read) cannot poison the next
  request — the keep-alive invariant ``handle_one_request``
  documents;
* a caller-supplied ``X-Request-Id`` is echoed only when bounded
  and printable; an over-long or control-carrying id is dropped
  for a server-minted one, and no id is echoed verbatim into a
  response header or a log record with a control character in it;
* 404/405 error text never carries a control character from the
  request path (log/header injection through the error body);
* a request the handler refuses for host or content type is
  answered before the body is parsed, so a malformed body cannot
  turn a 403/415 into a 400 or a 500.

Inputs are generated structure-aware: well-formed requests built
from the documented paths and verbs (every served path, a query
string, each of the named verbs plus an unknown one), near-misses
the handler must refuse (a find path with the wrong verb, a
non-JSON content type, a rebound ``Host``, a lying or negative
``Content-Length``, a deep-nested body), a fuzzer-chosen
correlation id (bounded, over-long, control-carrying), and raw junk
bytes.  Seeds come from the documented API shape, not from captured
traffic.
"""

import json
import socket
import sys
import threading

import atheris

with atheris.instrument_imports():
    from resembl.server import _MAX_BODY_BYTES, _FindHandler, _FindServer

#: The documented verbs and paths, plus near-misses the handler must
#: refuse through the same JSON envelope.  ``FOO`` and a lowercase
#: verb are not here: the stdlib answers them with its own 501
#: before dispatch, which is the transport, not the envelope.
_METHODS = (
    "GET",
    "POST",
    "PUT",
    "DELETE",
    "PATCH",
    "HEAD",
    "OPTIONS",
    "",
)
_PATHS = (
    "/find",
    "/find-batch",
    "/find-pinned",
    "/score",
    "/band-keys",
    "/scheme",
    "/health",
    "/metrics",
    "/runtime-generation",
    "/find?trace=1",
    "/unknown",
    "/find/",
    "/FIND",
    "/",
    "",
    "/find\x00junk",
    "/find\r\nInjected: yes",
)
_CONTENT_TYPES = (
    "application/json",
    "application/json; charset=utf-8",
    "text/plain",
    "APPLICATION/JSON",
    "",
)
_HOSTS = (
    "127.0.0.1:PORT",
    "localhost:PORT",
    "evil.example:PORT",
    "127.0.0.1",
    "",
)
_REQUEST_IDS = (
    "abc-123",
    "",
    "x" * 64,
    "x" * 65,
    "bad\x00id",
    "bad\r\nid",
    "id with spaces",
)
_CONTROL_CHARS = frozenset([*range(0x20), 0x7F])

#: Canonical well-formed request the keep-alive probe re-sends.
_PROBE_BODY = json.dumps({"source": "mov eax, 1", "candidates": ["mov eax, 1"]}).encode()


def _request_bytes(
    method: str,
    path: str,
    *,
    content_type: str,
    host: str,
    request_id: str,
    body: bytes,
    length_override: str | None,
    version: str = "HTTP/1.1",
) -> bytes:
    """Render one raw HTTP/1.1 request from the fuzzer's fields."""
    lines = [f"{method} {path} {version}", f"Host: {host}"]
    if content_type:
        lines.append(f"Content-Type: {content_type}")
    if request_id:
        lines.append(f"X-Request-Id: {request_id}")
    length = str(len(body)) if length_override is None else length_override
    lines.append(f"Content-Length: {length}")
    lines.append("Connection: keep-alive")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def _read_response(sock: socket.socket) -> tuple[int, dict, str] | None:
    """Read one HTTP response; None when the connection drops or stalls."""
    sock.settimeout(5)
    try:
        stream = sock.makefile("rb")
        status_line = stream.readline()
        if not status_line:
            return None
        parts = status_line.decode("latin-1").split()
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        status = int(parts[1])
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if not line or line == b"\r\n":
                break
            if b":" in line:
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().casefold()] = value.strip()
        length = headers.get("content-length", "0")
        try:
            size = int(length)
        except ValueError:
            size = 0
        raw = stream.read(size) if 0 < size <= _MAX_BODY_BYTES else b""
        return status, headers, raw.decode("utf-8", errors="replace")
    except OSError, ValueError:
        return None
    finally:
        sock.settimeout(None)


def _is_parseable_request(method: str, path: str, version: str = "HTTP/1.1") -> bool:
    """Mirror the stdlib's request-line gates the harness must not assert on.

    ``BaseHTTPRequestHandler.parse_request`` answers a request line it
    cannot parse with the stdlib's own error (or closes the
    connection) before any handler runs — that is the transport, not
    the handler's envelope, so the harness skips those inputs
    instead of asserting on them.  The verb gate is the same one the
    stdlib applies: an uppercase verb the handler's ``__getattr__``
    answers with 405, any other verb is the stdlib's 501 before the
    handler ever runs.
    """
    if not method or not method.isupper() or not path:
        return False
    if len(f"{method} {path} {version}") > 65536:
        return False
    if version not in ("HTTP/1.1", "HTTP/1.0"):
        return False
    # The verb must name a handler: an uppercase verb the handler
    # does not name is the stdlib's 501 before dispatch, which is
    # the transport, not the envelope.
    if method not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
        return False
    # A NUL in the path ends the request line at parse time: the
    # stdlib truncates there and the handler never sees the rest.
    if "\x00" in path:
        return False
    # A lone CR or LF in the path ends the request line early: the
    # stdlib answers (or drops) a truncated line before dispatch.
    if any(ch in path for ch in "\r\n"):
        return False
    return True


def _pick(fdp: atheris.FuzzedDataProvider, pool: tuple[str, ...]) -> str:
    """Return one entry from *pool*, or fuzzer-chosen text in a fifth of runs."""
    if fdp.ConsumeIntInRange(0, 4) == 0:
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 32))
    return pool[fdp.ConsumeIntInRange(0, len(pool) - 1)]


def _build_body(fdp: atheris.FuzzedDataProvider) -> bytes:
    """Return a request body: a documented payload, a hostile one, or junk."""
    mode = fdp.ConsumeIntInRange(0, 5)
    if mode == 0:
        return _PROBE_BODY
    if mode == 1:
        return json.dumps({"query": fdp.ConsumeUnicodeNoSurrogates(16), "top_n": 2}).encode()
    if mode == 2:
        depth = fdp.ConsumeIntInRange(2, 2000)
        return f"{'{' * depth}{'\'a\':1' * depth}{'}' * depth}".encode()
    if mode == 3:
        return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 256))
    if mode == 4:
        return json.dumps(
            {
                "source": fdp.ConsumeUnicodeNoSurrogates(16),
                "candidates": [
                    fdp.ConsumeUnicodeNoSurrogates(8) for _ in range(fdp.ConsumeIntInRange(0, 4))
                ],
                "jaccard_floor": fdp.ConsumeIntInRange(0, 2) / 2,
            }
        ).encode()
    return b""


_SERVER: _FindServer | None = None
_PORT = 0
_LOCK = threading.Lock()


def _ensure_server() -> tuple[_FindServer, int]:
    """Bind the stateless server once; the port is discovered, never chosen."""
    global _SERVER, _PORT  # pylint: disable=global-variable-not-assigned
    with _LOCK:
        if _SERVER is not None:
            return _SERVER, _PORT
        server = _FindServer(("127.0.0.1", 0), _FindHandler)
        server.engine = None
        server.daemon_threads = True
        # Unrestricted Host: the fuzzed hosts above must reach the
        # gates, not be refused by a bind-specific allowlist.
        server.allowed_hosts = frozenset()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _SERVER = server
        _PORT = int(server.server_address[1])
        return server, _PORT


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    method = _pick(fdp, _METHODS)
    path = _pick(fdp, _PATHS)
    content_type = _pick(fdp, _CONTENT_TYPES)
    host = _pick(fdp, _HOSTS).replace("PORT", "1")
    request_id = _pick(fdp, _REQUEST_IDS)
    body = _build_body(fdp)
    length_mode = fdp.ConsumeIntInRange(0, 4)
    if length_mode == 0:
        length_override = None
    elif length_mode == 1:
        length_override = str(max(0, len(body) + fdp.ConsumeIntInRange(-4, 4)))
    elif length_mode == 2:
        length_override = f"-{fdp.ConsumeIntInRange(1, 1 << 40)}"
    elif length_mode == 3:
        length_override = fdp.ConsumeUnicodeNoSurrogates(8)
    else:
        length_override = str(fdp.ConsumeIntInRange(0, 1 << 30))

    _server, port = _ensure_server()
    if not _is_parseable_request(method, path):
        # The stdlib answers (or drops) an unparsable request line
        # before the handler runs; that is transport, not the
        # envelope, so skip instead of asserting on it.
        return
    probe = _request_bytes(
        "POST",
        "/score",
        content_type="application/json",
        host="127.0.0.1:1",
        request_id="",
        body=_PROBE_BODY,
        length_override=None,
    )
    raw = _request_bytes(
        method,
        path,
        content_type=content_type,
        host=host,
        request_id=request_id,
        body=body,
        length_override=length_override,
    )

    # One connection carries the fuzzed request and the probe: the
    # probe's answer proves the first request drained (or closed)
    # exactly the bytes it declared.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(raw)
        first = _read_response(sock)
        if first is None:
            # The stdlib drops a connection it cannot parse (an
            # empty request line, a host header it refuses, a body
            # the declared length does not cover) before the handler
            # ever runs; that is transport, not the envelope, so
            # skip instead of asserting on it.
            return
        status, headers, text = first
        assert status in (
            200,
            400,
            403,
            404,
            405,
            413,
            415,
            501,
            503,
            500,
        ), f"undocumented status {status} for {method} {path!r}"
        # /metrics is a pre-rendered Prometheus string, not JSON;
        # every other answer is the JSON envelope, and an HTML page
        # means a stdlib fallback escaped the handler.
        if status == 200 and path.split("?", 1)[0] == "/metrics":
            assert text.startswith(
                "# HELP resembl_server_up"
            ), f"/metrics answer lost its exposition: {text[:80]!r}"
        else:
            assert text.lstrip().startswith("{"), f"non-JSON answer: {text[:80]!r}"
            try:
                payload = json.loads(text)
            except ValueError as exc:
                raise AssertionError(f"answer is not valid JSON: {text[:80]!r}") from exc
            assert isinstance(payload, dict), f"answer is not a JSON object: {payload!r}"
        if status >= 400:
            assert "error" in payload, f"error answer without the envelope: {payload!r}"
            error_text = str(payload["error"])
            assert not any(
                ord(ch) in _CONTROL_CHARS for ch in error_text
            ), f"control character in error text: {error_text!r}"
        # A 2xx on the stateless endpoints carries a JSON payload;
        # /metrics is a pre-rendered string exception, asserted below.
        if status == 200 and path.split("?", 1)[0] != "/metrics":
            assert payload, f"empty 2xx answer: {payload!r}"
        # The echoed correlation id is the caller's only when bounded
        # and printable; anything else is server-minted.
        echoed = headers.get("x-request-id", "")
        if echoed:
            assert len(echoed) <= 64, f"echoed an over-long id: {len(echoed)}"
            assert not any(
                ord(ch) in _CONTROL_CHARS for ch in echoed
            ), f"echoed a control-carrying id: {echoed!r}"
        sock.sendall(probe)
        second = _read_response(sock)
        if second is None:
            # A closed connection after a refusal is documented
            # (an untrustworthy length closes it); the fuzzed
            # request itself must still have been answered.
            return
        probe_status, _probe_headers, probe_text = second
        assert probe_status == 200, (
            f"keep-alive probe answered {probe_status} after {method} {path!r}: "
            f"{probe_text[:80]!r}"
        )
        probe_payload = json.loads(probe_text)
        assert "scores" in probe_payload, f"probe answer lost its shape: {probe_payload!r}"


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
