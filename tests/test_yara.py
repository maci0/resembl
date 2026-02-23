import os
import tempfile
from sqlmodel import Session
from resembl.core import snippet_add
from resembl.models import Snippet
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
            
            with open(out_file, "r", encoding="utf-8") as f:
                content = f.read()
                
            self.assertIn("rule resembl_test_func_", content)
            self.assertIn("rule resembl_test_func2_", content)
            self.assertIn('$asm = "MOV EAX, 1\\nRET"', content)
            self.assertIn('$asm = "PUSH EBP\\nMOV EBP, ESP\\n\\\\weird\\"quote"', content)
            self.assertIn("nocase ascii wide", content)
