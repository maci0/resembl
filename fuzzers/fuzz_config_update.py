#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the ``ResemblConfig.update`` configuration-value coercion.

``ResemblConfig.update`` is the one place a hand-edited ``config.toml`` is
merged into typed configuration: every value comes from ``tomllib`` and is
untrusted.  A value that escapes the coercion as a second exception type
turns a malformed config file into a traceback instead of a warning plus
defaults, and a value that slips through out of range (NaN, a fraction
for an int field, a threshold above the banding limit) silently bends
every later ``find`` / reindex instead of being refused where it was
written.

The harness drives the real entry point and asserts the invariants the
callers rely on, not just the absence of a crash:

* ``update`` never raises for any JSON/TOML-shaped value: it warns and
  keeps the current value on rejection (the documented contract), so
  any exception escaping is a bug the fuzzer can see;
* after the merge every field holds its documented type and range
  (``validate_value`` passes, ``format`` is an allowed spelling, the
  numeric fields are finite and inside ``VALUE_BOUNDS``), so an
  accepted value cannot poison scoring or banding downstream;
* a second ``update`` with the same body is idempotent: the merged
  config is a fixed point, so a value accepted once is not
  re-interpreted differently on the next load.

Values are generated structure-aware: one of the known keys (or an
unknown / near-miss key) gets a fuzzer-chosen JSON scalar from the edge
set the ``/find`` harness uses (including the values that overflow
``int()``/``float()`` and NaN/infinity), plus nested structures,
strings that look numeric, and bools — the shapes a hand-edited TOML
file actually contains.  Bodies round-trip through JSON so the harness
sees exactly the types ``tomllib`` can produce.
"""

import json
import math
import sys
from typing import Any

import atheris

with atheris.instrument_imports():
    from resembl.config import FORMATS, VALUE_BOUNDS, ResemblConfig, validate_value

#: The configuration keys a caller may write, plus one unknown key so the
#: "ignore unknown key" path is exercised too.
_PARAM_NAMES = (
    "lsh_threshold",
    "num_permutations",
    "top_n",
    "ngram_size",
    "jaccard_weight",
    "format",
    "unknown_key",
)

#: Values that are legal JSON/TOML but hostile to ``int()``/``float()``,
#: plus the range edges each field is checked against.
_EDGE_VALUES: tuple[Any, ...] = (
    0,
    1,
    -1,
    2,
    0.0,
    0.5,
    0.99,
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
    "128",
    "abc",
    "table",
    "json",
    "csv",
    "yaml",
    10**30,
    -(10**30),
    10**400,
    -(10**400),
    1 << 62,
    [],
    {},
    [1, 2],
    {"a": 1},
    128.0,
    128.5,
    0.995,
)


def _build_body(fdp: atheris.FuzzedDataProvider) -> dict[str, Any]:
    """Return a config body built from *fdp*."""
    body: dict[str, Any] = {}
    for _ in range(fdp.ConsumeIntInRange(1, len(_PARAM_NAMES))):
        name = _PARAM_NAMES[fdp.ConsumeIntInRange(0, len(_PARAM_NAMES) - 1)]
        body[name] = _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    return body


def _check_config(cfg: ResemblConfig) -> None:
    """Assert the merged config satisfies the bounds the callers rely on."""
    for key, value in cfg.items():
        problem = validate_value(key, value)
        assert problem is None, f"key {key!r} accepted out-of-range {value!r}: {problem}"
    assert cfg.format in FORMATS, f"format accepted: {cfg.format!r}"
    assert isinstance(cfg.num_permutations, int), "num_permutations is not an int"
    assert isinstance(cfg.top_n, int), "top_n is not an int"
    assert isinstance(cfg.ngram_size, int), "ngram_size is not an int"
    for name in ("lsh_threshold", "jaccard_weight"):
        value = getattr(cfg, name)
        assert isinstance(value, float), f"{name} is not a float: {value!r}"
        assert math.isfinite(value), f"{name} was accepted as non-finite"
        bounds = VALUE_BOUNDS[name]
        assert bounds.low <= value, f"{name} below range: {value!r}"
        if bounds.high_inclusive:
            assert value <= bounds.high, f"{name} above range: {value!r}"
        else:
            assert value < bounds.high, f"{name} above range: {value!r}"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    body = _build_body(fdp)
    # Round-trip through JSON so the harness sees exactly the types
    # ``tomllib`` can produce: JSON has no NaN/infinity of its own, and
    # ``json`` encodes those as bare tokens Python's decoder accepts back.
    try:
        body = json.loads(json.dumps(body))
    except ValueError:
        return
    assert isinstance(body, dict)

    cfg = ResemblConfig()
    # ``update`` must never raise: a rejection is a warning plus the
    # current value, per its documented contract.  Any exception type
    # escaping here is a bug the fuzzer can see.
    cfg.update(body)
    _check_config(cfg)

    snapshot = cfg.to_dict()
    cfg.update(body)
    assert cfg.to_dict() == snapshot, "update is not idempotent"


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
