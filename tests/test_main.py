"""Tests for the __main__ module."""

import runpy
import unittest
from unittest.mock import patch


class TestMain(unittest.TestCase):
    """Tests for the main entry point."""

    @patch("resembl.cli.main")
    def test_main_entry_point(self, mock_main):
        """Running the module calls ``cli.main`` exactly once, regardless of argv.

        The regression this pins: ``__main__`` must not swallow ``SystemExit``
        from typer or re-dispatch — it is a one-line ``main()`` call, so the
        mock must see exactly one invocation and the module must propagate
        whatever ``main`` raises (e.g. ``SystemExit`` from ``--help``).
        """
        with patch("sys.argv", ["resembl", "stats"]):
            runpy.run_module("resembl", run_name="__main__")
        mock_main.assert_called_once_with()

    @patch("resembl.cli.main")
    def test_main_propagates_system_exit(self, mock_main):
        """``SystemExit`` from ``main()`` is not swallowed by ``__main__``."""
        mock_main.side_effect = SystemExit(2)
        with patch("sys.argv", ["resembl", "--help"]):
            with self.assertRaises(SystemExit) as ctx:
                runpy.run_module("resembl", run_name="__main__")
        self.assertEqual(ctx.exception.code, 2)
        mock_main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
