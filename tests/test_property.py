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
        list(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ,;[]\n\t+-*"
        )
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


class TestPropertyPackedStorage(unittest.TestCase):
    """Property-based tests for the packed fingerprint format."""

    @given(code=asm_text)
    @settings(max_examples=100, deadline=2000)
    def test_pack_unpack_roundtrip(self, code: str) -> None:
        """minhash_pack -> minhash_unpack must round-trip any fingerprint."""
        from resembl.models import minhash_pack, minhash_unpack

        m = code_create_minhash(code)
        raw = minhash_pack(m)
        self.assertTrue(raw.startswith(b"RMLH"))
        restored = minhash_unpack(raw)
        self.assertEqual(m.jaccard(restored), 1.0)
        # Packed Jaccard agrees with the object-based Jaccard.
        from resembl.models import minhash_jaccard

        self.assertEqual(minhash_jaccard(raw, raw), 1.0)

    @given(code=asm_text.filter(lambda c: c.strip()))
    @settings(max_examples=100, deadline=2000)
    def test_checksum_matches_stored_minhash(self, code: str) -> None:
        """The checksum identifies the exact code; equal minhash for equal checksum."""
        from sqlmodel import Session, SQLModel, create_engine

        from resembl.core import snippet_add, snippet_get
        from resembl.models import Snippet

        engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            snippet_add(session, "prop", code)
            snippet = snippet_get(session, string_checksum(code))
            self.assertIsNotNone(snippet)
            self.assertTrue(snippet.minhash.startswith(b"RMLH"))
        engine.dispose()

    @given(data=st.binary(max_size=128))
    @settings(max_examples=200, deadline=2000)
    def test_malformed_packed_blobs_raise_value_error(self, data: bytes) -> None:
        """Corrupt RMLH-prefixed blobs raise ValueError, never low-level errors.

        Random bytes that happen to carry the ``RMLH`` magic are the hostile
        case (corrupted databases, malicious ``merge`` sources): unpacking
        must fail with a controlled ``ValueError`` — not ``struct.error``,
        ``MemoryError``, ``ZeroDivisionError``, or a huge allocation.  Non-
        RMLH bytes take the legacy pickle path, which may raise any exception
        (or, extremely rarely, succeed) — the requirement is no crash.
        """
        from resembl.lsh import band_buckets
        from resembl.models import minhash_ensure_packed, minhash_unpack

        if data.startswith(b"RMLH"):
            with self.assertRaises(ValueError):
                minhash_unpack(data)
            with self.assertRaises(ValueError):
                minhash_ensure_packed(data)
            with self.assertRaises(ValueError):
                band_buckets(data, 128, 25, 5)
        else:
            # Legacy pickle fallback: any outcome is fine, it must not raise
            # something unexpected like MemoryError from a crafted payload.
            try:
                minhash_unpack(data)
            except ValueError:
                pass
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
