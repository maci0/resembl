"""Dependency-light scoring core for resembl.

This module holds the *pure* tokenization / normalization / shingling /
MinHash-hashing code used to fingerprint and score assembly snippets.  It is
deliberately free of the database stack so that it can be imported without
``sqlmodel`` / ``sqlalchemy`` (and without the ORM model modules
``resembl.cache`` / ``resembl.lsh`` / ``resembl.models``).

It imports only:
- the standard library (``hashlib``, ``struct``, ``operator``, ``copy``)
- ``pygments.token`` (cheap constant types); the ``NasmLexer`` itself is
  constructed lazily on first use (:func:`get_lexer`) — importing
  ``pygments.lexers`` costs ~65 ms, which commands that never touch
  assembly text must not pay at startup
- ``numpy`` is imported *lazily* inside the function bodies that need it,
  and :class:`~resembl.minhash.MinHash` is imported lazily too (it needs
  numpy) — so merely importing this module never pulls them in.

The names used by ``resembl.core`` / ``resembl.models`` are re-exported from
those modules for backward compatibility; ``from resembl.core import
code_tokenize`` and ``from resembl.models import minhash_pack`` keep working.
"""

from __future__ import annotations

import hashlib
import operator
import struct
import threading
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pygments.lexers.asm import NasmLexer

    from .minhash import MinHash

from pygments.token import Comment, Name, Number, Punctuation, Text

#: Number of permutation functions for MinHash (higher = more accurate, slower).
NUM_PERMUTATIONS = 128

#: Magic prefix for the compact MinHash byte format.  Every stored fingerprint
#: must start with this prefix (``struct``-packed uint32 hash values,
#: self-describing).  Blobs in any other format are rejected: they are never
#: deserialized (a pickle blob would be arbitrary code execution when a
#: hostile ``merge`` source or corrupted database is read) — callers treat the
#: ``ValueError`` as "corrupt fingerprint" and recompute it from the code.
MINHASH_MAGIC = b"RMLH"

#: Upper bound on the permutation count accepted when unpacking a stored
#: fingerprint.  Real configurations use 64-128; anything near this limit is
#: corrupt or hostile.  The bound also keeps ``struct`` format strings and
#: ``MinHash`` allocations sane on malformed input.
MAX_NUM_PERM = 1 << 12

#: Cached MinHash templates keyed by num_perm, used to skip permutation
#: regeneration on every construction (~260 µs — the dominant cost of
#: building a fingerprint).  Permutations depend only on (num_perm, seed),
#: so cloning a template produces identical fingerprints.
_MINHASH_TEMPLATES: dict[int, MinHash] = {}

#: Serializes first-time template construction for *minhash_new*: the serve
#: process runs one handler thread per request, and the first concurrent
#: finds at a new permutation count all miss the cache and race this
#: check-then-insert on a plain dict.  Templates are identical either way
#: (seeded construction), so the old code never corrupted data, but every
#: racer paid the full permutation build.  The steady-state fast path (the
#: entry exists) stays lock-free, like ``lsh._ensure_tables_once``.
_MINHASH_TEMPLATES_LOCK = threading.Lock()

#: Shared Pygments lexer instance, created on first tokenize/normalize (see
#: :func:`get_lexer`).
_lexer: NasmLexer | None = None

#: Serializes first-time lexer construction: the serve process runs one
#: handler thread per request, and the first concurrent tokenizations race
#: this check-then-set on a plain global.  A double construction would be
#: harmless anyway (both instances are equivalent), but the lock guarantees
#: the documented single shared instance; the steady-state fast path (the
#: instance exists) stays lock-free.
_lexer_lock = threading.Lock()


