import json
from pathlib import Path

from aec_intelligence.dashboard import build_dashboard_data, write_dashboard
from aec_intelligence.pipeline import DXFIngestionPipeline


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_dashboard_is_source_backed_and_rebuildable(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "P-DASH", "Dashboard fixture")
    data = build_dashboard_data(tmp_path)
    assert data["metrics"]["project_count"] == 1
    assert data["metrics"]["object_count"] == 7
    outputs = write_dashboard(tmp_path)
    assert outputs["html"].is_file()
    assert outputs["data"].is_file()
    assert "Dashboard fixture" in outputs["html"].read_text(encoding="utf-8")
    assert json.loads(outputs["data"].read_text(encoding="utf-8"))["status"] == "SUCCESS"
