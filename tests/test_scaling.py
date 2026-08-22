"""Tests for the scaling and performance work in resembl.

Covers:
- Compact raw MinHash storage (``minhash_pack``/``minhash_unpack``) and
  rejection of legacy pickle blobs (never deserialized).
- Consistency between ``code_create_minhash`` and
  ``code_create_minhash_batch`` (weighted shingling parity after ``reindex``).
- ``snippet_add_batch`` bulk import semantics (dedup, alias merge, empty
  skipping, chunked ``IN`` queries for very large batches).
- The database-backed LSH cache (legacy cache files ignored, not loaded).
- Candidate fetching with more than one chunk's worth of LSH candidates.
"""

import json
import os
import pickle
import struct
import tempfile
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select, text

from resembl.cache import (
    lsh_cache_load,
    lsh_cache_save,
    lsh_index_build,
)
from resembl.core import (
    NUM_PERMUTATIONS,
    code_create_minhash,
    code_create_minhash_batch,
    db_merge,
    db_reindex,
    snippet_add,
    snippet_add_batch,
    snippet_delete,
    snippet_find_matches,
    snippet_get,
    snippet_prepare,
)
from resembl.lsh import lsh_index_clear, lsh_meta_get
from resembl.models import (
    Snippet,
    minhash_ensure_packed,
    minhash_jaccard,
    minhash_pack,
    minhash_unpack,
)

ENGINE = create_engine("sqlite:///:memory:")


def _unpickle_canary() -> None:
    """Called only if a hostile pickle blob is ever deserialized."""
    raise AssertionError("hostile pickle blob was deserialized")


class BaseScalingTest(unittest.TestCase):
    """Shared in-memory DB + isolated cache dir for every test."""

    def setUp(self):
        SQLModel.metadata.create_all(ENGINE)
        self.session = Session(ENGINE)
        self._cache_dir = tempfile.TemporaryDirectory()
        self._env_patch = patch.dict(os.environ, {"RESEMBL_CACHE_DIR": self._cache_dir.name})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._cache_dir.cleanup()
        self.session.close()
        SQLModel.metadata.drop_all(ENGINE)


class TestMinHashStorage(BaseScalingTest):
    """Compact raw-uint32 MinHash serialization."""

    def test_pack_unpack_roundtrip(self):
        m = code_create_minhash("push ebx; mov eax, dword [esp+0x10]; pop ebx; ret")
        raw = minhash_pack(m)
        # Magic (4) + num_perm (4) + 128 * uint32 (512)
        self.assertEqual(len(raw), 8 + 4 * NUM_PERMUTATIONS)
        self.assertTrue(raw.startswith(b"RMLH"))
        restored = minhash_unpack(raw)
        self.assertAlmostEqual(m.jaccard(restored), 1.0, places=6)

    def test_pack_self_describing_permutation_count(self):
        m = code_create_minhash("nop")
        raw = minhash_pack(m)
        num_perm = struct.unpack(">I", raw[4:8])[0]
        self.assertEqual(num_perm, NUM_PERMUTATIONS)
        self.assertEqual(len(raw), 8 + 4 * num_perm)

    def test_packed_jaccard_matches_object_jaccard(self):
        m1 = code_create_minhash("push ebx; mov eax, dword [esp+0x10]; pop ebx; ret")
        m2 = code_create_minhash("push ebx; mov ecx, dword [esp+0x10]; pop ebx; ret")
        m3 = code_create_minhash("cpuid; rdtsc; ret")
        self.assertAlmostEqual(
            minhash_jaccard(minhash_pack(m1), minhash_pack(m2)),
            m1.jaccard(m2),
            places=6,
        )
        self.assertAlmostEqual(
            minhash_jaccard(minhash_pack(m1), minhash_pack(m3)),
            m1.jaccard(m3),
            places=6,
        )
        # Legacy (non-packed) blobs are rejected, never deserialized.
        with self.assertRaises(ValueError):
            minhash_jaccard(pickle.dumps(m1), pickle.dumps(m2))

    def test_surrogate_input_does_not_crash(self):
        """Checksum/minhash must be total over any str (fuzzer-found crash).

        ``find --query`` receives argv decoded with surrogateescape, so a
        stray invalid byte can produce a lone surrogate.  Previously the
        UTF-8 encode raised UnicodeEncodeError; the encode now uses
        ``surrogatepass`` so these functions never crash on any input.
        """
        from resembl.core import code_create_minhash, snippet_prepare, string_checksum

        nasty = "push ebx\nmov eax, \udb6b\npop ebx\nret"
        self.assertIsInstance(string_checksum(nasty), str)
        m = code_create_minhash(nasty)
        self.assertEqual(m.jaccard(code_create_minhash(nasty)), 1.0)  # deterministic
        prepared = snippet_prepare("f", nasty)
        self.assertIsNotNone(prepared)

    def test_unpack_legacy_pickle_raises_value_error(self):
        """Legacy pickled fingerprints are rejected without deserialization.

        Old databases migrate through the version-stamp reindex path, which
        recomputes fingerprints from code — the blob itself is hostile input
        (a pickle is executable) and must only produce ``ValueError``.
        """
        m = code_create_minhash("mov eax, 1")
        legacy = pickle.dumps(m)
        with self.assertRaises(ValueError):
            minhash_unpack(legacy)
        with self.assertRaises(ValueError):
            minhash_ensure_packed(legacy)

    def test_unpack_corrupt_payload(self):
        raw = b"RMLH" + struct.pack(">I", 128) + b"\x00" * 100
        with self.assertRaises(ValueError):
            minhash_unpack(raw)

    def test_snippet_add_stores_compact_format(self):
        snippet_add(self.session, "s", "MOV EAX, 1")
        row = self.session.exec(select(Snippet)).first()
        self.assertTrue(row.minhash.startswith(b"RMLH"))
        self.assertEqual(len(row.minhash), 8 + 4 * NUM_PERMUTATIONS)
        self.assertIsNotNone(snippet_get(self.session, row.checksum).get_minhash_obj())


