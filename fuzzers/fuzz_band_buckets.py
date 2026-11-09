#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the LSH fingerprint band-slicing over stored blobs.

The packed fingerprint (``RMLH`` magic + uint32 count + uint32 values) is
the stored form every snippet carries, and :func:`resembl.lsh.band_buckets`
turns it into the bucket hex keys the database index looks up.  Both
halves of that step read blobs this process does not control: a hostile
``merge`` source or a hand-edited database supplies the bytes, while the
callers of :func:`band_buckets` supply ``b``/``r``.  ``banding_params``
never produces a split that overhangs the fingerprint, but ``_band_keys_payload``
exposes ``bands``/``rows`` on ``POST /band-keys`` to any caller, and a
corrupt row must be refused by the documented ``ValueError`` instead of
slicing past the end of the blob and collapsing every over-long band to
the empty key ``""`` — the shape where every snippet in the index would
answer every query.

The harness drives the real entry point with structure-aware inputs and
asserts the invariants the rest of the system relies on, not just the
absence of a crash:

* the only rejection is ``ValueError``; any other exception type
  escaping is a bug the fuzzer can see;
* an accepted answer has exactly ``b`` keys, each a lowercase hex string
  of exactly ``8 * r`` characters — a short or empty key means a slice
  overran the blob, the documented failure this harness exists to see;
* two blobs that differ only in the ignored tail (``b * r < num_perm``)
  yield the same keys, exactly as the packed format documents;
* a well-formed blob at the caller's own ``(threshold, num_perm)``
  banding yields keys a second call reproduces byte-for-byte.

Inputs are generated structure-aware: real packed fingerprints from the
committed tokenizer (so the accepted path is reached), blobs whose
encoded count contradicts their length in both directions, blobs with
a near-miss magic or an implausible count, raw junk bytes, and
``b``/``r`` drawn from the accepted range and from below/above it.
Seeds come from the documented packed shape, not from captured traffic.
"""

import struct
import sys

import atheris

with atheris.instrument_imports():
    from resembl.lsh import band_buckets, banding_params
    from resembl.scoring import (
        MINHASH_MAGIC,
        code_create_minhash,
        minhash_pack,
    )

#: A real packed fingerprint, the shape a stored row has.
_CONTROL = minhash_pack(code_create_minhash("mov eax, 1\nret"))

#: Counts worth spending runs on: the range bounds and the real configs.
_INTERESTING_COUNTS = (0, 1, 2, 3, 4, 8, 16, 64, 127, 128, 129, 1024, 4096, 4097)


def _build_blob(fdp: atheris.FuzzedDataProvider) -> bytes:
    """Return one fingerprint blob (or junk) built from *fdp*."""
    mode = fdp.ConsumeIntInRange(0, 4)
    if mode == 0:
        # A well-formed blob: a real fingerprint, maybe with its tail
        # extended — the tail the banding is documented to ignore.
        mode2 = fdp.ConsumeIntInRange(0, 2)
        if mode2 == 0:
            return _CONTROL
        if mode2 == 1:
            return _CONTROL + fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 32))
        return _CONTROL[:-1] if len(_CONTROL) > 1 else _CONTROL
    if mode == 1:
        # A plausible header whose payload contradicts the count,
        # in both directions.
        num_perm = _INTERESTING_COUNTS[fdp.ConsumeIntInRange(0, len(_INTERESTING_COUNTS) - 1)]
        payload = fdp.ConsumeBytes(4 * num_perm)
        delta = fdp.ConsumeIntInRange(-8, 8)
        return MINHASH_MAGIC + struct.pack(">I", num_perm) + payload[: max(0, len(payload) + delta)]
    if mode == 2:
        # No magic, or a near-miss magic: refused by the header check.
        return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 600))
    if mode == 3:
        # A plausible header and a well-formed payload but an
        # implausible count: refused by the range check.
        num_perm = _INTERESTING_COUNTS[fdp.ConsumeIntInRange(0, len(_INTERESTING_COUNTS) - 1)]
        return MINHASH_MAGIC + struct.pack(">I", num_perm) + fdp.ConsumeBytes(4 * num_perm)
    # A valid fingerprint whose count was rewritten: the header
    # count disagrees with the number the caller passes below.
    num_perm = _CONTROL and (len(_CONTROL) - 8) // 4
    return MINHASH_MAGIC + struct.pack(">I", num_perm + fdp.ConsumeIntInRange(-4, 4)) + _CONTROL[8:]


def _check_band_buckets(blob: bytes, fdp: atheris.FuzzedDataProvider) -> None:
    """Assert ``band_buckets`` holds its contract for one blob and split."""
    num_perm = fdp.ConsumeIntInRange(0, 260)
    b_value = fdp.ConsumeIntInRange(-2, 260)
    r_value = fdp.ConsumeIntInRange(-2, 260)
    try:
        keys = band_buckets(blob, num_perm, b_value, r_value)
    except ValueError:
        return

    assert isinstance(keys, list), f"band_buckets returned non-list: {keys!r}"
    assert len(keys) == b_value, f"band count {len(keys)} disagrees with b {b_value}"
    for key in keys:
        assert isinstance(key, str), f"non-string key: {key!r}"
        assert key == key.lower() and all(ch in "0123456789abcdef" for ch in key), (
            f"non-hex key: {key!r}"
        )
        assert len(key) == 8 * r_value, (
            f"key length {len(key)} disagrees with r {r_value}: a slice overran the blob"
        )
    # Fixed point: the same blob and split yield the same keys.
    assert band_buckets(blob, num_perm, b_value, r_value) == keys, "banding is not a fixed point"


def _check_tail_ignored(fdp: atheris.FuzzedDataProvider) -> None:
    """Assert the tail past ``b * r`` does not change the keys."""
    num_perm = (_CONTROL and (len(_CONTROL) - 8) // 4) or 128
    threshold = fdp.ConsumeIntInRange(0, 100) / 100.0
    b_value, r_value = banding_params(threshold, num_perm)
    if b_value * r_value >= num_perm:
        return  # No ignored tail to compare.
    extended = _CONTROL + b"\xde\xad\xbe\xef" * 8
    assert band_buckets(_CONTROL, num_perm, b_value, r_value) == band_buckets(
        extended, num_perm, b_value, r_value
    ), "an ignored tail changed the bucket keys"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    blob = _build_blob(fdp)
    _check_band_buckets(blob, fdp)
    _check_tail_ignored(fdp)


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
