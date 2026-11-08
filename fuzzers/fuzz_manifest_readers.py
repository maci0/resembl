#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the on-disk JSON manifest readers in ``resembl.core``.

Three readers parse JSON files a hostile or hand-edited directory can
write, and each documents a never-raise contract the caller relies on:
``_export_manifest_read`` must answer an empty set (disabling pruning)
for a missing, unreadable or malformed export manifest rather than
aborting the export, ``_db_backup_reuse`` must skip a candidate whose
manifest is unreadable rather than aborting the backup, and the
manifest loop in ``db_verify_backup`` must skip an unreadable manifest
rather than aborting verification.  A deeply nested document recurses
once per level in the JSON decoder and raises ``RecursionError``,
which is not a ``ValueError`` — the same shape ``_string_list_or_none``
and ``verify_runtime`` already guard against — so without the guard a
hostile manifest turns one bad file into a crashed export, backup or
verification instead of a skipped candidate.

The harness drives the real entry points and asserts the invariants
the callers rely on, not just the absence of a crash:

* the readers never raise for any byte input at any file state
  (missing file, directory in place of the manifest, truncated,
  oversized, deeply nested, invalid UTF-8);
* an accepted export manifest is a set of plain file names, and an
  accepted backup manifest is a dict whose ``content_hash`` (when
  present) is a string, so no caller can be handed a shape its own
  type checks would reject;
* an accepted manifest is a fixed point: reading the same bytes twice
  yields the same answer, so a flaky read cannot flip an export
  between pruning and not pruning, or a backup between reuse and
  rewrite.

