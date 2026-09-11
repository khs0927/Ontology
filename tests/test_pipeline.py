from pathlib import Path
import json

from jsonschema import validate

from aec_intelligence.pipeline import DXFIngestionPipeline


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_dxf_pipeline_writes_cair_ontology_preview_and_validation(tmp_path: Path):
    result = DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "AEC-TEST-000001", "Fixture House")
    assert result.validation.status == "SUCCESS"
    assert len(result.snapshot.objects) == 7
    assert {obj.type for obj in result.snapshot.objects} >= {"Wall", "Door", "Window", "Column", "Annotation"}
    assert Path(result.outputs["cair"]).is_file()
    assert Path(result.outputs["ontology"]).read_text(encoding="utf-8").startswith("@prefix aec:")
    assert Path(result.outputs["jsonld"]).is_file()
    assert (tmp_path / "global" / "00_GLOBAL" / "global-relations.jsonl").is_file()
    assert (tmp_path / "global" / "00_GLOBAL" / "global-provenance.jsonl").is_file()
    assert (tmp_path / "global" / "00_GLOBAL" / "global-object-registry.parquet").is_file()
    assert (tmp_path / "global" / "03_KNOWLEDGE_GRAPH" / "nodes.parquet").is_file()
    assert (tmp_path / "global" / "03_KNOWLEDGE_GRAPH" / "global.graphml").is_file()
    assert Path(result.outputs["preview"]).read_text(encoding="utf-8").startswith("<svg")
    report = json.loads(Path(result.outputs["validation"]).read_text(encoding="utf-8"))
    assert report["checks"]["counts"]["entity_count"] == 7
    manifest = json.loads((tmp_path / "projects" / "AEC-TEST-000001" / "00_MANIFEST" / "project-manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["source_artifacts"]) == 1
    assert len(manifest["derived_artifacts"]) >= 21
    assert Path(result.outputs["table_objects_jsonl"]).is_file()
    assert Path(result.outputs["cair_manifest"]).is_file()
    assert Path(result.outputs["table_manifest_json"]).is_file()
    table_root = Path(result.outputs["cair_manifest"]).parent / "tables"
    assert (table_root / "properties.jsonl").is_file()
    assert (table_root / "source-mappings.jsonl").is_file()
    assert (table_root / "application-mappings.jsonl").is_file()
    assert Path(result.outputs["freecad_exchange"]).is_file()
    assert Path(result.outputs["blender_exchange"]).is_file()
    assert Path(result.outputs["qgis_exchange"]).is_file()
    assert Path(result.outputs["project_validation"]).is_file()
    schema = json.loads((Path(__file__).parents[1] / "global" / "schemas" / "cair" / "cair-v0.1.schema.json").read_text(encoding="utf-8"))
    validate(json.loads(Path(result.outputs["cair"]).read_text(encoding="utf-8")), schema)


def test_reingest_does_not_duplicate_artifact_registry_rows(tmp_path: Path):
    pipeline = DXFIngestionPipeline(tmp_path)
    first = pipeline.ingest(FIXTURE, "AEC-TEST-000002")
    second = pipeline.ingest(FIXTURE, "AEC-TEST-000002")
    assert first.source_artifact.artifact_id == second.source_artifact.artifact_id
    manifest = json.loads((tmp_path / "projects" / "AEC-TEST-000002" / "00_MANIFEST" / "project-manifest.json").read_text(encoding="utf-8"))
    assert len(pipeline.store.list("AEC-TEST-000002")) == 1 + len(manifest["derived_artifacts"])
