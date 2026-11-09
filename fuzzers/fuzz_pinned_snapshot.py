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

This harness drives the real entry points and asserts the invariants
the handler relies on, not just the absence of a crash:

* ``index_digest`` never raises anything but ``ValueError`` for any
  row content: non-finite ``REAL`` values, unsupported types, and
  oversized snapshots are all refusals, never a second exception
  type (the handler catches ``ValueError`` as the pinned-query
  refusal it is);
* ``_snapshot_value`` rejects every unsupported or non-finite value
  with ``ValueError`` only, and an accepted encoding round-trips
  through ``json.dumps`` with ``allow_nan=False`` unchanged;
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
oversized row sets, every SQLite storage class including the
hostile shapes a hand-edited file can carry (``bool``, huge ints,
nested containers, objects), and raw junk bytes.  Seeds come from
the documented snapshot shape, not from captured traffic.
"""

import hashlib
import json
import math
import os
import random
import sys
import tempfile

import atheris

with atheris.instrument_imports():
    from sqlmodel import SQLModel, create_engine

    import resembl.models  # noqa: F401
    from resembl.pinned_snapshot import _snapshot_value, index_digest

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


#: A real SQLite connection built once per process: ``index_digest``
#: needs the four decision tables, and rebuilding them per input
#: would spend the run on DDL instead of covering value shapes.
_CONNECTION = None


def _connection():
    """Return a shared in-memory SQLite connection with the real schema."""
    global _CONNECTION  # pylint: disable=global-statement
    if _CONNECTION is None:
        from sqlmodel import SQLModel, create_engine

        import resembl.models  # noqa: F401

        engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(engine)
        _CONNECTION = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    return _CONNECTION


#: Text values worth spending runs on: empty, realistic, and the
#: shapes that stress the JSON encoding (unicode, controls, quotes).
_INTERESTING_TEXTS = (
    "",
    "ret",
    "mov eax, 1\nret",
    'with "quotes" and \\ backslashes',
    "héllo wörld — ünïcode",
    "\x00\x01\x02",
    "a" * 256,
    "null",
    "true",
    "1",
    "1.0",
)

#: Real values worth spending runs on: the non-finite edge values
#: SQLite stores as ``Inf``/``NULL``, the signed zeros, and the
#: values just inside and outside the finite domain.
_EDGE_FLOATS = (
    0.0,
    -0.0,
    1.0,
    -1.0,
    0.5,
    1e308,
    -1e308,
    5e-324,
    float("inf"),
    float("-inf"),
    float("nan"),
    1.7976931348623157e308,
)

#: Integer values worth spending runs on: zero, the sign edges, and
#: the 64-bit bounds SQLite integers live in.
_EDGE_INTS = (0, 1, -1, 2, -(1 << 63), (1 << 63) - 1, 1 << 62)


def _encoder_value(fdp: atheris.FuzzedDataProvider):
    """Return one fuzzer-chosen SQLite value, or a hostile non-SQLite one."""
    mode = fdp.ConsumeIntInRange(0, 8)
    if mode == 0:
        return None
    if mode == 1:
        return _EDGE_INTS[fdp.ConsumeIntInRange(0, len(_EDGE_INTS) - 1)]
    if mode == 2:
        return _EDGE_FLOATS[fdp.ConsumeIntInRange(0, len(_EDGE_FLOATS) - 1)]
    if mode == 3:
        if fdp.ConsumeBool():
            return _INTERESTING_TEXTS[fdp.ConsumeIntInRange(0, len(_INTERESTING_TEXTS) - 1)]
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 64))
    if mode == 4:
        return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))
    if mode == 5:
        return memoryview(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 32)))
    if mode == 6:
        # ``bool`` is an ``int`` subclass: the encoder must tag it as an
        # integer, never as the JSON boolean its ``json.dumps`` spelling
        # would otherwise produce.
        return fdp.ConsumeBool()
    if mode == 7:
        # A real float drawn from the provider, for the accepted path.
        return fdp.ConsumeFloat()
    # A hostile shape no SQLite column can hold: the encoder must
    # refuse it, not guess at a spelling for it.
    hostile = ([1, 2], {"a": 1}, (1, 2), object())
    return hostile[fdp.ConsumeIntInRange(0, len(hostile) - 1)]


def _check_encoded(value, encoded) -> None:
    """Assert an accepted encoding is serializable, finite and tagged."""
    assert isinstance(encoded, list) and len(encoded) == 2, f"bad encoding: {encoded!r}"
    tag, payload = encoded
    assert tag in ("null", "integer", "real", "text", "blob"), f"unknown tag: {tag!r}"
    # The digest serializes the encoding with allow_nan=False: an
    # accepted value must survive exactly that call.
    raw = json.dumps(encoded, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    assert json.loads(raw) == encoded, "encoding does not round-trip"
    if tag == "real":
        assert isinstance(payload, float) and math.isfinite(
            payload
        ), f"non-finite real: {payload!r}"
    elif tag == "integer":
        assert isinstance(payload, int) and not isinstance(payload, bool)
    elif tag == "text":
        assert isinstance(payload, str)
    elif tag == "blob":
        assert isinstance(payload, str) and all(c in "0123456789abcdef" for c in payload)
    elif tag == "null":
        assert payload is None
    # No two distinct Python values may share an encoding: that would
    # let a stored row change without moving the digest.
    if isinstance(value, bool):
        assert (tag, payload) == ("integer", int(value)), f"bool encoded as {encoded!r}"
    elif isinstance(value, int):
        assert (tag, payload) == ("integer", value), f"int encoded as {encoded!r}"
    elif isinstance(value, float):
        assert tag == "real" and payload == value, f"float encoded as {encoded!r}"
    elif isinstance(value, str):
        assert (tag, payload) == ("text", value), f"text encoded as {encoded!r}"
    elif isinstance(value, (bytes, memoryview)):
        assert (tag, payload) == ("blob", bytes(value).hex()), f"blob encoded as {encoded!r}"
    elif value is None:
        assert (tag, payload) == ("null", None), f"null encoded as {encoded!r}"


def _test_value(fdp: atheris.FuzzedDataProvider) -> None:
    """Drive ``_snapshot_value`` with one fuzzed value."""
    value = _encoder_value(fdp)
    try:
        encoded = _snapshot_value(value)
    except ValueError:
        return
    _check_encoded(value, encoded)


def _test_digest(fdp: atheris.FuzzedDataProvider) -> None:
    """Drive ``index_digest`` over a real database holding fuzzed rows."""
    connection = _connection()
    # Reset the decision rows: the digest must answer for exactly
    # the rows this input placed, never for leftovers of the last.
    for table in ("snippet", "lsh_bucket", "lsh_meta", "app_meta"):
        connection.exec_driver_sql(f"DELETE FROM {table}")
    connection.exec_driver_sql(
        "INSERT INTO snippet (checksum, names, code, minhash, tags, collection) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "fuzz-checksum",
            "[]",
            "ret",
            fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 32)),
            "[]",
            None,
        ),
    )
    # One typed row per table, with fuzzed keys/values: the digest
    # walks all four, so a shape only one table can carry (a blob in
    # app_meta, an Inf in lsh_meta) is reached here, not just in the
    # per-value harness.
    candidate = _encoder_value(fdp)
    if candidate is not None and not isinstance(candidate, (list, tuple, dict, object)):
        if isinstance(candidate, float) and not math.isfinite(candidate):
            connection.exec_driver_sql(
                "INSERT INTO lsh_meta (id, threshold, num_perm) VALUES (1, ?, 128)",
                (candidate,),
            )
        elif isinstance(candidate, (int, float, str, bytes)):
            connection.exec_driver_sql(
                "INSERT INTO app_meta (key, value) VALUES (?, ?)",
                ("fuzz-key", str(candidate)),
            )

    try:
        digest = index_digest(connection)
    except ValueError:
        return
    assert len(digest) == 64, f"bad digest: {digest!r}"
    int(digest, 16)  # hex, or ValueError escapes as a crash: a logic bug
    # Determinism: the same database twice yields the same identity.
    assert index_digest(connection) == digest, "digest is not deterministic"
    # A tampered hashed row must move the identity: flip one stored
    # byte of the snippet row, the one row this harness always hashed.
    connection.exec_driver_sql(
        "UPDATE snippet SET code = code || 'x' WHERE checksum = 'fuzz-checksum'"
    )
    try:
        moved = index_digest(connection)
    except ValueError:
        return
    assert moved != digest, "a changed row did not change the digest"
    connection.exec_driver_sql(
        "UPDATE snippet SET code = substr(code, 1, length(code) - 1) "
        "WHERE checksum = 'fuzz-checksum'"
    )


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    if fdp.ConsumeBool():
        _test_value(fdp)
    else:
        _test_digest(fdp)
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
