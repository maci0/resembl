"""Tests for resembl's text-encoding and Unicode contract.

Covers:
- NFC is the one form snippets are stored, hashed and compared in, so the
  NFD and NFC spellings of the same text are one snippet and one alias.
- Filenames that carry a byte no encoding can decode (a lone surrogate, as
  POSIX permits and ``os.fsdecode`` produces) export instead of crashing.
- Astral-plane and combining characters survive a full store/find round-trip.
- Every text boundary names its encoding, and output is UTF-8 whatever the
  host's locale is.
"""

# pylint: disable=protected-access  # tests exercise private internals

import io
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

import typer.main
from sqlalchemy import func
from sqlmodel import Session, SQLModel, create_engine, select

from resembl import cli
from resembl.core import (
    _EXPORT_STEM_MAX_BYTES,
    _export_safe_filename,
    collection_create,
    snippet_add,
    snippet_get,
    snippet_name_add,
    snippet_name_remove,
    snippet_prepare,
    snippet_search_by_name,
    snippet_tag_add,
    string_checksum,
)
from resembl.models import Collection, Snippet
from resembl.paths import console_utf8_reconfigure
from resembl.scoring import code_tokenize, normalize_unicode

#: The same assembly snippet written two ways: ``café`` precomposed (NFC)
#: and ``cafe`` + U+0301 combining acute (NFD).  macOS produces the second
#: spelling for any filename containing an accented character.
_NFC_CODE = 'mov rax, 0\ndb "café", 0'
_NFD_CODE = unicodedata.normalize("NFD", _NFC_CODE)


def _cli_run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m resembl.cli <args>`` in a child process, as a user would."""
    return subprocess.run(
        [sys.executable, "-m", "resembl.cli", *args],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": os.path.join(os.getcwd(), "."),
            "RESEMBL_CONFIG_DIR": os.path.join(tempfile.gettempdir(), "resembl-no-config"),
            "RESEMBL_CACHE_DIR": os.path.join(tempfile.gettempdir(), "resembl-no-cache"),
            **(env or {}),
        },
    )


class TestNormalizeUnicode(unittest.TestCase):
    """The ingestion-time normalization helper itself."""

    def test_nfd_and_nfc_spellings_collapse(self):
        self.assertEqual(normalize_unicode(_NFC_CODE), normalize_unicode(_NFD_CODE))

    def test_nfc_is_the_idempotent_fixed_point(self):
        once = normalize_unicode(_NFD_CODE)
        self.assertEqual(once, unicodedata.normalize("NFC", once))

    def test_ascii_is_unchanged(self):
        self.assertEqual(normalize_unicode("MOV EAX, 1"), "MOV EAX, 1")

    def test_compatibility_characters_are_not_folded(self):
        """NFKC would rewrite these; NFC must leave them alone.

        Folding a fullwidth or superscript digit changes the text the user
        wrote rather than its spelling, so a snippet containing one must not
        silently become its ASCII equivalent.  Spelled as escapes so the
        intent survives an editor that normalizes the literals.
        """
        for text in ("\uff12", "\u00b9\u00b2", "\u216b"):
            self.assertEqual(normalize_unicode(text), text)
            self.assertNotEqual(unicodedata.normalize("NFKC", text), text)


class TestChecksumIdentity(unittest.TestCase):
    """The checksum is the snippet's primary key, so its input must be canonical."""

    def test_nfd_and_nfc_code_hash_the_same(self):
        self.assertEqual(string_checksum(_NFC_CODE), string_checksum(_NFD_CODE))

    def test_nfd_and_nfc_code_tokenize_the_same(self):
        self.assertEqual(code_tokenize(_NFC_CODE), code_tokenize(_NFD_CODE))

    def test_different_code_still_hashes_differently(self):
        """The normalization must not collapse genuinely different snippets."""
        self.assertNotEqual(string_checksum(_NFC_CODE), string_checksum("mov rax, 1"))


