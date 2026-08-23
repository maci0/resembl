import os
import tempfile

from sqlmodel import Session

from resembl.core import snippet_add
from tests.test_cli import BaseCLITest


class TestYaraExport(BaseCLITest):
    def test_export_yara(self):
        with Session(self.engine) as session:
            snippet_add(session, "test_func", "MOV EAX, 1\nRET")
            snippet_add(session, "test_func2", 'PUSH EBP\nMOV EBP, ESP\n\\weird"quote')

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "rules.yara")
            result = self.run_command(f"export-yara --force {out_file}")
            self.assertEqual(result.returncode, 0)

            with open(out_file, encoding="utf-8") as f:
                content = f.read()

            self.assertIn("rule resembl_test_func_", content)
            self.assertIn("rule resembl_test_func2_", content)
            self.assertIn('$asm = "MOV EAX, 1\\nRET"', content)
            self.assertIn('$asm = "PUSH EBP\\nMOV EBP, ESP\\n\\\\weird\\"quote"', content)
            self.assertIn("nocase ascii wide", content)

    def test_failed_export_leaves_no_partial_rule_file(self):
        """A mid-export failure publishes nothing and leaves no temp files.

        The rules stream into a same-directory temp published by one atomic
        rename: a failure halfway (disk full, encoding, bug) must not leave
        a truncated rule file at the requested path looking complete.
        """
        import glob
        from unittest.mock import patch

        import resembl.core as core

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "rules.yara")
            calls = {"n": 0}

            def failing_escape(text):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise RuntimeError("simulated mid-export failure")
                return core._yara_string_escape(text)

            with patch.object(core, "_yara_string_escape", side_effect=failing_escape):
                with Session(self.engine) as session:
                    with self.assertRaises(RuntimeError):
                        core.snippet_export_yara(session, out_file)
            # No truncated artifact at the target path, no temp leftovers.
            self.assertFalse(os.path.exists(out_file))
            self.assertEqual(glob.glob(os.path.join(tmpdir, "*.tmp")), [])
