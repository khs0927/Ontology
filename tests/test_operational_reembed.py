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
    assert callable(seen.pop("progress")) and callable(seen.pop("on_retry"))
    assert seen == {"project": "P-1", "batch_size": 4, "dry_run": True, "delete_stale": True,
                    "chunk_retries": 8, "max_backoff": 300.0, "pause": 0.0, "timeout": None}


def test_reindex_retries_failed_chunk_with_backoff(monkeypatch):
    """A busy endpoint (Ollama shared with the workers) no longer ends a 100k-object run."""
    from aec_intelligence.operational import embeddings as emb

    class Conn:
        def __init__(self):
            self.rows = [{"id": f"o{i}", "revision": 0, "type": "Door", "search_text": f"door {i}"} for i in range(5)]
            self.written = []
            self.commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            rows = self.rows

            class R:
                def fetchall(self_inner):
                    return rows
            return R()

        def cursor(self):
            conn = self

            class Cur:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def executemany(self_inner, sql, seq):
                    conn.written.extend(seq)

                def execute(self_inner, sql, params=None):
                    pass
            return Cur()

        def commit(self):
            self.commits += 1

    conn = Conn()

    class DB:
        def connect(self):
            return conn

    settings = Settings(dsn="x", data_root=Path("."), import_roots=(), embedding_url="http://127.0.0.1:9",
                        embedding_model="bge-m3")
    calls = {"n": 0}

    def flaky(self, texts):
        calls["n"] += 1
        if calls["n"] in (1, 2):
            raise emb.EmbeddingEndpointError("timed out")
        return "bge-m3", [[0.0] * emb.EMBEDDING_DIM for _ in texts]

    monkeypatch.setattr(emb.EmbeddingService, "embed_with_model", flaky)
    waits, retries = [], []
    out = emb.reindex_embeddings(DB(), settings, batch_size=1, chunk_retries=3, max_backoff=7, pause=0.5,
                                 sleep=waits.append, on_retry=lambda n, w, e: retries.append((n, w)))
    # chunks of 8 rows -> one chunk of 5; batch_size=1 makes step 8: single chunk retried twice
    assert out["written"] == 5 and out["retried_chunks"] == 2 and out["complete"] and out["error"] is None
    assert retries == [(1, 5.0), (2, 7)] and waits == [5.0, 7]
    calls["n"] = 0
    conn.written.clear()

    def down(self, texts):
        raise emb.EmbeddingEndpointError("connection refused")

    monkeypatch.setattr(emb.EmbeddingService, "embed_with_model", down)
    out = emb.reindex_embeddings(DB(), settings, batch_size=1, chunk_retries=2, sleep=lambda s: None)
    assert out["written"] == 0 and not out["complete"] and "3 failed attempts" in out["error"]
    with pytest.raises(emb.EmbeddingEndpointError):
        emb.reindex_embeddings(DB(), settings, batch_size=1, sleep=lambda s: None)


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


@pytest.mark.skipif(not DSN, reason="AEC_TEST_DATABASE_URL not set")
def test_reindex_commits_each_chunk_postgres(monkeypatch):
    """An interrupted reembed keeps the chunks it already wrote, so a re-run only does the rest."""
    pytest.importorskip("psycopg")
    from aec_intelligence.operational import embeddings as emb
    from aec_intelligence.operational.db import Database

    db = Database(DSN)
    db.initialize()
    project = f"P-reembed-chunk-{os.getpid()}"
    doc = f"doc_reembed_chunk_{os.getpid()}"
    oids = [f"obj_reembed_chunk_{os.getpid()}_{i:02d}" for i in range(10)]

    def cleanup():
        with db.connect() as conn:
            conn.execute("DELETE FROM aec.embeddings WHERE object_id = ANY(%s)", (oids,))
            conn.execute("DELETE FROM aec.objects WHERE id = ANY(%s)", (oids,))
            conn.execute("DELETE FROM aec.documents WHERE id=%s", (doc,))

    cleanup()
    with db.connect() as conn:
        conn.execute("""INSERT INTO aec.documents(id, project_id, source_key, name)
                        VALUES (%s,%s,%s,'chunk.dxf')""", (doc, project, doc))
        for oid in oids:
            conn.execute("""INSERT INTO aec.objects(id, project_id, document_id, revision, kind, label, search_text,
                                                    payload)
                            VALUES (%s,%s,%s,0,'Door','D','door','{}'::jsonb)""", (oid, project, doc))
    settings = Settings(dsn=DSN, data_root=Path("."), import_roots=())
    real = emb.EmbeddingService.embed_with_model
    calls = {"n": 0}

    def flaky(self, texts):
        calls["n"] += 1
        if calls["n"] == 2:
            raise emb.EmbeddingEndpointError("boom")
        return real(self, texts)

    seen = []
    try:
        monkeypatch.setattr(emb.EmbeddingService, "embed_with_model", flaky)
        with pytest.raises(emb.EmbeddingEndpointError):
            emb.reindex_embeddings(db, settings, project, batch_size=1, progress=lambda d, t: seen.append((d, t)))
        assert seen == [(8, 10)]
        with db.connect() as conn:
            kept = conn.execute("SELECT count(*) AS n FROM aec.embeddings WHERE object_id = ANY(%s)", (oids,)).fetchone()
        assert kept["n"] == 8  # the first chunk was committed before the failure
        monkeypatch.setattr(emb.EmbeddingService, "embed_with_model", real)
        rest = emb.reindex_embeddings(db, settings, project, batch_size=1)
        assert rest["pending"] == 2 and rest["written"] == 2
    finally:
        cleanup()
