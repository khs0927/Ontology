"""Legacy doc_<stem> document report / re-ingest tool (offline, no PostgreSQL)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import ClassVar

from aec_intelligence.operational.census import document_id_for
from aec_intelligence.operational.config import Settings
from aec_intelligence.operational.ingest_jobs import file_sha256
from aec_intelligence.operational.legacy_ids import (
    cleanup_sql,
    is_legacy_id,
    main,
    scan,
)


def _snap(root: Path, doc: str, rev: int, source: Path, project: str = "P1") -> None:
    d = root / "snapshots" / doc
    d.mkdir(parents=True, exist_ok=True)
    (d / f"rev-{rev}.json").write_text(json.dumps({
        "document_id": doc, "project_id": project, "source_key": str(source),
        "revision": rev, "source_hash": f"h{rev}", "objects": [],
    }), encoding="utf-8")


def _fixture(tmp_path: Path):
    data = tmp_path / "data"
    imports = tmp_path / "imports"
    (imports / "A").mkdir(parents=True)
    (imports / "B").mkdir(parents=True)
    a = imports / "A" / "평면도.dxf"
    b = imports / "B" / "평면도.dxf"
    a.write_text("0\nEOF\n", encoding="utf-8")
    b.write_text("0\nSECTION\n0\nEOF\n", encoding="utf-8")
    _snap(data, "doc_평면도", 1, a)
    _snap(data, "doc_평면도", 2, b)  # same stem, different file → collision
    _snap(data, "doc_single", 1, imports / "gone.dxf")  # legacy, file no longer exists
    _snap(data, document_id_for("0" * 64), 1, a)  # content-addressed: ignored
    settings = Settings(dsn="dummy", data_root=data, import_roots=(imports.resolve(),))
    return settings, a, b


def test_legacy_id_detection():
    assert is_legacy_id("doc_평면도") and is_legacy_id("doc_plan")
    assert not is_legacy_id(document_id_for("a" * 64))
    assert not is_legacy_id("other")


def test_scan_reports_collisions(tmp_path):
    settings, a, b = _fixture(tmp_path)
    docs = {d.document_id: d for d in scan(settings.data_root)}
    assert set(docs) == {"doc_평면도", "doc_single"}
    assert docs["doc_평면도"].collided
    assert docs["doc_평면도"].sources == {str(a): [1], str(b): [2]}
    assert not docs["doc_single"].collided


def test_dry_run_changes_nothing(tmp_path):
    settings, *_ = _fixture(tmp_path)
    calls = []
    out = io.StringIO()
    report = tmp_path / "r.json"
    assert main(["--json", str(report)], settings=settings, database_factory=lambda dsn: calls.append(dsn), out=out) == 0
    assert calls == []  # no DB connection in dry-run
    text = out.getvalue()
    assert "COLLIDED" in text and "dry run" in text and "file missing" in text
    data = json.loads(report.read_text(encoding="utf-8"))
    assert len(data["reingest"]) == 2
    assert "DELETE FROM aec.documents" in data["cleanup_sql"]


def test_apply_only_enqueues_content_addressed_jobs(tmp_path):
    settings, a, b = _fixture(tmp_path)

    class DB:
        jobs: ClassVar[list] = []

        def __init__(self, dsn):
            pass

        def enqueue(self, payload, dedup_key):
            DB.jobs.append(payload)

    out = io.StringIO()
    assert main(["--apply"], settings=settings, database_factory=DB, out=out) == 0
    ids = {j["source"]: j["document_id"] for j in DB.jobs}
    assert ids == {str(a.resolve()): document_id_for(file_sha256(a)), str(b.resolve()): document_id_for(file_sha256(b))}
    assert {j["project_id"] for j in DB.jobs} == {"P1"}
    assert "COMMIT" in out.getvalue()


def test_cleanup_sql_quotes_ids():
    sql = cleanup_sql(["doc_o'brien"])
    assert "'doc_o''brien'" in sql and sql.startswith("BEGIN;")
    assert cleanup_sql([]) == ""
