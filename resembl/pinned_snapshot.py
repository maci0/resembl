"""Bounded identity of a SQLite index while its caller holds a writer fence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_ROWS = 1_000_000
_SNAPSHOT_CHUNK_ROWS = 1000


def _snapshot_value(value: Any) -> list[Any]:
    """Return the JSON encoding of one SQLite value for the digest."""
    if value is None:
        return ["null", None]
    if isinstance(value, bytes | memoryview):
        return ["blob", bytes(value).hex()]
    if isinstance(value, int):
        return ["integer", value]
    if isinstance(value, float):
        return ["real", value]
    if isinstance(value, str):
        return ["text", value]
    raise ValueError("unsupported SQLite snapshot value")


def index_digest(connection: Any) -> str:
    """Hash the exact decision-bearing rows from the caller's transaction."""
    if connection.dialect.name != "sqlite":
        raise ValueError("pinned queries require SQLite")
    digest = hashlib.sha256(b"resembl-index-snapshot-v2\0")
    bytes_count = row_count = 0
    for table, query in (
        (
            "snippet",
            "SELECT checksum,names,code,minhash,tags,collection FROM snippet ORDER BY checksum",
        ),
        ("lsh_bucket", "SELECT band,bucket,checksum FROM lsh_bucket ORDER BY band,bucket,checksum"),
        ("lsh_meta", "SELECT id,threshold,num_perm FROM lsh_meta ORDER BY id"),
        ("app_meta", "SELECT key,value FROM app_meta ORDER BY key"),
    ):
        digest.update(table.encode("utf-8") + b"\0")
        cursor = connection.exec_driver_sql(query)
        try:
            while True:
                rows_chunk = cursor.fetchmany(_SNAPSHOT_CHUNK_ROWS)
                if not rows_chunk:
                    break
                for row in rows_chunk:
                    row_count += 1
                    values = [_snapshot_value(value) for value in row]
                    raw = json.dumps(
                        values, ensure_ascii=True, separators=(",", ":"), allow_nan=False
                    ).encode("utf-8")
                    bytes_count += len(raw) + 8
                    if row_count > MAX_SNAPSHOT_ROWS or bytes_count > MAX_SNAPSHOT_BYTES:
                        raise ValueError("index snapshot exceeds qualification limit")
                    digest.update(len(raw).to_bytes(8, "big"))
                    digest.update(raw)
        finally:
            cursor.close()
    return digest.hexdigest()
