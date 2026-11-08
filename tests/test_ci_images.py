"""The CI service containers and runners stay pinned.

A tag alone moves between pulls, so an unpinned `image:` in a workflow
would test against a different database image than the one reviewed.
Each workflow that declares a service container must pin it by digest,
and every pinned image must be watched by Dependabot's docker ecosystem
so a new upstream release only arrives as a reviewed pull request.

The same holds for the hosted runner OS: a `-latest` label migrates
between OS versions under a green run, so every `runs-on:` and every
portability matrix entry must name a versioned label. The portability
matrix is covered by the `runs-on:` check through its `${{ matrix.os }}`
indirection: the matrix values are the labels, so they are checked too.
"""

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = sorted((_ROOT / ".github" / "workflows").glob("*.yml"))
_PINNED = re.compile(r"image:\s*\S+@sha256:[0-9a-f]{64}\s*$", re.MULTILINE)
_UNPINNED = re.compile(r"image:\s*\S+\s*$", re.MULTILINE)


class CiImagePinTests(unittest.TestCase):
    def test_service_images_are_digest_pinned(self) -> None:
        checked = 0
        for workflow in _WORKFLOWS:
            text = workflow.read_text(encoding="utf-8")
            images = _UNPINNED.findall(text)
            with self.subTest(workflow=workflow.name):
                for line in images:
                    self.assertRegex(
                        line,
                        r"^image:\s*\S+@sha256:[0-9a-f]{64}\s*$",
                        f"{workflow.name}: {line.strip()!r} is not digest-pinned",
                    )
                checked += len(images)
        self.assertGreater(checked, 0, "no service images found to check")

    def test_runner_labels_are_version_pinned(self) -> None:
        """No workflow may use a floating `-latest` runner label."""
        checked = 0
        for workflow in _WORKFLOWS:
            text = workflow.read_text(encoding="utf-8")
            with self.subTest(workflow=workflow.name):
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("runs-on:"):
                        if "${{" in stripped:
                            continue
                        self.assertRegex(
                            stripped,
                            r"^runs-on:\s+\S+-\d",
                            f"{workflow.name}: unpinned runs-on: {stripped!r}",
                        )
                        checked += 1
                    if stripped.startswith("os: ["):
                        for label in re.findall(r"[\w-]+-\d[\w.]*", stripped):
                            self.assertRegex(
                                label,
                                r"^(ubuntu|macos|windows)-\d+(?:\.\d+)?$",
                                f"{workflow.name}: unpinned matrix os {label!r}",
                            )
                            checked += 1
        self.assertGreater(checked, 0, "no runner labels found to check")

    def test_dependabot_watches_docker(self) -> None:
        text = (_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: docker", text)

    def test_service_dockerfile_is_digest_pinned(self) -> None:
        text = (_ROOT / "service" / "Dockerfile").read_text(encoding="utf-8")
        # The build stage pins its base image through the GO_IMAGE arg;
        # the runtime stage is `scratch` and needs no pin.
        self.assertRegex(text, r"ARG GO_IMAGE=\S+@sha256:[0-9a-f]{64}")
        self.assertIn("FROM ${GO_IMAGE} AS build", text)

    def test_db_service_wiring_agrees_between_workflows(self) -> None:
        """tests.yml and coverage.yml declare the same PostgreSQL/MySQL services.

        Both workflows stand up the integration databases so the reported
        coverage covers them too. A change to one file's service block
        (image, credential, health check, integration URL) without the
        other would make the two runs test different databases while
        claiming the same coverage.
        """
        tests = (_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        coverage = (_ROOT / ".github" / "workflows" / "coverage.yml").read_text(encoding="utf-8")
        for needle in (
            "image: postgres:",
            "image: mysql:",
            "POSTGRES_PASSWORD:",
            "MYSQL_ROOT_PASSWORD:",
            "MYSQL_PASSWORD:",
            "pg_isready",
            "mysqladmin ping",
            "RESEMBL_TEST_PG_URL:",
            "RESEMBL_TEST_MYSQL_URL:",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, tests)
                self.assertIn(needle, coverage)
        # Each workflow wires each service exactly once: a second copy
        # would hide a drifted duplicate.
        for name, text in (("tests.yml", tests), ("coverage.yml", coverage)):
            with self.subTest(workflow=name):
                self.assertEqual(text.count("image: postgres:"), 1)
                self.assertEqual(text.count("image: mysql:"), 1)

    def test_go_version_agrees_everywhere(self) -> None:
        """go.mod, Dockerfile, Makefile and docs name the same Go version."""
        mod = (_ROOT / "service" / "go.mod").read_text(encoding="utf-8")
        module_version = re.search(r"^go (\d+\.\d+)$", mod, re.MULTILINE)
        self.assertIsNotNone(module_version, "no go directive in service/go.mod")
        assert module_version is not None
        version = module_version.group(1)
        dockerfile = (_ROOT / "service" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"golang:{version}-alpine", dockerfile)
        for relative in ("Makefile", "CONTRIBUTING.md", "service/README.md"):
            text = (_ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(f"Go {version}", text, f"{relative} does not name Go {version}")


if __name__ == "__main__":
    unittest.main()