class TestSnippetRoundTrip(unittest.TestCase):
    """Both spellings must reach the database as a single row."""

    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        self.session = Session(engine)

    def tearDown(self):
        self.session.close()

    def test_adding_both_spellings_stores_one_snippet(self):
        first = snippet_add(self.session, "nfc", _NFC_CODE)
        second = snippet_add(self.session, "nfd", _NFD_CODE)
        self.assertIsNotNone(first)
        self.assertEqual(first.checksum, second.checksum)
        self.assertEqual(
            self.session.exec(select(func.count()).select_from(Snippet)).one(),
            1,
        )

    def test_stored_code_is_the_normalized_form_that_was_hashed(self):
        snippet = snippet_add(self.session, "nfd", _NFD_CODE)
        self.assertEqual(snippet.code, normalize_unicode(_NFD_CODE))

    def test_prepare_normalizes_name_and_code(self):
        checksum, name, code, _minhash = snippet_prepare("cafe\u0301", _NFD_CODE, 3)
        self.assertEqual(checksum, string_checksum(_NFC_CODE))
        self.assertEqual(name, "café")
        self.assertEqual(code, _NFC_CODE)

    def test_nfd_name_spelling_is_idempotent_against_an_nfc_one(self):
        """macOS hands back NFD names; adding the NFD spelling must not alias twice."""
        snippet = snippet_add(self.session, "café", "MOV EAX, 1")
        snippet_name_add(self.session, snippet.checksum, "second")
        same = snippet_name_add(self.session, snippet.checksum, "cafe\u0301")
        self.assertEqual(snippet.name_list, ["café", "second"])
        self.assertIsNotNone(same)

        # And the NFD spelling still finds the row to remove.
        self.assertIsNotNone(snippet_name_remove(self.session, snippet.checksum, "café"))

    def test_tags_collapse_the_same_way(self):
        snippet = snippet_add(self.session, "s", "MOV EAX, 1")
        snippet_tag_add(self.session, snippet.checksum, "café")
        snippet_tag_add(self.session, snippet.checksum, "café")
        self.assertEqual(snippet.tag_list, ["café"])

    def test_astral_and_combining_characters_survive_the_round_trip(self):
        code = 'db "\U0001f600é", 0'
        snippet = snippet_add(self.session, "\U0001f600-combining", code)
        self.assertIsNotNone(snippet)
        stored = snippet_get(self.session, snippet.checksum)
        self.assertEqual(stored.code, code)
        self.assertEqual(stored.name_list, ["\U0001f600-combining"])


class TestSafeFilenameSurrogates(unittest.TestCase):
    """POSIX filenames are bytes; an undecodable one must still export."""

    def test_lone_surrogate_in_a_name_does_not_crash_the_export(self):
        stem = _export_safe_filename("bad\udcff.asm")
        self.assertTrue(stem)
        self.assertNotIn("\udcff", stem)
        # The result must be encodable: that was the crash.
        self.assertIsInstance(stem.encode("utf-8"), bytes)

    def test_a_real_undecodable_filename_exports(self):
        """The import path, end to end, with a byte no encoding can decode."""
        with tempfile.TemporaryDirectory() as temp_dir:
            raw = os.path.join(os.fsencode(temp_dir), b"bad\xff.asm")
            with open(raw, "wb") as handle:
                handle.write(b"mov eax, 1\n")
            name = os.path.splitext(os.path.basename(os.fsdecode(raw)))[0]
            self.assertIn("\udcff", name)
            self.assertTrue(_export_safe_filename(name))

    def test_astral_characters_still_survive_sanitization(self):
        self.assertEqual(_export_safe_filename("caf\U0001f600"), "caf\U0001f600")

    def test_the_byte_bound_still_holds_for_surrogate_names(self):
        stem = _export_safe_filename("界" * 300)
        self.assertLessEqual(len(stem.encode("utf-8")), _EXPORT_STEM_MAX_BYTES)


class TestCollectionIdentity(unittest.TestCase):
    """A collection name is a primary key, so its form is part of its identity."""

    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        self.session = Session(engine)

    def tearDown(self):
        self.session.close()

    def test_nfd_create_finds_the_existing_nfc_collection(self):
        """The lookup runs on the normalized name, so it cannot miss the row."""
        first = collection_create(self.session, "café")
        again = collection_create(self.session, "café")
        self.assertEqual(first.name, "café")
        self.assertEqual(again.name, "café")
        self.assertEqual(
            self.session.exec(select(func.count()).select_from(Collection)).one(),
            1,
        )


