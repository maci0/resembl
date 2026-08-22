"""Dependency-light scoring core for resembl.

This module holds the *pure* tokenization / normalization / shingling /
MinHash-hashing code used to fingerprint and score assembly snippets.  It is
deliberately free of the database stack so that it can be imported without
``sqlmodel`` / ``sqlalchemy`` (and without the ORM model modules
``resembl.cache`` / ``resembl.lsh`` / ``resembl.models``).

It imports only:
- the standard library (``hashlib``, ``re``, ``struct``, ``pickle``,
  ``operator``, ``copy``)
- ``pygments`` (the ``NasmLexer`` + token types)
- ``rapidfuzz`` (mirrored from ``core``)
- ``numpy`` and ``datasketch`` are imported *lazily* inside the function
  bodies that need them, exactly as in the original ``core``/``models``
  source — so merely importing this module never pulls them in.

``resembl.core`` and ``resembl.models`` re-export everything defined here for
backward compatibility; ``from resembl.core import code_tokenize`` and
``from resembl.models import minhash_pack`` keep working unchanged.
"""

from __future__ import annotations

import hashlib
import operator
import pickle
import re
import struct
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from pygments.lexers.asm import NasmLexer
from pygments.token import Comment, Name, Number, Punctuation, Text
from rapidfuzz import fuzz  # noqa: F401  (re-exported surface used by consumers)

if TYPE_CHECKING:
    from datasketch import MinHash

#: Number of permutation functions for MinHash (higher = more accurate, slower).
NUM_PERMUTATIONS = 128

#: Magic prefix for the compact MinHash byte format.  A stored fingerprint
#: either starts with this prefix (``struct``-packed uint32 hash values,
#: self-describing) or is a legacy ``pickle`` blob produced by older versions.
MINHASH_MAGIC = b"RMLH"

#: Upper bound on the permutation count accepted when unpacking a stored
#: fingerprint.  Real configurations use 64–128; anything near this limit is
#: corrupt or hostile.  The bound also keeps ``struct`` format strings and
#: ``MinHash`` allocations sane on malformed input.
_MAX_NUM_PERM = 1 << 12

#: Cached MinHash templates keyed by num_perm, used to skip datasketch's
#: per-construction permutation regeneration (~260 µs — the dominant cost of
#: building a fingerprint).  Permutations depend only on (num_perm, seed),
#: so cloning a template produces identical fingerprints.
_MINHASH_TEMPLATES: dict[int, object] = {}

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
        first_word = stripped.split(None, 1)[0] if stripped else ""
        # A NASM label is an identifier at the start of the line immediately
        # followed by ':'.  Checking the leading word alone keeps memory
        # operands with segment overrides (e.g. ``mov eax, [fs:0]``) from
        # being misread as labels and needlessly splitting the block.
        is_label = len(first_word) > 1 and first_word.endswith(":")
        label_name = None
        if is_label:
            label_name = first_word[:-1]
            # If there's content after the label on the same line, treat as
            # part of the new block
            remainder = stripped[len(first_word) :].strip()

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
# MinHash helpers
# ---------------------------------------------------------------------------


def minhash_new(num_perm: int = 128) -> MinHash:
    """Return a fresh all-max MinHash without regenerating permutations.

    datasketch's constructor draws the permutation arrays from a numpy
    random stream on every call (~260 µs at 128 perms — most of the import
    worker's CPU).  Cloning a cached template (deepcopy of two small numpy
    arrays) is ~15x faster and yields identical permutations (seed 1), so
    fingerprints are byte-for-byte the same.
    """
    import copy

    from datasketch import MinHash

    template = _MINHASH_TEMPLATES.get(num_perm)
    if template is None:
        template = MinHash(num_perm=num_perm)
        _MINHASH_TEMPLATES[num_perm] = template
    return copy.deepcopy(template)


