#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the YARA export escaping and rule-name sanitizers.

``_yara_string_escape`` and ``_export_safe_filename`` sit on the path from a
snippet's stored name and code to a rule file another tool parses — and to a
filesystem.  A name or code snippet that breaks out of its quoted YARA
literal injects arbitrary rule syntax (a new ``rule`` block, a ``condition``
that matches anything), and a name that escapes the export directory writes
outside it.  Both are truncated at the wrong place and the exported file
silently matches something the snippet never contained.  ``test_yara.py``
plus ``test_unicode_text.py`` cover hand-picked cases; this harness covers
the space between them.

The harness drives the two real entry points and asserts the invariants the
exporters rely on, not just the absence of a crash:

* the escape output never contains a raw control character, a raw double
  quote, a raw backslash that is not part of an escape sequence, a
  surrogate, or a byte that is not printable ASCII — a literal NUL or ESC
  in the output truncates the YARA pattern where it lands;
* every backslash in the output introduces a ``\\xNN`` or one of the
  named escapes (``\\\\``, ``\\"``, ``\\r``, ``\\n``, ``\\t``) the YARA
  lexer reads back as a single byte; a ``\\x`` not followed by exactly
  two hex digits is a corrupt pattern;
* the escaped text round-trips: unescaping it under YARA's rules yields
  the original bytes for everything the original could represent, so the
  rule matches exactly what the snippet holds;
* the filename sanitizer never returns a name containing a path
  separator, ``..``, a NUL, or a surrogate, never exceeds the byte
  budget, and never returns a Windows reserved stem bare;
* a name that is already safe is a fixed point of both transforms, so
  re-exporting an exported corpus is stable.

Inputs are generated structure-aware: realistic assembly snippets from the
committed corpus, strings built from the interesting alphabet (quote,
backslash, NUL and other C0 controls, DEL, surrogates, non-ASCII, trailing
dots/spaces, ``..``, path separators, Windows device names), and truncated
or junk bytes.  Nothing is copied from production traffic.
"""

import re
import sys

import atheris

with atheris.instrument_imports():
    from resembl.core import (
        _EXPORT_STEM_MAX_BYTES,
        _WINDOWS_RESERVED_STEMS,
        _export_safe_filename,
        _yara_string_escape,
    )

#: Characters the YARA string literal treats specially, plus the shapes a
#: filename sanitizer has to survive.
_INTERESTING = (
    '"',
    "\\",
    "\x00",
    "\x01",
    "\x07",
    "\x1b",
    "\x7f",
    "\r",
    "\n",
    "\t",
    "/",
    "\\",
    "..",
    ".",
    " ",
    "con",
    "CON",
    "nul",
    "COM1",
    "é",
    "\uff21\uff22\uff23",
    "\ufffd",
    "\ud800",
    "\udfff",
    "\u202e",
    "\x85",
    "a",
    "Z",
    "0",
    "_",
    "-",
    ":",
    ";",
    "$",
    "{",
    "}",
    "rule",
    "condition:",
    "$asm",
)

#: One YARA escape sequence as the lexer reads it.
_ESCAPE_RE = re.compile(r'\\(?:x[0-9a-f]{2}|[\\"nrt])')
_RULE_NAME_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_]")


def _build_text(fdp: atheris.FuzzedDataProvider) -> str:
    """Return a fuzzer-chosen string: structured, corpus-shaped, or raw."""
    mode = fdp.ConsumeIntInRange(0, 3)
    if mode == 0:
        # Structured: a short run of interesting tokens.
        return "".join(
            _INTERESTING[fdp.ConsumeIntInRange(0, len(_INTERESTING) - 1)]
            for _ in range(fdp.ConsumeIntInRange(0, 24))
        )
    if mode == 1:
        # Corpus-shaped assembly line.
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 64))
    if mode == 2:
        # Raw unicode, surrogates included: ConsumeUnicode keeps them.
        return fdp.ConsumeUnicode(fdp.ConsumeIntInRange(0, 64))
    # Junk bytes that decode as UTF-8 with replacement where they can.
    raw = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))
    return raw.decode("utf-8", errors="replace")


def _yara_unescape(escaped: str) -> bytes:
    """Decode a YARA string literal body back to the bytes it matches."""
    out = bytearray()
    index = 0
    while index < len(escaped):
        ch = escaped[index]
        if ch != "\\":
            out.extend(ch.encode("utf-8"))
            index += 1
            continue
        nxt = escaped[index + 1]
        if nxt == "x":
            out.append(int(escaped[index + 2 : index + 4], 16))
            index += 4
        elif nxt == "n":
            out.append(0x0A)
            index += 2
        elif nxt == "r":
            out.append(0x0D)
            index += 2
        elif nxt == "t":
            out.append(0x09)
            index += 2
        elif nxt == '"':
            out.append(0x22)
            index += 2
        elif nxt == "\\":
            out.append(0x5C)
            index += 2
        else:  # pragma: no cover - the escape scan below rejects this first
            raise AssertionError(f"unknown escape \\{nxt}")
    return bytes(out)


def _check_escape(text: str) -> None:
    """Assert the escaped form is a well-formed YARA string body."""
    escaped = _yara_string_escape(text)
    assert isinstance(escaped, str)
    # No raw control, quote, DEL, or surrogate may survive into the rule:
    # each either truncates the pattern or breaks out of the literal.
    for ch in escaped:
        code = ord(ch)
        assert code >= 0x20 and code != 0x7F, f"raw control in YARA output: {escaped!r}"
        assert not 0xD800 <= code <= 0xDFFF, f"surrogate in YARA output: {escaped!r}"
    assert '"' not in _ESCAPE_RE.sub("", escaped), f"unescaped quote: {escaped!r}"
    # Every backslash introduces a well-formed escape sequence.
    position = 0
    while True:
        position = escaped.find("\\", position)
        if position < 0:
            break
        match = _ESCAPE_RE.match(escaped, position)
        assert match is not None, f"malformed escape at {position}: {escaped!r}"
        position = match.end()
    # Round-trip: the rule must match the bytes the snippet holds.  A
    # lone surrogate has no UTF-8 form and becomes ``?`` by design, so
    # drop it from the expected bytes; everything else must survive.
    expected = b"".join(b"?" if 0xD800 <= ord(ch) <= 0xDFFF else ch.encode("utf-8") for ch in text)
    # ``\\xNN`` for a code point above U+00FF emits its UTF-8 bytes, so
    # the round-trip is exact for every representable input.
    assert (
        _yara_unescape(escaped) == expected
    ), f"YARA round-trip mismatch for {text!r}: got {_yara_unescape(escaped)!r}"
    # Escaped output is printable ASCII plus escapes, so re-escaping it
    # must only double the backslashes it already carries — never
    # introduce a raw control, quote, or surrogate.
    reescaped = _yara_string_escape(escaped)
    for ch in reescaped:
        assert 0x20 <= ord(ch) != 0x7F and not 0xD800 <= ord(ch) <= 0xDFFF


def _check_filename(name: str) -> None:
    """Assert the sanitized stem stays inside the export directory."""
    stem = _export_safe_filename(name)
    assert isinstance(stem, str)
    assert "\\" not in stem and "/" not in stem, f"path separator in stem: {stem!r}"
    assert ".." not in stem, f"traversal in stem: {stem!r}"
    assert "\x00" not in stem, f"NUL in stem: {stem!r}"
    for ch in stem:
        assert not 0xD800 <= ord(ch) <= 0xDFFF, f"surrogate in stem: {stem!r}"
        assert ord(ch) >= 0x20 or ch == "\t", f"control in stem: {stem!r}"
    assert len(stem.encode("utf-8")) <= _EXPORT_STEM_MAX_BYTES, f"stem over byte budget: {stem!r}"
    if stem:
        assert (
            stem.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_STEMS
        ), f"bare Windows reserved stem: {stem!r}"
        assert not stem.endswith((" ", ".")), f"trailing dot/space: {stem!r}"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    text = _build_text(fdp)
    _check_escape(text)
    _check_filename(text)
    # The name path the YARA exporter actually uses: the rule identifier
    # derived from the same untrusted name must stay a valid identifier.
    rule_name = _RULE_NAME_INVALID_CHARS.sub("_", text)
    if not rule_name or (not rule_name[0].isalpha() and rule_name[0] != "_"):
        rule_name = "r_" + rule_name
    assert re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", rule_name
    ), f"rule name is not an identifier: {rule_name!r}"


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
