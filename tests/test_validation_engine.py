from pathlib import Path

from aec_intelligence.pipeline import DXFIngestionPipeline
from aec_intelligence.validation_engine import validate_project, validate_repository, write_validation_report


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_validation_engine_checks_project_and_repository(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "P-VALIDATION")
    project_report = validate_project(tmp_path, "P-VALIDATION")
    repository_report = validate_repository(tmp_path)
    assert project_report.status == "SUCCESS"
    assert project_report.checks["parse_object_count_match"] is True
    assert repository_report.status == "SUCCESS"
    output = write_validation_report(project_report, tmp_path / "projects" / "P-VALIDATION" / "11_VALIDATION" / "cross-format" / "project-validation.json")
    assert output.is_file()
