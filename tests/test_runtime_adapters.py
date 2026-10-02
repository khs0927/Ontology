from pathlib import Path

from aec_intelligence.cair import CAIRObject, CAIRRelation, CAIRSnapshot, SourceRef
from aec_intelligence.pipeline import DXFIngestionPipeline
from aec_intelligence.runtime_adapters import DuckDBRuntimeAdapter, Neo4jRuntimeAdapter, OxigraphRuntimeAdapter, RDFLibRuntimeAdapter, write_neo4j_cypher


def test_optional_runtime_adapters_report_explicit_state(tmp_path: Path):
    report = DuckDBRuntimeAdapter().materialize(tmp_path)
    assert report.status in {"SUCCESS", "REQUIRES_DEPENDENCY"}
    if report.status == "SUCCESS":
        import duckdb

        columns = {row[0] for row in duckdb.connect(report.target).execute("DESCRIBE artifacts").fetchall()}
        assert "security_classification" in columns
    assert Neo4jRuntimeAdapter().connect().status == "REQUIRES_CONFIGURATION"
    assert Neo4jRuntimeAdapter().ingest_global(tmp_path).status == "REQUIRES_CONFIGURATION"
    assert RDFLibRuntimeAdapter().load_turtle(tmp_path / "missing.ttl").status in {"FAILED", "REQUIRES_DEPENDENCY"}
    assert OxigraphRuntimeAdapter().load_turtle(tmp_path / "missing.ttl").status in {"FAILED", "REQUIRES_DEPENDENCY"}


def test_oxigraph_loads_project_ontology_when_installed(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(Path(__file__).parents[1] / "fixtures" / "simple_house.dxf", "AEC-OXIGRAPH")
    fixture = tmp_path / "projects" / "AEC-OXIGRAPH" / "04_ONTOLOGY" / "project.ttl"
    report = OxigraphRuntimeAdapter().load_turtle(fixture)
    assert report.status in {"SUCCESS", "REQUIRES_DEPENDENCY"}
    if report.status == "SUCCESS":
        assert report.counts["quads"] > 0


def test_neo4j_export_is_runtime_only_and_deterministic(tmp_path: Path):
    snapshot = CAIRSnapshot("P-GRAPH", [CAIRObject("aec://object/1", "P-GRAPH", "Wall", SourceRef("a.dxf", "DXF", "A1")), CAIRObject("aec://object/2", "P-GRAPH", "Door", SourceRef("a.dxf", "DXF", "A2"))], [CAIRRelation("aec://object/1", "containsElement", "aec://object/2")])
    path = write_neo4j_cypher(snapshot, tmp_path / "seed.cypher")
    text = path.read_text(encoding="utf-8")
    assert "runtime-only" in text
    assert "MATCH (s:AECObject" in text
