"""Guard the reordered NASM lexer in ``resembl.scoring``.

``resembl.scoring._reordered_nasm_lexer`` moves NasmLexer's
``instruction-args`` whitespace/comment rules to the front of the state so a
space does not fail ~14 regexes before matching (measured 1.55x faster
lexing, 39% fewer regex attempts).  The reorder is exact for the patterns
Pygments currently defines — the moved rules can only match whitespace, ``;``
or ``#``, which no other rule in that state can begin with — but it depends
on that rule set.  These tests fail loudly if a Pygments upgrade invalidates
the assumption, rather than silently changing every stored fingerprint:
token output is diffed against the stock NasmLexer over the corpus, and the
reorder is checked to have actually applied (a silent no-op would only cost
the speedup, but should still be visible).
"""

# pylint: disable=protected-access  # the test pins private Pygments layout

import os
import unittest

from pygments.lexers.asm import NasmLexer
from pygments.token import Comment, Whitespace

from resembl.scoring import get_lexer

_TEST_DATA = os.path.join(os.path.dirname(__file__), "test_data")


def _sample_files() -> list[str]:
    """The committed real-world ``.asm`` corpus, sorted for determinism."""
    return sorted(name for name in os.listdir(_TEST_DATA) if name.endswith(".asm"))


class TestReorderedLexerEquivalence(unittest.TestCase):
    """The reorder must be invisible in the token stream."""

    def test_token_stream_matches_stock_nasmlexer(self):
        stock = NasmLexer()
        fast = get_lexer()
        files = _sample_files()
        self.assertGreater(len(files), 0, "test corpus is missing")
        for name in files:
            with open(os.path.join(_TEST_DATA, name), encoding="utf-8", errors="replace") as f:
                code = f.read()
            self.assertEqual(
                list(fast.get_tokens(code)),
                list(stock.get_tokens(code)),
                f"token mismatch in {name}",
            )

    def test_whitespace_rules_are_tried_first(self):
        """The reorder must have applied, or the speedup silently disappears."""
        rules = get_lexer()._tokens["instruction-args"]
        actions = [action for _match, action, _state in rules]
        early = [action for action in actions if action in (Whitespace, Comment.Single)]
        self.assertGreater(len(early), 0, "no whitespace/comment rules found to reorder")
        self.assertEqual(actions[: len(early)], early, "whitespace rules are not at the front")

    def test_every_action_survives_the_reorder(self):
        """Reordering must not drop or duplicate any rule."""
        stock = NasmLexer()
        fast = get_lexer()
        self.assertEqual(
            len(fast._tokens["instruction-args"]), len(stock._tokens["instruction-args"])
        )


if __name__ == "__main__":
    unittest.main()
