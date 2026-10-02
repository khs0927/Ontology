import hashlib
from pathlib import Path

from aec_intelligence.operational.worker import source_sha256


def test_source_sha256_prefers_payload_hash(tmp_path: Path, monkeypatch):
    source = tmp_path / "a.dwg"
    source.write_bytes(b"data")
    given = "A" * 64
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError("must not read whole file")))
    assert source_sha256(source, {"sha256": given}) == given.lower()


def test_source_sha256_streams_when_payload_missing(tmp_path: Path, monkeypatch):
    source = tmp_path / "b.dwg"
    data = b"x" * (3 * 1024 + 7)
    source.write_bytes(data)
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError("must not read whole file")))
    assert source_sha256(source, {}, chunk_size=1024) == hashlib.sha256(data).hexdigest()
    assert source_sha256(source, {"sha256": "bogus"}) == hashlib.sha256(data).hexdigest()
    assert source_sha256(tmp_path / "missing.dwg", None) == "unknown"
