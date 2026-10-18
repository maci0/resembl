"""clispec 0.3 contract for resembl and resembl-find."""

import json
import os
import subprocess
import sys
import unittest

from tests.test_cli import BaseCLITest


def _run(
    module: str, args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        stdin=subprocess.DEVNULL,
        check=False,
    )


class TestSchema(unittest.TestCase):
    """schema is a JSON document and needs no config or database."""

    def test_resembl_schema_needs_no_config(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        result = _run("resembl.cli", ["schema"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["clispec"], "0.3")
        self.assertEqual(document["name"], "resembl")
        self.assertEqual(document["output"], {"tty": "text", "piped": "text"})
        self.assertTrue(document["commands"])
        self.assertTrue(document["errors"])
        names = [arg["name"] for arg in document["global_args"]]
        self.assertIn("--output", names)
        kinds = {error["kind"] for error in document["errors"]}
        self.assertIn("usage", kinds)
        self.assertIn("failure", kinds)
        self.assertIn("confirmation_required", kinds)
        for command in document["commands"]:
            self.assertIn(command["effects"], {"read_only", "idempotent", "non_idempotent"})

    def test_root_help_mentions_schema(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        result = _run("resembl.cli", ["--help"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema", result.stdout)

    def test_root_help_does_not_require_external_click(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.modules['click'] = None; from resembl.cli import main; main()",
                "--help",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("schema", result.stdout)

    def test_schema_filters_a_command_path(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        result = _run("resembl.cli", ["schema", "collection"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [command["name"] for command in json.loads(result.stdout)["commands"]]
        self.assertIn("collection list", names)
        self.assertNotIn("find", names)

    def test_find_client_schema_needs_no_server(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        result = _run("resembl.find_client", ["schema"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["clispec"], "0.3")
        self.assertEqual(document["name"], "resembl-find")
        self.assertEqual(document["output"], {"tty": "text", "piped": "text"})
        names = [arg["name"] for arg in document["global_args"]]
        self.assertIn("--output", names)
        self.assertIn("--json", names)
        self.assertTrue(document["commands"])
        self.assertTrue(document["errors"])


class TestOutputMode(BaseCLITest):
    """-o json is the items document; -o text is not JSON; failures use the envelope."""

    def test_list_json_is_an_items_document(self) -> None:
        result = self.run_command("-o json list")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["total"], 1)
        self.assertEqual(document["items"][0]["names"], ["test_snippet"])

    def test_text_list_is_not_json(self) -> None:
        result = self.run_command("-o text list")
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout)

    def test_json_failure_is_the_stderr_envelope(self) -> None:
        result = self.run_command("-o json show deadbeef")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        envelope = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(envelope["error"]["kind"], "failure")
        self.assertIn("No snippet found", envelope["error"]["message"])
        self.assertIn("hint", envelope["error"])

    def test_confirmation_off_a_tty_names_force(self) -> None:
        result = self.run_command("-o json reindex")
        self.assertEqual(result.returncode, 1, result.stderr)
        envelope = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(envelope["error"]["kind"], "confirmation_required")
        self.assertIn("--force", envelope["error"]["hint"])

    def test_find_client_json_failure_is_the_stderr_envelope(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        result = _run("resembl.find_client", ["-o", "json"], env=env)
        self.assertEqual(result.returncode, 2, result.stderr)
        envelope = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(envelope["error"]["kind"], "usage")
        self.assertIn("hint", envelope["error"])

    def test_find_client_text_is_not_json_on_failure(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        result = _run("resembl.find_client", ["-o", "text"], env=env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(result.stderr.startswith("error:"))
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stderr.strip().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
