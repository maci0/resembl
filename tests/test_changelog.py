"""Release-contract tests: the manifest version, the tag links and the
changelog sections have to agree, a `**Breaking:**` note may only ship in a
major release, and the supported-versions table has to name the release the
manifest carries.

These are the ways this repository's release contract can go wrong without
any test noticing: the manifest bumped for a release whose changelog section
was never written, a section renamed or added without its `[X.Y.Z]:` link
reference, a breaking change cut as a minor or a patch, and a release whose
number no policy document acknowledges.
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

#: The sentence in SECURITY.md that names the release the project supports.
_CURRENT_RELEASE_RE = re.compile(r"The current release is (?P<version>\d+\.\d+\.\d+)\.")

#: One row of the SECURITY.md supported-versions table, e.g. ``| 2.x (...) | yes |``.
_SUPPORTED_ROW_RE = re.compile(r"^\| (?P<major>\d+)\.x[^|]*\| yes \|\s*$", re.MULTILINE)

#: A phrase in a ``### Changed`` / ``### Fixed`` / ``### Removed`` note that
#: means the note changes a contract an existing caller depends on: a process
#: exit code, the stream a record is written to, a name that was dropped, a
#: name that moved, or a minimum version a running install no longer meets.
_CONTRACT_CHANGE_RE = re.compile(
    r"exits `[012]`"
    r"|exit `?[012]`?"
    r"|to stderr rather than stdout"
    r"|are gone from"
    r"|is gone\."
    r"|`resembl\.[a-z_.]+` is removed"
    r"|are removed\b"
    r"|is removed\b"
    r"|is renamed"
    r"|moved to the new"
    r"|are no longer supported"
)


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


def unreleased_body(text: str) -> str:
    """Return the body of ``## [Unreleased]``, up to the next release heading."""
    match = re.search(r"^## \[Unreleased\]\s*$", text, re.MULTILINE)
    if match is None:
        return ""
    rest = text[match.end() :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def parse_bullets(body: str) -> list[tuple[str, str]]:
    """Return ``(bullet, category)`` for each bullet under a ``###`` heading.

    A bullet is the ``- `` line plus every continuation line up to the next
    bullet or heading, so a phrase on a wrapped line is read with the bullet it
    belongs to.  The category is the heading the bullet sits under, or the
    empty string for a bullet written before the first heading.
    """
    bullets: list[tuple[str, str]] = []
    category = ""
    current: list[str] = []
    for line in body.splitlines():
        if line.startswith("### "):
            category = line[4:].strip()
        elif line.startswith("- "):
            if current:
                bullets.append(("\n".join(current), category))
            current = [line]
        elif current and line.strip():
            current.append(line)
    if current:
        bullets.append(("\n".join(current), category))
    return bullets


def unmarked_break_violations(sections: list[tuple[str, str, str]]) -> list[str]:
    """Return a complaint per released section that hides a break from a consumer.

    ``breaking_major_violations`` reads the ``**Breaking:**`` markers, so a
    section that changes an exit code, a stream or a name without one is
    invisible to it, and the release it describes can be cut as a patch.  This
    reads the notes themselves: a bullet that changes a contract and carries
    no marker, in a section that is not a major bump over the one below it,
    is a break a 1.x caller was never told about.
    """
    violations: list[str] = []
    for (version, _, body), (previous, _, _) in pairwise(sections):
        current_major = int(version.split(".")[0])
        previous_major = int(previous.split(".")[0])
        if current_major == previous_major + 1:
            continue
        for bullet, category in parse_bullets(body):
            if category == "Added" or "**Breaking:**" in bullet:
                continue
            if _CONTRACT_CHANGE_RE.search(bullet) is None:
                continue
            violations.append(
                f"[{version}] changes a contract under '### {category}' with no "
                f"**Breaking:** marker, and is not a major bump over [{previous}]"
            )
    return violations


class TestChangelog(unittest.TestCase):
    """The changelog, the manifest version and the tag links agree."""

    @classmethod
    def setUpClass(cls):
        cls.root = project_root()
        cls.text = (cls.root / "CHANGELOG.md").read_text(encoding="utf-8")
        cls.security = (cls.root / "SECURITY.md").read_text(encoding="utf-8")
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

    def test_unmarked_contract_change_ships_only_in_a_major(self):
        """A contract change with no marker still requires a major bump.

        The marker check above reads the markers, so a note that drops an exit
        code, moves a record to another stream or removes an export while
        describing none of them as a break passes it.  This reads the notes,
        which is the half that was unguarded.
        """
        self.assertEqual(unmarked_break_violations(self.sections), [])

    def test_unmarked_break_check_reads_a_break_in_any_position(self):
        """The unmarked check is the one a release leans on, so pin its edges.

        A synthetic changelog carries the note the real file has to reject: a
        patch release whose ``### Fixed`` bullet changes an exit code with no
        marker.  A change that hides under ``### Added`` is a first appearance,
        not a break to an existing contract, so it is left alone; a marked
        bullet in a major is the case that is already allowed.
        """
        unmarked = "- `resembl list` exits `1` where it exited `0`.\n"
        under_added = "- `resembl list --count` exits `0`, and the count is printed.\n"
        marked = "- **Breaking:** `resembl list` exits `1` where it exited `0`.\n"
        cases = [
            (
                [
                    ("1.2.0", "2026-09-15", f"### Fixed\n\n{unmarked}"),
                    ("1.1.0", "2026-09-13", "### Fixed\n\n- A.\n"),
                ],
                [
                    (
                        "[1.2.0] changes a contract under '### Fixed' with no "
                        "**Breaking:** marker, and is not a major bump over [1.1.0]"
                    )
                ],
            ),
            (
                [
                    ("1.2.0", "2026-09-15", f"### Added\n\n{under_added}"),
                    ("1.1.0", "2026-09-13", "### Added\n\n- A.\n"),
                ],
                [],
            ),
            (
                [
                    ("2.0.0", "2026-09-15", f"### Changed\n\n{marked}"),
                    ("1.2.0", "2026-09-15", "### Fixed\n\n- A.\n"),
                ],
                [],
            ),
        ]
        for sections, expected in cases:
            with self.subTest(sections=sections[0][0]):
                self.assertEqual(unmarked_break_violations(sections), expected)

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

    def test_supported_versions_name_the_released_line(self):
        """SECURITY.md has to acknowledge the version the manifest carries.

        The release commit bumps the manifest and the changelog in one change;
        the supported-versions table is prose next to them, so a major bump
        lands without it and leaves the project telling reporters that the
        line it just abandoned is the supported one.  Both the sentence and
        the table are checked against the manifest, so the omission fails the
        suite instead of a bug report.
        """
        match = _CURRENT_RELEASE_RE.search(self.security)
        self.assertIsNotNone(match, "SECURITY.md does not name the current release")
        assert match is not None  # narrows for the type checker
        self.assertEqual(
            match["version"],
            self.manifest_version,
            "SECURITY.md names a different release than pyproject.toml carries",
        )
        supported = [row["major"] for row in _SUPPORTED_ROW_RE.finditer(self.security)]
        self.assertEqual(
            supported,
            [self.manifest_version.split(".")[0]],
            "the supported-versions table does not mark the released line as supported",
        )

    def test_unreleased_section_is_grouped_by_impact(self):
        """``[Unreleased]`` is the source the next release inherits from.

        Its notes have to sit under Keep a Changelog impact headings, and a
        breaking note has to be marked, or a consumer cannot tell what needs
        a migration step.
        """
        match = re.search(r"^## \[Unreleased\]\s*$", self.text, re.MULTILINE)
        if match is None:
            return
        rest = self.text[match.end() :]
        end = rest.find("\n## ")
        body = rest if end == -1 else rest[:end]
        bullets = [line for line in body.splitlines() if line.startswith("- ")]
        if not bullets:
            return
        for line in body.splitlines():
            if line.startswith("### "):
                self.assertRegex(line, _CATEGORY_RE)
        for line in body.splitlines():
            if re.match(r"^\s*-\s+\*?\*?Breaking", line):
                self.assertIn("**Breaking:**", line)

    def test_behavior_change_bullets_are_marked_breaking(self):
        """A bullet that changes a contract a caller already relies on is marked.

        ``test_unreleased_section_is_grouped_by_impact`` only checks the
        bullets that already carry a marker, so a note can state an exit-code,
        stream or export change with no marker and pass.  Those are the breaks
        a consumer is least likely to notice, because the command still runs
        and the wrong answer comes out somewhere else, so each phrase in
        ``_CONTRACT_CHANGE_RE`` has to be introduced by ``**Breaking:**``.

        Only the impact headings a behavior change can hide under are read: a
        bullet under ``### Added`` names a first appearance, which is not a
        change to an existing contract.  The phrases are a list rather than a
        derivation because there is no mechanical way to tell a note that
        changes a contract from one that only describes it.
        """
        for bullet, category in parse_bullets(unreleased_body(self.text)):
            if category == "Added" or _CONTRACT_CHANGE_RE.search(bullet) is None:
                continue
            self.assertIn(
                "**Breaking:**",
                bullet,
                f"a bullet under '### {category}' that changes a contract is not marked",
            )


if __name__ == "__main__":
    unittest.main()
