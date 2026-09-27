"""Unit tests for the output palette."""

import unittest

from rich.color import Color

from resembl.theme import ACCENT, SCORE_STRONG, SCORE_WEAK, score_color


class TestTheme(unittest.TestCase):
    """Tests for the color decisions in ``resembl.theme``."""

    def test_score_color_tiers(self):
        """A score's color follows the tier boundaries, inclusive below."""
        self.assertEqual(score_color(SCORE_STRONG), "green")
        self.assertEqual(score_color(100.0), "green")
        self.assertEqual(score_color(SCORE_STRONG - 0.01), "yellow")
        self.assertEqual(score_color(SCORE_WEAK), "yellow")
        self.assertEqual(score_color(SCORE_WEAK - 0.01), "red")
        self.assertEqual(score_color(0.0), "red")

    def test_score_color_covers_the_whole_scale(self):
        """Every score in 0-100 maps to a color rich knows."""
        for hundredths in range(10001):
            score = hundredths / 100
            self.assertIn(score_color(score), {"green", "yellow", "red"}, f"score {score}")

    def test_accent_is_a_renderable_color(self):
        """The accent is a hex rich can paint, not a typo it silently drops."""
        self.assertEqual(Color.parse(ACCENT).name.lower(), ACCENT.lower())


if __name__ == "__main__":
    unittest.main()
