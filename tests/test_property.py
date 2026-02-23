"""Property-based tests for resembl core functions using hypothesis."""

import unittest

from datasketch import MinHash
from hypothesis import given, settings
from hypothesis import strategies as st

from resembl.core import (
    code_create_minhash,
    code_tokenize,
    string_checksum,
    string_normalize,
)


# Strategy for generating random assembly-like strings.
asm_text = st.text(
    alphabet=st.sampled_from(
        list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ,;[]\n\t+-*")
    ),
    min_size=0,
    max_size=500,
)


class TestPropertyTokenize(unittest.TestCase):
    """Property-based tests for the tokenizer."""

    @given(code=asm_text)
    @settings(max_examples=200, deadline=2000)
    def test_tokenize_never_crashes(self, code: str) -> None:
        """code_tokenize must never raise for any input string."""
        result = code_tokenize(code)
        self.assertIsInstance(result, list)
        for token in result:
            self.assertIsInstance(token, str)

    @given(code=asm_text)
    @settings(max_examples=200, deadline=2000)
    def test_tokenize_no_normalize_never_crashes(self, code: str) -> None:
        """code_tokenize(normalize=False) must never raise."""
        result = code_tokenize(code, normalize=False)
        self.assertIsInstance(result, list)


class TestPropertyChecksum(unittest.TestCase):
    """Property-based tests for checksum determinism."""

    @given(code=asm_text)
    @settings(max_examples=200, deadline=2000)
    def test_checksum_deterministic(self, code: str) -> None:
        """The same input must always produce the same checksum."""
        c1 = string_checksum(code)
        c2 = string_checksum(code)
        self.assertEqual(c1, c2)

    @given(code=asm_text)
    @settings(max_examples=200, deadline=2000)
    def test_checksum_is_hex_string(self, code: str) -> None:
        """Checksums must be valid hex strings of length 64 (SHA-256)."""
        c = string_checksum(code)
        self.assertEqual(len(c), 64)
        int(c, 16)  # Will raise if not valid hex


class TestPropertyNormalize(unittest.TestCase):
    """Property-based tests for string normalization."""

    @given(code=asm_text)
    @settings(max_examples=200, deadline=2000)
    def test_normalize_never_crashes(self, code: str) -> None:
        """string_normalize must never raise for any input string."""
        result = string_normalize(code)
        self.assertIsInstance(result, str)

    @given(code=asm_text)
    @settings(max_examples=200, deadline=2000)
    def test_tokenize_idempotent(self, code: str) -> None:
        """Tokenizing the normalized output should be stable.

        Note: string_normalize is lossy (numbers→IMM, registers→REG)
        so raw string idempotency is not expected. But tokenizing the
        normalized output twice should yield the same token list.
        """
        tokens_once = code_tokenize(code, normalize=True)
        normalized = string_normalize(code)
        tokens_twice = code_tokenize(normalized, normalize=True)
        self.assertEqual(tokens_once, tokens_twice)


class TestPropertyMinHash(unittest.TestCase):
    """Property-based tests for MinHash creation."""

    @given(code=asm_text)
    @settings(max_examples=200, deadline=2000)
    def test_minhash_always_returns_valid_object(self, code: str) -> None:
        """code_create_minhash must return a MinHash for any input."""
        m = code_create_minhash(code)
        self.assertIsInstance(m, MinHash)

    @given(code=asm_text)
    @settings(max_examples=100, deadline=2000)
    def test_minhash_deterministic(self, code: str) -> None:
        """The same input must produce equivalent MinHash objects."""
        m1 = code_create_minhash(code)
        m2 = code_create_minhash(code)
        self.assertEqual(m1.jaccard(m2), 1.0)


if __name__ == "__main__":
    unittest.main()
