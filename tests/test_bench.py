"""Benchmark suite for resembl core operations using pytest-benchmark."""

import os
import tempfile
import unittest

import pytest
from sqlmodel import Session, SQLModel, create_engine

from resembl.core import (
    code_create_minhash,
    code_tokenize,
    snippet_add,
    snippet_find_matches,
    string_normalize,
)

# Load a real-world .asm sample for benchmarking.
_SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "test_data")
_SAMPLE_FILES = sorted(
    os.path.join(_SAMPLE_DIR, f)
    for f in os.listdir(_SAMPLE_DIR)
    if f.endswith(".asm")
)

# Read the first sample file for single-snippet benchmarks.
with open(_SAMPLE_FILES[0], "r", encoding="utf-8") as _f:
    _SAMPLE_CODE = _f.read()

# Read a larger snippet for scaling benchmarks.
_LARGE_CODE = "\n".join(
    open(p, "r", encoding="utf-8").read() for p in _SAMPLE_FILES[:5]
)


# --- Pure function benchmarks (no DB) ---


def test_bench_tokenize(benchmark):
    """Benchmark tokenizing a real assembly sample."""
    benchmark(code_tokenize, _SAMPLE_CODE)


def test_bench_tokenize_no_normalize(benchmark):
    """Benchmark tokenizing without normalization."""
    benchmark(code_tokenize, _SAMPLE_CODE, False)


def test_bench_normalize(benchmark):
    """Benchmark string normalization."""
    benchmark(string_normalize, _SAMPLE_CODE)


def test_bench_minhash_small(benchmark):
    """Benchmark MinHash creation on a small snippet."""
    benchmark(code_create_minhash, _SAMPLE_CODE)


def test_bench_minhash_large(benchmark):
    """Benchmark MinHash creation on a larger combined snippet."""
    benchmark(code_create_minhash, _LARGE_CODE)


# --- DB-backed benchmarks ---


@pytest.fixture(scope="module")
def db_session():
    """Create an in-memory DB with sample snippets for benchmark queries."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    for path in _SAMPLE_FILES[:20]:
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, "r", encoding="utf-8") as f:
            code = f.read()
        snippet_add(session, name, code)
    yield session
    session.close()


def test_bench_find_matches(benchmark, db_session):
    """Benchmark finding matches in a populated database."""
    benchmark(snippet_find_matches, db_session, _SAMPLE_CODE, top_n=5)
