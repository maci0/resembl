"""Guard the encoding and normalization rules for user text.

Names are identity: a collection's primary key, a snippet alias, and the
stem of an exported file.  The text they come from is arbitrary — a
filesystem name (macOS hands out decomposed spellings, Linux composed
ones), a command-line argument, a file whose bytes are read as UTF-8 — so
these tests pin the one form every path agrees on (NFC), and the escaping
that keeps a control character from silently changing an exported rule.
"""

# pylint: disable=protected-access  # tests exercise private internals

import json
import os
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from resembl.core import (
    _yara_string_escape,
    collection_add_snippet,
    collection_create,
    collection_delete,
    db_merge,
    snippet_add,
    snippet_export_yara,
    snippet_name_add,
    snippet_name_remove,
    snippet_prepare,
    snippet_search_by_name,
    snippet_tag_add,
    snippet_tag_remove,
)
from resembl.models import Collection, Snippet, name_normalize

#: The composed and decomposed spellings of one filename.  macOS stores
#: the decomposed form, so this pair is the same file as far as a user is
#: concerned and must stay one name in the database.
_NFC_NAME = unicodedata.normalize("NFC", "café.asm")
_NFD_NAME = unicodedata.normalize("NFD", "café.asm")


class BaseDBTest(unittest.TestCase):
    """Base class providing an in-memory database session per test."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()


class TestNameNormalization(BaseDBTest):
    """A name is stored and found in one canonical form."""

    def test_spelling_pair_is_distinct_only_in_normalization_form(self):
        self.assertNotEqual(_NFC_NAME, _NFD_NAME)
        self.assertEqual(name_normalize(_NFC_NAME), name_normalize(_NFD_NAME))

    def test_add_stores_the_composed_spelling(self):
        snippet = snippet_add(self.session, _NFD_NAME, "MOV EAX, 1")
        self.assertEqual(snippet.name_list, [_NFC_NAME])

    def test_add_twice_under_both_spellings_yields_one_alias(self):
        """The same file reached from macOS and from Linux is one alias."""
        first = snippet_add(self.session, _NFD_NAME, "MOV EAX, 1")
        second = snippet_add(self.session, _NFC_NAME, "MOV EAX, 1")
        self.assertEqual(first.checksum, second.checksum)
        self.assertEqual(second.name_list, [_NFC_NAME])

    def test_import_prepares_the_same_name_as_add(self):
        """The bulk-import worker must not disagree with `resembl add`."""
        prepared = snippet_prepare(_NFD_NAME, "MOV EAX, 1")
        assert prepared is not None
        self.assertEqual(prepared[1], _NFC_NAME)

    def test_get_by_name_finds_a_non_ascii_name(self):
        """`json.dumps` escapes the column, so the probe must escape too."""
        snippet = snippet_add(self.session, "résumé", "MOV EAX, 1")
        found = Snippet.get_by_name(self.session, "résumé")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.checksum, snippet.checksum)

    def test_get_by_name_finds_a_name_stored_before_normalization(self):
        """A row written in the other canonical form still resolves."""
        snippet = snippet_add(self.session, "unused", "MOV EAX, 1")
        snippet.names = json.dumps([_NFD_NAME])
        self.session.add(snippet)
        self.session.commit()
        found = Snippet.get_by_name(self.session, _NFC_NAME)
        self.assertIsNotNone(found)

    def test_get_by_name_still_handles_quotes_and_backslashes(self):
        """The escaping the probe does must not break these two."""
        tricky = 'we"ird\\name'
        snippet_add(self.session, tricky, "MOV EAX, 1")
        found = Snippet.get_by_name(self.session, tricky)
        self.assertIsNotNone(found)

    def test_get_by_name_letter_wildcards_are_not_wildcards(self):
        """A name containing ``%``/``_`` matches itself, not everything."""
        snippet_add(self.session, "a_b", "MOV EAX, 1")
        self.assertIsNotNone(Snippet.get_by_name(self.session, "a_b"))
        self.assertIsNone(Snippet.get_by_name(self.session, "aXb"))

    def test_get_by_name_normalizes_the_query_once(self):
        """ASCII hits are checked against one composed query.

        The LIKE probe can admit many rows.  Composing those ASCII aliases
        again would repeat NFC on text that is already NFC.
        """
        for i in range(5):
            decoy = snippet_add(self.session, f"decoy_{i}", f"NOP\n; decoy {i}\nRET")
            # The stored JSON contains the quoted query as a substring, so
            # the LIKE probe returns the row, and the decoded name does not
            # equal the query, so verification rejects it.
            decoy.names = json.dumps(['x"mov_label'])
            self.session.add(decoy)
        real = snippet_add(self.session, "mov_label", "MOV EAX, 1\nRET")
        self.session.commit()

        calls: list[str] = []

        def counting(value: str) -> str:
            calls.append(value)
            return name_normalize(value)

        with patch("resembl.models.name_normalize", side_effect=counting):
            found = Snippet.get_by_name(self.session, "mov_label")

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.checksum, real.checksum)
        self.assertEqual(calls, ["mov_label"])

    def test_search_normalizes_the_query_once(self):
        """ASCII hits are checked against one composed pattern.

        The LIKE probe can return every alias that contains the pattern.
        Composing those ASCII aliases again would repeat NFC on text that
        is already NFC.
        """
        for i in range(12):
            snippet_add(self.session, f"mov_{i}", f"MOV EAX, {i}\nRET")
        self.session.commit()

        calls: list[str] = []

        def counting(value: str) -> str:
            calls.append(value)
            return name_normalize(value)

        with patch("resembl.core.name_normalize", side_effect=counting):
            found = snippet_search_by_name(self.session, "mov", limit=50)

        self.assertEqual(
            sorted(name for row in found for name in row.name_list),
            sorted(f"mov_{i}" for i in range(12)),
        )
        self.assertEqual(calls, ["mov"])

    def test_name_add_normalizes_the_query_once(self):
        """ASCII aliases are checked against one composed query.

        Adding a name walks every alias already on the snippet.  Composing
        those ASCII aliases again would repeat NFC on text that is already
        NFC.
        """
        snippet = snippet_add(self.session, "mov_label", "MOV EAX, 1\nRET")
        for i in range(5):
            snippet_name_add(self.session, snippet.checksum, f"alias_{i}")
        self.session.commit()

        calls: list[str] = []

        def counting(value: str) -> str:
            calls.append(value)
            return name_normalize(value)

        with patch("resembl.core.name_normalize", side_effect=counting):
            updated = snippet_name_add(self.session, snippet.checksum, "fresh_alias")

        assert updated is not None
        self.assertIn("fresh_alias", updated.name_list)
        self.assertEqual(calls, ["fresh_alias"])

    def test_name_add_is_idempotent_across_normalization_forms(self):
        snippet = snippet_add(self.session, _NFC_NAME, "MOV EAX, 1")
        same = snippet_name_add(self.session, snippet.checksum, _NFD_NAME)
        assert same is not None
        self.assertEqual(same.name_list, [_NFC_NAME])

    def test_name_remove_accepts_either_spelling(self):
        snippet = snippet_add(self.session, "unused", "MOV EAX, 1")
        snippet_name_add(self.session, snippet.checksum, _NFD_NAME)
        removed = snippet_name_remove(self.session, snippet.checksum, _NFC_NAME)
        assert removed is not None
        self.assertNotIn(_NFC_NAME, [name_normalize(n) for n in removed.name_list])

    def test_collection_is_one_row_per_name(self):
        collection_create(self.session, _NFD_NAME)
        collection_create(self.session, _NFC_NAME)
        self.assertEqual(len(self.session.exec(select(Collection)).all()), 1)

    def test_collection_lookup_and_delete_accept_either_spelling(self):
        snippet = snippet_add(self.session, "unused", "MOV EAX, 1")
        collection_create(self.session, _NFC_NAME)
        added = collection_add_snippet(self.session, _NFD_NAME, snippet.checksum)
        assert added is not None
        self.assertEqual(added.collection, _NFC_NAME)
        self.assertTrue(collection_delete(self.session, _NFD_NAME))

    def test_padded_and_unicode_space_spellings_are_one_name(self):
        snippet = snippet_add(self.session, " café ", "MOV EAX, 1")
        self.assertEqual(snippet.name_list, ["café"])
        self.assertIsNotNone(Snippet.get_by_name(self.session, " café "))
        self.assertIsNotNone(Snippet.get_by_name(self.session, "\u00a0café\u00a0"))
        self.assertEqual(
            [s.name_list for s in snippet_search_by_name(self.session, " café ")],
            [["café"]],
        )
        collection_create(self.session, "foo")
        self.assertTrue(collection_delete(self.session, " foo "))

    def test_merge_does_not_duplicate_a_decomposed_name(self):
        """Merging a macOS-written database creates no second alias."""
        snippet = snippet_add(self.session, "unused", "MOV EAX, 1")
        snippet_name_add(self.session, snippet.checksum, _NFC_NAME)

        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "source.db")
            source_engine = create_engine(f"sqlite:///{source_path}")
            SQLModel.metadata.create_all(source_engine)
            with Session(source_engine) as source_session:
                source_snippet = Snippet(
                    checksum=snippet.checksum,
                    names=json.dumps([_NFD_NAME]),
                    code="MOV EAX, 1",
                    minhash=snippet.minhash,
                )
                source_session.add(source_snippet)
                source_session.commit()
            source_engine.dispose()

            result = db_merge(self.session, source_path)

        self.assertEqual(result["skipped"], 1)
        stored = self.session.exec(select(Snippet.names)).all()
        self.assertEqual(json.loads(stored[0]), ["unused", _NFC_NAME])


class TestYaraStringEscape(unittest.TestCase):
    """A control character must not shorten the pattern it sits in."""

    def test_backslash_quote_and_newlines_are_escaped(self):
        self.assertEqual(_yara_string_escape('a"b\\c\nd\re'), 'a\\"b\\\\c\\nd\\re')

    def test_nul_is_escaped(self):
        """YARA reads a string literal as a C string: a raw NUL truncates."""
        self.assertEqual(_yara_string_escape("a\x00b"), "a\\x00b")

    def test_other_control_characters_are_escaped(self):
        self.assertEqual(_yara_string_escape("a\x1b[31m\x07b"), "a\\x1b[31m\\x07b")

    def test_tab_is_escaped(self):
        self.assertEqual(_yara_string_escape("a\tb"), "a\\tb")

    def test_a_lone_surrogate_does_not_become_a_short_escape(self):
        """A surrogate must not render as a four-digit hex escape, which
        parses as a two-digit escape plus two literal characters."""
        self.assertEqual(_yara_string_escape("bad\udcff"), "bad?")

    def test_printable_text_passes_through_unchanged(self):
        for text in ("mov eax, 1", "café", "日本語", "\U0001f600", ""):
            self.assertEqual(_yara_string_escape(text), text)

    def test_exported_rule_file_carries_no_raw_control_bytes(self):
        """End to end: a snippet with control bytes still exports cleanly."""
        engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            snippet_add(session, "weird", "MOV EAX, 0\x00\x1b[31m\x07")
            with tempfile.TemporaryDirectory() as tmpdir:
                out_file = os.path.join(tmpdir, "rules.yara")
                result = snippet_export_yara(session, out_file)
                self.assertEqual(result["num_exported"], 1)
                with open(out_file, "rb") as handle:
                    raw = handle.read()
        engine.dispose()
        self.assertNotIn(b"\x00", raw)
        self.assertNotIn(b"\x1b", raw)
        self.assertIn(b"\\x00", raw)


class TestNameSearchCaseInsensitivity(BaseDBTest):
    """snippet_search_by_name is documented case-insensitive; the check must be."""

    def test_upper_ascii_pattern_finds_lower_name(self):
        snippet_add(self.session, "mov_label", "MOV EAX, 1")
        found = snippet_search_by_name(self.session, "MOV")
        self.assertEqual([s.name_list for s in found], [["mov_label"]])

    def test_upper_accented_pattern_finds_lower_name(self):
        snippet_add(self.session, "café", "MOV EAX, 1")
        found = snippet_search_by_name(self.session, "CAFÉ")
        self.assertEqual([s.name_list for s in found], [["café"]])

    def test_casefold_pattern_finds_sharp_s_name(self):
        """casefold (not lower) maps ß to ss, so both ASCII spellings find it."""
        snippet_add(self.session, "straße", "MOV EAX, 1")
        for pattern in ("strasse", "STRASSE", "straße"):
            found = snippet_search_by_name(self.session, pattern)
            self.assertEqual([s.name_list for s in found], [["straße"]], pattern)

    def test_like_metacharacters_in_pattern_are_literal(self):
        snippet_add(self.session, "a_b", "MOV EAX, 1")
        found = snippet_search_by_name(self.session, "a_b")
        self.assertEqual([s.name_list for s in found], [["a_b"]])

    def test_limit_bounds_verified_results(self):
        for i in range(5):
            snippet_add(self.session, f"mov_{i}", f"MOV EAX, {i}\nRET")
        found = snippet_search_by_name(self.session, "MOV", limit=2)
        self.assertEqual(len(found), 2)


class TestTagNormalizationForms(BaseDBTest):
    """Tags are identity like names: one NFC form, whichever spelling arrives."""

    def test_add_is_idempotent_across_normalization_forms(self):
        snippet = snippet_add(self.session, "s", "MOV EAX, 1")
        snippet.tags = json.dumps([_NFD_NAME])
        self.session.add(snippet)
        self.session.commit()
        snippet_tag_add(self.session, snippet.checksum, _NFC_NAME)
        self.assertEqual(snippet.tag_list, [_NFD_NAME])

    def test_remove_accepts_either_spelling(self):
        snippet = snippet_add(self.session, "s", "MOV EAX, 1")
        snippet.tags = json.dumps([_NFD_NAME])
        self.session.add(snippet)
        self.session.commit()
        removed = snippet_tag_remove(self.session, snippet.checksum, _NFC_NAME)
        assert removed is not None
        self.assertEqual(removed.tag_list, [])


if __name__ == "__main__":
    unittest.main()
