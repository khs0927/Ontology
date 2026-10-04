"""Phase 2 (2026-10): configurable leases/timeouts, DWG conversion cache, pending embeddings, graph indexes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from aec_intelligence.dwg import DWGConversionResult, ODAConverter, cached_dxf_path, convert_dwg_cached
from aec_intelligence.operational import db as db_module
from aec_intelligence.operational.config import Settings
from aec_intelligence.operational.db import Database

FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"
SHA = "ab" * 32


# --------------------------------------------------------------------------- settings / timeouts
def test_lease_attempts_and_cache_come_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("AEC_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AEC_IMPORT_ROOTS", str(tmp_path))
    monkeypatch.setenv("AEC_LEASE_SECONDS", "900")
    monkeypatch.setenv("AEC_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("AEC_EMBEDDING_STRICT", "1")
    monkeypatch.setenv("AEC_ODA_TIMEOUT_SECONDS", "1200")
    monkeypatch.delenv("AEC_DXF_CACHE_DIR", raising=False)
    settings = Settings.from_env()
    assert (settings.lease_seconds, settings.max_attempts) == (900, 5)
    assert settings.embedding_strict is True and settings.oda_timeout_seconds == 1200
    assert settings.dxf_cache() == (tmp_path / "dxf-cache").resolve()


@pytest.mark.parametrize("name,value", [("AEC_LEASE_SECONDS", "5m"), ("AEC_LEASE_SECONDS", "3"),
                                        ("AEC_MAX_ATTEMPTS", "0")])
def test_malformed_numbers_fail_loudly(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv("AEC_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()


def test_database_connect_and_statement_timeouts(monkeypatch):
    seen = {}

    class Conn:
        statements: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=None):
            self.statements.append(str(statement))
            return self

    def fake_connect(dsn, **kwargs):
        seen.update(kwargs)
        return Conn()

    monkeypatch.setattr(db_module.psycopg, "connect", fake_connect)
    monkeypatch.setenv("AEC_DB_CONNECT_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("AEC_DB_STATEMENT_TIMEOUT_SECONDS", "45")
    db = Database("postgresql://example")
    with db.connect():
        pass
    assert seen["connect_timeout"] == 7
    assert "SET statement_timeout = '45s'" in Conn.statements
    with db.connect(statement_timeout_seconds=600):
        pass
    assert "SET statement_timeout = '600s'" in Conn.statements


# --------------------------------------------------------------------------- DWG conversion
class _CountingConverter:
    name = "fake"

    def __init__(self, ok=True):
        self.calls = 0
        self.ok = ok

    def convert_to_dxf(self, source, output_dir):
        self.calls += 1
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        target = out / (Path(source).stem + ".dxf")
        if not self.ok:
            return DWGConversionResult("FAILED", str(source), None, self.name, ["boom"], {})
        target.write_text("0\nEOF\n", encoding="utf-8")
        return DWGConversionResult("SUCCESS", str(source), str(target), self.name, [], {})


def test_conversion_cache_hit_skips_converter(tmp_path):
    source = tmp_path / "plan.dwg"
    source.write_bytes(b"dwg")
    conv = _CountingConverter()
    first = convert_dwg_cached(conv, source, tmp_path / "a", tmp_path / "cache", SHA)
    second = convert_dwg_cached(conv, source, tmp_path / "b", tmp_path / "cache", SHA)
    assert first.checks["cache"] == "miss" and second.checks["cache"] == "hit"
    assert conv.calls == 1
    assert Path(second.output).parent == cached_dxf_path(tmp_path / "cache", SHA, "x").parent
    assert Path(second.output).name.startswith(SHA)
    assert not list((tmp_path / "cache").rglob("*.tmp-*"))


def test_host_oda_cache_entry_serves_a_converter_without_oda(tmp_path):
    source = tmp_path / "plan.dwg"
    source.write_bytes(b"dwg")
    entry = cached_dxf_path(tmp_path / "cache", SHA, "oda-acad2018")
    entry.parent.mkdir(parents=True)
    entry.write_text("0\nEOF\n", encoding="utf-8")
    unconfigured = ODAConverter(None)
    unconfigured.output_version = "ACAD2013"  # different own key, still served by the ODA entry
    result = convert_dwg_cached(unconfigured, source, tmp_path / "o", tmp_path / "cache", SHA)
    assert result.status == "SUCCESS" and result.checks["cache"] == "hit" and Path(result.output) == entry


def test_failed_conversion_is_not_cached_and_no_sha_means_no_cache(tmp_path):
    source = tmp_path / "plan.dwg"
    source.write_bytes(b"dwg")
    bad = _CountingConverter(ok=False)
    assert convert_dwg_cached(bad, source, tmp_path / "a", tmp_path / "cache", SHA).status == "FAILED"
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").rglob("*.dxf"))
    good = _CountingConverter()
    convert_dwg_cached(good, source, tmp_path / "a", tmp_path / "cache", None)
    convert_dwg_cached(good, source, tmp_path / "a", tmp_path / "cache", "not-a-sha")
    assert good.calls == 2


def _fake_oda(tmp_path: Path) -> str:
    """ODA stand-in: converts every *.dwg in its input folder, like the real CLI, and logs what it saw."""
    log = tmp_path / "oda-seen.json"
    fake = tmp_path / "fake_oda.py"
    fake.write_text(
        "import json, sys, pathlib\n"
        "src, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
        "seen = sorted(p.name for p in src.glob('*.dwg'))\n"
        f"pathlib.Path({str(log)!r}).write_text(json.dumps({{'seen': seen, 'argv': sys.argv[1:]}}))\n"
        "[(out / (p.stem + '.dxf')).write_bytes(b'0\\nEOF\\n') for p in src.glob('*.dwg')]\n",
        encoding="utf-8",
    )
    if os.name == "nt":  # pragma: no cover - CI runs on Linux
        wrapper = tmp_path / "oda.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{fake}" %*\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "oda.sh"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return str(wrapper)


def test_oda_converts_only_the_job_source_not_its_whole_folder(tmp_path):
    folder = tmp_path / "drive"
    folder.mkdir()
    for name in ("a.dwg", "b.dwg", "c.dwg"):
        (folder / name).write_bytes(b"dwg " + name.encode())
    result = ODAConverter(_fake_oda(tmp_path)).convert_to_dxf(folder / "b.dwg", tmp_path / "out")
    seen = json.loads((tmp_path / "oda-seen.json").read_text())
    assert result.status == "SUCCESS", result.errors
    assert seen["seen"] == ["source.dwg"]  # staged alone; a.dwg and c.dwg were never handed to ODA
    assert seen["argv"][-1] == "*.DWG"
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["b.dxf"]


def test_oda_timeout_is_reported(monkeypatch, tmp_path):
    source = tmp_path / "plan.dwg"
    source.write_bytes(b"dwg")

    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr("aec_intelligence.dwg.subprocess.run", slow)
    result = ODAConverter("/x/ODAFileConverter", timeout_seconds=42).convert_to_dxf(source, tmp_path / "o")
    assert result.status == "FAILED" and "42s" in result.errors[0]


# --------------------------------------------------------------------------- Postgres-backed
DSN = os.getenv("AEC_TEST_DATABASE_URL")
pg = pytest.mark.skipif(not DSN, reason="AEC_TEST_DATABASE_URL not set")


class _VectorHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = [{"index": i, "embedding": [1.0] + [0.0] * 1023} for i, _ in enumerate(body["input"])]
        raw = json.dumps({"data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture()
def vector_server():
    server = HTTPServer(("127.0.0.1", 0), _VectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _stack(tmp_path, **settings_kw):
    pytest.importorskip("psycopg")
    pytest.importorskip("ezdxf")
    imports = tmp_path / "imports"
    imports.mkdir(exist_ok=True)
    source = imports / FIXTURE.name
    shutil.copy(FIXTURE, source)
    settings = Settings(dsn=DSN, data_root=tmp_path, import_roots=(imports,), **settings_kw)
    db = Database(DSN)
    db.initialize()
    return db, settings, source


@pg
def test_embedding_outage_keeps_objects_and_reembed_fills_them(tmp_path, vector_server):
    from aec_intelligence.operational import embeddings as emb
    from aec_intelligence.operational.worker import IngestionWorker

    emb._CIRCUIT.clear()
    # A model name of its own: texts embedded by an earlier run would be reused (one vector per distinct text).
    model = f"test-model-{os.getpid()}"
    db, down, source = _stack(tmp_path, embedding_url="http://127.0.0.1:9", embedding_model=model)
    project, key = f"P-pending-{os.getpid()}", f"pending:{os.getpid()}"
    with db.connect() as conn:
        conn.execute("DELETE FROM aec.jobs WHERE dedup_key=%s", (key,))
    db.enqueue({"source": str(source), "project_id": project, "document_id": f"doc_pending_{os.getpid()}"}, key)
    assert IngestionWorker(db, down).run_once()
    with db.connect() as conn:
        job = conn.execute("SELECT state, error, result FROM aec.jobs WHERE dedup_key=%s", (key,)).fetchone()
        objects = conn.execute("SELECT count(*) AS n FROM aec.objects WHERE project_id=%s", (project,)).fetchone()["n"]
    assert job["state"] == "SUCCEEDED", job["error"]
    assert job["result"]["embeddings_pending"] is True and objects > 0

    up = Settings(dsn=DSN, data_root=tmp_path, import_roots=(), embedding_url=vector_server, embedding_model=model)
    emb._CIRCUIT.clear()
    try:
        assert emb.reindex_embeddings(db, up, project, dry_run=True)["pending"] > 0
        done = emb.reindex_embeddings(db, up, project)
        assert done["written"] > 0 and done["error"] is None
        assert done["embedded_texts"] == done["distinct_texts"] <= done["written"]
        assert emb.reindex_embeddings(db, up, project, dry_run=True)["pending"] == 0
    finally:
        with db.connect() as conn:
            conn.execute("DELETE FROM aec.embeddings WHERE model=%s", (model,))
            conn.execute("DELETE FROM aec.text_vectors WHERE model=%s", (model,))


@pg
def test_strict_embedding_mode_still_fails_the_job(tmp_path):
    from aec_intelligence.operational import embeddings as emb
    from aec_intelligence.operational.worker import IngestionWorker

    emb._CIRCUIT.clear()
    db, strict, source = _stack(tmp_path, embedding_url="http://127.0.0.1:9", embedding_strict=True)
    key = f"strict:{os.getpid()}"
    with db.connect() as conn:
        conn.execute("DELETE FROM aec.jobs WHERE dedup_key=%s", (key,))
    db.enqueue({"source": str(source), "project_id": f"P-strict-{os.getpid()}",
                "document_id": f"doc_strict_{os.getpid()}"}, key)
    IngestionWorker(db, strict).run_once()
    with db.connect() as conn:
        job = conn.execute("SELECT state, error FROM aec.jobs WHERE dedup_key=%s", (key,)).fetchone()
    assert job["state"] == "FAILED" and "EmbeddingEndpointError" in job["error"]


@pg
def test_graph_indexes_and_two_way_expansion(tmp_path):
    from aec_intelligence.operational.db import graph_name
    from aec_intelligence.operational.search import SearchRouter
    from aec_intelligence.operational.worker import IngestionWorker

    db, settings, source = _stack(tmp_path)
    project, key = f"P-gidx-{os.getpid()}", f"gidx:{os.getpid()}"
    with db.connect() as conn:
        conn.execute("DELETE FROM aec.jobs WHERE dedup_key=%s", (key,))
    db.enqueue({"source": str(source), "project_id": project, "document_id": f"doc_gidx_{os.getpid()}"}, key)
    assert IngestionWorker(db, settings).run_once()
    graph = graph_name(project)
    with db.connect() as conn:
        names = {r["indexname"] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname=%s", (graph,))}
        rel = conn.execute("SELECT subject, object FROM aec.relations WHERE project_id=%s AND predicate='contains' "
                           "LIMIT 1", (project,)).fetchone()
    assert {f"{graph}_Entity_id", f"{graph}_Rel_start_id", f"{graph}_Rel_end_id", f"{graph}_entity_props"} <= names
    assert db.ensure_graph_indexes(graph) == []  # idempotent

    router = SearchRouter(db, settings)
    with db.connect() as conn:
        child = router._get_relations(conn, project, rel["object"])
        parent = router._get_relations(conn, project, rel["subject"])
    assert any(r["subject"] == rel["subject"] and r["object"] == rel["object"] and r["source"] == "AGE" for r in child)
    assert any(r["subject"] == rel["subject"] for r in parent)
