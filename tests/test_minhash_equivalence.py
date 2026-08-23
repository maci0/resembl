"""Bit-compatibility tests: resembl.minhash against the datasketch oracle.

``resembl.minhash`` vendors a minimal MinHash and LSH banding search so the
runtime tree does not need ``datasketch`` (and its heavy ``scipy``
dependency).  These tests are the contract that keeps the vendored code
byte-for-byte compatible with datasketch 1.x: fingerprints, permutation
tables, Jaccard values and chosen ``(b, r)`` banding parameters must all
match exactly.  If a datasketch upgrade ever changes any of these, this
suite fails and forces a deliberate compatibility decision (and a
``FINGERPRINT_VERSION`` bump if fingerprints change).
"""

import random
import unittest

import numpy as np

from resembl.minhash import MinHash, optimal_param, sha1_hash32


class TestSha1Hash32(unittest.TestCase):
    """The element hash must be datasketch's little-endian SHA1 prefix."""

    def test_matches_datasketch_hashfunc(self):
        from datasketch.hashfunc import sha1_hash32 as ds_sha1_hash32

        rng = random.Random(11)
        for _ in range(50):
            data = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 64)))
            self.assertEqual(sha1_hash32(data), ds_sha1_hash32(data))

    def test_known_vector(self):
        # SHA1("") = da39a3ee...; first 4 bytes little-endian.
        self.assertEqual(sha1_hash32(b""), 0xEEA339DA)


class TestPermutationsMatchDatasketch(unittest.TestCase):
    """Permutation tables must be identical to datasketch's (seed-1 stream)."""

    def test_permutations_identical(self):
        from datasketch import MinHash as DSMinHash

        for num_perm in (2, 16, 64, 128, 256):
            ours = MinHash(num_perm=num_perm)
            theirs = DSMinHash(num_perm=num_perm)
            np.testing.assert_array_equal(ours.permutations[0], theirs.permutations[0])
            np.testing.assert_array_equal(ours.permutations[1], theirs.permutations[1])


class TestDigestsMatchDatasketch(unittest.TestCase):
    """Fingerprints must be byte-for-byte identical across update paths."""

    def test_random_workloads_produce_identical_digests(self):
        from datasketch import MinHash as DSMinHash

        rng = random.Random(7)
        for _ in range(25):
            num_perm = rng.choice([2, 16, 64, 128])
            ours = MinHash(num_perm=num_perm)
            theirs = DSMinHash(num_perm=num_perm)
            for _ in range(rng.randint(0, 6)):
                batch = [
                    bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 30)))
                    for _ in range(rng.randint(1, 40))
                ]
                if rng.random() < 0.5:
                    for item in batch:
                        ours.update(item)
                        theirs.update(item)
                else:
                    ours.update_batch(batch)
                    theirs.update_batch(batch)
                np.testing.assert_array_equal(ours.digest(), theirs.digest())

    def test_update_and_update_batch_agree(self):
        single = MinHash(num_perm=64)
        batched = MinHash(num_perm=64)
        items = [f"tok{i}".encode() for i in range(10)]
        for item in items:
            single.update(item)
        batched.update_batch(items)
        np.testing.assert_array_equal(single.digest(), batched.digest())

    def test_empty_batch_is_a_noop(self):
        m = MinHash(num_perm=64)
        before = m.digest()
        m.update_batch([])
        np.testing.assert_array_equal(before, m.digest())


class TestJaccardMatchesDatasketch(unittest.TestCase):
    """Jaccard values and rejection errors must match datasketch's method."""

    def test_jaccard_parity_on_shared_elements(self):
        from datasketch import MinHash as DSMinHash

        shared = [f"common_{i}".encode() for i in range(30)]
        extra = [f"x_{i}".encode() for i in range(10)]
        ours_a, ours_b = MinHash(128), MinHash(128)
        theirs_a, theirs_b = DSMinHash(128), DSMinHash(128)
        # Feed set A through batch updates and set B through per-item updates,
        # exercising both code paths against the oracle on identical inputs.
        ours_a.update_batch(shared)
        theirs_a.update_batch(shared)
        for item in shared + extra:
            ours_b.update(item)
            theirs_b.update(item)
        expected = theirs_b.jaccard(theirs_a)
        self.assertGreater(expected, 0.0)
        self.assertLess(expected, 1.0)
        self.assertEqual(ours_b.jaccard(ours_a), expected)

    def test_mismatch_errors_match_datasketch_messages(self):
        from datasketch import MinHash as DSMinHash

        with self.assertRaisesRegex(ValueError, "different numbers of permutation"):
            MinHash(64).jaccard(MinHash(128))
        with self.assertRaisesRegex(ValueError, "different seeds"):
            MinHash(seed=1).jaccard(MinHash(seed=2))
        # The messages must stay identical to datasketch's own rejections.
        with self.assertRaisesRegex(ValueError, "different numbers of permutation"):
            DSMinHash(64).jaccard(DSMinHash(128))
        with self.assertRaisesRegex(ValueError, "different seeds"):
            DSMinHash(seed=1).jaccard(DSMinHash(seed=2))


class TestConstructorFromHashvalues(unittest.TestCase):
    """Deserialization path must behave like datasketch's hashvalues ctor."""

    def test_roundtrip_parity(self):
        from datasketch import MinHash as DSMinHash

        source = MinHash(128)
        oracle_source = DSMinHash(128)
        items = [f"v{i}".encode() for i in range(20)]
        source.update_batch(items)
        oracle_source.update_batch(items)
        values = [int(v) for v in source.digest()]
        ours = MinHash(hashvalues=values)
        theirs = DSMinHash(num_perm=len(values), hashvalues=values)
        np.testing.assert_array_equal(ours.digest(), theirs.digest())
        self.assertEqual(ours.jaccard(source), theirs.jaccard(oracle_source))

    def test_rejects_oversized_num_perm_like_datasketch(self):
        from datasketch import MinHash as DSMinHash

        with self.assertRaises(ValueError):
            DSMinHash(num_perm=(1 << 32) + 1)
        with self.assertRaises(ValueError):
            MinHash(num_perm=(1 << 32) + 1)


class TestOptimalParamMatchesDatasketch(unittest.TestCase):
    """Banding search must pick datasketch's exact (b, r) parameters.

    Only the quadrature differs (Gauss-Legendre vs scipy.quad), and both are
    far more accurate than the gaps between distinct candidate pairs — the
    sweep below pins that across thresholds, permutation counts and weightings.
    """

    def test_grid_parity(self):
        from datasketch.lsh import _optimal_param

        for threshold in (0.05, 0.25, 0.5, 0.65, 0.8, 0.95):
            for num_perm in (16, 64, 128):
                for w_fp, w_fn in ((0.5, 0.5), (0.2, 0.8)):
                    expected = _optimal_param(threshold, num_perm, w_fp, w_fn)
                    actual = optimal_param(threshold, num_perm, w_fp, w_fn)
                    self.assertEqual(
                        actual,
                        expected,
                        f"(b, r) diverged at threshold={threshold} num_perm={num_perm} "
                        f"weights=({w_fp}, {w_fn}): {actual} != {expected}",
                    )

    def test_production_threshold_banding(self):
        """The default configuration keeps datasketch's (25, 5) banding."""
        from datasketch.lsh import _optimal_param

        self.assertEqual(optimal_param(0.5, 128, 0.5, 0.5), _optimal_param(0.5, 128, 0.5, 0.5))


if __name__ == "__main__":
    unittest.main()
