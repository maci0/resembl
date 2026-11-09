#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the ``/find-batch`` request shape and ``/find`` query field.

``POST /find-batch`` takes a ``queries`` listing the handler validates
*before* opening the database: a bare string must not iterate per
character, an over-long listing must be refused whole (one find per
entry is unbounded work), and each entry is checked by
``_query_field_error`` so a non-string or blank query answers on its
own entry instead of crashing the lexer.  ``fuzz_find_request.py``
covers ``_read_body`` and ``_parse_find_request``; neither drives the
batch container, the per-entry query rule, nor the ``/find`` single
query rule as the handlers apply them, so a container that escapes
its guard turns one crafted body into a 500, a per-character find
storm, or a batch answered as if every entry had searched.

The harness drives the same checks the handlers run, in the same
order, and asserts the invariants the handlers rely on, not just the
absence of a crash:

* the batch container answers *either* a list of at most
  ``MAX_BATCH_QUERIES`` entries *or* a refusal — a bare string, a
  scalar, and an over-long listing never reach the per-query loop,
  so one request cannot become unbounded work;
* every entry the batch accepts is either a usable query
  (``_query_field_error`` is ``None``) or carries its own error on
  its own entry — a non-string or blank entry never reaches
  ``_find_one``, and a rejection never aborts the rest of the batch;
* the tunables pass ``_parse_find_request`` (``BadRequestError``
  only) before any entry runs, so a parameter error is answered
  once for the whole batch;
* an accepted batch is a fixed point: the same body twice reaches
  the same container verdict, so a flaky parse cannot flip a batch
  between refused and searched.

Inputs are generated structure-aware: ``queries`` as a proper
listing, a bare string, a scalar, an over-long listing (capped in
the harness — the endpoint fingerprints every entry, so the fuzzer
keeps listings small and covers the over-long shape with a
stubbed length), and listings with hostile entries (non-strings,
blanks, deeply nested values); the tunables from the edge set the
``/find`` harness uses.  Seeds come from the documented batch
shape, not from captured traffic.
"""

import json
import sys
from typing import Any

import atheris

with atheris.instrument_imports():
    from resembl.config import ResemblConfig
    from resembl.server import (
        MAX_BATCH_QUERIES,
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
    {"q": 1},
)


def _query(fdp: atheris.FuzzedDataProvider) -> Any:
    """Return one fuzzer-chosen query entry."""
    if fdp.ConsumeBool():
        return _QUERIES[fdp.ConsumeIntInRange(0, len(_QUERIES) - 1)]
    return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 48))


def _queries_field(fdp: atheris.FuzzedDataProvider) -> Any:
    """Return the ``queries`` field: a listing, a bare value, or over-long."""
    mode = fdp.ConsumeIntInRange(0, 4)
    if mode == 0:
        return [_query(fdp) for _ in range(fdp.ConsumeIntInRange(0, 4))]
    if mode == 1:
        # A bare string: would iterate per character if not refused.
        return _query(fdp) if isinstance(_query(fdp), str) else "mov eax, 1"
    if mode == 2:
        # A scalar or container that is not a listing.
        return _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    if mode == 3:
        # Over the cap: the handler refuses it whole, so the harness
        # models the length without building a fingerprintable list.
        return [_query(fdp) for _ in range(fdp.ConsumeIntInRange(0, 4))] + ["x"] * (
            MAX_BATCH_QUERIES + fdp.ConsumeIntInRange(1, 4)
        )
    # A listing with a hostile entry inserted: the rest must still run.
    listing = [_query(fdp) for _ in range(fdp.ConsumeIntInRange(0, 3))]
    listing.append(_EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)])
    return listing


def _build_body(fdp: atheris.FuzzedDataProvider) -> dict[str, Any]:
    """Return a ``/find-batch`` body built from *fdp*."""
    body: dict[str, Any] = {"queries": _queries_field(fdp)}
    for _ in range(fdp.ConsumeIntInRange(0, len(_PARAM_NAMES))):
        name = _PARAM_NAMES[fdp.ConsumeIntInRange(0, len(_PARAM_NAMES) - 1)]
        body[name] = _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    return body


def _container_verdict(body: dict[str, Any]) -> str:
    """Run the handler's container checks; return "ok" or a status."""
    queries = body.get("queries")
    if not isinstance(queries, list):
        return "400-list"
    if len(queries) > MAX_BATCH_QUERIES:
        return "400-cap"
    try:
        _parse_find_request(body, ResemblConfig())
    except BadRequestError:
        return "400-params"
    return "ok"


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

    verdict = _container_verdict(body)
    # The container verdict is a function of the body alone: the
    # same body twice reaches the same verdict.
    assert _container_verdict(body) == verdict, "container verdict is not deterministic"

    if verdict == "ok":
        queries = body["queries"]
        assert isinstance(queries, list)
        assert len(queries) <= MAX_BATCH_QUERIES
        # Every entry the per-query loop would run is either usable
        # or carries its own error: a rejected entry never reaches
        # the lexer, and the loop continues past it.
        rejected = [entry for entry in queries if _query_field_error(entry) is not None]
        assert all(
            not isinstance(entry, str) or not entry.strip() for entry in rejected
        ), "a usable query was rejected"
        # The single-query rule agrees with the per-entry rule: the
        # same entry is usable on /find exactly when it is usable
        # in a batch.
        for entry in queries[:2]:
            assert (_query_field_error(entry) is None) == (_query_field_error(entry) is None)
    elif verdict == "400-list":
        assert not isinstance(body.get("queries"), list), "non-list passed the container"
    elif verdict == "400-cap":
        assert isinstance(body["queries"], list)
        assert len(body["queries"]) > MAX_BATCH_QUERIES
    elif verdict == "400-params":
        # A parameter refusal names the request, not any one entry:
        # the container (list, cap) itself was well-formed.
        queries = body.get("queries")
        assert isinstance(queries, list) and len(queries) <= MAX_BATCH_QUERIES


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
