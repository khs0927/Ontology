"""End-to-end check of the operational stack against a real PostgreSQL with AGE, pgvector and PostGIS.

Skipped unless AEC_TEST_DATABASE_URL points at a disposable database (the test writes to it).
"""

import os
import shutil
from pathlib import Path

import pytest

DSN = os.getenv("AEC_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="AEC_TEST_DATABASE_URL not set")

FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


@pytest.fixture()
def stack(tmp_path):
    pytest.importorskip("psycopg")
    pytest.importorskip("ezdxf")
    from aec_intelligence.operational.config import Settings
    from aec_intelligence.operational.db import Database

    imports = tmp_path / "imports"
    imports.mkdir()
    source = imports / FIXTURE.name
    shutil.copy(FIXTURE, source)
    settings = Settings(dsn=DSN, data_root=tmp_path, import_roots=(imports,))
    db = Database(DSN)
    db.initialize()
    return db, settings, source


def test_migrations_are_recorded_and_idempotent(stack):
    db, _, _ = stack
    assert db.initialize() == []
    with db.connect() as conn:
        versions = [r["version"] for r in conn.execute("SELECT version FROM aec.schema_migrations ORDER BY version")]
        extensions = {r["extname"] for r in conn.execute("SELECT extname FROM pg_extension")}
    assert versions and versions[0] == "0001_core"
    assert {"age", "vector", "postgis", "pg_trgm"} <= extensions


def test_dxf_job_lands_in_sql_graph_and_vector_index(stack):
    from aec_intelligence.operational.db import graph_name
    from aec_intelligence.operational.search import SearchRouter
    from aec_intelligence.operational.worker import IngestionWorker

    db, settings, source = stack
    project = f"P-it-{os.getpid()}"
    db.enqueue({"source": str(source), "project_id": project, "document_id": f"doc_it_{os.getpid()}"},
               dedup_key=f"it:{project}")
    assert IngestionWorker(db, settings).run_once()

    with db.connect() as conn:
        job = conn.execute("SELECT state, error FROM aec.jobs WHERE dedup_key=%s", (f"it:{project}",)).fetchone()
        assert job["state"] == "SUCCEEDED", job["error"]
        objects = conn.execute("SELECT count(*) AS n FROM aec.objects WHERE project_id=%s", (project,)).fetchone()["n"]
        embedded = conn.execute("""SELECT count(*) AS n FROM aec.embeddings e JOIN aec.objects o ON o.id=e.object_id
            WHERE o.project_id=%s""", (project,)).fetchone()["n"]
        nodes = db.cypher(conn, graph_name(project), "MATCH (n:Entity) RETURN count(n)")
    assert objects > 0
    assert embedded > 0
    assert int(str(nodes[0]["value"])) == objects

    result = SearchRouter(db, settings).search("wall", project_id=project, top_k=5)
    assert result.hits
