"""CLI integration tests for collection, version, merge, and search commands."""

# pylint: disable=protected-access  # tests exercise private internals
# pylint: disable=consider-using-with  # a test keeps a temp file, a temp
# directory or a child process open for the whole test body on purpose

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlmodel import Session

from resembl.core import collection_create, snippet_add, string_checksum
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
            f.write("MOV EAX, 1; RET\nMOV EAX, 2; RET\n# a comment\nXOR EBX, EBX; RET\n")

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

    def test_find_batch_renders_per_query_server_errors(self):
        """A per-query server error entry renders instead of crashing the batch.

        The server isolates one failing query as ``{"query": ..., "error":
        ...}``; table mode used to crash with KeyError('lsh_candidates') on
        such entries and csv mode with a DictWriter field mismatch, turning
        one isolated failure into a lost whole-batch result.
        """
        import contextlib
        import io
        from unittest.mock import patch

        import resembl.cli as cli

        results = [
            {
                "query": "MOV EAX, 1\n RET",
                "lsh_candidates": 3,
                "matches": [{"checksum": "a" * 64, "names": ["f1"], "score": 91.0}],
            },
            {"query": "bad query", "error": "boom: internal failure"},
        ]
        for fmt in ("table", "csv", "json"):
            with self.subTest(format=fmt):
                stdout, stderr = io.StringIO(), io.StringIO()
                saved = (cli.state.format, cli.state.quiet)
                cli.state.format, cli.state.quiet = fmt, False
                try:
                    with patch.object(cli, "_find_batch_via_server", return_value=results):
                        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                            cli.find_batch(
                                file=io.StringIO("MOV EAX, 1; RET\n"), top_n=None, threshold=None
                            )
                finally:
                    cli.state.format, cli.state.quiet = saved
                self.assertIn("bad query", stdout.getvalue())
                self.assertIn("f1", stdout.getvalue())
                if fmt == "table":
                    # The isolated failure surfaces on stderr, visibly.
                    self.assertIn("boom: internal failure", stderr.getvalue())
                if fmt == "csv":
                    # The heterogeneous rows render under a union header.
                    self.assertTrue(stdout.getvalue().splitlines()[0].endswith(",error"))


