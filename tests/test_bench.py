"""Benchmark suite for resembl core operations using pytest-benchmark."""

# pylint: disable=consider-using-with  # a test keeps a temp file, a temp
# directory or a child process open for the whole test body on purpose

import os

import pytest
from sqlmodel import Session, SQLModel, create_engine

from resembl.core import (
    code_create_minhash,
    code_tokenize,
    snippet_add,
    snippet_find_matches,
    string_normalize,
)

_SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "test_data")
_SAMPLE_FILES = sorted(
    os.path.join(_SAMPLE_DIR, f) for f in os.listdir(_SAMPLE_DIR) if f.endswith(".asm")
)

assert _SAMPLE_FILES, "test corpus is missing: no .asm files in tests/test_data"

with open(_SAMPLE_FILES[0], encoding="utf-8") as _f:
    _SAMPLE_CODE = _f.read()

assert _SAMPLE_CODE.strip(), "first corpus file is empty"
_LARGE_CODE = "\n".join(open(p, encoding="utf-8").read() for p in _SAMPLE_FILES[:5])

# Fixed, hand-computed fixture: exact expectations below pin the
# tokenize/normalize/minhash behaviour this suite benchmarks.  The corpus
# benchmarks only need to prove the timed call did real work, the fixed
# fixture proves the work is correct — a regression that tokenized to
# garbage non-empty strings would still pass the corpus checks alone.
_FIXED_CODE = "MOV EAX, 1\nRET"
_FIXED_TOKENS = ["MOV", "REG", "IMM", "RET"]
_FIXED_TOKENS_RAW = ["MOV", "EAX", "1", "RET"]
_FIXED_NORMALIZED = "MOV EAX , 1\nRET"


# --- Pure function benchmarks (no DB) ---


def test_bench_tokenize(benchmark):
    """Benchmark tokenizing a real assembly sample."""
    result = benchmark(code_tokenize, _SAMPLE_CODE)
    assert isinstance(result, list) and result, "tokenize returned no tokens"
    assert all(isinstance(t, str) and t for t in result)
    # Exact pin on the fixed fixture: a silent tokenizer regression
    # (e.g. dropping normalization) changes these tokens.
    assert code_tokenize(_FIXED_CODE) == _FIXED_TOKENS
    assert code_tokenize("") == []


def test_bench_tokenize_no_normalize(benchmark):
    """Benchmark tokenizing without normalization."""
    result = benchmark(code_tokenize, _SAMPLE_CODE, False)
    assert isinstance(result, list) and result, "unnormalized tokenize returned no tokens"
    assert all(isinstance(t, str) and t for t in result)
    assert code_tokenize(_FIXED_CODE, normalize=False) == _FIXED_TOKENS_RAW
    assert code_tokenize("", normalize=False) == []


def test_bench_normalize(benchmark):
    """Benchmark string normalization."""
    result = benchmark(string_normalize, _SAMPLE_CODE)
    assert isinstance(result, str) and result.strip(), "normalize returned empty text"
    assert string_normalize(_FIXED_CODE) == _FIXED_NORMALIZED
    assert string_normalize("") == ""


def test_bench_minhash_small(benchmark):
    """Benchmark MinHash creation on a small snippet."""
    result = benchmark(code_create_minhash, _SAMPLE_CODE)
    assert result.jaccard(result) == 1.0, "minhash must be self-identical"
    # Self-identity alone holds for any deterministic hash, even a broken
    # one.  Pin: identical code scores 1.0, different code scores < 1.0.
    fixed = code_create_minhash(_FIXED_CODE)
    assert fixed.jaccard(code_create_minhash(_FIXED_CODE)) == 1.0
    assert 0.0 <= fixed.jaccard(code_create_minhash("MOV EAX, 2")) < 1.0


def test_bench_minhash_large(benchmark):
    """Benchmark MinHash creation on a larger combined snippet."""
    result = benchmark(code_create_minhash, _LARGE_CODE)
    assert result.jaccard(result) == 1.0, "minhash must be self-identical"
    assert result.jaccard(code_create_minhash(_LARGE_CODE)) == 1.0
    assert 0.0 <= result.jaccard(code_create_minhash(_FIXED_CODE)) < 1.0


# --- DB-backed benchmarks ---


@pytest.fixture(scope="module")
def db_session():
    """Create an in-memory DB with sample snippets for benchmark queries."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    for path in _SAMPLE_FILES[:20]:
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            code = f.read()
        snippet_add(session, name, code)
    yield session
    session.close()


def test_bench_find_matches(benchmark, db_session):
    """Benchmark finding matches in a populated database."""
    _count, result = benchmark(snippet_find_matches, db_session, _SAMPLE_CODE, top_n=5)
    assert isinstance(result, list) and result, "find_matches returned no matches"
    assert all(score > 0 for _snippet, score in result), "match scores must be positive"
    scores = [score for _snippet, score in result]
    assert scores == sorted(scores, reverse=True), "find_matches must return best-first"
    assert all(0.0 < score <= 100.0 for score in scores), "match scores must be in (0, 100]"
    assert len(result) <= 5, "top_n must bound the result"
