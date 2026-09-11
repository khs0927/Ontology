from pathlib import Path

import pytest

from aec_intelligence.freecad_adapter import FreeCADSceneAdapter
from aec_intelligence.mcp_gateway import MCPGateway
from aec_intelligence.pipeline import DXFIngestionPipeline


@pytest.mark.skipif(
    not Path(r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe").is_file(),
    reason="FreeCAD command-line runtime is not configured",
)
def test_optional_solid_mode_extrudes_closed_cair_polylines(tmp_path: Path):
    freecad = Path(r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe")
    fixture = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"
    repository = tmp_path / "repository"
    DXFIngestionPipeline(repository).ingest(fixture, "P-SOLID")

    result = FreeCADSceneAdapter(freecad).export_project(repository, "P-SOLID", solid_mode=True)

    assert result.status == "SUCCESS"
    assert result.solid_geometry_count == 2
    assert result.checks["solid_mode"] is True
    assert result.checks["solid_candidate_count"] == 2
    assert result.checks["solid_geometry_count"] == 2
    assert result.checks["step_solids"] >= 2
    assert result.checks["step_imported"] is True
    assert result.checks["planar_bbox_match"] is True
    assert result.checks["planar_max_deviation"] <= 0.001
    assert "-solid" in str(result.fcstd_path)
    assert "-solid" in str(result.step_path)

    reused = FreeCADSceneAdapter(freecad).export_project(repository, "P-SOLID", solid_mode=True)
    assert reused.status == "SUCCESS"
    assert reused.checks["reused"] is True
    assert reused.solid_geometry_count == 2


@pytest.mark.skipif(
    not Path(r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe").is_file(),
    reason="FreeCAD command-line runtime is not configured",
)
def test_mcp_freecad_solid_mode_is_explicit(tmp_path: Path):
    freecad = Path(r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe")
    fixture = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"
    repository = tmp_path / "repository"
    DXFIngestionPipeline(repository).ingest(fixture, "P-MCP-SOLID")

    result = MCPGateway(repository).call_tool(
        "aec.open_in_freecad",
        {
            "project_id": "P-MCP-SOLID",
            "executable": str(freecad),
            "export_scene": True,
            "solid_mode": True,
        },
    )

    assert result["status"] == "AVAILABLE"
    assert result["scene"]["status"] == "SUCCESS"
    assert result["scene"]["solid_geometry_count"] == 2
    assert result["scene"]["checks"]["solid_mode"] is True


def test_mcp_freecad_schema_exposes_opt_in_solid_mode(tmp_path: Path):
    definition = next(item for item in MCPGateway(tmp_path).list_tools() if item["name"] == "aec.open_in_freecad")
    assert definition["inputSchema"]["properties"]["solid_mode"] == {"type": "boolean"}
