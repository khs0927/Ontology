"""Census -> enqueue -> run-workers -> report -> backup pipeline.

Pure file-system parts always run; the database parts need AEC_TEST_DATABASE_URL (a disposable,
possibly shared database: every test uses unique project ids/queues and deletes its rows).
"""

import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from aec_intelligence.operational import backup, census

DSN = os.getenv("AEC_TEST_DATABASE_URL")


def _with_dbname(dsn: str, dbname: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{dbname}"))
needs_db = pytest.mark.skipif(not DSN, reason="AEC_TEST_DATABASE_URL not set")


def _tree(tmp_path: Path, tag: str = "") -> Path:
    root = tmp_path / "내 드라이브"
    (root / "현장A 오피스텔" / "건축" / "평면").mkdir(parents=True)
    (root / "현장A 오피스텔" / "구조").mkdir(parents=True)
    (root / "현장B" / "기계설비").mkdir(parents=True)
    (root / "현장B" / "ARCH").mkdir(parents=True)
    header = b"AC1032" + b"\x00" * 128 + tag.encode()
    (root / "현장A 오피스텔" / "건축" / "평면" / "1층 평면도.dwg").write_bytes(header + b"one")
    (root / "현장A 오피스텔" / "구조" / "기초 구조도.dwg").write_bytes(b"AC1015" + b"\x00" * 64 + tag.encode())
    # Duplicate content of the first drawing in another project.
    (root / "현장B" / "ARCH" / "copy of plan.dwg").write_bytes(header + b"one")
    (root / "현장B" / "기계설비" / "배관.pdf").write_bytes(b"%PDF-1.4 fake " + tag.encode())
    (root / "현장B" / "기계설비" / "배관.dwg.bak").write_bytes(b"backup")
    (root / "현장B" / "기계설비" / "1층 평면도.sv$").write_bytes(b"autosave")
    (root / "현장B" / "기계설비" / "~$lock.dwg").write_bytes(b"lock")
    (root / "현장B" / "기계설비" / "plan.dwl").write_bytes(b"lock")
    (root / "현장B" / "notes.txt").write_text("ignored", encoding="utf-8")
    (root / "root-level.dxf").write_text(f"999\n{tag or 'x'}\n0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1027\n0\nENDSEC\n0\nEOF\n")
    return root


def _rows(out: Path):
    return list(census.iter_census(out / "census.jsonl"))


def test_census_records_versions_duplicates_and_skips(tmp_path):
    root = _tree(tmp_path)
    result = census.run_census([root], tmp_path / "out")
    rows = {Path(r["path"]).name: r for r in _rows(tmp_path / "out")}
    plan = rows["1층 평면도.dwg"]
    assert plan["dwg_version"] == "AC1032" and plan["dwg_release"] == "2018+"
    assert plan["top_folder"] == "현장A 오피스텔" and plan["rel_path"] == "현장A 오피스텔/건축/평면/1층 평면도.dwg"
    assert len(plan["sha256"]) == 64
    assert rows["기초 구조도.dwg"]["dwg_release"] == "2000-2002"
    assert rows["root-level.dxf"]["dwg_version"] == "AC1027" and rows["root-level.dxf"]["top_folder"] == ""
    for skipped in ("배관.dwg.bak", "1층 평면도.sv$", "~$lock.dwg", "plan.dwl"):
        assert rows[skipped]["status"] == "skipped_temp" and rows[skipped]["sha256"] is None
    assert "notes.txt" not in rows
    summary = result.summary
    assert summary["files"] == 5 and summary["skipped_temp"] == 4
    assert summary["duplicates"]["groups"] == 1 and summary["duplicates"]["redundant_files"] == 1
    assert summary["by_extension"][".dwg"]["files"] == 3
    assert summary["by_top_folder"]["현장B"]["files"] == 2
    assert summary["other_extensions_ignored"] == {".txt": 1}
    assert any("AC1032" in key for key in summary["by_dwg_version"])
    out = tmp_path / "out"
    assert json.loads((out / "summary.json").read_text(encoding="utf-8"))["files"] == 5
    assert "현장A 오피스텔" in (out / "summary.md").read_text(encoding="utf-8")
    assert "1층 평면도.dwg" in (out / "census.csv").read_text(encoding="utf-8-sig")


def test_census_resume_skips_unchanged_and_rehashes_changed(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    out = tmp_path / "out"
    census.run_census([root], out)
    # Simulate a crash: torn last line must be dropped, not break the resume.
    with open(out / "census.jsonl", "a", encoding="utf-8") as handle:
        handle.write('{"path": "half')
    changed = root / "현장A 오피스텔" / "구조" / "기초 구조도.dwg"
    changed.write_bytes(b"AC1018" + b"\x00" * 80)
    (root / "현장B" / "ARCH" / "copy of plan.dwg").unlink()
    hashed = []
    real = census.hash_file
    monkeypatch.setattr(census, "hash_file", lambda p, e: hashed.append(Path(p).name) or real(p, e))
    result = census.run_census([root], out, resume=True)
    assert hashed == ["기초 구조도.dwg"]
    assert result.summary["run"]["resumed_unchanged"] == 7
    rows = _rows(out)
    assert len(rows) == len({r["path"] for r in rows}) == 8  # compacted, deleted file dropped
    assert {r["dwg_version"] for r in rows if r["name"] == "기초 구조도.dwg"} == {"AC1018"}
    assert result.summary["duplicates"]["groups"] == 0


def test_census_pilot_folder_keeps_project_grouping_and_other_rows(tmp_path):
    root = _tree(tmp_path)
    out = tmp_path / "out"
    first = census.run_census([root], out, only_folders=["현장A 오피스텔"])
    assert first.summary["files"] == 2 and set(first.summary["by_top_folder"]) == {"현장A 오피스텔"}
    second = census.run_census([root], out, only_folders=["현장B"], resume=True)
    assert set(second.summary["by_top_folder"]) == {"현장A 오피스텔", "현장B"}
    assert second.summary["files"] == 4


def test_project_discipline_and_document_ids():
    assert census.project_id_for("현장A 오피스텔") == "P-현장A-오피스텔"
    assert census.project_id_for("  ") == "P-ROOT"
    assert census.guess_discipline("현장/구조/S-101.dwg") == "STR"
    assert census.guess_discipline("현장/구조/기계실 MECH.dwg") == "MEP"  # deepest segment wins
    assert census.guess_discipline("Project/ARCH/A-101.dwg") == "ARCH"
    assert census.guess_discipline("Project/District/x.dwg", default="GEN") == "GEN"  # no 'STR' substring hit
    assert census.guess_discipline("현장/전기/E-1.dwg") == "ELEC"
    assert census.document_id_for("ab" * 32) == "doc_" + ("ab" * 12)


def test_plan_jobs_aliases_and_filters(tmp_path):
    root = _tree(tmp_path)
    census.run_census([root], tmp_path / "out")
    jobs = census.plan_jobs(tmp_path / "out")
    assert len(jobs) == 4  # 5 files, one duplicate pair
    dup = next(j for j in jobs if j["aliases"])
    assert len(dup["aliases"]) == 1 and dup["source"] < dup["aliases"][0]
    assert {j["project_id"] for j in jobs} == {"P-현장A-오피스텔", "P-현장B", "P-내-드라이브"}
    only = census.plan_jobs(tmp_path / "out", only_folders=["현장A 오피스텔"], extensions=[".dwg"])
    assert {j["discipline"] for j in only} == {"ARCH", "STR"}


def test_backup_rotation_only_touches_prefixed_dumps(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    names = [backup.backup_name(now=base + timedelta(days=i)) for i in range(5)]
    for name in names:
        (folder / name).write_bytes(b"PGDMP")
    (folder / "notes.dump").write_bytes(b"x")
    (folder / "other-20260101T000000Z.dump").write_bytes(b"x")
    (folder / "aec-db-latest.dump").write_bytes(b"x")
    deleted = backup.rotate(folder, keep=2)
    assert [p.name for p in deleted] == names[:3]
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        names[3:] + ["notes.dump", "other-20260101T000000Z.dump", "aec-db-latest.dump"])
    with pytest.raises(ValueError):
        backup.rotate(folder, keep=0)


def test_restore_requires_confirmation(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP")
    with pytest.raises(PermissionError):
        backup.run_restore("postgresql://nobody@/none", dump, yes=False)


# ------------------------------------------------------------------------------------------
# Database-backed tests
# ------------------------------------------------------------------------------------------

@pytest.fixture()
def db():
    pytest.importorskip("psycopg")
    from aec_intelligence.operational.db import Database

    database = Database(DSN)
    database.initialize()
    return database


def _cleanup(db, projects=(), shas=(), queue=None):
    from aec_intelligence.operational.db import graph_name

    with db.connect() as conn:
        for project in projects:
            docs = [r["id"] for r in conn.execute("SELECT id FROM aec.documents WHERE project_id=%s", (project,))]
            if docs:
                conn.execute("DELETE FROM aec.embeddings WHERE object_id IN (SELECT id FROM aec.objects WHERE document_id = ANY(%s))", (docs,))
                for table in ("objects", "relations", "index_state", "snapshots"):
                    conn.execute(f"DELETE FROM aec.{table} WHERE document_id = ANY(%s)", (docs,))
                conn.execute("DELETE FROM aec.documents WHERE id = ANY(%s)", (docs,))
            conn.execute("DELETE FROM aec.metrics WHERE payload->>'project_id'=%s", (project,))
            graph = graph_name(project)
            if conn.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name=%s", (graph,)).fetchone():
                conn.execute("SELECT drop_graph(%s, true)", (graph,))
        if shas:
            conn.execute("DELETE FROM aec.jobs WHERE dedup_key = ANY(%s)", (list(shas),))
        if queue:
            conn.execute("DELETE FROM aec.jobs WHERE payload->>'queue'=%s", (queue,))


@needs_db
def test_enqueue_census_is_idempotent_and_aliases_duplicates(tmp_path, db):
    tag = uuid.uuid4().hex
    root = _tree(tmp_path, tag)
    census.run_census([root], tmp_path / "out")
    queue = f"test-census-{tag[:8]}"
    shas = {r["sha256"] for r in _rows(tmp_path / "out") if r["sha256"]}
    try:
        first = census.enqueue_census(db, tmp_path / "out", queue=queue, import_roots=[tmp_path],
                                      prefix=f"P-T{tag[:6]}-")
        assert first["enqueued"] == 4 and first["aliased_paths"] == 1
        again = census.enqueue_census(db, tmp_path / "out", queue=queue, import_roots=[tmp_path],
                                      prefix=f"P-T{tag[:6]}-")
        assert again.get("enqueued", 0) == 0 and again["already_present"] == 4
        outside = census.enqueue_census(db, tmp_path / "out", queue=queue, import_roots=[tmp_path / "elsewhere"])
        assert outside["outside_import_roots"] == 4 and outside["planned"] == 0
        limited = census.enqueue_census(db, tmp_path / "out", queue=queue, limit=1, dry_run=True)
        assert limited["planned"] == 1
        with db.connect() as conn:
            rows = conn.execute("SELECT dedup_key, payload FROM aec.jobs WHERE payload->>'queue'=%s", (queue,)).fetchall()
        assert len(rows) == 4
        payload = next(r["payload"] for r in rows if r["payload"]["aliases"])
        assert payload["document_id"] == "doc_" + payload["sha256"][:24]
        assert payload["aliases"][0].endswith("copy of plan.dwg")
        assert {r["dedup_key"] for r in rows} <= shas
    finally:
        _cleanup(db, shas=shas, queue=queue)


def _dxf(path: Path, tag: str):
    ezdxf = pytest.importorskip("ezdxf")
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    doc.layers.add("A-WALL")
    msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "A-WALL"})
    msp.add_text(f"거실 {tag}", dxfattribs={"layer": "A-ANNO"}).set_placement((100, 100))
    doc.saveas(path)


@needs_db
def test_run_workers_drains_queue_with_two_processes(tmp_path, db):
    from aec_intelligence.operational.config import Settings

    tag = uuid.uuid4().hex[:10]
    root = tmp_path / "드라이브"
    (root / f"현장{tag}" / "건축").mkdir(parents=True)
    _dxf(root / f"현장{tag}" / "건축" / "1층 평면도.dxf", tag)
    _dxf(root / f"현장{tag}" / "건축" / "2층 평면도.dxf", tag + "2")
    (root / f"현장{tag}" / "건축" / "broken.dxf").write_text("not a dxf " + tag)
    census.run_census([root], tmp_path / "out")
    queue = f"test-workers-{tag}"
    project = f"P-현장{tag}"
    shas = {r["sha256"] for r in _rows(tmp_path / "out")}
    settings = Settings(dsn=DSN, data_root=tmp_path / "data", import_roots=(tmp_path,), lease_seconds=60)
    try:
        assert census.enqueue_census(db, tmp_path / "out", queue=queue, import_roots=[tmp_path])["enqueued"] == 3
        summary = census.run_workers(settings, processes=2, queue=queue, poll=0.5)
        assert summary["succeeded"] == 2 and summary["failed"] == 1
        assert summary["queued"] == summary["running"] == 0
        assert summary["failed_jobs"][0]["source"].endswith("broken.dxf")
        report = census.build_report(db, tmp_path / "out", projects=[project])
        entry = report["projects"][project]
        assert entry["documents"] == 2 and entry["census_unique_files"] == 3
        assert sum(entry["kinds"].values()) > 0
        assert len(report["failed_jobs"]) == 1
        assert project in census.report_markdown(report)
    finally:
        _cleanup(db, projects=[project], shas=shas, queue=queue)


@needs_db
def test_cli_report_and_census_commands(tmp_path, db, monkeypatch, capsys):
    from aec_intelligence.operational import cli

    root = _tree(tmp_path)
    monkeypatch.setenv("AEC_DATABASE_URL", DSN)
    monkeypatch.setenv("AEC_DATA_ROOT", str(tmp_path / "data"))
    cli.main(["census", str(root), "--out", str(tmp_path / "out"), "--extensions", ".dwg"])
    assert json.loads(capsys.readouterr().out)["files"] == 3
    cli.main(["enqueue-census", str(tmp_path / "out"), "--dry-run", "--ignore-import-roots", "--limit", "2"])
    assert json.loads(capsys.readouterr().out)["planned"] == 2
    cli.main(["report", "--out", str(tmp_path / "rep"), "--project", "P-does-not-exist-" + uuid.uuid4().hex])
    assert (tmp_path / "rep" / "report.md").exists() and (tmp_path / "rep" / "report.json").exists()


@needs_db
@pytest.mark.skipif(not (shutil.which("pg_dump") or Path("/usr/lib/postgresql/16/bin/pg_dump").exists()),
                    reason="pg_dump not available")
def test_backup_and_restore_against_live_server(tmp_path, db):
    import psycopg

    result = backup.run_backup(DSN, tmp_path / "backups", keep=1)
    dump = Path(result["backup"])
    assert dump.read_bytes()[:5] == b"PGDMP" and result["kept"] == [str(dump)]
    second = backup.run_backup(DSN, tmp_path / "backups", keep=1,
                               now=datetime.now(timezone.utc) + timedelta(seconds=5))
    assert second["deleted"] == [str(dump)] and not dump.exists()

    # Restore into a throw-away database on the same server.
    scratch = f"aec_restore_{uuid.uuid4().hex[:8]}"
    admin = _with_dbname(DSN, "postgres")
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{scratch}"')
    except Exception as exc:  # pragma: no cover - depends on role privileges
        pytest.skip(f"cannot create scratch database: {exc}")
    target = _with_dbname(DSN, scratch)
    try:
        backup.run_restore(target, second["backup"], yes=True, clean=False)
        with psycopg.connect(target) as conn:
            assert conn.execute("SELECT count(*) FROM aec.schema_migrations").fetchone()[0] >= 1
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
