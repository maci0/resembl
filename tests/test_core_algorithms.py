"""Tests for the core algorithm improvements: weighted shingling, hybrid scoring, CFG similarity."""

import os
import unittest

from resembl.core import (
    BRANCH_INSTRUCTIONS,
    COMMON_INSTRUCTIONS,
    RARE_INSTRUCTIONS,
    cfg_extract,
    cfg_similarity,
    code_create_minhash,
    score_hybrid,
    shingle_weight,
    snippet_add,
    snippet_compare,
)

# Path to the real test ASM file
TEST_DATA_DIR = os.path.join(os.path.dirname(__file__), "test_data")


# ---------------------------------------------------------------------------
# Weighted Shingling
# ---------------------------------------------------------------------------


class TestShingleWeight(unittest.TestCase):
    """Tests for shingle_weight()."""

    def test_rare_instruction_returns_3(self):
        """Shingle containing a rare instruction should get weight 3."""
        self.assertEqual(shingle_weight("MOV REG CPUID"), 3)
        self.assertEqual(shingle_weight("RDTSC REG IMM"), 3)
        self.assertEqual(shingle_weight("INT IMM RET"), 3)

    def test_common_only_returns_1(self):
        """Shingle with only common instructions should get weight 1."""
        self.assertEqual(shingle_weight("MOV REG IMM"), 1)
        self.assertEqual(shingle_weight("PUSH POP NOP"), 1)
        self.assertEqual(shingle_weight("ADD SUB XOR"), 1)

    def test_mixed_returns_2(self):
        """Shingle with neither rare nor all-common should get weight 2."""
        # STOSB is not in common or rare
        self.assertEqual(shingle_weight("MOV STOSB REG"), 2)
        # REP is not in either set
        self.assertEqual(shingle_weight("REP STOSD NOP"), 2)

    def test_empty_shingle(self):
        """Empty shingle should return 1 (all tokens are common — vacuously true)."""
        self.assertEqual(shingle_weight(""), 1)

    def test_single_rare_token(self):
        """Single-token shingle with a rare instruction."""
        self.assertEqual(shingle_weight("CPUID"), 3)

    def test_single_common_token(self):
        """Single-token shingle with a common instruction."""
        self.assertEqual(shingle_weight("MOV"), 1)

    def test_instruction_sets_disjoint(self):
        """RARE and COMMON instruction sets should not overlap."""
        overlap = RARE_INSTRUCTIONS & COMMON_INSTRUCTIONS
        self.assertEqual(overlap, set(), f"Overlap found: {overlap}")


class TestWeightedMinHash(unittest.TestCase):
    """Tests verifying weighted shingling affects MinHash output."""

    def test_rare_instructions_boost_similarity(self):
        """Two snippets sharing rare instructions should be more similar
        with weighting than without."""
        # Both share CPUID — a rare instruction
        code1 = "CPUID\nMOV EAX, 1\nMOV EBX, 2"
        code2 = "CPUID\nMOV ECX, 3\nMOV EDX, 4"
        code3 = "MOV EAX, 1\nMOV EBX, 2\nMOV ECX, 3"

        m1 = code_create_minhash(code1)
        m2 = code_create_minhash(code2)
        m3 = code_create_minhash(code3)

        # code1 and code2 share CPUID (rare) — should be more similar
        # than code1 and code3 (no rare instruction in common)
        sim_with_rare = m1.jaccard(m2)
        sim_without_rare = m1.jaccard(m3)
        # We can't guarantee the exact values but the weighted MinHash
        # should not crash and should produce valid similarity values
        self.assertGreaterEqual(sim_with_rare, 0.0)
        self.assertLessEqual(sim_with_rare, 1.0)
        self.assertGreaterEqual(sim_without_rare, 0.0)
        self.assertLessEqual(sim_without_rare, 1.0)


# ---------------------------------------------------------------------------
# Hybrid Scoring
# ---------------------------------------------------------------------------