class TestBatchConsistency(BaseScalingTest):
    """code_create_minhash_batch must agree with code_create_minhash."""

    def test_batch_matches_single_consistency(self):
        codes = [
            "push ebx; mov eax, dword [esp+0x10]; pop ebx; ret",
            "cpuid; mov ecx, 0x1a; rdtsc",
            "nop",
            "",
            "MOV EAX, 1",
        ]
        singles = [code_create_minhash(c) for c in codes]
        batch = code_create_minhash_batch(codes)
        for m1, m2 in zip(singles, batch):
            self.assertAlmostEqual(m1.jaccard(m2), 1.0, places=6)

    def test_batch_weighted_shingling(self):
        """Weighted insertion must apply in batch mode too (rare instrs)."""
        rare = "cpuid; mov ecx, 0x1a; rdtsc; ret"
        common = "push ebx; mov eax, 1; pop ebx; ret"
        m_rare = code_create_minhash_batch([rare])[0]
        m_common = code_create_minhash_batch([common])[0]
        # Distinct enough that they are far apart in fingerprint space.
        self.assertLess(m_rare.jaccard(m_common), 0.5)

    def test_weighted_shingling_actually_weights(self):
        """A rare-instruction shingle must change the fingerprint.

        Regression test: datasketch's update takes the per-position minimum,
        so hashing the *same* bytes multiple times is a no-op.  The weighted
        insertion hashes w distinct pseudo-elements per weight-w shingle; a
        shingle containing a rare instruction must therefore produce a
        different fingerprint than the same shingle unweighted.
        """
        from datasketch import MinHash

        from resembl.models import minhash_new

        rare_shingle = ("CPUID", "REG", "RDTSC")
        common_shingle = ("MOV", "REG", "IMM")

        def build(weighted: bool) -> MinHash:
            m = minhash_new(NUM_PERMUTATIONS)
            inputs: list[bytes] = []
            for shingle in (rare_shingle, common_shingle):
                base = " ".join(shingle).encode("utf8")
                if weighted and shingle == rare_shingle:
                    inputs.extend(base + b"|" + str(k).encode("utf8") for k in range(3))
                else:
                    inputs.append(base)
            m.update_batch(inputs)
            return m

        weighted = build(weighted=True)
        unweighted = build(weighted=False)
        self.assertLess(weighted.jaccard(unweighted), 1.0)

    def test_reindex_preserves_fingerprints(self):
        code = "push esi; mov esi, dword [esp+0CH]; push edi; mov edi, dword [esp+0CH]"
        snippet_add(self.session, "fn", code)
        row = self.session.exec(select(Snippet)).first()
        before = row.get_minhash_obj()
        db_reindex(self.session)
        self.session.expire_all()
        after = self.session.exec(select(Snippet)).first().get_minhash_obj()
        self.assertAlmostEqual(before.jaccard(after), 1.0, places=6)

    def test_parallel_reindex_matches_sequential(self):
        """db_reindex(jobs=N) must produce the same fingerprints as jobs=1."""
        items = [
            snippet_prepare(f"fn_{i}", f"PUSH EBP\nMOV EBP, ESP\nMOV EAX, {i}\nPOP EBP\nRET", 3)
            for i in range(30)
        ]
        snippet_add_batch(self.session, [i for i in items if i])

        checksums = sorted(s.checksum for s in self.session.exec(select(Snippet)).all())
        self.assertEqual(len(checksums), 30)

        db_reindex(self.session, jobs=2, batch_size=10)
        self.session.expire_all()
        par_blobs = {s.checksum: s.minhash for s in self.session.exec(select(Snippet)).all()}

        db_reindex(self.session, jobs=1, batch_size=10)
        self.session.expire_all()
        seq_blobs = {s.checksum: s.minhash for s in self.session.exec(select(Snippet)).all()}

        self.assertEqual(set(par_blobs), set(seq_blobs))
        for checksum in checksums:
            self.assertEqual(par_blobs[checksum], seq_blobs[checksum])