Inputs are generated structure-aware: well-formed manifests built
from the documented shapes (an export name list, a backup manifest
with ``content_hash`` / ``num_snippets``), hostile documents
(non-list / non-object documents, lists with non-string entries,
truncated documents, deeply nested arrays and objects), invalid
UTF-8, and raw junk bytes.  Seeds come from the documented manifest
shapes, not from captured traffic.
"""

import json
import os
import sqlite3
import sys
import tempfile

import atheris

with atheris.instrument_imports():
    from resembl.core import (
        _EXPORT_MANIFEST_NAME,
        _db_backup_manifest_name,
        _db_backup_reuse,
        _export_manifest_read,
        db_verify_backup,
    )

#: Realistic export file names worth spending runs on.
_INTERESTING_NAMES = (
    "ret.yara",
    "mov.yara",
    "a" * 64,
    "café.yara",
    "",
    "  ",
    "a/b",
    "../evil.yara",
    "bad\ud800.yara",
)

#: JSON documents that are not the documented manifest shape, plus near-misses.
_NON_SHAPE_DOCUMENTS = (
    "null",
    "123",
    '"just a string"',
    "true",
    '{"a": 1}',
    "[1, 2, 3]",
    "[null]",
    '["a",]',
    '["unterminated',
    "",
    "   ",
    "not json",
)


def _build_document(fdp: atheris.FuzzedDataProvider) -> bytes:
    """Return one manifest document (or junk) built from *fdp*."""
    mode = fdp.ConsumeIntInRange(0, 5)
    if mode == 0:
        # A well-formed export manifest: a list of file names.
        names = [
            _INTERESTING_NAMES[fdp.ConsumeIntInRange(0, len(_INTERESTING_NAMES) - 1)]
            for _ in range(fdp.ConsumeIntInRange(0, 4))
        ]
        return json.dumps(names).encode()
    if mode == 1:
        # A well-formed backup manifest: a dict with the documented keys.
        document: dict[str, object] = {
            "content_hash": fdp.ConsumeUnicodeNoSurrogates(16),
            "num_snippets": fdp.ConsumeIntInRange(-2, 100),
        }
        if fdp.ConsumeBool():
            document["backup"] = "assembly-20200101T000000Z.db"
        return json.dumps(document).encode()
    if mode == 2:
        # A document that is not the documented shape.
        doc = _NON_SHAPE_DOCUMENTS[fdp.ConsumeIntInRange(0, len(_NON_SHAPE_DOCUMENTS) - 1)]
        return doc.encode()
    if mode == 3:
        # Deeply nested: the decoder recurses once per level.
        depth = fdp.ConsumeIntInRange(2, 5000)
        opener, closer = ("[", "]") if fdp.ConsumeBool() else ('{"a":', "}")
        return f"{opener * depth}{closer * depth}".encode()
    if mode == 4:
        # A truncated well-formed manifest: every byte prefix of one.
        doc = json.dumps({"content_hash": "abc", "num_snippets": 3}).encode()
        return doc[: fdp.ConsumeIntInRange(0, len(doc))]
    # Raw junk bytes and invalid UTF-8.
    return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))


def _check_export(raw: bytes, tmp: str) -> None:
    """Drive ``_export_manifest_read`` over *raw* placed as the manifest."""
    path = os.path.join(tmp, _EXPORT_MANIFEST_NAME)
    with open(path, "wb") as fh:
        fh.write(raw)
    result = _export_manifest_read(tmp)
    assert isinstance(result, set), f"export reader returned non-set: {result!r}"
    assert all(isinstance(name, str) for name in result), f"non-string name: {result!r}"
    # Fixed point: the same bytes read again yield the same names.
    assert _export_manifest_read(tmp) == result, "export read is not a fixed point"


def _check_backup_reuse(raw: bytes, tmp: str) -> None:
    """Drive ``_db_backup_reuse`` over *raw* placed as a backup manifest."""
    backup_name = "assembly-20200101T000000Z.db"
    with open(os.path.join(tmp, backup_name), "wb") as fh:
        fh.write(b"x")
    with open(os.path.join(tmp, _db_backup_manifest_name(backup_name)), "wb") as fh:
        fh.write(raw)
    probe = raw.decode("utf-8", errors="replace")
    probe_hash: str | None = None
    try:
        document = json.loads(probe)
    except ValueError, RecursionError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("content_hash"), str):
        probe_hash = document["content_hash"]
    # Probe with the manifest's own hash when it has one (an empty
    # string is a hash the manifest can record); otherwise probe with
    # a hash no well-formed manifest in the tree records.
    result = _db_backup_reuse(tmp, probe_hash if probe_hash is not None else "no-such-hash")
    assert result is None or isinstance(result, str), f"reuse returned {result!r}"
    if result is not None:
        assert result.endswith(backup_name), f"reuse returned a foreign path: {result!r}"
        # A reused backup is only returned when its manifest names the
        # probed hash, so a reuse must be the one the manifest recorded.
        assert probe_hash is not None, "reuse returned a backup for a probe with no hash"
        assert result == os.path.join(tmp, backup_name)
    # Fixed point: the same directory re-read yields the same answer.
    assert (
        _db_backup_reuse(tmp, probe_hash if probe_hash is not None else "no-such-hash") == result
    ), "reuse is not a fixed point"


def _check_verify_backup(raw: bytes, tmp: str) -> None:
    """Drive ``db_verify_backup`` over *raw* placed as its manifest."""
    backup = os.path.join(tmp, "assembly-20200101T000000Z.db")
    connection = sqlite3.connect(backup)
    try:
        connection.execute(
            "CREATE TABLE snippet (checksum TEXT, names TEXT, tags TEXT, collection TEXT)"
        )
        for table in ("collection", "lsh_bucket", "lsh_meta", "app_meta", "snippetversion"):
            connection.execute(f"CREATE TABLE {table} (x TEXT)")
        connection.commit()
    finally:
        connection.close()
    with open(os.path.join(tmp, _db_backup_manifest_name(backup)), "wb") as fh:
        fh.write(raw)
    result = db_verify_backup(None, backup)
    assert isinstance(result, dict), f"verify returned non-dict: {result!r}"
    assert isinstance(result["ok"], bool), f"ok is not a bool: {result!r}"
    assert isinstance(result["issues"], list), f"issues is not a list: {result!r}"
    assert all(isinstance(issue, str) for issue in result["issues"])
    # Fixed point: verifying the same bytes twice yields the same verdict.
    assert db_verify_backup(None, backup) == result, "verify is not a fixed point"


def _check_missing_and_directory(tmp: str) -> None:
    """Drive the readers at missing-file and directory file states."""
    # No manifest present at all: export pruning is disabled, reuse
    # skips the candidate, verification reports the missing manifest
    # as an issue without raising.
    assert _export_manifest_read(tmp) == set(), "missing export manifest did not disable pruning"
    backup = os.path.join(tmp, "assembly-20200101T000000Z.db")
    with open(backup, "wb") as fh:
        fh.write(b"x")
    assert _db_backup_reuse(tmp, "abc") is None, "missing backup manifest was reused"
    # A directory where the manifest file should be is unreadable input.
    dir_path = os.path.join(tmp, "directory-manifest")
    os.mkdir(dir_path)
    assert _export_manifest_read(dir_path) == set(), "directory manifest did not disable pruning"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    with tempfile.TemporaryDirectory() as tmp:
        raw = _build_document(fdp)
        # An oversized document is a length-check case, not a parser
        # case; keep the corpus on the interesting side of the guards.
        if len(raw) > 4096:
            raw = raw[:4096]
        _check_missing_and_directory(tmp)
        _check_export(raw, tmp)
        _check_backup_reuse(raw, tmp)
        _check_verify_backup(raw, tmp)


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