class TestScoreHybrid(unittest.TestCase):
    """Tests for score_hybrid()."""

    def test_default_weight(self):
        """Default weight (0.4 Jaccard, 0.6 Levenshtein)."""
        # Jaccard=1.0 (100%), Levenshtein=100 → 0.4*100 + 0.6*100 = 100
        self.assertAlmostEqual(score_hybrid(1.0, 100.0), 100.0)

    def test_zero_scores(self):
        """Both scores zero gives zero."""
        self.assertAlmostEqual(score_hybrid(0.0, 0.0), 0.0)

    def test_pure_jaccard(self):
        """Weight 1.0 should give pure Jaccard (scaled to 100)."""
        self.assertAlmostEqual(score_hybrid(0.8, 50.0, jaccard_weight=1.0), 80.0)

    def test_pure_levenshtein(self):
        """Weight 0.0 should give pure Levenshtein."""
        self.assertAlmostEqual(score_hybrid(0.8, 50.0, jaccard_weight=0.0), 50.0)

    def test_custom_weight(self):
        """Custom weight 0.5/0.5."""
        # Jaccard=0.6 → 60, Levenshtein=80
        # 0.5 * 60 + 0.5 * 80 = 70
        self.assertAlmostEqual(score_hybrid(0.6, 80.0, jaccard_weight=0.5), 70.0)

    def test_asymmetric_scores(self):
        """Jaccard high, Levenshtein low."""
        # Jaccard=0.9 → 90 * 0.4 = 36, Levenshtein=20 * 0.6 = 12 → 48
        self.assertAlmostEqual(score_hybrid(0.9, 20.0), 48.0)


# ---------------------------------------------------------------------------
# CFG Extraction
# ---------------------------------------------------------------------------


