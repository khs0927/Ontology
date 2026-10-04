"""Exercise the stats endpoint's real count SQL with stale, missing and current vector revisions."""

import sqlite3
from contextlib import nullcontext

from fastapi.testclient import TestClient

from aec_intelligence.operational import api
from aec_intelligence.operational.config import Settings


def test_pending_embedding_stats_use_active_model_and_current_revision(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE documents (id TEXT);
        CREATE TABLE objects (id TEXT, kind TEXT, search_text TEXT, revision INTEGER);
        CREATE TABLE relations (id TEXT);
        CREATE TABLE embeddings (object_id TEXT, model TEXT, revision INTEGER);
        CREATE TABLE text_vectors (model TEXT, content_hash TEXT);
        CREATE TABLE jobs (state TEXT);
        INSERT INTO objects VALUES
            ('current','Space','room',2), ('stale','Space','room',2), ('missing','Space','room',0),
            ('other-model','Space','room',0), ('entity','CADEntity','line',0), ('empty','Space','',0);
        INSERT INTO embeddings VALUES
            ('current','bge-m3',2), ('stale','bge-m3',1), ('other-model','hash-embed-v1',0);
    """)

    class LocalConnection:
        def execute(self, sql, params=()):
            return conn.execute(sql.replace("aec.", "").replace("%s", "?"), params)

    class LocalDatabase:
        def __init__(self, dsn):
            pass

        def connect(self):
            return nullcontext(LocalConnection())

    class OfflineEmbedding:
        def __init__(self, settings):
            pass

        def active_model(self):
            return "bge-m3"

    monkeypatch.setattr(api, "Database", LocalDatabase)
    monkeypatch.setattr(api, "EmbeddingService", OfflineEmbedding)
    monkeypatch.delenv("AEC_API_TOKEN", raising=False)
    monkeypatch.delenv("AEC_CORS_ORIGINS", raising=False)
    settings = Settings(dsn="unused", data_root=tmp_path, import_roots=(tmp_path,))
    with TestClient(api.create_app(settings)) as client:
        response = client.get("/v1/stats")
        assert response.status_code == 200
        assert response.json()["embeddings_pending"] == 3
        conn.execute("UPDATE embeddings SET revision=2 WHERE object_id='stale'")
        conn.execute("INSERT INTO embeddings VALUES ('missing','bge-m3',0), ('other-model','bge-m3',0)")
        assert client.get("/v1/stats").json()["embeddings_pending"] == 0
    conn.close()
