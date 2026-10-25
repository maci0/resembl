"""The fuzz extra selects one Atheris requirement for each interpreter.

The cp315 wheel is only the CPython 3.15 Linux x86_64 requirement. Python
3.16, PyPy, and other machines get the maintained source. Interpreters
below 3.15 keep upstream Atheris.
"""

import tomllib
import unittest
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement

_ROOT = Path(__file__).resolve().parents[1]
_WHEEL = "6a1dd5169f6b9448d204164917da4749706c070efd0053120c22702d6c1dbd9c"
_SOURCE = "5f7fa7f9084d557a7b6002a9471dac88a0a36cd0"


def _requirements() -> list[Requirement]:
    manifest = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [Requirement(line) for line in manifest["project"]["optional-dependencies"]["fuzz"]]


def _selected(environment: dict[str, str]) -> Requirement:
    matched = [
        req
        for req in _requirements()
        if req.marker is not None and req.marker.evaluate(environment)
    ]
    if len(matched) != 1:
        raise AssertionError(f"{environment} matched {[str(req) for req in matched]}")
    return matched[0]


class FuzzExtraMarkerTests(unittest.TestCase):
    def test_matrix(self) -> None:
        cpython_linux = {
            "platform_python_implementation": "CPython",
            "sys_platform": "linux",
        }
        cases = (
            ({**cpython_linux, "python_version": "3.15", "platform_machine": "x86_64"}, "wheel"),
            ({**cpython_linux, "python_version": "3.16", "platform_machine": "x86_64"}, "source"),
            ({**cpython_linux, "python_version": "3.15", "platform_machine": "aarch64"}, "source"),
            (
                {
                    "python_version": "3.15",
                    "platform_python_implementation": "CPython",
                    "sys_platform": "darwin",
                    "platform_machine": "arm64",
                },
                "source",
            ),
            (
                {
                    "python_version": "3.15",
                    "platform_python_implementation": "CPython",
                    "sys_platform": "win32",
                    "platform_machine": "AMD64",
                },
                "source",
            ),
            (
                {
                    "python_version": "3.15",
                    "platform_python_implementation": "PyPy",
                    "sys_platform": "linux",
                    "platform_machine": "x86_64",
                },
                "source",
            ),
            ({**cpython_linux, "python_version": "3.14", "platform_machine": "x86_64"}, "upstream"),
        )
        for environment, kind in cases:
            with self.subTest(environment=environment):
                req = _selected(environment)
                url = "" if req.url is None else req.url
                if kind == "wheel":
                    self.assertIn(_WHEEL, url)
                    self.assertIn("relumea.2", url)
                    self.assertNotIn("relumea.1", url)
                elif kind == "source":
                    self.assertIn(_SOURCE, url)
                    self.assertNotIn(_WHEEL, url)
                else:
                    self.assertIsNone(req.url)
                    self.assertEqual(str(req).split(";", 1)[0].strip(), "atheris>=3.1.0")

    def test_markers_parse(self) -> None:
        for req in _requirements():
            self.assertIsInstance(req.marker, Marker)


if __name__ == "__main__":
    unittest.main()
