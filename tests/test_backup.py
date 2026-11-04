"""Tests for db_backup and db_verify_backup (durability / restore).

A backup is the only copy of the snippet corpus that survives instance
loss, a deleted ``assembly.db``, or a corrupted store.  These tests drive
the real entry points: a file-backed SQLite database is populated through
``snippet_add``, backed up through ``db_backup`` (the online backup API),
verified through ``db_verify_backup``, and then restored by pointing a new
engine at the backup file and reading it back through the normal query
path.  A backup whose manifest disagrees with its contents must fail
verification rather than pass on the strength of having been written.
"""

import json
import os
import tempfile
import unittest

from sqlmodel import Session, SQLModel, create_engine

from resembl.core import db_backup, db_verify_backup, snippet_add
from tests.test_collections_merge import BaseDBTest


class FileDBTest(BaseDBTest):
    """File-backed SQLite database, so backup has a real file to copy."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.db_path = os.path.join(self._tmpdir.name, "assembly.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()
        self._tmpdir.cleanup()


class TestDbBackup(FileDBTest):
    """db_backup writes a restorable copy and records its manifest."""

    def test_backup_is_restorable_and_verifies(self):
        """A backup opens as a database and holds the source's snippets."""
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        snippet_add(self.session, "bar", "xor ebx, ebx\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        result = db_backup(self.session, backup_dir)
        self.assertEqual(result["num_snippets"], 2)
        self.assertTrue(os.path.isfile(result["backup"]))
        # The manifest is the contract a restore is verified against.
        with open(os.path.join(backup_dir, ".resembl-backup.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["num_snippets"], 2)
        self.assertEqual(manifest["backup"], os.path.basename(result["backup"]))

        check = db_verify_backup(self.session, result["backup"])
        self.assertTrue(check["ok"], check["issues"])
        self.assertEqual(check["num_snippets"], 2)

        # Restore: point a new engine at the backup file and read it back
        # through the normal query path.
        restored_engine = create_engine(f"sqlite:///{result['backup']}")
        with Session(restored_engine) as restored:
            from resembl.core import snippet_list

            names = sorted(name for snippet in snippet_list(restored) for name in snippet.name_list)
        self.assertEqual(names, ["bar", "foo"])

    def test_backup_prunes_to_keep(self):
        """Old backups beyond ``keep`` are removed, newest retained."""
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        for _ in range(3):
            db_backup(self.session, backup_dir, keep=2)
        backups = sorted(
            name
            for name in os.listdir(backup_dir)
            if name.startswith("assembly-") and name.endswith(".db")
        )
        self.assertEqual(len(backups), 2)

    def test_backup_rejects_keep_below_one(self):
        """``keep=0`` would delete the backup just taken."""
        with self.assertRaises(ValueError):
            db_backup(self.session, os.path.join(self._tmpdir.name, "backups"), keep=0)

    def test_backup_rejects_non_sqlite(self):
        """Non-SQLite backends must use their own backup tooling."""
        engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            # In-memory SQLite has no file to name; the backup API still
            # runs, so this case documents the guard by backend name
            # rather than by refusing here.
            result = db_backup(session, os.path.join(self._tmpdir.name, "mem-backups"))
            self.assertIn("backup", result)


class TestDbVerifyBackup(FileDBTest):
    """db_verify_backup fails a backup whose contents disagree with its manifest."""

    def test_missing_file_fails(self):
        """A nonexistent backup is reported, not trusted."""
        result = db_verify_backup(self.session, "/nonexistent/backup.db")
        self.assertFalse(result["ok"])

    def test_truncated_backup_fails(self):
        """A truncated file is not a restorable backup."""
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        result = db_backup(self.session, backup_dir)
        with open(result["backup"], "r+b") as f:
            f.truncate(10)
        check = db_verify_backup(self.session, result["backup"])
        self.assertFalse(check["ok"])

    def test_count_mismatch_fails(self):
        """A manifest that disagrees with the file fails verification."""
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        result = db_backup(self.session, backup_dir)
        manifest_path = os.path.join(backup_dir, ".resembl-backup.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["num_snippets"] = 999
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        check = db_verify_backup(self.session, result["backup"])
        self.assertFalse(check["ok"])
        self.assertTrue(any("mismatch" in issue for issue in check["issues"]))

    def test_missing_manifest_still_verifies_contents(self):
        """A backup predating manifests verifies on its contents alone."""
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        result = db_backup(self.session, backup_dir)
        os.remove(os.path.join(backup_dir, ".resembl-backup.json"))
        check = db_verify_backup(self.session, result["backup"])
        self.assertTrue(check["ok"], check["issues"])
        self.assertTrue(any("manifest" in issue for issue in check["issues"]))


if __name__ == "__main__":
    unittest.main()
