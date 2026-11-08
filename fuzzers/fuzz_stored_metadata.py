#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the stored snippet metadata readers.

Every snippet row a ``merge`` copies from another database carries
two JSON-encoded string columns, ``names`` and ``tags``, that this
process does not control: a hostile merge source writes them
verbatim, and a hand-edited database file writes them too.  The merge
path reads them through :func:`_string_list_or_none` /
:func:`_source_lists_or_none`, whose documented contract is that one
corrupt row neither aborts the whole merge nor poisons the
destination — it is skipped with a warning and the merge continues.
A reader that escapes as a second exception type (a deeply nested
document recurses in the JSON decoder and raises ``RecursionError``,
which is not a ``ValueError``) turns that one row into a merge that
never finishes.

The harness drives the two real entry points with structure-aware
inputs and asserts the invariants the merge relies on, not just the
absence of a crash:

* ``_string_list_or_none`` never raises for any text: a missing,
  malformed, deeply nested, non-list, or list-of-non-strings value
  answers ``None``;
* an accepted value is a list of ``str`` and round-trips through
  ``json.dumps``/``json.loads`` unchanged, so what the merge writes
  back is what it read;
* ``_source_lists_or_none`` pairs two accepted lists or answers
  ``None`` when either column is corrupt, never a half-pair;
* a name/tag accepted from a corrupt-capable row survives the merge
  union (``_merge_names``) and NFC normalization without raising, so
  the destination never inherits a value its own readers choke on.

Inputs are generated structure-aware: realistic JSON string lists
drawn from the committed corpus shapes (realistic alias names,
unicode, whitespace), hostile shapes (non-list documents, lists with
non-string entries, truncated documents, deeply nested arrays and
objects, surrogate escapes, duplicate entries), and raw junk bytes.
Seeds come from the documented column shape, not from captured
traffic.
"""

import json
import sys

import atheris

with atheris.instrument_imports():
    from resembl.core import _merge_names, _source_lists_or_none, _string_list_or_none
    from resembl.models import name_normalize

#: Realistic alias/tag spellings worth spending runs on.
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
    """Return one stored-metadata value built from *fdp*."""
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


def _check_list(raw: str) -> list[str] | None:
    """Assert ``_string_list_or_none`` holds its contract for *raw*."""
    result = _string_list_or_none(raw)
    if result is None:
        return None
    assert isinstance(result, list), f"accepted non-list: {result!r}"
    assert all(isinstance(item, str) for item in result), f"accepted non-string: {result!r}"
    # Persistence round trip: what the merge reads must survive the
    # write back into the destination row.
    assert json.loads(json.dumps(result)) == result, "accepted list does not round-trip"
    # The merge union and NFC normalization must accept every name
    # the reader accepted, so a hostile row cannot poison the
    # destination through a value its own readers choke on.
    merged = _merge_names(result, result)
    normalized = [name_normalize(name) for name in result]
    assert merged in (
        normalized,
        result,
    ), f"merge union changed an accepted list: {result!r} -> {merged!r}"
    for name in result:
        # A lone surrogate has no UTF-8 form; normalization must
        # still answer a string (it may keep the surrogate).
        assert isinstance(name_normalize(name), str), f"normalize failed: {name!r}"
    return result


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    names_raw = _build_document(fdp)
    tags_raw = _build_document(fdp)
    names = _check_list(names_raw)
    tags = _check_list(tags_raw)

    # The paired reader answers a pair or None, never a half-pair,
    # and it agrees with the single-column reader on both columns.
    class _Row:
        def __init__(self, names_value: str, tags_value: str) -> None:
            self.names = names_value
            self.tags = tags_value
            self.checksum = "fuzz-checksum"

    paired = _source_lists_or_none(_Row(names_raw, tags_raw))
    if names is None or tags is None:
        assert paired is None, f"paired reader accepted corrupt column: {paired!r}"
    else:
        assert paired == (names, tags), f"paired reader disagrees: {paired!r} vs {(names, tags)!r}"


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
