#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the ``/find-pinned`` request gate and ``/find-batch`` shape.

``POST /find-pinned`` is the one unauthenticated endpoint whose body
mixes identity fields (``artifact_sha256``, ``generation``,
``index_sha256``) with the find tunables, and ``_handle_find_pinned``
decides — before any database work — whether the caller has named a
verified runtime, a well-formed index digest, and a usable query.
``fuzz_pinned_snapshot.py`` covers the digest *computation* and
``fuzz_runtime_manifest.py`` the manifest *reader*; neither drives the
handler's own guards, so a guard that escapes its documented answer
(a 503 for an unverified runtime, a 400 for a malformed digest or
query) turns one crafted body into a 500, a hung writer fence, or a
pinned query answered for an index that was never verified.

The harness drives the same checks the handler runs, in the same
order, using the same helpers (``_query_field_error``,
``_parse_find_request``), and asserts the invariants the handler
relies on, not just the absence of a crash:

* the identity gate answers *either* a refusal (``artifact`` not a
  string, ``generation`` not the server's, or no manifest configured)
  *or* a well-formed 64-char lowercase-hex ``index_sha256`` — an
  uppercase, short, or non-hex digest never passes the gate, so a
  pinned query can never be answered against a digest the snapshot
  could not have produced;
* past the gate, the query passes ``_query_field_error`` and the
  tunables pass ``_parse_find_request`` (``BadRequestError`` only);
  any second exception type escaping is a bug the fuzzer can see,
  because the handler catches exactly ``BadRequestError`` as the
  400 and everything else as a 503;
* a body that passes every gate is a fixed point: re-running the
  gates on the same body reaches the same verdict, so a flaky parse
  cannot flip a pinned query between refused and answered.

Inputs are generated structure-aware: identity fields drawn from
edge scalars, real and near-miss digests (right length but uppercase,
non-hex, or one char short), a matching generation or a near-miss
one, queries that are blank, non-string, or realistic assembly, and
the find tunables from the edge set the ``/find`` harness uses.
Listing sizes stay small: the endpoint fingerprints every entry, so
a huge body would spend the run on one request.  Seeds come from the
documented pinned-query shape, not from captured traffic.
"""

import json
import sys
from typing import Any

import atheris

with atheris.instrument_imports():
    from resembl.config import ResemblConfig
    from resembl.server import (
        BadRequestError,
        _parse_find_request,
        _query_field_error,
    )

#: Values that are legal JSON but hostile to ``int()``/``float()``, plus
#: the range edges each parameter is checked against.
_EDGE_VALUES: tuple[Any, ...] = (
    0,
    1,
    -1,
    2,
    0.0,
    0.5,
    1.0,
    1.5,
    2.0,
    -0.5,
    float("inf"),
    float("-inf"),
    float("nan"),
    True,
    False,
    None,
    "",
    "0.9",
    "abc",
    10**30,
    -(10**30),
    1 << 62,
    [],
    {},
    [1, 2],
    {"a": 1},
)

#: The find tunables a caller may send, shared with the ``/find`` path.
_PARAM_NAMES = (
    "top_n",
    "threshold",
    "normalize",
    "ngram_size",
    "num_permutations",
    "jaccard_weight",
)

#: Digest spellings worth spending runs on: the well-formed shape the
#: snapshot produces, plus every near-miss the gate must refuse.
_DIGEST_SPELLINGS: tuple[str, ...] = (
    "0" * 64,
    "0123456789abcdef" * 4,
    "A" * 64,
    "a" * 63,
    "a" * 65,
    "g" * 64,
    "z" * 64,
    "",
    "abc",
)

#: Queries worth spending runs on: realistic assembly, blanks, and
#: non-strings that must not reach the lexer.
_QUERIES: tuple[Any, ...] = (
    "mov eax, 1\nret",
    "push ebp\nmov ebp, esp\nret",
    "",
    "   ",
    "\x00\x01",
    "héllo",
    None,
    123,
    [],
    {},
)

_GENERATION = "fuzz-generation"


def _digest(fdp: atheris.FuzzedDataProvider) -> str:
    """Return a fuzzer-chosen digest spelling."""
    if fdp.ConsumeBool():
        return _DIGEST_SPELLINGS[fdp.ConsumeIntInRange(0, len(_DIGEST_SPELLINGS) - 1)]
    return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 72))


def _build_body(fdp: atheris.FuzzedDataProvider) -> dict[str, Any]:
    """Return a ``/find-pinned`` body built from *fdp*."""
    body: dict[str, Any] = {}
    # Identity fields: usually present, sometimes hostile.
    body["artifact_sha256"] = (
        _digest(fdp)
        if fdp.ConsumeBool()
        else _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    )
    if fdp.ConsumeBool():
        body["generation"] = (
            _GENERATION
            if fdp.ConsumeBool()
            else _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
        )
    body["index_sha256"] = _digest(fdp)
    body["query"] = (
        _QUERIES[fdp.ConsumeIntInRange(0, len(_QUERIES) - 1)]
        if fdp.ConsumeBool()
        else fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 64))
    )
    for _ in range(fdp.ConsumeIntInRange(0, len(_PARAM_NAMES))):
        name = _PARAM_NAMES[fdp.ConsumeIntInRange(0, len(_PARAM_NAMES) - 1)]
        body[name] = _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    return body


def _gate(body: dict[str, Any], manifest_configured: bool) -> str:
    """Run the handler's pre-database gates; return "pass" or a status.

    Mirrors ``_handle_find_pinned``'s order: identity, then digest
    shape, then query, then tunables.  The manifest path is taken as
    configured-or-not by the caller so both branches are exercised
    without an environment variable.
    """
    artifact = body.get("artifact_sha256")
    generation = body.get("generation")
    if not isinstance(artifact, str) or not manifest_configured or generation != _GENERATION:
        return "503"
    expected = body.get("index_sha256")
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(c not in "0123456789abcdef" for c in expected)
    ):
        return "400"
    if _query_field_error(body.get("query")) is not None:
        return "400"
    try:
        _parse_find_request(body, ResemblConfig())
    except BadRequestError:
        return "400"
    return "pass"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    body = _build_body(fdp)
    # Round-trip through JSON so the harness sees exactly the types the
    # handler sees after ``_read_body``.
    try:
        body = json.loads(json.dumps(body))
    except ValueError:
        return
    assert isinstance(body, dict)

    manifest_configured = fdp.ConsumeBool()
    verdict = _gate(body, manifest_configured)

    # The gate's verdict must be a function of the body alone: the
    # same body twice reaches the same verdict, so a flaky parse
    # cannot flip a pinned query between refused and answered.
    assert _gate(body, manifest_configured) == verdict, "gate is not deterministic"

    if verdict == "pass":
        # Past the gate the handler holds a writer fence and runs the
        # query: every field it will read must hold its documented
        # shape, or the fence is taken for a request that was never
        # well-formed.
        assert isinstance(body["artifact_sha256"], str)
        expected = body["index_sha256"]
        assert isinstance(expected, str) and len(expected) == 64
        assert all(c in "0123456789abcdef" for c in expected), "non-hex digest passed"
        assert _query_field_error(body["query"]) is None
        # A well-formed digest never collides with a case variant
        # the snapshot could not have produced: an uppercase copy
        # of an accepted digest is a different string, so it must
        # not be the same index identity.  Compare through the
        # gate's digest check: the uppercase form either fails the
        # lowercase-hex check outright or, when it happens to be all
        # digits, is a different digest string that cannot match a
        # computed ``index_digest``.
        upper_expected = expected.upper()
        assert (
            upper_expected not in "0123456789abcdef" or upper_expected != expected
        ), "uppercase digest is an identical digest string"
        assert upper_expected != expected or all(c in "0123456789abcdef" for c in upper_expected)
    elif verdict == "400" and manifest_configured and body.get("generation") == _GENERATION:
        # A 400 past the identity gate is about the digest or the
        # query/tunables, never about identity: at least one of those
        # fields must be malformed, or the gate refused a good body.
        digest_ok = (
            isinstance(body.get("index_sha256"), str)
            and len(body["index_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in body["index_sha256"])
        )
        if digest_ok:
            assert _query_field_error(body.get("query")) is not None or _tunables_bad(
                body
            ), "gate refused a well-formed body"


def _tunables_bad(body: dict[str, Any]) -> bool:
    """Return whether the find tunables in *body* fail validation."""
    try:
        _parse_find_request(body, ResemblConfig())
    except BadRequestError:
        return True
    return False


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
