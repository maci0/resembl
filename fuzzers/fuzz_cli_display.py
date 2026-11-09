#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the CLI output sanitizers.

``resembl.cli._csv_safe`` / ``_csv_safe_tree`` and ``_display`` /
``_strip_controls`` sit on the path from a snippet's stored name to a
spreadsheet cell and to a terminal.  A name that evades the CSV
formula guard opens as a live formula (``=HYPERLINK`` exfiltration,
DDE command execution) when the operator opens the export in a
spreadsheet; a name that survives ``_display`` with a raw ESC, CR, or
rich bracket markup rewrites the viewer's screen or restyles the
report.  These are untrusted-input parsers in the output direction:
the input is an arbitrary merge-sourced name, tag, or query string,
and a missed code point is a silent injection, not a crash a fuzzer
would see without an invariant.

The harness drives the real entry points and asserts the invariants
the callers rely on, not just the absence of a crash:

* ``_csv_safe`` never returns a string whose spreadsheet-visible
  first character is a formula trigger (``= + - @`` tab CR) after
  the stripping the implementation documents (whitespace plus the
  invisible set); a guarded string starts with the text-forcing
  apostrophe and the remainder is untouched;
* ``_csv_safe`` is a fixed point on its own output, and non-strings
  pass through unchanged;
* ``_csv_safe_tree`` applies the same guard at every depth, so a
  name smuggled into a nested find-batch payload is neutralized
  like a top-level cell;
* ``_strip_controls`` output carries no C0 control except tab and
  newline, no CR, no DEL, and no C1 control, so no ANSI sequence can
  survive;
