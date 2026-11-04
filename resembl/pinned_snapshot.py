"""Bounded identity of a SQLite index while its caller holds a writer fence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_ROWS = 1_000_000


def index_digest(connection: Any) -> str:
    """Hash the exact decision-bearing rows from the caller's transaction."""
    if connection.dialect.name != "sqlite":
        raise ValueError("pinned queries require SQLite")
    digest = hashlib.sha256(b"resembl-index-snapshot-v2\0")
    size = rows = 0
    for table, query in (
        (
            "snippet",
            "SELECT checksum,names,code,minhash,tags,collection FROM snippet ORDER BY checksum",
        ),
        ("lsh_bucket", "SELECT band,bucket,checksum FROM lsh_bucket ORDER BY band,bucket,checksum"),
        ("lsh_meta", "SELECT id,threshold,num_perm FROM lsh_meta ORDER BY id"),
        ("app_meta", "SELECT key,value FROM app_meta ORDER BY key"),
    ):
        digest.update(table.encode() + b"\0")
        for row in connection.exec_driver_sql(query):
            rows += 1
            values: list[list[Any]] = []
            for value in row:
                if value is None:
                    values.append(["null", None])
                elif isinstance(value, bytes | memoryview):
                    values.append(["blob", bytes(value).hex()])
                elif isinstance(value, int):
                    values.append(["integer", value])
                elif isinstance(value, float):
                    values.append(["real", value])
                elif isinstance(value, str):
                    values.append(["text", value])
                else:
                    raise ValueError("unsupported SQLite snapshot value")
            raw = json.dumps(
                values, ensure_ascii=True, separators=(",", ":"), allow_nan=False
            ).encode()
            size += len(raw)
            if rows > MAX_SNAPSHOT_ROWS or size > MAX_SNAPSHOT_BYTES:
                raise ValueError("index snapshot exceeds qualification limit")
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
    return digest.hexdigest()
