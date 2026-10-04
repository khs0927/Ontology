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
                    "chunk_retries": 8, "max_backoff": 300.0, "pause": 0.0, "timeout": None, "hours": None}


def test_reindex_retries_failed_chunk_with_backoff(monkeypatch):
    """A busy endpoint (Ollama shared with the workers) no longer ends a 100k-object run."""
    from aec_intelligence.operational import embeddings as emb

    class Conn:
        def __init__(self):
            self.rows = [{"id": f"o{i}", "document_id": "doc1", "revision": 0,
                          "type": "Door", "search_text": f"door {i}"} for i in range(5)]
            self.written = []
            self.commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            rows = [] if "aec.text_vectors" in sql else self.rows  # no text has a vector yet

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

        def rollback(self):
            pass

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
    state_updates = []
    monkeypatch.setattr(emb, "_refresh_embedding_state",
                        lambda conn, model, **scope: state_updates.append((model, scope)))
    out = emb.reindex_embeddings(DB(), settings, batch_size=1, chunk_retries=3, max_backoff=7, pause=0.5,
                                 sleep=waits.append, on_retry=lambda n, w, e: retries.append((n, w)))
    # chunks of 8 rows -> one chunk of 5; batch_size=1 makes step 8: single chunk retried twice
    assert out["written"] == 5 and out["retried_chunks"] == 2 and out["complete"] and out["error"] is None
    assert retries == [(1, 5.0), (2, 7)] and waits == [5.0, 7]
    assert state_updates == [("bge-m3", {"document_ids": ["doc1"]})]
    calls["n"] = 0
    conn.written.clear()

    def down(self, texts):
        raise emb.EmbeddingEndpointError("connection refused")

    monkeypatch.setattr(emb.EmbeddingService, "embed_with_model", down)
    out = emb.reindex_embeddings(DB(), settings, batch_size=1, chunk_retries=2, sleep=lambda s: None)
    assert out["written"] == 0 and not out["complete"] and "3 failed attempts" in out["error"]
    assert len(state_updates) == 1  # failed chunks never advertise readiness
    with pytest.raises(emb.EmbeddingEndpointError):
        emb.reindex_embeddings(DB(), settings, batch_size=1, sleep=lambda s: None)
    conn.rows.clear()
    recovered = emb.reindex_embeddings(DB(), settings, project_id="P-1")
    assert recovered["complete"] and recovered["written"] == 0
    assert state_updates[-1] == ("bge-m3", {"project_id": "P-1"})
    before = len(state_updates)
    assert emb.reindex_embeddings(DB(), settings, dry_run=True)["dry_run"]
    assert len(state_updates) == before  # read-only preview never repairs metadata


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
        conn.execute("""INSERT INTO aec.text_vectors(model, content_hash, embedding) VALUES ('old-model','x',%s::halfvec)
                        ON CONFLICT DO NOTHING""", (emb.vector_literal([0.5] * emb.EMBEDDING_DIM),))
        conn.execute("""INSERT INTO aec.embeddings(object_id, model, revision, content_hash)
                        VALUES (%s,'old-model',0,'x')""", (oid,))
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
            conn.execute("DELETE FROM aec.text_vectors WHERE content_hash = ANY(%s)",
                         ([emb.text_hash(f"door {oid}") for oid in oids],))
            conn.execute("DELETE FROM aec.objects WHERE id = ANY(%s)", (oids,))
            conn.execute("DELETE FROM aec.documents WHERE id=%s", (doc,))

    cleanup()
    with db.connect() as conn:
        conn.execute("""INSERT INTO aec.documents(id, project_id, source_key, name)
                        VALUES (%s,%s,%s,'chunk.dxf')""", (doc, project, doc))
        for oid in oids:
            conn.execute("""INSERT INTO aec.objects(id, project_id, document_id, revision, kind, label, search_text,
                                                    payload)
                            VALUES (%s,%s,%s,0,'Door','D',%s,'{}'::jsonb)""",
                         (oid, project, doc, f"door {oid}"))  # distinct texts: one vector each
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


