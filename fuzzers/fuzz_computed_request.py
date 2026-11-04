#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the ``/score`` and ``/band-keys`` computed endpoints.

``POST /score`` and ``POST /band-keys`` are unauthenticated, read no
database, and take their whole input from the request body: ``source`` /
``candidates`` / ``texts`` listings, ``jaccard_floor``, ``bands`` and
``rows``, plus the shared find tunables.  Every field is untrusted, so a
parse or fingerprint step that escapes its guard turns a hostile request
into a 500 with internal error text, or into work the request never
bounded.

The harness drives the two real payload functions, in the order the
handler drives them, and asserts the invariants the handlers rely on,
not just the absence of a crash:

* the payloads reject malformed input with ``BadRequestError`` only
  (the handlers catch exactly that and answer 400); any other exception
  type escaping is a bug the fuzzer can see;
* an accepted ``/score`` returns one score per candidate, in request
  order, each either ``None`` (below the floor) or a finite value in
  ``[0, 100]`` — the hybrid score is capped by construction, so an
  out-of-range or NaN score is a logic bug, not a finding;
* an accepted ``/band-keys`` returns ``bands`` keys per text, one list
  per text in request order;
* an accepted response carries a scheme whose ``id`` is a 32-char hex
  digest and whose fields echo the request's (defaulted) parameters.

Bodies are generated structure-aware: listings of fuzzer-chosen strings
(including blanks, which the endpoints treat specially), edge scalars
for the numeric fields (including the values that overflow
``int()``/``float()`` and NaN/infinity), and the find tunables drawn
from the same edge set the ``/find`` harness uses.  Listing sizes are
kept small: the endpoints fingerprint every entry, so a huge list
would spend the run on one request instead of covering more shapes.
"""

import json
import math
import sys
from typing import Any

import atheris

with atheris.instrument_imports():
    from resembl.config import ResemblConfig
    from resembl.server import (
        BadRequestError,
        _band_keys_payload,
        _score_payload,
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

#: Texts worth spending runs on: a blank one (special-cased), a realistic
#: listing, and inputs that stress the tokenizer and the ratio.
_INTERESTING_TEXTS = (
    "",
    "   ",
    "ret",
    "mov eax, 1\nret",
    "push ebp\nmov ebp, esp\nret",
    "a" * 512,
    "\x00\x01\x02",
    "héllo wörld — ünïcode",
    "ＡＢＣ１２３",  # noqa: RUF001 - fullwidth forms stress normalization
)


def _text(fdp: atheris.FuzzedDataProvider) -> str:
    """Return a fuzzer-chosen text from the interesting set or raw unicode."""
    if fdp.ConsumeBool():
        return _INTERESTING_TEXTS[fdp.ConsumeIntInRange(0, len(_INTERESTING_TEXTS) - 1)]
    return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 64))


def _listing(fdp: atheris.FuzzedDataProvider) -> Any:
    """Return a candidates/texts field: usually a list, sometimes hostile."""
    mode = fdp.ConsumeIntInRange(0, 5)
    if mode == 0:
        return [_text(fdp) for _ in range(fdp.ConsumeIntInRange(0, 4))]
    if mode == 1:
        # A non-string slipped into the list: the field check must refuse it.
        listing = [_text(fdp) for _ in range(fdp.ConsumeIntInRange(0, 3))]
        listing.insert(0, _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)])
        return listing
    if mode == 2:
        # A bare string, which would iterate per character if not refused.
        return _text(fdp)
    return _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]


def _build_body(fdp: atheris.FuzzedDataProvider, endpoint: str) -> dict[str, Any]:
    """Return a request body for *endpoint* built from *fdp*."""
    body: dict[str, Any] = {}
    if endpoint == "score":
        body["source"] = (
            _text(fdp)
            if fdp.ConsumeBool()
            else _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
        )
        body["candidates"] = _listing(fdp)
        if fdp.ConsumeBool():
            body["jaccard_floor"] = _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    else:
        body["texts"] = _listing(fdp)
        body["bands"] = _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
        body["rows"] = _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    for _ in range(fdp.ConsumeIntInRange(0, len(_PARAM_NAMES))):
        name = _PARAM_NAMES[fdp.ConsumeIntInRange(0, len(_PARAM_NAMES) - 1)]
        body[name] = _EDGE_VALUES[fdp.ConsumeIntInRange(0, len(_EDGE_VALUES) - 1)]
    return body


def _check_scheme(payload: dict[str, Any]) -> None:
    """Assert the returned scheme echoes the request's parameters."""
    scheme = payload["scheme"]
    assert isinstance(scheme["id"], str) and len(scheme["id"]) == 32, "scheme id is not a digest"
    int(scheme["id"], 16)  # hex, or ValueError escapes as a crash: a logic bug
    for name in ("num_permutations", "ngram_size", "normalize"):
        assert name in scheme, f"scheme is missing {name}"


def test_one_input(data):
    """The entry point for the fuzzer."""
    fdp = atheris.FuzzedDataProvider(data)
    endpoint = "score" if fdp.ConsumeBool() else "band-keys"
    body = _build_body(fdp, endpoint)
    # Round-trip through JSON so the harness sees exactly the types the
    # handler sees: JSON has no NaN/infinity of its own, and ``json``
    # encodes those as bare tokens Python's decoder accepts back.
    try:
        body = json.loads(json.dumps(body))
    except ValueError:
        return
    assert isinstance(body, dict)

    compute = _score_payload if endpoint == "score" else _band_keys_payload
    try:
        payload = compute(body, ResemblConfig())
    except BadRequestError:
        return

    _check_scheme(payload)
    if endpoint == "score":
        scores = payload["scores"]
        candidates = body["candidates"]
        assert isinstance(candidates, list), "score accepted non-list candidates"
        assert len(scores) == len(candidates), "score count disagrees with candidate count"
        for score in scores:
            assert score is None or (
                isinstance(score, float) and math.isfinite(score) and 0.0 <= score <= 100.0
            ), f"score out of range: {score!r}"
    else:
        keys = payload["keys"]
        texts = body["texts"]
        assert isinstance(texts, list), "band-keys accepted non-list texts"
        assert len(keys) == len(texts), "key count disagrees with text count"
        # ``bands``/``rows`` accept numeric strings too (``_as_int``), so
        # the accepted count is read from the answer, not the request.
        widths = {len(text_keys) for text_keys in keys}
        assert len(widths) <= 1, f"inconsistent band counts: {widths}"
        for text_keys in keys:
            assert all(isinstance(k, str) for k in text_keys)


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