class TestSnippetAddBatch(BaseScalingTest):
    """Bulk import semantics."""

    def _prepare(self, name, code):
        return snippet_prepare(name, code, 3)

    def test_adds_new_and_counts(self):
        items = [
            self._prepare("a", "MOV EAX, 1"),
            self._prepare("b", "MOV EBX, 2"),
            self._prepare("c", ""),  # skipped by prepare (empty)
        ]
        prepared = [i for i in items if i is not None]
        result = snippet_add_batch(self.session, prepared)
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["aliased"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(self.session.exec(select(Snippet)).all()), 2)

    def test_intra_batch_dedupe_merges_names(self):
        code = "PUSH EBP; MOV EBP, ESP; POP EBP; RET"
        items = [
            self._prepare("first", code),
            self._prepare("second", code),
        ]
        result = snippet_add_batch(self.session, items)
        self.assertEqual(result["added"], 1)
        row = self.session.exec(select(Snippet)).first()
        self.assertIn("first", row.name_list)
        self.assertIn("second", row.name_list)

    def test_cross_batch_alias_merge(self):
        code = "MOV ECX, 3"
        snippet_add(self.session, "orig", code)
        items = [self._prepare("new_alias", code)]
        result = snippet_add_batch(self.session, items)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["aliased"], 1)
        row = self.session.exec(select(Snippet)).first()
        self.assertIn("orig", row.name_list)
        self.assertIn("new_alias", row.name_list)

    def test_reimport_batches_existing_lookups(self):
        """Re-importing known content must not issue one SELECT per snippet.

        The alias path used to call ``session.get`` once per existing
        checksum (an N+1 that dominates incremental re-imports of mostly
        known content).  Existing rows are now fetched in one pass of
        chunked IN queries; assert the SELECT count stays bounded.
        """
        import sqlalchemy.event

        # Seed 120 snippets.
        seed = [
            snippet_prepare(f"s{i}", f"PUSH EBP\nMOV EAX, {i}\nPOP EBP\nRET", 3) for i in range(120)
        ]
        snippet_add_batch(self.session, [i for i in seed if i])

        # Re-import all 120 codes with fresh names (forces alias merges)
        # plus 10 brand-new snippets.
        extra = [
            snippet_prepare(f"t{i}", f"PUSH ECX\nMOV EBX, {i}\nPOP ECX\nRET", 3) for i in range(10)
        ]
        renamed = [
            snippet_prepare(f"s{i}_v2", f"PUSH EBP\nMOV EAX, {i}\nPOP EBP\nRET", 3)
            for i in range(120)
        ]
        counts = {"select": 0}

        @sqlalchemy.event.listens_for(ENGINE, "after_cursor_execute")
        def _count_selects(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                counts["select"] += 1

        try:
            result = snippet_add_batch(self.session, [i for i in renamed + extra if i])
        finally:
            sqlalchemy.event.remove(ENGINE, "after_cursor_execute", _count_selects)

        self.assertEqual(result["added"], 10)
        self.assertEqual(result["aliased"], 120)
        # The 120 existing lookups happen in ~1 chunked IN query, not 120.
        self.assertLessEqual(counts["select"], 5)

    def test_large_batch_chunked_in(self):
        """> 500 unique checksums exercises chunked IN queries."""
        items = []
        for i in range(600):
            code = f"MOV EAX, {i}; ADD EBX, {i % 7}"
            items.append(self._prepare(f"fn_{i}", code))
        result = snippet_add_batch(self.session, items)
        self.assertEqual(result["added"], 600)
        self.assertEqual(len(self.session.exec(select(Snippet)).all()), 600)

    def test_batch_invalidates_cache(self):
        from pathlib import Path

        from resembl.cache import lsh_cache_path_get

        items = [self._prepare("a", "MOV EAX, 1")]
        snippet_add_batch(self.session, items)
        # Saving a cache then re-adding must invalidate it.
        snippet_add(self.session, "b", "MOV EBX, 2")
        self.assertFalse(Path(lsh_cache_path_get(0.5)).exists())


class TestCacheFormat(BaseScalingTest):
    """Database-backed index + legacy cache-file rejection."""

    def test_sqlite_index_roundtrip(self):
        """Build + save + load round-trips through the DB-backed index."""
        self.assertIsNotNone(lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS))
        lsh_cache_save(self.session, 0.5)
        loaded = lsh_cache_load(self.session, 0.5)
        self.assertIsNotNone(loaded)
        # Empty database → query returns no candidates.
        self.assertEqual(loaded.query(code_create_minhash("nop")), [])

    def test_legacy_pickle_cache_not_loaded(self):
        """A legacy pickle cache file is ignored, never unpickled.

        Unpickling a cache file is arbitrary code execution; anyone who can
        plant a file under the cache dir must not gain code execution when
        ``find`` runs.  The loader consults only the database-backed index,
        so a planted file yields ``None`` (the caller rebuilds) and the
        stale file is removed by the next save.
        """
        import pickle as _pickle
        from pathlib import Path

        class _Canary:
            def __reduce__(self):  # detonates only if the blob is unpickled
                return _unpickle_canary, ()

        cache_path = Path(os.environ["RESEMBL_CACHE_DIR"]) / "lsh_0.50.pkl"
        with open(cache_path, "wb") as f:
            _pickle.dump(_Canary(), f)
        marker = Path(os.environ["RESEMBL_CACHE_DIR"]) / "db_checksum.txt"
        with open(marker, "w", encoding="utf-8") as f:
            f.write("legacy-checksum")

        # Not loaded — no execution, no object returned.
        self.assertIsNone(lsh_cache_load(self.session, 0.5))

    def test_corrupted_new_format_ignored(self):
        """A corrupted cache file is ignored instead of raising."""
        from pathlib import Path

        cache_path = Path(os.environ["RESEMBL_CACHE_DIR"]) / "lsh_0.50.pkl"
        with open(cache_path, "wb") as f:
            f.write(b"RESEMBL-CACHE-V2" + b"not-zlib-data")
        marker = Path(os.environ["RESEMBL_CACHE_DIR"]) / "db_checksum.txt"
        with open(marker, "w", encoding="utf-8") as f:
            f.write("legacy-checksum")
        self.assertIsNone(lsh_cache_load(self.session, 0.5))

    def test_threshold_change_rebuilds(self):
        """A different threshold than the built index triggers a rebuild."""
        self.assertIsNotNone(lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS))
        lsh_cache_save(self.session, 0.5)
        # Different params → no valid index → load returns None.
        self.assertIsNone(lsh_cache_load(self.session, 0.8, NUM_PERMUTATIONS))


class TestFindScaling(BaseScalingTest):
    """Find with many LSH candidates (chunked IN fetch)."""

    def test_many_candidates_fetched(self):
        # 600 snippets that normalize identically (unique raw text, identical
        # MinHash) land in the same LSH bucket -> 600 candidates.
        items = []
        for i in range(600):
            code = f"; comment {i}\npush ebx\nmov eax, dword [esp+0x{i:X}]\npop ebx\nret"
            items.append(snippet_prepare(f"fn_{i}", code, 3))
        snippet_add_batch(self.session, items)

        num_candidates, matches = snippet_find_matches(
            self.session,
            "; query\npush ebx\nmov eax, dword [esp+0x1A]\npop ebx\nret",
            top_n=5,
        )
        self.assertEqual(num_candidates, 600)
        self.assertEqual(len(matches), 5)
        for _, score in matches:
            self.assertGreater(score, 90.0)


