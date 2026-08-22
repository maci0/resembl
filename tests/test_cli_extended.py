"""CLI integration tests for collection, version, merge, and search commands."""

import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sqlmodel import Session

from resembl.core import collection_create, snippet_add, snippet_tag_add
from tests.test_cli import BaseCLITest


class TestCLIFindBatch(BaseCLITest):
    """find-batch processes many queries in one invocation."""

    def test_find_batch_matches_individual_finds(self):
        import tempfile

        from resembl.core import snippet_add

        with Session(self.engine) as session:
            snippet_add(session, "f1", "MOV EAX, 1\nRET")
            snippet_add(session, "f2", "MOV EAX, 2\nRET")
            snippet_add(session, "f3", "XOR EBX, EBX\nRET")

        queries_file = tempfile.mktemp(suffix=".txt")
        with open(queries_file, "w", encoding="utf-8") as f:
            f.write(
                "MOV EAX, 1; RET\nMOV EAX, 2; RET\n# a comment\nXOR EBX, EBX; RET\n"
            )

        try:
            result = self.run_command(f"--format json find-batch --file {queries_file}")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload), 3)  # comments are skipped
            by_query = {d["query"]: d for d in payload}
            self.assertGreater(by_query["MOV EAX, 1\n RET"]["lsh_candidates"], 0)
            # ';' on a single line is converted to a newline, like `find --query`.
            self.assertGreater(by_query["MOV EAX, 2\n RET"]["lsh_candidates"], 0)
            self.assertGreater(by_query["XOR EBX, EBX\n RET"]["lsh_candidates"], 0)

            # Per-query results match individual `find` calls.
            single = self.run_command("--format json find --query 'MOV EAX, 1; RET'")
            self.assertEqual(single.returncode, 0, single.stderr)
            single_payload = json.loads(single.stdout)
            self.assertEqual(
                by_query["MOV EAX, 1\n RET"]["lsh_candidates"],
                single_payload["lsh_candidates"],
            )
        finally:
            if os.path.exists(queries_file):
                os.remove(queries_file)


