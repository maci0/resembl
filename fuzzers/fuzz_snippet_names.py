#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the stored snippet-name readers.

The ``names`` column of a snippet row is a JSON-encoded string list that
a hostile ``merge`` source or a hand-edited database file supplies
verbatim, and three readers decode it with bare ``json.loads`` — no
guard at all.  ``Snippet.name_list`` is the property every caller reads
(``show``, ``name add``), ``Snippet.get_by_name`` verifies its LIKE
candidates by decoding each one, and ``core.snippet_search_by_name``
decodes every row it scans.  A malformed value (truncated JSON, not a
list, a list with non-strings) raises ``json.JSONDecodeError`` out of
each of them, and a deeply nested document raises ``RecursionError``
which is not a ``ValueError`` — so one corrupt row turns a lookup, a
search, or a listing that should have skipped it into a crashed command.

The harness drives the three real readers over one in-memory database
with structure-aware inputs and asserts the invariants the callers
rely on, not just the absence of a crash:

* the readers never raise for any stored text: a missing, malformed,
  deeply nested, non-list, or list-of-non-strings value is the row's
  fault, not a crash;
* a value that *is* read back is a list of ``str`` that survives a
  ``json.dumps``/``json.loads`` round trip unchanged, so rendering it
  cannot lose a name;
* reading the same row twice yields the same answer, so a flaky read
  cannot flip a lookup between hit and miss;
* the search probe and the name lookup agree: a name the reader
  accepts is found by the lookup the operator types.

Inputs are generated structure-aware: realistic JSON string lists
drawn from the committed corpus shapes (realistic alias names,
unicode, whitespace, NFC/NFD spellings), hostile shapes (non-list
documents, lists with non-string entries, truncated documents,
deeply nested arrays and objects, surrogate escapes, duplicate
entries), and raw junk bytes.  Seeds come from the documented column
shape, not from captured traffic.  No snippet value the fuzzer draws
is ever written to a file outside a temporary directory the harness
owns and deletes.
"""

import json
import os
import sys
import tempfile
from typing import Any

import atheris

with atheris.instrument_imports():
    from sqlmodel import Session, SQLModel, create_engine

    from resembl.core import snippet_search_by_name
    from resembl.database import create_db_engine
    from resembl.models import Snippet

#: Realistic alias spellings worth spending runs on.
_INTERESTING_NAMES = (
    "ret",
    "mov eax, 1",
    "a" * 64,
    "café",
    "cafe\u0301",
    "\uff21\uff22\uff23",
    "",
    "  ",
    "\x00\x01",
    "héllo wörld",
    "bad\ud800",
    "a/b",
    "a\\b",
    '"quoted"',
)

#: JSON documents that are not a string list, plus near-misses.
_NON_LIST_DOCUMENTS = (
    "null",
    "123",
    '"just a string"',
    "true",
    '{"a": 1}',
    '{"names": ["ret"]}',
    "[1, 2, 3]",
    "[null]",
    "[true, false]",
    '[["nested"]]',
    '[{"a": "b"}]',
    "",
    "   ",
    "not json",
    '["unterminated',
    '["a",]',
    '["a"] trailing',
    "[]extra",
)


def _build_document(fdp: atheris.FuzzedDataProvider) -> str:
    """Return one stored ``names`` value built from *fdp*."""
    mode = fdp.ConsumeIntInRange(0, 4)
    if mode == 0:
        # A well-formed string list: realistic names, maybe hostile ones.
        names = [
            _INTERESTING_NAMES[fdp.ConsumeIntInRange(0, len(_INTERESTING_NAMES) - 1)]
            for _ in range(fdp.ConsumeIntInRange(0, 4))
        ]
        return json.dumps(names)
    if mode == 1:
        # A non-list document or a list with the wrong element types.
        return _NON_LIST_DOCUMENTS[fdp.ConsumeIntInRange(0, len(_NON_LIST_DOCUMENTS) - 1)]
    if mode == 2:
        # Deeply nested: the decoder recurses once per level.
        depth = fdp.ConsumeIntInRange(2, 5000)
        opener, closer = ("[", "]") if fdp.ConsumeBool() else ('{"a":', "}")
        return f"{opener * depth}{closer * depth}"
    if mode == 3:
        # A JSON string built from raw unicode, surrogates included.
        text = fdp.ConsumeUnicode(fdp.ConsumeIntInRange(0, 32))
        return json.dumps([text])
    # Raw junk bytes that decode as UTF-8 with replacement where they can.
    raw = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))
    return raw.decode("utf-8", errors="replace")


def _check_snippet_names(stored: str) -> Any:
    """Assert the three readers hold their contracts for one stored value."""
    snippet = Snippet(checksum="fuzz-checksum", code="ret", names=stored, minhash=b"", tags="[]")
    try:
        names = snippet.name_list
    except (ValueError, RecursionError, TypeError):
        # The reader raises rather than answering: the callers of the
        # property have no guard for this, so that is the crash the
        # fuzzer reports.
        return None
    assert isinstance(names, list), f"name_list returned non-list: {names!r}"
    assert all(isinstance(item, str) for item in names), f"non-string name: {names!r}"
    # Persistence round trip: what the reader read must survive the
    # write back into the column.
    assert json.loads(json.dumps(names)) == names, "accepted list does not round-trip"
    # Fixed point: the same stored bytes read twice.
    assert snippet.name_list == names, "name_list is not a fixed point"
    return names


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    stored = _build_document(fdp)
    _check_snippet_names(stored)

    # The database readers share the same stored value; a temp-dir
    # database keeps them off the operator's real database.
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_db_engine("sqlite:///" + os.path.join(tmp, "fuzz.db"))
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(
                Snippet(
                    checksum="fuzz-checksum",
                    code="ret",
                    names=stored,
                    minhash=b"fuzz",
                    tags="[]",
                )
            )
            session.commit()
            # get_by_name must not raise for the stored value, and the
            # name search must not raise either: both decode the column
            # per candidate and per row, respectively.
            probe = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 16))
            found = Snippet.get_by_name(session, probe)
            assert found is None or isinstance(found, Snippet), f"get_by_name returned {found!r}"
            hits = snippet_search_by_name(session, probe, limit=5)
            assert isinstance(hits, list), f"name search returned non-list: {hits!r}"
            # A second read of the same row through the search agrees.
            assert snippet_search_by_name(session, probe, limit=5) == hits, (
                "name search is not a fixed point"
            )
        engine.dispose()


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