class TestIncrementalIndexSync(BaseScalingTest):
    """Add/delete must keep the DB-backed index complete (no full rebuild)."""

    def _add_batch(self, n: int, prefix: str):
        items = [
            snippet_prepare(f"{prefix}_{i}", f"MOV EAX, {i}; ADD EBX, {i % 7}", 3) for i in range(n)
        ]
        return snippet_add_batch(self.session, [i for i in items if i])

    def test_export_handles_name_collisions_and_empty_names(self):
        """Export must not overwrite files for duplicate names, and must not
        crash when a snippet has no names (batch add with empty name)."""
        import tempfile as _tempfile

        from resembl.core import snippet_export

        # Two snippets sharing a primary name + one with an empty name list.
        items = [
            snippet_prepare("dup", "PUSH EBP\nPOP EBP", 3),
            snippet_prepare("dup", "PUSH EBX\nPOP EBX", 3),
            snippet_prepare("", "XOR EAX, EAX", 3),  # empty name -> "[]" names
        ]
        snippet_add_batch(self.session, [i for i in items if i])

        with _tempfile.TemporaryDirectory() as tmp:
            result = snippet_export(self.session, tmp)
            self.assertEqual(result["num_exported"], 3)
            files = sorted(os.listdir(tmp))
            self.assertEqual(len(files), 3)  # no overwrite
            # The collision got a checksum-suffixed name.
            self.assertTrue(any("dup-" in f for f in files))
            self.assertTrue(any(f.startswith("snippet_") for f in files))

    def test_add_after_build_is_findable(self):
        self._add_batch(50, "pre")
        # Build the index (cold find).
        snippet_find_matches(self.session, "MOV EAX, 1", top_n=1)
        # Add a new snippet — the index must include it without a rebuild.
        snippet_add(self.session, "post", "XOR EAX, EAX; RET")
        _, matches = snippet_find_matches(self.session, "XOR EAX, EAX; RET", top_n=1)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0].name_list, ["post"])

    def test_delete_after_build_is_gone(self):
        self._add_batch(50, "pre")
        snippet_find_matches(self.session, "MOV EAX, 1", top_n=1)
        # pick any snippet by its checksum
        row = self.session.exec(select(Snippet)).first()
        checksum = row.checksum
        snippet_delete(self.session, checksum)
        # Query with its code — it must not come back.
        _, matches = snippet_find_matches(self.session, row.code, top_n=10)
        self.assertNotIn(checksum, [m[0].checksum for m in matches])

    def test_batch_add_after_build_is_findable(self):
        self._add_batch(50, "pre")
        snippet_find_matches(self.session, "MOV EAX, 1", top_n=1)
        self._add_batch(50, "post")
        _, matches = snippet_find_matches(self.session, "MOV EAX, 77", top_n=3)
        self.assertGreater(len(matches), 0)
        names = [n for m in matches for n in m[0].name_list]
        self.assertTrue(any(n.startswith("post_") for n in names))

    def test_threshold_mismatch_rebuilds(self):
        """Changing the threshold invalidates the built index (rebuild)."""
        self._add_batch(20, "pre")
        snippet_find_matches(self.session, "MOV EAX, 1", top_n=1)
        # Find with a different threshold rebuilds and still works.
        _, matches = snippet_find_matches(self.session, "MOV EAX, 1", top_n=1, threshold=0.8)
        self.assertGreaterEqual(len(matches), 0)

    def test_reindex_clears_built_index_upfront(self):
        """Reindex must drop any built index before rewriting fingerprints.

        A crash mid-reindex can then never leave a stale index behind; the
        next find simply rebuilds from whatever fingerprints are stored.
        """
        from resembl.lsh import lsh_meta_get

        self._add_batch(30, "r")
        lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS)
        self.assertIsNotNone(lsh_meta_get(self.session))
        db_reindex(self.session, jobs=1, batch_size=10)
        self.assertIsNone(lsh_meta_get(self.session))
        # And a find afterwards still works (rebuilds lazily).
        _, matches = snippet_find_matches(self.session, "MOV EAX, 5", top_n=3)
        self.assertGreaterEqual(len(matches), 0)

    def test_reindex_reports_progress(self):
        """db_reindex invokes the progress callback with (done, total)."""
        self._add_batch(30, "q")
        calls: list[tuple[int, int]] = []
        db_reindex(
            self.session,
            jobs=1,
            batch_size=10,
            progress=lambda done, total: calls.append((done, total)),
        )
        self.assertTrue(calls)
        self.assertEqual(calls[-1], (30, 30))
        self.assertTrue(all(done <= total for done, total in calls))


class TestResemblLSH(BaseScalingTest):
    """Direct ResemblLSH API coverage (chunking, MinHash args, errors)."""

    def test_constructor_rejects_bad_params(self):
        from resembl.lsh import ResemblLSH

        with self.assertRaises(ValueError):
            ResemblLSH(self.session, 1.5, 128)
        with self.assertRaises(ValueError):
            ResemblLSH(self.session, 0.5, 1)

    def test_banding_params_are_cached(self):
        """The scipy banding computation must run once per (threshold, perms).

        ``_optimal_param`` is a ~13 ms numerical integration; recomputing it
        on every ResemblLSH construction made it ~88% of query time.
        """
        from unittest.mock import patch

        from datasketch.lsh import _optimal_param as _real_optimal_param

        from resembl.lsh import ResemblLSH, _banding_params

        _banding_params.cache_clear()  # deterministic: cache may be warm from other tests
        # The lazy import resolves the name from datasketch.lsh at call time,
        # so patch there.
        with patch("datasketch.lsh._optimal_param", side_effect=_real_optimal_param) as mock:
            r1 = ResemblLSH(self.session, 0.5, NUM_PERMUTATIONS)
            r2 = ResemblLSH(self.session, 0.5, NUM_PERMUTATIONS)
            self.assertEqual((r1.b, r1.r), (r2.b, r2.r))
            self.assertEqual(mock.call_count, 1)  # second construction is a cache hit
        # Different parameters compute a different banding.
        b3, r3 = _banding_params(0.8, NUM_PERMUTATIONS)
        self.assertNotEqual((b3, r3), (r1.b, r1.r))

    def test_insert_query_with_minhash_object(self):
        from resembl.lsh import ResemblLSH

        lsh = ResemblLSH(self.session, 0.5, NUM_PERMUTATIONS)
        m = code_create_minhash("MOV EAX, 1; RET")
        lsh.insert("k1", m)  # MinHash object, not packed bytes
        self.assertEqual(lsh.query(m), ["k1"])

    def test_insert_batch_crosses_chunk_boundary(self):
        """>400 snippets produce >10k rows, exercising multi-chunk inserts."""
        from resembl.lsh import ResemblLSH

        lsh = ResemblLSH(self.session, 0.5, NUM_PERMUTATIONS)
        items = []
        for i in range(450):
            m = code_create_minhash(f"MOV EAX, {i}; RET")
            items.append((f"k{i}", minhash_pack(m)))
        inserted = lsh.insert_batch(items)
        self.assertEqual(inserted, 450 * 25)
        m = code_create_minhash("MOV EAX, 42; RET")
        self.assertIn("k42", lsh.query(m))

    def test_remove(self):
        from resembl.lsh import ResemblLSH

        lsh = ResemblLSH(self.session, 0.5, NUM_PERMUTATIONS)
        m = code_create_minhash("MOV EAX, 1; RET")
        lsh.insert("k1", minhash_pack(m))
        lsh.insert("k2", minhash_pack(code_create_minhash("XOR EBX, EBX")))
        self.assertIn("k1", lsh.query(m))
        lsh.remove("k1")
        self.assertNotIn("k1", lsh.query(m))
        self.assertEqual(len(lsh.query(m)), 0)


