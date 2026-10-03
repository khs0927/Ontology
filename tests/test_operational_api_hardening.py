"""REST boundary hardening for the operational API (no PostgreSQL needed)."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aec_intelligence.operational import api as api_module
from aec_intelligence.operational.census import document_id_for
from aec_intelligence.operational.config import Settings
from aec_intelligence.operational.ingest_jobs import file_sha256, ingest_job


class FakeDatabase:
    """Records enqueued jobs; any real SQL access fails the test."""

    def __init__(self, dsn):
        self.dsn = dsn
        self.jobs: list[tuple[dict, str]] = []

    def enqueue(self, payload, dedup_key):
        self.jobs.append((payload, dedup_key))
        return {"id": uuid.uuid4()}

    @contextmanager
    def connect(self):  # pragma: no cover - reaching this is the failure
        raise AssertionError("database must not be touched for rejected requests")
        yield


@pytest.fixture()
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, FakeDatabase] = {}

    def make_db(dsn):
        holder["db"] = FakeDatabase(dsn)
        return holder["db"]

    monkeypatch.setattr(api_module, "Database", make_db)
    monkeypatch.delenv("AEC_CORS_ORIGINS", raising=False)
    imports = tmp_path / "imports"
    imports.mkdir()
    settings = Settings(dsn="dummy", data_root=tmp_path / "data", import_roots=(imports.resolve(),))
    client = TestClient(api_module.create_app(settings))
    return client, holder["db"], imports, tmp_path


def test_ingestion_rejects_paths_outside_import_roots(app_env):
    client, db, _imports, tmp_path = app_env
    outside = tmp_path / "secret.dxf"
    outside.write_text("0\nEOF\n", encoding="utf-8")
    res = client.post("/v1/ingestions", json={"path": str(outside)})
    assert res.status_code == 403
    assert "import roots" in res.json()["detail"]
    assert db.jobs == []


def test_ingestion_missing_path_is_400(app_env):
    client, db, imports, _ = app_env
    res = client.post("/v1/ingestions", json={"path": str(imports / "missing.dxf")})
    assert res.status_code == 400
    assert "not found" in res.json()["detail"].lower()
    assert db.jobs == []


def test_ingestion_skips_symlinks_escaping_import_roots(app_env):
    client, db, imports, tmp_path = app_env
    outside = tmp_path / "outside.dxf"
    outside.write_text("0\nEOF\n", encoding="utf-8")
    inside = imports / "inside.dxf"
    inside.write_text("0\nSECTION\n0\nEOF\n", encoding="utf-8")
    try:
        os.symlink(outside, imports / "link.dxf")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    res = client.post("/v1/ingestions", json={"path": str(imports)})
    assert res.status_code == 202
    assert [Path(p["source"]).name for p, _ in db.jobs] == ["inside.dxf"]


def test_same_stem_files_get_distinct_content_addressed_document_ids(app_env):
    client, db, imports, _ = app_env
    (imports / "A").mkdir()
    (imports / "B").mkdir()
    (imports / "C").mkdir()
    (imports / "A" / "plan.dxf").write_text("0\nSECTION\n2\nA\n0\nEOF\n", encoding="utf-8")
    (imports / "B" / "plan.dxf").write_text("0\nSECTION\n2\nB\n0\nEOF\n", encoding="utf-8")
    (imports / "C" / "plan.dxf").write_text("0\nSECTION\n2\nA\n0\nEOF\n", encoding="utf-8")  # same bytes as A

    res = client.post("/v1/ingestions", json={"path": str(imports), "project_id": "P-1"})
    assert res.status_code == 202 and res.json()["enqueued_count"] == 3
    by_folder = {Path(p["source"]).parent.name: p for p, _ in db.jobs}
    assert by_folder["A"]["document_id"] != by_folder["B"]["document_id"]
    assert by_folder["A"]["document_id"] == by_folder["C"]["document_id"]
    sha = file_sha256(imports / "A" / "plan.dxf")
    assert by_folder["A"]["sha256"] == sha
    assert by_folder["A"]["document_id"] == document_id_for(sha)
    assert all(p["project_id"] == "P-1" and p["queue"] == "cad" for p, _ in db.jobs)
    # Distinct paths keep distinct dedup keys so every file is enqueued.
    assert len({key for _, key in db.jobs}) == 3


def test_ingestion_rejects_unknown_queue(app_env):
    client, db, imports, _ = app_env
    (imports / "a.dxf").write_text("0\nEOF\n", encoding="utf-8")
    res = client.post("/v1/ingestions", json={"path": str(imports / "a.dxf"), "queue": "gpu"})
    assert res.status_code == 422
    assert db.jobs == []


def test_ingest_job_payload_shape(tmp_path: Path):
    source = tmp_path / "x.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    payload, key = ingest_job(source, project_id="P", discipline="ARCH", queue="ocr")
    assert payload["source"] == str(source.resolve())
    assert payload["document_id"].startswith("doc_") and len(payload["document_id"]) == 28
    assert payload["queue"] == "ocr" and payload["revision"] == 0
    assert len(key) == 64


@pytest.mark.parametrize(
    "body",
    [
        {"object_id": "o1", "action": "DELETE"},
        {"object_id": "o1", "action": "CHANGE_TYPE"},
        {"object_id": "o1", "action": "CHANGE_TYPE", "new_type": "  "},
    ],
)
def test_invalid_review_actions_are_rejected_before_any_write(app_env, body):
    client, _db, _imports, _ = app_env
    assert client.post("/v1/reviews", json=body).status_code == 422


@pytest.mark.parametrize("path", ["/v1/jobs/not-a-uuid", "/v1/jobs/1;DROP"])
def test_malformed_job_ids_are_404_not_500(app_env, path):
    client, _db, _imports, _ = app_env
    assert client.get(path).status_code == 404
    assert client.post(path + "/retry").status_code == 404


def test_cors_is_not_open_by_default(app_env):
    client, _db, _imports, _ = app_env
    res = client.options(
        "/v1/ingestions",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in {k.lower() for k in res.headers}
    res = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in res.headers}


def test_cors_allow_list_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api_module, "Database", FakeDatabase)
    monkeypatch.setenv("AEC_CORS_ORIGINS", "http://localhost:3000/, *")
    settings = Settings(dsn="dummy", data_root=tmp_path, import_roots=(tmp_path,))
    client = TestClient(api_module.create_app(settings))
    ok = client.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "access-control-allow-credentials" not in {k.lower() for k in ok.headers}
    bad = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in bad.headers}


def test_cors_origins_parser_drops_wildcard():
    assert api_module.cors_origins_from_env("*") == []
    assert api_module.cors_origins_from_env(" http://a:1/ ,http://b ") == ["http://a:1", "http://b"]


def test_dashboard_escapes_drawing_text():
    html = (Path(api_module.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    assert "function esc(" in html
    assert "${hit.label}" not in html and "${item.label}" not in html
    assert 'onclick="submitReview(' not in html


def test_serve_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch):
    import sys
    import types

    from aec_intelligence.operational import cli

    captured = {}
    fake_uvicorn = types.SimpleNamespace(run=lambda *a, **kw: captured.update(kw))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    cli.main(["serve"])
    assert captured["host"] == "127.0.0.1"