def _reordered_nasm_lexer() -> NasmLexer:
    """Build a NasmLexer with ``instruction-args``' whitespace rules first.

    Pygments' ``RegexLexer`` tries a state's rules in order at every position
    and stops at the first match, so rule *order* is a performance knob.  In
    NasmLexer's ``instruction-args`` state the whitespace and comment rules
    sit at positions 13-16, behind every number, punctuation, register and
    identifier pattern: a single space therefore fails ~14 regexes before it
    matches, and whitespace plus comments are ~60% of all tokens in that
    state.  Measured on the 689-file corpus this cost **11.6 regex attempts
    per token** (5.86M total); moving those rules to the front drops it to
    **7.1** (3.59M, -39%) and lexing is **1.55x** faster.

    The reorder is exact, not a heuristic: the moved patterns can only match
    whitespace, ``;`` or ``#``, and no other ``instruction-args`` rule can
    begin with those characters, so no input can ever match an earlier rule
    than it did before.  Rule *actions* and state transitions are untouched
    (``[\r\n]+`` still pops back to ``root``).  ``tests/test_lexer.py`` pins
    this by diffing the whole token stream against the stock NasmLexer over
    the corpus, so a Pygments change that invalidates the assumption fails
    loudly instead of silently changing fingerprints.
    """
    from pygments.lexers.asm import NasmLexer
    from pygments.token import Whitespace

    lexer = NasmLexer()
    # Copy the shared table before editing: ``_tokens`` is shared by every
    # NasmLexer instance, and mutating it would reorder the lexer for other
    # instances (and for other tests) in the process.
    lexer_tokens = lexer._tokens  # pylint: disable=protected-access
    tables = {state: list(rules) for state, rules in lexer_tokens.items()}
    args = tables.get("instruction-args")
    if args is not None:
        early = [rule for rule in args if rule[1] in (Whitespace, Comment.Single)]
        if early and len(early) < len(args):
            tables["instruction-args"] = early + [
                rule for rule in args if rule[1] not in (Whitespace, Comment.Single)
            ]
            lexer._tokens = tables  # pylint: disable=protected-access
    return lexer


def get_lexer() -> NasmLexer:
    """Return the shared NasmLexer, constructing it on first call.

    ``pygments.lexers.asm`` costs ~65 ms to import, so the import and the
    instance creation are deferred until a snippet is actually lexed:
    commands that never touch assembly text (``list``, ``export``,
    ``config``, the collections/name/tag groups, ...) skip that cost at
    every startup.  The instance is the rule-reordered lexer built by
    :func:`_reordered_nasm_lexer`.
    """
    global _lexer
    if _lexer is None:
        with _lexer_lock:
            if _lexer is None:  # double-checked: loser re-probes before returning
                _lexer = _reordered_nasm_lexer()
    return _lexer


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
#: receive reduced weight (1x) to avoid drowning out distinctive patterns.
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

# Token-type classification bits, cached per Pygments token type.  The
# tokenizer inner loops ask the same half-dozen prefix questions
# (``ttype in Comment``, ``ttype in Name.Register``, ...) for every token;
# each is a Python-level ``_TokenType.__contains__`` call (~200 ns).  Pygments
# types are interned singletons, so answering them once per type and caching
# the bitmask turns the per-token cost into one C-level dict lookup.
_TT_COMMENT = 1
_TT_REGISTER = 2  # in Name.Register
_TT_NUMBER = 4  # in Number
_TT_LABEL = 8  # in Name.Label
_TT_NAME = 16  # in Name
_TT_PUNCT = 32  # in Punctuation
_TT_TEXT = 64  # exactly Text
_TT_WS = 128  # in Text (NasmLexer yields Text.Whitespace for runs and newlines)

#: Bound on the cache so a caller-supplied lexer emitting synthetic token
#: types cannot grow it without limit.  NasmLexer emits a few dozen types.
_TYPE_FLAGS_MAX = 4096
_type_flags: dict[object, int] = {}


def _token_type_flags(ttype: object) -> int:
    """Return the cached classification bitmask for a token type."""
    flags = _type_flags.get(ttype)
    if flags is None:
        flags = 0
        if ttype in Comment:
            flags |= _TT_COMMENT
        if ttype in Name.Register:
            flags |= _TT_REGISTER
        if ttype in Number:
            flags |= _TT_NUMBER
        if ttype in Name.Label:
            flags |= _TT_LABEL
        if ttype in Name:
            flags |= _TT_NAME
        if ttype in Punctuation:
            flags |= _TT_PUNCT
        if ttype == Text:
            flags |= _TT_TEXT
        if ttype in Text:
            flags |= _TT_WS
        if len(_type_flags) < _TYPE_FLAGS_MAX:
            _type_flags[ttype] = flags
    return flags


