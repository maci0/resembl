#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the ISO 8601 instant parser and timestamp normalizer.

``parse_instant`` gates every ``created_at`` value: timestamps are
stored as aware-UTC ISO 8601 strings and ordered by string
comparison (``SnippetVersion.get_by_checksum`` relies on it), so a
value that parses but normalizes to a different ordering corrupts
the ordering silently, and a value that should parse but does not
gets left verbatim and sorts where it was written.  Imported
timestamps come from foreign databases — a merge source writes them
verbatim — so they are untrusted input.

The harness drives the real entry points and asserts the invariants
the ordering relies on, not just the absence of a crash:

* ``parse_instant`` never raises for any string: a malformed
  instant answers ``None`` and is left verbatim by
  ``timestamp_normalize``;
* a parsed instant normalizes to an aware-UTC ISO string whose
  parsed form equals the original moment, so normalization never
  fabricates or drops an offset;
* normalized values sort chronologically: for two values
  ``a <= b`` chronologically, ``normalize(a) <= normalize(b)`` as
  strings — the property the stored ordering depends on;
* a value ``parse_instant`` rejects is returned verbatim, never
  rewritten (a merge never rewrites metadata it cannot read).

Inputs are generated structure-aware: well-formed instants built
from the documented shape (extended format, each offset spelling,
fractional seconds, ``z``/``Z``), near-misses the gate must refuse
(date-only, week-date, basic-format, minute-precision, ``-00:00``),
and raw junk strings.  Seeds come from the documented timestamp
shape, not from captured traffic.
"""

import sys
from datetime import UTC

import atheris

with atheris.instrument_imports():
    from resembl.models import parse_instant, timestamp_normalize

#: Offset spellings worth spending runs on: each documented form,
#: plus the ``-00:00`` "unknown offset" refusal shape.
_OFFSETS = ("+00:00", "-00:00", "-0000", "-00", "+05:30", "-08:00", "Z", "z", "")

#: Instant shapes the gate must refuse: none names a full instant.
_NON_INSTANTS = (
    "2024-06-01",
    "2024-W01-1",
    "20240601T100000",
    "2024-06-01T10:00",
    "2024-06-01 10:00:00-00:00",
    "2024-06-01T10:00:00-00:00",
    "not a date",
    "",
    "   ",
    "2024-13-45T99:99:99",
)


def _build_instant(fdp: atheris.FuzzedDataProvider) -> str:
    """Return one instant (or near-miss) built from *fdp*."""
    mode = fdp.ConsumeIntInRange(0, 3)
    if mode == 0:
        year = fdp.ConsumeIntInRange(1, 9999)
        month = fdp.ConsumeIntInRange(1, 12)
        day = fdp.ConsumeIntInRange(1, 28)
        hour = fdp.ConsumeIntInRange(0, 23)
        minute = fdp.ConsumeIntInRange(0, 59)
        second = fdp.ConsumeIntInRange(0, 59)
        base = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
        if fdp.ConsumeBool():
            base += f".{fdp.ConsumeIntInRange(0, 999999):06d}"
        return base + _OFFSETS[fdp.ConsumeIntInRange(0, len(_OFFSETS) - 1)]
    if mode == 1:
        return _NON_INSTANTS[fdp.ConsumeIntInRange(0, len(_NON_INSTANTS) - 1)]
    if mode == 2:
        # A well-formed instant with one field bent: an out-of-range
        # month/day/hour the gate must refuse without raising.
        return (
            f"{fdp.ConsumeIntInRange(0, 9999):04d}-{fdp.ConsumeIntInRange(0, 99):02d}-"
            f"{fdp.ConsumeIntInRange(0, 99):02d}T{fdp.ConsumeIntInRange(0, 99):02d}:"
            f"{fdp.ConsumeIntInRange(0, 99):02d}:{fdp.ConsumeIntInRange(0, 99):02d}"
        )
    return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 32))


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    first_raw = _build_instant(fdp)
    second_raw = _build_instant(fdp)

    for raw in (first_raw, second_raw):
        parsed = parse_instant(raw)
        normalized = timestamp_normalize(raw)
        if parsed is None:
            # A rejected value is returned verbatim, never rewritten.
            assert normalized == raw, f"rejected value rewritten: {raw!r} -> {normalized!r}"
            continue
        assert isinstance(normalized, str), f"normalized non-string: {normalized!r}"
        reparsed = parse_instant(normalized)
        assert reparsed is not None, f"normalized value does not reparse: {normalized!r}"
        # Normalization preserves the moment: the stored value names
        # the same instant, in UTC (a naive parsed value is read as
        # UTC exactly as timestamp_normalize reads it).
        parsed_dt = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        assert reparsed.astimezone(UTC) == parsed_dt.astimezone(
            UTC
        ), f"normalization changed the instant: {raw!r} -> {normalized!r}"

    first = parse_instant(first_raw)
    second = parse_instant(second_raw)
    if first is not None and second is not None:
        first_norm = timestamp_normalize(first_raw)
        second_norm = timestamp_normalize(second_raw)
        assert isinstance(first_norm, str) and isinstance(second_norm, str)
        # String order of the stored form must match chronological
        # order — the property SnippetVersion.get_by_checksum relies on.
        first_dt = first if first.tzinfo is not None else first.replace(tzinfo=UTC)
        second_dt = second if second.tzinfo is not None else second.replace(tzinfo=UTC)
        if first_dt <= second_dt:
            assert first_norm <= second_norm, (
                f"stored order disagrees with chronological order: "
                f"{first_raw!r} -> {first_norm!r}, {second_raw!r} -> {second_norm!r}"
            )


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
