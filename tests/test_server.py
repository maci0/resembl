"""Tests for the warm ``serve`` server and the thin find client."""

# pylint: disable=protected-access  # tests exercise private internals
# pylint: disable=consider-using-with  # a test keeps a temp file, a temp
# directory or a child process open for the whole test body on purpose

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
from tests import process_stop, serve_stop_flags


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


class _ServeTestCase(unittest.TestCase):
    """A real database, a private cache dir and a warm server per test.

    :attr:`snippet_count` is the only thing a subclass varies; the fixture
    and its teardown are the same for every server suite.
    """

    snippet_count = 10

    def setUp(self):
        self._db = tempfile.mktemp(suffix=".db")
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self._cache_dir = cache.name
        self.addCleanup(self._remove_db)
        self._engine = create_engine(f"sqlite:///{self._db}")
        # Dispose before ``_remove_db`` runs: cleanups are last-in-first-out,
        # and Windows refuses to unlink a database file whose SQLite handle is
        # still open (WinError 32), which failed this suite's teardown there.
        # POSIX unlinks it happily, which is why the leak stayed invisible.
        self.addCleanup(self._engine.dispose)
        SQLModel.metadata.create_all(self._engine)
        self._session = Session(self._engine)
        self.addCleanup(self._session.close)
        items = [
            snippet_prepare(f"f{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3)
            for i in range(self.snippet_count)
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
        self.addCleanup(self._env.stop)

    def _remove_db(self):
        for path in (self._db, self._db + "-wal", self._db + "-shm"):
            if os.path.exists(path):
                os.remove(path)

    @staticmethod
    def _opener_with(open_side_effect):
        """Return a fake OpenerDirector whose open() has the given behavior."""
        from unittest.mock import MagicMock

        opener = MagicMock()
        opener.open.side_effect = open_side_effect
        return opener

    def _start_server(self):
        """Serve the fixture database on a background thread; return the port."""
        from resembl.server import serve

        httpd = serve(f"sqlite:///{self._db}", port=0)
        # A short poll interval keeps the teardown below prompt: it waits for
        # the loop to notice the stop, and the default 0.5 s poll would add
        # that wait to every test that serves.
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()

        # Stop the loop before closing the socket.  ``server_close`` alone
        # leaves a running ``serve_forever`` polling a closed descriptor: poll
        # reports it invalid on every pass, the ``accept`` that follows fails
        # and socketserver swallows that error, so the loop spins instead of
        # exiting.  One such thread per test kept burning a core for the rest
        # of the process and starved the request threads of the tests that ran
        # later, which is how the thin client's fixed 5 s timeout expired
        # (test_thin_client_queries_server).  Cleanups run last-in-first-out,
        # so registering close, check, join, shutdown runs shutdown, join,
        # check, close — each step on its own, so a failure in one still lets
        # the socket and the advertisement be released.
        def _assert_loop_stopped() -> None:
            self.assertFalse(thread.is_alive(), "the serve loop is still running")

        self.addCleanup(httpd.server_close)
        self.addCleanup(_assert_loop_stopped)
        self.addCleanup(thread.join, 10)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]


class TestServerMode(_ServeTestCase):
    """The server serves find queries equivalent to the in-process path."""

    snippet_count = 100

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

        with patch.object(server_mod, "lsh_index_build", return_value=object()) as mock_build:
            httpd = server_mod.serve(f"sqlite:///{self._db}", port=0)
            httpd.server_close()
        mock_build.assert_called_once()

    def test_startup_fails_when_index_build_fails(self):
        """A failed startup index build raises instead of serving no matches.

        Ignoring the ``None`` would leave the server answering every find
        with zero matches while ``lsh_meta`` still advertises a complete
        index — the silent-data-loss shape the reindex-failure check
        already refuses.
        """
        from unittest.mock import patch

        from resembl import server as server_mod
        from resembl.core import IndexBuildError

        with patch.object(server_mod, "lsh_index_build", return_value=None):
            with self.assertRaises(IndexBuildError):
                server_mod.serve(f"sqlite:///{self._db}", port=0)

    def test_startup_does_not_resolve_the_bind_address(self):
        """serve must not block on a reverse-DNS lookup to fill server_name.

        ``http.server.HTTPServer.server_bind`` sets ``server_name`` from
        ``socket.getfqdn(...)``.  That call is unbounded, and on the macOS CI
        runners it took 35 s, so the port file (what a find client waits for)
        appeared long after the serve lifecycle tests gave up.  Nothing in the
        server reads the field.
        """
        from unittest.mock import patch

        from resembl.server import serve as serve_start

        with patch("socket.getfqdn") as resolve:
            httpd = serve_start(f"sqlite:///{self._db}", port=0)
        try:
            resolve.assert_not_called()
            self.assertGreater(httpd.server_address[1], 0)
            self.assertEqual(httpd.server_name, "127.0.0.1")
            self.assertEqual(httpd.server_port, httpd.server_address[1])
        finally:
            httpd.server_close()

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
            self.assertEqual(response.status, 413)
            self.assertEqual(json.loads(response.read()), {"error": "request too large"})
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

    def test_conflicting_framing_is_refused_before_dispatch(self):
        """Conflicting lengths or transfer coding must not choose a JSON prefix."""
        import http.client

        port = self._start_server()
        body = b'{"query":"ret"}'
        for name, value in (
            ("Content-Length", str(len(body) + 1)),
            ("Content-Length", str(len(body))),
            ("Transfer-Encoding", "chunked"),
            ("Transfer-Encoding", "gzip"),
            ("tRaNsFeR-EnCoDiNg", ""),
        ):
            for method, path in (("POST", "/find"), ("GET", "/health")):
                for sent_body in (body, None):
                    with self.subTest(header=name, value=value, method=method, body=sent_body):
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                        try:
                            connection.putrequest(method, path)
                            connection.putheader("Content-Type", "application/json")
                            connection.putheader("Content-Length", str(len(body)))
                            connection.putheader(name, value)
                            connection.endheaders(sent_body)
                            response = connection.getresponse()
                            payload = json.loads(response.read())
                            self.assertEqual(response.status, 400)
                            self.assertIn("error", payload)
                            self.assertTrue(response.will_close)
                        finally:
                            connection.close()
        status, payload = _post_json_status(port, "/find", {"query": "ret"})
        self.assertEqual(status, 200)
        self.assertIn("matches", payload)

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

    def test_content_length_requires_ascii_decimal_digits(self):
        """HTTP framing does not accept Python integer signs or separators."""
        import http.client

        port = self._start_server()
        body = b'{"query":"ret"}'
        digits = str(len(body))
        for length in (f"+{digits}", f"{digits[0]}_{digits[1:]}"):
            with self.subTest(length=length):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    connection.request(
                        "POST",
                        "/find",
                        body,
                        {"Content-Type": "application/json", "Content-Length": length},
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(json.loads(response.read()), {"error": "bad request body"})
                finally:
                    connection.close()

    def test_truncated_framed_body_is_rejected(self):
        """A complete JSON prefix is not the complete declared HTTP body."""
        import http.client
        import socket

        port = self._start_server()
        body = b'{"query":"ret"}'
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(
            "POST",
            "/find",
            body,
            {"Content-Type": "application/json", "Content-Length": str(len(body) + 1)},
        )
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.read()), {"error": "bad request body"})

    def test_thin_client_queries_server(self):
        """resembl.find_client._main returns the matches via the server."""
        from resembl.find_client import _main

        self._start_server()
        rc = _main(["--query", "push ebx; mov eax, 5; pop ebx; ret", "--json"])
        self.assertEqual(rc, 0)

    def test_load_config_parses_toml(self):
        """find_client reads lsh_threshold/ngram_size from config.toml."""
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
            def __init__(self):
                import io as _io

                self._stream = _io.BytesIO(b'{"lsh_candidates": 0, "matches": []}')

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                return self._stream.read(size)

        # Signature-conformance stub for OpenerDirector.open; the real
        # call site passes timeout but the fake needs no fetch delay.
        def fake_open(request, timeout=5):  # pylint: disable=unused-argument
            captured["body"] = json.loads(request.data)
            return _FakeResponse()

        self._start_server()  # writes the port file
        with _patch(
            "resembl.find_client._load_config",
            return_value={"lsh_threshold": 0.7, "ngram_size": 2},
        ):
            with _patch(
                "urllib.request.build_opener",
                return_value=self._opener_with(fake_open),
            ):
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

    def test_thin_client_masks_password_with_empty_username(self):
        """Password-only database credentials remain private in failures."""
        from contextlib import redirect_stderr

        from resembl.find_client import _main

        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"RESEMBL_DATABASE_URL": "postgresql://:test-password@host/db"}),
            redirect_stderr(stderr),
        ):
            rc = _main(["--query", "ret", "--json"])
        self.assertEqual(rc, 1)
        message = json.loads(stderr.getvalue())["error"]["message"]
        self.assertNotIn("test-password", message)
        self.assertIn("postgresql://:***@host/db", message)

    def test_thin_client_masks_literal_password_characters(self):
        """All user-info password characters accepted by the URL parser are hidden."""
        from contextlib import redirect_stderr

        from sqlalchemy.engine import make_url

        from resembl.find_client import _main

        for user, password in (
            ("user", "sample/slash"),
            ("user", "sample space"),
            ("user", "sample?question"),
            ("user", "sample#fragment"),
            ("test user", "sample-password"),
        ):
            with self.subTest(user=user, password=password):
                url = f"postgresql://{user}:{password}@host/db"
                self.assertEqual(make_url(url).password, password)
                stderr = io.StringIO()
                with patch.dict(os.environ, {"RESEMBL_DATABASE_URL": url}), redirect_stderr(stderr):
                    rc = _main(["--query", "ret", "--json"])
                self.assertEqual(rc, 1)
                message = json.loads(stderr.getvalue())["error"]["message"]
                self.assertNotIn(password, message)
                self.assertIn(f"postgresql://{user}:***@host/db", message)

    def test_thin_client_masks_every_query_value(self):
        """Query keys remain useful diagnostics while every value stays private."""
        from contextlib import redirect_stderr
        from urllib.parse import unquote_plus

        from resembl.find_client import _main

        for key in ("password", "passwd", "%70assword", "sslpassword", "application_name"):
            with self.subTest(key=key):
                url = f"postgresql://user:sample-password@host:6543/db?{key}=sample-value&empty="
                stderr = io.StringIO()
                with patch.dict(os.environ, {"RESEMBL_DATABASE_URL": url}), redirect_stderr(stderr):
                    rc = _main(["--query", "ret", "--json"])
                self.assertEqual(rc, 1)
                message = json.loads(stderr.getvalue())["error"]["message"]
                self.assertNotIn("sample-value", message)
                self.assertNotIn("sample-password", message)
                self.assertIn(
                    f"postgresql://user:***@host:6543/db?{unquote_plus(key)}=***&empty=***",
                    message,
                )

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
                "urllib.request.build_opener",
                return_value=self._opener_with(
                    urllib.error.URLError(ConnectionRefusedError("connection refused"))
                ),
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
                for configured in (url, "sqlite:///another.db", "invalid-url"):
                    with self.subTest(configured=configured):
                        with patch.dict(os.environ, {"RESEMBL_DATABASE_URL": configured}):
                            db_url = _session_db_url(session)
                        self.assertEqual(db_url, url)
        finally:
            engine.dispose()
        # The password is rendered back, not masked to ***.
        self.assertEqual(db_url, url)
        # Naming the file after the masked URL would break discovery.
        self.assertNotEqual(
            server_port_path(db_url, cache_dir_get()),
            server_port_path(db_url_mask(url), cache_dir_get()),
        )

    def test_port_file_digest_preserves_configured_windows_path(self):
        """URL rendering must not change the address used by thin clients."""
        from sqlalchemy import create_engine as sa_create_engine
        from sqlmodel import Session

        from resembl.cli import _session_db_url
        from resembl.paths import server_port_path

        url = r"sqlite:///C:\Users\tester\assembly.db"
        engine = sa_create_engine(url)
        try:
            with patch.dict(os.environ, {"RESEMBL_DATABASE_URL": url}):
                with Session(engine) as session:
                    actual = _session_db_url(session)
        finally:
            engine.dispose()
        self.assertEqual(actual, url)
        self.assertEqual(
            server_port_path(actual, self._cache_dir), server_port_path(url, self._cache_dir)
        )

    def test_thin_client_propagates_error_payload(self):
        """A server error payload surfaces on stderr with a failing exit code."""
        import contextlib
        import io

        from resembl.find_client import _main

        class _FakeResponse:
            def __init__(self):
                import io as _io

                self._stream = _io.BytesIO(b'{"error": "threshold too high"}')

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                return self._stream.read(size)

        self._start_server()  # writes a valid port file
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with patch(
                "urllib.request.build_opener",
                return_value=self._opener_with(lambda request, timeout=5: _FakeResponse()),
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
                "urllib.request.build_opener",
                return_value=self._opener_with(
                    urllib.error.HTTPError(
                        "http://127.0.0.1/find",
                        400,
                        "Bad Request",
                        {},
                        io.BytesIO(json.dumps({"error": "threshold 5.0 is not in range"}).encode()),
                    )
                ),
            ):
                rc = _main(["--query", "mov eax, 5", "--threshold", "5"])
        self.assertEqual(rc, 1)
        self.assertIn("threshold 5.0 is not in", stderr.getvalue())
        self.assertNotIn("unreachable", stderr.getvalue())

    def test_clients_refuse_non_object_responses(self):
        """A JSON scalar or array is a malformed response, not a traceback."""
        import contextlib
        import io

        from resembl import cli
        from resembl.find_client import _main
        from resembl.paths import cache_dir_get, server_port_path

        self._start_server()
        port_file = server_port_path(f"sqlite:///{self._db}", cache_dir_get())
        for payload in (None, [], "result", 1, True):
            for thin in (True, False):
                with self.subTest(payload=payload, thin=thin):
                    stderr = io.StringIO()
                    opener = self._opener_with(
                        lambda request, timeout=5, payload=payload: io.BytesIO(
                            json.dumps(payload).encode()
                        )
                    )
                    with (
                        contextlib.redirect_stderr(stderr),
                        patch("urllib.request.build_opener", return_value=opener),
                        patch.object(cli.state, "session", self._session, create=True),
                    ):
                        if thin:
                            self.assertEqual(_main(["--query", "ret", "--json"]), 1)
                            error = json.loads(stderr.getvalue())["error"]
                            self.assertIn("malformed find response", error["message"])
                        else:
                            self.assertIsNone(cli._server_request("/find", {"query": "ret"}, 5))
                    self.assertTrue(os.path.exists(port_file))

    def test_clients_refuse_malformed_match_payloads(self):
        """Invalid result columns must not crash rendering or be emitted as success."""
        import contextlib
        import io

        from resembl import cli
        from resembl.find_client import _main

        self._start_server()
        match = {"checksum": "a" * 64, "names": ["ret"], "score": 1.0}
        payloads = [
            {"lsh_candidates": 0, "matches": None},
            {"lsh_candidates": 0, "matches": [None]},
            {"lsh_candidates": "bad", "matches": []},
            *(
                {"lsh_candidates": 1, "matches": [{**match, key: value}]}
                for key, value in (
                    ("checksum", None),
                    ("names", None),
                    ("names", [7]),
                    ("score", "bad"),
                    ("score", float("nan")),
                    ("score", 10**400),
                )
            ),
        ]
        for payload in payloads:
            for mode in ("text", "json", "cli", "batch"):
                with self.subTest(payload=payload, mode=mode):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    opener = self._opener_with(
                        lambda request, timeout=5, payload=payload, mode=mode: io.BytesIO(
                            json.dumps(
                                {"results": [{"query": "ret", **payload}]}
                                if mode == "batch"
                                else payload
                            ).encode()
                        )
                    )
                    with (
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                        patch("urllib.request.build_opener", return_value=opener),
                        patch.object(cli.state, "session", self._session, create=True),
                    ):
                        if mode == "batch":
                            self.assertIsNone(
                                cli._server_request("/find-batch", {"queries": ["ret"]}, 5)
                            )
                        elif mode == "cli":
                            self.assertIsNone(cli._server_request("/find", {"query": "ret"}, 5))
                        else:
                            args = ["--query", "ret"] + (["--json"] if mode == "json" else [])
                            self.assertEqual(_main(args), 1)
                            self.assertIn("malformed find response", stderr.getvalue())
                            self.assertEqual(stdout.getvalue(), "")

    def test_batch_reply_preserves_queries_and_error_rows(self):
        """Missing or mislabelled rows fall back; a valid per-query error remains usable."""
        import io

        from resembl import cli

        self._start_server()
        replies = [
            ({"results": None}, False),
            ({"results": []}, False),
            ({"results": [{"query": "other", "lsh_candidates": 0, "matches": []}]}, False),
            ({"results": [{"query": "ret", "error": 7}]}, False),
            ({"results": [{"query": "ret", "error": "query has no tokens"}]}, True),
        ]
        for payload, valid in replies:
            with self.subTest(payload=payload):
                opener = self._opener_with(
                    lambda request, timeout=5, payload=payload: io.BytesIO(
                        json.dumps(payload).encode()
                    )
                )
                with (
                    patch("urllib.request.build_opener", return_value=opener),
                    patch.object(cli.state, "session", self._session, create=True),
                ):
                    result = cli._server_request("/find-batch", {"queries": ["ret"]}, 5)
                self.assertEqual(result, payload if valid else None)

    def test_thin_client_text_strips_terminal_controls(self):
        """A server-supplied name or refusal must not execute terminal escapes."""
        import contextlib
        import io

        from resembl.find_client import _main

        self._start_server()
        hostile = "x\x1b[2J\r\x07\x9by"
        replies = [
            {
                "lsh_candidates": 1,
                "matches": [{"checksum": "a" * 64, "names": [hostile], "score": 1}],
            },
            {"error": hostile},
        ]
        for payload in replies:
            with self.subTest(payload=payload):
                stdout, stderr = io.StringIO(), io.StringIO()
                opener = self._opener_with(
                    lambda request, timeout=5, payload=payload: io.BytesIO(
                        json.dumps(payload).encode()
                    )
                )
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    patch("urllib.request.build_opener", return_value=opener),
                ):
                    rc = _main(["--query", "ret"])
                self.assertEqual(rc, 1 if "error" in payload else 0)
                rendered = stdout.getvalue() + stderr.getvalue()
                self.assertIn("x[2Jy", rendered)
                for control in ("\x1b", "\r", "\x07", "\x9b"):
                    self.assertNotIn(control, rendered)
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    patch("urllib.request.build_opener", return_value=opener),
                ):
                    _main(["--query", "ret", "--json"])
                if "error" in payload:
                    self.assertEqual(json.loads(stderr.getvalue())["error"]["message"], hostile)
                else:
                    self.assertEqual(
                        json.loads(stdout.getvalue())["matches"][0]["names"], [hostile]
                    )

    def test_thin_client_rejects_deep_json_responses(self):
        """Decoder recursion errors use the client failure envelope."""
        from contextlib import redirect_stderr, redirect_stdout

        from resembl.find_client import _main

        self._start_server()
        # Exercise the recursive stdlib decoder even on Python with an
        # iterative C decoder; supported Python builds can use either.
        decoder = json.JSONDecoder()
        decoder.scan_once = json.scanner.py_make_scanner(decoder)
        depth = sys.getrecursionlimit() * 2
        body = b'{"error":' + b"[" * depth + b"0" + b"]" * depth + b"}"
        for status in (200, 400):
            for json_mode in (False, True):
                with self.subTest(status=status, json_mode=json_mode):
                    reply = (
                        (lambda request, timeout=5: io.BytesIO(body))
                        if status == 200
                        else urllib.error.HTTPError(
                            "http://127.0.0.1/find", status, "Bad Request", {}, io.BytesIO(body)
                        )
                    )
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        redirect_stdout(stdout),
                        redirect_stderr(stderr),
                        patch("json._default_decoder", decoder),
                        patch("urllib.request.build_opener", return_value=self._opener_with(reply)),
                    ):
                        rc = _main(["--query", "ret"] + (["--json"] if json_mode else []))
                    self.assertEqual(rc, 1)
                    self.assertEqual(stdout.getvalue(), "")
                    if json_mode:
                        self.assertEqual(json.loads(stderr.getvalue())["error"]["kind"], "failure")
                    else:
                        self.assertTrue(stderr.getvalue().startswith("error: "))

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
                import io as _io

                self._stream = _io.BytesIO(body)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                return self._stream.read(size)

        self._start_server()  # writes a valid port file
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with patch(
                "urllib.request.build_opener",
                return_value=self._opener_with(
                    lambda request, timeout=5: _FakeResponse(json.dumps(payload).encode("utf-8"))
                ),
            ):
                rc = _main(["--query", "mov eax, 5"])
        out = stdout.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Found 2 candidates via LSH.", stderr.getvalue())
        self.assertNotIn("Found 2 candidates via LSH.", out)
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

        # Every other verb the base class does not implement (HEAD, OPTIONS)
        # is answered by this handler too, not by the stdlib's HTML 501.
        for method in ("HEAD", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, body = request(method, "/find")
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("Allow"), "POST")
                self.assertEqual(headers.get("Content-Type"), "application/json")
                if method == "HEAD":
                    # A HEAD answer carries the headers a GET produces and no
                    # body bytes; anything else desynchronizes keep-alive.
                    self.assertEqual(body, b"")
                    self.assertGreater(int(headers["Content-Length"]), 0)
                else:
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
        from resembl.server import MAX_BATCH_QUERIES

        port = self._start_server()
        queries = ["mov eax, 5"] * (MAX_BATCH_QUERIES + 1)
        status, payload = _post_json_status(port, "/find-batch", {"queries": queries})
        self.assertEqual(status, 400)
        self.assertIn(str(MAX_BATCH_QUERIES), payload["error"])

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

    def test_find_rejects_non_positive_top_n(self):
        """A top_n below 1 answers 400, not 200 with an empty match list.

        ``config.VALUE_BOUNDS`` already refuses a non-positive ``top_n`` for
        the CLI, and a non-positive one silently truncates the ranking to
        nothing: a client asking for ``top_n: -1`` (a miscomputed cap) read
        the empty ``matches`` as "this snippet has no duplicates".
        """
        port = self._start_server()
        for top_n in (0, -1):
            with self.subTest(top_n=top_n):
                status, payload = _post_json_status(
                    port, "/find", {"query": "push ebx\nret", "top_n": top_n}
                )
                self.assertEqual(status, 400)
                self.assertIn("top_n", payload["error"])

    def test_find_batch_rejects_non_positive_top_n(self):
        """The batch endpoint applies the same top_n bound as /find."""
        port = self._start_server()
        status, payload = _post_json_status(
            port, "/find-batch", {"queries": ["push ebx\nret"], "top_n": 0}
        )
        self.assertEqual(status, 400)
        self.assertIn("top_n", payload["error"])

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

    def test_custom_loopback_bind_accepts_only_its_configured_ip(self):
        """A bound loopback literal works without admitting sibling addresses."""
        import errno
        import http.client

        from resembl.server import _loopback_allowed_hosts, serve

        bind_host = "127.0.0.2"
        try:
            httpd = serve(None, host=bind_host, port=0)
        except OSError as exc:
            if exc.errno != errno.EADDRNOTAVAIL:
                raise
            # macOS needs an alias for extra loopback IPs; exercise the same
            # Host policy over a reachable transport without privileged setup.
            bind_host = "127.0.0.1"
            httpd = serve(None, host=bind_host, port=0)
            httpd.allowed_hosts = _loopback_allowed_hosts("127.0.0.2", httpd.server_address[1])
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        port = httpd.server_address[1]
        try:
            for host, expected in (
                (f"127.0.0.2:{port}", 200),
                ("127.0.0.2", 200),
                (f"attacker.example:{port}", 403),
                (f"127.0.0.3:{port}", 403),
            ):
                with self.subTest(host=host):
                    conn = http.client.HTTPConnection(bind_host, port, timeout=5)
                    try:
                        conn.request("GET", "/health", headers={"Host": host})
                        response = conn.getresponse()
                        self.assertEqual(response.status, expected, response.read())
                    finally:
                        conn.close()
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()
        self.assertFalse(thread.is_alive())

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
        expanded = "0:0:0:0:0:0:0:1"
        ipv6 = _loopback_allowed_hosts(expanded, 8080)
        self.assertIn(f"[{expanded}]", ipv6)
        self.assertIn(f"[{expanded}]:8080", ipv6)
        self.assertNotIn(f"{expanded}:8080", ipv6)

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

    def test_find_rejects_blank_query(self):
        """A blank query answers 400, not 200 with an empty match list.

        ``resembl find`` refuses a query it reads as empty, and a blank one
        fingerprints to an empty token set: the endpoint answered ``200``
        with no ``matches``, which a client reads as "this snippet has no
        duplicates" rather than as a request that searched for nothing.
        """
        port = self._start_server()
        for blank in ("", "   ", "\n\t"):
            with self.subTest(query=blank):
                status, payload = _post_json_status(port, "/find", {"query": blank})
                self.assertEqual(status, 400)
                self.assertIn("query must not be empty", payload["error"])

    def test_find_batch_isolates_unusable_queries(self):
        """A blank or wrongly typed entry fails alone; the rest still answer."""
        port = self._start_server()
        status, payload = _post_json_status(
            port, "/find-batch", {"queries": ["push ebx\nret", "   ", 7]}
        )
        self.assertEqual(status, 200)
        found, blank, wrong_type = payload["results"]
        self.assertIn("matches", found)
        self.assertEqual(blank["error"], "query must not be empty")
        self.assertEqual(wrong_type["error"], "query must be a string")

    def test_served_top_n_default_stays_within_the_request_bound(self):
        """A configured top_n above the request cap still answers requests.

        The config layer leaves ``top_n`` unbounded while the request path
        caps it, so the configured value became a default the endpoint
        refused: every request omitting ``top_n`` — what a plain third-party
        client sends — was answered ``400`` over a value it never sent.
        """
        from resembl.server import _MAX_TOP_N

        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        with open(os.path.join(config_dir.name, "config.toml"), "w", encoding="utf-8") as f:
            f.write(f"top_n = {_MAX_TOP_N * 2}\n")
        with patch.dict(os.environ, {"RESEMBL_CONFIG_DIR": config_dir.name}):
            with self.assertLogs("resembl.server", level="WARNING"):
                port = self._start_server()
            status, payload = _post_json_status(port, "/find", {"query": "push ebx\nret"})
        self.assertEqual(status, 200)
        self.assertIn("matches", payload)

    def test_find_rejects_non_boolean_normalize(self):
        """A non-boolean ``normalize`` answers 400, it is not truthy-coerced.

        ``bool("false")`` is True, so a client sending the string asked for
        normalization off and silently searched the normalized corpus,
        getting a 200 with the wrong matches.
        """
        port = self._start_server()
        for bad in ("false", 0, 1, []):
            status, payload = _post_json_status(
                port, "/find", {"query": "push ebx\nret", "normalize": bad}
            )
            self.assertEqual(status, 400)
            self.assertIn("normalize", payload["error"])
        # The boolean itself still works, both ways.
        for good in (True, False):
            status, _payload = _post_json_status(
                port, "/find", {"query": "push ebx\nret", "normalize": good}
            )
            self.assertEqual(status, 200)

    def test_every_method_answers_the_json_envelope(self):
        """Every verb answers the JSON error envelope, never the stdlib's HTML.

        ``BaseHTTPRequestHandler`` answers an unhandled method with
        ``send_error(501)``, an HTML page: a client parsing responses as JSON
        got a decode error instead of the status, on exactly the methods
        (``HEAD``, ``OPTIONS``) it probes first.
        """
        import http.client

        port = self._start_server()
        for method in ("HEAD", "OPTIONS", "PUT", "DELETE", "PATCH", "TRACE"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request(method, "/find")
                response = conn.getresponse()
                body = response.read()
                self.assertEqual(response.status, 405, method)
                self.assertEqual(response.getheader("Allow"), "POST", method)
                self.assertEqual(response.getheader("Content-Type"), "application/json", method)
                self.assertEqual(response.getheader("Cache-Control"), "no-store", method)
                # HEAD carries the status and headers of the GET it stands
                # for, and no body.
                if method == "HEAD":
                    self.assertEqual(body, b"")
                else:
                    self.assertIn("method not allowed", json.loads(body)["error"])
            finally:
                conn.close()

    def test_unknown_path_is_404_on_every_method(self):
        """An unknown path answers 404 whatever verb addressed it.

        Answering 405 to ``GET /nope`` reported a method problem for a path
        that does not exist, sending the client to debug its verb instead of
        its URL.
        """
        import http.client

        port = self._start_server()
        for method in ("GET", "POST", "HEAD"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request(method, "/nope", body=b"{}" if method == "POST" else None)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 404, method)
            finally:
                conn.close()

    def test_query_string_does_not_change_the_route(self):
        """``/find?trace=1`` is the ``/find`` endpoint.

        ``self.path`` carries the query string, so a client appending one (a
        browser, a proxy, a logging library) addressed a path the server does
        not serve and got a 404 for a request the documentation calls valid.
        The find parameters are read from the body, so it is dropped.
        """
        port = self._start_server()
        plain = _post_json(port, "/find", {"query": "push ebx\nret"})
        with_query = _post_json(port, "/find?trace=1&top_n=1", {"query": "push ebx\nret"})
        self.assertEqual(plain["lsh_candidates"], with_query["lsh_candidates"])

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

    def test_find_rejects_fractional_count_params(self):
        """A fractional ``top_n`` / ``ngram_size`` / ``num_permutations`` is a 400.

        ``int()`` truncates: ``2.9`` permutations became a valid 2 and
        ``5.9`` results became 5, so the server answered as if the client
        had sent a number it did not.  An integral float stays legal.
        """
        from resembl.server import _find_one

        query = "push ebx\nmov eax, 5\npop ebx\nret"
        port = self._start_server()
        for bad in ({"top_n": 5.9}, {"ngram_size": 2.5}, {"num_permutations": 2.9}):
            status, payload = _post_json_status(port, "/find", {"query": query, **bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("whole number", payload["error"])
        # ``3.0`` is the same value as ``3`` and must still be accepted.
        self.assertIn("matches", _find_one(self._session, {"query": query, "top_n": 3.0}, query))

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

    def test_omitted_threshold_defaults_to_the_served_one(self):
        """A request that omits ``threshold`` searches the server's own index.

        The served index is built for the server's configured
        ``lsh_threshold`` and a request naming any other one is refused, so
        an omitted field has to resolve to that same value.  It used to
        resolve to the module default instead, which answered ``400`` to a
        plain ``{"query": ...}`` on every server configured with anything
        other than 0.5 — blaming the client's omission for the mismatch.
        """
        from resembl.config import ResemblConfig
        from resembl.server import BadRequestError, _parse_find_request

        params = ResemblConfig(lsh_threshold=0.7, ngram_size=3, num_permutations=128)
        request = _parse_find_request({"query": "push ebx\nret"}, params)
        self.assertEqual(request.effective_threshold, 0.7)
        # An explicit ``null`` means the same thing, and a name that differs
        # from the served index is still refused.
        nulled = _parse_find_request({"query": "push ebx\nret", "threshold": None}, params)
        self.assertEqual(nulled.effective_threshold, 0.7)
        with self.assertRaises(BadRequestError):
            _parse_find_request({"query": "push ebx\nret", "threshold": 0.5}, params)

    def test_result_cache_refuses_an_oversized_payload(self):
        """The entry count is not a memory bound; a huge payload is not stored.

        ``top_n`` reaches 1000 and every match carries its snippet's whole
        name list, so 128 cached large finds pinned an unbounded multiple of
        the entry cap.  The oversized entry is simply not cached: the next
        request recomputes it and answers the same.
        """
        from resembl.server import (
            _RESULT_CACHE,
            _RESULT_CACHE_MAX_CHARS,
            _result_cache_put,
        )

        _RESULT_CACHE.clear()
        self.addCleanup(_RESULT_CACHE.clear)

        def payload(match_count: int, name: str) -> dict:
            return {
                "lsh_candidates": match_count,
                "matches": [
                    {"checksum": f"{i:064x}", "names": [name], "score": 1.0}
                    for i in range(match_count)
                ],
            }

        small = payload(3, "push")
        _result_cache_put(("small",), 1, small)
        self.assertEqual(len(_RESULT_CACHE), 1)

        big = payload(64, "n" * (_RESULT_CACHE_MAX_CHARS // 32))
        _result_cache_put(("big",), 1, big)
        self.assertEqual(len(_RESULT_CACHE), 1, "an oversized payload must not be stored")

    def test_result_cache_is_isolated_from_caller_mutation(self):
        """A caller mutating a payload must not rewrite the cached answer.

        Handlers hold the returned dict past the cache lock (batch merges
        it), so the cache must copy on the way in and on the way out: mutating
        the computed payload after ``put`` and mutating a ``get`` result must
        both leave the stored entry byte-identical.
        """
        from resembl.server import _RESULT_CACHE, _result_cache_get, _result_cache_put

        _RESULT_CACHE.clear()
        self.addCleanup(_RESULT_CACHE.clear)

        payload = {
            "lsh_candidates": 1,
            "matches": [{"checksum": "a" * 64, "names": ["x"], "score": 1.0}],
        }
        _result_cache_put(("k",), 1, payload)
        payload["lsh_candidates"] = 999
        payload["matches"][0]["names"].append("EVIL")
        first = _result_cache_get(("k",), 1)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first["lsh_candidates"], 1)
        self.assertEqual(first["matches"][0]["names"], ["x"])
        first["matches"].append({"checksum": "b" * 64, "names": [], "score": 0.0})
        again = _result_cache_get(("k",), 1)
        self.assertIsNotNone(again)
        assert again is not None
        self.assertEqual(len(again["matches"]), 1)

    def test_result_cache_evicts_oldest_beyond_max(self):
        """The version-guarded result cache evicts its oldest entry past the cap."""
        from resembl.server import (
            _RESULT_CACHE,
            _RESULT_CACHE_MAX,
            _find_one,
            _query_cache_key,
        )

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
            any(k[0] == _query_cache_key(first_query) for k in _RESULT_CACHE),
            "oldest entry must have been evicted",
        )
        newest = f"push ebx\nmov eax, {_RESULT_CACHE_MAX + 4}\npop ebx\nret"
        self.assertTrue(
            any(k[0] == _query_cache_key(newest) for k in _RESULT_CACHE),
            "newest entry must be retained",
        )
        self.assertIsNotNone(last_payload)
        self.assertIn("matches", last_payload)

    def test_result_cache_key_does_not_retain_the_query(self):
        """A cache key is a fixed-size digest, not the request's query text.

        The body cap is 8 MiB, so keeping the query verbatim made the
        128-entry cap an entry count rather than a memory bound: 128 large
        requests pinned a gigabyte of key strings.  Two distinct queries
        must still land on distinct entries.
        """
        from resembl.server import _query_cache_key

        small = _query_cache_key("push ebx; ret")
        large = _query_cache_key("mov eax, 1; " + "; nop\n" * 400_000)

        self.assertEqual(small, _query_cache_key("push ebx; ret"))
        self.assertNotEqual(small, large)
        self.assertLessEqual(len(small.encode()), 64)
        self.assertLessEqual(len(large.encode()), 64)
        # A request the cache must never confuse with another: the key is
        # the only thing separating their payloads.
        self.assertNotEqual(_query_cache_key("push ebx; ret "), small)

    def test_result_cache_computes_a_missed_key_once(self):
        """Concurrent requests for one cold key never recompute at once.

        ``serve`` runs a handler thread per connection, so without the
        per-key compute lock every request in a burst of identical queries
        ran the same find simultaneously, each holding its own database
        connection and LSH query for the whole of it.
        """
        from resembl import server as server_mod
        from resembl.server import _INFLIGHT, _RESULT_CACHE, _find_one

        _RESULT_CACHE.clear()
        self.addCleanup(_RESULT_CACHE.clear)
        _INFLIGHT.clear()
        self.addCleanup(_INFLIGHT.clear)

        real_find = server_mod.snippet_find_matches
        first_entered = threading.Semaphore(0)
        release = threading.Event()
        guard = threading.Lock()
        calls = 0
        in_flight = 0
        peak = 0

        def counting_find(*args, **kwargs):
            nonlocal calls, in_flight, peak
            with guard:
                calls += 1
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                first_entered.release()
                # Long enough for every racer to reach the key if it is not
                # serialized behind the first.
                release.wait(10)
                return real_find(*args, **kwargs)
            finally:
                with guard:
                    in_flight -= 1

        query = "push ebx\nmov edi, 99\npop ebx\nret"
        workers = 8
        payloads: list[dict] = []
        errors: list[BaseException] = []

        def run() -> None:
            # A session per thread, as ``_handle_find`` gives each request.
            with Session(self._session.get_bind()) as session:
                try:
                    payloads.append(_find_one(session, {"top_n": 5}, query))
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(workers)]
        with patch.object(server_mod, "snippet_find_matches", counting_find):
            for t in threads:
                t.start()
            first_entered.acquire(timeout=10)
            release.set()
            for t in threads:
                t.join(timeout=30)
                self.assertFalse(t.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(len(payloads), workers)
        # The property under test: the key's find ran alone.  (More than one
        # call in total is correct — a find that has to build the index
        # commits, which advances the version the next request re-samples,
        # so the one request behind the lock legitimately recomputes once.)
        self.assertEqual(peak, 1)
        self.assertLess(calls, workers)
        # Every request was answered the same way.
        self.assertEqual(payloads.count(payloads[0]), workers)
        self.assertEqual(len(_RESULT_CACHE), 1)
        # The registry is emptied as the keys finish: it tracks the keys
        # being computed, not every query ever asked for.
        self.assertEqual(_INFLIGHT, {})

    def test_result_cache_skips_a_payload_filed_under_a_stale_version(self):
        """A write committed during the find must not file the payload.

        The version sampled before the find is the only guard the next
        request has; a payload computed for that version but stored after
        the database moved on is stale by the time it is read back, and
        serving it violates read-your-writes for the writer whose commit
        landed mid-find.
        """
        from unittest.mock import patch as mock_patch

        from resembl import server as server_mod
        from resembl.core import snippet_add
        from resembl.server import _RESULT_CACHE, _db_version, _find_one

        _RESULT_CACHE.clear()
        self.addCleanup(_RESULT_CACHE.clear)
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        _db_version(self._session)  # warm the probe
        real_find = server_mod.snippet_find_matches
        written = {"done": False}

        def find_then_write(*args, **kwargs):
            payload = real_find(*args, **kwargs)
            # A concurrent writer commits while this find is in flight.
            if not written["done"]:
                written["done"] = True
                snippet_add(self._session, "mid_find", "push ebx\nmov eax, 123\npop ebx\nret")
            return payload

        with mock_patch.object(server_mod, "snippet_find_matches", find_then_write):
            first = _find_one(self._session, {"top_n": 5}, query)
        # The computing caller still gets its answer...
        self.assertIn("matches", first)
        # ...but it is not cached after the concurrent commit, so the next
        # request recomputes against the new version and sees the new row.
        second = _find_one(self._session, {"top_n": 5}, query)
        self.assertIn("matches", second)
        self.assertGreater(second["lsh_candidates"], first["lsh_candidates"])
        # The freshly computed entry is the current one.
        self.assertEqual(len(_RESULT_CACHE), 1)
        (entry,) = _RESULT_CACHE.values()
        self.assertEqual(entry[0], _db_version(self._session))

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

    def test_result_cache_invalidates_on_db_file_replace(self):
        """A replaced database file invalidates the cache despite equal data_version.

        A restore or backup copy puts a different file under the served
        path: the new file's ``PRAGMA data_version`` restarts from the same
        small counter, so the version guard alone would serve the previous
        corpus's cached finds forever.  The file fingerprint in the guard
        token is what catches the replace.
        """
        import shutil

        from resembl.server import _RESULT_CACHE, _find_one

        _RESULT_CACHE.clear()
        self.addCleanup(_RESULT_CACHE.clear)
        params = ResemblConfig()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        # Build the index before testing cache entries for a stable corpus.
        snippet_find_matches(self._session, query, top_n=5)
        first = _find_one(self._session, {"top_n": 5}, query, params)
        self.assertGreater(first["lsh_candidates"], 0)
        self.assertEqual(len(_RESULT_CACHE), 1)

        # Build a different database and copy its file over the served path,
        # the way a restore would leave the directory.
        other = tempfile.mktemp(suffix=".db")
        other_engine = create_engine(f"sqlite:///{other}")
        SQLModel.metadata.create_all(other_engine)
        from resembl.core import snippet_add_batch, snippet_prepare

        with Session(other_engine) as other_session:
            items = [
                snippet_prepare(f"g{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3)
                for i in range(5)
            ]
            snippet_add_batch(other_session, [x for x in items if x])
        other_engine.dispose()
        shutil.copyfile(other, self._db)
        for suffix in ("-wal", "-shm"):
            sidecar = self._db + suffix
            if os.path.exists(sidecar):
                os.remove(sidecar)
        for path in (other, other + "-wal", other + "-shm"):
            if os.path.exists(path):
                os.remove(path)

        after = _find_one(self._session, {"top_n": 5}, query, params)
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
        self.addCleanup(
            lambda: [
                os.remove(p) for p in (db_b, db_b + "-wal", db_b + "-shm") if os.path.exists(p)
            ]
        )
        # Registered after the removal so it runs before it: a cleanup that
        # unlinks a database file Windows still has open fails with WinError 32.
        self.addCleanup(engine_b.dispose)
        SQLModel.metadata.create_all(engine_b)
        # Database A (setUp) holds 100 snippets; database B is empty.  Both
        # were built with the same operation sequence, so their
        # ``data_version`` counters match.
        session_b = Session(engine_b)
        self.addCleanup(session_b.close)

        params = ResemblConfig()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        # Each cold index build commits; the following reads must be stable.
        snippet_find_matches(self._session, query, top_n=5)
        snippet_find_matches(session_b, query, top_n=5)
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
        engines = []
        for db_path in (db_a, db_b):
            engine = create_engine(f"sqlite:///{db_path}")
            engines.append(engine)
            SQLModel.metadata.create_all(engine)
        # Registered before the disposes below: cleanups run
        # last-in-first-out, so both handles are released first, which is what
        # Windows requires to unlink a database file.
        self.addCleanup(
            lambda: [
                os.remove(p)
                for p in (db_a, db_a + "-wal", db_a + "-shm", db_b, db_b + "-wal", db_b + "-shm")
                if os.path.exists(p)
            ]
        )
        for engine in engines:
            self.addCleanup(engine.dispose)

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
        # After the removal cleanup, so it runs first (see above).
        self.addCleanup(engine.dispose)

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
        # After the removal cleanup above, so it runs before it: Windows
        # refuses to unlink a database file whose handle is still open.
        self.addCleanup(engine2.dispose)
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

    def test_server_close_keeps_non_utf8_port_file(self):
        """A corrupted advertisement must not break server shutdown."""
        from resembl.paths import server_port_path
        from resembl.server import serve

        httpd = serve(f"sqlite:///{self._db}", port=0)
        self.addCleanup(httpd.server_close)
        port_file = server_port_path(f"sqlite:///{self._db}", self._cache_dir)
        with open(port_file, "wb") as stream:
            stream.write(b"\xff")
        try:
            httpd.server_close()
            with open(port_file, "rb") as stream:
                self.assertEqual(stream.read(), b"\xff")
        finally:
            os.remove(port_file)

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


class TestScoreEndpoints(_ServeTestCase):
    """``POST /score`` and ``POST /band-keys`` fingerprint caller-supplied text."""

    def _stored(self, index: int):
        """The fixture snippet ``f<index>``."""
        from resembl.models import Snippet

        code = f"push ebx\nmov eax, {index}\npop ebx\nret"
        return next(s for s in Snippet.get_all(self._session) if s.code == code)

    def test_score_matches_compare_of_the_same_listings(self):
        from resembl.core import snippet_compare

        port = self._start_server()
        first, second = self._stored(1), self._stored(2)
        other = "xor ecx, ecx\nlea esi, [edi+8]\ncall dword ptr [eax]\nleave\nret 8"
        reply = _post_json(
            port, "/score", {"source": first.code, "candidates": [second.code, first.code, other]}
        )
        compared = snippet_compare(self._session, first.checksum, second.checksum)
        assert compared is not None
        self.assertEqual(len(reply["scores"]), 3)
        self.assertAlmostEqual(reply["scores"][0], compared["comparison"]["hybrid_score"])
        self.assertAlmostEqual(reply["scores"][1], 100.0)
        self.assertLess(reply["scores"][2], reply["scores"][0])

    def test_score_prunes_candidates_below_the_jaccard_floor(self):
        port = self._start_server()
        source = self._stored(1).code
        other = "xor ecx, ecx\nlea esi, [edi+8]\ncall dword ptr [eax]\nleave\nret 8"
        reply = _post_json(
            port, "/score", {"source": source, "candidates": [other, source], "jaccard_floor": 0.9}
        )
        self.assertIsNone(reply["scores"][0])
        self.assertAlmostEqual(reply["scores"][1], 100.0)

    def test_score_honours_the_jaccard_weight(self):
        port = self._start_server()
        source = self._stored(1).code
        other = "xor ecx, ecx\nlea esi, [edi+8]\ncall dword ptr [eax]\nleave\nret 8"
        text_only = _post_json(
            port, "/score", {"source": source, "candidates": [other], "jaccard_weight": 0.0}
        )
        from rapidfuzz import fuzz

        self.assertAlmostEqual(text_only["scores"][0], fuzz.ratio(source, other))

    def test_score_scores_a_blank_candidate_zero_or_prunes_it(self):
        port = self._start_server()
        source = self._stored(1).code
        self.assertEqual(
            _post_json(port, "/score", {"source": source, "candidates": ["  "]})["scores"], [0.0]
        )
        pruned = _post_json(
            port, "/score", {"source": source, "candidates": [""], "jaccard_floor": 0.1}
        )
        self.assertEqual(pruned["scores"], [None])

    def test_score_refuses_malformed_requests(self):
        port = self._start_server()
        for body, field in (
            ({"candidates": []}, "source"),
            ({"source": "  ", "candidates": []}, "source"),
            ({"source": "ret"}, "candidates"),
            ({"source": "ret", "candidates": "ret"}, "candidates"),
            ({"source": "ret", "candidates": [5]}, "candidates"),
            ({"source": "ret", "candidates": ["ret"] * 1001}, "candidates"),
            ({"source": "ret", "candidates": [], "jaccard_floor": 1.5}, "jaccard_floor"),
            ({"source": "ret", "candidates": [], "jaccard_floor": "x"}, "jaccard_floor"),
            ({"source": "ret", "candidates": [], "jaccard_weight": 2}, "jaccard_weight"),
            ({"source": "ret", "candidates": [], "num_permutations": 64}, "num_permutations"),
        ):
            with self.subTest(body=str(body)[:60]):
                status, reply = _post_json_status(port, "/score", body)
                self.assertEqual(status, 400)
                self.assertIn(field, reply["error"])

    def test_band_keys_match_the_stored_fingerprint(self):
        from resembl.lsh import band_buckets

        port = self._start_server()
        snippet = self._stored(3)
        reply = _post_json(port, "/band-keys", {"texts": [snippet.code], "bands": 16, "rows": 8})
        self.assertEqual(reply["keys"], [band_buckets(snippet.minhash, 128, 16, 8)])

    def test_band_keys_answer_one_key_list_per_text_in_order(self):
        port = self._start_server()
        texts = [self._stored(1).code, "xor ecx, ecx\nleave\nret 8"]
        reply = _post_json(port, "/band-keys", {"texts": texts, "bands": 4, "rows": 2})
        self.assertEqual([len(k) for k in reply["keys"]], [4, 4])
        self.assertNotEqual(reply["keys"][0], reply["keys"][1])
        self.assertEqual(reply["keys"][0][0], reply["keys"][0][0].lower())
        self.assertEqual(len(reply["keys"][0][0]), 16)

    def test_band_keys_refuse_malformed_requests(self):
        port = self._start_server()
        for body, field in (
            ({"bands": 4, "rows": 2}, "texts"),
            ({"texts": ["ret", " "], "bands": 4, "rows": 2}, "texts"),
            ({"texts": [None], "bands": 4, "rows": 2}, "texts"),
            ({"texts": ["ret"] * 1001, "bands": 4, "rows": 2}, "texts"),
            ({"texts": ["ret"], "rows": 2}, "bands"),
            ({"texts": ["ret"], "bands": 0, "rows": 2}, "bands"),
            ({"texts": ["ret"], "bands": 4, "rows": 1.5}, "rows"),
            ({"texts": ["ret"], "bands": 64, "rows": 4}, "bands"),
        ):
            with self.subTest(body=str(body)[:60]):
                status, reply = _post_json_status(port, "/band-keys", body)
                self.assertEqual(status, 400)
                self.assertIn(field, reply["error"])

    def test_score_and_band_keys_are_post_only_and_counted(self):
        port = self._start_server()
        for path in ("/score", "/band-keys"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10)
                self.assertEqual(caught.exception.code, 405)
        _post_json(port, "/score", {"source": "ret", "candidates": ["ret"]})
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as response:
            metrics = response.read().decode()
        self.assertIn('resembl_server_requests_total{path="/score",status="200"}', metrics)
        self.assertIn('path="/band-keys"', metrics)

    def _scheme(self, port: int) -> dict:
        """``GET /scheme``'s scheme object."""
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/scheme", timeout=10) as response:
            return json.loads(response.read())["scheme"]

    def test_score_and_band_keys_report_their_scheme(self):
        from resembl.models import FINGERPRINT_VERSION
        from resembl.scoring import MINHASH_SEED

        port = self._start_server()
        served = self._scheme(port)
        self.assertEqual(
            {k: v for k, v in served.items() if k != "id"},
            {
                "version": FINGERPRINT_VERSION,
                "hash": "sha1-le32",
                "seed": MINHASH_SEED,
                "num_permutations": 128,
                "ngram_size": 3,
                "normalize": True,
            },
        )
        self.assertRegex(served["id"], r"^[0-9a-f]{32}$")
        scored = _post_json(port, "/score", {"source": "ret", "candidates": ["ret"]})
        self.assertEqual(scored["scheme"], served)
        raw = _post_json(port, "/score", {"source": "ret", "candidates": [], "normalize": False})
        self.assertFalse(raw["scheme"]["normalize"])
        self.assertNotEqual(raw["scheme"]["id"], served["id"])
        keyed = _post_json(port, "/band-keys", {"texts": ["ret"], "bands": 4, "rows": 2})
        self.assertEqual(keyed["scheme"]["bands"], 4)
        self.assertEqual(keyed["scheme"]["rows"], 2)
        self.assertEqual(
            {k: v for k, v in keyed["scheme"].items() if k not in ("id", "bands", "rows")},
            {k: v for k, v in served.items() if k != "id"},
        )
        other_layout = _post_json(port, "/band-keys", {"texts": ["ret"], "bands": 2, "rows": 4})
        ids = {served["id"], keyed["scheme"]["id"], other_layout["scheme"]["id"]}
        self.assertEqual(len(ids), 3)
        again = _post_json(port, "/band-keys", {"texts": ["mov eax, 1"], "bands": 4, "rows": 2})
        self.assertEqual(again["scheme"], keyed["scheme"])

    def test_scheme_id_follows_the_computation_not_only_the_parameters(self):
        from resembl import server as server_mod

        port = self._start_server()
        before = self._scheme(port)["id"]
        # Same parameters, different shingle weighting: the probe listing
        # fingerprints differently, so the id must move.
        with patch("resembl.scoring._shingle_weight_tokens", return_value=1):
            changed = self._scheme(port)["id"]
        with patch.object(server_mod, "FINGERPRINT_VERSION", 999):
            bumped = self._scheme(port)
        self.assertEqual(self._scheme(port)["id"], before)
        self.assertNotEqual(changed, before)
        self.assertEqual(bumped["version"], 999)
        self.assertNotEqual(bumped["id"], before)

    def test_scheme_id_follows_the_seed(self):
        from resembl import scoring
        from resembl import server as server_mod

        port = self._start_server()
        before = self._scheme(port)
        self.addCleanup(scoring._MINHASH_TEMPLATES.clear)
        scoring._MINHASH_TEMPLATES.clear()
        with patch.object(scoring, "MINHASH_SEED", 7), patch.object(server_mod, "MINHASH_SEED", 7):
            seeded = self._scheme(port)
        scoring._MINHASH_TEMPLATES.clear()
        self.assertEqual(seeded["seed"], 7)
        self.assertNotEqual(seeded["id"], before["id"])


class TestStatelessServe(unittest.TestCase):
    """``serve(None)``: no database, only the endpoints that need none."""

    def setUp(self):
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self._cache_dir = os.path.join(workdir.name, "cache")
        # A database path in a directory that does not exist: opening it
        # fails, so any database access in stateless mode would show up.
        self._db = os.path.join(workdir.name, "missing", "assembly.db")
        env = patch.dict(
            os.environ,
            {"RESEMBL_CACHE_DIR": self._cache_dir, "DATABASE_URL": f"sqlite:///{self._db}"},
        )
        env.start()
        self.addCleanup(env.stop)

    def _start_server(self, config: ResemblConfig | None = None) -> int:
        """Serve without a database on a background thread; return the port."""
        from resembl.server import serve

        with patch("resembl.config.load_config", return_value=config or ResemblConfig()):
            httpd = serve(None, port=0)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 10)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def _get_status(self, port: int, path: str) -> tuple[int, dict]:
        """GET *path* and return ``(status, decoded JSON body)``."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_scores_and_band_keys_without_a_database(self):
        port = self._start_server()
        reply = _post_json(port, "/score", {"source": "ret", "candidates": ["ret"]})
        self.assertEqual(reply["scores"], [100.0])
        keys = _post_json(port, "/band-keys", {"texts": ["push ebx\nret"], "bands": 4, "rows": 2})
        self.assertEqual(len(keys["keys"][0]), 4)
        self.assertEqual(keys["scheme"]["bands"], 4)
        self.assertFalse(os.path.exists(os.path.dirname(self._db)))
        # Nothing advertises a stateless server to `resembl find`.
        self.assertFalse(os.path.exists(self._cache_dir))

    def test_health_reports_process_readiness(self):
        port = self._start_server()
        status, payload = self._get_status(port, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["mode"], "stateless")
        self.assertNotIn("database", payload)
        self.assertGreaterEqual(payload["uptime_seconds"], 0.0)
        status, _ = self._get_status(port, "/scheme")
        self.assertEqual(status, 200)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as response:
            self.assertIn('path="/health",status="200"', response.read().decode())

    def test_database_endpoints_answer_501(self):
        port = self._start_server()
        for path, body in (
            ("/find", {"query": "ret"}),
            ("/find-batch", {"queries": ["ret"]}),
            ("/find-pinned", {"query": "ret"}),
        ):
            with self.subTest(path=path):
                status, reply = _post_json_status(port, path, body)
                self.assertEqual(status, 501)
                self.assertIn("--stateless", reply["error"])
        status, reply = self._get_status(port, "/runtime-generation")
        self.assertEqual(status, 501)
        self.assertIn("--stateless", reply["error"])

    def test_serves_the_configured_fingerprint_settings(self):
        port = self._start_server(ResemblConfig(num_permutations=64, ngram_size=4))
        _, payload = self._get_status(port, "/scheme")
        self.assertEqual(payload["scheme"]["num_permutations"], 64)
        self.assertEqual(payload["scheme"]["ngram_size"], 4)
        status, reply = _post_json_status(
            port, "/band-keys", {"texts": ["ret"], "bands": 64, "rows": 2}
        )
        self.assertEqual(status, 400)
        self.assertIn("num_permutations (64)", reply["error"])

    def test_cli_serve_stateless_opens_no_database(self):
        env = {
            **os.environ,
            "PYTHONPATH": os.path.abspath("."),
            "DATABASE_URL": f"sqlite:///{self._db}",
            "RESEMBL_CACHE_DIR": self._cache_dir,
        }
        server = subprocess.Popen(
            [sys.executable, "-m", "resembl.cli", "serve", "--stateless", "--port", "0"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            **serve_stop_flags(),
        )
        self.addCleanup(server.wait, 10)
        self.addCleanup(process_stop, server)
        assert server.stdout is not None
        line = ""
        for line in server.stdout:
            if "listening on" in line:
                break
        if "listening on" not in line:
            server.wait(timeout=10)
            self.fail(f"serve --stateless did not start: {server.stderr.read()}")
        port = int(line.strip().rsplit(":", 1)[1])
        reply = _post_json(port, "/score", {"source": "ret", "candidates": ["ret"]})
        self.assertEqual(reply["scores"], [100.0])
        status, payload = self._get_status(port, "/health")
        self.assertEqual((status, payload["mode"]), (200, "stateless"))
        self.assertFalse(os.path.exists(os.path.dirname(self._db)))


class TestServerObservability(_ServeTestCase):
    """Health, metrics and the per-request trail a served query leaves."""

    def _get(self, port: int, path: str) -> tuple[int, str, str]:
        """GET *path* and return ``(status, content_type, body)``."""
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return response.status, response.headers["Content-Type"], response.read().decode()

    def test_non_loopback_bind_refuses_computed_and_metrics(self):
        """A non-loopback bind refuses /score, /band-keys, /scheme and /metrics.

        The server has no authentication; a routable bind is reachable by
        any host that can route to it.  Computed endpoints that read no
        database and the metrics exposition are refused there — /health
        stays open for probes.
        """
        from resembl.server import serve

        httpd = serve(None, host="0.0.0.0", port=0)
        # serve() bound a real socket on 0.0.0.0; serve_forever in a thread
        # and hit it via loopback — the gate is the bind flag, not the peer.
        port = httpd.server_address[1]
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(lambda: thread.join(10))
        self.addCleanup(httpd.shutdown)
        # _stateless_serve sets is_non_loopback for 0.0.0.0; assert it.
        self.assertTrue(getattr(httpd, "is_non_loopback", False))
        for verb, path in (
            ("POST", "/score"),
            ("POST", "/band-keys"),
            ("GET", "/scheme"),
            ("GET", "/metrics"),
        ):
            with self.subTest(verb=verb, path=path):
                if verb == "POST":
                    status, payload = _post_json_status(
                        port, path, {"source": "ret", "candidates": ["ret"]}
                    )
                else:
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}{path}", timeout=10
                        ) as response:
                            status, payload = response.status, {}
                    except urllib.error.HTTPError as exc:
                        status, payload = exc.code, json.loads(exc.read())
                self.assertEqual(status, 403)
                self.assertIn("not exposed on non-loopback", payload.get("error", ""))
        # /health stays open for probes on the same bind.
        status, _, _ = self._get(port, "/health")
        self.assertEqual(status, 200)

    def test_health_reports_ready_against_a_live_database(self):
        port = self._start_server()
        status, content_type, body = self._get(port, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["database"], "ok")
        self.assertEqual(payload["mode"], "database")
        self.assertGreaterEqual(payload["uptime_seconds"], 0.0)

    def test_health_reports_degraded_when_the_database_answers_no_query(self):
        port = self._start_server()
        # Standing in for a database that stopped answering: every probe
        # session fails, the way a dropped connection or a locked file does.
        with patch("resembl.server.Session", side_effect=RuntimeError("pool is closed")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._get(port, "/health")
        self.assertEqual(caught.exception.code, 503)
        payload = json.loads(caught.exception.read())
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["database"], "unavailable")
        # The driver's own message (SQL, file paths) stays in the log; only the
        # exception type reaches the client.
        self.assertEqual(payload["error"], "RuntimeError")
        self.assertNotIn("pool is closed", json.dumps(payload))

    def test_metrics_expose_requests_latency_and_cache_outcome(self):
        port = self._start_server()
        _post_json(port, "/find", {"query": "push ebx\nmov eax, 5\npop ebx\nret"})
        status, content_type, body = self._get(port, "/metrics")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/plain; version=0.0.4"))
        self.assertIn("resembl_server_up 1", body)
        self.assertIn('resembl_server_requests_total{path="/find",status="200"}', body)
        self.assertIn(
            'resembl_server_request_duration_seconds_bucket{path="/find",le="+Inf"}', body
        )
        self.assertIn('resembl_server_request_duration_seconds_count{path="/find"}', body)
        self.assertIn('resembl_server_result_cache_total{result="miss"}', body)
        self.assertIn("resembl_server_handler_rejected_total", body)

    def test_handler_slot_rejection_is_logged_and_counted(self):
        from unittest.mock import MagicMock

        from resembl import server as server_mod

        port = self._start_server()
        before = self._series_total(
            self._get(port, "/metrics")[2],
            "resembl_server_handler_rejected_total",
        )
        httpd = server_mod._FindServer.__new__(server_mod._FindServer)
        import threading as _threading

        httpd._handler_slots = _threading.BoundedSemaphore(1)
        self.assertTrue(httpd._handler_slots.acquire(blocking=False))
        request = MagicMock()
        with (
            patch.object(server_mod, "_HANDLER_SLOT_TIMEOUT_SECONDS", 0.01),
            self.assertLogs("resembl.server", level="WARNING") as captured,
        ):
            httpd.process_request(request, ("127.0.0.1", 12345))
        request.close.assert_called_once()
        self.assertTrue(any("handler slots exhausted" in line for line in captured.output))
        after = self._series_total(
            self._get(port, "/metrics")[2],
            "resembl_server_handler_rejected_total",
        )
        self.assertEqual(after, before + 1)
        httpd._handler_slots.release()

    def test_metrics_fold_an_unbounded_path_into_one_series(self):
        port = self._start_server()
        series = 'resembl_server_requests_total{path="other",status="404"}'
        before = self._series_total(self._get(port, "/metrics")[2], series)
        for i in range(5):
            _post_json_status(port, f"/nope{i}", {})
        _, _, body = self._get(port, "/metrics")
        # A crafted path must not buy a series per request: every one of the
        # five lands in the same label.  The counters are per process, so the
        # assertion is on the delta, not on an absolute count.
        self.assertEqual(self._series_total(body, series), before + 5)
        self.assertNotIn("/nope3", body)

    @staticmethod
    def _series_total(exposition: str, series: str) -> int:
        """Return the counter value of *series* in a Prometheus exposition."""
        for line in exposition.splitlines():
            if line.startswith(f"{series} "):
                return int(line.rsplit(" ", 1)[1])
        return 0

    def test_a_bad_request_is_counted_by_status(self):
        port = self._start_server()
        status, _ = _post_json_status(port, "/find", {"query": "   "})
        self.assertEqual(status, 400)
        _, _, body = self._get(port, "/metrics")
        self.assertIn('resembl_server_requests_total{path="/find",status="400"}', body)

    def test_slow_request_is_reported_without_verbose(self):
        from resembl import server as server_mod

        port = self._start_server()
        with self.assertLogs("resembl.server", level="WARNING") as captured:
            with patch.object(server_mod, "_SLOW_REQUEST_SECONDS", 0.0):
                _post_json(port, "/find", {"query": "push ebx\nmov eax, 5\npop ebx\nret"})
        self.assertTrue(
            any("slow request" in line for line in captured.output),
            captured.output,
        )

    def test_error_records_carry_the_request_id(self):
        port = self._start_server()
        with self.assertLogs("resembl.server", level="DEBUG") as captured:
            with patch("resembl.server._find_uncached", side_effect=RuntimeError("boom")):
                status, _ = _post_json_status(port, "/find", {"query": "push ebx"})
        self.assertEqual(status, 500)
        ids = {
            line.split("[", 1)[1].split("]", 1)[0]
            for line in captured.output
            if "resembl.server" in line and "[" in line
        }
        # The 500 traceback and the request's own record quote the same id, so
        # a client report naming one id reaches both.
        self.assertEqual(len(ids), 1, captured.output)

    def test_response_carries_request_id_header_matching_logs(self):
        port = self._start_server()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find",
            data=json.dumps({"query": "push ebx"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertLogs("resembl.server", level="DEBUG") as captured:
            with urllib.request.urlopen(request, timeout=10) as response:
                header_id = response.headers.get("X-Request-Id")
        self.assertTrue(header_id and header_id != "-")
        self.assertTrue(any(f"[{header_id}]" in line for line in captured.output))

    def test_supplied_request_id_is_adopted_not_reminted(self):
        port = self._start_server()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find",
            data=json.dumps({"query": "push ebx"}).encode(),
            headers={"Content-Type": "application/json", "X-Request-Id": "upstream-42"},
        )
        with self.assertLogs("resembl.server", level="DEBUG") as captured:
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.headers.get("X-Request-Id"), "upstream-42")
        # The engine records quote the upstream id, so the trail the
        # upstream service logged reaches this server's records.
        self.assertTrue(any("[upstream-42]" in line for line in captured.output), captured.output)

    def test_oversized_or_control_char_request_id_is_reminted(self):
        port = self._start_server()
        for bad in ("x" * 65, "bad\x7fid", "bad\x85id"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/find",
                data=json.dumps({"query": "push ebx"}).encode(),
                headers={"Content-Type": "application/json", "X-Request-Id": bad},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                got = response.headers.get("X-Request-Id")
            # An unbounded or log-forging id is dropped for a server
            # id rather than echoed or written into the log.
            self.assertTrue(got and got != bad and "\n" not in got, got)

    def test_metrics_include_find_pinned_and_runtime_generation_labels(self):
        port = self._start_server()
        self._get(port, "/runtime-generation")
        _, _, body = self._get(port, "/metrics")
        self.assertIn(
            'resembl_server_requests_total{path="/runtime-generation",status="200"}', body
        )
        from resembl.server import _metric_path_label

        self.assertEqual(_metric_path_label("/find-pinned"), "/find-pinned")
        self.assertEqual(_metric_path_label("/runtime-generation"), "/runtime-generation")


class TestResultCacheCoherence(unittest.TestCase):
    """The version guard holds across the connections of a pool."""

    def setUp(self):
        self._db = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(self._db) and os.remove(self._db))
        self._engine = create_engine(f"sqlite:///{self._db}", pool_size=4)
        # Runs before the file removal above (last-in-first-out cleanups):
        # Windows refuses to unlink a database file whose handle is still open.
        self.addCleanup(self._engine.dispose)
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

    def test_db_cache_id_distinguishes_passwords(self):
        """Two databases differing only by password never share cache entries.

        A masked URL (``user:***@host/db``) collapses ``secret1`` and
        ``secret2`` onto one key, so with matching version counters one
        tenant's payload could be served to the other.
        """
        from unittest.mock import MagicMock

        from resembl.server import _db_cache_id

        engine_a = MagicMock()
        engine_b = MagicMock()
        engine_a.url.render_as_string.side_effect = lambda hide_password=True: (
            "postgresql+pg8000://user:***@dbhost/db"
            if hide_password
            else "postgresql+pg8000://user:secret1@dbhost/db"
        )
        engine_b.url.render_as_string.side_effect = lambda hide_password=True: (
            "postgresql+pg8000://user:***@dbhost/db"
            if hide_password
            else "postgresql+pg8000://user:secret2@dbhost/db"
        )
        self.assertNotEqual(_db_cache_id(engine_a), _db_cache_id(engine_b))

    def test_batch_session_mid_batch_write_is_not_served_as_current(self):
        """A writer committing between two finds of one batch is visible to the second.

        ``/find-batch`` runs up to 1000 queries on one session, whose read
        snapshot outlives the batch.  Filing the second payload under the
        advanced counter while it was computed from the pre-write snapshot
        would serve stale results as current.
        """
        from resembl.server import _RESULT_CACHE, _find_one

        _RESULT_CACHE.clear()
        self.addCleanup(_RESULT_CACHE.clear)
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        body = {"top_n": 5}
        params = ResemblConfig()

        batch_session = Session(self._engine)
        self.addCleanup(batch_session.close)

        first = _find_one(batch_session, body, query, params)
        self.assertGreater(first["lsh_candidates"], 0)

        # Writer commits while the batch session's snapshot is still open.
        # ``snippet_add`` keeps the LSH index in sync, so the new snippet
        # (close to the query's shape) becomes a candidate.
        from resembl.core import snippet_add

        writer = Session(self._engine)
        self.addCleanup(writer.close)
        snippet_add(writer, "mid_batch", "push ebx\nmov eax, 250\npop ebx\nret")

        second = _find_one(batch_session, body, query, params)
        # The second find must reflect the write, not file the old snapshot
        # under the new version (which the next request would serve).
        self.assertGreater(second["lsh_candidates"], first["lsh_candidates"])

        # The entry filed is the current one: a fresh request hits it and
        # agrees with what the batch just computed.
        fresh = Session(self._engine)
        self.addCleanup(fresh.close)
        third = _find_one(fresh, body, query, params)
        self.assertEqual(third["lsh_candidates"], second["lsh_candidates"])


class TestCLIServerEndToEnd(unittest.TestCase):
    """The real CLI `serve` + `find` wiring, via subprocesses."""

    def setUp(self):
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
            encoding="utf-8",
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
            encoding="utf-8",
            **serve_stop_flags(),
        )
        self.addCleanup(process_stop, server)
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
                encoding="utf-8",
                env=self._env,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("matches", payload)
            self.assertGreater(payload["lsh_candidates"], 0)
        finally:
            # The platform's own stop signal, so the child runs its shutdown
            # path and releases the database file this test's tearDown removes.
            process_stop(server)
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
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        # Lazy exports still resolve and are the real symbols, not Nones.
        from resembl import Snippet, code_tokenize, snippet_add

        self.assertTrue(callable(code_tokenize))
        self.assertTrue(callable(snippet_add))
        self.assertIs(Snippet, __import__("resembl.models", fromlist=["Snippet"]).Snippet)
        # The resolved export is usable: tokenizing and adding work.
        self.assertEqual(code_tokenize("MOV EAX, 1\nRET"), ["MOV", "REG", "IMM", "RET"])

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
            encoding="utf-8",
            timeout=60,
            # The next line asserts returncode == 0 with a useful message;
            # a CalledProcessError would hide result.stderr from the output.
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class TestServerConcurrency(_ServeTestCase):
    """The warm server is shared by one handler thread per request.

    The tests drive the real HTTP endpoint against a real database: a mocked
    session cannot show what a second thread sees while the first one is
    mid-query.
    """

    snippet_count = 100

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