def string_normalize_lexed(tokens: Iterable[tuple[object, str]]) -> str:
    """Normalize a lexer token stream to a canonical string (no lexing).

    Shares the normalization logic between :func:`string_normalize` and the
    import hot path, which lexes each snippet once and derives both the
    checksum string and the tokens from the same stream.
    """
    return _lexed_pass(tokens, normalize=False, want_tokens=False, want_normalized=True)[1]


def string_normalize(code_snippet: str) -> str:
    """Normalize an assembly snippet and return a canonical string."""
    return string_normalize_lexed(get_lexer().get_tokens(code_snippet))


def string_checksum(code_snippet: str) -> str:
    """Calculate the SHA256 checksum of a normalized code snippet."""
    normalized_string = string_normalize(code_snippet)
    return hashlib.sha256(normalized_string.encode("utf-8", errors="surrogatepass")).hexdigest()


def token_is_label(token_type: object, value: str) -> bool:
    """Check if a token is a label."""
    flags = _token_type_flags(token_type)
    return bool(flags & _TT_LABEL) or bool(flags & _TT_NAME and value.endswith(":"))


def _lexed_pass(
    tokens: Iterable[tuple[object, str]],
    normalize: bool,
    want_tokens: bool,
    want_normalized: bool,
) -> tuple[list[str], str]:
    """Single pass over a lexer token stream producing tokens and/or a string.

    The import hot path needs both outputs from one stream; running the two
    public helpers separately iterated the tokens twice and re-classified
    every token type.  This is the one implementation both delegate to, so
    they can never disagree.
    """
    output_tokens: list[str] = []
    append = output_tokens.append
    normalized_parts: list[str] = []
    normalized_append = normalized_parts.append
    # Local alias + inline miss path: the cache hits for every token after the
    # first of its type, so the per-token ``_token_type_flags`` call is pure
    # overhead (measured ~1.2x on the tokenize phase).
    cache = _type_flags
    for ttype, value in tokens:
        flags = cache.get(ttype)
        if flags is None:
            flags = _token_type_flags(ttype)
        if flags & _TT_COMMENT:
            continue

        if want_normalized and (flags & _TT_TEXT) == 0:
            normalized_append(value)

        if not want_tokens:
            continue

        # Text tokens are whitespace in NASM (~47% of all tokens): they never
        # survive the ``value.strip()`` tail, so skipping them here avoids the
        # ``lower()`` + register/mem-size probes every other token pays.
        if flags & _TT_WS and value.isspace():
            continue

        if normalize:
            if flags & _TT_REGISTER:
                append("REG")
                continue
            # Pygments already classifies registers; the string check covers
            # register spellings the lexer misses.  ``lower`` is computed
            # once and reused instead of per-branch.
            lower = value.lower()
            if lower in ALL_REGISTERS:
                append("REG")
                continue
            if flags & _TT_NUMBER:
                append("IMM")
                continue
            if flags & _TT_LABEL or (flags & _TT_NAME and value.endswith(":")):
                append("LABEL")
                continue
            if lower in _MEM_SIZE_WORDS:
                append("MEM_SIZE")
                continue

        # Shared tail for both modes: normalization only rewrites the token
        # classes above; everything else passes through upper-cased.  A
        # leading ``isupper()`` would only decide between ``value`` and
        # ``value.upper()`` — the same string, since ``upper`` is the
        # identity on an all-uppercase value — so the extra scan was wasted.
        if not (flags & _TT_PUNCT) and value.strip():
            append(value.upper())

    normalized = " ".join(normalized_parts).strip() if want_normalized else ""
    return output_tokens, normalized


