"""Document readiness must follow current vector coverage, including interrupted catch-up runs."""

import json
import re
import sqlite3
from contextlib import nullcontext
from pathlib import Path

from aec_intelligence.operational import embeddings
from aec_intelligence.operational.config import Settings
from aec_intelligence.operational.embeddings import _refresh_embedding_state


class SQLiteConnection:
    """Exercise the actual guarded UPDATE offline with PostgreSQL bind/array syntax adapted to SQLite."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=None):
        sql = sql.replace("UPDATE aec.index_state s", "UPDATE aec.index_state AS s")
        sql = re.sub(r"=\s*ANY\((%\(\w+\)s|%s)\)", r" IN (SELECT value FROM json_each(\1))", sql)
        sql = sql.replace("::text[]", "").replace("::text", "").replace("::vector", "").replace("::halfvec", "")
        if isinstance(params, dict):
            sql = re.sub(r"%\((\w+)\)s", r":\1", sql)
            params = {k: json.dumps(v) if isinstance(v, list) else v for k, v in params.items()}
        else:
            sql = sql.replace("%s", "?")
            params = tuple(json.dumps(v) if isinstance(v, list) else v for v in params or ())
        return self.raw.execute(sql, params)

    def cursor(self):
        return nullcontext(self)

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()


def test_metadata_advanced_only_for_complete_current_document_vectors():
    raw = sqlite3.connect(":memory:")
    raw.execute("ATTACH DATABASE ':memory:' AS aec")
    raw.executescript("""
        CREATE TABLE aec.documents(id TEXT PRIMARY KEY, project_id TEXT, revision INT);
        CREATE TABLE aec.objects(id TEXT PRIMARY KEY, document_id TEXT, kind TEXT, search_text TEXT, revision INT);
        CREATE TABLE aec.embeddings(object_id TEXT, model TEXT, revision INT);
        CREATE TABLE aec.index_state(document_id TEXT PRIMARY KEY, embedding_model TEXT, embedding_revision INT);
    """)
    ids = ["complete", "partial", "stale", "empty", "other"]
    raw.executemany("INSERT INTO aec.documents VALUES (?, ?, 2)", [(i, "Q" if i == "other" else "P") for i in ids])
    raw.executemany("INSERT INTO aec.index_state VALUES (?, 'hash-sha256-1024-v1', 0)", [(i,) for i in ids])
    raw.executemany("INSERT INTO aec.objects VALUES (?, ?, 'Door', 'door', 2)",
                    [("c1", "complete"), ("p1", "partial"), ("p2", "partial"),
                     ("s1", "stale"), ("q1", "other")])
    raw.executemany("INSERT INTO aec.embeddings VALUES (?, 'bge-m3', ?)",
                    [("c1", 2), ("p1", 2), ("s1", 1), ("q1", 2)])
    conn = SQLiteConnection(raw)
    try:
        _refresh_embedding_state(conn, "bge-m3", project_id="P")
        states = dict(raw.execute("SELECT document_id, embedding_model FROM aec.index_state"))
        assert states["complete"] == "bge-m3"
        assert all(states[i] == "hash-sha256-1024-v1" for i in ids if i != "complete")
        assert raw.execute("SELECT embedding_revision FROM aec.index_state WHERE document_id='complete'").fetchone() == (2,)
        # Finish only the last missing chunk; repair the stale revision separately.
        raw.execute("INSERT INTO aec.embeddings VALUES ('p2', 'bge-m3', 2)")
        raw.execute("UPDATE aec.embeddings SET revision=2 WHERE object_id='s1'")
        _refresh_embedding_state(conn, "bge-m3", document_ids=["partial"])
        states = dict(raw.execute("SELECT document_id, embedding_model FROM aec.index_state"))
        assert states["partial"] == "bge-m3" and states["stale"] == "hash-sha256-1024-v1"
        _refresh_embedding_state(conn, "bge-m3", project_id="P")
        states = dict(raw.execute("SELECT document_id, embedding_model FROM aec.index_state"))
        assert states["stale"] == "bge-m3" and states["other"] == "hash-sha256-1024-v1"
    finally:
        raw.close()


def test_reembed_replaces_old_parser_revision_and_repairs_metadata(monkeypatch):
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.execute("ATTACH DATABASE ':memory:' AS aec")
    raw.executescript("""
        CREATE TABLE aec.documents(id TEXT PRIMARY KEY, project_id TEXT, revision INT);
        CREATE TABLE aec.objects(id TEXT PRIMARY KEY, document_id TEXT, project_id TEXT,
                                 kind TEXT, search_text TEXT, revision INT);
        CREATE TABLE aec.embeddings(object_id TEXT, model TEXT, revision INT, content_hash TEXT,
                                    PRIMARY KEY(object_id, model));
        CREATE TABLE aec.text_vectors(model TEXT, content_hash TEXT, embedding TEXT,
                                      PRIMARY KEY(model, content_hash));
        CREATE TABLE aec.index_state(document_id TEXT PRIMARY KEY, embedding_model TEXT, embedding_revision INT);
        INSERT INTO aec.documents VALUES ('d', 'P', 2);
        INSERT INTO aec.objects VALUES ('o', 'd', 'P', 'Door', 'changed door', 2);
        INSERT INTO aec.text_vectors VALUES ('bge-m3', 'old', '[]');
        INSERT INTO aec.text_vectors VALUES ('hash-sha256-1024-v1', 'old', '[]');
        INSERT INTO aec.embeddings VALUES ('o', 'bge-m3', 1, 'old');
        INSERT INTO aec.embeddings VALUES ('o', 'hash-sha256-1024-v1', 1, 'old');
        INSERT INTO aec.index_state VALUES ('d', 'hash-sha256-1024-v1', 1);
    """)
    calls = []

    def embed(self, texts):
        calls.extend(texts)
        return "bge-m3", [[1.0] + [0.0] * 1023 for _ in texts]

    class DB:
        def connect(self):
            return nullcontext(SQLiteConnection(raw))

    monkeypatch.setattr(embeddings.EmbeddingService, "embed_with_model", embed)
    settings = Settings(dsn="unused", data_root=Path("."), import_roots=(),
                        embedding_url="http://127.0.0.1:9", embedding_model="bge-m3")
    try:
        out = embeddings.reindex_embeddings(DB(), settings)
        assert out["written"] == 1 and out["complete"] and calls == ["changed door"]
        state = raw.execute("SELECT * FROM aec.index_state").fetchone()
        assert state["embedding_model"] == "bge-m3" and state["embedding_revision"] == 2
        vectors = raw.execute("SELECT model, revision FROM aec.embeddings").fetchall()
        assert [(v["model"], v["revision"]) for v in vectors] == [("bge-m3", 2)]
    finally:
        raw.close()


def test_reembed_reconciles_documents_it_did_not_process(monkeypatch):
    """A productive run must still reconcile the documents it never touched.

    Live data (36 of 547 documents) showed documents whose objects were embedded by an earlier run
    left carrying the offline ``hash-sha256-1024-v1`` marker: they never appear in a chunk, and the
    project-wide refresh used to run only when a whole run found nothing pending.
    """
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.execute("ATTACH DATABASE ':memory:' AS aec")
    raw.executescript("""
        CREATE TABLE aec.documents(id TEXT PRIMARY KEY, project_id TEXT, revision INT);
        CREATE TABLE aec.objects(id TEXT PRIMARY KEY, document_id TEXT, project_id TEXT,
                                 kind TEXT, search_text TEXT, revision INT);
        CREATE TABLE aec.embeddings(object_id TEXT, model TEXT, revision INT, content_hash TEXT,
                                    PRIMARY KEY(object_id, model));
        CREATE TABLE aec.text_vectors(model TEXT, content_hash TEXT, embedding TEXT,
                                      PRIMARY KEY(model, content_hash));
        CREATE TABLE aec.index_state(document_id TEXT PRIMARY KEY, embedding_model TEXT, embedding_revision INT);
        INSERT INTO aec.documents VALUES ('done', 'P', 1);
        INSERT INTO aec.objects VALUES ('o1', 'done', 'P', 'Door', 'embedded earlier', 1);
        INSERT INTO aec.embeddings VALUES ('o1', 'bge-m3', 1, 'h1');
        INSERT INTO aec.text_vectors VALUES ('bge-m3', 'h1', '[]');
        INSERT INTO aec.index_state VALUES ('done', 'hash-sha256-1024-v1', 1);
        INSERT INTO aec.documents VALUES ('todo', 'P', 1);
        INSERT INTO aec.objects VALUES ('o2', 'todo', 'P', 'Window', 'needs a vector', 1);
        INSERT INTO aec.index_state VALUES ('todo', 'hash-sha256-1024-v1', 0);
    """)

    def embed(self, texts):
        return "bge-m3", [[1.0] + [0.0] * 1023 for _ in texts]

    class DB:
        def connect(self):
            return nullcontext(SQLiteConnection(raw))

    monkeypatch.setattr(embeddings.EmbeddingService, "embed_with_model", embed)
    settings = Settings(dsn="unused", data_root=Path("."), import_roots=(),
                        embedding_url="http://127.0.0.1:9", embedding_model="bge-m3")
    try:
        out = embeddings.reindex_embeddings(DB(), settings)
        assert out["pending"] == 1 and out["written"] == 1 and out["complete"]
        states = dict(raw.execute("SELECT document_id, embedding_model FROM aec.index_state"))
        assert states == {"done": "bge-m3", "todo": "bge-m3"}
    finally:
        raw.close()