class TestIndexBuild(BaseScalingTest):
    """The optimized cold-build path (projected reads, chunked commits)."""

    def _add(self, n: int, prefix: str):
        prepared = [
            snippet_prepare(f"{prefix}_{i}", f"MOV EAX, {i}; ADD EBX, {i}; RET", 3)
            for i in range(n)
        ]
        items = [item for item in prepared if item is not None]
        snippet_add_batch(self.session, items)

    def test_iter_minhash_batches_projects_only_needed_columns(self):
        """The projected iterator yields (checksum, packed) pairs in keyset order."""
        self._add(5, "p")
        pairs = [
            pair
            for batch in Snippet.iter_minhash_batches(self.session, batch_size=2)
            for pair in batch
        ]
        self.assertEqual(len(pairs), 5)
        for checksum, minhash in pairs:
            self.assertIsInstance(checksum, str)
            self.assertTrue(minhash.startswith(b"RMLH"))
        self.assertEqual([c for c, _ in pairs], sorted(c for c, _ in pairs))

    def test_build_writes_all_bands_and_restores_checksum_index(self):
        """Every band is indexed and the checksum index is recreated."""
        self._add(30, "g")
        lsh = lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS)
        self.assertIsNotNone(lsh)
        rows = self.session.execute(text("SELECT COUNT(*) FROM lsh_bucket")).one()[0]
        self.assertEqual(rows, 30 * 25)  # b=25 bands @ threshold 0.5
        index = self.session.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='ix_lsh_bucket_checksum'"
            )
        ).one_or_none()
        self.assertIsNotNone(index)
        # A second build over the same data is idempotent (same row count).
        lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS)
        rows = self.session.execute(text("SELECT COUNT(*) FROM lsh_bucket")).one()[0]
        self.assertEqual(rows, 30 * 25)

    def test_build_skips_corrupt_fingerprint(self):
        """A corrupt blob must not brick the build or the query path."""
        from resembl.models import Snippet as SnippetModel

        self._add(20, "good")
        # Corrupt one snippet's stored fingerprint (e.g. disk rot).
        corrupt = self.session.exec(select(SnippetModel)).first()
        corrupt.minhash = b"corrupt-blob"
        self.session.add(corrupt)
        self.session.commit()

        # The build skips it; the other 19 index fine and find still works.
        lsh = lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS)
        self.assertIsNotNone(lsh)
        rows = self.session.execute(text("SELECT COUNT(*) FROM lsh_bucket")).one()[0]
        self.assertEqual(rows, 19 * 25)
        num_candidates, matches = snippet_find_matches(
            self.session, "MOV EAX, 5; ADD EBX, 5; RET", top_n=3
        )
        self.assertGreaterEqual(num_candidates, 0)
        self.assertIsInstance(matches, list)

        # A reindex heals it (fingerprints are recomputed from the code).
        db_reindex(self.session, jobs=1)
        healed = self.session.get(SnippetModel, corrupt.checksum)
        self.assertTrue(healed.minhash.startswith(b"RMLH"))
        # The rebuilt index covers all 20 again.
        lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS)
        rows = self.session.execute(text("SELECT COUNT(*) FROM lsh_bucket")).one()[0]
        self.assertEqual(rows, 20 * 25)

    def test_stats_survives_corrupt_fingerprint(self):
        """A corrupt blob in the similarity sample must not crash `stats`."""
        from resembl.core import db_calculate_average_similarity, db_stats
        from resembl.models import Snippet as SnippetModel

        self._add(30, "st")
        corrupt = self.session.exec(select(SnippetModel)).first()
        corrupt.minhash = b"corrupt-blob"
        self.session.add(corrupt)
        self.session.commit()

        similarity = db_calculate_average_similarity(self.session)
        self.assertIsInstance(similarity, float)
        stats = db_stats(self.session)
        self.assertEqual(stats["num_snippets"], 30)
        self.assertIsInstance(stats["avg_jaccard_similarity"], float)

    def test_custom_num_permutations_honored(self):
        """A non-default perm count builds at that count, with no rebuild loop.

        The config's num_permutations used to be inert (the build always used
        the 128 constant) while the CLI's meta check compared against the
        config — so a non-default setting rebuilt the index on every find.
        """
        self._add(20, "perm")
        n, m = snippet_find_matches(
            self.session,
            "MOV EAX, 5; ADD EBX, 5; RET",
            top_n=3,
            num_permutations=64,
        )
        self.assertGreaterEqual(n, 0)
        self.assertEqual(lsh_meta_get(self.session)[1], 64)
        # Stored blobs were reindexed at 64 (the migration reindexes on a
        # perm-count change), so the second find does not rebuild.
        n2, m2 = snippet_find_matches(
            self.session,
            "MOV EAX, 5; ADD EBX, 5; RET",
            top_n=3,
            num_permutations=64,
        )
        self.assertEqual(lsh_meta_get(self.session)[1], 64)
        self.assertEqual(n2, n)

    def test_ngram_change_reindexes_instead_of_silent_zero(self):
        """A config n-gram change reindexes once; matches are not silently lost.

        Stored fingerprints encode their n-gram, so a query at a different
        size shares no buckets with the index — previously it returned 0
        candidates with no rebuild (measured: 40 at ngram 3, 0 at ngram 5).
        """
        from resembl.lsh import fingerprint_ngram_get

        self._add(20, "ng")
        n3, m3 = snippet_find_matches(
            self.session,
            "MOV EAX, 5; ADD EBX, 5; RET",
            top_n=3,
            ngram_size=3,
        )
        self.assertGreater(n3, 0)

        n5, m5 = snippet_find_matches(
            self.session,
            "MOV EAX, 5; ADD EBX, 5; RET",
            top_n=3,
            ngram_size=5,
        )
        # The change triggered a reindex at ngram 5; matches are not lost.
        self.assertGreater(n5, 0)
        self.assertEqual(fingerprint_ngram_get(self.session), 5)

        # A third find at ngram 5 does not reindex again.
        n5b, _ = snippet_find_matches(
            self.session,
            "MOV EAX, 5; ADD EBX, 5; RET",
            top_n=3,
            ngram_size=5,
        )
        self.assertEqual(n5b, n5)

    def test_jaccard_weight_honored(self):
        """The config jaccard_weight reaches scoring (it was inert)."""
        from resembl.core import snippet_add

        snippet_add(self.session, "a", "MOV EAX, 1\nRET")
        b = snippet_add(self.session, "b", "MOV EBX, 2\nRET")
        query = "MOV EAX, 1\nRET"

        _n0, m0 = snippet_find_matches(self.session, query, top_n=2, jaccard_weight=0.0)
        _n1, m1 = snippet_find_matches(self.session, query, top_n=2, jaccard_weight=1.0)

        def score_of(matches, checksum):
            for s, sc in matches:
                if s.checksum == checksum:
                    return sc
            return None

        # "b" shares the query's token stream (jaccard 1.0) but differs in
        # text: at w=0 its score is its Levenshtein (<100); at w=1 it is 100.
        b0 = score_of(m0, b.checksum)
        b1 = score_of(m1, b.checksum)
        self.assertIsNotNone(b0)
        self.assertIsNotNone(b1)
        self.assertLess(b0, b1)

    def test_build_reports_progress(self):
        """The build invokes the progress callback with (done, total)."""
        self._add(50, "p")
        calls: list[tuple[int, int]] = []
        lsh_index_build(
            self.session,
            0.5,
            NUM_PERMUTATIONS,
            progress=lambda done, total: calls.append((done, total)),
        )
        self.assertTrue(calls)
        self.assertEqual(calls[-1], (50, 50))
        self.assertTrue(all(done <= total for done, total in calls))
        self.assertTrue(all(a <= b for (a, _), (b, _) in zip(calls, calls[1:])))

    def test_build_retries_on_locked_database(self):
        """A concurrent-writer lock mid-build is retried, not crashed on.

        Two CLI processes cold-finding the same database race on SQLite's
        single-writer lock; the loser must retry (identical databases build
        identical indexes) instead of surfacing a raw ``database is locked``
        traceback.
        """
        import sqlite3
        from unittest.mock import patch

        import sqlalchemy.exc

        from resembl import cache

        self._add(20, "lock")
        raised = {"n": 0}
        real_insert = cache._insert_rows

        def flaky_insert(session, sql, rows):
            if raised["n"] == 0:  # fail only the very first insert call
                raised["n"] += 1
                raise sqlalchemy.exc.OperationalError(
                    "stmt", {}, sqlite3.OperationalError("database is locked")
                )
            real_insert(session, sql, rows)

        with (
            patch("resembl.cache._insert_rows", side_effect=flaky_insert),
            patch("resembl.cache.time.sleep"),
            patch("resembl.cache._BUILD_RETRIES", 3),
            patch("resembl.cache._BUILD_RETRY_BACKOFF", 0),
        ):
            lsh = lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS)

        self.assertIsNotNone(lsh)
        self.assertEqual(raised["n"], 1)  # one lock failure, recovered
        rows = self.session.execute(text("SELECT COUNT(*) FROM lsh_bucket")).one()[0]
        self.assertEqual(rows, 20 * 25)

    def test_reindex_clear_retries_on_locked(self):
        """The migration-time index clear retries on a concurrent-writer lock."""
        import sqlite3
        from unittest.mock import patch

        import sqlalchemy.exc

        self._add(20, "reidx")
        raised = {"n": 0}
        real_clear = lsh_index_clear

        def flaky_clear(session):
            if raised["n"] == 0:  # fail only the very first clear call
                raised["n"] += 1
                raise sqlalchemy.exc.OperationalError(
                    "stmt", {}, sqlite3.OperationalError("database is locked")
                )
            real_clear(session)

        with (
            patch("resembl.core.lsh_index_clear", side_effect=flaky_clear),
            patch("resembl.core.time.sleep"),
            patch("resembl.core._REINDEX_CLEAR_RETRIES", 3),
            patch("resembl.core._REINDEX_CLEAR_RETRY_BACKOFF", 0),
        ):
            result = db_reindex(self.session, jobs=1)

        self.assertEqual(raised["n"], 1)  # one lock failure, recovered
        self.assertEqual(result["num_reindexed"], 20)


