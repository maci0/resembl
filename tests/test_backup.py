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
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from sqlmodel import Session, SQLModel, create_engine

from resembl.core import db_backup, db_verify_backup, snippet_add, snippet_name_add
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

    def test_backup_file_permissions_are_private(self):
        """The backup file holding the full corpus is mode 0600 like its manifest."""
        if sys.platform == "win32":
            self.skipTest("POSIX file modes do not apply on Windows")
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        result = db_backup(self.session, backup_dir)
        mode = stat.S_IMODE(os.stat(result["backup"]).st_mode)
        self.assertEqual(mode, 0o600)

    def test_backup_includes_config_sidecar_and_verifies_it(self):
        """db_backup copies config.toml beside the backup; verify checks it.

        ``config.toml`` is the only tuned state outside the corpus; a
        backup that dropped it would restore an untuned database.  The
        sidecar must be byte-identical to the live config and its SHA-256
        recorded in the manifest.
        """
        import hashlib

        snippet_add(self.session, "foo", "mov eax, 1\nret")
        config_path = os.path.join(self._tmpdir.name, "config.toml")
        config_bytes = b"top_n = 7\nlsh_threshold = 0.8\n"
        with open(config_path, "wb") as f:
            f.write(config_bytes)
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        with mock.patch("resembl.core.config_path_get", return_value=config_path):
            result = db_backup(self.session, backup_dir)
        stem = os.path.splitext(os.path.basename(result["backup"]))[0]
        sidecar = os.path.join(backup_dir, f"{stem}.config.toml")
        self.assertTrue(os.path.isfile(sidecar))
        with open(sidecar, "rb") as f:
            self.assertEqual(f.read(), config_bytes)
        with open(os.path.join(backup_dir, f"{stem}.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["config_sha256"], hashlib.sha256(config_bytes).hexdigest())
        self.assertEqual(manifest["config"], f"{stem}.config.toml")
        check = db_verify_backup(self.session, result["backup"])
        self.assertTrue(check["ok"], check["issues"])

    def test_backup_without_config_records_absent_config(self):
        """No live config.toml: manifest records null, verify still passes."""
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        missing = os.path.join(self._tmpdir.name, "nope.toml")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        with mock.patch("resembl.core.config_path_get", return_value=missing):
            result = db_backup(self.session, backup_dir)
        stem = os.path.splitext(os.path.basename(result["backup"]))[0]
        self.assertFalse(os.path.exists(os.path.join(backup_dir, f"{stem}.config.toml")))
        with open(os.path.join(backup_dir, f"{stem}.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertIsNone(manifest["config_sha256"])
        check = db_verify_backup(self.session, result["backup"])
        self.assertTrue(check["ok"], check["issues"])

    def test_corrupted_config_sidecar_fails_verification(self):
        """A config sidecar that disagrees with its manifest must fail.

        A truncated or tampered sidecar would restore wrong tuned state;
        verify-backup must report it as unrestorable rather than pass on
        the database copy alone.
        """
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        config_path = os.path.join(self._tmpdir.name, "config.toml")
        with open(config_path, "wb") as f:
            f.write(b"top_n = 7\n")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        with mock.patch("resembl.core.config_path_get", return_value=config_path):
            result = db_backup(self.session, backup_dir)
        stem = os.path.splitext(os.path.basename(result["backup"]))[0]
        sidecar = os.path.join(backup_dir, f"{stem}.config.toml")
        with open(sidecar, "wb") as f:
            f.write(b"top_n = 999\n")
        check = db_verify_backup(self.session, result["backup"])
        self.assertFalse(check["ok"])
        self.assertTrue(any("config mismatch" in i for i in check["issues"]), check["issues"])

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

    def test_prune_never_deletes_the_backup_just_written(self):
        """A directory already over ``keep`` cannot prune the newest backup.

        The directory already holds more than ``keep`` ``assembly-*.db``
        files (e.g. a user-supplied copy with a later timestamp, or a
        clock skew), so the retention window counts it as the newest
        and the fresh backup as the oldest.  Without the preserve
        guard the prune would delete the backup ``db_backup`` just
        wrote, reporting success for a directory from which it was
        already gone.
        """
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        os.makedirs(backup_dir)
        # An existing backup with a *later* timestamp than the new one.
        far_future = os.path.join(backup_dir, "assembly-29991231T235959-999999.db")
        with open(far_future, "wb") as f:
            f.write(b"not a real backup")
        result = db_backup(self.session, backup_dir, keep=1)
        self.assertTrue(os.path.isfile(result["backup"]), result)
        self.assertTrue(db_verify_backup(self.session, result["backup"])["ok"])

    def test_backup_self_verifies_before_reporting_success(self):
        """The published backup verifies against its manifest at write time.

        ``db_backup`` hands back a backup a restore drill would trust by
        name order, so the copy must pass ``db_verify_backup`` before
        success is reported.  A backup whose manifest write failed was
        already untrustworthy; this guards the published file itself.
        """
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        result = db_backup(self.session, backup_dir)
        self.assertTrue(result["manifest_written"])
        self.assertTrue(result["verified"])
        check = db_verify_backup(self.session, result["backup"])
        self.assertTrue(check["ok"], check["issues"])

    def test_backup_name_uses_utc_regardless_of_local_zone(self):
        """``db_backup`` filenames are UTC so prune sorting stays chronological across DST.

        Regression: stamps used local time, so the repeated wall-clock hour
        at a fall-back (2024-11-03 01:30 EDT and 01:30 EST) produced
        identical timestamp prefixes and the second backup could sort
        oldest, making prune delete the newest backup.
        """
        import calendar
        from unittest import mock

        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        original_localtime = time.localtime

        def located_localtime(epoch=None):
            if epoch is None:
                epoch = time.time()
            return time.gmtime(epoch - 14400)

        # Pin the logical clock to a fixed instant so the stamp is exact and
        # the test does not depend on wall-clock timing (the old ±60s window
        # was flaky under CI load and second-boundary straddles).
        pinned_epoch = calendar.timegm((2024, 6, 1, 12, 0, 0, 0, 0, 0))
        time.localtime = located_localtime
        try:
            with mock.patch.dict(
                os.environ,
                {"RESEMBL_NOW": "2024-06-01T12:00:00+00:00"},
            ):
                result = db_backup(self.session, backup_dir)
        finally:
            time.localtime = original_localtime
        backup_name = os.path.basename(result["backup"])
        stamp_str = backup_name.removesuffix(".db").removeprefix("assembly-").rsplit("-", 1)[0]
        parsed = time.strptime(stamp_str, "%Y%m%dT%H%M%S")
        epoch_from_stamp = calendar.timegm(parsed)
        self.assertEqual(epoch_from_stamp, pinned_epoch)

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
        # db_backup reads the logical clock (``timestamp_now``), not a
        # second wall-clock source: pin it to the same instant the old
        # ``time_ns`` mock used to return, so the second and microsecond
        # still come from one read.
        with (
            mock.patch.object(
                time, "time_ns", return_value=(epoch_s + 1) * 1_000_000_000 + 123_456_000
            ),
            mock.patch.dict(
                os.environ,
                {"RESEMBL_NOW": "2024-06-01T12:00:01.123456+00:00"},
            ),
        ):
            result = db_backup(self.session, backup_dir)
        backup_name = os.path.basename(result["backup"])
        self.assertTrue(
            backup_name.endswith("T120001-123456.db"),
            f"stamp must be one consistent instant, got {backup_name}",
        )

    def test_backup_replays_byte_for_byte_under_pinned_clock(self):
        """``RESEMBL_NOW`` pins backup filenames and manifests, not just rows.

        Regression: ``db_backup`` read ``time_ns()`` directly while
        ``created_at`` honored ``RESEMBL_NOW``, so a pinned replay's
        database rows matched but its backup names and manifests never
        did — the one wall-clock read the replay could not reproduce.
        Two fresh databases backed up under the same pinned instant must
        produce the same backup name and the same manifest.
        """
        from unittest import mock

        pinned = {"RESEMBL_NOW": "2024-06-01T12:00:00+00:00"}
        results: list[tuple[str, dict]] = []
        for _ in range(2):
            # Fresh database per run (the filename bump for distinct
            # content is covered by the sibling test), so the pinned
            # instant alone decides the name.
            with mock.patch.dict(os.environ, pinned):
                snippet_add(self.session, "foo", "mov eax, 1\nret")
                backup_dir = os.path.join(self._tmpdir.name, "backups")
                result = db_backup(self.session, backup_dir)
                manifest_name = os.path.splitext(os.path.basename(result["backup"]))[0] + ".json"
                with open(os.path.join(backup_dir, manifest_name), encoding="utf-8") as f:
                    manifest = json.load(f)
                results.append((os.path.basename(result["backup"]), manifest))
                self.session.rollback()
                # Remove the row so the next run's content matches the
                # first; the backup directory already holds the first
                # backup, so clear it too.
                from sqlmodel import delete

                from resembl.models import Snippet

                self.session.exec(delete(Snippet))
                self.session.commit()
                os.remove(result["backup"])
                os.remove(os.path.join(backup_dir, manifest_name))
        first = results[0]
        second = results[1]
        self.assertEqual(first[0], "assembly-20240601T120000-000000.db")
        self.assertEqual(second[0], first[0])
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[1]["created_at"], "2024-06-01T12:00:00+00:00")

    def test_pinned_clock_same_instant_backups_get_distinct_names(self):
        """A second distinct backup at a pinned instant does not overwrite.

        The microsecond suffix is a prune sort key, not a uniqueness
        guarantee at a pinned instant: bumping it keeps two distinct
        backups from colliding while staying deterministic."""
        from unittest import mock

        with mock.patch.dict(os.environ, {"RESEMBL_NOW": "2024-06-01T12:00:00+00:00"}):
            backup_dir = os.path.join(self._tmpdir.name, "backups")
            snippet_add(self.session, "foo", "mov eax, 1\nret")
            first = db_backup(self.session, backup_dir)
            snippet_add(self.session, "bar", "xor ebx, ebx\nret")
            second = db_backup(self.session, backup_dir)
        self.assertEqual(os.path.basename(first["backup"]), "assembly-20240601T120000-000000.db")
        self.assertEqual(os.path.basename(second["backup"]), "assembly-20240601T120000-000001.db")

    def test_pinned_clock_microsecond_overflow_carries_into_next_second(self):
        """Bumping past 999999 microseconds carries into the next second.

        The old loop incremented the microsecond counter past 999999 and
        formatted it as a 7-digit field (``-1000000``), which sorts after
        every 6-digit stamp of the original second: a backup taken at
        12:00:00.999999+1 bump could sort as the newest and prune could
        delete a genuinely newer backup.  Carrying into the next second
        keeps the fixed width, so lexicographic order stays chronological.
        """
        from unittest import mock

        with mock.patch.dict(os.environ, {"RESEMBL_NOW": "2024-06-01T12:00:00.999999+00:00"}):
            backup_dir = os.path.join(self._tmpdir.name, "backups")
            snippet_add(self.session, "foo", "mov eax, 1\nret")
            first = db_backup(self.session, backup_dir)
            snippet_add(self.session, "bar", "xor ebx, ebx\nret")
            second = db_backup(self.session, backup_dir)
        self.assertEqual(os.path.basename(first["backup"]), "assembly-20240601T120000-999999.db")
        self.assertEqual(os.path.basename(second["backup"]), "assembly-20240601T120001-000000.db")

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

    def test_backup_after_a_metadata_change_writes_a_new_file(self):
        """An alias/tag change is a content change: the old backup is stale."""
        snippet = snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        first = db_backup(self.session, backup_dir, keep=7)
        snippet_name_add(self.session, snippet.checksum, "alias")
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

    def test_corrupted_reused_backup_is_replaced(self):
        """A reused backup corrupted after it was written is not handed back.

        Regression: the reuse path returned the existing file by manifest
        hash alone, so a backup corrupted in place (bit rot, a truncated
        copy) was reported as a fresh verified success and the system
        kept no restorable copy. The reuse path now re-verifies, so the
        corrupt file is replaced by a fresh verified backup.
        """
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        first = db_backup(self.session, backup_dir, keep=7)
        reused = db_backup(self.session, backup_dir, keep=7)
        self.assertEqual(reused["backup"], first["backup"])
        self.assertTrue(reused["verified"])
        with open(reused["backup"], "r+b") as f:
            f.truncate(10)
        self.assertFalse(db_verify_backup(self.session, reused["backup"])["ok"])
        replaced = db_backup(self.session, backup_dir, keep=7)
        self.assertNotEqual(replaced["backup"], first["backup"])
        self.assertTrue(replaced["verified"])
        self.assertTrue(db_verify_backup(self.session, replaced["backup"])["ok"])


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

    def test_wal_sidecar_beside_backup_fails(self):
        """A -wal beside the backup must fail: restore copies only the .db.

        The read-only verify open would replay the wal and measure
        main+wal, while the documented restore ships the single file —
        so a backup that verifies with a wal present can restore empty.
        """
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        result = db_backup(self.session, backup_dir)
        with open(result["backup"] + "-wal", "wb") as f:
            f.write(b"\x00" * 32)
        check = db_verify_backup(self.session, result["backup"])
        self.assertFalse(check["ok"], check)
        self.assertTrue(any("wal sidecar" in i for i in check["issues"]), check["issues"])

    def test_rebackup_of_corrupted_retained_backup_writes_fresh(self):
        """A corrupt retained backup is not reused as a successful backup.

        The reuse check keys on the manifest's content hash, which a
        truncated on-disk copy still matches.  Without re-verification,
        ``db_backup`` would return the corrupt file as its newest
        restore point and report success.
        """
        snippet_add(self.session, "foo", "mov eax, 1\nret")
        backup_dir = os.path.join(self._tmpdir.name, "backups")
        first = db_backup(self.session, backup_dir)
        with open(first["backup"], "r+b") as f:
            f.truncate(10)
        second = db_backup(self.session, backup_dir)
        self.assertNotEqual(second["backup"], first["backup"])
        self.assertTrue(second["verified"])
        self.assertTrue(db_verify_backup(self.session, second["backup"])["ok"])


if __name__ == "__main__":
    unittest.main()
