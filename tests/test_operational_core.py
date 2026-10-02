"""Comprehensive test suite for operational AEC intelligence components."""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from aec_intelligence.operational.config import Settings
from aec_intelligence.operational.embeddings import (
    EmbeddingService,
    _deterministic_hash_vector,
    EMBEDDING_DIM,
)
from aec_intelligence.operational.parsers import (
    observation,
    relation,
    parse_source,
    PIPELINE_VERSION,
)
from aec_intelligence.operational.search import Citation, SearchHit, SearchResult
from aec_intelligence.operational.api import create_app


FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
SIMPLE_HOUSE_DXF = FIXTURES_DIR / "simple_house.dxf"


def test_settings_configuration(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings = Settings(
        dsn="postgresql://user:pass@localhost:5432/db",
        data_root=data_dir,
        import_roots=(data_dir,),
    )
    assert settings.dsn == "postgresql://user:pass@localhost:5432/db"
    assert settings.data_root == data_dir
    assert settings.lease_seconds == 300
    assert settings.max_attempts == 3

    # Allowed source check
    valid_file = data_dir / "test.dxf"
    valid_file.write_text("dummy", encoding="utf-8")
    assert settings.allowed_source(str(valid_file)) == valid_file.resolve()

    # Out of boundary source check
    outside_file = tmp_path / "outside.dxf"
    outside_file.write_text("dummy", encoding="utf-8")
    with pytest.raises(ValueError):
        settings.allowed_source(str(outside_file))


def test_embedding_service():
    settings = Settings(
        dsn="dummy",
        data_root=Path("."),
        import_roots=(),
        embedding_model="BAAI/bge-m3",
    )
    service = EmbeddingService(settings)
    vec = service.embed_text("외벽 200mm 콘크리트 벽체")

    assert len(vec) == EMBEDDING_DIM
    norm = sum(x * x for x in vec) ** 0.5
    assert pytest.approx(norm, rel=1e-2) == 1.0

    batch = service.embed_batch(["벽체", "출입문", "창호"])
    assert len(batch) == 3
    for v in batch:
        assert len(v) == EMBEDDING_DIM


def test_parsers_observation_and_relation():
    obs = observation("doc1", "handle_10A", "Wall", "콘크리트 벽체", {"handle": "10A"})
    assert obs["type"] == "Wall"
    assert obs["label"] == "콘크리트 벽체"
    assert "Wall" in obs["search_text"]
    assert obs["state"] == "OBSERVED"
    assert obs["id"].startswith("obs_")

    rel = relation("obs_1", "contains", "obs_2")
    assert rel["subject"] == "obs_1"
    assert rel["predicate"] == "contains"
    assert rel["object"] == "obs_2"
    assert rel["id"].startswith("rel_")


def test_parsers_dxf_source(tmp_path: Path):
    if not SIMPLE_HOUSE_DXF.is_file():
        pytest.skip("simple_house.dxf fixture missing")

    data_root = tmp_path / "data"
    output_dir = data_root / "artifacts" / "doc_house" / "rev-0"
    settings = Settings(
        dsn="dummy",
        data_root=data_root,
        import_roots=(FIXTURES_DIR,),
    )

    parsed = parse_source(
        source=SIMPLE_HOUSE_DXF,
        doc="doc_house",
        output=output_dir,
        settings=settings,
        source_name="simple_house.dxf",
    )

    assert parsed["parser_version"] == PIPELINE_VERSION
    assert len(parsed["objects"]) > 0
    assert len(parsed["relations"]) > 0

    types = {o["type"] for o in parsed["objects"]}
    assert "Document" in types
    assert "View" in types
    assert {"Wall", "Door", "Window"} & types

    # Check SVG preview generation
    svg_previews = list(output_dir.glob("layout-*.svg"))
    assert len(svg_previews) >= 1
    assert "<svg" in svg_previews[0].read_text(encoding="utf-8")

    # Check geometry jsonl
    # One file per layout; the fixture's paper-space layout is empty, so
    # check the model-space file (layout 0) rather than whichever globs first.
    geom_files = sorted(output_dir.glob("geometry-*.jsonl"))
    assert len(geom_files) >= 1
    assert (output_dir / "geometry-0.jsonl").stat().st_size > 0


def test_search_data_structures():
    citation = Citation(
        document_id="doc_1",
        document_name="FloorPlan_1F.dxf",
        revision=0,
        layout_or_page="Model",
        handle_or_id="2A3",
        coordinate_system="CAD_WCS",
        bbox=[0.0, 0.0, 1000.0, 2000.0],
    )
    hit = SearchHit(
        object_id="obs_123",
        project_id="P-001",
        kind="Wall",
        label="내력벽",
        discipline="ARCH",
        storey="1F",
        revision=0,
        score=0.89,
        lexical_score=0.8,
        vector_score=0.95,
        citation=citation,
    )
    result = SearchResult(
        query="내력벽",
        total_hits=1,
        hits=[hit],
    )
    data = result.to_dict()
    assert data["total_hits"] == 1
    assert data["hits"][0]["kind"] == "Wall"
    assert data["hits"][0]["citation"]["handle_or_id"] == "2A3"


def test_fastapi_app_endpoints(tmp_path: Path):
    settings = Settings(
        dsn="postgresql://user:pass@localhost:5432/aec",
        data_root=tmp_path / "data",
        import_roots=(tmp_path,),
    )
    app = create_app(settings)
    client = TestClient(app)

    # Health check
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

    # Ingestion invalid path
    res = client.post("/v1/ingestions", json={"path": str(tmp_path / "nonexistent.dxf")})
    assert res.status_code == 400
    assert "not found" in res.json()["detail"].lower()

    # Dashboard endpoint (should return 200 with HTML)
    res = client.get("/")
    assert res.status_code == 200
    assert "AEC" in res.text