class TestCSVFormulaGuard(unittest.TestCase):
    """CSV cells that spreadsheets would evaluate as formulas are neutralized."""

    def test_csv_safe_prefixes_formula_characters(self):
        """Every formula-triggering leading character gets a text-forcing quote."""
        from resembl.cli import _csv_safe

        for payload in ("=1+1", "+2", "-x", "@SUM(A1)", "\tTAB", "\r\nCRLF"):
            self.assertEqual(_csv_safe(payload), "'" + payload)
        # Safe values pass through unchanged (including non-strings).
        self.assertEqual(_csv_safe("plain name"), "plain name")
        self.assertEqual(_csv_safe(""), "")
        self.assertEqual(_csv_safe(91.0), 91.0)

    def test_find_csv_output_neutralizes_formula_names(self):
        """A merge-sourced name like '=HYPERLINK(...)' is not a live CSV formula.

        Snippet names are arbitrary text from merge sources; without the
        guard the CSV cell opens as a spreadsheet formula (data exfiltration
        via WEBSERVICE, command execution via DDE).
        """
        import contextlib
        import io

        import resembl.cli as cli

        results = [
            {
                "query": "MOV EAX, 1\n RET",
                "lsh_candidates": 1,
                "matches": [
                    {
                        "checksum": "a" * 64,
                        "names": ['=HYPERLINK("http://evil")'],
                        "score": 91.0,
                    }
                ],
            }
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        saved = (cli.state.format, cli.state.quiet)
        cli.state.format, cli.state.quiet = "csv", False
        try:
            with patch.object(cli, "_find_batch_via_server", return_value=results):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    cli.find_batch(
                        file=io.StringIO("MOV EAX, 1; RET\n"), top_n=None, threshold=None
                    )
        finally:
            cli.state.format, cli.state.quiet = saved
        out = stdout.getvalue()
        self.assertIn("'=HYPERLINK", out)
        # No unguarded formula cell remains anywhere in the output.
        self.assertNotIn("=HYPERLINK", out.replace("'=HYPERLINK", ""))


class TestCLICollections(BaseCLITest):
    """Integration tests for the collection command group."""

    def test_collection_create(self):
        """Creating a collection should succeed."""
        result = self.run_command("collection create test_col --description 'Test collection'")
        self.assertEqual(result.returncode, 0)
        self.assertIn("test_col", result.stdout)

    def test_collection_create_twice_exits_zero(self):
        """A repeated create converges instead of failing on the primary key."""
        first = self.run_command("collection create twice_col --description 'Original'")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_command("collection create twice_col --description 'Overwritten'")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already exists", second.stdout)
        with Session(self.engine) as session:
            from resembl.models import Collection

            rows = Collection.get_all(session)
            self.assertEqual([row.name for row in rows].count("twice_col"), 1)
            self.assertEqual(Collection.get_by_name(session, "twice_col").description, "Original")

    def test_name_add_twice_exits_zero(self):
        """A repeated `name add` is a no-op, like `tag add`."""
        with Session(self.engine) as session:
            snippet = snippet_add(session, "rerun_proc", "MOV ECX, 9")
            checksum = snippet.checksum
        first = self.run_command(f"name add {checksum} alias")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_command(f"name add {checksum} alias")
        self.assertEqual(second.returncode, 0, second.stderr)
        with Session(self.engine) as session:
            from resembl.core import snippet_get

            self.assertEqual(snippet_get(session, checksum).name_list.count("alias"), 1)

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

    def test_empty_collection_output_stays_machine_readable(self):
        """An empty result set renders as an empty document, not prose.

        "No collections found." on stdout is not JSON; a script piping
        `--format json` into a parser had to special-case it.
        """
        result = self.run_command("--format json collection list")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [])

        result = self.run_command("--format json collection show absent")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [])

        # CSV's "no rows" is no output: there are no columns to name.
        result = self.run_command("--format csv collection list")
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
        # Built from the platform temp dir so the path is nonexistent
        # everywhere (a literal /tmp/... is meaningless on Windows).
        missing = os.path.join(tempfile.gettempdir(), "resembl_nonexistent_db.db")
        result = self.run_command(f"merge {missing}")
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

    def test_version_without_history_is_empty_not_an_error(self):
        """No recorded version is a message in table mode, empty in the
        machine formats, and an exit code of 0 in all three."""
        with Session(self.engine) as session:
            from resembl.models import Snippet

            checksum = Snippet.get_by_name(session, "test_snippet").checksum

        table = self.run_command(f"version {checksum}")
        self.assertEqual(table.returncode, 0)
        self.assertIn("No version history", table.stdout)

        as_json = self.run_command(f"--format json version {checksum}")
        self.assertEqual(as_json.returncode, 0, as_json.stderr)
        self.assertEqual(json.loads(as_json.stdout), [])

        as_csv = self.run_command(f"--format csv version {checksum}")
        self.assertEqual(as_csv.returncode, 0, as_csv.stderr)
        self.assertEqual(as_csv.stdout.strip(), "")


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

    def test_search_no_match_names_the_next_step(self):
        """A search that matches nothing says what to try, not just how many."""
        result = self.run_command("search zzz_no_such_name")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Found 0 snippets", result.stdout)
        self.assertIn("shorter pattern", result.stdout)
        self.assertIn("resembl list", result.stdout)


class TestCLIEmptyResults(BaseCLITest):
    """Empty results say so, and point at the command that fills them."""

    def setUp(self):
        super().setUp()
        from sqlmodel import select

        from resembl.models import Snippet

        with Session(self.engine) as session:
            for snippet in session.exec(select(Snippet)).all():
                session.delete(snippet)
            session.commit()

    def test_list_on_empty_database_says_so(self):
        """`list` on an empty database used to print nothing at all."""
        result = self.run_command("list")
        self.assertEqual(result.returncode, 0)
        self.assertIn("No snippets found.", result.stdout)
        self.assertIn("resembl add", result.stdout)

    def test_list_empty_json_is_still_a_document(self):
        """The empty-state line is table output only; JSON stays a document."""
        result = self.run_command("--format json list")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [])

    def test_list_range_past_the_end_says_so(self):
        """An out-of-window --range names the range, not a bare empty table."""
        result = self.run_command("list --range 5-10")
        self.assertEqual(result.returncode, 0)
        self.assertIn("No snippets in range 5-10.", result.stdout)