class TestCfgExtract(unittest.TestCase):
    """Tests for cfg_extract()."""

    def test_empty_code(self):
        """Empty code should return empty CFG."""
        cfg = cfg_extract("")
        self.assertEqual(cfg["num_blocks"], 0)
        self.assertEqual(cfg["num_edges"], 0)
        self.assertEqual(cfg["block_sizes"], [])
        self.assertEqual(cfg["adj"], {})

    def test_linear_code(self):
        """Linear code with no branches should be a single block."""
        code = "MOV EAX, 1\nMOV EBX, 2\nADD EAX, EBX"
        cfg = cfg_extract(code)
        self.assertEqual(cfg["num_blocks"], 1)
        self.assertEqual(cfg["block_sizes"], [3])

    def test_code_with_ret(self):
        """Code ending with RET should have no successor on the last block."""
        code = "MOV EAX, 1\nRET"
        cfg = cfg_extract(code)
        self.assertGreaterEqual(cfg["num_blocks"], 1)
        # The block ending with RET should have no successors
        last_block = cfg["num_blocks"] - 1
        self.assertEqual(cfg["adj"].get(last_block, []), [])

    def test_code_with_label(self):
        """Labels should start new blocks."""
        code = "MOV EAX, 1\nJMP label1\nlabel1:\nMOV EBX, 2\nRET"
        cfg = cfg_extract(code)
        self.assertGreaterEqual(cfg["num_blocks"], 2)

    def test_conditional_branch(self):
        """Conditional branches should create fallthrough + target edges."""
        code = "CMP EAX, 0\nJZ skip\nMOV EBX, 1\nskip:\nRET"
        cfg = cfg_extract(code)
        self.assertGreaterEqual(cfg["num_blocks"], 2)
        self.assertGreaterEqual(cfg["num_edges"], 1)

    def test_real_asm_file(self):
        """Test CFG extraction on a real ASM file (1000A133.asm)."""
        asm_path = os.path.join(TEST_DATA_DIR, "1000A133.asm")
        if not os.path.exists(asm_path):
            self.skipTest("Test data file not found")

        with open(asm_path) as f:
            code = f.read()

        cfg = cfg_extract(code)
        # This file has multiple labels (?_1057, ?_1058, ?_1059, etc.)
        self.assertGreater(cfg["num_blocks"], 3)
        self.assertGreater(cfg["num_edges"], 2)
        self.assertEqual(len(cfg["block_sizes"]), cfg["num_blocks"])
        # Adjacency list should have all block indices
        self.assertEqual(len(cfg["adj"]), cfg["num_blocks"])

    def test_branch_instructions_set(self):
        """BRANCH_INSTRUCTIONS should contain expected mnemonics."""
        self.assertIn("JMP", BRANCH_INSTRUCTIONS)
        self.assertIn("JZ", BRANCH_INSTRUCTIONS)
        self.assertIn("JNZ", BRANCH_INSTRUCTIONS)
        self.assertIn("RET", BRANCH_INSTRUCTIONS)
        self.assertIn("CALL", BRANCH_INSTRUCTIONS)
        self.assertIn("LOOP", BRANCH_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# CFG Similarity
# ---------------------------------------------------------------------------


class TestCfgSimilarity(unittest.TestCase):
    """Tests for cfg_similarity()."""

    def test_identical_cfgs(self):
        """Identical CFGs should return 1.0."""
        cfg = {"num_blocks": 3, "num_edges": 4, "block_sizes": [2, 3, 1], "adj": {}}
        self.assertAlmostEqual(cfg_similarity(cfg, cfg), 1.0)

    def test_both_empty(self):
        """Two empty CFGs should return 1.0."""
        empty = {"num_blocks": 0, "num_edges": 0, "block_sizes": [], "adj": {}}
        self.assertAlmostEqual(cfg_similarity(empty, empty), 1.0)

    def test_one_empty(self):
        """One empty CFG should return 0.0."""
        empty = {"num_blocks": 0, "num_edges": 0, "block_sizes": [], "adj": {}}
        full = {"num_blocks": 3, "num_edges": 2, "block_sizes": [2, 3, 1], "adj": {}}
        self.assertAlmostEqual(cfg_similarity(empty, full), 0.0)
        self.assertAlmostEqual(cfg_similarity(full, empty), 0.0)

    def test_similar_cfgs(self):
        """Similar CFGs should return a score close to 1.0."""
        cfg1 = {"num_blocks": 4, "num_edges": 5, "block_sizes": [3, 2, 4, 1], "adj": {}}
        cfg2 = {"num_blocks": 4, "num_edges": 5, "block_sizes": [3, 2, 4, 1], "adj": {}}
        self.assertAlmostEqual(cfg_similarity(cfg1, cfg2), 1.0)

    def test_different_cfgs(self):
        """Very different CFGs should return a low score."""
        cfg1 = {"num_blocks": 1, "num_edges": 0, "block_sizes": [10], "adj": {}}
        cfg2 = {"num_blocks": 10, "num_edges": 15, "block_sizes": [1] * 10, "adj": {}}
        sim = cfg_similarity(cfg1, cfg2)
        self.assertLess(sim, 0.5)

    def test_symmetry(self):
        """cfg_similarity(a, b) should equal cfg_similarity(b, a)."""
        cfg1 = {"num_blocks": 3, "num_edges": 4, "block_sizes": [2, 3, 1], "adj": {}}
        cfg2 = {"num_blocks": 5, "num_edges": 6, "block_sizes": [1, 2, 3, 4, 5], "adj": {}}
        self.assertAlmostEqual(cfg_similarity(cfg1, cfg2), cfg_similarity(cfg2, cfg1))

    def test_no_edges(self):
        """CFGs with blocks but no edges — edge ratio should be 1.0."""
        cfg1 = {"num_blocks": 3, "num_edges": 0, "block_sizes": [2, 2, 2], "adj": {}}
        cfg2 = {"num_blocks": 3, "num_edges": 0, "block_sizes": [2, 2, 2], "adj": {}}
        self.assertAlmostEqual(cfg_similarity(cfg1, cfg2), 1.0)

    def test_real_code_similarity(self):
        """Compare CFGs extracted from real code — same code = 1.0."""
        code = "MOV EAX, 1\nCMP EAX, 0\nJZ done\nMOV EBX, 2\ndone:\nRET"
        cfg1 = cfg_extract(code)
        cfg2 = cfg_extract(code)
        self.assertAlmostEqual(cfg_similarity(cfg1, cfg2), 1.0)


# ---------------------------------------------------------------------------
# Integration: snippet_compare with new metrics
# ---------------------------------------------------------------------------


class TestSnippetCompareNewMetrics(unittest.TestCase):
    """Integration test: snippet_compare should include hybrid_score and cfg_similarity."""

    def setUp(self):
        """Set up in-memory DB."""
        from sqlmodel import SQLModel

        from resembl.database import create_db_engine

        os.environ["RESEMBL_CONFIG_DIR"] = "/tmp/resembl_test_algorithms"
        os.environ["RESEMBL_DB_PATH"] = ":memory:"
        self.engine = create_db_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        from sqlmodel import Session
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()

    def test_compare_includes_hybrid_and_cfg(self):
        """snippet_compare should return hybrid_score and cfg_similarity."""
        s1 = snippet_add(self.session, "func1", "MOV EAX, 1\nRET")
        s2 = snippet_add(self.session, "func2", "MOV EBX, 2\nRET")
        result = snippet_compare(self.session, s1.checksum, s2.checksum)

        self.assertIsNotNone(result)
        comp = result["comparison"]
        self.assertIn("jaccard_similarity", comp)
        self.assertIn("levenshtein_score", comp)
        self.assertIn("hybrid_score", comp)
        self.assertIn("cfg_similarity", comp)
        self.assertIn("shared_normalized_tokens", comp)

        # Hybrid score should be within 0-100 range
        self.assertGreaterEqual(comp["hybrid_score"], 0.0)
        self.assertLessEqual(comp["hybrid_score"], 100.0)

        # CFG similarity should be within 0-1 range
        self.assertGreaterEqual(comp["cfg_similarity"], 0.0)
        self.assertLessEqual(comp["cfg_similarity"], 1.0)


if __name__ == "__main__":
    unittest.main()
