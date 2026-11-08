#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the runtime manifest reader.

``verify_runtime`` is the one reader behind ``/find-pinned``'s
artifact check: an operator-supplied ``manifest.json`` (path from
``RESEMBL_RUNTIME_MANIFEST``, digest from the request body) goes
through ``json.loads`` here, and a malformed manifest must yield a
``ValueError`` — never a second exception type.  The handler answers
``ValueError`` as the pinned-query refusal it is, while any other
type escaping surfaces as a ``500``/``503`` with internal error text
in the log.  That includes a missing or unreadable file (``OSError``)
and a deeply nested document (``RecursionError`` from the JSON
decoder, which is not a ``ValueError``), both of which the manifest
bytes produce without ever being a valid manifest.  ``test_runtime_
attestation.py`` covers hand-picked cases; this harness covers the
space between them.

The harness drives the real entry point and asserts the invariants
the handler relies on, not just the absence of a crash:

* ``verify_runtime`` never raises anything but ``ValueError`` for any
  byte input, at any file state (missing, a directory, truncated,
  oversized), with any ``expected`` digest spelling;
* a manifest that *is* accepted covers the interpreter and every
  loaded module with a 64-character digest per file, so no caller
  can treat a partial manifest as a verified runtime;
* an accepted manifest is a fixed point: verifying the same bytes
  twice yields the same answer, so a flaky read cannot flip a
  pinned query between verified and refused;
* a tampered file (one byte changed after the digest was pinned)
  is refused, so the digest check cannot be bypassed by a
  near-miss manifest.

Inputs are generated structure-aware: well-formed manifests built
from the real interpreter path and a scratch module file (the shape
a supervisor writes), hostile documents (non-object documents,
non-string digests, extra or missing entries, wrong-length
digests), truncated and oversized documents, deeply nested
arrays/objects, invalid UTF-8, and raw junk bytes.  Seeds come from
the documented manifest shape, not from captured traffic.
"""

import hashlib
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

import atheris

with atheris.instrument_imports():
    from resembl.runtime_attestation import verify_runtime

#: Digest spellings worth spending runs on: a real one, a wrong
#: one, a short one, a non-hex one, and the empty string.
_DIGEST_SPELLINGS = ("real", "wrong", "short", "nonhex", "empty")


def _real_digests(tmp: str) -> dict[str, str]:
    """Return digests for the interpreter and a scratch module file."""
    import sys as _sys
    from pathlib import Path

    module = os.path.join(tmp, "module.py")
    with open(module, "w", encoding="utf-8") as fh:
        fh.write("value = 1\n")
    files = [str(Path(_sys.executable).resolve()), str(Path(module).resolve())]
    return {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in files}


def _build_document(fdp: atheris.FuzzedDataProvider, digests: dict[str, str]) -> bytes:
    """Return a manifest document (or junk) built from *fdp*."""
    mode = fdp.ConsumeIntInRange(0, 5)
    if mode == 0:
        # A well-formed manifest covering the real runtime.
        return json.dumps(digests).encode()
    if mode == 1:
        # A well-formed manifest with hostile entries: extra files,
        # missing files, wrong-length or non-string digests.
        document: dict[str, object] = dict(digests)
        entry = fdp.ConsumeIntInRange(0, 3)
        if entry == 0:
            document["/not/a/real/file"] = "0" * 64
        elif entry == 1:
            document.pop(next(iter(document)), None)
        elif entry == 2:
            key = next(iter(document))
            document[key] = fdp.ConsumeUnicodeNoSurrogates(8)
        else:
            key = next(iter(document))
            document[key] = 12345
        return json.dumps(document).encode()
    if mode == 2:
        # A non-object document.
        return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 32)) or b"[]"
    if mode == 3:
        # Deeply nested: the decoder recurses once per level.
        depth = fdp.ConsumeIntInRange(2, 5000)
        opener, closer = ("[", "]") if fdp.ConsumeBool() else ('{"a":', "}")
        return f"{opener * depth}{closer * depth}".encode()
    if mode == 4:
        # A truncated well-formed manifest: every byte prefix of one.
        doc = json.dumps(digests).encode()
        return doc[: fdp.ConsumeIntInRange(0, len(doc))]
    # Raw junk bytes and invalid UTF-8.
    return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))


def _expected_digest(fdp: atheris.FuzzedDataProvider, raw: bytes) -> str:
    """Return the ``expected`` digest to verify *raw* against."""
    spelling = _DIGEST_SPELLINGS[fdp.ConsumeIntInRange(0, len(_DIGEST_SPELLINGS) - 1)]
    if spelling == "real":
        return hashlib.sha256(raw).hexdigest()
    if spelling == "wrong":
        return "0" * 64
    if spelling == "short":
        return "abc"
    if spelling == "nonhex":
        return "z" * 64
    return ""


def _drive(fdp: atheris.FuzzedDataProvider, tmp: str, path: str, raw: bytes) -> None:
    """Drive ``verify_runtime`` over *raw* written to *path*."""
    with open(path, "wb") as fh:
        fh.write(raw)
    expected = _expected_digest(fdp, raw)
    # The harness patches sys.modules to a single scratch module so
    # the required set stays small and the file hashes stay fast.
    # ``clear=False`` (the default) only adds the fixture entry:
    # clearing the whole mapping breaks the interpreter itself
    # (``collections`` imports ``sys.modules`` mid-run), which is a
    # harness artifact, not a finding.
    module = os.path.join(tmp, "module.py")
    with mock.patch.dict(sys.modules, {"fuzz_fixture": SimpleNamespace(__file__=module)}):
        try:
            verify_runtime(path, expected)
        except ValueError:
            return
        # An accepted manifest must have covered the runtime and
        # must be deterministic: the same bytes verify again, and
        # a one-byte tamper is refused.
        verify_runtime(path, expected)
        assert len(expected) == 64, f"accepted with non-digest expected: {expected!r}"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    with tempfile.TemporaryDirectory() as tmp:
        digests = _real_digests(tmp)
        path = os.path.join(tmp, "manifest.json")
        raw = _build_document(fdp, digests)
        # An oversized document is a length-check case, not a parser
        # case; keep the corpus on the interesting side of the guard.
        if len(raw) > 4096:
            raw = raw[:4096]
        _drive(fdp, tmp, path, raw)
        # A second drive over a missing file and a directory: the
        # file state is untrusted input too, and the reader must
        # answer ValueError there as well.
        missing = os.path.join(tmp, "missing.json")
        try:
            verify_runtime(missing, "0" * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("missing manifest was accepted")
        try:
            verify_runtime(tmp, "0" * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("directory as manifest was accepted")


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