class TestCLIFormatFlag(BaseCLITest):
    """Integration tests for --format json/csv."""

    def test_stats_json(self):
        """stats --format json reports the snippets the database holds."""
        result = self.run_command("--format json stats")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        # setUp seeds exactly one snippet, so the count is pinned rather than
        # merely "a key exists": a stats that always reported 0 or a constant
        # would otherwise pass.
        self.assertEqual(data["num_snippets"], 1)

    def test_list_json(self):
        """list --format json streams one document per snippet."""
        result = self.run_command("--format json list")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["names"], ["test_snippet"])
        self.assertEqual(data[0]["checksum"], string_checksum("MOV EAX, 1"))

    def test_list_csv(self):
        """list --format csv parses back into the rows it wrote."""
        import csv
        import io

        result = self.run_command("--format csv list")
        self.assertEqual(result.returncode, 0)
        rows = list(csv.DictReader(io.StringIO(result.stdout)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows[0]), ["checksum", "names"])
        self.assertEqual(rows[0]["names"], "test_snippet")
        self.assertEqual(rows[0]["checksum"], string_checksum("MOV EAX, 1"))

    def test_csv_records_terminate_with_lf_only(self):
        """CSV records end in a bare LF, never a CR, on any platform.

        ``csv`` defaults to ``"\r\n"`` and the writers target ``sys.stdout``,
        a text stream that rewrites every ``"\n"`` to ``os.linesep``.  On
        Windows that stacks into ``"\r\r\n"``, and every CSV reader then
        parses the stray ``\r`` as a field of its own.  Pinning the absence
        of ``\r`` keeps the fix from being reverted on a Linux-only run,
        where the bug is invisible.
        """
        import csv
        import io

        for command in ("--format csv list", "--format csv stats"):
            with self.subTest(command=command):
                result = self.run_command(command)
                self.assertEqual(result.returncode, 0)
                self.assertNotIn("\r", result.stdout)
                rows = list(csv.reader(io.StringIO(result.stdout)))
                self.assertGreaterEqual(len(rows), 1)
                # Every record carries exactly the header's fields: a stray
                # terminator would show up here as an extra empty field.
                self.assertTrue(all(len(row) == len(rows[0]) for row in rows))

    def test_collection_show_csv_terminates_with_lf_only(self):
        """``collection show --format csv`` ends records with a bare LF too.

        Its writer is built inline rather than through the shared CSV helper,
        so it is the one place the ``lineterminator`` can silently be left at
        ``csv``'s ``"\\r\\n"`` default.  A Linux-only run cannot see the
        difference on the wire, but the absent argument is visible here.
        """
        import csv
        import io

        with Session(self.engine) as session:
            collection_create(session, "csv_col")
            from resembl.core import collection_add_snippet
            from resembl.models import Snippet

            snippet = Snippet.get_by_name(session, "test_snippet")
            collection_add_snippet(session, "csv_col", snippet.checksum)

        result = self.run_command("--format csv collection show csv_col")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("\r", result.stdout)
        rows = list(csv.DictReader(io.StringIO(result.stdout)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["collection"], "csv_col")
        self.assertEqual(rows[0]["checksum"], snippet.checksum)

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

    def test_find_no_normalization_skips_token_folding(self):
        """--no-normalization stops registers and immediates being folded.

        The stored snippet is ``MOV EAX, 1``; a query renaming the register
        and changing the immediate still matches once both sides are folded
        to REG/IMM, and no longer matches when the folding is skipped.
        """
        result = self.run_command("--format json find --query 'MOV ECX, 9'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [m["names"] for m in json.loads(result.stdout)["matches"]], [["test_snippet"]]
        )

        result = self.run_command("--format json find --no-normalization --query 'MOV ECX, 9'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["matches"], [])

    def test_find_reports_lazy_index_build(self):
        """Table-mode find announces the one-time LSH index build."""
        result = self.run_command("find --query 'MOV EAX, 1'")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Building LSH index", result.stdout)
        # Second find uses the built index — no announcement.
        result = self.run_command("find --query 'MOV EAX, 1'")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Building LSH index", result.stdout)


class TestCLIServeWarnings(BaseCLITest):
    """The bind warning the serve story requires.

    ``serve`` blocks in ``serve_forever`` once it is listening, so each
    command is pointed at a port this process already holds: the bind
    fails, the command exits 1, and the warning is on stdout by then.

    The port is held on the same address the command is given, not always
    on 127.0.0.1.  A wildcard bind shares a port a specific-address
    listener holds on macOS, so ``--host 0.0.0.0`` used to bind, enter
    ``serve_forever`` and leave the harness waiting on a server that never
    exits.  An exact address-and-port conflict is refused on macOS, Linux
    and Windows alike.
    """

    def run_serve(self, host):
        """Run ``serve --host host`` on a port already in use; return the result."""
        import socket

        with socket.socket() as taken:
            taken.bind((host, 0))
            port = taken.getsockname()[1]
            taken.listen(1)
            result = self.run_command(f"serve --host {host} --port {port}")
        self.assertEqual(result.returncode, 1, result.stderr)
        return result

    def test_non_loopback_bind_warns_about_authentication(self):
        """Binding a routable interface says the service is unauthenticated."""
        result = self.run_serve("0.0.0.0")
        self.assertIn("non-loopback", result.stdout)
        self.assertIn("unauthenticated", result.stdout)

    def test_loopback_bind_does_not_warn(self):
        """The default loopback bind is the safe one, so it stays quiet."""
        result = self.run_serve("127.0.0.1")
        self.assertNotIn("unauthenticated", result.stdout)


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
        # Idempotent means one stored tag, not a second append.
        with Session(self.engine) as session:
            from resembl.models import Snippet

            stored = Snippet.get_by_name(session, "test_snippet")
            self.assertEqual(stored.tag_list.count("crypto"), 1)


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
        self.assertEqual(result.returncode, 0, result.stderr)
        # The prefix resolved to the snippet, not to an empty page.
        self.assertIn("test_snippet", result.stdout)
        self.assertIn("MOV EAX, 1", result.stdout)

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
            [sys.executable, "-m", "resembl.cli", "serve"],
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
        raise AssertionError(f"serve did not write {port_file} within {timeout}s")

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
                self.assertTrue(any("test_snippet" in m["names"] for m in payload["matches"]))

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
                    [sys.executable, "-m", "resembl.cli", "serve"],
                    env=self._serve_env(cache_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
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
                    [sys.executable, "-m", "resembl.cli", "serve", "--port", str(occupied)],
                    env=self._serve_env(cache_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
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
        self.assertTrue(os.path.exists(self.port_file), "port file must survive a timeout")

    def test_reset_keeps_port_file(self):
        """A connection reset against a live server keeps it discoverable.

        Concurrent load surfaces connection churn as RST; deleting the port
        file on a reset would orphan the healthy warm server for every other
        find client until it restarts.
        """
        import urllib.error
        from unittest.mock import patch

        import resembl.cli as cli

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ConnectionResetError("connection reset")),
        ):
            result = cli._find_via_server("MOV EAX, 1", 5, 0.5, True, 3)
        self.assertIsNone(result)
        self.assertTrue(os.path.exists(self.port_file), "port file must survive a reset")

    def test_malformed_response_keeps_port_file(self):
        """A malformed JSON reply does not delete a live server's advertisement."""
        import resembl.cli as cli

        class _BadResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"<html>not json</html>"

        with patch(
            "urllib.request.urlopen",
            side_effect=lambda request, timeout=None: _BadResponse(),
        ):
            result = cli._find_via_server("MOV EAX, 1", 5, 0.5, True, 3)
        self.assertIsNone(result)
        self.assertTrue(os.path.exists(self.port_file), "port file must survive a bad reply")

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

    def test_connection_refused_after_retarget_keeps_new_advertisement(self):
        """A refused connect must not delete a newer serve's advertisement.

        Between this client reading the port file and its refused connect,
        another ``serve`` may have bound and rewritten the file.  The old
        unconditional delete orphaned that healthy newcomer: every later
        find client missed the warm server until it restarted.  Only a file
        still naming the dead *port* may be retired.
        """
        import urllib.error
        from unittest.mock import patch

        import resembl.cli as cli

        # Signature-conformance stub for urllib.request.urlopen.
        def refused_after_retarget(request, timeout=None):  # pylint: disable=unused-argument
            # Simulate the newcomer winning the advertisement race between
            # this client's read of the port file and its connect attempt.
            with open(self.port_file, "w", encoding="utf-8") as f:
                f.write("6789")
            raise urllib.error.URLError(ConnectionRefusedError("no server"))

        with patch("urllib.request.urlopen", side_effect=refused_after_retarget):
            result = cli._find_via_server("MOV EAX, 1", 5, 0.5, True, 3)
        self.assertIsNone(result)
        with open(self.port_file, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "6789", "newer server's file was deleted")

    def test_find_batch_timeout_keeps_port_file(self):
        """The find-batch client has the same timeout-vs-refused behavior."""
        import urllib.error
        from unittest.mock import patch

        import resembl.cli as cli

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(TimeoutError("server busy")),
        ):
            result = cli._find_batch_via_server(["MOV EAX, 1", "MOV EBX, 2"], 5, 0.5, True, 3)
        self.assertIsNone(result)
        self.assertTrue(os.path.exists(self.port_file), "port file must survive a timeout")

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError("no server")),
        ):
            result = cli._find_batch_via_server(["MOV EAX, 1", "MOV EBX, 2"], 5, 0.5, True, 3)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.port_file), "stale file should be removed")


if __name__ == "__main__":
    unittest.main()
