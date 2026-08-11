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
import heapq
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import TYPE_CHECKING

from pygments.lexers.asm import NasmLexer
from pygments.token import Comment, Name, Number, Punctuation, Text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from datasketch import MinHash
from rapidfuzz import fuzz, process
from sqlmodel import Session, func, select, text

from .cache import (
    lsh_cache_load,
    lsh_cache_save,
    lsh_index_add,
    lsh_index_add_batch,
    lsh_index_build,
    lsh_index_clear,
    lsh_index_remove,
)
from .lsh import (
    ResemblLSH,
    fingerprint_version_clear,
    fingerprint_version_get,
    fingerprint_version_set,
)
from .models import (
    FINGERPRINT_VERSION,
    Collection,
    LSHBucket,
    Snippet,
    SnippetVersion,
    minhash_ensure_packed,
    minhash_jaccard,
    minhash_jaccard_batch,
    minhash_pack,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of permutation functions for MinHash (higher = more accurate, slower).
NUM_PERMUTATIONS = 128

#: Default LSH similarity threshold for candidate filtering.
LSH_THRESHOLD = 0.5

#: Bounded retries for the index clear inside ``db_reindex`` (see there):
#: concurrent cold finds of the same database contend on SQLite's exclusive
#: schema lock, and the loser should wait rather than crash.
_REINDEX_CLEAR_RETRIES = 3
#: Linear backoff between clear retries, in seconds.
_REINDEX_CLEAR_RETRY_BACKOFF = 3


def adaptive_worker_count(num_items: int, cpu_count: int) -> int:
    """Choose a sensible worker count for a parallel job of *num_items* items.

    One worker per CPU is wasteful for small jobs: spawning each ``spawn``
    worker costs the full interpreter + library import (~450 ms and ~50 MB),
    so a 300-item job measured 1.85 s with 32 workers vs 0.84 s with 4.
    The default scales with the work (one worker per ~100 items) and is
    capped at the CPU count, so small jobs stay single-process and large
    ones parallelize fully.
    """
    return max(1, min(cpu_count, num_items // 100 + 1))


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
    "r0",
    "r1",
    "r2",
    "r3",
    "r4",
    "r5",
    "r6",
    "r7",
    "r8",
    "r9",
    "r10",
    "r11",
    "r12",
    "r13",
    "r14",
    "r15",
    "sp",
    "lr",
    "pc",
    "cpsr",
    "spsr",
    "fpscr",
    # AArch64 general purpose
    "x0",
    "x1",
    "x2",
    "x3",
    "x4",
    "x5",
    "x6",
    "x7",
    "x8",
    "x9",
    "x10",
    "x11",
    "x12",
    "x13",
    "x14",
    "x15",
    "x16",
    "x17",
    "x18",
    "x19",
    "x20",
    "x21",
    "x22",
    "x23",
    "x24",
    "x25",
    "x26",
    "x27",
    "x28",
    "x29",
    "x30",
    "w0",
    "w1",
    "w2",
    "w3",
    "w4",
    "w5",
    "w6",
    "w7",
    "w8",
    "w9",
    "w10",
    "w11",
    "w12",
    "w13",
    "w14",
    "w15",
    "w16",
    "w17",
    "w18",
    "w19",
    "w20",
    "w21",
    "w22",
    "w23",
    "w24",
    "w25",
    "w26",
    "w27",
    "w28",
    "w29",
    "w30",
    "xzr",
    "wzr",
    # NEON / FP
    "d0",
    "d1",
    "d2",
    "d3",
    "d4",
    "d5",
    "d6",
    "d7",
    "d8",
    "d9",
    "d10",
    "d11",
    "d12",
    "d13",
    "d14",
    "d15",
    "q0",
    "q1",
    "q2",
    "q3",
    "q4",
    "q5",
    "q6",
    "q7",
    "q8",
    "q9",
    "q10",
    "q11",
    "q12",
    "q13",
    "q14",
    "q15",
    "s0",
    "s1",
    "s2",
    "s3",
    "s4",
    "s5",
    "s6",
    "s7",
    "s8",
    "s9",
    "s10",
    "s11",
    "s12",
    "s13",
    "s14",
    "s15",
}

# MIPS registers (numeric and ABI names)
MIPS_REGISTERS = {
    "$0",
    "$1",
    "$2",
    "$3",
    "$4",
    "$5",
    "$6",
    "$7",
    "$8",
    "$9",
    "$10",
    "$11",
    "$12",
    "$13",
    "$14",
    "$15",
    "$16",
    "$17",
    "$18",
    "$19",
    "$20",
    "$21",
    "$22",
    "$23",
    "$24",
    "$25",
    "$26",
    "$27",
    "$28",
    "$29",
    "$30",
    "$31",
    "$zero",
    "$at",
    "$v0",
    "$v1",
    "$a0",
    "$a1",
    "$a2",
    "$a3",
    "$t0",
    "$t1",
    "$t2",
    "$t3",
    "$t4",
    "$t5",
    "$t6",
    "$t7",
    "$t8",
    "$t9",
    "$s0",
    "$s1",
    "$s2",
    "$s3",
    "$s4",
    "$s5",
    "$s6",
    "$s7",
    "$k0",
    "$k1",
    "$gp",
    "$sp",
    "$fp",
    "$ra",
    "$hi",
    "$lo",
    # FP
    "$f0",
    "$f1",
    "$f2",
    "$f3",
    "$f4",
    "$f5",
    "$f6",
    "$f7",
    "$f8",
    "$f9",
    "$f10",
    "$f11",
    "$f12",
    "$f13",
    "$f14",
    "$f15",
    "$f16",
    "$f17",
    "$f18",
    "$f19",
    "$f20",
    "$f21",
    "$f22",
    "$f23",
    "$f24",
    "$f25",
    "$f26",
    "$f27",
    "$f28",
    "$f29",
    "$f30",
    "$f31",
}

# RISC-V registers (x-names and ABI names)
RISCV_REGISTERS = {
    "x0",
    "x1",
    "x2",
    "x3",
    "x4",
    "x5",
    "x6",
    "x7",
    "x8",
    "x9",
    "x10",
    "x11",
    "x12",
    "x13",
    "x14",
    "x15",
    "x16",
    "x17",
    "x18",
    "x19",
    "x20",
    "x21",
    "x22",
    "x23",
    "x24",
    "x25",
    "x26",
    "x27",
    "x28",
    "x29",
    "x30",
    "x31",
    "zero",
    "ra",
    "gp",
    "tp",
    "t0",
    "t1",
    "t2",
    "t3",
    "t4",
    "t5",
    "t6",
    "s0",
    "s1",
    "s2",
    "s3",
    "s4",
    "s5",
    "s6",
    "s7",
    "s8",
    "s9",
    "s10",
    "s11",
    "a0",
    "a1",
    "a2",
    "a3",
    "a4",
    "a5",
    "a6",
    "a7",
    # FP
    "f0",
    "f1",
    "f2",
    "f3",
    "f4",
    "f5",
    "f6",
    "f7",
    "f8",
    "f9",
    "f10",
    "f11",
    "f12",
    "f13",
    "f14",
    "f15",
    "f16",
    "f17",
    "f18",
    "f19",
    "f20",
    "f21",
    "f22",
    "f23",
    "f24",
    "f25",
    "f26",
    "f27",
    "f28",
    "f29",
    "f30",
    "f31",
    "ft0",
    "ft1",
    "ft2",
    "ft3",
    "ft4",
    "ft5",
    "ft6",
    "ft7",
    "ft8",
    "ft9",
    "ft10",
    "ft11",
    "fs0",
    "fs1",
    "fs2",
    "fs3",
    "fs4",
    "fs5",
    "fs6",
    "fs7",
    "fs8",
    "fs9",
    "fs10",
    "fs11",
    "fa0",
    "fa1",
    "fa2",
    "fa3",
    "fa4",
    "fa5",
    "fa6",
    "fa7",
}

# Combined register set for multi-architecture normalization.
# Used by the tokenizer to replace any register token with the placeholder "REG",
# ensuring that register renaming does not affect similarity scoring.
ALL_REGISTERS = REGISTERS | ARM_REGISTERS | MIPS_REGISTERS | RISCV_REGISTERS

#: Lower-cased memory-size qualifiers recognized during token normalization.
_MEM_SIZE_WORDS = frozenset(("dword", "word", "byte", "qword", "ptr"))

#: System, privileged, or uncommon instructions that are highly distinctive.
#: Shingles containing these get boosted weight during MinHash construction.
RARE_INSTRUCTIONS = {
    "CPUID",
    "RDTSC",
    "RDTSCP",
    "RDRAND",
    "RDSEED",
    "XGETBV",
    "VMCALL",
    "VMLAUNCH",
    "VMRESUME",
    "VMXOFF",
    "SYSENTER",
    "SYSEXIT",
    "SYSCALL",
    "SYSRET",
    "INT",
    "IRET",
    "IRETD",
    "IRETQ",
    "EMMS",
    "WBINVD",
    "INVLPG",
    "INVD",
    "SGDT",
    "LGDT",
    "SLDT",
    "LLDT",
    "LIDT",
    "SIDT",
    "STR",
    "LTR",
    "LMSW",
    "CLTS",
    "MONITOR",
    "MWAIT",
    "HLT",
    "RSM",
    "UD2",
    "RDMSR",
    "WRMSR",
    "RDPMC",
}

#: The most common x86 instructions. Shingles composed entirely of these
#: receive reduced weight (1×) to avoid drowning out distinctive patterns.
COMMON_INSTRUCTIONS = {
    "MOV",
    "PUSH",
    "POP",
    "NOP",
    "LEA",
    "ADD",
    "SUB",
    "XOR",
    "CMP",
    "AND",
    "OR",
    "NOT",
    "NEG",
    "JMP",
    "CALL",
    "RET",
    "RETN",
    "TEST",
    "INC",
    "DEC",
    "SHL",
    "SHR",
    "SAR",
    "SAL",
    "REG",
    "IMM",
    "MEM_SIZE",
    "LABEL",  # normalized placeholders
}

#: Branch / jump mnemonics used by CFG extraction to identify basic-block
#: boundaries (terminators).
BRANCH_INSTRUCTIONS = {
    "JMP",
    "JZ",
    "JNZ",
    "JE",
    "JNE",
    "JG",
    "JGE",
    "JL",
    "JLE",
    "JA",
    "JAE",
    "JB",
    "JBE",
    "JO",
    "JNO",
    "JS",
    "JNS",
    "JP",
    "JNP",
    "JCXZ",
    "JECXZ",
    "JRCXZ",
    "LOOP",
    "LOOPZ",
    "LOOPNZ",
    "LOOPE",
    "LOOPNE",
    "RET",
    "RETN",
    "RETF",
    "CALL",  # not a terminator per-se, but starts a new edge
}


# ---------------------------------------------------------------------------
# Weighted Shingling
# ---------------------------------------------------------------------------


def _shingle_weight_tokens(tokens: Sequence[str]) -> int:
    """Return the insertion weight for a shingle given its token list.

    - **3** if the shingle contains at least one rare instruction.
    - **1** if every token in the shingle is a common instruction.
    - **2** otherwise (the default).

    Higher weight means the shingle is inserted multiple times into the
    MinHash, increasing its probability of being selected as a minimum
    hash value and thus boosting its influence on similarity.
    """
    has_rare = any(t in RARE_INSTRUCTIONS for t in tokens)
    if has_rare:
        return 3
    if all(t in COMMON_INSTRUCTIONS for t in tokens):
        return 1
    return 2


def shingle_weight(shingle: str) -> int:
    """Return the insertion weight for a shingle (see ``_shingle_weight_tokens``)."""
    return _shingle_weight_tokens(shingle.split())


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


def _string_normalize_lexed(tokens: Iterable[tuple[object, str]]) -> str:
    """Normalize a lexer token stream to a canonical string (no lexing).

    Shares the normalization logic between :func:`string_normalize` and the
    import hot path, which lexes each snippet once and derives both the
    checksum string and the tokens from the same stream.
    """
    return " ".join(
        value for ttype, value in tokens if ttype not in Comment and ttype != Text
    ).strip()


def string_normalize(code_snippet: str) -> str:
    """Normalize an assembly snippet and return a canonical string."""
    return _string_normalize_lexed(lexer.get_tokens(code_snippet))


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


def snippet_tag_add(
    session: Session, checksum: str, tag: str, quiet: bool = False
) -> Snippet | None:
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


def snippet_tag_remove(
    session: Session, checksum: str, tag: str, quiet: bool = False
) -> Snippet | None:
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
    return hashlib.sha256(
        normalized_string.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def token_is_label(token_type, value: str) -> bool:
    """Check if a token is a label."""
    return token_type in Name.Label or (token_type in Name and value.endswith(":"))


def _code_tokenize_lexed(
    tokens: Iterable[tuple[object, str]], normalize: bool = True
) -> list[str]:
    """Tokenize an already-lexed token stream (no re-lexing)."""
    output_tokens: list[str] = []
    append = output_tokens.append
    for ttype, value in tokens:
        if ttype in Comment:
            continue

        if normalize:
            if ttype in Name.Register:
                append("REG")
                continue
            # Pygments already classifies registers; the string check covers
            # register spellings the lexer misses.  ``lower`` is computed
            # once and reused instead of per-branch.
            lower = value.lower()
            if lower in ALL_REGISTERS:
                append("REG")
            elif ttype in Number:
                append("IMM")
            elif token_is_label(ttype, value):
                append("LABEL")
            elif lower in _MEM_SIZE_WORDS:
                append("MEM_SIZE")
            elif ttype not in Punctuation and value.strip():
                append(value if value.isupper() else value.upper())
        else:
            if ttype not in Punctuation and value.strip():
                append(value if value.isupper() else value.upper())

    return output_tokens


def code_tokenize(code_snippet: str, normalize: bool = True) -> list[str]:
    """Return a list of tokens from a code snippet."""
    return _code_tokenize_lexed(lexer.get_tokens(code_snippet), normalize)


def code_create_minhash(
    code_snippet: str, normalize: bool = True, ngram_size: int = 3
) -> MinHash:
    """Return a MinHash object representing the given code snippet.

    Uses configurable n-gram shingling to preserve token ordering so that
    structurally different snippets produce distinct fingerprints.
    """
    return _minhash_from_tokens(code_tokenize(code_snippet, normalize), ngram_size)


def _minhash_from_tokens(tokens: list[str], ngram_size: int = 3) -> MinHash:
    """Build a MinHash from an already-tokenized snippet.

    Shares the shingling/weighting logic with :func:`code_create_minhash`
    (the import hot path tokenizes once and reuses the tokens here).
    Shingles are deduplicated as token tuples — no per-shingle string join —
    and the weight check runs directly on the tokens instead of re-splitting
    the joined string.
    """
    from .models import minhash_new

    m = minhash_new(NUM_PERMUTATIONS)
    if not tokens:
        return m
    if len(tokens) < ngram_size:
        m.update(" ".join(tokens).encode("utf8", errors="surrogatepass"))
        return m
    shingles: set[tuple[str, ...]] = set()
    for i in range(len(tokens) - ngram_size + 1):
        shingles.add(tuple(tokens[i : i + ngram_size]))
    # Weighted insertion: a weight-w shingle contributes w *distinct*
    # pseudo-elements, so its hash values are w times as likely to be the
    # per-position minimum — the documented "boost" for rare instructions.
    # (Repeatedly hashing the *same* bytes would be a no-op: datasketch's
    # update takes the per-position min, which is unchanged by duplicates.)
    inputs: list[bytes] = []
    for shingle_tokens in shingles:
        base = " ".join(shingle_tokens).encode("utf8", errors="surrogatepass")
        weight = _shingle_weight_tokens(shingle_tokens)
        if weight <= 1:
            inputs.append(base)
        else:
            inputs.extend(base + b"|" + str(k).encode("utf8") for k in range(weight))
    m.update_batch(inputs)
    return m


def code_create_minhash_batch(
    snippets: list[str], normalize: bool = True, ngram_size: int = 3
) -> list[MinHash]:
    """Create MinHash objects for multiple code snippets in batch.

    Pre-tokenizes all snippets and builds MinHash objects in a tight loop,
    amortizing interpreter overhead across the batch.  Produces exactly the
    same fingerprints as :func:`code_create_minhash` (including weighted
    shingling) so that ``reindex`` never changes existing similarity scores.
    """
    results: list[MinHash] = []
    for code_snippet in snippets:
        tokens = code_tokenize(code_snippet, normalize)
        results.append(_minhash_from_tokens(tokens, ngram_size))
    return results


# ---------------------------------------------------------------------------
# Snippet CRUD
# ---------------------------------------------------------------------------


def snippet_prepare(
    name: str, code: str, ngram_size: int = 3
) -> tuple[str, str, str, bytes] | None:
    """Compute the checksum and MinHash fingerprint for a snippet.

    Returns ``(checksum, name, code, minhash_bytes)`` or ``None`` for empty
    code.  This is a pure function with no database access, so it is safe to
    run in worker processes when bulk-importing many files.

    The snippet is lexed exactly once: the normalized string (for the
    checksum) and the token list (for the MinHash) are both derived from the
    same token stream.  Lexing with Pygments is the dominant per-snippet
    cost, so this halves it on the import hot path.
    """
    if not code.strip():
        return None
    # Materialize the token stream: it is consumed twice (once for the
    # normalized checksum string, once for the MinHash tokens).
    tokens = list(lexer.get_tokens(code))
    normalized = _string_normalize_lexed(tokens)
    checksum = hashlib.sha256(
        normalized.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    minhash_bytes = minhash_pack(
        _minhash_from_tokens(_code_tokenize_lexed(tokens), ngram_size)
    )
    return checksum, name, code, minhash_bytes


def _checksum_chunks(checksums: list[str], batch_size: int = 900) -> list[list[str]]:
    """Split checksums into chunks small enough for one SQL ``IN`` clause.

    900 stays comfortably under SQLite's variable limit (999 by default,
    32766 on modern builds), halving the round trips of the old 500.
    """
    return [checksums[i : i + batch_size] for i in range(0, len(checksums), batch_size)]


def _snippets_by_checksums(
    session: Session, checksums: list[str]
) -> dict[str, Snippet]:
    """Fetch snippets by checksum using chunked ``IN`` queries (no N+1)."""
    result: dict[str, Snippet] = {}
    for chunk in _checksum_chunks(list(checksums)):
        for snippet in session.exec(
            select(Snippet).where(  # type: ignore[attr-defined]
                Snippet.checksum.in_(chunk)  # type: ignore[attr-defined]
            )
        ).all():
            result[snippet.checksum] = snippet
    return result


def _snippet_minhashes_by_checksums(
    session: Session, checksums: list[str]
) -> dict[str, bytes]:
    """Fetch only ``(checksum, minhash)`` pairs for many checksums.

    The ``code`` column dominates the table, so reading it for every LSH
    candidate would pull megabytes of text through the ORM per query even
    though most candidates are pruned before they are ever Levenshtein-
    scored.  The find hot path reads just the fingerprints here, vectorizes
    the Jaccard pass, and only then fetches full rows for the survivors.
    """
    result: dict[str, bytes] = {}
    for chunk in _checksum_chunks(list(checksums)):
        for row in session.exec(
            select(Snippet.checksum, Snippet.minhash).where(  # type: ignore[attr-defined]
                Snippet.checksum.in_(chunk)  # type: ignore[attr-defined]
            )
        ).all():
            result[row[0]] = row[1]
    return result


def _snippet_code_batches(
    session: Session, batch_size: int = 500
) -> Iterator[list[Snippet]]:
    """Yield snippets in fixed-size lists, streaming to bound memory usage.

    Uses keyset pagination so each batch is fully consumed before it is
    yielded — callers commit mid-loop, which SQLite would reject while a
    streaming read cursor is still open.
    """
    yield from Snippet.iter_batches(session, batch_size)


#: Parameterized template for one snippet row (the executemany path).
_SNIPPET_INSERT_SQL = (
    "INSERT INTO snippet (checksum, names, code, minhash, tags, collection) "
    "VALUES (:checksum, :names, :code, :minhash, :tags, :collection)"
)


def _duckdb_sql_literal(value: object) -> str:
    """Render one snippet-column value as a safe DuckDB SQL literal.

    Text is single-quoted with quote doubling — standard SQL escaping, and
    complete for DuckDB because it treats backslash literally inside string
    literals (no ``\\`` escape sequences).  Bytes use ``FROM_HEX``,
    DuckDB's blob-from-hex function (the ``X'...'`` hex literal is not
    supported).  ``None`` becomes ``NULL``.  This is the correctness and
    injection boundary of the DuckDB multi-VALUES fast path: snippet code
    and names are arbitrary user text, so every value must pass through
    here before being interpolated into SQL.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"FROM_HEX('{value.hex()}')"
    return "'" + str(value).replace("'", "''") + "'"


def _insert_snippet_rows(
    session: Session, rows: list[dict[str, object]], batch_size: int = 500
) -> None:
    """Insert snippet rows with the dialect's fastest strategy.

    DuckDB's executemany path is ~7x slower than multi-row ``VALUES``
    statements, and the snippet insert dominates import throughput there
    (measured 2,665 vs 19,872 rows/s at 500 rows/statement).  Values are
    rendered through :func:`_duckdb_sql_literal`, which is the correctness
    and injection boundary for the fast path.  Other dialects keep the
    parameterized executemany, which is already C-accelerated there.
    """
    if not rows:
        return
    if session.get_bind().dialect.name != "duckdb":
        for i in range(0, len(rows), batch_size):
            session.execute(text(_SNIPPET_INSERT_SQL), params=rows[i : i + batch_size])
        return
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        values = ",".join(
            "("
            + ", ".join(
                _duckdb_sql_literal(v)
                for v in (
                    row["checksum"],
                    row["names"],
                    row["code"],
                    row["minhash"],
                    row["tags"],
                    row["collection"],
                )
            )
            + ")"
            for row in chunk
        )
        # exec_driver_sql, not text(): the generated statement has no bind
        # parameters, and text()'s marker scan cannot tell a literal ``$1``
        # or ``:0`` inside user content from a real bind placeholder — a
        # snippet containing either would otherwise raise StatementError.
        session.connection().exec_driver_sql(
            "INSERT INTO snippet (checksum, names, code, minhash, tags, "
            f"collection) VALUES {values}"
        )


def snippet_add_batch(
    session: Session,
    prepared_items: list[tuple[str, str, str, bytes]],
    batch_size: int = 500,
) -> dict:
    """Insert many prepared snippets in one pass.

    ``prepared_items`` is a list of ``(checksum, name, code, minhash_bytes)``
    tuples as produced by :func:`snippet_prepare`.

    Deduplication is content-addressable: code that already exists in the
    database is not re-inserted; any new names are merged into the existing
    snippet as aliases.  Rows are written in batches with a single LSH cache
    invalidation at the end, making bulk imports orders of magnitude faster
    than one ``snippet_add`` call per file.

    Returns ``{"added", "aliased", "skipped", "time_elapsed"}``.
    """
    start_time = time.time()

    # Group by checksum: within one batch, identical code is deduplicated and
    # its names are merged.  ``(code, minhash_bytes, names)`` tuples keep the
    # entry strongly typed for the hot loop below.
    by_checksum: dict[str, tuple[str, bytes, list[str]]] = {}
    for checksum, name, code, minhash_bytes in prepared_items:
        entry = by_checksum.get(checksum)
        if entry is None:
            entry = (code, minhash_bytes, [])
            by_checksum[checksum] = entry
        if name and name not in entry[2]:
            entry[2].append(name)

    if not by_checksum:
        return {
            "added": 0,
            "aliased": 0,
            "skipped": len(prepared_items),
            "time_elapsed": 0.0,
        }

    # Batch-fetch the full rows for every candidate checksum in one pass of
    # chunked IN queries.  Checksums absent from the map are new.  This
    # replaces the old two-step flow (a checksum-only EXISTS select, then a
    # ``session.get`` per existing row) which issued one round trip per
    # existing snippet — an N+1 that dominated incremental re-imports of
    # mostly-known content at scale.
    existing_map = _snippets_by_checksums(session, list(by_checksum))

    aliased = 0
    new_snippets: list[Snippet] = []
    for checksum, (code, minhash_bytes, names) in by_checksum.items():
        snippet = existing_map.get(checksum)
        if snippet is not None:
            name_list = snippet.name_list
            merged = list(dict.fromkeys(name_list + names))
            if len(merged) > len(name_list):
                snippet.names = json.dumps(merged)
                session.add(snippet)
                aliased += 1
            continue
        new_snippets.append(
            Snippet(
                checksum=checksum,
                names=json.dumps(names),
                code=code,
                minhash=minhash_bytes,
            )
        )

    # Single transaction: per-group commits would repeatedly trigger WAL
    # checkpoints that rewrite the whole database file (quadratic at scale).
    # New rows are bulk-inserted with a raw ``executemany`` — measured ~30x
    # faster than the ORM's per-object ``add_all``, which was the import
    # write-path bottleneck — while alias name merges flush through the ORM.
    # DuckDB swaps in multi-row VALUES statements (its executemany is ~7x
    # slower; see ``_insert_snippet_rows``).  One commit persists everything.
    if new_snippets:
        rows: list[dict[str, object]] = [
            {
                "checksum": s.checksum,
                "names": s.names,
                "code": s.code,
                "minhash": s.minhash,
                "tags": s.tags,
                "collection": s.collection,
            }
            for s in new_snippets
        ]
        _insert_snippet_rows(session, rows, batch_size)
    if new_snippets or aliased:
        session.commit()

    # Keep the DB-backed LSH index in sync if one is already built.
    lsh_index_add_batch(session, [(s.checksum, s.minhash) for s in new_snippets])

    elapsed = time.time() - start_time
    return {
        "added": len(new_snippets),
        "aliased": aliased,
        "skipped": len(prepared_items) - len(by_checksum),
        "time_elapsed": elapsed,
    }


def snippet_add(
    session: Session, name: str, code: str, ngram_size: int = 3
) -> Snippet | None:
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
    minhash_bytes = minhash_pack(minhash_obj)

    new_snippet = Snippet(
        checksum=checksum,
        names=json.dumps([name]),
        code=code,
        minhash=minhash_bytes,
    )
    session.add(new_snippet)
    session.commit()
    session.refresh(new_snippet)
    # Keep the DB-backed LSH index in sync if one is already built.
    lsh_index_add(session, new_snippet.checksum, new_snippet.minhash)
    return new_snippet


def snippet_find_matches(
    session: Session,
    query_string: str,
    top_n: int = 3,
    threshold: float | None = None,
    normalize: bool = True,
    ngram_size: int = 3,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, list[tuple[Snippet, float]]]:
    """Find and rank matches for a query string.

    ``progress`` is forwarded to the lazy index build and to a one-time
    automatic reindex (see below) when either is triggered.
    """
    if threshold is None:
        threshold = LSH_THRESHOLD

    # Fingerprint-format migration: if the stored blobs were written by an
    # older algorithm (stamp missing or outdated), recompute them once so
    # queries never silently match old fingerprints against new ones.
    # Reindexing current-format blobs is idempotent (identical fingerprints).
    if fingerprint_version_get(session) != FINGERPRINT_VERSION:
        num_snippets = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]
        db_reindex(
            session,
            ngram_size=ngram_size,
            jobs=adaptive_worker_count(num_snippets, os.cpu_count() or 1),
            progress=progress,
        )

    lsh = lsh_cache_load(session, threshold, NUM_PERMUTATIONS)
    if not lsh:
        lsh = lsh_index_build(session, threshold, NUM_PERMUTATIONS, progress=progress)
        if lsh:
            lsh_cache_save(session, lsh, threshold, NUM_PERMUTATIONS)

    if lsh is None:
        return 0, []  # Error handled in build_lsh_index

    query_minhash = code_create_minhash(query_string, normalize, ngram_size=ngram_size)
    if isinstance(lsh, ResemblLSH):
        # DB-backed index queries against the packed fingerprint.
        candidate_keys = lsh.query(minhash_pack(query_minhash))
    else:
        # Legacy pickled datasketch index expects a MinHash object.
        candidate_keys = lsh.query(query_minhash)

    if not candidate_keys:
        return 0, []
    if top_n <= 0:
        return len(candidate_keys), []

    # Fetch only the fingerprint columns for every candidate first — the
    # ``code`` column dominates the table, and most candidates are pruned
    # before they are ever Levenshtein-scored, so loading full rows for all
    # of them would move megabytes of text through the ORM per query.
    keys = list(candidate_keys)
    minhashes = _snippet_minhashes_by_checksums(session, keys)

    # Jaccard is computed directly from the packed fingerprints (no MinHash
    # object construction), vectorized across all candidates in one numpy
    # pass over the (N, 128) uint32 array — SIMD under the hood.  This is
    # what keeps find fast when a query lands in a crowded band (thousands
    # of candidates at scale).
    query_minhash_bytes = minhash_pack(query_minhash)
    jaccards = minhash_jaccard_batch(query_minhash_bytes, [minhashes[k] for k in keys])

    # Hybrid score (Jaccard + Levenshtein) with an early exit: since
    # ``hybrid = 40 * jaccard + 0.6 * levenshtein`` and levenshtein <= 100,
    # a candidate whose upper bound ``40 * jaccard + 60`` is strictly below
    # the current n-th best hybrid can never enter the top-n — it skips the
    # ``fuzz.ratio`` call and the full-row fetch entirely.  Candidates are
    # processed in descending jaccard order so the bound only shrinks: once
    # one candidate is pruned, the rest of the list is provably pruned too,
    # and no further rows are fetched at all.  Full rows are loaded in small
    # chunks, and the heap keeps the top-n by (hybrid, insertion index) with
    # a final sort that replicates a stable sort, so the returned matches
    # are identical to scoring and fetching everything.
    order = sorted(range(len(keys)), key=lambda i: jaccards[i], reverse=True)
    scored: list[tuple[float, int, Snippet]] = []
    for start in range(0, len(order), 64):
        batch = order[start : start + 64]
        # Best possible hybrid in this batch cannot beat the current top-n.
        if len(scored) >= top_n and 40 * jaccards[batch[0]] + 60 < scored[0][0]:
            break
        full_rows = _snippets_by_checksums(session, [keys[i] for i in batch])
        for i in batch:
            snippet = full_rows.get(keys[i])
            if snippet is None:
                continue  # deleted concurrently between the two fetches
            jaccard = jaccards[i]
            if len(scored) >= top_n and 40 * jaccard + 60 < scored[0][0]:
                continue
            levenshtein = fuzz.ratio(query_string, snippet.code)
            hybrid = score_hybrid(jaccard, levenshtein)
            heapq.heappush(scored, (hybrid, i, snippet))
            if len(scored) > top_n:
                heapq.heappop(scored)
    scored.sort(key=lambda t: (-t[0], t[1]))
    top_matches = [(snippet, hybrid) for hybrid, _idx, snippet in scored[:top_n]]

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

    # Keep the DB-backed LSH index in sync if one is already built.
    lsh_index_remove(session, checksum)
    return True


def snippet_export_yara(session: Session, output_file: str) -> dict:
    """Export snippets as YARA string matching rules."""
    start_time = time.time()
    num_exported = 0

    with open(output_file, "w", encoding="utf-8") as f:
        for snippet in Snippet.stream_all(session):
            primary_name = (
                snippet.name_list[0]
                if snippet.name_list
                else f"snippet_{snippet.checksum[:16]}"
            )
            rule_name = re.sub(r"[^a-zA-Z0-9_]", "_", primary_name)
            if not rule_name[0].isalpha() and rule_name[0] != "_":
                rule_name = "r_" + rule_name
            rule_name = f"resembl_{rule_name}_{snippet.checksum[:8]}"

            code_escaped = snippet.code.replace("\\", "\\\\").replace('"', '\\"')
            code_escaped = code_escaped.replace("\r", "\\r").replace("\n", "\\n")

            yara_rule = f"""rule {rule_name} {{
    meta:
        description = "Resembl exported snippet: {primary_name}"
        checksum = "{snippet.checksum}"
    strings:
        $asm = "{code_escaped}" nocase ascii wide
    condition:
        $asm
}}

"""
            f.write(yara_rule)
            num_exported += 1

    end_time = time.time()
    time_elapsed = end_time - start_time

    return {
        "num_exported": num_exported,
        "time_elapsed": time_elapsed,
        "avg_time_per_snippet": (
            (time_elapsed / num_exported) if num_exported > 0 else 0
        ),
    }


def _reindex_prepare(args: tuple[list[str], int]) -> list[bytes]:
    """Worker: recompute packed fingerprints for a batch of codes.

    Pure function (no database access) so it can run in a process pool.
    """
    codes, ngram_size = args
    return [
        minhash_pack(m) for m in code_create_minhash_batch(codes, ngram_size=ngram_size)
    ]


def db_reindex(
    session: Session,
    ngram_size: int = 3,
    batch_size: int = 500,
    jobs: int = 1,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Recalculate the MinHash for every snippet in the database.

    With ``jobs > 1`` the CPU-bound tokenization runs in a process pool
    (bounded in-flight batches), turning a long sequential reindex into a
    parallel one.

    The fingerprints are invalidated *before* the update: any built index is
    cleared up front, so a crash mid-reindex can never leave a stale index
    behind (the next ``find`` simply rebuilds it from whatever fingerprints
    are stored).  On SQLite the writes are committed periodically so the WAL
    stays bounded — a single transaction spanning the whole reindex would
    grow the WAL to the size of the database and force one huge checkpoint
    at commit.  PostgreSQL segments its own WAL and pays an fsync per
    commit, so it keeps a single final commit.  If *progress* is given it is
    called as ``progress(done, total)`` with snippets processed so far.
    """
    import multiprocessing as _mp
    from collections import deque
    from concurrent.futures import Future, ProcessPoolExecutor

    start_time = time.time()
    num_snippets = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]

    if num_snippets == 0:
        fingerprint_version_set(session, FINGERPRINT_VERSION)
        return {"num_reindexed": 0, "time_elapsed": 0, "avg_time_per_snippet": 0}

    reindexed = 0
    parallel = jobs > 1 and num_snippets > batch_size
    is_sqlite = session.get_bind().dialect.name == "sqlite"
    # Commit every N batches on SQLite (WAL stays bounded); never on PG.
    commit_interval = 10 if is_sqlite else 0
    batches_since_commit = 0

    def apply_batch(batch: list[Snippet], blobs: list[bytes]) -> None:
        nonlocal reindexed, batches_since_commit
        for snippet, blob in zip(batch, blobs):
            snippet.minhash = blob
        reindexed += len(batch)
        if progress is not None:
            progress(reindexed, num_snippets)
        # Flush the batch's writes, then drop the objects so the identity
        # map stays bounded.
        session.flush()
        session.expunge_all()
        batches_since_commit += 1
        if commit_interval and batches_since_commit >= commit_interval:
            session.commit()
            batches_since_commit = 0

    # Fingerprints are about to change — drop any built index now so a crash
    # mid-reindex cannot leave a stale one behind.  The clear takes an
    # exclusive lock; when another process is concurrently building the index
    # (two CLI processes cold-finding the same database), retry briefly
    # instead of surfacing a raw "database is locked".
    for attempt in range(_REINDEX_CLEAR_RETRIES):
        try:
            lsh_index_clear(session)
            break
        except OperationalError:
            session.rollback()
            if attempt + 1 < _REINDEX_CLEAR_RETRIES:
                time.sleep(_REINDEX_CLEAR_RETRY_BACKOFF * (attempt + 1))
    else:
        logger.error(
            "Could not clear the index (another process may be writing to "
            "this database); retry once it is idle."
        )
        return {
            "num_reindexed": 0,
            "time_elapsed": 0,
            "avg_time_per_snippet": 0,
        }

    if parallel:
        ctx = _mp.get_context("spawn")
        try:
            with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as executor:
                in_flight: deque[tuple[list[Snippet], Future[list[bytes]]]] = deque()
                max_in_flight = jobs * 2
                for batch in _snippet_code_batches(session, batch_size):
                    codes = [snippet.code for snippet in batch]
                    in_flight.append(
                        (batch, executor.submit(_reindex_prepare, (codes, ngram_size)))
                    )
                    if len(in_flight) >= max_in_flight:
                        pending_batch, future = in_flight.popleft()
                        apply_batch(pending_batch, future.result())
                while in_flight:
                    pending_batch, future = in_flight.popleft()
                    apply_batch(pending_batch, future.result())
        except Exception:
            # Pool unavailable (e.g. spawned from a stdin script) — redo the
            # work sequentially; correctness must never depend on the pool.
            # Re-applying identical fingerprints is idempotent, so reset the
            # counter and process every batch again.
            logger.warning("Process pool unavailable; reindexing sequentially.")
            reindexed = 0
            batches_since_commit = 0
            for batch in _snippet_code_batches(session, batch_size):
                codes = [snippet.code for snippet in batch]
                apply_batch(batch, _reindex_prepare((codes, ngram_size)))
    else:
        for batch in _snippet_code_batches(session, batch_size):
            codes = [snippet.code for snippet in batch]
            apply_batch(batch, _reindex_prepare((codes, ngram_size)))
    session.commit()
    # Fingerprints are now current — stamp the format version so `find`
    # does not reindex again.
    fingerprint_version_set(session, FINGERPRINT_VERSION)

    end_time = time.time()
    time_elapsed = end_time - start_time

    return {
        "num_reindexed": reindexed,
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


def _random_expr(session: Session):
    """Return a dialect-portable random expression for ``ORDER BY`` sampling.

    PostgreSQL/SQLite/DuckDB use ``random()``; MySQL/MariaDB use ``rand()``.
    """
    if session.get_bind().dialect.name == "mysql":
        return func.rand()  # type: ignore[attr-defined]
    return func.random()  # type: ignore[attr-defined]


def db_calculate_average_similarity(session: Session, sample_size: int = 100) -> float:
    """Estimate average Jaccard similarity from a random sample."""
    count = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]
    if count < 2:
        return 1.0

    if count > sample_size:
        # Random sample directly in SQL — no need to load the whole table.
        sample_snippets = session.exec(
            select(Snippet).order_by(_random_expr(session)).limit(sample_size)
        ).all()
    else:
        sample_snippets = list(Snippet.get_all(session))

    total_similarity: float = 0.0
    num_comparisons: int = 0

    # Fast packed-bytes Jaccard (no MinHash object construction in the loop).
    blobs = [s.minhash for s in sample_snippets]

    num_snippets = len(sample_snippets)
    for i in range(num_snippets):
        for j in range(i + 1, num_snippets):
            total_similarity += minhash_jaccard(blobs[i], blobs[j])
            num_comparisons += 1

    return total_similarity / num_comparisons if num_comparisons > 0 else 1.0


def db_stats(session: Session) -> dict:
    """Return a dictionary of database statistics."""
    num_snippets = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]
    if num_snippets == 0:
        return {
            "num_snippets": 0,
            "avg_snippet_size": 0,
            "vocabulary_size": 0,
            "avg_jaccard_similarity": 0.0,
        }

    # Aggregate the average snippet size in SQL instead of loading every row.
    avg_size = session.exec(
        select(func.avg(func.length(Snippet.code)))  # type: ignore[arg-type]
    ).one()
    avg_snippet_size = float(avg_size or 0.0)

    # Vocabulary: tokenize a bounded random sample so the command stays
    # constant-time at scale (tokenizing every code took ~1 min at 500k).
    # For small databases the sample is the whole corpus (exact).
    sample_codes = session.exec(
        select(Snippet.code).order_by(_random_expr(session)).limit(2000)
    ).all()
    all_tokens: set[str] = set()
    for code in sample_codes:
        all_tokens.update(code_tokenize(code))

    return {
        "num_snippets": num_snippets,
        "avg_snippet_size": avg_snippet_size,
        # Estimated from up to 2000 sampled snippets on large databases.
        "vocabulary_size": len(all_tokens),
        "avg_jaccard_similarity": db_calculate_average_similarity(session),
    }


def snippet_list(session: Session, start: int = 0, end: int = 0) -> list[Snippet]:
    """List snippets, optionally within a given range."""
    if end > 0:
        return list(
            session.exec(select(Snippet).offset(start).limit(end - start)).all()
        )
    return list(Snippet.get_all(session))


def snippet_names_stream(
    session: Session, batch_size: int = 2000
) -> Iterator[list[tuple[str, str]]]:
    """Yield ``(checksum, names)`` pairs in batches via keyset pagination.

    Reads only the two columns the ``list`` command renders.  The ``code``
    column dominates the table, so loading full rows to list a large
    database would pull the whole corpus through the ORM (~1 GB at 500k
    snippets) — this keeps the unbounded listing flat in memory regardless
    of database size.  Each batch is fully consumed before the next is
    fetched (same keyset semantics as :meth:`Snippet.iter_batches`).
    """
    last: str | None = None
    while True:
        stmt = (
            select(Snippet.checksum, Snippet.names)
            .order_by(Snippet.checksum)  # type: ignore[attr-defined]
            .limit(batch_size)
        )
        if last is not None:
            stmt = stmt.where(Snippet.checksum > last)  # type: ignore[attr-defined]
        rows = session.exec(stmt).all()
        if not rows:
            return
        yield [(row[0], row[1]) for row in rows]
        last = rows[-1][0]


def snippet_search_by_name(
    session: Session, pattern: str, limit: int = 50
) -> list[Snippet]:
    """Search for snippets where any name matches the pattern (case-insensitive).

    The JSON structure means names are embedded in the string, so a standard
    LIKE '%pattern%' matches anywhere in the names list.  *limit* bounds the
    result (and the fetch) so a broad pattern on a large database returns a
    useful page instead of everything.
    """
    query_pattern = f"%{pattern}%"
    return list(
        session.exec(
            select(Snippet)
            .where(Snippet.names.like(query_pattern))  # type: ignore[attr-defined]
            .limit(limit)
        ).all()
    )


def snippet_export(session: Session, export_dir: str) -> dict:
    """Export all snippets to a directory."""
    start_time = time.time()
    num_exported = 0

    os.makedirs(export_dir, exist_ok=True)

    abs_export_dir = os.path.realpath(export_dir)
    used_paths: set[str] = set()

    for snippet in Snippet.stream_all(session):
        # Use the first name as the primary name, sanitized for safety.
        primary_name = (
            snippet.name_list[0]
            if snippet.name_list
            else f"snippet_{snippet.checksum[:16]}"
        )
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

        # Avoid silently overwriting when several snippets share a name.
        if file_path in used_paths:
            # 12 hex chars (48 bits) keeps the disambiguator collision-free
            # even with hundreds of thousands of same-named snippets (the
            # previous 8 chars collided at ~30 pairs per 500k).
            file_path = os.path.join(
                abs_export_dir, f"{safe_name}-{snippet.checksum[:12]}.asm"
            )
        used_paths.add(file_path)

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


def db_verify(session: Session) -> dict:
    """Report the database's health: index, fingerprints, and pending work.

    Returns a dict with counts, ``warnings`` (self-healing states: a missing
    index or stale fingerprints, both repaired by the next ``find``) and
    ``issues`` (a bucket/snippet mismatch — a genuinely stale index that
    ``reindex --force`` should resolve).  Callers typically exit non-zero
    only when ``issues`` is non-empty.
    """
    from .lsh import _banding_params, fingerprint_version_get, lsh_meta_get
    from .models import FINGERPRINT_VERSION

    num_snippets = session.exec(select(func.count(Snippet.checksum))).one()  # type: ignore[arg-type]
    warnings: list[str] = []
    issues: list[str] = []

    stored_version = fingerprint_version_get(session)
    if stored_version != FINGERPRINT_VERSION:
        warnings.append(
            "fingerprints are from an older format — the next `find` reindexes once"
        )

    meta = lsh_meta_get(session)
    num_buckets = 0
    expected_buckets: int | None = None
    if meta is None:
        warnings.append("no LSH index — the next `find` builds it")
    else:
        threshold, num_perm = meta
        b, _r = _banding_params(threshold, num_perm)
        expected_buckets = num_snippets * b
        num_buckets = session.exec(select(func.count(LSHBucket.checksum))).one()  # type: ignore[arg-type]
        if num_snippets > 0 and num_buckets != expected_buckets:
            issues.append(
                f"index may be stale ({num_buckets} bucket rows, expected "
                f"{expected_buckets}) — run `resembl reindex --force`"
            )

    return {
        "num_snippets": num_snippets,
        "num_buckets": num_buckets,
        "expected_buckets": expected_buckets,
        "fingerprint_version": stored_version,
        "warnings": warnings,
        "issues": issues,
    }


def db_clean(session: Session) -> dict:
    """Clean the LSH cache and vacuum the database."""
    start_time = time.time()

    # 1. Wipe the legacy cache files and the DB-backed index.
    lsh_index_clear(session)

    # 2. Vacuum the database to reclaim space (SQLite only).
    vacuum_success = False
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("VACUUM"))
        session.commit()
        vacuum_success = True

    end_time = time.time()
    time_elapsed = end_time - start_time

    return {
        "time_elapsed": time_elapsed,
        "vacuum_success": vacuum_success,
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
        results.append(
            {
                "name": col.name,
                "description": col.description,
                "snippet_count": len(snippets),
                "created_at": col.created_at,
            }
        )
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
    """Merge snippets from a source database into the current one.

    *source_db_path* is a SQLite file path, or a full database URL (e.g.
    ``duckdb:///file.db`` or ``postgresql+pg8000://user:pass@host/db``) —
    any backend with its driver installed can be a source.

    Deduplicates by checksum:
    - New snippets (unique checksum) are inserted.
    - Existing snippets gain any new names and tags from the source.
    - Collections from the source are created if they don't exist.

    Returns a dict with counts of added, updated, and skipped snippets.
    """
    from .database import create_db_engine

    start_time = time.time()
    # The source may be any backend: a full URL (e.g. duckdb:///file.db,
    # postgresql+pg8000://...) is used as-is; otherwise it is a SQLite path.
    source_url = (
        source_db_path if "://" in source_db_path else f"sqlite:///{source_db_path}"
    )

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
    added_minhashes: list[tuple[str, bytes]] = []
    new_rows: list[dict[str, object]] = []

    try:
        # Import collections first
        source_collections = source_session.exec(select(Collection)).all()
        for col in source_collections:
            existing_collection = Collection.get_by_name(session, col.name)
            if not existing_collection:
                new_col = Collection(
                    name=col.name,
                    description=col.description,
                    created_at=col.created_at,
                )
                session.add(new_col)

        # Pre-compute the set of checksums already present in this database
        # so the per-row lookup below stays in-memory (no per-row query).
        local_checksums = set(session.exec(select(Snippet.checksum)).all())

        # Source snippets that overlap local content are merged.  Their local
        # rows are fetched in chunked IN batches instead of one
        # ``session.get`` per overlap — merging two heavily-overlapping
        # databases would otherwise issue a round trip per overlap (N+1).
        # The pending buffer is bounded, so memory stays flat regardless of
        # how much of the source already exists locally.
        pending_overlap: list[Snippet] = []

        def record_new(src_snippet: Snippet) -> None:
            nonlocal added
            new_rows.append(
                {
                    "checksum": src_snippet.checksum,
                    "names": src_snippet.names,
                    "code": src_snippet.code,
                    "minhash": src_snippet.minhash,
                    "tags": src_snippet.tags,
                    "collection": src_snippet.collection,
                }
            )
            added += 1
            added_minhashes.append(
                (src_snippet.checksum, minhash_ensure_packed(src_snippet.minhash))
            )

        def merge_overlap(batch: list[Snippet]) -> None:
            nonlocal updated, skipped
            local_rows = _snippets_by_checksums(session, [s.checksum for s in batch])
            for src_snippet in batch:
                existing = local_rows.get(src_snippet.checksum)
                if existing is None:
                    # Vanished between the checksum snapshot and the fetch —
                    # re-add it as new, matching the old session.get fallback.
                    record_new(src_snippet)
                    continue
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

        # Import snippets (streaming, so memory stays bounded for big sources)
        for src_snippet in Snippet.stream_all(source_session):
            if src_snippet.checksum in local_checksums:
                pending_overlap.append(src_snippet)
                if len(pending_overlap) >= 900:
                    merge_overlap(pending_overlap)
                    pending_overlap.clear()
            else:
                record_new(src_snippet)
        if pending_overlap:
            merge_overlap(pending_overlap)

        # Bulk-insert the new rows instead of per-object ORM adds — the same
        # ~30x write-path win as snippet_add_batch (multi-VALUES on DuckDB).
        _insert_snippet_rows(session, new_rows)
        session.commit()
        # Keep the DB-backed LSH index in sync if one is already built.
        lsh_index_add_batch(session, added_minhashes)
        # The source blobs were copied verbatim and may be from an older
        # fingerprint format — drop the version stamp so the next `find`
        # reindexes once and normalizes everything.
        fingerprint_version_clear(session)
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
