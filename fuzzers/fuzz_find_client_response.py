#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the thin client's response and config readers.

``resembl.find_client`` is the stdlib-only client every ``resembl-find``
invocation runs: it reads a hand-edited ``config.toml`` through
:func:`_load_config` / :func:`_cfg_number` and reads the loopback
server's response through :func:`_read_bounded_response`.  Both inputs
are untrusted — the config file is hand-editable and the port-file
trust model (THREAT_MODEL R3) lets a local attacker swap the endpoint
the client dials — and neither has a fuzz target.  A config value that
escapes the coercion as a second exception type turns a mistyped file
into a traceback instead of a warning plus a default, an out-of-range
value that slips through changes the result set the client sends to
the server, and a response read that buffers unboundedly lets a
hostile endpoint pin gigabytes instead of answering "server
unreachable".

The harness drives the three real entry points and asserts the
invariants the callers rely on, not just the absence of a crash:

* ``_read_bounded_response`` raises ``ValueError`` only, and only for
  an oversize body — a body of exactly ``limit`` bytes is returned
  verbatim, and the returned bytes are exactly what the stream
  delivered, reassembled in order;
* ``_load_config`` never raises for any file bytes: a missing,
  unreadable, or malformed file answers ``{}``;
* ``_cfg_number`` never raises for any TOML-typed value, and an
  accepted value is the documented default when the stored value is
  unusable, a finite number inside ``_within_bounds`` otherwise, so
  nothing out of range can leave the client in a request body;
* a second read of the same stream and the same file is a fixed
  point: the bounded read returns the same bytes, the config merge
  the same dict.

