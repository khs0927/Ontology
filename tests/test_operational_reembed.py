"""reembed CLI wiring, AEC_IMPORT_ROOTS splitting, and (optionally) reindex_embeddings against Postgres."""

import os
from pathlib import Path

import pytest

from aec_intelligence.operational import cli
from aec_intelligence.operational.config import Settings, split_roots


def test_split_roots_semicolon_and_pathsep():
    assert split_roots("/a:/b;/c", ":") == ["/a", "/b", "/c"]
    assert split_roots("/a;;/b ", ":") == ["/a", "/b"]


def test_split_roots_keeps_windows_drive_letters():
    assert split_roots(r"C:\x;G:\내 드라이브", ";") == [r"C:\x", r"G:\내 드라이브"]
    assert split_roots(r"C:\x;D:/y:/z", ":") == [r"C:\x", "D:/y", "/z"]
    assert split_roots(r"C:\x", ":") == [r"C:\x"]


def test_settings_from_env_uses_pathsep(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("AEC_IMPORT_ROOTS", f"{a}{os.pathsep}{b};{tmp_path / 'c'}")
    roots = Settings.from_env().import_roots
    assert roots == (a.resolve(), b.resolve(), (tmp_path / "c").resolve())


def test_reembed_delete_stale_requires_embedding_url(monkeypatch, capsys):
    from aec_intelligence.operational import embeddings

    monkeypatch.delenv("AEC_EMBEDDING_URL", raising=False)
    monkeypatch.setattr(embeddings, "reindex_embeddings", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr(cli, "Database", lambda dsn: object())
    with pytest.raises(SystemExit) as exc:
        cli.main(["reembed", "--delete-stale"])
    assert exc.value.code != 0 and "AEC_EMBEDDING_URL" in capsys.readouterr().err


def test_reembed_cli_passes_options(monkeypatch):
    monkeypatch.setenv("AEC_EMBEDDING_URL", "http://127.0.0.1:9")
    from aec_intelligence.operational import embeddings

    seen = {}

    def fake(db, settings, project_id=None, **kw):
        seen.update(project=project_id, **kw)
        return {"model": "m", "written": 0}

    monkeypatch.setattr(embeddings, "reindex_embeddings", fake)
    monkeypatch.setattr(cli, "Database", lambda dsn: object())
    cli.main(["reembed", "--project", "P-1", "--batch-size", "4", "--dry-run", "--delete-stale"])
    assert seen == {"project": "P-1", "batch_size": 4, "dry_run": True, "delete_stale": True}


DSN = os.getenv("AEC_TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="AEC_TEST_DATABASE_URL not set")
def test_reindex_dry_run_and_delete_stale_postgres():
    pytest.importorskip("psycopg")
    from aec_intelligence.operational import embeddings as emb
    from aec_intelligence.operational.db import Database

    db = Database(DSN)
    db.initialize()
    project = f"P-reembed-{os.getpid()}"
    oid = f"obj_reembed_{os.getpid()}"
    doc = f"doc_reembed_{os.getpid()}"
    with db.connect() as conn:
        conn.execute("DELETE FROM aec.embeddings WHERE object_id=%s", (oid,))
        conn.execute("DELETE FROM aec.objects WHERE id=%s", (oid,))
        conn.execute("DELETE FROM aec.documents WHERE id=%s", (doc,))
    with db.connect() as conn:
        conn.execute("""INSERT INTO aec.documents(id, project_id, source_key, name)
                        VALUES (%s,%s,%s,'reembed.dxf')""", (doc, project, doc))
        conn.execute("""INSERT INTO aec.objects(id, project_id, document_id, revision, kind, label, search_text, payload)
                        VALUES (%s,%s,%s,0,'Door','D1','door D1','{}'::jsonb)""", (oid, project, doc))
        conn.execute("""INSERT INTO aec.embeddings(object_id, model, revision, content_hash, embedding)
                        VALUES (%s,'old-model',0,'x',%s::vector)""",
                     (oid, emb.vector_literal([0.0] * emb.EMBEDDING_DIM)))
    settings = Settings(dsn=DSN, data_root=Path("."), import_roots=())  # no URL -> hash target
    try:
        dry = emb.reindex_embeddings(db, settings, project, dry_run=True)
        assert dry["dry_run"] and dry["pending"] == 1 and dry["stale_by_model"] == {}
        done = emb.reindex_embeddings(db, settings, project, batch_size=2, delete_stale=True)
        assert done["model"] == emb.HASH_MODEL and done["written"] == 1 and done["deleted"] == 1
        with db.connect() as conn:
            models = [r["model"] for r in conn.execute("SELECT model FROM aec.embeddings WHERE object_id=%s", (oid,))]
        assert models == [emb.HASH_MODEL]
    finally:
        with db.connect() as conn:
            conn.execute("DELETE FROM aec.embeddings WHERE object_id=%s", (oid,))
            conn.execute("DELETE FROM aec.objects WHERE id=%s", (oid,))
            conn.execute("DELETE FROM aec.documents WHERE id=%s", (doc,))
