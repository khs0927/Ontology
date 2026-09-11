from pathlib import Path

from aec_intelligence.compliance import audit_repository
from aec_intelligence.pipeline import DXFIngestionPipeline


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_framework_compliance_gate_passes_after_ingest(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "AEC-COMPLIANCE")
    report = audit_repository(tmp_path)
    assert report.status == "PASS"
    assert report.checks["cair_schema_present"]
    assert report.checks["cair_table_schema_present"]
    assert report.checks["ontology_alignment_present"]
    assert report.checks["ontology_alignment_manifest_present"]
    assert report.checks["portable_tables_present"]
    assert report.checks["application_exchange_manifests_present"]
    assert report.checks["runtime_separate_from_canonical"]
    assert report.checks["object_ids_are_global"]