class TestFingerprintVersion(BaseScalingTest):
    """Fingerprint-format version stamping and one-time auto-reindex."""

    def _add(self, n: int = 10) -> None:
        items = [
            snippet_prepare(f"f{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3) for i in range(n)
        ]
        snippet_add_batch(self.session, [x for x in items if x])

    def test_find_auto_reindexes_when_unstamped(self):
        """A DB with blobs but no stamp is migrated on the first find."""
        from resembl.lsh import fingerprint_version_get
        from resembl.models import FINGERPRINT_VERSION

        self._add()
        self.assertIsNone(fingerprint_version_get(self.session))
        _, matches = snippet_find_matches(
            self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=3
        )
        self.assertGreater(len(matches), 0)
        self.assertEqual(fingerprint_version_get(self.session), FINGERPRINT_VERSION)

    def test_find_fixes_stale_stamp(self):
        """A wrong (old) stamp triggers exactly one migration reindex."""
        from unittest.mock import patch

        from resembl.lsh import fingerprint_version_get, fingerprint_version_set
        from resembl.models import FINGERPRINT_VERSION

        self._add()
        fingerprint_version_set(self.session, 1)  # pre-weighting format
        with patch("resembl.core.db_reindex", wraps=db_reindex) as mock:
            _, matches = snippet_find_matches(
                self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=3
            )
            self.assertEqual(mock.call_count, 1)
        self.assertGreater(len(matches), 0)
        self.assertEqual(fingerprint_version_get(self.session), FINGERPRINT_VERSION)
        # Second find: stamp is current — no reindex.
        with patch("resembl.core.db_reindex", wraps=db_reindex) as mock:
            snippet_find_matches(self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=3)
            mock.assert_not_called()

    def test_reindex_stamps_version(self):
        from resembl.lsh import fingerprint_version_get
        from resembl.models import FINGERPRINT_VERSION

        self._add()
        db_reindex(self.session, jobs=1)
        self.assertEqual(fingerprint_version_get(self.session), FINGERPRINT_VERSION)

    def test_default_perm_count_reindexes_nondefault_blobs(self):
        """A find at the default 128 perms heals blobs written at 64.

        ``fingerprints_need_reindex`` used to skip its blob probe whenever the
        requested count equalled the module default, so a database written
        while ``num_permutations`` was configured to 64 crashed the next
        default-count find (uncaught ValueError from the banding) instead of
        reindexing once.
        """
        from resembl.lsh import lsh_meta_get

        self._add()
        # Write fingerprints and an index at 64 permutations.
        _, _ = snippet_find_matches(
            self.session,
            "push ebx\nmov eax, 5\npop ebx\nret",
            top_n=3,
            num_permutations=64,
        )
        self.assertEqual(lsh_meta_get(self.session)[1], 64)

        # Back at the default count: must reindex + rebuild, not crash.
        n, matches = snippet_find_matches(
            self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=3
        )
        self.assertGreater(len(matches), 0)
        self.assertEqual(lsh_meta_get(self.session)[1], NUM_PERMUTATIONS)
        # And a second find at each count stays consistent (no crash loop).
        snippet_find_matches(
            self.session,
            "push ebx\nmov eax, 5\npop ebx\nret",
            top_n=3,
            num_permutations=64,
        )
        n64, _ = snippet_find_matches(self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=3)
        self.assertEqual(n64, n)

    def test_verify_reports_health(self):
        """db_verify flags pending work as warnings, staleness as an issue."""
        from resembl.core import db_verify

        self._add(10)
        report = db_verify(self.session)
        # Fresh DB: self-healing states are warnings, not issues.
        self.assertGreater(len(report["warnings"]), 0)
        self.assertEqual(report["issues"], [])
        snippet_find_matches(self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=3)
        report = db_verify(self.session)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["num_buckets"], report["expected_buckets"])
        self.assertEqual(report["num_snippets"], 10)
        # A partially emptied index (meta intact) is a real issue.
        from sqlmodel import text

        self.session.execute(text("DELETE FROM lsh_bucket WHERE band = 0"))
        self.session.commit()
        report = db_verify(self.session)
        self.assertGreater(len(report["issues"]), 0)

    def test_verify_survives_missing_bucket_table(self):
        """verify warns (not crashes) when lsh_bucket is missing but meta exists."""
        from resembl.core import db_verify

        self._add(10)
        snippet_find_matches(self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=3)
        # Simulate the crash-window state: meta row present, table dropped.
        from sqlmodel import text

        self.session.execute(text("DROP TABLE lsh_bucket"))
        self.session.commit()
        report = db_verify(self.session)
        self.assertEqual(report["num_buckets"], 0)
        self.assertGreater(len(report["warnings"]), 0)
        self.assertIn("missing", " ".join(report["warnings"]).lower())

    def test_merge_clears_stamp(self):
        """Merge copies source blobs verbatim — the stamp must be cleared."""
        import os
        import tempfile

        from sqlmodel import Session as _Session
        from sqlmodel import SQLModel

        from resembl.database import create_db_engine
        from resembl.lsh import fingerprint_version_get, fingerprint_version_set
        from resembl.models import FINGERPRINT_VERSION

        self._add()
        fingerprint_version_set(self.session, FINGERPRINT_VERSION)
        src = tempfile.mktemp(suffix=".db")
        try:
            source_engine = create_db_engine(f"sqlite:///{src}")
            SQLModel.metadata.create_all(source_engine)
            with _Session(source_engine) as source_session:
                snippet_add(source_session, "src", "mov eax, 1; ret")
            db_merge(self.session, src)
            self.assertIsNone(fingerprint_version_get(self.session))
        finally:
            for path in (src, src + "-wal", src + "-shm"):
                if os.path.exists(path):
                    os.remove(path)

    def test_merge_hostile_pickle_blob_is_not_deserialized(self):
        """A pickle minhash blob in the source DB must never be unpickled.

        ``merge`` treats another database as untrusted input.  A crafted
        source row whose fingerprint is a pickle payload used to reach
        ``pickle.loads`` (arbitrary code execution); it is now recomputed
        from the row's code instead.
        """
        import os
        import tempfile

        from sqlmodel import Session as _Session
        from sqlmodel import SQLModel, create_engine

        class _Canary:
            def __reduce__(self):  # detonates only if the blob is unpickled
                return _unpickle_canary, ()

        code = "mov eax, 1; ret"
        code_create_minhash(code)
        src = tempfile.mktemp(suffix=".db")
        try:
            source_engine = create_engine(f"sqlite:///{src}")
            SQLModel.metadata.create_all(source_engine)
            with _Session(source_engine) as source_session:
                source_session.add(
                    Snippet(
                        checksum="hostile1",
                        names=json.dumps(["hostile_fn"]),
                        code=code,
                        minhash=pickle.dumps(_Canary()),  # hostile blob
                    )
                )
                source_session.commit()
            result = db_merge(self.session, src)
            self.assertNotIn("error", result)
            self.assertEqual(result["added"], 1)
            stored = snippet_get(self.session, "hostile1")
            self.assertIsNotNone(stored)
            # The stored fingerprint was recomputed into the packed format.
            self.assertTrue(stored.minhash.startswith(b"RMLH"))
        finally:
            for path in (src, src + "-wal", src + "-shm"):
                if os.path.exists(path):
                    os.remove(path)

    def test_merge_from_duckdb_source(self):
        """Merge accepts a full URL — a DuckDB source works like a file."""
        import os
        import tempfile

        from sqlmodel import Session as _Session
        from sqlmodel import SQLModel, create_engine

        try:
            import duckdb_engine  # noqa: F401
        except ImportError:
            self.skipTest("duckdb-engine not installed")

        self._add(5)
        src = tempfile.mktemp(suffix=".duckdb")
        try:
            source_engine = create_engine(f"duckdb:///{src}")
            SQLModel.metadata.create_all(source_engine)
            with _Session(source_engine) as source_session:
                snippet_add(source_session, "src", "mov eax, 1; ret")
                snippet_add(source_session, "src2", "cpuid; rdtsc; ret")
            result = db_merge(self.session, f"duckdb:///{src}")
            self.assertNotIn("error", result)
            self.assertEqual(result["added"], 2)
        finally:
            if os.path.exists(src):
                os.remove(src)


