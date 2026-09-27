"""Tests for the warm ``serve`` server and the thin find client."""

# pylint: disable=protected-access  # tests exercise private internals

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

import resembl.models  # noqa: F401  (registers tables)
from resembl.config import ResemblConfig
from resembl.core import snippet_add_batch, snippet_find_matches, snippet_prepare


def _post_json(port: int, path: str, payload: dict, timeout: int = 10) -> dict:
    """POST *payload* as JSON to the server on *port* and decode the reply."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _post_json_status(port: int, path: str, payload: dict, timeout: int = 10) -> tuple[int, dict]:
    """Like :func:`_post_json`, but returns ``(status, reply)`` for 4xx/5xx."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _capturing_create() -> tuple[object, dict]:
    """Return ``(create_fn, created)`` for the engine-disposal tests.

    ``create_fn`` is a drop-in ``resembl.database.create_db_engine`` that
    records every engine it builds in *created* (key ``"engine"``), so a
    test can assert on the pool state after serve raises.
    """
    import resembl.database as db_mod

    real_create = db_mod.create_db_engine
    created: dict = {}

    def capturing_create(url, **kwargs):
        engine = real_create(url, **kwargs)
        created["engine"] = engine
        return engine

    return capturing_create, created


class TestServerMode(unittest.TestCase):
    """The server serves find queries equivalent to the in-process path."""

    def setUp(self):
        self._db = tempfile.mktemp(suffix=".db")
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self._cache_dir = cache.name
        self._engine = create_engine(f"sqlite:///{self._db}")
        SQLModel.metadata.create_all(self._engine)
        self._session = Session(self._engine)
        items = [
            snippet_prepare(f"f{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3) for i in range(100)
        ]
        snippet_add_batch(self._session, [x for x in items if x])
        self._env = patch.dict(
            os.environ,
            {
                "RESEMBL_CACHE_DIR": self._cache_dir,
                "DATABASE_URL": f"sqlite:///{self._db}",
            },
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._session.close()
        for path in (self._db, self._db + "-wal", self._db + "-shm"):
            if os.path.exists(path):
                os.remove(path)

    def _start_server(self):
        from resembl.server import serve

        httpd = serve(f"sqlite:///{self._db}", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1]

    def test_startup_skips_current_index_rebuild(self):
        """serve does not rebuild an already-current index on restart.

        Restarting serve used to pay the full index build every time (~2 min
        at 500k) even when the index was current — a real cost under process
        managers that restart often.
        """
        from unittest.mock import patch

        from resembl import server as server_mod
        from resembl.cache import lsh_index_build
        from resembl.lsh import lsh_meta_get

        lsh_index_build(self._session, 0.5, 128)
        self.assertIsNotNone(lsh_meta_get(self._session))

        with patch.object(server_mod, "lsh_index_build") as mock_build:
            httpd = server_mod.serve(f"sqlite:///{self._db}", port=0)
            httpd.server_close()
        mock_build.assert_not_called()

    def test_startup_builds_missing_index(self):
        """serve builds the index when none exists yet."""
        from unittest.mock import patch

        from resembl import server as server_mod

        with patch.object(server_mod, "lsh_index_build") as mock_build:
            httpd = server_mod.serve(f"sqlite:///{self._db}", port=0)
            httpd.server_close()
        mock_build.assert_called_once()

    def test_server_close_disposes_engine_pool(self):
        """server_close releases the engine's pooled DB connections.

        The warm engine holds up to pool_size + max_overflow SQLite handles;
        a stopped server generation must not pin them until interpreter exit.
        """
        from resembl.server import serve as serve_start

        httpd = serve_start(f"sqlite:///{self._db}", port=0)
        try:
            # Startup's warm-up session returned its connection to the pool.
            self.assertGreaterEqual(httpd.engine.pool.checkedin(), 1)
            httpd.server_close()
            self.assertEqual(httpd.engine.pool.checkedin(), 0)
        finally:
            httpd.server_close()

    def test_server_close_retires_port_file_and_atexit_registration(self):
        """server_close retires the advertisement and releases its exit hook.

        ``serve`` registers one atexit cleanup per call.  A long-lived
        embedder starting and stopping servers repeatedly must not accumulate
        one handler (plus one stale port file) per cycle: ``server_close``
        runs the cleanup eagerly and unregisters exactly its own entry,
        leaving other servers' registrations and advertisements intact.
        """
        import atexit

        from resembl import server as server_mod
        from resembl.paths import cache_dir_get, server_port_path

        db_url = f"sqlite:///{self._db}"
        httpd_a = server_mod.serve(db_url, port=0)
        port_file = server_port_path(db_url, cache_dir_get())
        self.assertTrue(os.path.exists(port_file))

        # A second server for another database shares this cache directory
        # and holds its own exit-hook registration.  (serve assumes the CLI
        # already created its schema, so initialize the other database first.)
        other_db_url = f"sqlite:///{self._db}-other"
        other_engine = create_engine(other_db_url)
        try:
            SQLModel.metadata.create_all(other_engine)
        finally:
            other_engine.dispose()
        httpd_b = server_mod.serve(other_db_url, port=0)
        self.addCleanup(httpd_b.server_close)

        with patch("atexit.unregister", wraps=atexit.unregister) as mock_unreg:
            httpd_a.server_close()
        # Exactly A's callback is released (atexit.unregister matches by
        # callable alone, hence the per-generation partial).
        mock_unreg.assert_called_once_with(httpd_a._atexit_cleanup)
        self.assertIsNot(httpd_a._atexit_cleanup, httpd_b._atexit_cleanup)
        # A's advertisement is retired eagerly, not left for interpreter exit.
        self.assertFalse(os.path.exists(port_file))
        # B's advertisement is untouched by A's close.
        self.assertTrue(os.path.exists(server_port_path(other_db_url, cache_dir_get())))

        # Closing again is idempotent (callers with finally blocks may repeat).
        httpd_a.server_close()
        self.assertFalse(os.path.exists(port_file))

    def test_server_query_matches_in_process(self):
        """POST /find returns the same top matches as the in-process path."""
        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        payload = _post_json(port, "/find", {"query": query, "top_n": 5})

        num_candidates, matches = snippet_find_matches(self._session, query, top_n=5)
        self.assertEqual(payload["lsh_candidates"], num_candidates)
        self.assertEqual(
            [m["checksum"] for m in payload["matches"]],
            [s.checksum for s, _ in matches],
        )
        self.assertEqual(
            [round(m["score"], 6) for m in payload["matches"]],
            [round(score, 6) for _, score in matches],
        )

    def test_server_rejects_unbuildable_threshold(self):
        """The server answers an unbuildable threshold with a 400 error payload."""
        port = self._start_server()
        status, payload = _post_json_status(
            port,
            "/find",
            {"query": "push ebx\nmov eax, 5\npop ebx\nret", "threshold": 0.985},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertIn("too high", payload["error"])

    def test_server_rejects_out_of_range_num_permutations(self):
        """An out-of-range num_permutations is refused with an error payload.

        Fingerprint construction and LSH banding allocate memory proportional
        to the permutation count, and ``minhash_new`` caches one MinHash
        template per distinct count for the life of the warm server.  An
        unbounded request value would let any client grow (or OOM) the
        long-lived process.
        """
        from resembl.scoring import _MINHASH_TEMPLATES

        port = self._start_server()
        absurd = 1 << 20
        status, payload = _post_json_status(
            port,
            "/find",
            {"query": "push ebx\nmov eax, 5\npop ebx\nret", "num_permutations": absurd},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertIn("num_permutations", payload["error"])
        # The rejected value must not leave a cached fingerprint template.
        self.assertNotIn(absurd, _MINHASH_TEMPLATES)

        # The lower bound is enforced by the same check.
        low_status, low_payload = _post_json_status(
            port, "/find", {"query": "mov eax, 5", "num_permutations": 1}
        )
        self.assertEqual(low_status, 400)
        self.assertIn("error", low_payload)

    def test_serve_bind_failure_disposes_engine(self):
        """A failed bind releases the startup engine instead of leaking it.

        ``serve`` builds its engine (pool_size + max_overflow SQLite handles
        once warmed) before binding the HTTP port; when the bind fails the
        caller keeps running (embedded use, test harnesses), so the pool
        must be released rather than left pinned until interpreter exit.
        """
        import socket as socket_mod
        from unittest.mock import patch

        import resembl.database as db_mod
        from resembl.server import serve as serve_start

        capturing_create, created = _capturing_create()

        blocker = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied = blocker.getsockname()[1]
        try:
            with patch.object(db_mod, "create_db_engine", capturing_create):
                with self.assertRaises(OSError):
                    serve_start(f"sqlite:///{self._db}", port=occupied)
        finally:
            blocker.close()
        self.assertIn("engine", created)
        self.assertEqual(created["engine"].pool.checkedin(), 0)

    def test_serve_warmup_failure_disposes_engine(self):
        """A failed startup warm-up releases the engine instead of leaking it.

        ``serve`` reindexes and builds the index between creating the engine
        and binding the HTTP port; a failure there must not pin the pool's
        SQLite handles in a process that keeps running after serve raises
        (embedded callers, test harnesses).
        """
        from unittest.mock import patch

        import resembl.database as db_mod
        from resembl.server import serve as serve_start

        capturing_create, created = _capturing_create()

        with patch.object(db_mod, "create_db_engine", capturing_create):
            # fingerprints_need_reindex is imported inside serve(), so the
            # patch must target resembl.core; db_reindex is a module-level
            # import in server.py.
            with patch("resembl.core.fingerprints_need_reindex", return_value=True):
                with patch(
                    "resembl.server.db_reindex",
                    side_effect=RuntimeError("warm-up blew up"),
                ):
                    with self.assertRaises(RuntimeError):
                        serve_start(f"sqlite:///{self._db}", port=0)
        self.assertIn("engine", created)
        self.assertEqual(created["engine"].pool.checkedin(), 0)

    def test_serve_failed_migration_reindex_raises_and_disposes(self):
        """A migration reindex that reports failure fails the serve startup.

        ``db_reindex`` signals "could not run" through its result dict;
        ignoring it let serve start anyway, and the index build below then
        stamped the unmigrated fingerprints as current — serving silently
        wrong (empty) results forever.  The startup must raise instead,
        releasing the engine like any other warm-up failure.
        """
        import resembl.database as db_mod
        from resembl.core import IndexBuildError
        from resembl.server import serve as serve_start

        capturing_create, created = _capturing_create()

        with patch.object(db_mod, "create_db_engine", capturing_create):
            with patch("resembl.core.fingerprints_need_reindex", return_value=True):
                with patch(
                    "resembl.server.db_reindex",
                    return_value={"error": "could not clear the index (locked)"},
                ):
                    with self.assertRaises(IndexBuildError) as ctx:
                        serve_start(f"sqlite:///{self._db}", port=0)
        self.assertIn("could not clear the index (locked)", str(ctx.exception))
        self.assertIn("engine", created)
        self.assertEqual(created["engine"].pool.checkedin(), 0)

    def test_server_rejects_oversized_body(self):
        """A Content-Length above the cap is refused without reading the body.

        The bound keeps a local process from making the server allocate
        unbounded memory per request.
        """
        import http.client

        port = self._start_server()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.putrequest("POST", "/find")
            conn.putheader("Content-Type", "application/json")
            # Announce far more body than the server accepts; send nothing.
            conn.putheader("Content-Length", str(64 * 1024 * 1024))
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            self.assertIn(b"bad request body", response.read())
        finally:
            conn.close()

    def test_server_rejects_negative_content_length(self):
        """A negative Content-Length is rejected instead of blocking the handler."""
        import http.client

        port = self._start_server()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.putrequest("POST", "/find")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "-1")
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
        finally:
            conn.close()

    def test_port_file_written(self):
        """serve writes a discoverable port file in the cache dir."""
        from resembl.paths import cache_dir_get, server_port_path

        port = self._start_server()
        port_file = server_port_path(f"sqlite:///{self._db}", cache_dir_get())
        self.assertTrue(os.path.exists(port_file))
        with open(port_file, encoding="utf-8") as f:
            self.assertEqual(int(f.read()), port)

    def test_port_file_publish_leaves_no_temp_files(self):
        """The write-temp-rename advertisement publish leaves no temp files.

        ``serve`` publishes the port file via a same-directory temp plus
        ``os.replace`` so concurrent find clients never read an empty or
        half-written port number; a failed publish (or a leaked temp) must
        leave nothing behind next to the real advertisement.
        """
        import glob

        from resembl.paths import cache_dir_get, server_port_path

        self._start_server()
        port_file = server_port_path(f"sqlite:///{self._db}", cache_dir_get())
        with open(port_file, encoding="utf-8") as f:
            self.assertTrue(f.read().strip().isdigit())
        leftovers = glob.glob(os.path.join(os.path.dirname(port_file), "*.tmp"))
        self.assertEqual(leftovers, [])

    def test_thin_client_queries_server(self):
        """resembl.find_client._main returns the matches via the server."""
        from resembl.find_client import _main

        self._start_server()
        rc = _main(["--query", "push ebx; mov eax, 5; pop ebx; ret", "--json"])
        self.assertEqual(rc, 0)

    def test_load_config_parses_toml(self):
        """find_client reads lsh_threshold/ngram_size from config.toml."""
        import tempfile

        from resembl.find_client import _load_config

        cfg_dir = tempfile.mkdtemp()
        with open(os.path.join(cfg_dir, "config.toml"), "w", encoding="utf-8") as f:
            f.write("lsh_threshold = 0.7\nngram_size = 2\n")
        with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": cfg_dir}):
            cfg = _load_config()
        self.assertEqual(cfg["lsh_threshold"], 0.7)
        self.assertEqual(cfg["ngram_size"], 2)

    def test_load_config_reports_unreadable_file(self):
        """A malformed config.toml is reported, not mistaken for no file.

        The client falls back to defaults on a read error, so a swallowed
        failure would silently drop every setting the user wrote and answer
        with a different result set than `resembl find` on the same database.
        """
        import io
        import tempfile
        from contextlib import redirect_stderr

        from resembl.find_client import _load_config

        cfg_dir = tempfile.mkdtemp()
        with open(os.path.join(cfg_dir, "config.toml"), "w", encoding="utf-8") as f:
            f.write("lsh_threshold = = 0.7\n")
        stderr = io.StringIO()
        with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": cfg_dir}):
            with redirect_stderr(stderr):
                cfg = _load_config()
        self.assertEqual(cfg, {})
        self.assertIn("config.toml", stderr.getvalue())

    def test_thin_client_sends_config_values(self):
        """The thin client's request honors the CLI config (same results)."""
        from unittest.mock import patch as _patch

        from resembl.find_client import _main

        captured: dict = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"lsh_candidates": 0, "matches": []}'

        # Signature-conformance stub for urllib.request.urlopen; the real
        # call site passes timeout but the fake needs no fetch delay.
        def fake_urlopen(request, timeout=5):  # pylint: disable=unused-argument
            captured["body"] = json.loads(request.data)
            return _FakeResponse()

        self._start_server()  # writes the port file
        with _patch(
            "resembl.find_client._load_config",
            return_value={"lsh_threshold": 0.7, "ngram_size": 2},
        ):
            with _patch("urllib.request.urlopen", side_effect=fake_urlopen):
                rc = _main(["--query", "mov", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["body"]["threshold"], 0.7)
        self.assertEqual(captured["body"]["ngram_size"], 2)

    def test_thin_client_no_server_running(self):
        """resembl-find without a port file exits 1 with guidance, no traceback."""
        import contextlib
        import io

        from resembl.find_client import _main

        # setUp points RESEMBL_CACHE_DIR/DATABASE_URL at fresh temp paths:
        # no serve process ever ran for this DB URL.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = _main(["--query", "mov eax, 5", "--json"])
        self.assertEqual(rc, 1)
        self.assertIn("no server running", stderr.getvalue())
        self.assertIn("resembl serve", stderr.getvalue())

    def test_thin_client_unreachable_server_connection_refused(self):
        """Connection-refused reaches the clean 'unreachable' exit path."""
        import contextlib
        import io
        import urllib.error

        from resembl.find_client import _main
        from resembl.paths import server_port_path

        port_file = server_port_path(f"sqlite:///{self._db}", self._cache_dir)
        with open(port_file, "w", encoding="utf-8") as f:
            f.write(str(1))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError(ConnectionRefusedError("connection refused")),
            ):
                rc = _main(["--query", "mov eax, 5"])
        self.assertEqual(rc, 1)
        self.assertIn("unreachable", stderr.getvalue())

    def test_port_file_digest_uses_unmasked_url(self):
        """The port file is named from the raw URL, never the masked one.

        ``str(engine.url)`` masks the password (``user:***@host``); hashing
        that masked string made ``resembl-find`` look under a different
        port-file name than ``resembl serve`` wrote whenever DATABASE_URL
        carried credentials — the warm server was undiscoverable and every
        find silently fell back to cold process startups.
        """
        from sqlalchemy import create_engine as sa_create_engine
        from sqlmodel import Session

        from resembl.cli import _session_db_url
        from resembl.paths import cache_dir_get, db_url_mask, server_port_path

        url = "postgresql+pg8000://user:secretpw@dbhost/resembl"
        engine = sa_create_engine(url)
        try:
            with Session(engine) as session:
                db_url = _session_db_url(session)
        finally:
            engine.dispose()
        # The password is rendered back, not masked to ***.
        self.assertEqual(db_url, url)
        # Naming the file after the masked URL would break discovery.
        self.assertNotEqual(
            server_port_path(db_url, cache_dir_get()),
            server_port_path(db_url_mask(url), cache_dir_get()),
        )

    def test_thin_client_propagates_error_payload(self):
        """A server error payload surfaces on stderr with a failing exit code."""
        import contextlib
        import io

        from resembl.find_client import _main

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"error": "threshold too high"}'

        self._start_server()  # writes a valid port file
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with patch(
                "urllib.request.urlopen",
                side_effect=lambda request, timeout=5: _FakeResponse(),
            ):
                rc = _main(["--query", "mov eax, 5", "--json"])
        self.assertEqual(rc, 1)
        self.assertIn("too high", stderr.getvalue())

    def test_thin_client_reports_error_status_body(self):
        """A 400 answer prints the server's error message, not "HTTP Error 400".

        ``urllib`` raises ``HTTPError`` for a 4xx; treated as a transport
        failure the client printed "server unreachable", naming neither the
        offending parameter nor the server's reason.
        """
        import contextlib
        import io
        import urllib.error

        from resembl.find_client import _main

        self._start_server()  # writes a valid port file
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    "http://127.0.0.1/find",
                    400,
                    "Bad Request",
                    {},
                    io.BytesIO(b'{"error": "threshold 5.0 is not in [0.0, 1.0]"}'),
                ),
            ):
                rc = _main(["--query", "mov eax, 5", "--threshold", "5"])
        self.assertEqual(rc, 1)
        self.assertIn("threshold 5.0 is not in", stderr.getvalue())
        self.assertNotIn("unreachable", stderr.getvalue())

    def test_thin_client_table_output(self):
        """Without --json, the client prints a ranked table and exits 0."""
        import contextlib
        import io

        from resembl.find_client import _main

        payload = {
            "lsh_candidates": 2,
            "matches": [
                {"checksum": "a" * 64, "names": ["fn_a"], "score": 97.5},
                {"checksum": "b" * 64, "names": ["fn_b", "alias"], "score": 51.25},
            ],
        }

        class _FakeResponse:
            def __init__(self, body):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self._body

        self._start_server()  # writes a valid port file
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with patch(
                "urllib.request.urlopen",
                side_effect=lambda request, timeout=5: _FakeResponse(
                    json.dumps(payload).encode("utf-8")
                ),
            ):
                rc = _main(["--query", "mov eax, 5"])
        out = stdout.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Found 2 candidates via LSH.", out)
        self.assertIn("fn_a", out)
        self.assertIn("97.50", out)
        self.assertIn("fn_b, alias", out)
        self.assertIn("51.25", out)

    def test_find_batch_endpoint(self):
        """POST /find-batch returns per-query results matching single finds."""
        port = self._start_server()
        q1 = "push ebx\nmov eax, 5\npop ebx\nret"
        q2 = "push ebx\nmov eax, 99\npop ebx\nret"
        payload = _post_json(port, "/find-batch", {"queries": [q1, q2], "top_n": 5})

        self.assertEqual(len(payload["results"]), 2)
        for query, result in zip((q1, q2), payload["results"], strict=True):
            self.assertEqual(result["query"], query)
            # Matches the single /find result for the same query.
            single = _post_json(port, "/find", {"query": query, "top_n": 5})
            self.assertEqual(result["lsh_candidates"], single["lsh_candidates"])

    def test_find_batch_isolates_bad_queries(self):
        """A malformed query fails itself, not the whole batch."""
        port = self._start_server()
        payload = _post_json(
            port,
            "/find-batch",
            {"queries": ["push ebx\nmov eax, 5\npop ebx\nret", 12345]},
        )
        self.assertEqual(len(payload["results"]), 2)
        self.assertIn("lsh_candidates", payload["results"][0])
        self.assertIn("error", payload["results"][1])

    def test_find_batch_rejects_non_list_queries(self):
        """A non-list 'queries' value answers 400 JSON, not per-char finds."""
        port = self._start_server()
        # An integer is not iterable; a bare string would otherwise iterate
        # one single-character find per letter.
        for bad in (12345, "push ebx\nret"):
            status, payload = _post_json_status(port, "/find-batch", {"queries": bad})
            self.assertEqual(status, 400)
            self.assertIn("queries must be a list", payload["error"])

    def test_error_payloads_do_not_leak_internal_text(self):
        """500s and per-query failures answer generic errors, never exception text.

        Driver/ORM exception messages carry SQL fragments, bind parameters,
        and file paths; those belong in the server log (``logger.exception``
        / ``logger.warning``), not in HTTP payloads any client can read.
        """
        marker = "secret-internal: SELECT password FROM app_users"
        port = self._start_server()
        with patch("resembl.server._find_one", side_effect=RuntimeError(marker)):
            status, payload = _post_json_status(port, "/find", {"query": "push ebx\nret"})
            self.assertEqual(status, 500)
            self.assertNotIn(marker, json.dumps(payload))

            batch_status, batch = _post_json_status(port, "/find-batch", {"queries": ["q1"]})
            self.assertEqual(batch_status, 200)
            entry = batch["results"][0]
            self.assertIn("error", entry)
            self.assertNotIn(marker, json.dumps(batch))

    def test_response_security_headers(self):
        """Every JSON response carries anti-sniffing, framing, and cache headers."""
        import http.client

        port = self._start_server()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(
                "POST",
                "/find",
                body=json.dumps({"query": "push ebx\nret"}),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            csp = response.getheader("Content-Security-Policy") or ""
            self.assertIn("default-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
        finally:
            conn.close()

    def test_server_rejects_malformed_requests(self):
        """Malformed requests answer 4xx errors instead of crashing handlers.

        Contract for the /find API boundary: a non-numeric or negative
        Content-Length, a non-JSON body, an unknown path, and missing
        required keys each produce a clean error response.
        """
        import http.client

        port = self._start_server()

        def raw_post(path: str, body: bytes, content_length=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.putrequest("POST", path)
                conn.putheader("Content-Type", "application/json")
                if content_length is None:
                    content_length = str(len(body))
                conn.putheader("Content-Length", content_length)
                conn.endheaders()
                if body:
                    conn.send(body)
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()

        # Non-numeric Content-Length.
        status, _body = raw_post("/find", b"{}", content_length="abc")
        self.assertEqual(status, 400)

        # Valid length but non-JSON body.
        status, payload = raw_post("/find", b"not-json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload), {"error": "bad request body"})

        # Unknown path.
        status, _body = raw_post("/other", b"{}")
        self.assertEqual(status, 404)

        # /find without the required "query" key.
        status, payload = raw_post("/find", b"{}")
        self.assertEqual(status, 400)
        self.assertIn("bad request", json.loads(payload)["error"])

        # /find-batch without the required "queries" key.
        status, payload = raw_post("/find-batch", b"{}")
        self.assertEqual(status, 400)
        self.assertIn("bad request", json.loads(payload)["error"])

    def test_errors_share_one_json_envelope(self):
        """Every error status answers ``{"error": ...}`` as JSON, never HTML.

        ``BaseHTTPRequestHandler.send_error`` (the path-not-found and
        method-not-allowed answers the stdlib provides) emits an HTML page:
        a client parsing responses as JSON got a decode error instead of the
        status, and a browser pointed at the port got a page.
        """
        import http.client

        port = self._start_server()

        def request(method: str, path: str, body: bytes | None = None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request(
                    method,
                    path,
                    body=body,
                    headers={"Content-Type": "application/json"} if body is not None else {},
                )
                response = conn.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                conn.close()

        # Unknown path: 404 with the JSON error envelope.
        status, _headers, body = request("POST", "/nope", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "unknown path: /nope"})

        # Wrong method: 405 with the same envelope and an Allow header.
        status, headers, body = request("GET", "/find")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")
        self.assertIn("method not allowed", json.loads(body)["error"])

        # Explicit non-JSON content type: 415, not a silently parsed body.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(
                "POST",
                "/find",
                body=json.dumps({"query": "push ebx\nret"}),
                headers={"Content-Type": "text/plain"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 415)
            self.assertIn("application/json", payload["error"])
        finally:
            conn.close()

    def test_non_object_body_is_a_bad_request(self):
        """A JSON body that is not an object answers 400, never a 500.

        The handlers index the body by ``query`` / ``queries``; a list,
        number, or string body raised ``TypeError`` there, which surfaced as
        a 500 echoing the exception text.
        """
        import http.client

        port = self._start_server()
        for body in (b"[]", b"5", b'"push ebx"'):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.putrequest("POST", "/find")
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders()
                conn.send(body)
                response = conn.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 400)
                self.assertEqual(payload, {"error": "bad request body"})
            finally:
                conn.close()

    def test_find_batch_rejects_bad_parameters_once(self):
        """A parameter error answers one 400 for the whole batch.

        Validated per entry, the same parameter error was repeated in every
        result and the request ran N finds before answering.
        """
        port = self._start_server()
        status, payload = _post_json_status(
            port,
            "/find-batch",
            {"queries": ["push ebx\nret", "mov eax, 5"], "threshold": 5.0},
        )
        self.assertEqual(status, 400)
        self.assertIn("threshold", payload["error"])

    def test_find_batch_caps_query_count(self):
        """A batch larger than the documented cap answers 400."""
        from resembl.server import _MAX_BATCH_QUERIES

        port = self._start_server()
        queries = ["mov eax, 5"] * (_MAX_BATCH_QUERIES + 1)
        status, payload = _post_json_status(port, "/find-batch", {"queries": queries})
        self.assertEqual(status, 400)
        self.assertIn(str(_MAX_BATCH_QUERIES), payload["error"])

    def test_find_rejects_degenerate_ngram_size(self):
        """An ngram_size below 1 answers a clean error, never garbage results.

        An n-gram of 0 does not crash: every shingle collapses to the empty
        token tuple and every snippet matches every other one — silently
        wrong results instead of a failure.
        """
        port = self._start_server()
        status, payload = _post_json_status(
            port, "/find", {"query": "push ebx\nret", "top_n": 5, "ngram_size": 0}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertIn("ngram_size", payload["error"])

    def test_find_rejects_out_of_range_top_n(self):
        """A top_n that names the whole corpus answers 400, not the corpus.

        top_n was the one find parameter with no bound, so one unauthenticated
        POST could ask for every row the LSH index returns.
        """
        port = self._start_server()
        status, payload = _post_json_status(port, "/find", {"query": "push ebx", "top_n": 10**9})
        self.assertEqual(status, 400)
        self.assertIn("top_n", payload["error"])

    def test_find_accepts_top_n_at_the_cap(self):
        """The bound is a ceiling, not a refusal: a top_n at the cap still runs."""
        from resembl.server import _MAX_TOP_N

        port = self._start_server()
        status, payload = _post_json_status(
            port, "/find", {"query": "push ebx\nret", "top_n": _MAX_TOP_N}
        )
        self.assertEqual(status, 200)
        self.assertIn("matches", payload)

    def test_find_rejects_rebound_host(self):
        """A loopback server refuses a request whose Host names another host.

        A page that rebinds its own name onto 127.0.0.1 sends a same-origin
        request (no preflight, response readable) to an endpoint with no
        authentication; the Host header still carries the rebound name.
        """
        port = self._start_server()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find",
            data=json.dumps({"query": "push ebx"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Host": "attacker.example"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, payload = response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, json.loads(exc.read())
        self.assertEqual(status, 403)
        self.assertIn("error", payload)

    def test_allowed_hosts_covers_only_loopback_binds(self):
        """The Host check is scoped to loopback binds, and covers their names."""
        from resembl.server import _loopback_allowed_hosts

        allowed = _loopback_allowed_hosts("127.0.0.1", 8080)
        self.assertIn("127.0.0.1:8080", allowed)
        self.assertIn("localhost:8080", allowed)
        self.assertIn("[::1]:8080", allowed)
        self.assertNotIn("attacker.example:8080", allowed)
        # A routable bind is reached by names the server cannot know in
        # advance, so it stays unrestricted rather than refusing real clients.
        self.assertEqual(_loopback_allowed_hosts("0.0.0.0", 8080), frozenset())
        self.assertEqual(_loopback_allowed_hosts("192.168.1.10", 8080), frozenset())

    def test_find_rejects_non_string_query(self):
        """A non-string query answers 400, never a 500 with internal error text.

        Mirrors the /find-batch per-query type rule at the /find boundary: a
        dict or number would otherwise crash the lexer downstream and leak
        the exception message through a 500 payload.
        """
        port = self._start_server()
        for bad_query in ({"query": "push ebx"}, 12345, ["push ebx"]):
            status, payload = _post_json_status(port, "/find", {"query": bad_query})
            self.assertEqual(status, 400)
            self.assertIn("query must be a string", payload["error"])

    def test_find_rejects_non_numeric_params_cleanly(self):
        """Non-numeric find parameters answer a clean error payload, not a 500.

        A hostile or broken client sending e.g. ``"top_n": {"a": 1}`` must
        not crash int()/float() inside the handler: that surfaced as a 500
        echoing the internal exception text.  ``threshold`` is coerced in
        the same guard: a string threshold used to slip past the numeric
        conversions and raise TypeError at the range check, leaking a 500.
        """
        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        for bad in (
            {"top_n": "many"},
            {"ngram_size": [3]},
            {"jaccard_weight": {}},
            {"threshold": "high"},
        ):
            status, payload = _post_json_status(port, "/find", {"query": query, **bad})
            self.assertEqual(status, 400)
            self.assertIn("error", payload)
            self.assertIn("must be", payload["error"])

    def test_find_rejects_out_of_range_threshold_cleanly(self):
        """A threshold outside [0, 1] answers a clean error, never a 500.

        ResemblLSH rejects such values anyway; without this boundary check
        every out-of-range request surfaced as a 500 echoing that internal
        error instead of an error payload.
        """
        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        for bad_threshold in (-0.5, 1.5):
            status, payload = _post_json_status(
                port, "/find", {"query": query, "threshold": bad_threshold}
            )
            self.assertEqual(status, 400)
            self.assertIn("error", payload)
            self.assertIn("threshold", payload["error"])

    def test_find_rejects_non_finite_jaccard_weight_cleanly(self):
        """A NaN/Infinity/out-of-range jaccard_weight answers a clean error.

        Python's JSON decoder accepts bare ``NaN`` / ``Infinity`` literals,
        so an unvalidated weight let a hostile or broken client plant a
        non-finite value: every hybrid score became NaN, which silently
        broke the top-n ranking comparisons and serialized as a bare
        ``NaN`` token that no external JSON parser accepts.
        """
        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        for bad_weight in (float("nan"), float("inf"), -0.5, 1.5):
            status, payload = _post_json_status(
                port, "/find", {"query": query, "jaccard_weight": bad_weight}
            )
            self.assertEqual(status, 400)
            self.assertIn("error", payload)
            self.assertIn("jaccard_weight", payload["error"])

    def test_find_rejects_infinite_integer_params_cleanly(self):
        """``"top_n": Infinity`` answers a clean bad-request payload, not a 500.

        json.loads maps ``Infinity`` (and ``1e400``) to float infinity;
        ``int()`` on it raises OverflowError, which the numeric-coercion
        guard did not catch — the request surfaced as a 500 echoing internal
        error text instead of the clean error payload.
        """
        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        for bad in (
            {"top_n": float("inf")},
            {"ngram_size": float("inf")},
            {"num_permutations": float("inf")},
        ):
            status, payload = _post_json_status(port, "/find", {"query": query, **bad})
            self.assertEqual(status, 400)
            self.assertIn("error", payload)
            self.assertIn("must be", payload["error"])

    def test_find_treats_explicit_null_params_as_absent(self):
        """An explicit JSON null for a find parameter uses the configured default.

        ``dict.get``'s default only applies to absent keys, so a ``null``
        value used to crash int()/float() into a 500 (and ``"normalize":
        null`` silently disabled normalization) instead of meaning "not
        provided" like an omitted field.
        """
        from resembl.server import _find_one

        query = "push ebx\nmov eax, 5\npop ebx\nret"
        omitted = _find_one(self._session, {"query": query}, query)
        nulled = _find_one(
            self._session,
            {
                "query": query,
                "top_n": None,
                "threshold": None,
                "normalize": None,
                "ngram_size": None,
                "num_permutations": None,
                "jaccard_weight": None,
            },
            query,
        )
        self.assertEqual(nulled, omitted)
        self.assertIn("matches", nulled)

    def test_result_cache_evicts_oldest_beyond_max(self):
        """The version-guarded result cache evicts its oldest entry past the cap."""
        from resembl.server import _RESULT_CACHE, _RESULT_CACHE_MAX, _find_one

        _RESULT_CACHE.clear()
        self.addCleanup(_RESULT_CACHE.clear)

        def find(query: str) -> dict:
            return _find_one(self._session, {"top_n": 5}, query)

        first_query = "push ebx\nmov eax, 1000\npop ebx\nret"
        find(first_query)
        last_payload = None
        for i in range(_RESULT_CACHE_MAX + 5):
            last_payload = find(f"push ebx\nmov eax, {i}\npop ebx\nret")

        self.assertLessEqual(len(_RESULT_CACHE), _RESULT_CACHE_MAX)
        self.assertFalse(
            any(k[0] == first_query for k in _RESULT_CACHE), "oldest entry must have been evicted"
        )
        newest = f"push ebx\nmov eax, {_RESULT_CACHE_MAX + 4}\npop ebx\nret"
        self.assertTrue(any(k[0] == newest for k in _RESULT_CACHE), "newest entry must be retained")
        self.assertIsNotNone(last_payload)
        self.assertIn("matches", last_payload)

    def test_version_probe_is_released_with_the_server(self):
        """A closed server no longer holds the cache's version-probe engine.

        The probe is a second engine over the served database.  It was only
        weakly referenced from the served engine, so disposing the server
        left its connection open until the process collected both: a process
        cycling servers (embedded callers, this test module) accumulated one
        handle and pool per generation.
        """
        from resembl import server as server_mod
        from resembl.server import _VERSION_PROBES

        httpd = server_mod.serve(f"sqlite:///{self._db}", port=0)
        engine = httpd.engine
        with Session(engine) as session:
            # Reading the counter is what creates the probe.
            self.assertIsNotNone(server_mod._db_version(session))
        self.assertIn(engine, _VERSION_PROBES)

        httpd.server_close()
        self.assertNotIn(engine, _VERSION_PROBES)

    def test_thin_client_unreadable_file_errors_cleanly(self):
        """resembl-find --file with an unreadable file exits 1 without a traceback."""
        from resembl.find_client import _main

        rc = _main(["--file", "/nonexistent/resembl_query.asm"])
        self.assertEqual(rc, 1)

    def test_result_cache_invalidates_on_db_change(self):
        """Cached finds are served until the database changes (data_version)."""

        from resembl.server import _RESULT_CACHE

        _RESULT_CACHE.clear()
        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"

        def find_once() -> dict:
            return _post_json(port, "/find", {"query": query, "top_n": 5})

        first = find_once()
        self.assertGreater(first["lsh_candidates"], 0)
        cached = find_once()
        self.assertEqual(cached["lsh_candidates"], first["lsh_candidates"])
        self.assertEqual(len(_RESULT_CACHE), 1)

        # A DB change (add a snippet) must invalidate the cache entry.  A
        # different immediate yields a new checksum with the same minhash, so
        # the query's candidate count necessarily grows.
        from resembl.core import snippet_add

        snippet_add(self._session, "new_one", "push ebx\nmov eax, 250\npop ebx\nret")
        after = find_once()
        self.assertGreater(after["lsh_candidates"], first["lsh_candidates"])
        self.assertNotEqual(after["lsh_candidates"], first["lsh_candidates"])

    def test_result_cache_keys_isolate_served_databases(self):
        """One process serving two databases never shares result-cache hits.

        ``_RESULT_CACHE`` is shared by every server generation in the
        process, while SQLite's ``data_version`` counter is per database:
        two identically built fresh databases carry equal counters, so
        without the per-database key component a query against database B
        could be answered from database A's cached payload.
        """
        from resembl.server import _RESULT_CACHE, _find_one

        _RESULT_CACHE.clear()
        self.addCleanup(_RESULT_CACHE.clear)

        db_b = tempfile.mktemp(suffix=".db")
        engine_b = create_engine(f"sqlite:///{db_b}")
        self.addCleanup(engine_b.dispose)
        self.addCleanup(
            lambda: [
                os.remove(p) for p in (db_b, db_b + "-wal", db_b + "-shm") if os.path.exists(p)
            ]
        )
        SQLModel.metadata.create_all(engine_b)
        # Database A (setUp) holds 100 snippets; database B is empty.  Both
        # were built with the same operation sequence, so their
        # ``data_version`` counters match.
        session_b = Session(engine_b)
        self.addCleanup(session_b.close)

        params = ResemblConfig()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        payload_a = _find_one(self._session, {"top_n": 5}, query, params)
        payload_b = _find_one(session_b, {"top_n": 5}, query, params)

        self.assertGreater(payload_a["lsh_candidates"], 0)
        self.assertEqual(payload_b["lsh_candidates"], 0)
        # Each database owns its own entry; B must never see A's payload.
        self.assertEqual(len(_RESULT_CACHE), 2)
        again_b = _find_one(session_b, {"top_n": 5}, query, params)
        self.assertEqual(again_b["lsh_candidates"], 0)

    def test_second_serve_keeps_first_servers_find_defaults(self):
        """Each server generation carries its own find defaults.

        Two ``serve()`` calls in one process must not retarget the older,
        still-serving server's default n-gram / permutation / jaccard-weight
        values — the same hazard ``_FindHandler.engine`` documents for the
        engine itself.
        """
        from resembl import server as server_mod
        from resembl.config import ResemblConfig

        db_a = tempfile.mktemp(suffix=".db")
        db_b = tempfile.mktemp(suffix=".db")
        for db_path in (db_a, db_b):
            engine = create_engine(f"sqlite:///{db_path}")
            self.addCleanup(engine.dispose)
            SQLModel.metadata.create_all(engine)
        self.addCleanup(
            lambda: [
                os.remove(p)
                for p in (db_a, db_a + "-wal", db_a + "-shm", db_b, db_b + "-wal", db_b + "-shm")
                if os.path.exists(p)
            ]
        )

        with patch("resembl.config.load_config", return_value=ResemblConfig(ngram_size=5)):
            httpd_a = server_mod.serve(f"sqlite:///{db_a}", port=0)
        self.addCleanup(httpd_a.server_close)
        self.assertEqual(httpd_a.find_defaults.ngram_size, 5)

        with patch("resembl.config.load_config", return_value=ResemblConfig(ngram_size=7)):
            httpd_b = server_mod.serve(f"sqlite:///{db_b}", port=0)
        self.addCleanup(httpd_b.server_close)

        # The newer serve call must not have retargeted the older server.
        self.assertEqual(httpd_a.find_defaults.ngram_size, 5)
        self.assertEqual(httpd_b.find_defaults.ngram_size, 7)

    def test_concurrent_requests_all_succeed(self):
        """The server answers concurrent finds correctly (per-request sessions)."""
        import concurrent.futures

        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"

        # ThreadPoolExecutor.map supplies each item's index; the worker
        # ignores it but must accept the positional argument.
        def do_find(i: int) -> dict:  # pylint: disable=unused-argument
            return _post_json(port, "/find", {"query": query, "top_n": 5}, timeout=30)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(do_find, range(16)))
        self.assertEqual(len(results), 16)
        for payload in results:
            self.assertIn("matches", payload)
            self.assertEqual(len(payload["matches"]), 5)

    def test_handler_uses_keepalive_with_idle_timeout(self):
        """HTTP/1.1 keep-alive + idle timeout bound connection churn.

        Measured under concurrent load, connection churn (not request
        logic) was the cause of client-visible resets; keep-alive cut them
        ~8x.  The idle timeout bounds how long a kept-alive connection can
        hold its handler thread.
        """
        from resembl.server import _FindHandler

        self.assertEqual(_FindHandler.protocol_version, "HTTP/1.1")
        self.assertEqual(_FindHandler.timeout, 30)

    def test_read_body_rejects_deeply_nested_json(self):
        """A body nested past the decoder's recursion limit is a 400, not a crash.

        ``json.loads`` recurses once per nesting level, so a few hundred
        kilobytes of ``[[[...]]]`` raised ``RecursionError``.  That is not a
        ``ValueError``, so it escaped ``_read_body``'s guard and the
        handler's own ``except`` alike, killing the thread with no response
        at all.  Found by ``fuzzers/fuzz_find_request.py``.
        """
        from resembl.server import _FindHandler

        class _Stub:
            def __init__(self, body):
                self.headers = {"Content-Length": str(len(body))}
                self.rfile = io.BytesIO(body)

        depth = sys.getrecursionlimit() * 4
        for body in (b"[" * depth + b"]" * depth, b'{"a":' * depth + b"}" * depth):
            self.assertIsNone(_FindHandler._read_body(_Stub(body)))

    def test_ensure_tables_once_survives_concurrent_first_finds(self):
        """Concurrent LSH facade construction serializes the one-time DDL.

        Serve runs one handler thread per request, and the first finds
        after startup all construct ``ResemblLSH`` at once against a
        database whose tables may not exist yet.  Unsynchronized, two
        threads interleave ``create_all``'s has-table probe and CREATE
        TABLE, failing real requests with "table already exists".
        """
        from resembl import lsh as lsh_mod

        db_path = tempfile.mktemp(suffix=".db")
        self.addCleanup(
            lambda: [
                os.remove(p)
                for p in (db_path, db_path + "-wal", db_path + "-shm")
                if os.path.exists(p)
            ]
        )
        engine = create_engine(f"sqlite:///{db_path}")

        barrier = threading.Barrier(8)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                with Session(engine) as session:
                    lsh_mod._ensure_tables_once(session)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertIn(engine, lsh_mod._TABLES_ENSURED)

        # The marker is per engine, not process-wide: a second engine for a
        # different database in the same process must get its own DDL run —
        # with the old module-global flag it silently skipped table creation.
        db_path2 = tempfile.mktemp(suffix=".db")
        self.addCleanup(
            lambda: [
                os.remove(p)
                for p in (db_path2, db_path2 + "-wal", db_path2 + "-shm")
                if os.path.exists(p)
            ]
        )
        engine2 = create_engine(f"sqlite:///{db_path2}")
        with Session(engine2) as session:
            lsh_mod._ensure_tables_once(session)
        self.assertIn(engine2, lsh_mod._TABLES_ENSURED)

        # The index facade is usable on both engines' now-created tables.
        with Session(engine) as session:
            lsh = lsh_mod.ResemblLSH(session, 0.5, 128)
            self.assertGreaterEqual(lsh.b, 2)
        with Session(engine2) as session:
            lsh = lsh_mod.ResemblLSH(session, 0.5, 128)
            self.assertGreaterEqual(lsh.b, 2)

    def test_minhash_template_cache_survives_concurrent_construction(self):
        """Concurrent first finds at one permutation count build one template.

        Serve runs one handler thread per request and every find builds its
        query fingerprint via ``minhash_new``.  The template cache's
        check-then-insert used to be unlocked: N threads missing at once
        each paid the full permutation regeneration (~260 µs apiece).  With
        the lock, construction happens exactly once while every caller still
        receives identical fingerprints (same seeded permutations).
        """
        import time as time_mod
        from unittest.mock import patch as mock_patch

        import resembl.minhash as minhash_mod
        from resembl.scoring import _MINHASH_TEMPLATES, minhash_new

        num_perm = 97  # unused elsewhere in the suite
        _MINHASH_TEMPLATES.pop(num_perm, None)
        self.addCleanup(_MINHASH_TEMPLATES.pop, num_perm, None)

        constructions: list[int] = []

        class CountingMinHash(minhash_mod.MinHash):
            def __init__(self, *args, **kwargs):
                constructions.append(1)
                # Widen the race window so an unlocked check-then-insert is
                # caught deterministically by the count assertion below.
                time_mod.sleep(0.05)
                super().__init__(*args, **kwargs)

        barrier = threading.Barrier(8)
        fingerprints: list = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                m = minhash_new(num_perm)
                m.update(b"push ebx; ret")
                fingerprints.append(m.hashvalues.copy())
            except Exception as exc:
                errors.append(exc)

        with mock_patch.object(minhash_mod, "MinHash", CountingMinHash):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(fingerprints), 8)
        # Exactly one construction despite eight simultaneous first callers:
        # the losers must re-probe under the lock and hit the winner's entry.
        self.assertEqual(
            len(constructions),
            1,
            "template constructed more than once under concurrent first use",
        )
        self.assertIn(num_perm, _MINHASH_TEMPLATES)
        reference = fingerprints[0]
        for fingerprint in fingerprints[1:]:
            self.assertTrue((fingerprint == reference).all())

    def test_port_file_cleanup_keeps_foreign_advertisement(self):
        """An exiting serve must not delete another server's port file.

        Two serve processes started close together both pass the
        double-serve check (neither has written yet) and both bind; only
        the last writer owns the advertisement.  The loser exiting must
        leave the survivor discoverable to find clients.
        """
        from resembl.server import port_file_cleanup

        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        port_file = os.path.join(cache.name, "server_audit.port")

        # A foreign server's advertisement survives our exit.
        with open(port_file, "w", encoding="utf-8") as f:
            f.write("4242")
        port_file_cleanup(port_file, 1111)
        self.assertTrue(os.path.exists(port_file))
        with open(port_file, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "4242")

        # Our own advertisement is removed.
        with open(port_file, "w", encoding="utf-8") as f:
            f.write("1111")
        port_file_cleanup(port_file, 1111)
        self.assertFalse(os.path.exists(port_file))

        # A missing file is not an error.
        port_file_cleanup(port_file, 1111)


class TestResultCacheCoherence(unittest.TestCase):
    """The version guard holds across the connections of a pool."""

    def setUp(self):
        self._db = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(self._db) and os.remove(self._db))
        self._engine = create_engine(f"sqlite:///{self._db}", pool_size=4)
        SQLModel.metadata.create_all(self._engine)
        self._session = Session(self._engine)
        self.addCleanup(self._session.close)
        snippet_add_batch(
            self._session,
            [
                snippet_prepare(f"f{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3)
                for i in range(20)
            ],
        )

    def _write_from_another_process(self, code: str) -> None:
        """Commit a snippet on a connection the serving engine does not own."""
        import sqlite3

        prepared = snippet_prepare("ext", code, 3)
        assert prepared is not None, "test fixtures pass non-empty code"
        checksum, name, snippet_code, minhash = prepared
        raw = sqlite3.connect(self._db)
        try:
            raw.execute(
                "INSERT INTO snippet (checksum, names, code, minhash, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (checksum, f'["{name}"]', snippet_code, minhash, "[]"),
            )
            raw.commit()
        finally:
            raw.close()

    def test_stale_entry_not_served_by_a_second_pooled_connection(self):
        from resembl.server import _RESULT_CACHE, _find_one

        _RESULT_CACHE.clear()
        self.addCleanup(_RESULT_CACHE.clear)
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        body = {"top_n": 5}

        # Two live pooled connections, as the serving pool holds under load.
        first = Session(self._engine)
        second = Session(self._engine)
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        _find_one(first, body, query)
        self._write_from_another_process("push ecx\nmov eax, 250\npop ecx\nret")
        cached = _find_one(first, body, query)
        self.assertGreater(cached["lsh_candidates"], 0)

        # A second write, observed by the first connection only.  The other
        # connection still reports the counter value the payload was cached
        # under, and would hand back that same stale object.
        self._write_from_another_process("push edx\nmov eax, 251\npop edx\nret")
        after = _find_one(second, body, query)
        self.assertIsNot(after, cached, "a write from another connection must invalidate the entry")


class TestCLIServerEndToEnd(unittest.TestCase):
    """The real CLI `serve` + `find` wiring, via subprocesses."""

    def setUp(self):
        import tempfile

        self._db = tempfile.mktemp(suffix=".db")
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self._cache_dir = cache.name
        # Keep the test process and its subprocesses on the same cache dir.
        self._env_patch = patch.dict(
            os.environ,
            {
                "RESEMBL_CACHE_DIR": self._cache_dir,
                "DATABASE_URL": f"sqlite:///{self._db}",
            },
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        # Build a small database through the CLI itself.
        env = {
            **os.environ,
            "PYTHONPATH": os.path.abspath("."),
            "DATABASE_URL": f"sqlite:///{self._db}",
            "RESEMBL_CACHE_DIR": self._cache_dir,
        }
        subprocess.run(
            [
                sys.executable,
                "-m",
                "resembl.cli",
                "--quiet",
                "import",
                "--force",
                "--jobs",
                "2",
                "tests/test_data",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self._env = env

    def tearDown(self):
        for path in (self._db, self._db + "-wal", self._db + "-shm"):
            if os.path.exists(path):
                os.remove(path)

    def test_find_uses_running_server(self):
        """`find` answers via a running `serve` process (subprocess wiring)."""
        import time

        server = subprocess.Popen(
            [sys.executable, "-m", "resembl.cli", "serve", "--port", "0"],
            env=self._env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(server.terminate)
        try:
            # Wait for the port file.
            from resembl.paths import cache_dir_get, server_port_path

            port_file = server_port_path(f"sqlite:///{self._db}", cache_dir_get())
            deadline = time.monotonic() + 20
            while not os.path.exists(port_file) and time.monotonic() < deadline:
                time.sleep(0.1)
            if server.poll() is not None:
                self.fail(f"serve exited early: {server.stderr.read()}")
            if not os.path.exists(port_file):
                entries = (
                    os.listdir(self._cache_dir)
                    if os.path.isdir(self._cache_dir)
                    else "no cache dir"
                )
                self.fail(f"serve did not start; cache dir: {entries}; port_file: {port_file}")

            query_file = os.path.join("tests", "test_data", min(os.listdir("tests/test_data")))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "resembl.find_client",
                    "--file",
                    query_file,
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=self._env,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("matches", payload)
            self.assertGreater(payload["lsh_candidates"], 0)
        finally:
            server.terminate()
            server.wait(timeout=10)


class TestLazyPackageInit(unittest.TestCase):
    """`import resembl` must not eagerly load the heavy dependencies."""

    def test_import_is_light(self):
        """A bare ``import resembl`` must not pull the optional stack in.

        The check runs in a subprocess.  Popping the modules out of
        ``sys.modules`` and re-importing them in this process left two live
        copies of ``sqlmodel``/``pygments`` behind for every later test, and
        the assertion it supported (the fresh import must not load them) is a
        property of a fresh process anyway.
        """
        code = (
            "import sys, importlib; importlib.import_module('resembl'); "
            "heavy = [m for m in ('datasketch', 'scipy') if m in sys.modules]; "
            "assert not heavy, f'eagerly imported: {heavy}'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        # Lazy exports still resolve.
        from resembl import Snippet, code_tokenize, snippet_add

        self.assertTrue(callable(code_tokenize))
        self.assertTrue(callable(snippet_add))
        self.assertTrue(Snippet is not None)

    def test_submodule_access(self):
        import resembl

        # Every package submodule must resolve as an attribute after a bare
        # `import resembl`; resolution stays lazy (PEP 562) but must exist.
        for name in (
            "cache",
            "cli",
            "config",
            "core",
            "database",
            "find_client",
            "lsh",
            "models",
            "scoring",
            "server",
        ):
            with self.subTest(submodule=name):
                self.assertIsNotNone(getattr(resembl, name))

    def test_core_import_defers_scoring_stack(self):
        # Importing resembl.core must not pull in the tokenize/score stack:
        # every CLI invocation imports core, and light commands (list,
        # export, config, collections, ...) never lex or Levenshtein-score.
        # A subprocess isolates sys.modules from this test process's own
        # imports.  pygments.token stays eager (~1 ms); only the lexer
        # package and rapidfuzz are the deferred heavyweights.
        code = (
            "import sys, importlib; importlib.import_module('resembl.core'); "
            "heavy = [m for m in ('pygments.lexers', 'rapidfuzz') if m in sys.modules]; "
            "assert not heavy, f'eagerly imported: {heavy}'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            # The next line asserts returncode == 0 with a useful message;
            # a CalledProcessError would hide result.stderr from the output.
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class TestServerConcurrency(unittest.TestCase):
    """The warm server is shared by one handler thread per request.

    The tests drive the real HTTP endpoint against a real database: a mocked
    session cannot show what a second thread sees while the first one is
    mid-query.
    """

    def setUp(self):
        self._db = tempfile.mktemp(suffix=".db")
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self._engine = create_engine(f"sqlite:///{self._db}")
        SQLModel.metadata.create_all(self._engine)
        self._session = Session(self._engine)
        items = [
            snippet_prepare(f"f{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3) for i in range(100)
        ]
        snippet_add_batch(self._session, [x for x in items if x])
        self._env = patch.dict(
            os.environ,
            {
                "RESEMBL_CACHE_DIR": cache.name,
                "DATABASE_URL": f"sqlite:///{self._db}",
            },
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._session.close()
        for path in (self._db, self._db + "-wal", self._db + "-shm"):
            if os.path.exists(path):
                os.remove(path)

    def _start_server(self):
        from resembl.server import serve

        httpd = serve(f"sqlite:///{self._db}", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1]

    def _bucket_row_count(self) -> int:
        from sqlmodel import func, select

        from resembl.models import LSHBucket

        rows = select(func.count(1)).select_from(LSHBucket)
        return self._session.exec(rows).one()

    def test_request_cannot_rebuild_the_served_index(self):
        """Parameters the served index was not built for are refused, not built.

        Honouring them would drop the shared ``lsh_bucket`` table and rebuild
        it from a request thread while the other handler threads query that
        same table; two such requests interleave into an index that
        ``lsh_meta`` advertises as complete while most of its rows are gone,
        and every later find silently answers with a fraction of its matches.
        """
        from resembl.lsh import lsh_meta_get

        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        expected_meta = lsh_meta_get(self._session)
        expected_rows = self._bucket_row_count()
        self.assertGreater(expected_rows, 0)

        for field, value in (
            ("threshold", 0.6),
            ("ngram_size", 2),
            ("num_permutations", 64),
        ):
            with self.subTest(field=field):
                status, payload = _post_json_status(port, "/find", {"query": query, field: value})
                self.assertEqual(status, 400)
                self.assertIn(field, payload["error"])
                # The refusal is not cosmetic: the index other requests read
                # is byte-for-byte the one they were served.
                self.assertEqual(lsh_meta_get(self._session), expected_meta)
                self.assertEqual(self._bucket_row_count(), expected_rows)

        # The parameters the server does serve keep answering normally.
        self.assertEqual(_post_json(port, "/find", {"query": query})["lsh_candidates"], 100)

    def test_concurrent_finds_return_the_serial_result(self):
        """Concurrent requests answer exactly like one serial find."""
        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        expected = _post_json(port, "/find", {"query": query, "top_n": 5})

        results: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                payload = _post_json(port, "/find", {"query": query, "top_n": 5})
            except BaseException as exc:
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(payload)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "request thread did not finish")

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 12)
        for payload in results:
            self.assertEqual(payload, expected)

    def test_concurrent_index_builds_leave_a_complete_index(self):
        """Two builds racing in one process cannot publish a partial index.

        Each build drops the bucket table and repopulates it, then stamps
        ``lsh_meta``.  Interleaved, the last stamp to land describes rows the
        other build's ``DROP`` removed: the index looks complete and returns
        a fraction of its matches forever, with nothing left to rebuild it.
        """
        from sqlmodel import func, select

        from resembl.cache import lsh_index_build
        from resembl.lsh import banding_params, lsh_meta_get
        from resembl.models import Snippet

        def build(threshold: float) -> None:
            with Session(self._engine) as session:
                self.assertIsNotNone(lsh_index_build(session, threshold, 128))

        threads = [
            threading.Thread(target=build, args=(threshold,)) for threshold in (0.5, 0.6, 0.7)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
            self.assertFalse(thread.is_alive(), "index build did not finish")

        meta = lsh_meta_get(self._session)
        self.assertIsNotNone(meta)
        bands, _r = banding_params(meta[0], meta[1])
        num_snippets = self._session.exec(select(func.count(Snippet.checksum))).one()
        # Every snippet contributes exactly one row per band, and the rows
        # are unique by (band, bucket, checksum) — so the count is exact.
        self.assertEqual(self._bucket_row_count(), bands * num_snippets)
        self.assertEqual(num_snippets, 100)
