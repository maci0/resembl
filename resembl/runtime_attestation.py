"""Verify an operator-provided runtime manifest against loaded module files.

The supervisor must protect the manifest and interpreter environment from writes.
This is file integrity under that trust boundary, not remote attestation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

MAX_FILES = 10000
MAX_BYTES = 512 * 1024 * 1024


def verify_runtime(manifest_path: str, expected: str) -> None:
    """Require the pinned manifest to cover interpreter and all loaded modules.

    Every rejection is a :class:`ValueError`: the ``/find-pinned``
    handler answers any other exception as a ``503`` with internal text
    in the log, so a malformed manifest must not escape as a second
    type.  That includes a missing or unreadable file (``OSError``) and
    a deeply nested document (``RecursionError`` from the JSON decoder,
    which is not a ``ValueError``), both of which the manifest bytes
    can produce without ever being a valid manifest.
    """
    try:
        raw = Path(manifest_path).read_bytes()
    except OSError as exc:
        raise ValueError("runtime manifest unreadable") from exc
    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("runtime manifest mismatch")
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid runtime manifest") from exc
    if not isinstance(document, dict) or not document or len(document) > MAX_FILES:
        raise ValueError("invalid runtime manifest")
    required = {str(Path(sys.executable).resolve())}
    for module in sys.modules.values():
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str):
            continue
        if filename.endswith(".pyc"):
            try:
                filename = importlib.util.source_from_cache(filename)
            except ValueError:
                pass
        required.add(str(Path(filename).resolve()))
    if len(required) > MAX_FILES or not required.issubset(document):
        raise ValueError("unverified runtime module")
    total = 0
    for name in required:
        expected_file = document[name]
        if not isinstance(expected_file, str) or len(expected_file) != 64:
            raise ValueError("invalid runtime digest")
        with open(name, "rb") as source:
            size = os.fstat(source.fileno()).st_size
            total += size
            if total > MAX_BYTES:
                raise ValueError("runtime verification limit")
            h = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                h.update(chunk)
        if h.hexdigest() != expected_file:
            raise ValueError("runtime file mismatch")