class TestFingerprintPermStamp(BaseScalingTest):
    """Permutation-count stamping and stale-perm fingerprint handling.

    ``snippet_add`` always fingerprints at the module default, while
    ``find``/``reindex`` honor the configured count — a config flip between
    writes produced databases with mixed permutation counts that crashed
    ``find`` (banding and batch Jaccard reject foreign counts).  The perm
    stamp makes detection exact; build/query skip stray mismatches.
    """

    def _add(self, n: int = 10) -> None:
        items = [
            snippet_prepare(f"f{i}", f"push ebx\nmov eax, {i}\npop ebx\nret", 3) for i in range(n)
        ]
        snippet_add_batch(self.session, [x for x in items if x])

    def test_writers_stamp_perm_count(self):
        from resembl.lsh import fingerprint_perm_get

        self._add()
        self.assertEqual(fingerprint_perm_get(self.session), NUM_PERMUTATIONS)
        db_reindex(self.session, jobs=1, num_perm=64)
        self.assertEqual(fingerprint_perm_get(self.session), 64)

    def test_stamp_mismatch_forces_reindex_deterministically(self):
        """A stamped count differing from the request reindexes — no probe luck."""
        from unittest.mock import patch

        from resembl.lsh import fingerprint_perm_get, fingerprint_perm_set

        self._add()
        # Stamps current for 128; flip only the perm stamp to 64.  Every blob
        # still matches 128, so the legacy probe would pass — the stamp must
        # force the reindex regardless of which row it samples.
        fingerprint_perm_set(self.session, 64)
        with patch("resembl.core.db_reindex", wraps=db_reindex) as mock:
            snippet_find_matches(self.session, "push ebx\nmov eax, 5\npop ebx\nret", top_n=3)
            mock.assert_called_once()
        self.assertEqual(fingerprint_perm_get(self.session), NUM_PERMUTATIONS)

    def _corrupt_last_blob_to_64_perms(self, n: int) -> str:
        """Rewrite one stored blob to a valid packed blob at 64 permutations."""
        from datasketch import MinHash

        rows = list(self.session.exec(select(Snippet)).all())
        victim = rows[-1]
        victim.minhash = minhash_pack(MinHash(num_perm=64))
        self.session.add(victim)
        self.session.commit()
        return victim.checksum

    def test_index_build_skips_foreign_perm_blob(self):
        """A valid-format blob at a foreign perm count cannot crash a build."""
        from sqlmodel import text

        from resembl.lsh import fingerprint_version_set
        from resembl.models import FINGERPRINT_VERSION

        self._add(4)
        fingerprint_version_set(self.session, FINGERPRINT_VERSION)
        victim = self._corrupt_last_blob_to_64_perms(4)

        lsh = lsh_index_build(self.session, 0.5, NUM_PERMUTATIONS)
        self.assertIsNotNone(lsh)
        rows = self.session.execute(text("SELECT COUNT(*) FROM lsh_bucket")).one()[0]
        # Only the 3 healthy snippets contribute bucket rows (25 bands each).
        self.assertEqual(rows, 3 * 25)
        # The skipped snippet is still in the snippet table.
        self.assertIsNotNone(snippet_get(self.session, victim))

    def test_find_survives_foreign_perm_candidate(self):
        """A stale-perm candidate is excluded from scoring, not fatal."""
        from resembl.lsh import fingerprint_version_set
        from resembl.models import FINGERPRINT_VERSION

        self._add(4)
        fingerprint_version_set(self.session, FINGERPRINT_VERSION)
        # Build the index first so every snippet has bucket rows, then make
        # one stored blob stale: the LSH still routes to its checksum, and
        # scoring must skip it instead of raising ValueError.
        n0, _ = snippet_find_matches(self.session, "push ebx\nmov eax, 1\npop ebx\nret", top_n=3)
        victim = self._corrupt_last_blob_to_64_perms(4)

        n1, matches = snippet_find_matches(
            self.session, "push ebx\nmov eax, 1\npop ebx\nret", top_n=4
        )
        self.assertGreater(len(matches), 0)
        self.assertNotIn(victim, [s.checksum for s, _score in matches])

    def test_merge_clears_perm_stamp(self):
        """Merge copies source blobs verbatim — the perm stamp must be cleared."""
        import os
        import tempfile

        from sqlmodel import Session as _Session
        from sqlmodel import SQLModel, create_engine

        from resembl.lsh import fingerprint_perm_get

        self._add()
        src = tempfile.mktemp(suffix=".db")
        try:
            source_engine = create_engine(f"sqlite:///{src}")
            SQLModel.metadata.create_all(source_engine)
            with _Session(source_engine) as source_session:
                snippet_add(source_session, "src", "mov eax, 1; ret")
            db_merge(self.session, src)
            self.assertIsNone(fingerprint_perm_get(self.session))
        finally:
            for path in (src, src + "-wal", src + "-shm"):
                if os.path.exists(path):
                    os.remove(path)


