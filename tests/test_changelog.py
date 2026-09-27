"""Release-contract tests: the manifest version, the tag links and the
changelog sections have to agree, and a `**Breaking:**` note may only ship in
a major release.

These are the three ways this repository's release contract can go wrong
without any test noticing: the manifest bumped for a release whose changelog
section was never written, a section renamed or added without its
`[X.Y.Z]:` link reference, and a breaking change cut as a minor or a patch.
The release procedure (CONTRIBUTING.md, Part 6) is a manual sequence of
steps, so these are the parts of it a test can hold.
"""

import re
import tomllib
import unittest
from itertools import pairwise
from pathlib import Path

#: Every `## [X.Y.Z] - date` heading, in the order they appear.
_SECTION_RE = re.compile(r"^## \[(?P<version>\d+\.\d+\.\d+)\] - (?P<date>\d{4}-\d{2}-\d{2})\s*$")

#: The marker a consumer-facing note uses to flag a breaking change.
_BREAKING_RE = re.compile(r"^[ \t]*-[ \t]+\*\*Breaking:\*\*", re.MULTILINE)

#: The ``[X.Y.Z]: <url>`` link reference block at the foot of the file.
_LINK_RE = re.compile(r"^\[(?P<version>[^\]]+)\]:\s+\S+", re.MULTILINE)

#: A Keep a Changelog impact heading, e.g. ``### Changed``.
_CATEGORY_RE = re.compile(r"^### (Added|Changed|Deprecated|Removed|Fixed|Security)\s*$")


def project_root() -> Path:
    """Return the repository root, located by walking up for pyproject.toml."""
    for candidate in (Path(__file__).resolve().parent, *Path(__file__).resolve().parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "CHANGELOG.md").is_file():
            return candidate
    raise FileNotFoundError("no directory with both pyproject.toml and CHANGELOG.md")


def parse_sections(text: str) -> list[tuple[str, str, str]]:
    """Return ``(version, date, body)`` per released section, newest first.

    The body runs to the next heading of any level, so the link-reference
    block at the foot is not mistaken for section content.
    """
    lines = text.splitlines()
    starts = [
        (index, match) for index, line in enumerate(lines) if (match := _SECTION_RE.match(line))
    ]
    sections: list[tuple[str, str, str]] = []
    for position, (index, match) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        sections.append((match["version"], match["date"], "\n".join(lines[index + 1 : end])))
    return sections


def breaking_major_violations(sections: list[tuple[str, str, str]]) -> list[str]:
    """Return a complaint per released section whose break needs a major bump.

    A section pairs with the release below it, so the newest section is
    checked against the one before it and the oldest against nothing.
    """
    violations: list[str] = []
    for (version, _, body), (previous, _, _) in pairwise(sections):
        if not _BREAKING_RE.search(body):
            continue
        current_major = int(version.split(".")[0])
        previous_major = int(previous.split(".")[0])
        if current_major != previous_major + 1:
            violations.append(
                f"[{version}] documents a breaking change but is not a major "
                f"bump over [{previous}]"
            )
    return violations


class TestChangelog(unittest.TestCase):
    """The changelog, the manifest version and the tag links agree."""

    @classmethod
    def setUpClass(cls):
        cls.root = project_root()
        cls.text = (cls.root / "CHANGELOG.md").read_text(encoding="utf-8")
        cls.sections = parse_sections(cls.text)
        with (cls.root / "pyproject.toml").open("rb") as handle:
            cls.manifest_version = tomllib.load(handle)["project"]["version"]

    def test_manifest_version_has_a_changelog_section(self):
        """The version in pyproject.toml must be a released changelog section.

        The release commit bumps the manifest and writes the section in one
        change; a manifest version with no section is a release whose notes
        were never written, and no link reference points at it either.
        """
        released = {version for version, _, _ in self.sections}
        self.assertIn(self.manifest_version, released)

    def test_sections_are_strictly_descending(self):
        """A newer section has to carry a higher version than the one below."""
        parsed = [
            tuple(int(part) for part in version.split(".")) for version, _, _ in self.sections
        ]
        for newer, older in pairwise(parsed):
            self.assertGreater(newer, older, f"sections out of order: {self.sections}")

    def test_every_section_has_a_link_reference(self):
        """Each released version needs its ``[X.Y.Z]:`` link at the foot."""
        links = {match["version"] for match in _LINK_RE.finditer(self.text)}
        for version, _, _ in self.sections:
            self.assertIn(version, links, f"no link reference for [{version}]")

    def test_breaking_notes_ship_only_in_a_major(self):
        """A ``**Breaking:**`` note requires a major bump over the last release.

        The next release inherits every ``**Breaking:**`` bullet from
        ``[Unreleased]``, so this is what catches a break cut as 2.0.1: rename
        the section, and if the number is not a major bump the test fails
        before the tag does.
        """
        self.assertTrue(len(self.sections) > 1, "no released sections parsed")
        self.assertEqual(breaking_major_violations(self.sections), [])

    def test_breaking_major_check_reads_a_break_in_any_position(self):
        """The check is what makes the next release honest, so pin its edges.

        A synthetic changelog carries the break the real file has to reject:
        a ``**Breaking:**`` note cut as a patch.  A break in the first section
        is invisible, because that release has no predecessor to compare to.
        """
        breaking = "\n### Changed\n\n- **Breaking:** a default moved.\n"
        sections = parse_sections(
            "## [2.0.1] - 2026-09-20\n"
            + breaking
            + "## [2.0.0] - 2026-09-15\n\n### Added\n\n- A.\n"
        )
        self.assertEqual(
            breaking_major_violations(sections),
            ["[2.0.1] documents a breaking change but is not a major bump over [2.0.0]"],
        )
        major = parse_sections(
            "## [3.0.0] - 2026-09-20\n"
            + breaking
            + "## [2.0.0] - 2026-09-15\n\n### Added\n\n- A.\n"
        )
        self.assertEqual(breaking_major_violations(major), [])

    def test_unreleased_section_is_grouped_by_impact(self):
        """``[Unreleased]`` is the source the next release inherits from.

        Its notes have to sit under Keep a Changelog impact headings, and a
        breaking note has to be marked, or a consumer cannot tell what needs
        a migration step.
        """
        match = re.search(r"^## \[Unreleased\]\s*$", self.text, re.MULTILINE)
        self.assertIsNotNone(match, "no [Unreleased] section")
        assert match is not None  # narrows for the type checker
        rest = self.text[match.end() :]
        end = rest.find("\n## ")
        body = rest if end == -1 else rest[:end]
        bullets = [line for line in body.splitlines() if line.startswith("- ")]
        self.assertTrue(bullets, "[Unreleased] has no entries")
        for line in body.splitlines():
            if line.startswith("### "):
                self.assertRegex(line, _CATEGORY_RE)
        for line in body.splitlines():
            if re.match(r"^\s*-\s+\*?\*?Breaking", line):
                self.assertIn("**Breaking:**", line)


if __name__ == "__main__":
    unittest.main()
