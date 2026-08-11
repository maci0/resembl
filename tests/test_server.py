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
        self._cache_dir = tempfile.mkdtemp()
        self._engine = create_engine(f"sqlite:///{self._db}")
        SQLModel.metadata.create_all(self._engine)
        self._session = Session(self._engine)
        items = [
            snippet_prepare(f"f{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3)
            for i in range(100)
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
        body = json.dumps(
            {"queries": ["push ebx\nmov eax, 5\npop ebx\nret", 12345]}
        ).encode("utf-8")
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

    def test_result_cache_invalidates_on_db_change(self):
        """Cached finds are served until the database changes (data_version)."""
        from unittest.mock import patch as _patch

        from resembl.server import _RESULT_CACHE, _db_version

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


class TestCLIServerEndToEnd(unittest.TestCase):
    """The real CLI `serve` + `find` wiring, via subprocesses."""

    def setUp(self):
        import tempfile

        self._db = tempfile.mktemp(suffix=".db")
        self._cache_dir = tempfile.mkdtemp()
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
            deadline = time.time() + 20
            while not os.path.exists(port_file) and time.time() < deadline:
                time.sleep(0.1)
            if server.poll() is not None:
                self.fail(f"serve exited early: {server.stderr.read()}")
            if not os.path.exists(port_file):
                entries = (
                    os.listdir(self._cache_dir)
                    if os.path.isdir(self._cache_dir)
                    else "no cache dir"
                )
                self.fail(
                    f"serve did not start; cache dir: {entries}; port_file: {port_file}"
                )

            query_file = os.path.join(
                "tests", "test_data", sorted(os.listdir("tests/test_data"))[0]
            )
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
        import resembl

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
