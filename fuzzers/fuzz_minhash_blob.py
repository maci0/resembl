#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the packed MinHash blob readers (the stored-fingerprint format).

Every fingerprint this project persists is a self-describing byte blob
(``RMLH`` magic + big-endian uint32 permutation count + uint32 hash values)
and every read of it goes back through :func:`minhash_num_perm` /
:func:`minhash_unpack`.  The bytes come from places this process does not
control: another database opened by ``merge``, a database file edited on
disk, a fingerprint written by a differently-configured older build.  So the
blob is an untrusted-input parser, and a malformed one that escapes as
``struct.error``, ``IndexError`` or a 4 GB allocation takes down the
caller instead of the row it came from.

The harness asserts the invariants the callers rely on, not just the absence
of a crash:

* the readers reject malformed blobs with ``ValueError`` only (the callers
  catch exactly that and heal the row by recomputing the fingerprint);
* a blob that *is* accepted round-trips byte-for-byte through
  :func:`minhash_pack` — nothing is silently dropped on the way through the
  database;
* the encoded permutation count always equals the digest length, so no
  caller can size a banding structure from one number and fill it from
  another;
* Jaccard stays in [0, 1], is 1.0 for a blob against itself, and is
  symmetric — the vectorized batch path must agree with the scalar one
  position for position, since the two are used interchangeably on the same
  candidate set.
"""

import struct
import sys

import atheris

with atheris.instrument_imports():
    from resembl.scoring import (
        MAX_NUM_PERM,
        MINHASH_MAGIC,
        code_create_minhash,
        minhash_ensure_packed,
        minhash_jaccard,
        minhash_jaccard_batch,
        minhash_num_perm,
        minhash_pack,
        minhash_unpack,
    )

#: A blob with a valid header and a payload, the shape a stored row has.
_CONTROL = minhash_pack(code_create_minhash("mov eax, 1\nret"))

#: Counts worth spending runs on: the bounds of the accepted range, the
#: values that make the length check disagree with the header, and the
#: counts real configurations use.  Drawn from uniformly, a 4-byte count is
#: accepted only in 4094 of 2**32 possibilities, so the accepted path would
#: never be reached.
_INTERESTING_COUNTS = (0, 1, 2, 3, 4, 8, 16, 64, 127, 128, 129, 1024, 4095, 4096, 4097, 1 << 20)


def _payload(fdp: atheris.FuzzedDataProvider, length: int) -> bytes:
    """Return *length* fuzzer-chosen hash-value bytes."""
    return fdp.ConsumeBytes(length)


def _build_candidate(fdp: atheris.FuzzedDataProvider) -> bytes:
    """Build a plausible fingerprint blob from *fdp*.

    Random bytes almost never reach the header checks (they fail the magic
    test immediately), so the interesting inputs are a blob built around a
    real header: a count that is out of range, a payload whose length
    contradicts the count, or a well-formed blob carrying junk hash values.
    """
    mode = fdp.ConsumeIntInRange(0, 4)
    if mode == 0:
        return fdp.ConsumeBytes(fdp.remaining_bytes())
    if mode == 1:
        # No magic: a payload-only blob, which must be refused by header.
        return fdp.ConsumeBytes(fdp.remaining_bytes())
    num_perm = _INTERESTING_COUNTS[fdp.ConsumeIntInRange(0, len(_INTERESTING_COUNTS) - 1)]
    payload = _payload(fdp, 4 * num_perm)
    if mode == 2:
        # Length matches the encoded count: this is an acceptable blob.
        return MINHASH_MAGIC + struct.pack(">I", num_perm) + payload
    if mode == 3:
        # Length contradicts the encoded count, in both directions.
        delta = fdp.ConsumeIntInRange(-8, 8)
        return MINHASH_MAGIC + struct.pack(">I", num_perm) + payload[: max(0, len(payload) + delta)]
    # Valid header, a real blob's hash values, near-miss magic or count.
    return MINHASH_MAGIC + struct.pack(">I", num_perm) + _CONTROL[8:][-4 * num_perm :]


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    candidate = _build_candidate(fdp)

    try:
        num_perm = minhash_num_perm(candidate)
    except ValueError:
        # Rejected with the documented error: the header is what the callers
        # guard on, so nothing below can read a malformed blob.
        return

    # An accepted header must describe the whole blob and a real count.
    assert 2 <= num_perm <= MAX_NUM_PERM, f"accepted implausible count {num_perm}"
    assert len(candidate) == 8 + 4 * num_perm, "accepted a blob whose length contradicts its count"

    unpacked = minhash_unpack(candidate)
    assert len(unpacked.digest()) == num_perm, "unpacked digest length disagrees with the header"
    # Persistence round trip: what goes into the database must come back out.
    assert minhash_pack(unpacked) == candidate, "pack(unpack(blob)) changed the blob"
    # The heal path must accept the blob untouched, never repack it.
    assert minhash_ensure_packed(candidate) == candidate, "ensure_packed rewrote a valid blob"

    jaccard_self = minhash_jaccard(candidate, candidate)
    assert jaccard_self == 1.0, f"a blob scored {jaccard_self} against itself"
    # Blobs of different permutation counts are not comparable: that is a
    # ValueError by contract, in both the scalar and the batched path, and
    # the fuzzer must not read a count mismatch as a crash.
    if num_perm != minhash_num_perm(_CONTROL):
        for comparison in (
            lambda: minhash_jaccard(candidate, _CONTROL),
            lambda: minhash_jaccard_batch(candidate, [_CONTROL]),
        ):
            try:
                comparison()
            except ValueError:
                pass
            else:
                raise AssertionError("mismatched permutation counts were scored")
        return
    forward = minhash_jaccard(candidate, _CONTROL)
    backward = minhash_jaccard(_CONTROL, candidate)
    assert 0.0 <= forward <= 1.0, f"jaccard out of range: {forward}"
    assert forward == backward, f"jaccard is not symmetric: {forward} vs {backward}"
    # The batched path is the scoring hot path and must match the scalar one.
    candidates = [candidate, _CONTROL]
    batch = minhash_jaccard_batch(candidate, candidates)
    assert batch == [minhash_jaccard(candidate, c) for c in candidates], (
        "batch jaccard disagrees with the scalar jaccard"
    )


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