def minhash_num_perm(data: bytes) -> int:
    """Return the permutation count encoded in a packed fingerprint header.

    Validates the header (magic, 4-byte count in range) and raises
    ``ValueError`` on malformed input — callers that unpack untrusted blobs
    (legacy databases, ``merge`` sources, corrupted files) must never see raw
    ``struct.error`` or pathological counts.
    """
    if len(data) < 8:
        raise ValueError("Corrupt MinHash payload: shorter than the 8-byte header.")
    num_perm = struct.unpack(">I", data[4:8])[0]
    if num_perm < 2 or num_perm > _MAX_NUM_PERM:
        raise ValueError(
            f"Corrupt MinHash payload: implausible permutation count {num_perm}."
        )
    expected = 8 + 4 * num_perm
    if len(data) != expected:
        raise ValueError(
            f"Corrupt MinHash payload: expected {expected} bytes, got {len(data)}."
        )
    return num_perm


def minhash_pack(m: MinHash) -> bytes:
    """Serialize a MinHash into a compact, self-describing byte string.

    The format is ``MINHASH_MAGIC`` + big-endian uint32 ``num_perm`` +
    ``num_perm`` big-endian uint32 hash values (512 bytes for the default
    128 permutations — several times smaller than a pickle).
    """
    digest = m.digest()
    num_perm = len(digest)
    return MINHASH_MAGIC + struct.pack(f">I{num_perm}I", num_perm, *digest)


def minhash_unpack(data: bytes) -> MinHash:
    """Deserialize a MinHash stored with :func:`minhash_pack`.

    Falls back to ``pickle.loads`` for legacy pickled fingerprints, so
    databases created by older versions keep working unchanged.  Malformed
    packed payloads raise ``ValueError`` (never low-level ``struct`` errors).
    """
    if data.startswith(MINHASH_MAGIC):
        from datasketch import MinHash

        num_perm = minhash_num_perm(data)
        values = struct.unpack(f">{num_perm}I", data[8 : 8 + 4 * num_perm])
        return MinHash(num_perm=num_perm, hashvalues=list(values))
    try:
        return pickle.loads(data)
    except Exception as e:
        # Corrupt legacy blobs (truncated pickles, disk rot) surface as
        # UnpicklingError/EOFError/AttributeError/... — normalize them to
        # ValueError so every caller's documented "malformed blob raises
        # ValueError" contract holds and no low-level exception escapes.
        raise ValueError(f"Corrupt legacy fingerprint: {e}") from e


def minhash_jaccard(packed_a: bytes, packed_b: bytes) -> float:
    """Jaccard similarity of two stored MinHash byte blobs (0.0–1.0).

    Fast path: when both blobs use the compact packed format, similarity is
    computed directly from the uint32 arrays, bypassing the ``MinHash``
    constructor — which dominates the cost when scoring thousands of
    candidates (the constructor is ~300 µs per object).  Falls back to
    object-based comparison for legacy pickled blobs.

    The metric matches :meth:`datasketch.MinHash.jaccard` exactly: the
    fraction of positions whose hash values are equal (element-wise), not a
    set intersection — the two differ on degenerate fingerprints where hash
    values repeat (e.g. short or empty snippets).
    """
    if not (packed_a.startswith(MINHASH_MAGIC) and packed_b.startswith(MINHASH_MAGIC)):
        return minhash_unpack(packed_a).jaccard(minhash_unpack(packed_b))
    # Byte-identical blobs are exact matches — a single C-level memcmp that
    # is ~100x faster than the element-wise loop.  This is the common
    # self-match / exact-duplicate case in candidate scoring.
    if packed_a == packed_b:
        return 1.0
    # The two blobs may encode different permutation counts; reject the
    # mismatch the same way datasketch's MinHash.jaccard does.
    num_perm_a = minhash_num_perm(packed_a)
    num_perm_b = minhash_num_perm(packed_b)
    if num_perm_a != num_perm_b:
        raise ValueError(
            "Cannot compute Jaccard for MinHash blobs with different "
            f"permutation counts ({num_perm_a} vs {num_perm_b})."
        )
    a = struct.unpack(f">{num_perm_a}I", packed_a[8 : 8 + 4 * num_perm_a])
    b = struct.unpack(f">{num_perm_b}I", packed_b[8 : 8 + 4 * num_perm_b])
    # ``map(operator.eq, ...)`` iterates with C-level callbacks instead of a
    # Python ``for``/generator — ~1.6x faster over 128 hash values.
    return sum(map(operator.eq, a, b)) / num_perm_a


