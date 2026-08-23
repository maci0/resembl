"""A minimal, bit-compatible MinHash implementation and LSH banding search.

This module vendors the two pieces of ``datasketch`` that resembl actually
uses — :class:`MinHash` and the ``_optimal_param`` banding search — so the
runtime dependency tree no longer pulls in ``datasketch`` and, through it,
``scipy`` (~60 MB installed) for a single data structure and one numerical
integral.  It also removes the coupling to ``datasketch.lsh._optimal_param``,
a private API.

The behavior is pinned to datasketch 1.x by the test suite
(``tests/test_minhash_equivalence.py``), which cross-checks fingerprints,
Jaccard values and banding parameters against the real library:

- Permutations come from ``numpy.random.RandomState(seed)`` draws in the
  same order as datasketch's ``_init_permutations``.  The legacy MT19937
  stream is stable across numpy releases by numpy's compatibility policy.
- Hash values are 32-bit SHA1 prefixes (little-endian first word).
- The permutation arithmetic ``(a * h + b) % p & max_hash`` uses uint64
  wraparound in exactly datasketch's operation order, so produced
  fingerprints are byte-for-byte identical to datasketch's.
- :meth:`MinHash.jaccard` counts equal positions over ``num_perm``, like
  ``datasketch.MinHash.jaccard``.
- :func:`optimal_param` minimizes the same weighted false-positive/false-
  negative error as datasketch's ``_optimal_param``; the integrals are
  evaluated with fixed-node Gauss-Legendre quadrature (numpy only) instead
  of ``scipy.integrate.quad``.  Both are accurate to well below the gaps
  between distinct ``(b, r)`` candidates, so the chosen parameters agree.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable

import numpy as np

_MERSENNE_PRIME = np.uint64((1 << 61) - 1)
_MAX_HASH = np.uint64((1 << 32) - 1)
_HASH_RANGE = 1 << 32


def sha1_hash32(data: bytes) -> int:
    """Return the first 4 bytes of SHA1(*data*) as a little-endian uint32."""
    return struct.unpack("<I", hashlib.sha1(data).digest()[:4])[0]


class MinHash:
    """MinHash fingerprint, compatible with ``datasketch.MinHash``.

    Only the surface resembl uses is implemented: construction from a
    permutation count or existing hash values, ``update`` / ``update_batch``,
    ``digest``, ``jaccard`` and ``__len__``.  Permutation arrays are generated
    lazily on first update so deserialized objects (which can only be read,
    never updated) skip the random-stream work entirely.
    """

    def __init__(
        self,
        num_perm: int = 128,
        seed: int = 1,
        hashvalues: Iterable[int] | None = None,
    ) -> None:
        if hashvalues is not None:
            num_perm = len(tuple(hashvalues))
        if num_perm > _HASH_RANGE:
            raise ValueError(f"Cannot have more than {_HASH_RANGE} number of permutation functions")
        self.seed = seed
        self.num_perm = num_perm
        if hashvalues is not None:
            self.hashvalues = np.array(list(hashvalues), dtype=np.uint64)
        else:
            self.hashvalues = np.ones(num_perm, dtype=np.uint64) * _MAX_HASH
        self._permutations: tuple[np.ndarray, np.ndarray] | None = None

    @property
    def permutations(self) -> tuple[np.ndarray, np.ndarray]:
        """The ``(a, b)`` universal-hash parameters, generated on first use.

        Drawn from ``RandomState(seed)`` in the same per-permutation order as
        datasketch (a from [1, p), then b from [0, p)), keeping the legacy
        MT19937 stream byte-identical across numpy versions.
        """
        if self._permutations is None:
            gen = np.random.RandomState(self.seed)
            table = np.array(
                [
                    (
                        gen.randint(1, _MERSENNE_PRIME, dtype=np.uint64),
                        gen.randint(0, _MERSENNE_PRIME, dtype=np.uint64),
                    )
                    for _ in range(self.num_perm)
                ],
                dtype=np.uint64,
            ).T
            self._permutations = (table[0], table[1])
        return self._permutations

    def update(self, value: bytes) -> None:
        """Fold one element into the fingerprint (per-position minimum)."""
        hv = sha1_hash32(value)
        a, b = self.permutations
        phv = np.bitwise_and((a * hv + b) % _MERSENNE_PRIME, _MAX_HASH)
        self.hashvalues = np.minimum(phv, self.hashvalues)

    def update_batch(self, values: Iterable[bytes]) -> None:
        """Fold many elements at once; identical result to repeated :meth:`update`."""
        hv_list = [sha1_hash32(value) for value in values]
        if not hv_list:
            return
        a, b = self.permutations
        hv = np.array(hv_list, dtype=np.uint64, ndmin=2).T
        phv = (hv * a + b) % _MERSENNE_PRIME
        phv = np.bitwise_and(phv, _MAX_HASH)
        self.hashvalues = np.minimum(self.hashvalues, phv.min(axis=0))

    def digest(self) -> np.ndarray:
        """Return a copy of the internal hash values."""
        return self.hashvalues.copy()

    def jaccard(self, other: MinHash) -> float:
        """Estimated Jaccard similarity: fraction of equal positions."""
        if other.seed != self.seed:
            raise ValueError("Cannot compute Jaccard given MinHash with different seeds")
        if len(self) != len(other):
            raise ValueError(
                "Cannot compute Jaccard given MinHash with "
                "different numbers of permutation functions"
            )
        return float(np.count_nonzero(self.hashvalues == other.hashvalues)) / float(len(self))

    def __len__(self) -> int:
        return len(self.hashvalues)


#: Gauss-Legendre nodes for the banding integrals.  The integrands are
#: analytic on [0, 1], so convergence is spectral: far more accurate than
#: the adaptive quadrature tolerance of the scipy version.
_QUAD_NODES = 128


def optimal_param(
    threshold: float, num_perm: int, false_positive_weight: float, false_negative_weight: float
) -> tuple[int, int]:
    """Return the ``(b, r)`` LSH banding minimizing weighted FP+FN error.

    Equivalent to ``datasketch.lsh._optimal_param(threshold, num_perm, w_fp,
    w_fn)``: it searches every divisor split of ``num_perm`` into ``b`` bands
    of ``r`` rows and minimizes the weighted sum of the false-positive
    probability ``∫₀ᵗ 1-(1-sʳ)ᵇ ds`` and the false-negative probability
    ``∫ₜ¹ 1-(1-(1-sʳ)ᵇ) ds``.  Ties keep the first candidate in ``(b``
    ascending, ``r`` ascending``)`` order, matching the reference loop.
    """
    nodes, weights = np.polynomial.legendre.leggauss(_QUAD_NODES)
    bs = np.concatenate([np.full(num_perm // b, b, dtype=np.int64) for b in range(1, num_perm + 1)])
    rs = np.concatenate(
        [np.arange(1, num_perm // b + 1, dtype=np.int64) for b in range(1, num_perm + 1)]
    )
    bf = bs.astype(np.float64)
    rf = rs.astype(np.float64)

    # False positives: similarity below threshold slipping through a band.
    s_pos = threshold * (nodes + 1.0) / 2.0
    fp_weights = weights * (threshold / 2.0)
    match_prob = 1.0 - (1.0 - s_pos[:, None] ** rf[None, :]) ** bf[None, :]
    fp = match_prob.T @ fp_weights

    # False negatives: similarity above threshold missing every band.
    s_neg = threshold + (1.0 - threshold) * (nodes + 1.0) / 2.0
    fn_weights = weights * ((1.0 - threshold) / 2.0)
    miss_prob = 1.0 - (1.0 - (1.0 - s_neg[:, None] ** rf[None, :]) ** bf[None, :])
    fn = miss_prob.T @ fn_weights

    best = int(np.argmin(fp * false_positive_weight + fn * false_negative_weight))
    return int(bs[best]), int(rs[best])
