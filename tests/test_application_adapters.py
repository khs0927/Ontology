import json
import os
from pathlib import Path

import pytest

from aec_intelligence.application_adapters import BlenderApplicationAdapter, FreeCADApplicationAdapter, QGISApplicationAdapter, write_application_exchange_manifest
from aec_intelligence.blender_adapter import BlenderSceneAdapter, _scene_payload
from aec_intelligence.freecad_adapter import FreeCADSceneAdapter
from aec_intelligence.cair import CAIRObject, CAIRSnapshot, SourceRef
from aec_intelligence.pipeline import DXFIngestionPipeline


def test_application_adapters_do_not_guess_uninstalled_software():
    for adapter in (FreeCADApplicationAdapter(), BlenderApplicationAdapter(), QGISApplicationAdapter()):
        status = adapter.probe("C:/path/that/does/not/exist.exe")
        assert status.status == "REQUIRES_CONFIGURATION"


def test_application_exchange_manifest_keeps_geometry_external(tmp_path: Path):
    snapshot = CAIRSnapshot("P-APP", objects=[CAIRObject("aec://object/1", "P-APP", "Wall", SourceRef("a.dxf", "DXF", "A1"), geometry_ref="aec://geometry/1")])
    path = write_application_exchange_manifest(snapshot, "FreeCAD", tmp_path / "freecad-exchange.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["objects"][0]["geometry_ref"] == "aec://geometry/1"
    assert "geometry" not in data["objects"][0]
    assert "raw source geometry" in data["policy"]


def test_blender_scene_payload_covers_all_fixture_geometry(tmp_path: Path):
    fixture = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"
    DXFIngestionPipeline(tmp_path).ingest(fixture, "P-BLENDER-PAYLOAD")
    payload, object_count, geometry_count = _scene_payload(tmp_path / "projects" / "P-BLENDER-PAYLOAD")
    assert object_count == 7
    assert geometry_count == 7
    assert {item["geometry"]["kind"] for item in payload["objects"]} >= {"polyline", "line", "arc", "circle", "text"}


def test_blender_scene_adapter_reports_missing_executable(tmp_path: Path):
    result = BlenderSceneAdapter("C:/path/that/does/not/exist.exe").export_project(tmp_path, "P-MISSING-BLENDER")
    assert result.status == "REQUIRES_CONFIGURATION"


def test_freecad_scene_adapter_reports_missing_executable(tmp_path: Path):
    result = FreeCADSceneAdapter("C:/path/that/does/not/exist.exe").export_project(tmp_path, "P-MISSING-FREECAD")
    assert result.status == "REQUIRES_CONFIGURATION"


def test_qgis_runtime_probe_keeps_missing_configuration_explicit(tmp_path: Path):
    result = QGISApplicationAdapter().probe_runtime(str(tmp_path / "missing-qgis-process.bat"))
    assert result.status == "REQUIRES_CONFIGURATION"
    assert result.executable is None


def test_qgis_runtime_probe_accepts_real_wrapper_and_geopackage_processing(tmp_path: Path):
    executable = os.environ.get("AEC_QGIS_EXECUTABLE")
    if not executable or not Path(executable).is_file():
        pytest.skip("set AEC_QGIS_EXECUTABLE to an explicit qgis_process wrapper for the host acceptance test")
    fixture = Path(__file__).parents[1] / "fixtures" / "site.geojson"
    output = tmp_path / "site-buffer.gpkg"
    result = QGISApplicationAdapter().probe_runtime(executable, str(fixture), str(output), timeout=120)
    assert result.status == "SUCCESS"
    assert result.checks == {"version_command": True, "processing_list": True}
    assert "native:buffer" in result.algorithms
    assert result.processing is not None
    assert result.processing["status"] == "SUCCESS"
    assert result.processing["output_exists"] is True
