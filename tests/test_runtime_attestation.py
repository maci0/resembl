"""The pinned runtime refuses changed or unlisted loaded code."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from resembl.runtime_attestation import verify_runtime


def test_runtime_file_change_and_unknown_module_refuse(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text("value = 1\n", encoding="utf-8")
    executable = str(Path(sys.executable).resolve())
    files = [executable, str(module.resolve())]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in files})
    )
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with patch.dict(sys.modules, {"fixture": SimpleNamespace(__file__=str(module))}, clear=True):
        verify_runtime(str(manifest), digest)
        module.write_text("value = 2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="runtime file mismatch"):
            verify_runtime(str(manifest), digest)
        with pytest.raises(ValueError, match="manifest mismatch"):
            verify_runtime(str(manifest), "0" * 64)


def test_real_warm_engine_with_runtime_manifest(tmp_path, monkeypatch) -> None:
    import threading
    import urllib.request

    from sqlmodel import Session, SQLModel, create_engine

    import resembl.models  # noqa: F401
    from resembl import server
    from resembl.pinned_snapshot import index_digest

    database = tmp_path / "runtime.db"
    monkeypatch.setenv("RESEMBL_CACHE_DIR", str(tmp_path / "cache"))
    engine = create_engine(f"sqlite:///{database}")
    SQLModel.metadata.create_all(engine)
    httpd = cast(
        server._FindServer,  # pylint: disable=protected-access
        server.serve(f"sqlite:///{database}", port=0),
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        warm = urllib.request.Request(
            url + "/find", data=b'{"query":"ret"}', headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(warm, timeout=10) as response:
            assert response.status == 200
        with Session(httpd.engine) as session:
            index = index_digest(session.connection())
        import importlib.util

        paths = {str(Path(sys.executable).resolve())}
        for mod in tuple(sys.modules.values()):
            name = getattr(mod, "__file__", None)
            if isinstance(name, str):
                if name.endswith(".pyc"):
                    try:
                        name = importlib.util.source_from_cache(name)
                    except ValueError:
                        pass
                paths.add(str(Path(name).resolve()))
        manifest = tmp_path / "runtime.json"
        manifest.write_text(
            json.dumps({p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths})
        )
        pin = hashlib.sha256(manifest.read_bytes()).hexdigest()
        monkeypatch.setenv("RESEMBL_RUNTIME_MANIFEST", str(manifest))
        body = {
            "query": "ret",
            "index_sha256": index,
            "artifact_sha256": pin,
            "generation": httpd.runtime_generation,
        }
        request = urllib.request.Request(
            url + "/find-pinned",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        assert result["artifact_sha256"] == pin and result["index_sha256"] == index
        if shutil.which("go") is None:
            pytest.skip("Python half passed; the Go half needs a go toolchain")
        env = os.environ.copy()
        env.update(
            RESEMBL_TEST_ENGINE_URL=url,
            RESEMBL_TEST_INDEX=index,
            RESEMBL_TEST_ARTIFACT=pin,
            RESEMBL_TEST_GENERATION=httpd.runtime_generation,
        )
        subprocess.run(
            [
                "go",
                "-C",
                "service",
                "test",
                "-run",
                "^TestRealPinnedEngineWithAuthorityBoundary$",
                "-count=1",
            ],
            check=True,
            env=env,
            timeout=60,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        engine.dispose()


def _manifest(tmp_path: Path, document: object) -> tuple[str, str]:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document))
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "invalid runtime manifest"),
        ([], "invalid runtime manifest"),
        ({"/not/the/executable": "0" * 64}, "unverified runtime module"),
    ],
)
def test_manifest_must_cover_the_runtime(tmp_path: Path, document: object, message: str) -> None:
    path, digest = _manifest(tmp_path, document)
    with patch.dict(sys.modules, {}, clear=True):
        with pytest.raises(ValueError, match=message):
            verify_runtime(path, digest)


def test_manifest_digest_must_be_a_sha256_string(tmp_path: Path) -> None:
    executable = str(Path(sys.executable).resolve())
    path, digest = _manifest(tmp_path, {executable: "short"})
    with patch.dict(sys.modules, {}, clear=True):
        with pytest.raises(ValueError, match="invalid runtime digest"):
            verify_runtime(path, digest)


def test_oversized_manifest_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="manifest mismatch"):
        verify_runtime(str(path), hashlib.sha256(path.read_bytes()).hexdigest())


def test_manifest_read_stops_at_its_byte_limit(tmp_path: Path) -> None:
    """A refused oversized manifest must not first be loaded whole into memory."""
    path = tmp_path / "oversized.json"
    limit = 2 * 1024 * 1024
    with path.open("wb") as output:
        output.seek(3 * limit - 1)
        output.write(b" ")
    transferred = 0
    with path.open("rb") as source:
        real_read = source.read

        def counted_read(size: int = -1) -> bytes:
            nonlocal transferred
            data = real_read(size)
            transferred += len(data)
            return data

        with (
            patch.object(Path, "open", return_value=source),
            patch.object(source, "read", side_effect=counted_read),
            pytest.raises(ValueError, match="manifest mismatch"),
        ):
            verify_runtime(str(path), "0" * 64)
    assert transferred == limit + 1


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{not json", "invalid runtime manifest"),
        (b"[" * 200000 + b"]" * 200000, "invalid runtime manifest"),
        (b"", "invalid runtime manifest"),
        (b"null", "invalid runtime manifest"),
        (b'"just a string"', "invalid runtime manifest"),
    ],
)
def test_malformed_manifest_is_a_value_error(tmp_path: Path, raw: bytes, message: str) -> None:
    """Every malformed manifest is a ``ValueError``, never a second type.

    A deeply nested document recurses in the JSON decoder and raises
    ``RecursionError``, which is not a ``ValueError``; the handler
    answers ``ValueError`` as the pinned-query refusal and any other
    type as a ``503`` with internal error text in the log.
    """
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match=message):
        verify_runtime(str(path), hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("missing", [True, False])
def test_unreadable_manifest_is_a_value_error(tmp_path: Path, missing: bool) -> None:
    """A missing or directory manifest path is a ``ValueError``.

    ``Path.read_bytes`` raises ``FileNotFoundError`` /
    ``IsADirectoryError`` (both ``OSError``) there; the handler's
    ``ValueError``-only contract needs them wrapped.
    """
    target = tmp_path / "missing.json" if missing else tmp_path
    with pytest.raises(ValueError, match="runtime manifest unreadable"):
        verify_runtime(str(target), "0" * 64)


def test_verification_byte_limit(tmp_path: Path, monkeypatch) -> None:
    executable = str(Path(sys.executable).resolve())
    path, digest = _manifest(
        tmp_path, {executable: hashlib.sha256(Path(executable).read_bytes()).hexdigest()}
    )
    monkeypatch.setattr("resembl.runtime_attestation.MAX_BYTES", 1)
    with patch.dict(sys.modules, {}, clear=True):
        with pytest.raises(ValueError, match="runtime verification limit"):
            verify_runtime(path, digest)