@pytest.mark.skipif(not DSN, reason="AEC_TEST_DATABASE_URL not set")
def test_identical_texts_share_one_vector_and_gc_postgres():
    """Storage (0003): N objects with the same text -> N mappings, 1 halfvec; orphaned vectors are collected."""
    pytest.importorskip("psycopg")
    from aec_intelligence.operational import embeddings as emb
    from aec_intelligence.operational.db import Database

    db = Database(DSN)
    db.initialize()
    pid = os.getpid()
    project, doc = f"P-dedup-{pid}", f"doc_dedup_{pid}"
    oids = [f"obj_dedup_{pid}_{i}" for i in range(6)]
    texts = [f"dedup room {pid}"] * 4 + [f"dedup stair {pid}"] * 2
    hashes = sorted({emb.text_hash(t) for t in texts})

    def cleanup():
        with db.connect() as conn:
            conn.execute("DELETE FROM aec.embeddings WHERE object_id = ANY(%s)", (oids,))
            conn.execute("DELETE FROM aec.text_vectors WHERE content_hash = ANY(%s)", (hashes,))
            conn.execute("DELETE FROM aec.objects WHERE id = ANY(%s)", (oids,))
            conn.execute("DELETE FROM aec.documents WHERE id=%s", (doc,))

    cleanup()
    with db.connect() as conn:
        conn.execute("INSERT INTO aec.documents(id, project_id, source_key, name) VALUES (%s,%s,%s,'d.dxf')",
                     (doc, project, doc))
        for oid, text in zip(oids, texts):
            conn.execute("""INSERT INTO aec.objects(id, project_id, document_id, revision, kind, label, search_text,
                                                    payload) VALUES (%s,%s,%s,0,'Space','x',%s,'{}'::jsonb)""",
                         (oid, project, doc, text))
    settings = Settings(dsn=DSN, data_root=Path("."), import_roots=())
    calls = []
    real = emb.EmbeddingService.embed_with_model

    def counting(self, batch):
        calls.append(list(batch))
        return real(self, batch)

    try:
        import pytest as _pytest
        mp = _pytest.MonkeyPatch()
        mp.setattr(emb.EmbeddingService, "embed_with_model", counting)
        try:
            dry = emb.reindex_embeddings(db, settings, project, dry_run=True)
            assert dry["pending"] == 6 and dry["distinct_texts"] == 2 and dry["texts_to_embed"] == 2
            out = emb.reindex_embeddings(db, settings, project)
        finally:
            mp.undo()
        assert out["written"] == 6 and out["embedded_texts"] == 2 and sum(len(c) for c in calls) == 2
        with db.connect() as conn:
            n_vec = conn.execute("SELECT count(*) AS n FROM aec.text_vectors WHERE content_hash = ANY(%s)",
                                 (hashes,)).fetchone()["n"]
            n_map = conn.execute("SELECT count(DISTINCT content_hash) AS h, count(*) AS n FROM aec.embeddings "
                                 "WHERE object_id = ANY(%s)", (oids,)).fetchone()
            typ = conn.execute("SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute "
                               "WHERE attrelid = 'aec.text_vectors'::regclass AND attname = 'embedding'").fetchone()["t"]
        assert n_vec == 2 and n_map["n"] == 6 and n_map["h"] == 2 and typ == "halfvec(1024)"
        # A second ingest of the same texts embeds nothing.
        with db.connect() as conn:
            snap = {"document_id": doc, "revision": 0,
                    "objects": [{"id": o, "type": "Space", "search_text": t} for o, t in zip(oids, texts)]}
            calls.clear()
            mp = _pytest.MonkeyPatch()
            mp.setattr(emb.EmbeddingService, "embed_with_model", counting)
            try:
                assert emb.index_snapshot_embeddings(conn, snap, settings) == 6
            finally:
                mp.undo()
        assert calls == []
        # Orphans: drop the stair objects' mappings -> their vector is collectable (min age 0).
        with db.connect() as conn:
            conn.execute("DELETE FROM aec.embeddings WHERE object_id = ANY(%s)", (oids[4:],))
        assert emb.vectors_gc(db, min_age_seconds=3600)["removed"] == 0  # too young
        preview = emb.vectors_gc(db, min_age_seconds=0, dry_run=True)
        assert preview["removed"] >= 1 and preview["error"] is None  # counted, not deleted
        with db.connect() as conn:
            still_there = conn.execute("SELECT count(*) AS n FROM aec.text_vectors WHERE content_hash = ANY(%s)",
                                       (hashes,)).fetchone()["n"]
        assert still_there == 2  # the dry run touched nothing
        gc = emb.vectors_gc(db, min_age_seconds=0)
        assert gc["removed"] >= 1 and gc["error"] is None
        with db.connect() as conn:
            left = conn.execute("SELECT count(*) AS n FROM aec.text_vectors WHERE content_hash = ANY(%s)",
                                (hashes,)).fetchone()["n"]
        assert left == 1
    finally:
        cleanup()
