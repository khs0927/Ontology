from pathlib import Path

import pytest

from aec_intelligence.compliance import audit_repository
from aec_intelligence.pipeline import DXFIngestionPipeline
from aec_intelligence.shacl import shapes_text, validate_turtle, validate_turtle_file


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"

pytest.importorskip("pyshacl")


def test_shapes_resource_is_packaged():
    assert "aec:CAIRObjectShape" in shapes_text()


def test_ingested_dxf_export_conforms_to_cair_shapes(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "AEC-SHACL")
    report = validate_turtle_file(tmp_path / "projects" / "AEC-SHACL" / "04_ONTOLOGY" / "project.ttl")
    assert report.conforms, report.violations
    audit = audit_repository(tmp_path)
    assert audit.checks["ontology_exports_shacl_conformant"]
    assert audit.status == "PASS"


def test_out_of_range_confidence_and_unknown_state_are_violations():
    data = """@prefix aec: <https://example.org/aec#> .
<aec://object/x> a aec:Wall ;
    aec:projectId "P" ;
    aec:sourceFile "a.dxf" ;
    aec:classificationConfidence 1.4 ;
    aec:classificationState "MAYBE" ;
    aec:hasGeometry <aec://geometry/x> .
"""
    report = validate_turtle(data)
    assert not report.conforms
    paths = {row["path"] for row in report.violations}
    assert "https://example.org/aec#classificationConfidence" in paths
    assert "https://example.org/aec#classificationState" in paths


def test_audit_fails_when_project_export_breaks_shapes(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "AEC-SHACL-BAD")
    export = tmp_path / "projects" / "AEC-SHACL-BAD" / "04_ONTOLOGY" / "project.ttl"
    export.write_text(export.read_text(encoding="utf-8").replace("aec:hasGeometry", "aec:hasGeometryTypo"), encoding="utf-8")
    audit = audit_repository(tmp_path)
    assert audit.status == "FAIL"
    assert not audit.checks["ontology_exports_shacl_conformant"]


def test_ifc_project_entity_is_not_mistaken_for_the_snapshot_project(tmp_path: Path):
    pytest.importorskip("ifcopenshell")
    from aec_intelligence.format_pipeline import SemanticFormatIngestionPipeline

    fixture = Path(__file__).parents[1] / "fixtures" / "simple_house.ifc"
    SemanticFormatIngestionPipeline(tmp_path).ingest(fixture, "AEC-SHACL-IFC")
    report = validate_turtle_file(tmp_path / "projects" / "AEC-SHACL-IFC" / "04_ONTOLOGY" / "project.ttl")
    assert report.conforms, report.violations
