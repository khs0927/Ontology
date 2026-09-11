from pathlib import Path

import pytest

from aec_intelligence.blender_adapter import BlenderSceneAdapter
from aec_intelligence.format_pipeline import SemanticFormatIngestionPipeline
from aec_intelligence.freecad_adapter import FreeCADSceneAdapter, probe_native_ifc


FREECAD = Path(r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe")
BLENDER = Path(r"C:\Users\USER\Documents\ChatGPT\온톨로지\runtime\external\blender-5.2.0\blender.exe")


@pytest.mark.skipif(
    not FREECAD.is_file() or not BLENDER.is_file(),
    reason="FreeCAD and Blender runtimes are not both configured",
)
def test_ifc_geometry_flows_to_freecad_and_blender_review_outputs(tmp_path: Path):
    result = SemanticFormatIngestionPipeline(tmp_path).ingest(
        Path(__file__).parents[1] / "fixtures" / "simple_house_geometry.ifc",
        "P-IFC-GEOMETRY-APPS",
        "IFC Geometry Application House",
    )
    assert result.status == "SUCCESS_WITH_WARNINGS"

    freecad = FreeCADSceneAdapter(FREECAD).export_project(tmp_path, "P-IFC-GEOMETRY-APPS")
    assert freecad.status == "SUCCESS"
    assert freecad.geometry_count == 1
    assert freecad.checks["created_objects"] == 1
    assert freecad.checks["step_imported"] is True
    assert freecad.checks["step_edges"] > 0
    assert freecad.checks["step_solids"] == 0
    assert freecad.checks["planar_bbox_match"] is True

    blender = BlenderSceneAdapter(BLENDER).export_project(tmp_path, "P-IFC-GEOMETRY-APPS")
    assert blender.status == "SUCCESS"
    assert blender.geometry_count == 1
    assert blender.checks["created_objects"] == 1
    assert Path(blender.blend_path).is_file()
    assert Path(blender.glb_path).is_file()
    assert Path(blender.preview_path).is_file()


@pytest.mark.skipif(
    not FREECAD.is_file(),
    reason="FreeCAD command-line runtime is not configured",
)
def test_native_ifc_probe_requires_non_null_freecad_shape(tmp_path: Path):
    source = Path(__file__).parents[1] / "fixtures" / "simple_house_geometry.ifc"
    result = probe_native_ifc(source, FREECAD, tmp_path)

    assert result.status == "SUCCESS"
    assert result.non_null_shape_count >= 1
    assert result.solid_object_count >= 1
    assert any(item["label"] == "Wall" and item["is_null"] is False for item in result.objects)
    assert result.checks["authoritative_source_unchanged"] is True
    assert result.checks["authoritative_source_sha256_before"] == result.checks["authoritative_source_sha256_after"]
    assert Path(result.checks["report_path"]).is_file()


@pytest.mark.skipif(
    not FREECAD.is_file(),
    reason="FreeCAD command-line runtime is not configured",
)
def test_native_ifc_probe_rejects_semantic_only_ifc(tmp_path: Path):
    source = Path(__file__).parents[1] / "fixtures" / "simple_house.ifc"
    result = probe_native_ifc(source, FREECAD, tmp_path)

    assert result.status == "FAILED"
    assert result.non_null_shape_count == 0
    assert any("no non-null shape" in error for error in result.errors)
    assert result.checks["authoritative_source_unchanged"] is True
