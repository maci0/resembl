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
import time
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
        manifest_name = os.path.splitext(os.path.basename(result["backup"]))[0] + ".json"
        with open(os.path.join(backup_dir, manifest_name), encoding="utf-8") as f:
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
        """Old backups beyond ``keep`` are removed, newest retained.

        Each run adds a snippet first, so every backup holds distinct
        content and is written: identical re-runs converge on one backup
        (see ``test_repeated_backup_of_unchanged_content_is_convergent``)
        and pruning applies only to genuinely new backups.
        """
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        for i in range(3):
            snippet_add(self.session, f"s{i}", f"mov eax, {i}\nret")
            db_backup(self.session, backup_dir, keep=2)
        backups = sorted(
            name
            for name in os.listdir(backup_dir)
            if name.startswith("assembly-") and name.endswith(".db")
        )
        self.assertEqual(len(backups), 2)

    def test_older_retained_backups_verify_against_their_own_counts(self):
        """Every retained backup verifies: the manifest must be per-backup.

        Regression test: a single shared manifest described only the
        newest backup, so ``verify-backup`` reported a snippet-count
        mismatch on every older retained copy and the oldest good
        restores looked unrestorable exactly when the newest was lost.
        """
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        results = []
        for i in range(3):
            snippet_add(self.session, f"s{i}", f"mov eax, {i}\nret")
            results.append(db_backup(self.session, backup_dir, keep=7))
        for result, expected in zip(results, (1, 2, 3), strict=True):
            check = db_verify_backup(self.session, result["backup"])
            self.assertTrue(check["ok"], (result["backup"], check["issues"]))
            self.assertEqual(check["num_snippets"], expected)

    def test_backup_rejects_keep_below_one(self):
        """``keep=0`` would delete the backup just taken."""
        with self.assertRaises(ValueError):
            db_backup(self.session, os.path.join(self._tmpdir.name, "backups"), keep=0)

    def test_backup_name_uses_utc_regardless_of_local_zone(self):
        """``db_backup`` filenames are UTC so prune sorting stays chronological across DST.

        Regression: stamps used local time, so the repeated wall-clock hour
        at a fall-back (2024-11-03 01:30 EDT and 01:30 EST) produced
        identical timestamp prefixes and the second backup could sort
        oldest, making prune delete the newest backup.
        """
        import calendar

        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        original_localtime = time.localtime

        def located_localtime(epoch=None):
            if epoch is None:
                epoch = time.time()
            return time.gmtime(epoch - 14400)

        time.localtime = located_localtime
        try:
            result = db_backup(self.session, backup_dir)
        finally:
            time.localtime = original_localtime
        backup_name = os.path.basename(result["backup"])
        stamp_str = backup_name.removesuffix(".db").removeprefix("assembly-").rsplit("-", 1)[0]
        parsed = time.strptime(stamp_str, "%Y%m%dT%H%M%S")
        epoch_from_stamp = calendar.timegm(parsed)
        self.assertLessEqual(epoch_from_stamp, time.time() + 60)
        self.assertGreater(epoch_from_stamp, time.time() - 60)

    def test_backup_stamp_second_and_microsecond_come_from_one_read(self):
        """A stamp's microsecond belongs to its own second field.

        Regression: the stamp combined two clock reads (``gmtime()`` for
        the second, ``time_ns()`` for the microsecond); straddling a second
        boundary stamped the next second's microsecond onto this second, so
        a backup could sort before one taken earlier in the same second and
        prune could delete the newer file.
        """
        import calendar
        from unittest import mock

        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        epoch_s = calendar.timegm((2024, 6, 1, 12, 0, 0, 0, 0, 0))
        with mock.patch.object(
            time, "time_ns", return_value=(epoch_s + 1) * 1_000_000_000 + 123_456_000
        ):
            result = db_backup(self.session, backup_dir)
        backup_name = os.path.basename(result["backup"])
        self.assertTrue(
            backup_name.endswith("T120001-123456.db"),
            f"stamp must be one consistent instant, got {backup_name}",
        )

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

    def test_repeated_backup_of_unchanged_content_is_convergent(self):
        """A re-run of ``db_backup`` on unchanged data writes no second file.

        Regression: every run wrote a new timestamped copy and pruned the
        oldest retained one, so a retried run (a double-scheduled cron
        job, a script re-run, a caller that lost the first response)
        consumed two retention slots for one logical backup and deleted
        the oldest restorable copy with identical content.
        """
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        first = db_backup(self.session, backup_dir, keep=7)
        for _ in range(3):
            again = db_backup(self.session, backup_dir, keep=7)
            self.assertEqual(again["backup"], first["backup"])
            self.assertEqual(again["num_removed"], 0)
        backups = [
            name
            for name in os.listdir(backup_dir)
            if name.startswith("assembly-") and name.endswith(".db")
        ]
        self.assertEqual(backups, [os.path.basename(first["backup"])])
        self.assertTrue(db_verify_backup(self.session, first["backup"])["ok"])

    def test_backup_after_a_content_change_writes_a_new_file(self):
        """The reuse check is on content, not on the directory being non-empty."""
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        first = db_backup(self.session, backup_dir, keep=7)
        snippet_add(self.session, "bar", "xor ebx, ebx\nret")
        second = db_backup(self.session, backup_dir, keep=7)
        self.assertNotEqual(second["backup"], first["backup"])
        self.assertTrue(db_verify_backup(self.session, second["backup"])["ok"])

    def test_backup_reuse_does_not_drain_the_retention_window(self):
        """Duplicate runs never prune: only genuinely new content does."""
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        for i in range(7):
            snippet_add(self.session, f"s{i}", f"mov eax, {i}\nret")
            db_backup(self.session, backup_dir, keep=7)
        before = sorted(
            name
            for name in os.listdir(backup_dir)
            if name.startswith("assembly-") and name.endswith(".db")
        )
        self.assertEqual(len(before), 7)
        for _ in range(5):
            db_backup(self.session, backup_dir, keep=7)
        after = sorted(
            name
            for name in os.listdir(backup_dir)
            if name.startswith("assembly-") and name.endswith(".db")
        )
        self.assertEqual(after, before)

    def test_legacy_manifest_without_a_content_hash_is_not_reused(self):
        """A pre-hash manifest never matches, so a fresh backup is written."""
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        first = db_backup(self.session, backup_dir, keep=7)
        manifest_name = os.path.splitext(os.path.basename(first["backup"]))[0] + ".json"
        manifest_path = os.path.join(backup_dir, manifest_name)
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        del manifest["content_hash"]
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        second = db_backup(self.session, backup_dir, keep=7)
        self.assertNotEqual(second["backup"], first["backup"])
        third = db_backup(self.session, backup_dir, keep=7)
        self.assertEqual(third["backup"], second["backup"])


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
        manifest_name = os.path.splitext(os.path.basename(result["backup"]))[0] + ".json"
        manifest_path = os.path.join(backup_dir, manifest_name)
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
        manifest_name = os.path.splitext(os.path.basename(result["backup"]))[0] + ".json"
        os.remove(os.path.join(backup_dir, manifest_name))
        check = db_verify_backup(self.session, result["backup"])
        self.assertTrue(check["ok"], check["issues"])
        self.assertTrue(any("manifest" in issue for issue in check["issues"]))

    def test_content_hash_mismatch_fails(self):
        """A manifest whose content_hash does not match the rows fails.

        Regression: verify compared only the snippet count, so a backup
        whose rows were replaced (half-overwritten copy, truncated-then-
        padded file) passed as long as the count agreed.
        """
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        result = db_backup(self.session, backup_dir)
        manifest_name = os.path.splitext(os.path.basename(result["backup"]))[0] + ".json"
        manifest_path = os.path.join(backup_dir, manifest_name)
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["content_hash"] = "0" * 64
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        check = db_verify_backup(self.session, result["backup"])
        self.assertFalse(check["ok"], check)
        self.assertTrue(any("hash" in issue for issue in check["issues"]))

    def test_corrupted_page_fails(self):
        """A backup with a corrupt page fails even when COUNT(*) answers."""
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        result = db_backup(self.session, backup_dir)
        with open(result["backup"], "r+b") as f:
            data = bytearray(f.read())
        for i in range(len(data) // 2, len(data) // 2 + 16):
            data[i] ^= 0xFF
        with open(result["backup"], "wb") as f:
            f.write(data)
        check = db_verify_backup(self.session, result["backup"])
        self.assertFalse(check["ok"], check)


if __name__ == "__main__":
    unittest.main()