def minhash_jaccard_batch(
    query_packed: bytes, packed_list: Sequence[bytes], chunk_size: int = 50_000
) -> list[float]:
    """Jaccard of one packed fingerprint against many, vectorized with numpy.

    Every blob (query and candidates) must use the compact packed format;
    if any blob is a legacy pickle the call falls back to per-blob scoring
    via :func:`minhash_jaccard`, so correctness is preserved on old
    databases.  The vectorized pass loads each candidate's uint32 hash
    values with ``numpy.frombuffer`` (no per-blob ``struct.unpack`` Python
    loops) and compares the whole ``(N, 128)`` array against the query row
    in one C-level pass — measured ~7x faster than the per-blob path at 10k
    candidates.  Results are bit-for-bit identical to repeated
    :func:`minhash_jaccard` calls: equality counts are small integers and
    the ``num_perm`` divisor is a power of two, so both paths round
    identically.  Candidates are processed in chunks to bound peak memory.

    Raises ``ValueError`` when the query or a candidate is malformed, or
    when a candidate uses a different permutation count than the query —
    matching :func:`minhash_jaccard`.
    """
    if not packed_list:
        return []
    if not query_packed.startswith(MINHASH_MAGIC):
        return [minhash_jaccard(query_packed, p) for p in packed_list]
    num_perm = minhash_num_perm(query_packed)
    if any(not p.startswith(MINHASH_MAGIC) for p in packed_list):
        return [minhash_jaccard(query_packed, p) for p in packed_list]

    import numpy as np

    query_values = np.frombuffer(query_packed[8 : 8 + 4 * num_perm], dtype=">u4")
    results: list[float] = []
    for start in range(0, len(packed_list), chunk_size):
        chunk = packed_list[start : start + chunk_size]
        for p in chunk:
            p_num_perm = minhash_num_perm(p)
            if p_num_perm != num_perm:
                raise ValueError(
                    "Cannot compute Jaccard for MinHash blobs with different "
                    f"permutation counts ({num_perm} vs {p_num_perm})."
                )
        values = np.frombuffer(b"".join(p[8:] for p in chunk), dtype=">u4").reshape(
            len(chunk), num_perm
        )
        results.extend((values == query_values[None, :]).mean(axis=1).tolist())
    return results


def minhash_ensure_packed(data: bytes) -> bytes:
    """Return *data* in the compact packed format, converting legacy pickles."""
    if data.startswith(MINHASH_MAGIC):
        # Validate rather than trust: blobs from merged databases or legacy
        # files may be corrupt, and every downstream use (banding, Jaccard,
        # the query path) assumes a well-formed header.
        minhash_num_perm(data)
        return data
    return minhash_pack(minhash_unpack(data))


def _minhash_from_tokens(
    tokens: list[str], ngram_size: int = 3, num_perm: int = NUM_PERMUTATIONS
) -> MinHash:
    """Build a MinHash from an already-tokenized snippet.

    Shares the shingling/weighting logic with :func:`code_create_minhash`
    (the import hot path tokenizes once and reuses the tokens here).
    Shingles are deduplicated as token tuples — no per-shingle string join —
    and the weight check runs directly on the tokens instead of re-splitting
    the joined string.
    """
    m = minhash_new(num_perm)
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


def code_create_minhash(
    code_snippet: str,
    normalize: bool = True,
    ngram_size: int = 3,
    num_perm: int = NUM_PERMUTATIONS,
) -> MinHash:
    """Return a MinHash object representing the given code snippet.

    Uses configurable n-gram shingling to preserve token ordering so that
    structurally different snippets produce distinct fingerprints.
    """
    return _minhash_from_tokens(
        code_tokenize(code_snippet, normalize), ngram_size, num_perm
    )


def code_create_minhash_batch(
    snippets: list[str],
    normalize: bool = True,
    ngram_size: int = 3,
    num_perm: int = NUM_PERMUTATIONS,
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
        results.append(_minhash_from_tokens(tokens, ngram_size, num_perm))
    return results