class TestNameSearch(unittest.TestCase):
    """A name search looks stored text up, so it must run on the stored form."""

    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        self.session = Session(engine)

    def tearDown(self):
        self.session.close()

    def test_nfd_pattern_finds_the_nfc_name(self):
        """The pattern macOS and a copy-paste hand over: the base letter plus
        a combining acute, a sequence no stored row contains."""
        snippet = snippet_add(self.session, "café", "MOV EAX, 1")
        self.assertIsNotNone(snippet)
        found = snippet_search_by_name(self.session, "cafe\u0301")
        self.assertEqual([s.checksum for s in found], [snippet.checksum])

    def test_a_pattern_no_name_contains_still_matches_nothing(self):
        snippet_add(self.session, "café", "MOV EAX, 1")
        self.assertEqual(snippet_search_by_name(self.session, "tea"), [])

    def test_a_name_lookup_finds_a_non_ascii_name(self):
        """The LIKE probe has to spell the name the way the column stores it:
        ``json.dumps`` escapes every non-ASCII character, so the character
        itself matches no row."""
        snippet = snippet_add(self.session, "café", "MOV EAX, 1")
        self.assertIsNotNone(snippet)
        found = Snippet.get_by_name(self.session, "café")
        self.assertIsNotNone(found)
        self.assertEqual(found.checksum, snippet.checksum)

    def test_a_name_lookup_still_finds_a_name_with_a_quote(self):
        snippet = snippet_add(self.session, 'say "hi"', "MOV EAX, 1")
        self.assertIsNotNone(snippet)
        found = Snippet.get_by_name(self.session, 'say "hi"')
        self.assertIsNotNone(found)
        self.assertEqual(found.checksum, snippet.checksum)


class TestEncodingBoundaries(unittest.TestCase):
    """Both text boundaries name UTF-8: the file a query is read from, and the
    stream a name is printed to."""

    def test_query_file_options_declare_utf8(self):
        """``--file`` must not inherit the platform's locale.

        ``resembl import`` has always read snippets as UTF-8.  A query file
        read with the locale's encoding decodes the same bytes to different
        text, so the query stops matching the snippet that same file imports
        as, and a file outside the local code page is refused outright.
        """
        command = typer.main.get_command(cli.app)
        for name in ("find", "find-batch"):
            sub = command.commands[name]
            file_param = next(p for p in sub.params if p.name == "file")
            self.assertEqual(getattr(file_param.type, "encoding", None), "utf-8", name)

    def test_a_utf8_query_file_matches_the_imported_snippet(self):
        """End to end, with the child's locale defaulting to plain ASCII."""
        code = 'mov eax, 1\ndb "café", 0\n'
        with tempfile.TemporaryDirectory() as temp_dir:
            query_path = os.path.join(temp_dir, "query.asm")
            with open(query_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(code)

            added = _cli_run("import", query_path, "--force")
            self.assertEqual(added.returncode, 0, added.stderr)

            # The C locale with coercion off is the platform default a
            # ``--file`` read used to inherit: ANSI_X3.4-1968, which cannot
            # decode the file at all.
            found = _cli_run(
                "--format",
                "json",
                "find",
                "--file",
                query_path,
                env={
                    "LC_ALL": "C",
                    "LANG": "C",
                    "PYTHONCOERCECLOCALE": "0",
                    "PYTHONUTF8": "0",
                },
            )
            self.assertEqual(found.returncode, 0, found.stderr)
            self.assertEqual(len(json.loads(found.stdout)["matches"]), 1, found.stdout)

    def test_output_is_utf8_whatever_the_locale_says(self):
        """A locale-encoded stdout raises on the first name it cannot draw,
        losing the whole report instead of one character."""
        raw = io.BytesIO()
        legacy = io.TextIOWrapper(raw, encoding="cp1252")
        with patch("sys.stdout", legacy), patch("sys.stderr", legacy):
            console_utf8_reconfigure()
            self.assertEqual(sys.stdout.encoding, "utf-8")
            print("日本語")
            sys.stdout.flush()
        self.assertEqual(raw.getvalue().decode("utf-8"), "日本語\n")

    def test_a_utf8_stream_is_left_alone(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8")
        with patch("sys.stdout", stream), patch("sys.stderr", stream):
            console_utf8_reconfigure()
        self.assertEqual(stream.encoding, "utf-8")
        stream.detach()

    def test_a_name_the_local_code_page_cannot_print_still_lists(self):
        """The shipped report, with the child's stdout pinned to cp1252: this
        raised ``UnicodeEncodeError`` and printed nothing at all."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_url = f"sqlite:///{os.path.join(temp_dir, 'test.db')}"
            env = {"DATABASE_URL": db_url, "PYTHONIOENCODING": "cp1252"}
            added = _cli_run("add", "日本語", 'db "café", 0', env=env)
            self.assertEqual(added.returncode, 0, added.stderr)
            listed = _cli_run("--format", "json", "list", env=env)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)[0]["names"], ["日本語"])


if __name__ == "__main__":
    unittest.main()