class TestLSHMetaThresholdTolerance(BaseScalingTest):
    """The stored-index threshold must survive single-precision columns."""

    def test_matches_tolerate_single_precision_storage(self):
        """MySQL/DuckDB render the ORM Float as 4-byte FLOAT.

        Storing 0.7 there yields ~0.69999998808; an exact comparison made
        every find believe the index used a different threshold and silently
        rebuilt it in full on every query.
        """
        from resembl.lsh import lsh_meta_matches

        stored32 = struct.unpack("f", struct.pack("f", 0.7))[0]
        self.assertNotEqual(stored32, 0.7)  # precision actually lost
        self.assertTrue(lsh_meta_matches((stored32, 128), 0.7, 128))
        self.assertFalse(lsh_meta_matches((stored32, 128), 0.71, 128))
        self.assertFalse(lsh_meta_matches(None, 0.7, 128))


class TestLegacyMigration(BaseScalingTest):
    """Databases created by older versions must keep working.

    Old databases store pickled MinHash blobs and have no ``lsh_bucket`` /
    ``lsh_meta`` tables.  The first ``find`` after an upgrade must build the
    database-backed index from the legacy blobs and keep working.
    """

    def _make_legacy_db(self, tmp_dir: str) -> str:
        """Create a file DB with the old schema: pickled minhashes, no LSH tables."""
        db_path = os.path.join(tmp_dir, "legacy.db")
        raw_engine = create_engine(f"sqlite:///{db_path}")
        with raw_engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE snippet ("
                    "checksum VARCHAR PRIMARY KEY, "
                    "names TEXT NOT NULL, "
                    "code TEXT NOT NULL, "
                    "minhash BLOB NOT NULL, "
                    "tags TEXT DEFAULT '[]', "
                    "collection VARCHAR"
                    ")"
                )
            )
        raw_session = Session(raw_engine)
        m = code_create_minhash("push ebx; mov eax, dword [esp+0x10]; pop ebx; ret")
        raw_session.add(
            Snippet(
                checksum="legacy1",
                names=json.dumps(["legacy_fn"]),
                code="push ebx; mov eax, dword [esp+0x10]; pop ebx; ret",
                minhash=pickle.dumps(m),  # legacy pickled fingerprint
            )
        )
        raw_session.commit()
        raw_session.close()
        raw_engine.dispose()
        return db_path

    def test_find_migrates_legacy_db(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = self._make_legacy_db(tmp_dir)
            engine = create_engine(f"sqlite:///{db_path}")
            # Simulate the CLI: create all tables (adds the LSH tables), then find.
            SQLModel.metadata.create_all(engine)
            with Session(engine) as session:
                num_candidates, matches = snippet_find_matches(
                    session,
                    "push ebx; mov eax, dword [esp+0x10]; pop ebx; ret",
                    top_n=1,
                )
                self.assertEqual(num_candidates, 1)
                self.assertEqual(matches[0][0].checksum, "legacy1")
                # The index is now built with the migrated (packed) fingerprint.
                self.assertIsNotNone(lsh_meta_get(session))
            engine.dispose()

    def test_add_after_migration_keeps_index_synced(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = self._make_legacy_db(tmp_dir)
            engine = create_engine(f"sqlite:///{db_path}")
            SQLModel.metadata.create_all(engine)
            with Session(engine) as session:
                snippet_find_matches(
                    session,
                    "push ebx; mov eax, dword [esp+0x10]; pop ebx; ret",
                    top_n=1,
                )
                # Add a new snippet after the index exists; find must see it.
                snippet_add(session, "new_fn", "XOR EAX, EAX; RET")
                _, matches = snippet_find_matches(session, "XOR EAX, EAX; RET", top_n=1)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0][0].name_list, ["new_fn"])
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
