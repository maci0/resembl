"""Integration tests verifying --no-color suppresses ANSI escape codes."""

import re

from tests.test_cli import BaseCLITest


# Matches any ANSI escape sequence (CSI sequences and OSC sequences).
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[\d;]*[a-zA-Z]|\x1b\][\d;]*\x07")


class TestNoColorOutput(BaseCLITest):
    """Verify that --no-color prevents ANSI/Rich markup in output."""

    def _assert_no_ansi(self, text: str, label: str) -> None:
        """Assert that a string contains no ANSI escape sequences."""
        matches = ANSI_ESCAPE_RE.findall(text)
        self.assertEqual(
            matches, [], f"ANSI escapes found in {label}: {matches!r}"
        )

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
