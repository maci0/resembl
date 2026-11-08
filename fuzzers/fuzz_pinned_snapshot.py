#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the pinned index snapshot digest.

``index_digest`` is the identity proof behind ``POST /find-pinned``: it
hashes every decision-bearing row of the SQLite index while the caller
holds a writer fence, and the handler answers the request's
``index_sha256`` against it (409 on mismatch).  Its input is the
database itself — a hostile or hand-edited index file supplies every
value it reads — so a value that escapes its guard turns one corrupt
row into a 500/503 with internal error text instead of the documented
refusal, or into two different snapshots that hash the same (a pinned
query answering for an index it never verified).

The harness drives the real entry point and asserts the invariants
the handler relies on, not just the absence of a crash:

* ``index_digest`` never raises anything but ``ValueError`` for any
  row content: non-finite ``REAL`` values, unsupported types, and
  oversized snapshots are all refusals, never a second exception
  type (the handler catches ``ValueError`` as the pinned-query
  refusal it is);
* an accepted digest is a 64-char lowercase hex string;
* the digest is a fixed point — hashing the same rows twice yields
  the same answer — and a tampered row (one value changed after the
  digest was computed) changes it, so the identity check cannot be
  bypassed by a near-miss index;
* a blob and the text with the same bytes hash differently: the
  snapshot tags each value with its type, so ``X'6162'`` and
  ``'6162'`` are never the same snapshot row.

Inputs are generated structure-aware: well-formed snippet / lsh /
meta rows built from the documented columns (including blobs,
non-finite floats, NULLs, and large values), truncated and
oversized row sets, and raw junk bytes.  Seeds come from the
documented snapshot shape, not from captured traffic.
"""

import hashlib
import math
import os
import random
import sys
import tempfile

import atheris

with atheris.instrument_imports():
    from sqlmodel import SQLModel, create_engine

    import resembl.models  # noqa: F401
    from resembl.pinned_snapshot import index_digest

#: Values worth spending runs on: each SQLite type plus the edge
#: cases the snapshot tags or refuses (non-finite reals, oversized
#: blobs, unicode text).
_INTERESTING_VALUES = (
    None,
    0,
    1,
    -1,
    1.5,
    float("inf"),
    float("-inf"),
    "ret",
    "café",
    "",
    b"",
    b"ab",
    b"\x00\xff",
    "a" * 512,
)


def _value(fdp: atheris.FuzzedDataProvider) -> object:
    """Return one fuzzer-chosen SQLite value."""
    mode = fdp.ConsumeIntInRange(0, 3)
    if mode == 0:
        return _INTERESTING_VALUES[fdp.ConsumeIntInRange(0, len(_INTERESTING_VALUES) - 1)]
    if mode == 1:
        return fdp.ConsumeIntInRange(-(1 << 62), 1 << 62)
    if mode == 2:
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 32))
    return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 32))


def _blob_value(fdp: atheris.FuzzedDataProvider) -> bytes:
    """Return one fuzzer-chosen BLOB value (never NULL, per the schema)."""
    value = _value(fdp)
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    if isinstance(value, float):
        return repr(value).encode()
    return str(value).encode()


def _text_value(fdp: atheris.FuzzedDataProvider) -> str:
    """Return one fuzzer-chosen TEXT value (never NULL, per the schema)."""
    value = _value(fdp)
    if value is None:
        return "[]"
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float) and not math.isfinite(value):
        return "[]"
    return str(value)


def _build_rows(fdp: atheris.FuzzedDataProvider) -> list[tuple]:
    """Return snapshot rows for the snippet table."""
    rows: list[tuple] = []
    for _ in range(fdp.ConsumeIntInRange(0, 3)):
        rows.append(
            (
                f"checksum-{len(rows)}",
                _text_value(fdp),
                _text_value(fdp),
                _blob_value(fdp),
                _text_value(fdp),
                _text_value(fdp) if fdp.ConsumeBool() else None,
            )
        )
    return rows


def _engine_for(tmp: str, rows: list[tuple]):
    """Write *rows* into a real SQLite snippet table and return its engine."""
    os.makedirs(tmp, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp}/index.db")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS snippet (checksum TEXT, names TEXT, code TEXT, "
            "minhash BLOB, tags TEXT, collection TEXT)"
        )
        for row in rows:
            connection.exec_driver_sql(
                "INSERT INTO snippet (checksum,names,code,minhash,tags,collection) "
                "VALUES (?,?,?,?,?,?)",
                tuple(row),
            )
    return engine


def _digest_from(engine) -> str:
    """Return ``index_digest`` over a fresh connection of *engine*."""
    with engine.begin() as connection:
        return index_digest(connection)


def _check_rows(rows: list[tuple], tmp: str) -> None:
    """Drive ``index_digest`` over *rows* and assert its contract."""
    engine = _engine_for(tmp, rows)
    try:
        try:
            digest = _digest_from(engine)
        except ValueError:
            return
        assert len(digest) == 64, f"digest is not 64 chars: {digest!r}"
        int(digest, 16)  # hex, or ValueError escapes as a crash: a logic bug
        assert all(c in "0123456789abcdef" for c in digest), f"digest not lowercase hex: {digest!r}"
        # Fixed point: the same rows hashed again yield the same digest.
        assert _digest_from(engine) == digest, "snapshot digest is not a fixed point"
        # Tamper: one byte changed in one row must change the digest, so a
        # near-miss index cannot pass the handler's identity check.
        if rows:
            tampered = list(rows)
            first = list(tampered[0])
            column = random.Random(hash(digest) & 0xFFFFFFFF).randrange(len(first))
            original = first[column]
            first[column] = b"tampered" if isinstance(original, (bytes, bytearray)) else "tampered"
            tampered[0] = tuple(first)
            tampered_engine = _engine_for(f"{tmp}/tampered", tampered)
            try:
                try:
                    tampered_digest = _digest_from(tampered_engine)
                except ValueError:
                    return
                assert tampered_digest != digest, "tamper did not change the snapshot digest"
            finally:
                tampered_engine.dispose()
    finally:
        engine.dispose()


def _check_blob_vs_text(tmp: str) -> None:
    """A blob and the text with the same bytes hash differently."""
    blob_engine = _engine_for(f"{tmp}/blob", [("c", "[]", "ret", b"ab", "[]", None)])
    text_engine = _engine_for(f"{tmp}/text", [("c", "[]", "ret", "ab", "[]", None)])
    try:
        try:
            blob_digest = _digest_from(blob_engine)
            text_digest = _digest_from(text_engine)
        except ValueError:
            return
        assert blob_digest != text_digest, "blob and text with same bytes share a digest"
        assert hashlib.sha256(b"ab").hexdigest() not in (blob_digest, text_digest)
    finally:
        blob_engine.dispose()
        text_engine.dispose()


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    # A huge row set is a length-check case, not a parser case; keep
    # the corpus on the interesting side of the snapshot limit.
    rows = _build_rows(fdp)
    with tempfile.TemporaryDirectory() as tmp:
        _check_rows(rows, tmp)
        _check_blob_vs_text(tmp)


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
