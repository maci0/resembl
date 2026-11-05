#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the CFG extraction and similarity entry points.

``cfg_extract`` parses assembly text that reaches it from a stored
snippet's ``code`` column (``snippet_compare``) — that is, from a
database an untrusted ``merge`` source or a hand-edited file can
write — and ``cfg_similarity`` combines the two extracted graphs into
the score the CLI prints.  Neither has a fuzz target today, so a
label, comment, or branch shape that escapes the block splitter
turns a hostile row into a crash or a phantom edge instead of a
score in ``[0, 1]``.

The harness drives the real entry points with structure-aware
inputs and asserts the invariants the callers rely on, not just the
absence of a crash:

* ``cfg_extract`` never raises for any text, including labels with
  no following block, a branch as the last line, and comment-only
  lines;
* an extracted graph is well-formed: ``num_blocks`` counts the
  blocks, ``adj`` keys are exactly ``range(num_blocks)``, every
  successor index is in range, ``num_edges`` is the successor
  total, and ``block_sizes`` has one non-negative entry per block;
* ``cfg_similarity`` of a graph against itself is exactly ``1.0``
  and of any two graphs is in ``[0, 1]`` and symmetric;
* extraction is deterministic: the same text twice yields the same
  graph, so a reindex cannot change a stored comparison.

Inputs are generated structure-aware: realistic assembly lines
(mnemonics, labels, branches, comments, segment overrides) drawn
from the committed corpus shapes, plus raw unicode and junk bytes.
"""

import math
import sys

import atheris

with atheris.instrument_imports():
    from resembl.scoring import cfg_extract, cfg_similarity

#: Assembly lines worth spending runs on: labels (leading, trailing,
#: chained, segment-override lookalikes), branches and returns, both
#: comment spellings, and blank lines — the shapes the block splitter
#: treats specially.
_INTERESTING_LINES = (
    "",
    "   ",
    "ret",
    "retn",
    "retf",
    "mov eax, 1",
    "mov eax, [fs:0]",
    "push ebp",
    "pop ebp",
    "jmp foo",
    "jz foo",
    "jnz probe_top",
    "call foo",
    "foo:",
    "foo: ret",
    "foo: bar: baz:",
    "label:",
    "; comment",
    "# comment",
    "mov eax, 1 ; trailing",
    "mov eax, 1 # trailing",
    "#foo: not a label",
    "a" * 512,
    "\x00\x01\x02",
    "héllo wörld — ünïcode",
    "ＡＢＣ１２３",  # noqa: RUF001 - fullwidth forms stress normalization
    "jmp",
    "jz",
)


def _text(fdp: atheris.FuzzedDataProvider) -> str:
    """Return fuzzer-chosen assembly text from the interesting set or raw unicode."""
    mode = fdp.ConsumeIntInRange(0, 3)
    if mode == 0:
        lines = [
            _INTERESTING_LINES[fdp.ConsumeIntInRange(0, len(_INTERESTING_LINES) - 1)]
            for _ in range(fdp.ConsumeIntInRange(0, 32))
        ]
        return "\n".join(lines)
    if mode == 1:
        line = _INTERESTING_LINES[fdp.ConsumeIntInRange(0, len(_INTERESTING_LINES) - 1)]
        return line * fdp.ConsumeIntInRange(1, 64)
    if mode == 2:
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 256))
    raw = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 256))
    return raw.decode("utf-8", errors="replace")


def _check_graph(graph: dict) -> None:
    """Assert an extracted graph is well-formed and self-consistent."""
    num_blocks = graph["num_blocks"]
    num_edges = graph["num_edges"]
    block_sizes = graph["block_sizes"]
    adj = graph["adj"]
    assert isinstance(num_blocks, int) and num_blocks >= 0
    assert isinstance(num_edges, int) and num_edges >= 0
    assert len(block_sizes) == num_blocks, "block_sizes disagrees with num_blocks"
    assert all(isinstance(s, int) and s >= 0 for s in block_sizes), "negative block size"
    assert set(adj) == set(range(num_blocks)), "adj keys are not range(num_blocks)"
    for index, successors in adj.items():
        assert isinstance(index, int) and 0 <= index < num_blocks
        for successor in successors:
            assert 0 <= successor < num_blocks, f"edge to non-existent block {successor}"
    assert num_edges == sum(len(s) for s in adj.values()), "num_edges disagrees with adj"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    text = _text(fdp)
    other_text = _text(fdp)

    graph = cfg_extract(text)
    _check_graph(graph)
    # Determinism: the same text twice must extract the same graph.
    assert cfg_extract(text) == graph, "cfg_extract is not deterministic"

    other = cfg_extract(other_text)
    _check_graph(other)

    self_similarity = cfg_similarity(graph, graph)
    assert self_similarity == 1.0, f"self-similarity is {self_similarity}, not 1.0"
    forward = cfg_similarity(graph, other)
    backward = cfg_similarity(other, graph)
    assert 0.0 <= forward <= 1.0, f"similarity out of range: {forward}"
    assert math.isfinite(forward), f"similarity is not finite: {forward}"
    assert forward == backward, f"similarity is not symmetric: {forward} vs {backward}"


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
