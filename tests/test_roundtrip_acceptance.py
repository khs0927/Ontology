from pathlib import Path

import pytest

from aec_intelligence.freecad_adapter import FreeCADSceneAdapter
from aec_intelligence.pipeline import DXFIngestionPipeline


def test_dwg_derived_cair_freecad_step_preserves_planar_bbox(tmp_path: Path):
    freecad = Path(r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe")
    if not freecad.is_file():
        pytest.skip("FreeCAD command-line runtime is not configured")

    repository = tmp_path / "repository"
    DXFIngestionPipeline(repository).ingest(Path(__file__).parents[1] / "fixtures" / "simple_house.dxf", "P-ROUNDTRIP")
    result = FreeCADSceneAdapter(freecad).export_project(repository, "P-ROUNDTRIP")

    assert result.status == "SUCCESS"
    assert result.checks["step_imported"] is True
    assert result.checks["planar_bbox_match"] is True
    assert result.checks["planar_max_deviation"] <= 0.001
    assert result.checks["step_solids"] == 0  # explicit CAIR wireframe policy

