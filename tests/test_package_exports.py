"""The package's own export surface.

``resembl.__init__`` resolves exports lazily through ``__getattr__``, so a
name it does not list is invisible rather than broken at import time.  These
tests fail when a module is added to the tree and not registered there, and
when ``resembl-find``'s hand-copied find defaults drift from
``ResemblConfig``.
"""

import dataclasses
import unittest
from pathlib import Path

import resembl
from resembl.config import ResemblConfig
from resembl.find_client import _CFG_DEFAULTS

PACKAGE_DIR = Path(resembl.__file__).parent


def package_modules() -> set[str]:
    """Return the importable module names shipped in the package.

    ``__init__`` and ``__main__`` are the package itself and the ``python -m``
    shim, not names a caller reaches as ``resembl.<name>``.
    """
    return {path.stem for path in PACKAGE_DIR.glob("*.py")} - {"__init__", "__main__"}


class TestExportSurface(unittest.TestCase):
    """Tests for the lazily resolved names ``import resembl`` exposes."""

    def test_every_module_is_registered(self):
        """A new module in the tree is reachable as ``resembl.<name>``."""
        self.assertEqual(package_modules() - resembl.SUBMODULES, set())

    def test_registered_submodules_resolve(self):
        """Each registered name resolves, and is the module it claims to be."""
        for name in sorted(resembl.SUBMODULES):
            with self.subTest(name=name):
                module = getattr(resembl, name)
                self.assertEqual(module.__name__, f"resembl.{name}")

    def test_public_names_resolve(self):
        """Every name in ``__all__`` resolves through the lazy dispatch."""
        for name in resembl.__all__:
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(resembl, name))

    def test_unknown_name_raises(self):
        """An unregistered name is an error, not a silent None."""
        with self.assertRaises(AttributeError):
            _ = resembl.not_a_module


class TestFindClientDefaults(unittest.TestCase):
    """``resembl-find`` answers on defaults without importing the config."""

    def test_defaults_mirror_resembl_config(self):
        """The client's copied defaults equal ``ResemblConfig``'s fields.

        The client stays off the config import graph deliberately, so the
        copy is what keeps it in step; nothing else would catch a change to a
        default, and a stale one silently changes which matches a query
        returns.
        """
        config_defaults = {
            field.name: getattr(ResemblConfig(), field.name)
            for field in dataclasses.fields(ResemblConfig)
        }
        self.assertEqual(_CFG_DEFAULTS, {k: config_defaults[k] for k in _CFG_DEFAULTS})


if __name__ == "__main__":
    unittest.main()
