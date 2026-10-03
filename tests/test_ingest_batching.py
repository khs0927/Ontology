"""Large-drawing projection: batched inserts and a per-transaction statement_timeout."""
from contextlib import contextmanager

import pytest

pytest.importorskip("psycopg")

from aec_intelligence.operational import db as dbmod


class _Cursor:
    def __init__(self, log):
        self.log = log

    def executemany(self, query, rows):
        self.log.append(("executemany", str(query), len(list(rows))))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self):
        self.log = []

    def execute(self, query, params=None):
        self.log.append(("execute", query if isinstance(query, str) else query.as_string(None), params))

    def cursor(self):
        return _Cursor(self.log)


def test_project_batches_rows_and_raises_statement_timeout(monkeypatch):
    monkeypatch.setattr(dbmod, "INSERT_BATCH", 1000)
    monkeypatch.delenv("AEC_INGEST_STATEMENT_TIMEOUT", raising=False)
    database = dbmod.Database("postgresql://unused")
    monkeypatch.setattr(database, "project_graph", lambda conn, snapshot: None)
    objects = [{"id": f"o{i}", "type": "Annotation", "label": "x", "search_text": "x",
                "bbox": {"min_x": 0, "min_y": 0, "max_x": 1, "max_y": 1}} for i in range(2500)]
    relations = [{"id": f"r{i}", "subject": "a", "predicate": "contains", "object": "b", "state": "OBSERVED"}
                 for i in range(1200)]
    conn = _Conn()
    database.project(conn, {"document_id": "d", "revision": 1, "project_id": "p", "source_key": "k", "name": "n",
                            "source_hash": "h", "objects": objects, "relations": relations}, "snap.json")
    first = conn.log[0]
    assert first[0] == "execute" and "SET LOCAL statement_timeout" in first[1] and "15min" in first[1]
    many = [entry for entry in conn.log if entry[0] == "executemany"]
    assert [n for _, q, n in many if "aec.objects" in q] == [1000, 1000, 500]
    assert [n for _, q, n in many if "aec.relations" in q] == [1000, 200]
    # No per-row INSERT statements remain.
    assert not [e for e in conn.log if e[0] == "execute" and "INSERT INTO aec.objects" in e[1]]


def test_ingest_statement_timeout_env(monkeypatch):
    monkeypatch.setenv("AEC_INGEST_STATEMENT_TIMEOUT", "5min")
    assert dbmod.ingest_statement_timeout() == "5min"
    monkeypatch.setenv("AEC_INGEST_STATEMENT_TIMEOUT", "1; DROP TABLE x")
    assert dbmod.ingest_statement_timeout() == dbmod.INGEST_STATEMENT_TIMEOUT
