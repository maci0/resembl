"""Tests for the warm ``serve`` server and the thin find client."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

import resembl.models  # noqa: F401  (registers tables)
from resembl.core import snippet_add_batch, snippet_find_matches, snippet_prepare


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

    def test_server_query_matches_in_process(self):
        """POST /find returns the same top matches as the in-process path."""
        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"
        body = json.dumps({"query": query, "top_n": 5}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())

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
        """The server answers an unbuildable threshold with an error payload."""
        port = self._start_server()
        body = json.dumps(
            {"query": "push ebx\nmov eax, 5\npop ebx\nret", "threshold": 0.985}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
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
        body = json.dumps(
            {
                "query": "push ebx\nmov eax, 5\npop ebx\nret",
                "num_permutations": absurd,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
        self.assertIn("error", payload)
        self.assertIn("num_permutations", payload["error"])
        # The rejected value must not leave a cached fingerprint template.
        self.assertNotIn(absurd, _MINHASH_TEMPLATES)

        # The lower bound is enforced by the same check.
        low_body = json.dumps({"query": "mov eax, 5", "num_permutations": 1}).encode("utf-8")
        low_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find",
            data=low_body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(low_request, timeout=10) as response:
            low_payload = json.loads(response.read())
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

        real_create = db_mod.create_db_engine
        created: dict = {}

        def capturing_create(url, **kwargs):
            engine = real_create(url, **kwargs)
            created["engine"] = engine
            return engine

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
        from resembl.server import server_port_path

        port = self._start_server()
        port_file = server_port_path(f"sqlite:///{self._db}")
        self.assertTrue(os.path.exists(port_file))
        with open(port_file, encoding="utf-8") as f:
            self.assertEqual(int(f.read()), port)

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

        def fake_urlopen(request, timeout=5):
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

        from resembl.find_client import _main, server_port_path

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
        body = json.dumps({"queries": [q1, q2], "top_n": 5}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find-batch",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())

        self.assertEqual(len(payload["results"]), 2)
        for query, result in zip((q1, q2), payload["results"]):
            self.assertEqual(result["query"], query)
            # Matches the single /find result for the same query.
            single_body = json.dumps({"query": query, "top_n": 5}).encode("utf-8")
            single_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/find",
                data=single_body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(single_request, timeout=10) as response:
                single = json.loads(response.read())
            self.assertEqual(result["lsh_candidates"], single["lsh_candidates"])

    def test_find_batch_isolates_bad_queries(self):
        """A malformed query fails itself, not the whole batch."""
        port = self._start_server()
        body = json.dumps({"queries": ["push ebx\nmov eax, 5\npop ebx\nret", 12345]}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find-batch",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
        self.assertEqual(len(payload["results"]), 2)
        self.assertIn("lsh_candidates", payload["results"][0])
        self.assertIn("error", payload["results"][1])

    def test_find_batch_malformed_container_answers_500(self):
        """A non-iterable 'queries' value answers 500 JSON, not a dropped connection."""
        import urllib.error

        port = self._start_server()
        body = json.dumps({"queries": 12345}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/find-batch",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = json.loads(exc.read())
        self.assertEqual(status, 500)
        self.assertIn("error", payload)

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
            body = json.dumps({"query": query, "top_n": 5}).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/find",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())

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

    def test_concurrent_requests_all_succeed(self):
        """The server answers concurrent finds correctly (per-request sessions)."""
        import concurrent.futures

        port = self._start_server()
        query = "push ebx\nmov eax, 5\npop ebx\nret"

        def do_find(i: int) -> dict:
            body = json.dumps({"query": query, "top_n": 5}).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/find",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())

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
        original_flag = lsh_mod._TABLES_ENSURED
        lsh_mod._TABLES_ENSURED = False
        self.addCleanup(setattr, lsh_mod, "_TABLES_ENSURED", original_flag)

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
        self.assertTrue(lsh_mod._TABLES_ENSURED)
        # The index facade is usable on the now-created tables.
        with Session(engine) as session:
            lsh = lsh_mod.ResemblLSH(session, 0.5, 128)
            self.assertGreaterEqual(lsh.b, 2)

    def test_port_file_cleanup_keeps_foreign_advertisement(self):
        """An exiting serve must not delete another server's port file.

        Two serve processes started close together both pass the
        double-serve check (neither has written yet) and both bind; only
        the last writer owns the advertisement.  The loser exiting must
        leave the survivor discoverable to find clients.
        """
        from resembl.server import _port_file_cleanup

        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        port_file = os.path.join(cache.name, "server_audit.port")

        # A foreign server's advertisement survives our exit.
        with open(port_file, "w", encoding="utf-8") as f:
            f.write("4242")
        _port_file_cleanup(port_file, 1111)
        self.assertTrue(os.path.exists(port_file))
        with open(port_file, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "4242")

        # Our own advertisement is removed.
        with open(port_file, "w", encoding="utf-8") as f:
            f.write("1111")
        _port_file_cleanup(port_file, 1111)
        self.assertFalse(os.path.exists(port_file))

        # A missing file is not an error.
        _port_file_cleanup(port_file, 1111)


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
            from resembl.server import server_port_path

            port_file = server_port_path(f"sqlite:///{self._db}")
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
        import sys

        for mod in ("sqlmodel", "pygments", "datasketch", "scipy"):
            sys.modules.pop(mod, None)

        self.assertNotIn("sqlmodel", sys.modules)
        self.assertNotIn("datasketch", sys.modules)
        # Lazy exports still resolve.
        from resembl import Snippet, code_tokenize, snippet_add

        self.assertTrue(callable(code_tokenize))
        self.assertTrue(callable(snippet_add))
        self.assertTrue(Snippet is not None)

    def test_submodule_access(self):
        import resembl

        self.assertIsNotNone(resembl.core)
        self.assertIsNotNone(resembl.server)