* ``_display`` output carries no unescaped ``[`` bracket markup, so
  rich cannot restyle the report with a stored name.

Inputs are generated structure-aware: an interesting alphabet (formula
triggers, whitespace, the invisible set, ESC and other C0/C1 controls,
rich brackets, surrogates, non-ASCII, corpus-shaped assembly text),
plus raw unicode and junk bytes.  Nothing is copied from production
traffic.
"""

import csv
import io
import sys

import atheris

with atheris.instrument_imports():
    from resembl.cli import _csv_safe, _csv_safe_tree, _display, _strip_controls

#: Spreadsheet-visible formula triggers plus the characters the guard
#: strips before the check (whitespace classes and the invisible set).
#: A code point outside this alphabet that a spreadsheet skips when it
#: lands on a trigger would evade the guard and fail the invariant.
_INVISIBLE = (
    "\ufeff",
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\u00ad",
    "\u180e",
    "\u061c",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\u00a0",
    "\u2000",
    "\u2001",
    "\u2002",
    "\u2003",
    "\u2004",
    "\u2005",
    "\u2006",
    "\u2007",
    "\u2008",
    "\u2009",
    "\u200a",
    "\u202f",
    "\u205f",
    "\u3000",
    "\u1680",
)

#: Tokens worth spending runs on: formula triggers, invisibles, ANSI /
#: rich markup, controls, and realistic name shapes.
_INTERESTING = (
    "=",
    "+",
    "-",
    "@",
    "\t",
    "\r",
    "\n",
    " ",
    "\x00",
    "\x01",
    "\x07",
    "\x1b",
    "\x7f",
    "\x80",
    "\x85",
    "\x9f",
    "[",
    "]",
    "[bold]",
    "[/]",
    "[red]evil[/]",
    "rule",
    "a",
    "Z",
    "0",
    "1+1",
    "=1+1",
    "HYPERLINK",
    "é",
    "\uff21\uff22",
    "\ud800",
    "\udfff",
    "\u202e",
    "push ebp",
    "mov eax, 1",
    "http://example.com",
    *_INVISIBLE,
)


def _build_text(fdp: atheris.FuzzedDataProvider) -> str:
    """Return fuzzer-chosen text from the interesting alphabet or raw unicode."""
    mode = fdp.ConsumeIntInRange(0, 3)
    if mode == 0:
        return "".join(
            _INTERESTING[fdp.ConsumeIntInRange(0, len(_INTERESTING) - 1)]
            for _ in range(fdp.ConsumeIntInRange(0, 24))
        )
    if mode == 1:
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 64))
    if mode == 2:
        return fdp.ConsumeUnicode(fdp.ConsumeIntInRange(0, 64))
    raw = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))
    return raw.decode("utf-8", errors="replace")


def _spreadsheet_first(text: str) -> str:
    """Return the first spreadsheet-visible character of *text*.

    Mirrors the documented stripping: whitespace (``str.strip``) and
    the invisible set in any interleaving, repeated to a fixed point.
    """
    stripped = text
    while True:
        nxt = stripped.strip().lstrip("".join(_INVISIBLE))
        if nxt == stripped:
            break
        stripped = nxt
    return stripped[:1]


def _check_csv(text: str) -> None:
    """Assert the CSV guard neutralizes every formula-shaped value."""
    out = _csv_safe(text)
    assert isinstance(out, str), f"_csv_safe returned non-str: {type(out)}"
    first = _spreadsheet_first(out)
    # A guarded cell starts with the apostrophe; after it the remainder
    # is the original value untouched.
    if out.startswith("'") and out != text:
        assert out[1:] == text, f"guard altered the value: {text!r} -> {out!r}"
        return
    assert first not in (
        "=",
        "+",
        "-",
        "@",
        "\t",
        "\r",
    ), f"live formula cell survived: {text!r} -> {out!r}"
    # Safe values pass through unchanged and output is a fixed point.
    if first not in ("=", "+", "-", "@", "\t", "\r"):
        assert out == text, f"safe value altered: {text!r} -> {out!r}"
    assert _csv_safe(out) == out, f"_csv_safe is not a fixed point: {out!r}"
    # The guarded output round-trips through a real CSV writer/reader
    # as a single field whose first character is not a trigger.
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([out])
    buf.seek(0)
    row = next(csv.reader(buf))
    assert row and row[0] == out, f"CSV round-trip changed the cell: {out!r}"


def _check_tree(value: object) -> None:
    """Assert the tree guard neutralizes every nested string."""
    out = _csv_safe_tree(value)
    if isinstance(out, dict):
        assert isinstance(value, dict), "tree guard changed dict type"
        assert set(out) == set(value), "tree guard changed keys"
        for key in out:
            _check_tree(out[key])
    elif isinstance(out, list):
        assert isinstance(value, list), "tree guard changed list type"
        assert len(out) == len(value), "tree guard changed list length"
        for item in out:
            _check_tree(item)
    elif isinstance(out, str):
        first = _spreadsheet_first(out)
        assert out.startswith("'") or first not in (
            "=",
            "+",
            "-",
            "@",
            "\t",
            "\r",
        ), f"nested formula survived: {out!r}"


def _check_display(text: str) -> None:
    """Assert display output cannot inject ANSI or rich markup."""
    stripped = _strip_controls(text)
    assert isinstance(stripped, str)
    for ch in stripped:
        code = ord(ch)
        assert (
            code == 0x09
            or code == 0x0A
            or not (code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F)
        ), f"control survived _strip_controls: {text!r} -> {stripped!r}"
    assert "\x1b" not in stripped, f"ESC survived: {text!r}"
    displayed = _display(text)
    assert isinstance(displayed, str)
    # Rich escapes every bracket that could open a style tag; a
    # bracket that cannot form a tag (``[3``) is left alone by design,
    # and rich renders it literally.  The caller's invariant is that
    # rendering the displayed string yields no markup: check that no
    # well-formed ``[tag]`` survives.
    from rich.markup import render as _render

    _rendered = _render(displayed)
    assert not _rendered.spans, f"rich markup survived _display: {text!r} -> {displayed!r}"
    for ch in displayed:
        code = ord(ch)
        assert (
            code == 0x09
            or code == 0x0A
            or not (code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F)
        ), f"control survived _display: {text!r} -> {displayed!r}"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    text = _build_text(fdp)
    _check_csv(text)
    _check_display(text)
    # Non-strings and nested shapes the find-batch payload carries.
    assert _csv_safe(None) is None
    assert _csv_safe(91.0) == 91.0
    assert _csv_safe(True) is True
    tree = {"query": text, "matches": [{"names": [text, "plain"], "score": 1.5}]}
    _check_tree(tree)
    # A value the guard accepted must not become live after a second
    # pass, and a guarded value stays guarded.
    assert _csv_safe(_csv_safe(text)) == _csv_safe(text)


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
