"""Integration tests verifying --no-color suppresses ANSI escape codes."""

import os
import re
from unittest.mock import patch

from tests.test_cli import BaseCLITest

# Matches any ANSI escape sequence (CSI sequences and OSC sequences).
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[\d;]*[a-zA-Z]|\x1b\][\d;]*\x07")


class TestNoColorOutput(BaseCLITest):
    """Verify that --no-color prevents ANSI/Rich markup in output."""

    def _assert_no_ansi(self, text: str, label: str) -> None:
        """Assert that a string contains no ANSI escape sequences."""
        matches = ANSI_ESCAPE_RE.findall(text)
        self.assertEqual(matches, [], f"ANSI escapes found in {label}: {matches!r}")

    def test_no_color_help(self) -> None:
        """--help output with --no-color should be escape-free."""
        result = self.run_command("--no-color --help")
        self._assert_no_ansi(result.stdout, "stdout")
        self._assert_no_ansi(result.stderr, "stderr")

    def test_no_color_stats(self) -> None:
        """stats command with --no-color should produce plain text."""
        result = self.run_command("--no-color stats")
        self.assertEqual(result.returncode, 0)
        self._assert_no_ansi(result.stdout, "stdout")
        self._assert_no_ansi(result.stderr, "stderr")

    def test_no_color_add(self) -> None:
        """add command with --no-color should produce plain text."""
        result = self.run_command("--no-color add test_snippet 'MOV EAX, 1'")
        self.assertEqual(result.returncode, 0)
        self._assert_no_ansi(result.stdout, "stdout")
        self._assert_no_ansi(result.stderr, "stderr")

    def test_no_color_list(self) -> None:
        """list command with --no-color should produce plain text."""
        self.run_command("--no-color add mysnippet 'RET'")
        result = self.run_command("--no-color list")
        self.assertEqual(result.returncode, 0)
        self._assert_no_ansi(result.stdout, "stdout")
        self._assert_no_ansi(result.stderr, "stderr")

    def test_no_color_config_list(self) -> None:
        """config list command with --no-color should produce plain text."""
        result = self.run_command("--no-color config list")
        self.assertEqual(result.returncode, 0)
        self._assert_no_ansi(result.stdout, "stdout")
        self._assert_no_ansi(result.stderr, "stderr")


class TestCompareAccent(BaseCLITest):
    """The compare story: only the hybrid score wears the accent.

    A value that carried a color sequence is one Rich styled; the other
    four metric rows render their numbers bare, so the accented number is
    the one that stands out in the table.
    """

    #: A foreground SGR: any ``ESC [ <params> m`` that sets a color.
    COLOR_SGR_RE = re.compile(r"\x1b\[[\d;]*3[0-9]")

    def test_only_hybrid_score_is_accented(self) -> None:
        self.run_command("add other_snippet 'MOV EBX, 2; RET'")
        listing = self.run_command("--format json list")
        checksums = re.findall(r'"([0-9a-f]{64})"', listing.stdout)
        self.assertEqual(len(checksums), 2)

        # NO_COLOR and a dumb TERM are the CI defaults; drop them so Rich
        # emits the color the accent is, and name the terminal that has it.
        with patch.dict(os.environ, {"FORCE_COLOR": "1", "TERM": "xterm-256color"}):
            os.environ.pop("NO_COLOR", None)
            result = self.run_command(f"compare {checksums[0]} {checksums[1]}")
        self.assertEqual(result.returncode, 0, result.stderr)

        rows = {
            name: self._metric_row(result.stdout, label) for name, label in _METRIC_NAMES.items()
        }
        for name in ("jaccard", "levenshtein", "cfg", "shared tokens"):
            self.assertEqual(
                self.COLOR_SGR_RE.findall(rows[name]),
                [],
                f"{name} must be uncolored",
            )
        self.assertTrue(
            self.COLOR_SGR_RE.search(rows["hybrid"]),
            "the hybrid score must carry the accent",
        )

    def _metric_row(self, output: str, metric: str) -> str:
        """Return the rendered line of the *metric* row, failing if absent."""
        row = next((line for line in output.splitlines() if metric in line), None)
        self.assertIsNotNone(row, f"the compare table has no {metric} row")
        return row or ""


#: The five metric rows of the compare table, in render order.
_METRIC_NAMES = {
    "jaccard": "Jaccard Similarity",
    "levenshtein": "Levenshtein Score",
    "hybrid": "Hybrid Score",
    "cfg": "CFG Similarity",
    "shared tokens": "Shared Normalized Tokens",
}
