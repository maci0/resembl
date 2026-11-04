"""Unit tests for the output palette."""

import math
import unittest

from rich.color import Color

from resembl.theme import (
    ACCENT,
    LIGHT,
    RICH_STYLES,
    SCORE_STRONG,
    SCORE_WEAK,
    TERMINAL,
    score_color,
)


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

    def test_score_color_nonfinite_and_out_of_range(self):
        """Non-finite / out-of-range scores clamp to the nearest tier.

        Production callers feed ``score_hybrid`` output, which is validated
        to [0, 100]; a NaN must not silently read as a strong green match
        and an out-of-range number must not upgrade beyond its tier.
        """
        self.assertEqual(score_color(float("nan")), "red")
        self.assertEqual(score_color(float("inf")), "green")
        self.assertEqual(score_color(float("-inf")), "red")
        self.assertEqual(score_color(-1.0), "red")
        self.assertEqual(score_color(101.0), "green")
        self.assertNotEqual(score_color(math.nan), "green")

    def test_palettes_share_the_same_roles(self):
        """LIGHT and TERMINAL carry the same role keys, all renderable."""
        self.assertEqual(set(LIGHT), set(TERMINAL))
        for name, hex_value in {**LIGHT, **TERMINAL}.items():
            Color.parse(hex_value)
            self.assertTrue(hex_value.startswith("#"), f"{name}={hex_value!r}")

    def test_rich_styles_map_stock_names_onto_terminal_palette(self):
        """RICH_STYLES resolves every stock name to a TERMINAL hex."""
        terminal_hexes = set(TERMINAL.values())
        for style_name, style_value in RICH_STYLES.items():
            hex_part = style_value.replace("bold ", "").strip()
            self.assertIn(hex_part, terminal_hexes, f"{style_name}={style_value!r}")
        # Both bold spellings exist for every base name.
        for base in ("green", "cyan", "blue", "magenta", "yellow", "red"):
            self.assertIn(base, RICH_STYLES)
            self.assertIn(f"bold {base}", RICH_STYLES)
            self.assertIn(f"{base} bold", RICH_STYLES)

    def test_accent_is_a_renderable_color(self):
        """The accent is a hex rich can paint, not a typo it silently drops."""
        self.assertEqual(Color.parse(ACCENT).name.lower(), ACCENT.lower())


if __name__ == "__main__":
    unittest.main()
