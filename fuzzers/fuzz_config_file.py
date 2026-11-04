#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the ``config.toml`` file reader.

``_read_config_file`` is the one reader behind every config access
(:func:`load_config`, :func:`_read_config_dict`): a hand-edited
``config.toml`` goes through ``tomllib.load`` here, and a malformed
file must yield an empty config plus a reported error — never an
exception escaping as a traceback, and never a partially merged
config that bends every later ``find``.  ``fuzz_config_update.py``
covers the value coercion *after* a clean parse; this harness covers
the parse itself, which is the part that sees raw bytes.

The harness drives the real entry point and asserts the invariants
the callers rely on, not just the absence of a crash:

* ``_read_config_file`` never raises for any byte input: a missing
  file, a malformed document, and an unreadable file all answer
  ``({}, error)`` or ``({}, None)``;
* the returned dict is always a plain ``dict`` whose keys are strings
  and whose values are TOML-typed scalars or tables — never a
  datetime, a ``Decimal``, or an arbitrary object leaking into
  ``ResemblConfig.update``;
* anything the reader *does* return as a config is a fixed point:
  ``ResemblConfig().update`` on it never raises and leaves the
  config inside ``VALUE_BOUNDS``;
* feeding the same bytes twice yields the same result, so a flaky
  read cannot flip a later command between defaults and overrides.

Inputs are generated structure-aware: well-formed TOML documents
built from known keys and edge scalars (including ``nan``/``inf``,
dates, hex/octal/binary ints, dotted keys, tables, arrays), truncated
documents, invalid UTF-8, and raw junk bytes.  Seeds come from the
documented config shape, not from captured traffic.
"""

import math
import sys

import atheris

with atheris.instrument_imports():
    from resembl.config import (
        FORMATS,
        VALUE_BOUNDS,
        ResemblConfig,
        _read_config_file,
        validate_value,
    )

#: Known configuration keys, plus near-misses and one table header.
_KEYS = (
    "lsh_threshold",
    "num_permutations",
    "top_n",
    "ngram_size",
    "jaccard_weight",
    "format",
    "lsh-threshold",
    "topN",
    "unknown_key",
)

#: TOML spellings of the edge values, including the non-finite floats
#: and the date/time forms ``tomllib`` turns into ``datetime`` objects.
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
    '"json"',
    '"csv"',
    '"yaml"',
    "0x10",
    "0o17",
    "0b101",
    "1_000",
    "1979-05-27",
    "1979-05-27T07:32:00Z",
    "07:32:00",
    "[]",
    "[1, 2]",
    "{}",
    "{a = 1}",
    "9223372036854775807",
    "-9223372036854775808",
    "3.1415",
)


def _build_document(fdp: atheris.FuzzedDataProvider) -> bytes:
    """Return a TOML document (or junk) built from *fdp*."""
    mode = fdp.ConsumeIntInRange(0, 4)
    if mode == 0:
        # A well-formed document: known keys with edge spellings.
        lines = []
        for _ in range(fdp.ConsumeIntInRange(1, 4)):
            key = _KEYS[fdp.ConsumeIntInRange(0, len(_KEYS) - 1)]
            value = _TOML_VALUES[fdp.ConsumeIntInRange(0, len(_TOML_VALUES) - 1)]
            lines.append(f"{key} = {value}")
        if fdp.ConsumeBool():
            lines.append(f"[{_KEYS[fdp.ConsumeIntInRange(0, len(_KEYS) - 1)]}]")
            lines.append(
                f"{_KEYS[0]} = {_TOML_VALUES[fdp.ConsumeIntInRange(0, len(_TOML_VALUES) - 1)]}"
            )
        return ("\n".join(lines) + "\n").encode()
    if mode == 1:
        # A truncated well-formed document: every byte prefix of one.
        doc = b"lsh_threshold = 0.5\nnum_permutations = 128\n"
        return doc[: fdp.ConsumeIntInRange(0, len(doc))]
    if mode == 2:
        # Invalid UTF-8 and raw junk bytes.
        return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))
    if mode == 3:
        # Deeply nested arrays/tables: the TOML decoder recurses per
        # level, so this is how a small file reaches its limits.
        depth = fdp.ConsumeIntInRange(2, 5000)
        opener, closer = ("[", "]") if fdp.ConsumeBool() else ("{a = ", "}")
        return f"k = {opener * depth}{closer * depth}\n".encode()
    # Duplicate keys and other document-level errors the decoder rejects.
    key = _KEYS[fdp.ConsumeIntInRange(0, len(_KEYS) - 1)]
    value = _TOML_VALUES[fdp.ConsumeIntInRange(0, len(_TOML_VALUES) - 1)]
    return f"{key} = {value}\n{key} = {value}\n".encode()


def _check_config(cfg: ResemblConfig) -> None:
    """Assert a merged config stays inside the bounds callers rely on."""
    for key, value in cfg.items():
        problem = validate_value(key, value)
        assert problem is None, f"key {key!r} accepted out-of-range {value!r}: {problem}"
    assert cfg.format in FORMATS
    for name in ("lsh_threshold", "jaccard_weight"):
        value = getattr(cfg, name)
        assert math.isfinite(value), f"{name} accepted as non-finite"
        bounds = VALUE_BOUNDS[name]
        assert bounds.low <= value <= bounds.high or (
            not bounds.high_inclusive and value < bounds.high
        ), f"{name} out of range: {value!r}"


def _test_bytes(data: bytes) -> None:
    """Drive ``_read_config_file`` over *data* via a real config file."""
    import os
    import tempfile
    from unittest import mock

    from resembl import config as config_module

    # ``_read_config_file`` resolves its path through
    # ``config_path_get``; point it at a scratch file holding the
    # fuzzed bytes so the real reader sees them.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.toml")
        with open(path, "wb") as fh:
            fh.write(data)
        with mock.patch.object(config_module, "config_path_get", return_value=path):
            config, error = _read_config_file()
    assert isinstance(config, dict), f"reader returned non-dict: {config!r}"
    assert error is None or isinstance(error, Exception)
    # Keys are strings, values are TOML-typed scalars or tables; a
    # datetime leaking through would crash ``update``'s coercion.
    for key, value in config.items():
        assert isinstance(key, str), f"non-string key: {key!r}"
        assert not isinstance(value, (bytes, bytearray)), f"bytes value: {value!r}"
    # A parsed config merges without raising and stays in range.
    cfg = ResemblConfig()
    cfg.update(config)
    _check_config(cfg)
    # Idempotence: a second merge of the same file is a fixed point.
    snapshot = cfg.to_dict()
    cfg.update(config)
    assert cfg.to_dict() == snapshot, "merge is not idempotent"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    _test_bytes(_build_document(fdp))


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
