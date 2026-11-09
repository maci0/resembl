"""Exercise recipe shell flags through the shipped Makefile on real shells."""

import os
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
        self.assertIn("bench", help_result.stdout)
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

    def test_bench_enables_benchmarks(self) -> None:
        """bench runs test_bench.py with --benchmark-enable, not the disabled default."""
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is required")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [make, "-n", "bench"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tests/test_bench.py", result.stdout)
        self.assertIn("--benchmark-enable", result.stdout)

    def test_service_check_names_missing_go(self) -> None:
        """Without Go on PATH, service-check names Go 1.26+ instead of a bare shell error."""
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is required")
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env["PATH"] = directory
            result = subprocess.run(
                [make, "service-check"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("needs Go 1.26+", result.stdout + result.stderr)
        self.assertIn("https://go.dev/dl/", result.stdout + result.stderr)

    def test_install_names_missing_uv(self) -> None:
        """Without uv on PATH, install names uv and its install docs."""
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is required")
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env["PATH"] = directory
            result = subprocess.run(
                [make, "install"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("needs uv 0.12.13", result.stdout + result.stderr)
        self.assertIn("https://docs.astral.sh/uv/", result.stdout + result.stderr)

    def test_dist_uses_uv_python_not_bare_python3(self) -> None:
        """The wheel rewrite in `dist` runs on the locked interpreter."""
        root = Path(__file__).resolve().parent.parent
        text = (root / "Makefile").read_text(encoding="utf-8")
        self.assertNotIn("python3 -c", text, "Makefile dist uses bare python3")
        self.assertIn("uv run --locked python", text, "Makefile dist not using uv python")

    def test_backup_names_missing_backup_dir(self) -> None:
        """Without BACKUP_DIR, backup fails loud instead of backing up nowhere."""
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is required")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [make, "backup"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("make backup needs BACKUP_DIR", result.stdout + result.stderr)
        self.assertIn("docs/restore.md", result.stdout + result.stderr)

    def test_backup_drill_falls_back_to_older_verifying_backup(self) -> None:
        """A corrupt newest backup does not block the drill for older good ones."""
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is required")
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            # Two copies: newest (corrupt) and older (good, verified via real backup).
            # Build a good backup using the shipped CLI against a scratch DB.
            db_path = Path(directory) / "assembly.db"
            env = dict(os.environ)
            env["RESEMBL_DATABASE_URL"] = f"sqlite:///{db_path}"
            env["RESEMBL_CONFIG_DIR"] = str(Path(directory) / "cfg")
            add = [
                "uv",
                "run",
                "--locked",
                "python",
                "-m",
                "resembl",
                "add",
                "foo",
                "mov eax, 1; ret",
            ]
            subprocess.run(add, cwd=root, env=env, capture_output=True, text=True, check=True)
            bak = [
                "uv",
                "run",
                "--locked",
                "python",
                "-m",
                "resembl",
                "backup",
                str(backup_dir),
                "--force",
            ]
            subprocess.run(bak, cwd=root, env=env, capture_output=True, text=True, check=True)
            good = min(backup_dir.glob("assembly-*.db"))
            import time as _time

            _time.sleep(0.02)
            corrupt = backup_dir / "assembly-29991231T235959-999999.db"
            corrupt.write_bytes(b"corrupt-not-a-db")
            # Manifest for corrupt would be absent; verify fails for it.
            # Drill must skip corrupt and prove the good one.
            result = subprocess.run(
                [make, "backup-drill", f"BACKUP_DIR={backup_dir}"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("failed verify-backup", result.stdout + result.stderr)
            self.assertIn(good.name, (backup_dir / ".restore-drill.log").read_text())

    def test_backup_drill_names_missing_backup_dir(self) -> None:
        """Without BACKUP_DIR, backup-drill fails loud for the same reason."""
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is required")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [make, "backup-drill"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("make backup-drill needs BACKUP_DIR", result.stdout + result.stderr)

    def test_test_one_names_missing_test(self) -> None:
        """Without TEST, test-one names the missing variable instead of running the suite."""
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is required")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [make, "test-one"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("make test-one needs TEST=", result.stdout + result.stderr)

    def test_test_one_dry_run_shows_single_target(self) -> None:
        """test-one with TEST invokes pytest on that target only."""
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is required")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [make, "-n", "test-one", "TEST=tests/test_cli.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tests/test_cli.py", result.stdout)
        self.assertIn("pytest", result.stdout)