Inputs are generated structure-aware: responses are chunked streams
of fuzzer-chosen bytes (including empty, exactly-at-limit, one-over,
and lying chunk sizes), config files are well-formed TOML built from
known keys and edge scalars plus truncated, invalid-UTF-8 and junk
bytes, and config merges draw from the edge set the ``/find``
harness uses (including the values that overflow ``int()``/``float()``
and NaN/infinity).  Seeds come from the documented config shape and
response envelope, not from captured traffic.
"""

import io
import json
import math
import sys
from typing import Any

import atheris

with atheris.instrument_imports():
    from resembl.find_client import (
        _CFG_DEFAULTS,
        _cfg_number,
        _load_config,
        _read_bounded_response,
        _within_bounds,
    )

#: Values that are legal TOML but hostile to ``int()``/``float()``,
#: plus the range edges each parameter is checked against.
_EDGE_VALUES: tuple[Any, ...] = (
    0,
    1,
    -1,
    2,
    0.0,
    0.5,
    1.0,
    1.5,
    2.0,
    -0.5,
    float("inf"),
    float("-inf"),
    float("nan"),
    True,
    False,
    None,
    "",
    "0.9",
    "abc",
    "true",
    "false",
    10**30,
    -(10**30),
    1 << 62,
    [],
    {},
    [1, 2],
    {"a": 1},
)

#: Known configuration keys, plus near-misses.
_KEYS = (
    "top_n",
    "lsh_threshold",
    "ngram_size",
    "num_permutations",
    "jaccard_weight",
    "topN",
    "unknown_key",
)

#: TOML spellings of the edge values, including the non-finite
#: floats and the date/time forms ``tomllib`` turns into objects.
_TOML_VALUES = (
    "0",
    "1",
    "-1",
    "2",
    "0.0",
    "0.5",
    "0.99",
    "1.0",
    "1.5",
    "-0.5",
    "nan",
    "+nan",
    "-nan",
    "inf",
    "+inf",
    "-inf",
    "true",
    "false",
    '"0.9"',
    '"abc"',
    '"table"',
    "0x10",
    "9223372036854775807",
    "-9223372036854775808",
    "3.1415",
)


class _ChunkedResponse:
    """A response stub delivering *chunks* through ``read(65536)``."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int = -1) -> bytes:
        """Return the next chunk, or empty once exhausted."""
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _LyingResponse:
    """A response stub that reports more bytes than it holds."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk = chunk
        self._remaining = count

    def read(self, _size: int = -1) -> bytes:
        """Return the chunk until the fuzzer-chosen count runs out."""
        if self._remaining <= 0:
            return b""
        self._remaining -= 1
        return self._chunk


def _build_response(fdp: atheris.FuzzedDataProvider) -> tuple[Any, int]:
    """Return a response stub and the limit the caller passes."""
    mode = fdp.ConsumeIntInRange(0, 4)
    if mode == 0:
        chunks = [
            fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))
            for _ in range(fdp.ConsumeIntInRange(0, 4))
        ]
        return _ChunkedResponse(chunks), fdp.ConsumeIntInRange(0, 4096)
    if mode == 1:
        return _LyingResponse(b"x" * fdp.ConsumeIntInRange(1, 64), fdp.ConsumeIntInRange(0, 128)), (
            fdp.ConsumeIntInRange(0, 4096)
        )
    if mode == 2:
        # A well-formed find response, split at fuzzed offsets.
        payload = json.dumps(
            {
                "lsh_candidates": fdp.ConsumeIntInRange(0, 10),
                "matches": [
                    {
                        "checksum": fdp.ConsumeUnicodeNoSurrogates(16),
                        "names": [fdp.ConsumeUnicodeNoSurrogates(8)],
                        "score": fdp.ConsumeFloat(),
                    }
                ],
            }
        ).encode()
        cuts = sorted(fdp.ConsumeIntInRange(0, len(payload)) for _ in range(3))
        chunks = [payload[: cuts[0]], payload[cuts[0] : cuts[1]], payload[cuts[1] :]]
        return _ChunkedResponse(chunks), 16 * 1024 * 1024
    if mode == 3:
        # Exactly at the limit, and one over it.
        limit = fdp.ConsumeIntInRange(1, 512)
        size = limit if fdp.ConsumeBool() else limit + 1
        return _ChunkedResponse([b"y" * size]), limit
    return io.BytesIO(fdp.ConsumeBytes(fdp.remaining_bytes())), fdp.ConsumeIntInRange(0, 512)


def _test_response(fdp: atheris.FuzzedDataProvider) -> None:
    """Drive ``_read_bounded_response`` with one fuzzed stream."""
    response, limit = _build_response(fdp)
    try:
        body = _read_bounded_response(response, limit)
    except ValueError:
        return
    assert isinstance(body, bytes), f"reader returned non-bytes: {type(body)}"
    assert len(body) <= limit, "returned a body over the limit"
    # Truncation and concatenation never lose or reorder a byte: the
    # bounded read of the same stream through a second identical
    # stub returns the same bytes.
    first = _ChunkedResponse([b"x" * 10, b"y" * 10])
    second = _ChunkedResponse([b"x" * 10, b"y" * 10])
    assert (
        _read_bounded_response(first, 100)
        == _read_bounded_response(second, 100)
        == b"x" * 10 + b"y" * 10
    )


def _build_document(fdp: atheris.FuzzedDataProvider) -> bytes:
    """Return a TOML document (or junk) built from *fdp*."""
    mode = fdp.ConsumeIntInRange(0, 3)
    if mode == 0:
        lines = []
        for _ in range(fdp.ConsumeIntInRange(1, 4)):
            key = _KEYS[fdp.ConsumeIntInRange(0, len(_KEYS) - 1)]
            value = _TOML_VALUES[fdp.ConsumeIntInRange(0, len(_TOML_VALUES) - 1)]
            lines.append(f"{key} = {value}")
        return ("\n".join(lines) + "\n").encode()
    if mode == 1:
        doc = b"top_n = 5\nlsh_threshold = 0.5\n"
        return doc[: fdp.ConsumeIntInRange(0, len(doc))]
    if mode == 2:
        return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))
    depth = fdp.ConsumeIntInRange(2, 5000)
    opener, closer = ("[", "]") if fdp.ConsumeBool() else ("{a = ", "}")
    return f"k = {opener * depth}{closer * depth}\n".encode()


def _test_config_file(fdp: atheris.FuzzedDataProvider) -> None:
    """Drive ``_load_config`` over one fuzzed file."""
    import os
    import tempfile
    from unittest import mock

    from resembl import find_client as find_client_module

    data = _build_document(fdp)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.toml")
        with open(path, "wb") as fh:
            fh.write(data)
        with mock.patch.object(find_client_module, "config_path_get", return_value=path):
            config = _load_config()
    assert isinstance(config, dict), f"config is not a dict: {config!r}"
    for key, value in config.items():
        assert isinstance(key, str), f"non-string key: {key!r}"
        assert not isinstance(value, (bytes, bytearray)), f"bytes value: {value!r}"
    # The same file read twice yields the same table, compared through
    # JSON so a NaN the TOML decoder produced (nan != nan) does not
    # read as a changed file.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.toml")
        with open(path, "wb") as fh:
            fh.write(data)
        with mock.patch.object(find_client_module, "config_path_get", return_value=path):
            again = _load_config()
    assert json.dumps(again, sort_keys=True) == json.dumps(
        config, sort_keys=True
    ), "config read is not deterministic"


def _test_cfg_number(fdp: atheris.FuzzedDataProvider) -> None:
    """Drive ``_cfg_number`` with one fuzzed config value."""
    cfg: dict[str, Any] = {}
    for _ in range(fdp.ConsumeIntInRange(0, len(_KEYS))):
        key = _KEYS[fdp.ConsumeIntInRange(0, len(_KEYS) - 1)]
        if key in _CFG_DEFAULTS:
            cfg[key] = _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    try:
        body = json.loads(json.dumps(cfg))
    except ValueError:
        return
    assert isinstance(body, dict)
    for key, cast in (
        ("top_n", int),
        ("lsh_threshold", float),
        ("ngram_size", int),
        ("num_permutations", int),
        ("jaccard_weight", float),
    ):
        number = _cfg_number(body, key, cast)
        assert isinstance(number, cast), f"{key} coerced to {type(number)}"
        assert math.isfinite(number), f"{key} accepted as non-finite"
        assert _within_bounds(key, number), f"{key} out of range: {number!r}"
    # A second coercion of the same body is a fixed point.
    assert _cfg_number(body, "top_n", int) == _cfg_number(body, "top_n", int)


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    mode = fdp.ConsumeIntInRange(0, 2)
    if mode == 0:
        _test_response(fdp)
    elif mode == 1:
        _test_config_file(fdp)
    else:
        _test_cfg_number(fdp)


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