def code_tokenize_lexed(tokens: Iterable[tuple[object, str]], normalize: bool = True) -> list[str]:
    """Tokenize an already-lexed token stream (no re-lexing)."""
    return _lexed_pass(tokens, normalize, want_tokens=True, want_normalized=False)[0]


def code_tokenize_normalize_lexed(
    tokens: Iterable[tuple[object, str]], normalize: bool = True
) -> tuple[list[str], str]:
    """Return ``(tokens, normalized_string)`` from one pass over *tokens*.

    The import hot path needs both; deriving them from a single stream avoids
    a second full iteration and a second token-type classification pass.
    """
    return _lexed_pass(tokens, normalize, want_tokens=True, want_normalized=True)


def code_tokenize(code_snippet: str, normalize: bool = True) -> list[str]:
    """Return a list of tokens from a code snippet."""
    return code_tokenize_lexed(get_lexer().get_tokens(code_snippet), normalize)


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
    # ``set.isdisjoint`` / ``set.issuperset`` loop in C; the generator
    # ``any``/``all`` equivalents measured ~2.4x slower on the same shingles.
    if not RARE_INSTRUCTIONS.isdisjoint(tokens):
        return 3
    if COMMON_INSTRUCTIONS.issuperset(tokens):
        return 1
    return 2


def shingle_weight(shingle: str) -> int:
    """Return the insertion weight for a shingle (see ``_shingle_weight_tokens``)."""
    return _shingle_weight_tokens(shingle.split())


# ---------------------------------------------------------------------------
# Hybrid Scoring
# ---------------------------------------------------------------------------


def score_hybrid(jaccard: float, levenshtein: float, jaccard_weight: float = 0.4) -> float:
    """Combine Jaccard (0-1) and Levenshtein (0-100) into a single 0-100 score.

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
    lines = [line.strip() for line in code.splitlines() if line.strip()]
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
        words = stripped.split()
        mnemonic = words[0].upper() if words else ""
        if mnemonic in BRANCH_INSTRUCTIONS:
            blocks.append(current_block)
            current_block = []

    # Don't forget the final block
    if current_block:
        blocks.append(current_block)

    # Build adjacency list
    # A label whose basic block never materialized (e.g. the code ends with
    # ``label:`` and no instruction follows) maps one past the last block;
    # such targets are dropped so the graph never contains edges to
    # non-existent blocks or phantom edge counts.
    def _resolve(target: str) -> int | None:
        idx = label_to_block.get(target)
        if idx is None or idx >= len(blocks):
            return None
        return idx

    adj: dict[int, list[int]] = {i: [] for i in range(len(blocks))}
    for i, block in enumerate(blocks):
        if not block:
            # Empty block (label-only) falls through
            if i + 1 < len(blocks):
                adj[i].append(i + 1)
            continue

        last_line = block[-1]
        words = last_line.split()
        mnemonic = words[0].upper() if words else ""

        if mnemonic in {"RET", "RETN", "RETF"}:
            # No successor — function exit
            pass
        elif mnemonic == "JMP":
            # Unconditional jump — try to resolve target
            if len(words) > 1:
                target_block = _resolve(words[-1].strip())
                if target_block is not None:
                    adj[i].append(target_block)
            # No fallthrough for unconditional jumps
        elif mnemonic in BRANCH_INSTRUCTIONS:
            # Conditional branch — both fallthrough and target
            if i + 1 < len(blocks):
                adj[i].append(i + 1)
            if len(words) > 1:
                target_block = _resolve(words[-1].strip())
                if target_block is not None:
                    adj[i].append(target_block)
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
    """Compute structural similarity between two CFGs (0.0-1.0).

    Combines three sub-metrics with equal weight:

    1. **Block-count ratio** - min/max of block counts.
    2. **Edge-count ratio** - min/max of edge counts.
    3. **Block-size histogram cosine similarity** - how similar the
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
    # Element-wise max of each list, then combined (max(sizes1, sizes2)
    # would compare the lists, not their elements).
    widest1 = max(sizes1, default=0)
    widest2 = max(sizes2, default=0)
    max_size = max(widest1, widest2) + 1

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


