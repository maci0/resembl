"""The opt-in snapshot stays fenced from identity through execution."""

import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

import resembl.models  # noqa: F401
from resembl import runtime_attestation, server
from resembl.pinned_snapshot import index_digest


def test_pinned_query_blocks_writer_and_rejects_changed_snapshot(tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    monkeypatch.setenv("RESEMBL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("RESEMBL_RUNTIME_MANIFEST", "fixture")
    monkeypatch.setattr(runtime_attestation, "verify_runtime", lambda *_: None)
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    httpd = server.serve(f"sqlite:///{db}", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with Session(httpd.engine) as session:
            expected = index_digest(session.connection())
        entered = threading.Event()
        release = threading.Event()
        written = threading.Event()
        result = []

        def fake_find(_session, _query, _request):
            entered.set()
            assert release.wait(5)
            return {"lsh_candidates": 0, "matches": []}

        def post():
            request = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_address[1]}/find-pinned",
                data=json.dumps(
                    {
                        "query": "ret",
                        "index_sha256": expected,
                        "artifact_sha256": "a" * 64,
                        "generation": httpd.runtime_generation,
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                result.append(json.load(response))

        with patch.object(server, "_find_uncached", fake_find):
            client = threading.Thread(target=post)
            client.start()
            assert entered.wait(5)

            def write():
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "INSERT INTO app_meta (key,value) VALUES ('test-write','1')"
                    )
                written.set()

            writer = threading.Thread(target=write)
            writer.start()
            assert not written.wait(0.2)
            release.set()
            client.join(5)
            writer.join(5)
            assert written.is_set()
            assert result[0]["index_sha256"] == expected
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/find-pinned",
            data=json.dumps(
                {
                    "query": "ret",
                    "index_sha256": expected,
                    "artifact_sha256": "a" * 64,
                    "generation": httpd.runtime_generation,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            with urllib.request.urlopen(request, timeout=5):
                pass
        assert exc.value.code == 409
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        engine.dispose()


def test_pinned_query_executes_real_engine(tmp_path, monkeypatch):
    db = tmp_path / "real.db"
    monkeypatch.setenv("RESEMBL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("RESEMBL_RUNTIME_MANIFEST", "fixture")
    monkeypatch.setattr(runtime_attestation, "verify_runtime", lambda *_: None)
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    httpd = server.serve(f"sqlite:///{db}", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with Session(httpd.engine) as session:
            digest = index_digest(session.connection())
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/find-pinned",
            data=json.dumps(
                {
                    "query": "ret",
                    "index_sha256": digest,
                    "artifact_sha256": "a" * 64,
                    "generation": httpd.runtime_generation,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.load(response)
        assert body["index_sha256"] == digest
        assert isinstance(body["result"]["matches"], list)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        engine.dispose()


def test_pinned_query_rejects_other_process_generation(tmp_path, monkeypatch):
    db = tmp_path / "generation.db"
    monkeypatch.setenv("RESEMBL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("RESEMBL_RUNTIME_MANIFEST", "fixture")
    monkeypatch.setattr(runtime_attestation, "verify_runtime", lambda *_: None)
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    httpd = server.serve(f"sqlite:///{db}", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with Session(httpd.engine) as session:
            digest = index_digest(session.connection())
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/find-pinned",
            data=json.dumps(
                {
                    "query": "ret",
                    "index_sha256": digest,
                    "artifact_sha256": "a" * 64,
                    "generation": "stale",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            with urllib.request.urlopen(request, timeout=5):
                pass
        assert exc.value.code == 503
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        engine.dispose()


def test_warm_restart_rejects_previous_generation(tmp_path, monkeypatch):
    db = tmp_path / "restart.db"
    monkeypatch.setenv("RESEMBL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("RESEMBL_RUNTIME_MANIFEST", "fixture")
    monkeypatch.setattr(runtime_attestation, "verify_runtime", lambda *_: None)
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    first = server.serve(f"sqlite:///{db}", port=0)
    old = first.runtime_generation
    first.server_close()
    second = server.serve(f"sqlite:///{db}", port=0)
    thread = threading.Thread(target=second.serve_forever, daemon=True)
    thread.start()
    try:
        assert second.runtime_generation != old
        with Session(second.engine) as session:
            digest = index_digest(session.connection())
        request = urllib.request.Request(
            f"http://127.0.0.1:{second.server_address[1]}/find-pinned",
            data=json.dumps(
                {
                    "query": "ret",
                    "index_sha256": digest,
                    "artifact_sha256": "a" * 64,
                    "generation": old,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            with urllib.request.urlopen(request, timeout=5):
                pass
        assert exc.value.code == 503
    finally:
        second.shutdown()
        second.server_close()
        thread.join(5)
        engine.dispose()


def test_snapshot_distinguishes_blob_from_text(tmp_path):
    db = tmp_path / "types.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO snippet (checksum, names, code, minhash, tags, collection) "
            "VALUES ('type-test', '[]', 'ret', X'6162', '[]', NULL)"
        )
        blob_digest = index_digest(connection)
        connection.exec_driver_sql(
            "UPDATE snippet SET minhash = '6162' WHERE checksum = 'type-test'"
        )
        assert index_digest(connection) != blob_digest
    engine.dispose()
