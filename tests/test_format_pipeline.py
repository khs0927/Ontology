import pytest
import json
from pathlib import Path

from aec_intelligence.format_pipeline import SemanticFormatIngestionPipeline
from aec_intelligence.validation_engine import validate_project


FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_ifc_pipeline_preserves_ifc_semantics_and_builds_repository_outputs(tmp_path: Path):
    pytest.importorskip("ifcopenshell", reason="authoritative IFC parsing requires the [bim] extra")
    result = SemanticFormatIngestionPipeline(tmp_path).ingest(FIXTURES / "simple_house.ifc", "P-IFC-PIPE", "IFC House")
    assert result.status == "SUCCESS"
    assert result.source_format == "IFC"
    assert {obj.type for obj in result.snapshot.objects} == {"Project", "Site", "Building", "Storey", "Space", "Wall", "Door", "Window"}
    assert len(result.snapshot.relations) == 4
    assert result.validation and result.validation.status == "SUCCESS"
    assert Path(result.outputs["ontology"]).is_file()
    parse_report = json.loads(Path(result.outputs["parse_report"]).read_text(encoding="utf-8"))
    assert parse_report["counts"]["raw_entity_count"] == 13
    assert parse_report["counts"]["entity_count"] == 8
    assert validate_project(tmp_path, "P-IFC-PIPE").status == "SUCCESS"


def test_ifc_geometry_pipeline_persists_mesh_in_portable_geometry_index(tmp_path: Path):
    pytest.importorskip("ifcopenshell", reason="authoritative IFC parsing requires the [bim] extra")
    result = SemanticFormatIngestionPipeline(tmp_path).ingest(FIXTURES / "simple_house_geometry.ifc", "P-IFC-GEOMETRY-PIPE", "IFC Geometry House")
    assert result.status == "SUCCESS_WITH_WARNINGS"
    geometry_rows = [json.loads(line) for line in Path(result.outputs["geometry_index"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    wall_rows = [row for row in geometry_rows if row["source_id"] == "18usTnQo99cPP$puzZobR7"]
    assert len(wall_rows) == 1
    assert wall_rows[0]["geometry"]["kind"] == "mesh"
    assert wall_rows[0]["properties"]["face_count"] == 12
    assert wall_rows[0]["bbox"]["max_z"] == 3000.0
    table_rows = [json.loads(line) for line in (Path(result.outputs["cair"]).parent / "tables" / "geometry_index.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row for row in table_rows if row["source_entity_id"] == "18usTnQo99cPP$puzZobR7"][0]["geometry_type"] == "mesh"
    assert validate_project(tmp_path, "P-IFC-GEOMETRY-PIPE").status == "SUCCESS"


def test_geojson_pipeline_keeps_geometry_in_index_and_crs_on_objects(tmp_path: Path):
    result = SemanticFormatIngestionPipeline(tmp_path).ingest(FIXTURES / "site.geojson", "P-GIS-PIPE", "GIS House Site")
    assert result.status == "SUCCESS"
    assert result.source_format == "GIS"
    assert {obj.type for obj in result.snapshot.objects} == {"Building", "Parcel"}
    assert all(obj.placement.get("crs") == "EPSG:5179" for obj in result.snapshot.objects)
    assert all(obj.geometry_ref.startswith("gis://feature/") for obj in result.snapshot.objects)
    geometry_rows = [json.loads(line) for line in Path(result.outputs["geometry_index"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(geometry_rows) == 2
    assert geometry_rows[0]["geometry"]["type"] == "Polygon"
    assert validate_project(tmp_path, "P-GIS-PIPE").status == "SUCCESS"
