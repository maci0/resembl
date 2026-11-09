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

    def test_pylint_scope_agrees_everywhere(self) -> None:
        """The pre-commit hook, Makefile, CI and AGENTS.md lint the same trees.

        A scope that differs between the commit gate and CI lets a file
        pass locally and fail remotely (or the reverse): the benchmarks/ move
        left the hook checking fewer trees than `make check` and pylint.yml.
        """
        scope = "resembl/ tests/ fuzzers/ benchmarks/"
        pre_commit = (_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        for tree in ("resembl/", "tests/", "fuzzers/", "benchmarks/"):
            self.assertIn(f'"{tree}"', pre_commit, f".pre-commit-config.yaml {tree}")
        pylint_yml = (_ROOT / ".github" / "workflows" / "pylint.yml").read_text(encoding="utf-8")
        self.assertIn(scope, pylint_yml, "pylint.yml pylint invocation")
        makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(scope, makefile, "Makefile pylint invocation")
        agents = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(scope, agents, "AGENTS.md pylint scope")

    def test_sbom_validation_uses_uv_python(self) -> None:
        """The SBOM JSON check runs on the pinned interpreter, not bare python3.

        The runner's system python3 is unpinned and uncached, so the check
        would parse with whatever the image ships while uv manages 3.15.2.
        """
        text = (_ROOT / ".github" / "workflows" / "sbom.yml").read_text(encoding="utf-8")
        self.assertNotIn("python3 -c", text, "sbom.yml uses bare python3")
        self.assertIn("uv run --locked python", text, "sbom.yml validation step")

    def test_uv_installer_is_https_only_and_retried(self) -> None:
        """The uv installer fetch refuses non-HTTPS and retries transient failures.

        `curl | sh` with no protocol restriction would silently follow a
        downgrade to plain HTTP if astral.sh ever redirected, and a transient
        5xx would fail the whole job instead of retrying the pinned download.
        """
        text = (_ROOT / ".github" / "actions" / "setup-uv" / "action.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("--proto '=https'", text, "uv installer curl protocol")
        self.assertIn("--retry", text, "uv installer curl retry")

    def test_os_classifiers_match_ci_portability_matrix(self) -> None:
        """pyproject.toml OS classifiers name exactly the CI-tested platforms.

        The classifiers are the claim indexers and users read; the CI matrix
        is the evidence.  A classifier added without a runner (or a runner
        dropped without removing the classifier) makes support asserted and
        never tested.  Linux is the `test` job's `ubuntu-24.04`; macOS and
        Windows are the `portability` job's matrix.
        """
        text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for classifier in (
            "Operating System :: POSIX :: Linux",
            "Operating System :: MacOS",
            "Operating System :: Microsoft :: Windows",
        ):
            with self.subTest(classifier=classifier):
                self.assertIn(classifier, text)
        self.assertNotIn('"Operating System :: OS Independent"', text)
        workflows = (_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-24.04", workflows)
        self.assertIn("os: [macos-15, windows-2025]", workflows)

    def test_dockerignore_patterns_agree(self) -> None:
        """Root and service .dockerignore files carry the same patterns.

        service/Dockerfile is built from the root context, so the root
        file is the one Docker reads. The service/ copy is kept in sync
        by eye; a pattern present in one but not the other lets a local
        database, WAL sidecar or benchmark cache drift back into the
        context the other file excludes.
        """

        def patterns(path: Path) -> set[str]:
            return {
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }

        root = patterns(_ROOT / ".dockerignore")
        service = patterns(_ROOT / "service" / ".dockerignore")
        self.assertEqual(root, service)
        for needed in (
            "benchmarks",
            "*.db-wal",
            "**.db-wal",
            "*.db-shm",
            "**.db-shm",
            "*.duckdb",
            "**.duckdb",
            "*.duckdb-wal",
            "**.duckdb-wal",
            "*.sqlite3-wal",
            "**.sqlite3-wal",
            "*.sqlite3-shm",
            "**.sqlite3-shm",
        ):
            with self.subTest(pattern=needed):
                self.assertIn(needed, root)

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
        # The patch must agree too: go-version-file names only the minor,
        # so setup-go would resolve the latest patch at run time and the
        # CI binary would differ from the digest-pinned image binary.
        workflow = (_ROOT / ".github" / "workflows" / "service.yml").read_text(encoding="utf-8")
        self.assertNotIn("go-version-file: service", workflow)
        pinned = re.search(r"go-version:\s*'(\d+\.\d+\.\d+)'", workflow)
        self.assertIsNotNone(pinned, "service.yml does not pin an exact Go patch")
        assert pinned is not None
        self.assertTrue(
            pinned.group(1).startswith(version + "."),
            f"service.yml Go {pinned.group(1)} does not match go.mod {version}",
        )

    def test_every_workflow_declares_top_level_permissions(self) -> None:
        """Every workflow declares a top-level `permissions:` block.

        Without one, a workflow inherits the repository default token
        permissions (which may be broader than `contents: read`) instead
        of the least privilege the job needs. A job-level block cannot
        narrow a workflow that declares none; the top level is the
        ceiling for every job in the file.
        """
        for workflow in _WORKFLOWS:
            text = workflow.read_text(encoding="utf-8")
            with self.subTest(workflow=workflow.name):
                self.assertRegex(
                    text,
                    r"(?m)^permissions:\s*\n",
                    f"{workflow.name}: no top-level permissions: block",
                )

    def test_service_smoke_step_is_pipefail_safe(self) -> None:
        """The service image smoke step must not pipe `-h` into grep.

        `resembl-service -h` exits 1 (Go's flag package), so a
        `docker run ... -h 2>&1 | grep -q` pipeline fails under any
        shell with `pipefail` even when the usage line is present,
        while the default GitHub shell (no pipefail) masks a docker
        failure whose output happens to match. Capturing the output
        first and grepping the capture works under both.
        """
        text = (_ROOT / ".github" / "workflows" / "service.yml").read_text(encoding="utf-8")
        self.assertNotIn(
            "docker run --rm resembl-service:ci -h 2>&1 | grep",
            text,
            "service smoke step pipes the exit-1 -h output into grep",
        )
        self.assertIn("|| true", text)
        self.assertIn("Usage of resembl-service:", text)


if __name__ == "__main__":
    unittest.main()
