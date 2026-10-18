"""Exercise recipe shell flags through the shipped Makefile on real shells."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class MakefileShellTests(unittest.TestCase):
    """Reject unsupported flags while retaining available pipeline fail-fast."""

    def shell_check(self, name: str, pipefail: bool | None = None) -> None:
        """Run help and a failing pipeline using the named shell, if installed."""
        shell = shutil.which(name)
        make = shutil.which("make")
        if shell is None or make is None:
            self.skipTest(f"{name} and make are required")
        if pipefail is None:
            capability = subprocess.run(
                [shell, "-o", "pipefail", "-c", "false | true"],
                capture_output=True,
                text=True,
                check=False,
            )
            # Unsupported options exit 2; a supported failing pipeline exits 1.
            self.assertIn(capability.returncode, (1, 2), capability.stderr)
            pipefail = capability.returncode == 1
        root = Path(__file__).resolve().parent.parent
        help_result = subprocess.run(
            [make, "help", f"SHELL={shell}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("dist-verify", help_result.stdout)
        self.assertIn("sbom", help_result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "Makefile"
            probe.write_text(
                "include Makefile\n"
                "shell-probe:\n"
                "\t@printf 'flags=%s\\n' '$(.SHELLFLAGS)'\n"
                "\t@false | true\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [make, "-f", str(probe), "shell-probe", f"SHELL={shell}"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
        flags = "-eu -o pipefail -c" if pipefail else "-eu -c"
        self.assertIn(f"flags={flags}\n", result.stdout)
        self.assertEqual(result.returncode != 0, pipefail, result.stderr)

    def test_dash_uses_supported_flags(self) -> None:
        """Dash versions with and without pipefail run with their supported flags."""
        self.shell_check("dash")

    def test_bash_retains_pipeline_failure(self) -> None:
        """Bash still refuses a pipeline whose first command fails."""
        self.shell_check("bash", True)
