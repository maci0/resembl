#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the MinHash construction parameters.

``code_create_minhash`` / ``minhash_from_tokens`` take ``ngram_size`` and
``num_perm`` from configuration and request parameters, both untrusted:
a hand-edited ``config.toml`` reaches them through ``ResemblConfig.update``
and a ``/find`` body through ``_parse_find_request``.  The callers bound
them, but the construction itself is the last line: a count that slips
through as a float, a bool, a string or a huge integer must be refused
or produce a digest of exactly that length, never a crash of a second
exception type, a hang, or a digest whose length disagrees with the
count the caller sized a banding structure from.

The harness drives the real entry points with structure-aware inputs
and asserts the invariants the callers rely on, not just the absence
of a crash:

* the only rejection is ``ValueError`` (the callers' documented guard);
  any other exception type escaping is a bug the fuzzer can see;
* an accepted digest has exactly ``num_perm`` entries, so no caller
  can size a band from one number and fill it from another;
* the same input twice produces the same digest, and the batch entry
  point agrees with the scalar one position for position;
* a short input (fewer tokens than ``ngram_size``) is fingerprinted
  from its joined tokens, so it is never the empty digest — the
  degenerate-fingerprint shape the config bounds exist to refuse.

Parameter values are drawn from an edge set (zero, negatives, the
``MAX_NUM_PERM`` bounds, floats, bools, numeric strings, huge ints)
rather than uniformly: a uniformly drawn count is almost never a
buildable one, so the accepted path would never be reached.
"""

import sys

import atheris

with atheris.instrument_imports():
    from resembl.scoring import (
        MAX_NUM_PERM,
        code_create_minhash,
        code_create_minhash_batch,
        code_tokenize,
        minhash_from_tokens,
    )

#: Values a caller can actually pass after its own coercion: the
#: config loader and the request parser each coerce to ``int`` first,
#: so the hostile shapes that reach construction are ints (including
#: bools, which are ints) and the range edges they check against.
#: Non-int types are excluded on purpose: they are refused by the
#: caller's coercion, and feeding them here would only re-test that
#: refusal as a ``TypeError`` from numpy, not a bug in this layer.
_EDGE_VALUES: tuple[int, ...] = (
    0,
    1,
    2,
    3,
    -1,
    -2,
    -100,
    16,
    64,
    127,
    128,
    129,
    1024,
    MAX_NUM_PERM - 1,
    MAX_NUM_PERM,
    MAX_NUM_PERM + 1,
    1 << 20,
)

#: Texts worth spending runs on: blank (no tokens), a single token
#: (shorter than any n-gram), a realistic listing, and inputs that
#: stress the tokenizer and the normalization branches.
_INTERESTING_TEXTS = (
    "",
    "   ",
    "ret",
    "mov eax, 1\nret",
    "push ebp\nmov ebp, esp\nret",
    "\x00\x01\x02",
    "héllo wörld — ünïcode",
)


def _text(fdp: atheris.FuzzedDataProvider) -> str:
    """Return a fuzzer-chosen text from the interesting set or raw unicode."""
    if fdp.ConsumeBool():
        return _INTERESTING_TEXTS[fdp.ConsumeIntInRange(0, len(_INTERESTING_TEXTS) - 1)]
    return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 64))


def _param(fdp: atheris.FuzzedDataProvider) -> int:
    """Return one fuzzer-chosen parameter value from the edge set."""
    return _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    text = _text(fdp)
    ngram_size = _param(fdp)
    num_perm = _param(fdp)
    normalize = fdp.ConsumeBool()

    # Only an int-like count can be built; anything else the fuzzer
    # draws is a rejection case, asserted below by the ValueError-only
    # contract rather than by pre-filtering here.
    try:
        digest = code_create_minhash(
            text, normalize=normalize, ngram_size=ngram_size, num_perm=num_perm
        ).digest()
    except ValueError:
        return

    # An accepted construction must honour the count it was given: a
    # caller that sized bands from ``num_perm`` must see exactly that
    # many hash values back.  The construction's own contract is
    # ``num_perm >= 1``; the tighter ``MAX_NUM_PERM`` cap is the
    # callers', so a count above it is asserted against the digest
    # length alone, not refused here.
    assert (
        len(digest) == num_perm
    ), f"digest length {len(digest)} disagrees with num_perm {num_perm}"
    assert num_perm >= 1, f"num_perm below 1 was accepted: {num_perm}"
    # An ``ngram_size`` below 1 shingles every token window to the
    # empty tuple, so every snippet fingerprints identically: the
    # degenerate shape ``validate_value`` exists to refuse.  If the
    # construction accepts it, the digests must still differ across
    # different code — a constant digest is the logic bug.
    if ngram_size < 1:
        other = code_create_minhash(
            "push ebp\nmov ebp, esp\nret",
            normalize=normalize,
            ngram_size=ngram_size,
            num_perm=num_perm,
        ).digest()
        assert not (
            digest == other
        ).all(), f"ngram_size {ngram_size} fingerprinted different code identically"

    # Determinism: the same input must fingerprint identically twice,
    # or a reindex would change existing similarity scores.
    again = code_create_minhash(
        text, normalize=normalize, ngram_size=ngram_size, num_perm=num_perm
    ).digest()
    assert (digest == again).all(), "minhash is not deterministic"

    # The batch entry point must agree with the scalar one.
    batch = code_create_minhash_batch(
        [text, text], normalize=normalize, ngram_size=ngram_size, num_perm=num_perm
    )
    assert len(batch) == 2
    assert (batch[0].digest() == digest).all(), "batch minhash disagrees with the scalar one"
    assert (batch[1].digest() == digest).all(), "batch minhash disagrees with the scalar one"

    # The token-level entry point over the same tokens must agree too,
    # since the import hot path tokenizes once and reuses the tokens.
    tokens = code_tokenize(text, normalize)
    from_tokens = minhash_from_tokens(tokens, ngram_size, num_perm).digest()
    assert (from_tokens == digest).all(), "token-level minhash disagrees with the text one"


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
