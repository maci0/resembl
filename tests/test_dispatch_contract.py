"""The backend's dispatch contract for resembl, held against the stateless server.

``tests/fixtures/dispatch/resembl.schema.json`` is the backend's schema, copied
byte for byte (``NOTE.txt`` there names the commit and digest).  The backend's
adapter sends each op's request as the body of one ``resembl serve`` endpoint
and answers the endpoint's reply as the op's reply, so a request valid under
the schema has to be accepted and its reply has to be valid under it.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

from resembl.config import ResemblConfig

FIXTURES = Path(__file__).parent / "fixtures" / "dispatch"
CONTRACT_BYTES = (FIXTURES / "resembl.schema.json").read_bytes()
CONTRACT = json.loads(CONTRACT_BYTES)

#: Each op's endpoint on ``resembl serve`` (the backend's
#: internal/engines/resembl/target.go) and a request the backend could send.
OPS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "resembl.scheme": ("GET", "/scheme", {}),
    "resembl.score": (
        "POST",
        "/score",
        {
            "source": "push ebx\nmov eax, 1\npop ebx\nret",
            "candidates": [
                "push ebx\nmov eax, 2\npop ebx\nret",
                "xor ecx, ecx\nlea esi, [edi+8]\ncall dword ptr [eax]\nleave\nret 8",
                "  ",
            ],
            "jaccard_floor": 0.2,
            "jaccard_weight": 0.5,
        },
    ),
    "resembl.band_keys": (
        "POST",
        "/band-keys",
        {
            "texts": ["push ebx\nmov eax, 1\npop ebx\nret", "xor ecx, ecx\nleave\nret 8"],
            "bands": 16,
            "rows": 8,
            "num_permutations": 128,
        },
    ),
}

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}
_KEYWORDS = {
    "$schema",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "oneOf",
}


def _errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Where *value* breaks *schema*; refuses a keyword this checker does not read."""
    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise AssertionError(f"{path}: the contract uses keywords this check ignores: {unknown}")
    if "oneOf" in schema:
        matching = [option for option in schema["oneOf"] if not _errors(value, option, path)]
        return [] if len(matching) == 1 else [f"{path}: matches {len(matching)} oneOf options"]
    expected = _TYPES[schema["type"]]
    if isinstance(value, bool) and bool not in expected:
        return [f"{path}: {value!r} is not {schema['type']}"]
    if not isinstance(value, expected):
        return [f"{path}: {value!r} is not {schema['type']}"]
    found: list[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        found += [
            f"{path}: missing {name}" for name in schema.get("required", []) if name not in value
        ]
        if schema.get("additionalProperties") is False:
            found += [f"{path}: unexpected {name}" for name in value if name not in properties]
        for name, item in value.items():
            if name in properties:
                found += _errors(item, properties[name], f"{path}.{name}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            found += _errors(item, schema["items"], f"{path}[{index}]")
    return found


class TestDispatchContract(unittest.TestCase):
    """Every op the backend's schema names, through the server the backend deploys."""

    def setUp(self) -> None:
        # The directory lives for the whole test; addCleanup removes it.
        workdir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(workdir.cleanup)
        env = patch.dict(
            os.environ,
            {
                "RESEMBL_CACHE_DIR": os.path.join(workdir.name, "cache"),
                "DATABASE_URL": f"sqlite:///{os.path.join(workdir.name, 'missing', 'a.db')}",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        from resembl.server import serve

        with patch("resembl.config.load_config", return_value=ResemblConfig()):
            httpd = serve(None, port=0)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 10)
        self.addCleanup(httpd.shutdown)
        self._port = httpd.server_address[1]

    def _call(self, method: str, path: str, body: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=json.dumps(body).encode() if method == "POST" else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def test_the_copy_is_the_one_its_note_names(self) -> None:
        note = (FIXTURES / "NOTE.txt").read_text(encoding="utf-8")
        named = re.search(r"SHA-256: ([0-9a-f]{64})", note)
        self.assertIsNotNone(named)
        assert named is not None
        self.assertEqual(hashlib.sha256(CONTRACT_BYTES).hexdigest(), named.group(1))

    def test_every_op_of_the_contract_is_exercised(self) -> None:
        self.assertEqual(set(CONTRACT["ops"]), set(OPS))

    def test_requests_and_replies_follow_the_contract(self) -> None:
        for op, (method, path, request) in OPS.items():
            with self.subTest(op=op):
                schema = CONTRACT["ops"][op]
                self.assertEqual(_errors(request, schema["request"]), [])
                reply = self._call(method, path, request)
                self.assertEqual(_errors(reply, schema["response"]), [], reply)

    def test_a_score_below_the_floor_is_null_in_the_reply(self) -> None:
        method, path, request = OPS["resembl.score"]
        scores = self._call(method, path, request)["scores"]
        self.assertIsInstance(scores[0], float)
        self.assertIsNone(scores[2])
