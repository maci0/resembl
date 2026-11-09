from sqlmodel import Session

from resembl.core import snippet_add, snippet_get
from tests.test_cli import BaseCLITest


class TestCLITags(BaseCLITest):
    """Tests for the tag command group."""

    def test_tag_add_and_remove(self):
        """Test adding and removing a tag from a snippet."""
        # Add a snippet
        with Session(self.engine) as session:
            snippet = snippet_add(session, "test_func", "MOV EAX, 1\\nRET")
            checksum = snippet.checksum

        # Add a tag
        result = self.run_command(f"tag add {checksum} 'malware'")
        self.assertEqual(result.returncode, 0)
        self.assertIn("now has tags: malware", result.stdout)
        with Session(self.engine) as session:
            snippet = snippet_get(session, checksum)
            assert snippet is not None
            self.assertEqual(snippet.tag_list, ["malware"])

        # Remove the tag
        result = self.run_command(f"tag remove {checksum} 'malware'")
        self.assertEqual(result.returncode, 0)
        self.assertIn("now has tags:", result.stdout)
        self.assertNotIn("malware", result.stdout)
        with Session(self.engine) as session:
            snippet = snippet_get(session, checksum)
            assert snippet is not None
            self.assertEqual(snippet.tag_list, [])

    def test_tag_invalid_snippet(self):
        """Test adding a tag to a non-existent snippet."""
        result = self.run_command("tag add invalid_checksum 'malware'")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No snippet found matching", result.stderr)
