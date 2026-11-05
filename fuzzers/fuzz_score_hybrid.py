#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the hybrid score combiner.

``score_hybrid`` folds a Jaccard similarity (0-1) and a Levenshtein
ratio (0-100) into the single 0-100 score every ``find`` ranking
and every ``compare`` output is built on.  Its inputs come from
request tunables (``jaccard_weight``) and computed similarities, so
a non-finite or out-of-domain value that slips through turns a
hostile request into a NaN score that poisons every comparison it
takes part in, or into a second exception type the handlers do not
catch.

The harness drives the real entry point with structure-aware
inputs and asserts the invariants the callers rely on, not just the
absence of a crash:

* the only rejection is ``ValueError`` (the documented guard); any
  other exception type escaping is a bug the fuzzer can see;
* an accepted score is finite and in ``[0, 100]`` — the hybrid is
  capped by construction, so an out-of-range or NaN score is a
  logic bug, not a finding;
* the score is the documented weighted sum of its inputs, so a
  mis-wired weight cannot silently re-rank candidates;
* ``jaccard_weight`` 0.0 is pure Levenshtein and 1.0 is pure
  Jaccard — the two documented endpoints of the balance;
* the score is monotone in each input over its domain, so a
  better Jaccard or Levenshtein never lowers a candidate's rank.

Parameter values are drawn from an edge set (domain bounds,
just-outside values, NaN/inf, huge ints) rather than uniformly: a
uniformly drawn float is almost never a valid one, so the accepted
path would never be reached.
"""

import math
import sys

import atheris

with atheris.instrument_imports():
    from resembl.scoring import score_hybrid

#: Values that are legal floats/ints in Python but hostile to the
#: domain checks, plus the range edges each parameter is checked
#: against.
_EDGE_VALUES: tuple[float, ...] = (
    0.0,
    1.0,
    0.5,
    100.0,
    -0.0,
    1e-300,
    1e300,
    float("inf"),
    float("-inf"),
    float("nan"),
    -1.0,
    2.0,
    0.9999999999999999,
    1.0000000000000002,
    99.99999999999999,
    100.00000000000001,
    float(1 << 62),
)


def _value(fdp: atheris.FuzzedDataProvider) -> float:
    """Return one fuzzer-chosen parameter value from the edge set or raw."""
    if fdp.ConsumeBool():
        return _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    return fdp.ConsumeFloatingPoint()


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    jaccard = _value(fdp)
    levenshtein = _value(fdp)
    weight = _value(fdp)

    try:
        score = score_hybrid(jaccard, levenshtein, weight)
    except ValueError:
        return

    assert math.isfinite(score), f"accepted a non-finite score: {score!r}"
    assert 0.0 <= score <= 100.0, f"score out of range: {score!r}"
    expected = (jaccard * 100 * weight) + (levenshtein * (1 - weight))
    assert score == expected, f"score {score!r} is not the documented weighted sum"

    # The documented endpoints: weight 0.0 is pure Levenshtein, 1.0 pure Jaccard.
    assert score_hybrid(jaccard, levenshtein, 0.0) == levenshtein
    assert score_hybrid(jaccard, levenshtein, 1.0) == jaccard * 100

    # Monotonicity in each input over its domain: a better input never
    # lowers the score, which is what keeps ranking stable.
    lower_jaccard = max(0.0, jaccard - 0.25)
    assert score_hybrid(lower_jaccard, levenshtein, weight) <= score
    lower_levenshtein = max(0.0, levenshtein - 25.0)
    assert score_hybrid(jaccard, lower_levenshtein, weight) <= score


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
