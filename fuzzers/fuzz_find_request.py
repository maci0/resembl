#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the HTTP request body and find-parameter validation.

``POST /find`` and ``POST /find-batch`` take their whole input from the
network: the ``Content-Length`` header, the raw body bytes, and the JSON
object decoded from them.  Every other byte is untrusted, so this is the
widest attack surface the project has — a parse that escapes its guard turns
a hostile request into a 500 with internal error text, or into a handler
thread that dies without answering at all.

The harness drives the two real entry points, in the order the handler
drives them:

* :meth:`_FindHandler._read_body` reads the declared length and the body,
  parses JSON, and rejects anything that is not an object.  It is called
  with a stub supplying only ``headers`` and ``rfile``, the two attributes
  it touches, so the fuzzed length/body pair goes through shipped code
  rather than a copy of it.
* :func:`_parse_find_request` coerces and range-checks the tunables.  The
  harness asserts it answers *either* a request whose fields hold the
  documented bounds or a ``BadRequestError`` — never a second exception
  type, which is the shape a bug takes here: a type the handlers do not
  catch, escaping as an unhandled 500.

Bodies are generated structure-aware: one of the parameter names gets a
fuzzer-chosen JSON scalar (including strings, bools, null, and the
values that overflow ``int()``/``float()``), plus nesting, non-object
documents, truncation, and length/body disagreement.
"""

import io
import json
import math
import sys
from typing import Any

import atheris

with atheris.instrument_imports():
    from resembl.config import ResemblConfig
    from resembl.scoring import MAX_NUM_PERM
    from resembl.server import BadRequestError, _FindHandler, _parse_find_request

#: The find tunables a caller may send, and the type each one is coerced to.
_PARAM_NAMES = (
    "top_n",
    "threshold",
    "normalize",
    "ngram_size",
    "num_permutations",
    "jaccard_weight",
)

#: Values that are legal JSON but hostile to ``int()``/``float()``, plus the
#: range edges each parameter is checked against.
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


class _StubHandler:
    """The two attributes :meth:`_FindHandler._read_body` reads."""

    def __init__(self, content_length: str, body: bytes) -> None:
        self.headers = {"Content-Length": content_length}
        self.rfile = io.BytesIO(body)


def _build_body(fdp: atheris.FuzzedDataProvider) -> Any:
    """Return a JSON body, or raw bytes to send verbatim."""
    mode = fdp.ConsumeIntInRange(0, 5)
    if mode == 0:
        return fdp.ConsumeBytes(fdp.remaining_bytes())
    if mode == 1:
        # Deeply nested: the JSON decoder recurses per level, so this is how
        # a small body reaches the interpreter's recursion limit.
        depth = fdp.ConsumeIntInRange(2, 200_000)
        opener, closer = ("[", "]") if fdp.ConsumeBool() else ('{"a":', "}")
        return f"{opener * depth}{closer * depth}".encode()
    if mode == 2:
        # A well-formed document that is not an object.
        return json.dumps(fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())).encode()
    body: dict[str, Any] = {"query": fdp.ConsumeUnicodeNoSurrogates(32)}
    if mode == 3:
        body["queries"] = [
            fdp.ConsumeUnicodeNoSurrogates(16) for _ in range(fdp.ConsumeIntInRange(0, 4))
        ]
    for _ in range(fdp.ConsumeIntInRange(1, len(_PARAM_NAMES))):
        name = _PARAM_NAMES[fdp.ConsumeIntInRange(0, len(_PARAM_NAMES) - 1)]
        body[name] = _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    return json.dumps(body).encode()


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    body = _build_body(fdp)
    if isinstance(body, str):  # pragma: no cover - _build_body returns bytes
        body = body.encode()

    # Fuzz the declared length independently of the body: a lying
    # Content-Length, a negative one, and a non-numeric one are all inputs.
    length_mode = fdp.ConsumeIntInRange(0, 5)
    if length_mode == 0:
        content_length = str(len(body))
    elif length_mode == 1:
        content_length = str(max(0, len(body) + fdp.ConsumeIntInRange(-4, 4)))
    elif length_mode == 2:
        content_length = f"-{fdp.ConsumeIntInRange(1, 1 << 40)}"
    elif length_mode == 3:
        content_length = fdp.ConsumeUnicodeNoSurrogates(8)
    elif length_mode == 4:
        content_length = str(fdp.ConsumeIntInRange(0, 1 << 30))
    else:
        content_length = ""

    # A malformed body answers None, never an exception: the handler turns
    # that into a clean 400.
    parsed = _FindHandler._read_body(  # pylint: disable=protected-access
        _StubHandler(content_length, body)
    )
    assert parsed is None or isinstance(
        parsed, dict
    ), "read_body returned a non-object, non-None body"
    if parsed is None:
        return

    try:
        request = _parse_find_request(parsed, ResemblConfig())
    except BadRequestError:
        return

    # An accepted request must satisfy the bounds the handler advertised:
    # every one of them is used to size a query, a banding structure, or a
    # cached MinHash template, so a value slipping through here is a memory
    # or a ranking bug downstream.
    assert isinstance(request.top_n, int) and not isinstance(
        request.top_n, bool
    ), f"top_n is not an int: {request.top_n!r}"
    assert request.ngram_size >= 1, f"ngram_size below 1 accepted: {request.ngram_size}"
    assert (
        2 <= request.num_permutations <= MAX_NUM_PERM
    ), f"num_permutations out of range: {request.num_permutations}"
    assert (
        0.0 <= request.jaccard_weight <= 1.0
    ), f"jaccard_weight out of range: {request.jaccard_weight}"
    assert (
        0.0 <= request.effective_threshold <= 1.0
    ), f"effective_threshold out of range: {request.effective_threshold}"
    if request.threshold is not None:
        assert 0.0 <= request.threshold <= 1.0, f"threshold out of range: {request.threshold}"
    # NaN survives a naive range check, then poisons every comparison it
    # takes part in and serializes as a token no JSON parser accepts.
    for name in ("jaccard_weight", "effective_threshold", "threshold"):
        value = getattr(request, name)
        assert value is None or not math.isnan(value), f"{name} was accepted as NaN"


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