def minhash_new(num_perm: int = NUM_PERMUTATIONS) -> MinHash:
    """Return a fresh all-max MinHash without regenerating permutations.

    Constructing a MinHash draws the permutation arrays from a numpy random
    stream on every call (~260 µs at 128 perms — most of the import
    worker's CPU).  Cloning a cached template (deepcopy of two small numpy
    arrays) is ~15x faster and yields identical permutations (seed 1), so
    fingerprints are byte-for-byte the same.
    """
    import copy

    from .minhash import MinHash

    template = _MINHASH_TEMPLATES.get(num_perm)
    if template is None:
        # Double-checked: re-probe inside the lock so exactly one racer
        # constructs and publishes the template (see the lock's comment).
        with _MINHASH_TEMPLATES_LOCK:
            template = _MINHASH_TEMPLATES.get(num_perm)
            if template is None:
                template = MinHash(num_perm=num_perm)
                # Materialize the permutation table on the template before
                # it is cached.  ``MinHash.permutations`` is lazy, so a clone
                # that inherited ``_permutations = None`` regenerated the
                # table on its first update (~290 µs) — exactly the cost this
                # template exists to avoid, making the cache a no-op.  Reading
                # the property here means every clone deep-copies the ready
                # arrays instead (~10 µs per fingerprint, ~30x faster).
                _ = template.permutations
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
    if num_perm < 2 or num_perm > MAX_NUM_PERM:
        raise ValueError(f"Corrupt MinHash payload: implausible permutation count {num_perm}.")
    expected = 8 + 4 * num_perm
    if len(data) != expected:
        raise ValueError(f"Corrupt MinHash payload: expected {expected} bytes, got {len(data)}.")
    return num_perm


def minhash_pack(m: MinHash) -> bytes:
    """Serialize a MinHash into a compact, self-describing byte string.

    The format is ``MINHASH_MAGIC`` + big-endian uint32 ``num_perm`` +
    ``num_perm`` big-endian uint32 hash values (520 bytes for the default
    128 permutations — several times smaller than a pickle).
    """
    digest = m.digest()
    num_perm = len(digest)
    # ``astype(">u4").tobytes()`` emits the whole uint32 array in one C pass;
    # the equivalent ``struct.pack`` with 129 separate arguments measured ~8x
    # slower.  Hash values are masked to 32 bits by construction.
    return MINHASH_MAGIC + struct.pack(">I", num_perm) + digest.astype(">u4").tobytes()


def minhash_unpack(data: bytes) -> MinHash:
    """Deserialize a MinHash stored with :func:`minhash_pack`.

    Malformed payloads raise ``ValueError`` (never low-level ``struct``
    errors).  Blobs without the ``RMLH`` magic — including legacy pickled
    fingerprints from very old databases — raise ``ValueError`` as well:
    deserializing them would execute attacker-controlled pickle code (a
    hostile ``merge`` source or a planted cache file becomes remote code
    execution).  Legacy rows self-heal instead: the version-stamp check
    routes old databases through :func:`db_reindex`, which recomputes every
    fingerprint from its snippet's code.
    """
    if not data.startswith(MINHASH_MAGIC):
        raise ValueError("Corrupt MinHash payload: missing RMLH magic (unsupported format).")
    from .minhash import MinHash

    num_perm = minhash_num_perm(data)
    values = struct.unpack(f">{num_perm}I", data[8 : 8 + 4 * num_perm])
    return MinHash(num_perm=num_perm, hashvalues=list(values))


def require_same_num_perm(num_perm_a: int, num_perm_b: int) -> None:
    """Raise the shared mismatch error when two blobs disagree on num_perm.

    datasketch's ``MinHash.jaccard`` rejects differently-sized fingerprints;
    every Jaccard path here (scalar, batched, sampled) raises this exact
    error for the same condition, so the message lives in one place.
    """
    if num_perm_a != num_perm_b:
        raise ValueError(
            "Cannot compute Jaccard for MinHash blobs with different "
            f"permutation counts ({num_perm_a} vs {num_perm_b})."
        )


