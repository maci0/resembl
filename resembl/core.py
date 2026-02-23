"""Core functions for tokenizing and comparing assembly snippets.

This module provides:
- Assembly code tokenization and normalization (multi-arch)
- MinHash / LSH-based similarity matching
- Snippet CRUD with checksum-based deduplication
- Collection grouping, tagging, and versioning
- Database merge with independent name/tag reconciliation
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import random
import re
import time

from datasketch import MinHash
from pygments.lexers.asm import NasmLexer
from pygments.token import Comment, Name, Number, Punctuation, Text
from rapidfuzz import fuzz, process
from sqlmodel import Session, select, text

from .cache import lsh_cache_invalidate, lsh_cache_load, lsh_cache_save, lsh_index_build
from .models import Collection, Snippet, SnippetVersion

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of permutation functions for MinHash (higher = more accurate, slower).
NUM_PERMUTATIONS = 128

#: Default LSH similarity threshold for candidate filtering.
LSH_THRESHOLD = 0.5

# Reuse a single Pygments lexer instance across all calls.
lexer = NasmLexer()

# A set of common register names to assist the lexer
REGISTERS = {
    "ah",
    "al",
    "ax",
    "bh",
    "bl",
    "bp",
    "bx",
    "ch",
    "cl",
    "cr0",
    "cr2",
    "cr3",
    "cr4",
    "cs",
    "cx",
    "dh",
    "di",
    "dl",
    "dr0",
    "dr1",
    "dr2",
    "dr3",
    "dr6",
    "dr7",
    "ds",
    "dx",
    "eax",
    "ebp",
    "ebx",
    "ecx",
    "edi",
    "edx",
    "eflags",
    "eip",
    "es",
    "esi",
    "esp",
    "fs",
    "gs",
    "rax",
    "rbp",
    "rbx",
    "rcx",
    "rdi",
    "rdx",
    "rip",
    "rsi",
    "rsp",
    "si",
    "sp",
    "ss",
    "st0",
    "st1",
    "st2",
    "st3",
    "st4",
    "st5",
    "st6",
    "st7",
    "xmm0",
    "xmm1",
    "xmm2",
    "xmm3",
    "xmm4",
    "xmm5",
    "xmm6",
    "xmm7",
    "ymm0",
    "ymm1",
    "ymm2",
    "ymm3",
    "ymm4",
    "ymm5",
    "ymm6",
    "ymm7",
    "r8",
    "r9",
    "r10",
    "r11",
    "r12",
    "r13",
    "r14",
    "r15",
    "r8d",
    "r9d",
    "r10d",
    "r11d",
    "r12d",
    "r13d",
    "r14d",
    "r15d",
    "r8w",
    "r9w",
    "r10w",
    "r11w",
    "r12w",
    "r13w",
    "r14w",
    "r15w",
    "r8b",
    "r9b",
    "r10b",
    "r11b",
    "r12b",
    "r13b",
    "r14b",
    "r15b",
}

# ARM registers (AArch32 general-purpose + AArch64 general-purpose + NEON/FP)
ARM_REGISTERS = {
    # AArch32 general purpose
    "r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7",
    "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15",
    "sp", "lr", "pc", "cpsr", "spsr", "fpscr",
    # AArch64 general purpose
    "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7",
    "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15",
    "x16", "x17", "x18", "x19", "x20", "x21", "x22", "x23",
    "x24", "x25", "x26", "x27", "x28", "x29", "x30",
    "w0", "w1", "w2", "w3", "w4", "w5", "w6", "w7",
    "w8", "w9", "w10", "w11", "w12", "w13", "w14", "w15",
    "w16", "w17", "w18", "w19", "w20", "w21", "w22", "w23",
    "w24", "w25", "w26", "w27", "w28", "w29", "w30",
    "xzr", "wzr",
    # NEON / FP
    "d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7",
    "d8", "d9", "d10", "d11", "d12", "d13", "d14", "d15",
    "q0", "q1", "q2", "q3", "q4", "q5", "q6", "q7",
    "q8", "q9", "q10", "q11", "q12", "q13", "q14", "q15",
    "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
    "s8", "s9", "s10", "s11", "s12", "s13", "s14", "s15",
}

# MIPS registers (numeric and ABI names)
MIPS_REGISTERS = {
    "$0", "$1", "$2", "$3", "$4", "$5", "$6", "$7",
    "$8", "$9", "$10", "$11", "$12", "$13", "$14", "$15",
    "$16", "$17", "$18", "$19", "$20", "$21", "$22", "$23",
    "$24", "$25", "$26", "$27", "$28", "$29", "$30", "$31",
    "$zero", "$at", "$v0", "$v1", "$a0", "$a1", "$a2", "$a3",
    "$t0", "$t1", "$t2", "$t3", "$t4", "$t5", "$t6", "$t7",
    "$t8", "$t9", "$s0", "$s1", "$s2", "$s3", "$s4", "$s5",
    "$s6", "$s7", "$k0", "$k1", "$gp", "$sp", "$fp", "$ra",
    "$hi", "$lo",
    # FP
    "$f0", "$f1", "$f2", "$f3", "$f4", "$f5", "$f6", "$f7",
    "$f8", "$f9", "$f10", "$f11", "$f12", "$f13", "$f14", "$f15",
    "$f16", "$f17", "$f18", "$f19", "$f20", "$f21", "$f22", "$f23",
    "$f24", "$f25", "$f26", "$f27", "$f28", "$f29", "$f30", "$f31",
}

# RISC-V registers (x-names and ABI names)
RISCV_REGISTERS = {
    "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7",
    "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15",
    "x16", "x17", "x18", "x19", "x20", "x21", "x22", "x23",
    "x24", "x25", "x26", "x27", "x28", "x29", "x30", "x31",
    "zero", "ra", "gp", "tp",
    "t0", "t1", "t2", "t3", "t4", "t5", "t6",
    "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
    "s8", "s9", "s10", "s11",
    "a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7",
    # FP
    "f0", "f1", "f2", "f3", "f4", "f5", "f6", "f7",
    "f8", "f9", "f10", "f11", "f12", "f13", "f14", "f15",
    "f16", "f17", "f18", "f19", "f20", "f21", "f22", "f23",
    "f24", "f25", "f26", "f27", "f28", "f29", "f30", "f31",
    "ft0", "ft1", "ft2", "ft3", "ft4", "ft5", "ft6", "ft7",
    "ft8", "ft9", "ft10", "ft11",
    "fs0", "fs1", "fs2", "fs3", "fs4", "fs5", "fs6", "fs7",
    "fs8", "fs9", "fs10", "fs11",
    "fa0", "fa1", "fa2", "fa3", "fa4", "fa5", "fa6", "fa7",
}

# Combined register set for multi-architecture normalization.
# Used by the tokenizer to replace any register token with the placeholder "REG",
# ensuring that register renaming does not affect similarity scoring.
ALL_REGISTERS = REGISTERS | ARM_REGISTERS | MIPS_REGISTERS | RISCV_REGISTERS

#: System, privileged, or uncommon instructions that are highly distinctive.
#: Shingles containing these get boosted weight during MinHash construction.
RARE_INSTRUCTIONS = {
    "CPUID", "RDTSC", "RDTSCP", "RDRAND", "RDSEED", "XGETBV",
    "VMCALL", "VMLAUNCH", "VMRESUME", "VMXOFF",
    "SYSENTER", "SYSEXIT", "SYSCALL", "SYSRET",
    "INT", "IRET", "IRETD", "IRETQ",
    "EMMS", "WBINVD", "INVLPG", "INVD",
    "SGDT", "LGDT", "SLDT", "LLDT", "LIDT", "SIDT",
    "STR", "LTR", "LMSW", "CLTS",
    "MONITOR", "MWAIT",
    "HLT", "RSM", "UD2",
    "RDMSR", "WRMSR", "RDPMC",
}

#: The most common x86 instructions. Shingles composed entirely of these
#: receive reduced weight (1×) to avoid drowning out distinctive patterns.
COMMON_INSTRUCTIONS = {
    "MOV", "PUSH", "POP", "NOP", "LEA",
    "ADD", "SUB", "XOR", "CMP", "AND", "OR", "NOT", "NEG",
    "JMP", "CALL", "RET", "RETN",
    "TEST", "INC", "DEC",
    "SHL", "SHR", "SAR", "SAL",
    "REG", "IMM", "MEM_SIZE", "LABEL",  # normalized placeholders
}

#: Branch / jump mnemonics used by CFG extraction to identify basic-block
#: boundaries (terminators).
BRANCH_INSTRUCTIONS = {
    "JMP", "JZ", "JNZ", "JE", "JNE",
    "JG", "JGE", "JL", "JLE",
    "JA", "JAE", "JB", "JBE",
    "JO", "JNO", "JS", "JNS", "JP", "JNP",
    "JCXZ", "JECXZ", "JRCXZ",
    "LOOP", "LOOPZ", "LOOPNZ", "LOOPE", "LOOPNE",
    "RET", "RETN", "RETF",
    "CALL",  # not a terminator per-se, but starts a new edge
}


# ---------------------------------------------------------------------------
# Weighted Shingling
# ---------------------------------------------------------------------------


def shingle_weight(shingle: str) -> int:
    """Return the insertion weight for a shingle.

    - **3** if the shingle contains at least one rare instruction.
    - **1** if every token in the shingle is a common instruction.
    - **2** otherwise (the default).

    Higher weight means the shingle is inserted multiple times into the
    MinHash, increasing its probability of being selected as a minimum
    hash value and thus boosting its influence on similarity.
    """
    tokens = shingle.split()
    has_rare = any(t in RARE_INSTRUCTIONS for t in tokens)
    if has_rare:
        return 3
    all_common = all(t in COMMON_INSTRUCTIONS for t in tokens)
    if all_common:
        return 1
    return 2


# ---------------------------------------------------------------------------
# Hybrid Scoring
# ---------------------------------------------------------------------------


def score_hybrid(
    jaccard: float, levenshtein: float, jaccard_weight: float = 0.4
) -> float:
    """Combine Jaccard (0–1) and Levenshtein (0–100) into a single 0–100 score.

    ``jaccard_weight`` controls the balance:
    - 0.0 = pure Levenshtein
    - 1.0 = pure Jaccard
    - 0.4 (default) = 40 % Jaccard + 60 % Levenshtein
    """
    return (jaccard * 100 * jaccard_weight) + (levenshtein * (1 - jaccard_weight))


# ---------------------------------------------------------------------------
# CFG Extraction & Similarity
# ---------------------------------------------------------------------------


def cfg_extract(code: str) -> dict:
    """Extract a simplified control-flow graph from assembly code.

    Parses line-by-line, splitting at labels and branch instructions to
    identify basic blocks.  Returns a dict with:

    - ``num_blocks``: number of basic blocks
    - ``num_edges``: number of control-flow edges
    - ``block_sizes``: list of instruction counts per block
    - ``adj``: adjacency list (block index → list of successor indices)
    """
    lines = [l.strip() for l in code.splitlines() if l.strip()]
    if not lines:
        return {"num_blocks": 0, "num_edges": 0, "block_sizes": [], "adj": {}}

    blocks: list[list[str]] = []  # each block is a list of instruction lines
    current_block: list[str] = []
    label_to_block: dict[str, int] = {}  # label name → block index

    for line in lines:
        # Strip comments (everything after ';')
        if ";" in line:
            line = line[: line.index(";")].strip()
        if not line:
            continue

        # Detect label (line starts with a label token ending in ':')
        stripped = line.lstrip()
        is_label = ":" in stripped and not stripped.startswith(";")
        label_name = None
        if is_label:
            # Extract the label name (part before the first ':')
            label_name = stripped.split(":")[0].strip()
            # If there's content after the label on the same line, treat as
            # part of the new block
            remainder = stripped[stripped.index(":") + 1 :].strip()

            # Start a new block at every label
            if current_block:
                blocks.append(current_block)
                current_block = []
            label_to_block[label_name] = len(blocks)
            if remainder:
                current_block.append(remainder)
            continue

        current_block.append(stripped)

        # Check if this instruction is a branch (terminates the block)
        mnemonic = stripped.split()[0].upper() if stripped.split() else ""
        if mnemonic in BRANCH_INSTRUCTIONS:
            blocks.append(current_block)
            current_block = []

    # Don't forget the final block
    if current_block:
        blocks.append(current_block)

    # Build adjacency list
    adj: dict[int, list[int]] = {i: [] for i in range(len(blocks))}
    for i, block in enumerate(blocks):
        if not block:
            # Empty block (label-only) falls through
            if i + 1 < len(blocks):
                adj[i].append(i + 1)
            continue

        last_line = block[-1]
        mnemonic = last_line.split()[0].upper() if last_line.split() else ""

        if mnemonic in {"RET", "RETN", "RETF"}:
            # No successor — function exit
            pass
        elif mnemonic == "JMP":
            # Unconditional jump — try to resolve target
            parts = last_line.split()
            if len(parts) > 1:
                target = parts[-1].strip()
                if target in label_to_block:
                    adj[i].append(label_to_block[target])
            # No fallthrough for unconditional jumps
        elif mnemonic in BRANCH_INSTRUCTIONS:
            # Conditional branch — both fallthrough and target
            if i + 1 < len(blocks):
                adj[i].append(i + 1)
            parts = last_line.split()
            if len(parts) > 1:
                target = parts[-1].strip()
                if target in label_to_block:
                    adj[i].append(label_to_block[target])
        else:
            # Non-branch — fallthrough to next block
            if i + 1 < len(blocks):
                adj[i].append(i + 1)

    num_edges = sum(len(succs) for succs in adj.values())
    block_sizes = [len(b) for b in blocks]

    return {
        "num_blocks": len(blocks),
        "num_edges": num_edges,
        "block_sizes": block_sizes,
        "adj": adj,
    }


def cfg_similarity(cfg1: dict, cfg2: dict) -> float:
    """Compute structural similarity between two CFGs (0.0–1.0).

    Combines three sub-metrics with equal weight:

    1. **Block-count ratio** – min/max of block counts.
    2. **Edge-count ratio** – min/max of edge counts.
    3. **Block-size histogram cosine similarity** – how similar the
       distribution of instructions per block is.
    """
    b1, b2 = cfg1["num_blocks"], cfg2["num_blocks"]
    e1, e2 = cfg1["num_edges"], cfg2["num_edges"]

    if b1 == 0 and b2 == 0:
        return 1.0  # Both empty
    if b1 == 0 or b2 == 0:
        return 0.0  # One is empty

    # Sub-metric 1: block count ratio
    block_ratio = min(b1, b2) / max(b1, b2)

    # Sub-metric 2: edge count ratio
    if e1 == 0 and e2 == 0:
        edge_ratio = 1.0
    elif e1 == 0 or e2 == 0:
        edge_ratio = 0.0
    else:
        edge_ratio = min(e1, e2) / max(e1, e2)

    # Sub-metric 3: block-size histogram cosine similarity
    sizes1 = cfg1["block_sizes"]
    sizes2 = cfg2["block_sizes"]
    max_size = max(max(sizes1, default=0), max(sizes2, default=0)) + 1

    hist1 = [0] * max_size
    hist2 = [0] * max_size
    for s in sizes1:
        hist1[s] += 1
    for s in sizes2:
        hist2[s] += 1

    dot = sum(a * b for a, b in zip(hist1, hist2))
    mag1 = sum(a * a for a in hist1) ** 0.5
    mag2 = sum(b * b for b in hist2) ** 0.5

    if mag1 == 0 or mag2 == 0:
        cosine_sim = 0.0
    else:
        cosine_sim = dot / (mag1 * mag2)

    return (block_ratio + edge_ratio + cosine_sim) / 3.0


# ---------------------------------------------------------------------------
# Tokenization & Hashing
# ---------------------------------------------------------------------------


def string_normalize(code_snippet: str) -> str:
    """Normalize an assembly snippet and return a canonical string."""
    tokens = lexer.get_tokens(code_snippet)
    # Join tokens, but only if they are not comments or pure whitespace
    return " ".join(
        value for ttype, value in tokens if ttype not in Comment and ttype != Text
    ).strip()


def snippet_name_add(
    session: Session, checksum: str, new_name: str, quiet: bool = False
) -> Snippet | None:
    """Add a new name to an existing snippet."""
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    name_list = snippet.name_list
    if new_name in name_list:
        if not quiet:
            logger.error("Name '%s' already exists for this snippet.", new_name)
        return None

    name_list.append(new_name)
    snippet.names = json.dumps(name_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def snippet_name_remove(
    session: Session, checksum: str, name_to_remove: str, quiet: bool = False
) -> Snippet | None:
    """Remove a name from a snippet."""
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    name_list = snippet.name_list
    if name_to_remove not in name_list:
        if not quiet:
            logger.error("Name '%s' not found for this snippet.", name_to_remove)
        return None

    if len(name_list) == 1:
        if not quiet:
            logger.error("Cannot remove the last name from a snippet.")
        return None

    name_list.remove(name_to_remove)
    snippet.names = json.dumps(name_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def snippet_tag_add(session: Session, checksum: str, tag: str, quiet: bool = False) -> Snippet | None:
    """Add a tag to a snippet (idempotent — adding an existing tag is a no-op)."""
    tag = tag.strip()
    if not tag:
        if not quiet:
            logger.error("Tag cannot be empty.")
        return None

    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    tag_list = snippet.tag_list
    if tag in tag_list:
        return snippet  # Idempotent: already tagged

    tag_list.append(tag)
    snippet.tags = json.dumps(tag_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def snippet_tag_remove(session: Session, checksum: str, tag: str, quiet: bool = False) -> Snippet | None:
    """Remove a tag from a snippet (idempotent — removing a missing tag is a no-op)."""
    tag = tag.strip()
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    tag_list = snippet.tag_list
    if tag not in tag_list:
        return snippet  # Idempotent: tag not present

    tag_list.remove(tag)
    snippet.tags = json.dumps(tag_list)
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def string_checksum(code_snippet: str) -> str:
    """Calculate the SHA256 checksum of a normalized code snippet."""
    normalized_string = string_normalize(code_snippet)
    return hashlib.sha256(normalized_string.encode("utf-8")).hexdigest()


def token_is_label(token_type, value: str) -> bool:
    """Check if a token is a label."""
    return token_type in Name.Label or (token_type in Name and value.endswith(":"))


def code_tokenize(code_snippet: str, normalize: bool = True) -> list[str]:
    """Return a list of tokens from a code snippet."""
    tokens = lexer.get_tokens(code_snippet)
    output_tokens = []
    for ttype, value in tokens:
        if ttype in Comment:
            continue

        if normalize:
            if ttype in Name.Register or value.lower() in ALL_REGISTERS:
                output_tokens.append("REG")
            elif ttype in Number:
                output_tokens.append("IMM")
            elif token_is_label(ttype, value):
                output_tokens.append("LABEL")
            elif value.lower() in [
                "dword",
                "word",
                "byte",
                "qword",
                "ptr",
            ]:
                output_tokens.append("MEM_SIZE")
            elif ttype not in Punctuation and value.strip():
                output_tokens.append(value.upper())
        else:
            if ttype not in Punctuation and value.strip():
                output_tokens.append(value.upper())

    return output_tokens


def code_create_minhash(code_snippet: str, normalize: bool = True, ngram_size: int = 3) -> MinHash:
    """Return a MinHash object representing the given code snippet.

    Uses configurable n-gram shingling to preserve token ordering so that
    structurally different snippets produce distinct fingerprints.
    """
    tokens = code_tokenize(code_snippet, normalize)
    m = MinHash(num_perm=NUM_PERMUTATIONS)
    if not tokens:
        return m
    if len(tokens) < ngram_size:
        m.update(" ".join(tokens).encode("utf8"))
        return m
    shingles: set[str] = set()
    for i in range(len(tokens) - ngram_size + 1):
        shingles.add(" ".join(tokens[i : i + ngram_size]))
    for shingle in shingles:
        # Weighted insertion: rare-instruction shingles are inserted multiple
        # times to boost their influence on the MinHash signature.
        weight = shingle_weight(shingle)
        encoded = shingle.encode("utf8")
        for _ in range(weight):
            m.update(encoded)
    return m


def code_create_minhash_batch(
    snippets: list[str], normalize: bool = True, ngram_size: int = 3
) -> list[MinHash]:
    """Create MinHash objects for multiple code snippets in batch.

    Pre-tokenizes all snippets and builds MinHash objects in a tight loop,
    amortizing interpreter overhead across the batch.
    """
    results: list[MinHash] = []
    for code_snippet in snippets:
        tokens = code_tokenize(code_snippet, normalize)
        m = MinHash(num_perm=NUM_PERMUTATIONS)
        if not tokens:
            results.append(m)
            continue
        if len(tokens) < ngram_size:
            m.update(" ".join(tokens).encode("utf8"))
            results.append(m)
            continue
        shingles: set[str] = set()
        for i in range(len(tokens) - ngram_size + 1):
            shingles.add(" ".join(tokens[i : i + ngram_size]))
        encoded = [s.encode("utf8") for s in shingles]
        for e in encoded:
            m.update(e)
        results.append(m)
    return results


# ---------------------------------------------------------------------------
# Snippet CRUD
# ---------------------------------------------------------------------------


def snippet_add(session: Session, name: str, code: str, ngram_size: int = 3) -> Snippet | None:
    """Add a new snippet or alias to the database."""
    if not code.strip():
        return None
    checksum = string_checksum(code)

    existing_snippet = Snippet.get_by_checksum(session, checksum)

    if existing_snippet:
        # Code exists, add new name as an alias
        name_list = existing_snippet.name_list
        if name and name not in name_list:
            name_list.append(name)
            existing_snippet.names = json.dumps(name_list)
            session.add(existing_snippet)
            session.commit()
            session.refresh(existing_snippet)
        return existing_snippet

    # Snippet with this code does not exist, create a new one
    minhash_obj = code_create_minhash(code, ngram_size=ngram_size)
    minhash_bytes = pickle.dumps(minhash_obj)

    new_snippet = Snippet(
        checksum=checksum,
        names=json.dumps([name]),
        code=code,
        minhash=minhash_bytes,
    )
    session.add(new_snippet)
    session.commit()
    session.refresh(new_snippet)
    lsh_cache_invalidate()
    return new_snippet


def snippet_find_matches(
    session: Session,
    query_string: str,
    top_n: int = 3,
    threshold: float | None = None,
    normalize: bool = True,
    ngram_size: int = 3,
) -> tuple[int, list[tuple[Snippet, float]]]:
    """Find and rank matches for a query string."""
    if threshold is None:
        threshold = LSH_THRESHOLD

    lsh = lsh_cache_load(session, threshold)
    if not lsh:
        lsh = lsh_index_build(session, threshold, NUM_PERMUTATIONS)
        if lsh:
            lsh_cache_save(session, lsh, threshold)

    if lsh is None:
        return 0, []  # Error handled in build_lsh_index

    query_minhash = code_create_minhash(query_string, normalize, ngram_size=ngram_size)
    candidate_keys = lsh.query(query_minhash)

    if not candidate_keys:
        return 0, []

    candidate_snippets = [
        Snippet.get_by_checksum(session, key) for key in candidate_keys
    ]

    candidate_map = {s.checksum: s for s in candidate_snippets if s}

    # Compute hybrid score (Jaccard + Levenshtein) for each candidate
    scored_matches: list[tuple[Snippet, float, float, float]] = []
    for checksum, snippet in candidate_map.items():
        jaccard = query_minhash.jaccard(snippet.get_minhash_obj())
        levenshtein = fuzz.ratio(query_string, snippet.code)
        hybrid = score_hybrid(jaccard, levenshtein)
        scored_matches.append((snippet, hybrid, jaccard, levenshtein))

    # Sort by hybrid score descending, take top_n
    scored_matches.sort(key=lambda x: x[1], reverse=True)
    top_matches = [
        (snippet, hybrid) for snippet, hybrid, _, _ in scored_matches[:top_n]
    ]

    return len(candidate_keys), top_matches


def snippet_delete(session: Session, checksum: str, quiet: bool = False) -> bool:
    """Delete a snippet by its checksum."""
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return False

    session.delete(snippet)
    session.commit()
    if not quiet:
        logger.info("Snippet with checksum %s deleted.", checksum)

    lsh_cache_invalidate()
    return True


def snippet_export_yara(session: Session, output_file: str) -> dict:
    """Export snippets as YARA string matching rules."""
    start_time = time.time()
    snippets = Snippet.get_all(session)
    num_exported = 0

    with open(output_file, "w", encoding="utf-8") as f:
        for snippet in snippets:
            primary_name = snippet.name_list[0] if snippet.name_list else f"snippet_{snippet.checksum[:16]}"
            rule_name = re.sub(r"[^a-zA-Z0-9_]", "_", primary_name)
            if not rule_name[0].isalpha() and rule_name[0] != "_":
                rule_name = "r_" + rule_name
            rule_name = f"resembl_{rule_name}_{snippet.checksum[:8]}"

            code_escaped = snippet.code.replace("\\", "\\\\").replace('"', '\\"')
            code_escaped = code_escaped.replace("\r", "\\r").replace("\n", "\\n")

            yara_rule = f'''rule {rule_name} {{
    meta:
        description = "Resembl exported snippet: {primary_name}"
        checksum = "{snippet.checksum}"
    strings:
        $asm = "{code_escaped}" nocase ascii wide
    condition:
        $asm
}}

'''
            f.write(yara_rule)
            num_exported += 1

    end_time = time.time()
    time_elapsed = end_time - start_time

    return {
        "num_exported": num_exported,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": (time_elapsed / num_exported) if num_exported > 0 else 0,
    }


def db_reindex(session: Session, ngram_size: int = 3) -> dict:
    """Recalculate the MinHash for every snippet in the database."""
    start_time = time.time()
    snippets = Snippet.get_all(session)
    num_snippets = len(snippets)

    if num_snippets == 0:
        return {"num_reindexed": 0, "time_elapsed": 0, "avg_time_per_snippet": 0}

    codes = [snippet.code for snippet in snippets]
    minhashes = code_create_minhash_batch(codes, ngram_size=ngram_size)
    for snippet, minhash_obj in zip(snippets, minhashes):
        snippet.minhash = pickle.dumps(minhash_obj)
        session.add(snippet)

    session.commit()
    lsh_cache_invalidate()

    end_time = time.time()
    time_elapsed = end_time - start_time

    return {
        "num_reindexed": num_snippets,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": time_elapsed / num_snippets,
    }


def snippet_get(session: Session, checksum: str) -> Snippet | None:
    """Return a snippet by its checksum."""
    return Snippet.get_by_checksum(session, checksum)


def snippet_compare(session: Session, checksum1: str, checksum2: str) -> dict | None:
    """Compare two snippets and return similarity metrics."""
    snippet1 = snippet_get(session, checksum1)
    snippet2 = snippet_get(session, checksum2)

    if not snippet1 or not snippet2:
        return None

    m1 = snippet1.get_minhash_obj()
    m2 = snippet2.get_minhash_obj()
    jaccard_similarity = m1.jaccard(m2)

    levenshtein_score = fuzz.ratio(snippet1.code, snippet2.code)
    hybrid = score_hybrid(jaccard_similarity, levenshtein_score)

    tokens1 = set(code_tokenize(snippet1.code, normalize=True))
    tokens2 = set(code_tokenize(snippet2.code, normalize=True))
    shared_tokens = len(tokens1.intersection(tokens2))

    # CFG structural comparison
    cfg1 = cfg_extract(snippet1.code)
    cfg2 = cfg_extract(snippet2.code)
    cfg_sim = cfg_similarity(cfg1, cfg2)

    return {
        "snippet1": {
            "checksum": snippet1.checksum,
            "names": snippet1.name_list,
            "token_count": len(tokens1),
        },
        "snippet2": {
            "checksum": snippet2.checksum,
            "names": snippet2.name_list,
            "token_count": len(tokens2),
        },
        "comparison": {
            "jaccard_similarity": jaccard_similarity,
            "levenshtein_score": levenshtein_score,
            "hybrid_score": hybrid,
            "cfg_similarity": cfg_sim,
            "shared_normalized_tokens": shared_tokens,
        },
    }


def db_calculate_average_similarity(session: Session, sample_size: int = 100) -> float:
    """Estimate average Jaccard similarity from a random sample."""
    all_snippets = Snippet.get_all(session)
    if len(all_snippets) < 2:
        return 1.0

    if len(all_snippets) > sample_size:
        sample_snippets = random.sample(list(all_snippets), sample_size)
    else:
        sample_snippets = list(all_snippets)

    total_similarity: float = 0.0
    num_comparisons: int = 0

    # Deserialize MinHash objects once to avoid O(n²) pickle.loads calls
    minhashes = [s.get_minhash_obj() for s in sample_snippets]

    num_snippets = len(sample_snippets)
    for i in range(num_snippets):
        for j in range(i + 1, num_snippets):
            total_similarity += minhashes[i].jaccard(minhashes[j])
            num_comparisons += 1

    return total_similarity / num_comparisons if num_comparisons > 0 else 1.0


def db_stats(session: Session) -> dict:
    """Return a dictionary of database statistics."""
    snippets = Snippet.get_all(session)
    if not snippets:
        return {
            "num_snippets": 0,
            "avg_snippet_size": 0,
            "vocabulary_size": 0,
            "avg_jaccard_similarity": 0.0,
        }

    total_size = sum(len(s.code) for s in snippets)

    all_tokens = set()
    for s in snippets:
        all_tokens.update(code_tokenize(s.code))

    return {
        "num_snippets": len(snippets),
        "avg_snippet_size": total_size / len(snippets),
        "vocabulary_size": len(all_tokens),
        "avg_jaccard_similarity": db_calculate_average_similarity(session),
    }


def snippet_list(session: Session, start: int = 0, end: int = 0) -> list[Snippet]:
    """List snippets, optionally within a given range."""
    if end > 0:
        return session.exec(select(Snippet).offset(start).limit(end - start)).all()
    return Snippet.get_all(session)


def snippet_search_by_name(session: Session, pattern: str) -> list[Snippet]:
    """Search for snippets where any name matches the pattern (case-insensitive)."""
    # The JSON structure means names are embedded in the string,
    # so a standard LIKE '%pattern%' will match anywhere in the names list.
    query_pattern = f"%{pattern}%"
    return list(session.exec(select(Snippet).where(Snippet.names.like(query_pattern))).all())


def snippet_export(session: Session, export_dir: str) -> dict:
    """Export all snippets to a directory."""
    start_time = time.time()
    snippets = Snippet.get_all(session)
    num_exported = 0

    os.makedirs(export_dir, exist_ok=True)

    abs_export_dir = os.path.realpath(export_dir)

    for snippet in snippets:
        # Use the first name as the primary name, sanitized for safety
        primary_name = snippet.name_list[0]
        # Strip path separators to prevent directory traversal
        safe_name = os.path.basename(primary_name.replace("..", "_"))
        if not safe_name:
            safe_name = snippet.checksum[:12]
        file_path = os.path.join(abs_export_dir, f"{safe_name}.asm")

        # Final guard: ensure the resolved path is within the export directory
        if not os.path.realpath(file_path).startswith(abs_export_dir):
            logger.warning(
                "Skipping snippet '%s': resolved path is outside export directory.",
                primary_name,
            )
            continue
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(snippet.code)
        num_exported += 1

    end_time = time.time()
    time_elapsed = end_time - start_time

    return {
        "num_exported": num_exported,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": (
            time_elapsed / num_exported if num_exported > 0 else 0
        ),
    }


def db_clean(session: Session) -> dict:
    """Clean the LSH cache and vacuum the database."""
    start_time = time.time()

    # 1. Invalidate (delete) all cache files
    lsh_cache_invalidate()

    # 2. Vacuum the database to reclaim space
    session.execute(text("VACUUM"))
    session.commit()

    end_time = time.time()
    time_elapsed = end_time - start_time

    return {
        "time_elapsed": time_elapsed,
        "vacuum_success": True,
    }


# ---------------------------------------------------------------------------
# Collection Functions
# ---------------------------------------------------------------------------


def collection_create(session: Session, name: str, description: str = "") -> Collection:
    """Create a new snippet collection."""
    collection = Collection(name=name, description=description)
    session.add(collection)
    session.commit()
    session.refresh(collection)
    return collection


def collection_delete(session: Session, name: str, quiet: bool = False) -> bool:
    """Delete a collection and unassign all its snippets."""
    collection = Collection.get_by_name(session, name)
    if not collection:
        if not quiet:
            logger.error("Collection '%s' not found.", name)
        return False

    # Unassign snippets from this collection
    for snippet in Snippet.get_by_collection(session, name):
        snippet.collection = None
        session.add(snippet)

    session.delete(collection)
    session.commit()
    return True


def collection_list(session: Session) -> list[dict]:
    """List all collections with snippet counts."""
    collections = Collection.get_all(session)
    results = []
    for col in collections:
        snippets = Snippet.get_by_collection(session, col.name)
        results.append({
            "name": col.name,
            "description": col.description,
            "snippet_count": len(snippets),
            "created_at": col.created_at,
        })
    return results


def collection_add_snippet(
    session: Session, collection_name: str, checksum: str, quiet: bool = False
) -> Snippet | None:
    """Add a snippet to a collection."""
    collection = Collection.get_by_name(session, collection_name)
    if not collection:
        if not quiet:
            logger.error("Collection '%s' not found.", collection_name)
        return None

    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    snippet.collection = collection_name
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


def collection_remove_snippet(
    session: Session, checksum: str, quiet: bool = False
) -> Snippet | None:
    """Remove a snippet from its collection."""
    snippet = Snippet.get_by_checksum(session, checksum)
    if not snippet:
        if not quiet:
            logger.error("Snippet with checksum %s not found.", checksum)
        return None

    snippet.collection = None
    session.add(snippet)
    session.commit()
    session.refresh(snippet)
    return snippet


# ---------------------------------------------------------------------------
# Version Functions
# ---------------------------------------------------------------------------


def snippet_version_list(session: Session, checksum: str) -> list[dict]:
    """Return version history for a snippet."""
    versions = SnippetVersion.get_by_checksum(session, checksum)
    return [
        {
            "id": v.id,
            "snippet_checksum": v.snippet_checksum,
            "created_at": v.created_at,
        }
        for v in versions
    ]


# ---------------------------------------------------------------------------
# Merge Functions
# ---------------------------------------------------------------------------


def db_merge(session: Session, source_db_path: str) -> dict:
    """Merge snippets from *source_db_path* into the current database.

    Deduplicates by checksum:
    - New snippets (unique checksum) are inserted.
    - Existing snippets gain any new names and tags from the source.
    - Collections from the source are created if they don't exist.

    Returns a dict with counts of added, updated, and skipped snippets.
    """
    from .database import create_db_engine

    start_time = time.time()
    source_url = f"sqlite:///{source_db_path}"

    try:
        source_engine = create_db_engine(source_url)
        from sqlmodel import Session as SourceSession
        source_session = SourceSession(source_engine)
    except Exception as e:
        logger.error("Failed to open source database: %s", e)
        return {"error": str(e)}

    added = 0
    updated = 0
    skipped = 0

    try:
        # Import collections first
        source_collections = source_session.exec(select(Collection)).all()
        for col in source_collections:
            existing = Collection.get_by_name(session, col.name)
            if not existing:
                new_col = Collection(
                    name=col.name,
                    description=col.description,
                    created_at=col.created_at,
                )
                session.add(new_col)

        # Import snippets
        source_snippets = source_session.exec(select(Snippet)).all()
        for src_snippet in source_snippets:
            existing = Snippet.get_by_checksum(session, src_snippet.checksum)

            if existing:
                changed = False

                # Merge names
                existing_names = set(existing.name_list)
                source_names = set(src_snippet.name_list)
                merged_names = existing_names | source_names
                if merged_names != existing_names:
                    existing.names = json.dumps(sorted(merged_names))
                    changed = True

                # Merge tags (independent of names)
                existing_tags = set(existing.tag_list)
                source_tags = set(src_snippet.tag_list)
                merged_tags = existing_tags | source_tags
                if merged_tags != existing_tags:
                    existing.tags = json.dumps(sorted(merged_tags))
                    changed = True

                if changed:
                    session.add(existing)
                    updated += 1
                else:
                    skipped += 1

                # Assign collection if the existing snippet doesn't have one
                if not existing.collection and src_snippet.collection:
                    existing.collection = src_snippet.collection
                    session.add(existing)
            else:
                # New snippet — insert it
                new_snippet = Snippet(
                    checksum=src_snippet.checksum,
                    names=src_snippet.names,
                    code=src_snippet.code,
                    minhash=src_snippet.minhash,
                    tags=src_snippet.tags,
                    collection=src_snippet.collection,
                )
                session.add(new_snippet)
                added += 1

        session.commit()
        lsh_cache_invalidate()
    except Exception as e:
        logger.error("Merge failed: %s", e)
        return {"error": str(e)}
    finally:
        source_session.close()

    end_time = time.time()
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "total_source": added + updated + skipped,
        "time_elapsed": end_time - start_time,
    }

