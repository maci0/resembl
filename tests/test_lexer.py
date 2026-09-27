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
import sys
import threading
import unittest

from pygments.lexers.asm import NasmLexer
from pygments.token import Comment, Token, Whitespace

from resembl import scoring
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


class TestTokenTypeFlagsConcurrency(unittest.TestCase):
    """The classification cache is published, never mutated in place.

    ``serve`` runs one handler thread per request and every request lexes, so
    this cache is written from several threads while others read it.  Writers
    publish a fresh dict and rebind the name rather than inserting into the
    dict readers hold, so a reader that looked the name up before a
    publication keeps answering from a complete snapshot and can never observe
    one mid-update.  Mutating in place instead would put every reader's lookup
    in a data race with every other thread's insert.
    """

    #: Racers enough to exercise the publication path from several threads at
    #: once; each asks for a type no other racer asks for, so every one of them
    #: takes the publish branch.
    _THREADS = 8

    def setUp(self):
        saved = scoring._type_flags
        self.addCleanup(setattr, scoring, "_type_flags", saved)
        # A thread switch per bytecode, so the racers genuinely interleave
        # rather than happening to run one at a time.
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, previous)
        # A private, empty starting snapshot, so the test neither depends on
        # nor disturbs the process-wide cache.
        scoring._type_flags = {}

    def test_publication_leaves_the_previous_snapshot_intact(self):
        """A reader holding the old dict must not see the new entry appear."""
        held = scoring._type_flags
        scoring._token_type_flags(Token.Name.First)
        self.assertIsNot(scoring._type_flags, held, "the cache was mutated in place")
        self.assertNotIn(
            Token.Name.First,
            held,
            "a publication wrote into a dict another thread may be reading",
        )
        self.assertIn(Token.Name.First, scoring._type_flags)

    def test_cap_holds_when_threads_publish_together(self):
        """Racing publishers must not push the cache past its bound."""
        for i in range(scoring._TYPE_FLAGS_MAX - 1):
            scoring._token_type_flags(getattr(Token.Name, f"Probe{i}"))
        self.assertEqual(len(scoring._type_flags), scoring._TYPE_FLAGS_MAX - 1)

        barrier = threading.Barrier(self._THREADS)

        def publish(index: int) -> None:
            barrier.wait()
            scoring._token_type_flags(getattr(Token.Name, f"Racer{index}"))

        threads = [
            threading.Thread(target=publish, args=(i,), name=f"type-flags-{i}")
            for i in range(self._THREADS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), f"{thread.name} did not finish")

        self.assertLessEqual(len(scoring._type_flags), scoring._TYPE_FLAGS_MAX)
        # The pre-filled entries stay readable: publication copies, it never
        # drops what a snapshot already held.
        self.assertEqual(
            scoring._token_type_flags(Token.Name.Probe0),
            scoring._type_flags[Token.Name.Probe0],
        )

    def test_every_racer_still_gets_its_own_answer(self):
        """A publication that finds the cache full must not cost the answer."""
        for i in range(scoring._TYPE_FLAGS_MAX):
            scoring._token_type_flags(getattr(Token.Name, f"Filler{i}"))
        flags = {}
        for index in range(self._THREADS):
            flags[index] = scoring._token_type_flags(getattr(Token.Name, f"Racer{index}"))
        for index, value in flags.items():
            self.assertEqual(value, scoring._TT_NAME, f"racer{index} was misclassified")


if __name__ == "__main__":
    unittest.main()