def minhash_jaccard(packed_a: bytes, packed_b: bytes) -> float:
    """Jaccard similarity of two stored MinHash byte blobs (0.0-1.0).

    Fast path: when both blobs use the compact packed format, similarity is
    computed directly from the uint32 arrays, bypassing the ``MinHash``
    constructor — which dominates the cost when scoring thousands of
    candidates (the constructor is ~300 µs per object).  Blobs in any other
    format raise ``ValueError`` (they are never deserialized — see
    :func:`minhash_unpack`).

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
    require_same_num_perm(num_perm_a, minhash_num_perm(packed_b))
    a = struct.unpack(f">{num_perm_a}I", packed_a[8 : 8 + 4 * num_perm_a])
    b = struct.unpack(f">{num_perm_a}I", packed_b[8 : 8 + 4 * num_perm_a])
    # ``map(operator.eq, ...)`` iterates with C-level callbacks instead of a
    # Python ``for``/generator — ~1.6x faster over 128 hash values.
    return sum(map(operator.eq, a, b)) / num_perm_a


def minhash_jaccard_batch(
    query_packed: bytes, packed_list: Sequence[bytes], chunk_size: int = 50_000
) -> list[float]:
    """Jaccard of one packed fingerprint against many, vectorized with numpy.

    Every blob (query and candidates) must use the compact packed format;
    a blob in any other format raises ``ValueError`` (blobs are never
    deserialized — see :func:`minhash_unpack`).  The vectorized pass loads
    each candidate's uint32 hash values with ``numpy.frombuffer`` (no
    per-blob ``struct.unpack`` Python loops) and compares the whole
    ``(N, 128)`` array against the query row in one C-level pass — measured
    ~7x faster than the per-blob path at 10k candidates.  Results are
    bit-for-bit identical to repeated :func:`minhash_jaccard` calls:
    equality counts are small integers and the ``num_perm`` divisor is a
    power of two, so both paths round identically.  Candidates are processed
    in chunks to bound peak memory.

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
    expected_len = 8 + 4 * num_perm
    for start in range(0, len(packed_list), chunk_size):
        chunk = packed_list[start : start + chunk_size]
        # A well-formed packed blob's length implies its permutation count,
        # so the common candidate only pays one C-level ``len`` comparison;
        # a mismatched or corrupt blob falls back to the full header parse
        # and raises the precise error.  (A blob with a matching claimed
        # count but a wrong length used to slip past validation and break
        # the reshape below; rejecting it here also closes that path.)
        for p in chunk:
            if len(p) != expected_len:
                require_same_num_perm(num_perm, minhash_num_perm(p))
        # The list comprehension lets ``bytes.join`` take its pre-sized
        # fast path; over a generator the same join measured ~2x slower
        # (this copy dominates a 10k-candidate find's scoring pass).
        values = np.frombuffer(b"".join([p[8:] for p in chunk]), dtype=">u4").reshape(
            len(chunk), num_perm
        )
        results.extend((values == query_values[None, :]).mean(axis=1).tolist())
    return results


def minhash_ensure_packed(data: bytes) -> bytes:
    """Return *data* in the compact packed format, validating its header.

    Blobs without the ``RMLH`` magic raise ``ValueError`` (they are never
    deserialized — see :func:`minhash_unpack`).  Callers treat that as
    "corrupt fingerprint" and heal it by recomputing from the snippet's
    code.
    """
    if data.startswith(MINHASH_MAGIC):
        # Validate rather than trust: blobs from merged databases or legacy
        # files may be corrupt, and every downstream use (banding, Jaccard,
        # the query path) assumes a well-formed header.
        minhash_num_perm(data)
        return data
    return minhash_pack(minhash_unpack(data))


def minhash_from_tokens(
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
    return minhash_from_tokens(code_tokenize(code_snippet, normalize), ngram_size, num_perm)


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
        results.append(minhash_from_tokens(tokens, ngram_size, num_perm))
    return results