class TestCLICollections(BaseCLITest):
    """Integration tests for the collection command group."""

    def test_collection_create(self):
        """Creating a collection should succeed."""
        result = self.run_command(
            "collection create test_col --description 'Test collection'"
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("test_col", result.stdout)

    def test_collection_list(self):
        """Listing collections should show created ones."""
        with Session(self.engine) as session:
            collection_create(session, "my_col", description="A test")
        result = self.run_command("collection list")
        self.assertEqual(result.returncode, 0)
        self.assertIn("my_col", result.stdout)

    def test_collection_show(self):
        """Showing a collection should list its snippets."""
        with Session(self.engine) as session:
            collection_create(session, "group")
            from resembl.core import collection_add_snippet
            from resembl.models import Snippet

            s = Snippet.get_by_name(session, "test_snippet")
            collection_add_snippet(session, "group", s.checksum)
        result = self.run_command("collection show group")
        self.assertEqual(result.returncode, 0)
        self.assertIn("test_snippet", result.stdout)

    def test_collection_delete(self):
        """Deleting a collection should succeed."""
        with Session(self.engine) as session:
            collection_create(session, "to_delete")
        result = self.run_command("collection delete to_delete")
        self.assertEqual(result.returncode, 0)

    def test_collection_add_snippet(self):
        """Adding a snippet to a collection via CLI."""
        with Session(self.engine) as session:
            collection_create(session, "target_col")
            from resembl.models import Snippet

            s = Snippet.get_by_name(session, "test_snippet")
            checksum = s.checksum
        result = self.run_command(f"collection add target_col {checksum}")
        self.assertEqual(result.returncode, 0)

    def test_collection_remove_snippet(self):
        """Removing a snippet from its collection via CLI."""
        with Session(self.engine) as session:
            collection_create(session, "my_col")
            from resembl.core import collection_add_snippet
            from resembl.models import Snippet

            s = Snippet.get_by_name(session, "test_snippet")
            collection_add_snippet(session, "my_col", s.checksum)
            checksum = s.checksum
        result = self.run_command(f"collection remove {checksum}")
        self.assertEqual(result.returncode, 0)

    def test_collection_list_quiet(self):
        """--quiet should suppress collection list output."""
        with Session(self.engine) as session:
            collection_create(session, "quiet_col")
        result = self.run_command("--quiet collection list")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")


class TestCLIMerge(BaseCLITest):
    """Integration tests for the merge command."""

    def _create_source_db(self):
        """Create a source DB with a unique snippet."""
        from sqlmodel import SQLModel, create_engine

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        src_engine = create_engine(f"sqlite:///{tmp.name}")
        SQLModel.metadata.create_all(src_engine)
        with Session(src_engine) as session:
            snippet_add(session, "source_func", "PUSH EBP; MOV EBP, ESP; POP EBP")
        src_engine.dispose()
        return tmp.name

    def test_merge_command(self):
        """Merging a source DB should report results."""
        source_path = self._create_source_db()
        try:
            result = self.run_command(f"merge {source_path}")
            self.assertEqual(result.returncode, 0)
            self.assertIn("Merge Complete", result.stdout)
        finally:
            os.unlink(source_path)

    def test_merge_json_format(self):
        """Merging with --format json should produce valid JSON."""
        source_path = self._create_source_db()
        try:
            result = self.run_command(f"--format json merge {source_path}")
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertIn("added", data)
        finally:
            os.unlink(source_path)

    def test_merge_nonexistent_file(self):
        """Merging a nonexistent file should fail."""
        result = self.run_command("merge /tmp/nonexistent_db.db")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error", result.stderr)


class TestCLIVersion(BaseCLITest):
    """Integration tests for the version command."""

    def test_version_command(self):
        """version should return results (possibly empty)."""
        with Session(self.engine) as session:
            from resembl.models import Snippet

            s = Snippet.get_by_name(session, "test_snippet")
            checksum = s.checksum
        result = self.run_command(f"version {checksum}")
        self.assertEqual(result.returncode, 0)


class TestCLISearch(BaseCLITest):
    """Integration tests for the search command."""

    def test_search_by_name(self):
        """search command should find snippets by name pattern."""
        with Session(self.engine) as session:
            snippet_add(session, "memcpy_impl", "REP MOVSB")
            snippet_add(session, "strcmp_impl", "CMPSB")
        result = self.run_command("search mem")
        self.assertEqual(result.returncode, 0)
        self.assertIn("memcpy_impl", result.stdout)
        self.assertNotIn("strcmp_impl", result.stdout)

    def test_search_limit(self):
        """--limit bounds the results (and reports N+ when truncated)."""
        with Session(self.engine) as session:
            for i in range(5):
                snippet_add(session, f"mem_{i}", f"REP MOVSB {i}")
        result = self.run_command("search mem --limit 2")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Found 2+ snippets", result.stdout)
        result = self.run_command("search mem --limit 10")
        self.assertIn("Found 5 snippets", result.stdout)


class TestCLIFormatFlag(BaseCLITest):
    """Integration tests for --format json/csv."""

    def test_stats_json(self):
        """stats --format json should produce valid JSON."""
        result = self.run_command("--format json stats")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertIn("num_snippets", data)

    def test_list_json(self):
        """list --format json should produce valid JSON."""
        result = self.run_command("--format json list")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertIsInstance(data, list)

    def test_list_csv(self):
        """list --format csv should produce CSV output."""
        result = self.run_command("--format csv list")
        self.assertEqual(result.returncode, 0)
        # CSV output should have header row
        lines = result.stdout.strip().split("\n")
        self.assertGreaterEqual(len(lines), 1)

    def test_find_json(self):
        """find --format json should produce valid JSON with matches key."""
        result = self.run_command("--format json find --query 'MOV EAX, 1'")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertIn("matches", data)
        self.assertIsInstance(data["matches"], list)

    def test_find_query_semicolon_separator(self):
        """Inline --query uses ';' as a statement separator (documented format).

        Without the fix, the pygments lexer treats ';' as a comment and the
        query would silently truncate to just the first instruction.
        """
        with Session(self.engine) as session:
            snippet_add(session, "multi", "PUSH EBP\nMOV EBP, ESP\nPOP EBP\nRET")
        result = self.run_command(
            "--format json find --query 'PUSH EBP; MOV EBP, ESP; POP EBP; RET'"
        )
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertGreaterEqual(len(data["matches"]), 1)
        self.assertEqual(data["matches"][0]["names"], ["multi"])

    def test_find_reports_lazy_index_build(self):
        """Table-mode find announces the one-time LSH index build."""
        result = self.run_command("find --query 'MOV EAX, 1'")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Building LSH index", result.stdout)
        # Second find uses the built index — no announcement.
        result = self.run_command("find --query 'MOV EAX, 1'")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Building LSH index", result.stdout)


class TestCLITagEdgeCases(BaseCLITest):
    """Edge-case tests for tag commands."""

    def test_tag_add_idempotent(self):
        """Adding the same tag twice should succeed both times (idempotent)."""
        with Session(self.engine) as session:
            from resembl.models import Snippet

            s = Snippet.get_by_name(session, "test_snippet")
            checksum = s.checksum
        # First add
        result1 = self.run_command(f"tag add {checksum} 'crypto'")
        self.assertEqual(result1.returncode, 0)
        # Second add (should be idempotent)
        result2 = self.run_command(f"tag add {checksum} 'crypto'")
        self.assertEqual(result2.returncode, 0)


class TestCLIShowCommand(BaseCLITest):
    """Tests for the show command."""

    def test_show_by_checksum(self):
        """show should display snippet details."""
        with Session(self.engine) as session:
            from resembl.models import Snippet

            s = Snippet.get_by_name(session, "test_snippet")
            checksum = s.checksum
        result = self.run_command(f"show {checksum}")
        self.assertEqual(result.returncode, 0)
        self.assertIn("test_snippet", result.stdout)

    def test_show_by_partial_checksum(self):
        """show should work with checksum prefix."""
        with Session(self.engine) as session:
            from resembl.models import Snippet

            s = Snippet.get_by_name(session, "test_snippet")
            prefix = s.checksum[:8]
        result = self.run_command(f"show {prefix}")
        self.assertEqual(result.returncode, 0)

    def test_show_nonexistent(self):
        """show with invalid checksum should fail."""
        result = self.run_command("show ffffffffffffffff")
        self.assertNotEqual(result.returncode, 0)

    def test_compare_corrupt_blob_heals_from_code(self):
        """compare on a corrupt fingerprint heals it from code, never a traceback.

        Stored blobs are never deserialized in non-packed formats; a corrupt
        one is recomputed from the snippet's own code (same self-healing
        semantics as ``find``), so the comparison still succeeds cleanly.
        """
        from sqlmodel import Session, text

        with Session(self.engine) as session:
            row = session.exec(text("SELECT checksum FROM snippet LIMIT 1")).one()
            checksum = row[0]
            session.execute(
                text("UPDATE snippet SET minhash = :m WHERE checksum = :c"),
                {"m": b"corrupt-blob", "c": checksum},
            )
            session.commit()

        result = self.run_command(f"compare {checksum} {checksum}")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Traceback", result.stdout)


class TestImportJobs(BaseCLITest):
    """The adaptive default worker count for imports."""

    def test_default_jobs_scales_with_directory(self):
        # The import command derives its default from
        # core.adaptive_worker_count (one worker per ~100 files, capped at
        # the CPU count).
        from resembl.core import adaptive_worker_count as d

        self.assertEqual(d(0, 32), 1)
        self.assertEqual(d(50, 32), 1)  # small dirs: no pool spawn at all
        self.assertEqual(d(300, 32), 4)
        self.assertEqual(d(1000, 32), 11)
        self.assertEqual(d(10000, 32), 32)  # large dirs: capped at CPU count
        self.assertEqual(d(10_000, 4), 4)


class TestCLIServeLifecycle(BaseCLITest):
    """End-to-end: a real ``resembl serve`` subprocess answers warm finds.

    Unlike the in-process server tests, these exercise the actual CLI
    entry points: ``resembl serve`` starts, writes its port file, ``find``
    and ``find-batch`` route through the thin client, and a SIGTERM (the
    signal service managers send) shuts the process down cleanly.
    """

    def _serve_env(self, cache_dir):
        return {
            **os.environ,
            "PYTHONPATH": os.path.join(os.getcwd(), "."),
            "DATABASE_URL": f"sqlite:///{self.db_name}",
            "RESEMBL_CACHE_DIR": cache_dir,
        }

    def _start_serve(self, cache_dir):
        return subprocess.Popen(
            ["python", "-m", "resembl.cli", "serve"],
            env=self._serve_env(cache_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _wait_for_port_file(self, port_file, timeout=30):
        """Block until the serve process writes its port file."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with open(port_file, encoding="utf-8") as f:
                    port = f.read().strip()
                if port.isdigit():
                    return int(port)
            except OSError:
                pass
            time.sleep(0.05)
        self.fail(f"serve did not write {port_file} within {timeout}s")

    def test_serve_subprocess_answers_find_and_shuts_down(self):
        import hashlib

        db_url = f"sqlite:///{self.db_name}"
        with tempfile.TemporaryDirectory() as cache_dir:
            # Replicate server_port_path() against the temp cache dir.
            digest = hashlib.sha1(db_url.encode("utf-8")).hexdigest()[:12]
            port_file = os.path.join(cache_dir, f"server_{digest}.port")
            proc = self._start_serve(cache_dir)
            try:
                port = self._wait_for_port_file(port_file)
                self.assertGreater(port, 0)

                # A find subprocess routes through the running server.
                result = self.run_command(
                    "--format json find --query 'MOV EAX, 1'",
                    extra_env={"RESEMBL_CACHE_DIR": cache_dir},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertGreater(payload["lsh_candidates"], 0)
                self.assertTrue(
                    any("test_snippet" in m["names"] for m in payload["matches"])
                )

                # A repeat find hits the server's version-guarded result cache.
                result2 = self.run_command(
                    "--format json find --query 'MOV EAX, 1'",
                    extra_env={"RESEMBL_CACHE_DIR": cache_dir},
                )
                self.assertEqual(result2.returncode, 0, result2.stderr)
                self.assertEqual(
                    json.loads(result2.stdout)["lsh_candidates"],
                    payload["lsh_candidates"],
                )

                # find-batch also routes through the server, one round trip.
                queries_file = tempfile.mktemp(suffix=".txt")
                with open(queries_file, "w", encoding="utf-8") as f:
                    f.write("MOV EAX, 1\nMOV EAX, 2\n")
                try:
                    batch = self.run_command(
                        f"--format json find-batch --file {queries_file}",
                        extra_env={"RESEMBL_CACHE_DIR": cache_dir},
                    )
                finally:
                    if os.path.exists(queries_file):
                        os.remove(queries_file)
                self.assertEqual(batch.returncode, 0, batch.stderr)
                batch_payload = json.loads(batch.stdout)
                self.assertEqual(len(batch_payload), 2)
            finally:
                # SIGTERM — the signal a service manager sends — must shut the
                # process down cleanly and remove the port file.
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                    self.fail("serve did not exit after SIGTERM")
            self.assertFalse(os.path.exists(port_file), "stale port file left behind")

    def test_second_serve_refuses_to_double_start(self):
        """Starting serve twice for the same DB fails cleanly instead of orphaning."""
        import hashlib

        db_url = f"sqlite:///{self.db_name}"
        with tempfile.TemporaryDirectory() as cache_dir:
            digest = hashlib.sha1(db_url.encode("utf-8")).hexdigest()[:12]
            port_file = os.path.join(cache_dir, f"server_{digest}.port")
            first = self._start_serve(cache_dir)
            try:
                self._wait_for_port_file(port_file)
                # Second serve must refuse (already running) with a clean error.
                second = subprocess.run(
                    ["python", "-m", "resembl.cli", "serve"],
                    env=self._serve_env(cache_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(second.returncode, 0)
                self.assertIn("already running", second.stderr)
            finally:
                first.terminate()
                try:
                    first.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    first.kill()
                    first.wait(timeout=5)
                    self.fail("first serve did not exit after SIGTERM")
            self.assertFalse(os.path.exists(port_file))

    def test_serve_port_in_use_fails_cleanly(self):
        """serve --port N on an occupied port errors cleanly, not with a traceback."""
        import socket

        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied = blocker.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as cache_dir:
                result = subprocess.run(
                    ["python", "-m", "resembl.cli", "serve", "--port", str(occupied)],
                    env=self._serve_env(cache_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not bind", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        finally:
            blocker.close()


class TestServerFallback(unittest.TestCase):
    """The thin-client fallback must not orphan a slow-but-live server."""

    def setUp(self):
        import hashlib

        from sqlmodel import Session, SQLModel, create_engine

        import resembl.cli as cli

        self._cache = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache.cleanup)
        self._db = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(self._db) and os.remove(self._db))
        engine = create_engine(f"sqlite:///{self._db}")
        SQLModel.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self._session = Session(engine)
        self.addCleanup(self._session.close)
        cli.state.session = self._session

        digest = hashlib.sha1(str(engine.url).encode("utf-8")).hexdigest()[:12]
        self.port_file = os.path.join(self._cache.name, f"server_{digest}.port")
        with open(self.port_file, "w", encoding="utf-8") as f:
            f.write("12345")
        self._env = patch.dict(os.environ, {"RESEMBL_CACHE_DIR": self._cache.name})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_timeout_keeps_port_file(self):
        """A slow-but-live server keeps its port file (no orphaning)."""
        import urllib.error
        from unittest.mock import patch

        import resembl.cli as cli

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(TimeoutError("server busy")),
        ):
            result = cli._find_via_server("MOV EAX, 1", 5, 0.5, True, 3)
        self.assertIsNone(result)
        self.assertTrue(
            os.path.exists(self.port_file), "port file must survive a timeout"
        )

    def test_connection_refused_removes_stale_port_file(self):
        """A dead server's stale port file is cleaned up."""
        import urllib.error
        from unittest.mock import patch

        import resembl.cli as cli

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError("no server")),
        ):
            result = cli._find_via_server("MOV EAX, 1", 5, 0.5, True, 3)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.port_file), "stale file should be removed")

    def test_find_batch_timeout_keeps_port_file(self):
        """The find-batch client has the same timeout-vs-refused behavior."""
        import urllib.error
        from unittest.mock import patch

        import resembl.cli as cli

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(TimeoutError("server busy")),
        ):
            result = cli._find_batch_via_server(
                ["MOV EAX, 1", "MOV EBX, 2"], 5, 0.5, True, 3
            )
        self.assertIsNone(result)
        self.assertTrue(
            os.path.exists(self.port_file), "port file must survive a timeout"
        )

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError("no server")),
        ):
            result = cli._find_batch_via_server(
                ["MOV EAX, 1", "MOV EBX, 2"], 5, 0.5, True, 3
            )
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.port_file), "stale file should be removed")


if __name__ == "__main__":
    unittest.main()
