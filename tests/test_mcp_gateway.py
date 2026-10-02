import pytest
from pathlib import Path
import shutil

from aec_intelligence.mcp_gateway import MCPGateway
from aec_intelligence.pipeline import DXFIngestionPipeline


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_mcp_gateway_exposes_framework_tools_and_explicit_errors(tmp_path: Path):
    gateway = MCPGateway(tmp_path)
    names = {tool["name"] for tool in gateway.list_tools()}
    assert {"aec.audit", "aec.ingest_dxf", "aec.ingest_project", "aec.ingest_file", "aec.parse_cad", "aec.parse_ifc", "aec.parse_gis", "aec.build_cair", "aec.classify_objects", "aec.build_ontology", "aec.build_project_graph", "aec.query_project", "aec.rebuild_runtime", "aec.get_object", "aec.query_global_memory", "aec.find_similar_projects", "aec.validate_project", "aec.validate_repository", "aec.build_dashboard", "aec.register_drive_repository", "aec.rebuild_global_memory_from_drive", "aec.refresh_derived_exports", "aec.probe_native_ifc", "aec.probe_qgis_runtime", "aec.context_inspect_code", "aec.graph_backend_plan", "aec.graph_hydradb_preview", "aec.graph_hydradb_export"} <= names
    assert gateway.call_tool("unknown", {})["status"] == "FAILED"
    assert gateway.call_tool("aec.query_plan", {"question": "인접한 출입문"})["route"] == "KNOWLEDGE_GRAPH"


def test_mcp_gateway_uses_cair_and_rebuilds_runtime(tmp_path: Path):
    gateway = MCPGateway(tmp_path)
    result = gateway.call_tool("aec.ingest_dxf", {"source": str(FIXTURE), "project_id": "P-MCP"})
    assert result["status"] == "SUCCESS"
    object_id = next(iter(__import__("json").loads(line)["id"] for line in (tmp_path / "global" / "00_GLOBAL" / "global-object-registry.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()))
    assert gateway.call_tool("aec.get_object", {"object_id": object_id})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.validate_project", {"project_id": "P-MCP"})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.validate_repository", {})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.query_global_memory", {"question": "Wall", "top_k": 1})["hits"]
    assert gateway.call_tool("aec.find_similar_projects", {"project_id": "P-MCP"})["hits"] == []
    assert gateway.call_tool("aec.build_memory_packages", {})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.refresh_derived_exports", {"project_id": "P-MCP"})["status"] == "SUCCESS"
    dashboard = gateway.call_tool("aec.build_dashboard", {})
    assert dashboard["status"] == "SUCCESS"
    assert Path(dashboard["outputs"]["html"]).is_file()
    iteration = gateway.call_tool("aec.create_design_iteration", {"project_id": "P-MCP", "reason": "Imported", "validation_status": "SUCCESS"})
    assert iteration["iteration"]["iteration_id"] == "V001"
    promoted = gateway.call_tool("aec.promote_iteration", {"project_id": "P-MCP", "iteration_id": "V001", "approved_by": "test"})
    assert promoted["status"] == "SUCCESS"
    assert promoted["global_rebuild"]["status"] == "SUCCESS"
    assert promoted["runtime"]["status"] == "SUCCESS"
    global_context = __import__("json").loads((tmp_path / "global" / "09_AGENT_MEMORY" / "global-context.json").read_text(encoding="utf-8"))
    assert global_context["projects"][0]["latest_iteration"] == "V001"
    assert Path(promoted["memory_outputs"]["vector_index_jsonl"]).is_file()
    assert (tmp_path / "global" / "07_DESIGN_KNOWLEDGE" / "lessons" / "lessons.jsonl").read_text(encoding="utf-8").strip()
    assert gateway.call_tool("aec.rebuild_runtime", {"target": str(tmp_path / "runtime" / "mcp.sqlite3")})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.audit", {})["status"] == "PASS"


def test_mcp_gateway_dispatches_authoritative_ifc_gis_and_high_level_boundaries(tmp_path: Path):
    pytest.importorskip("ifcopenshell", reason="authoritative IFC parsing requires the [bim] extra")
    gateway = MCPGateway(tmp_path)
    fixture_root = Path(__file__).parents[1] / "fixtures"
    ifc = gateway.call_tool("aec.ingest_file", {"source": str(fixture_root / "simple_house.ifc"), "project_id": "P-HIGH-IFC"})
    gis = gateway.call_tool("aec.ingest_file", {"source": str(fixture_root / "site.geojson"), "project_id": "P-HIGH-GIS"})
    assert ifc["status"] == "SUCCESS"
    assert ifc["source_format"] == "IFC"
    assert ifc["objects"] == 8
    assert gis["status"] == "SUCCESS"
    assert gis["source_format"] == "GIS"
    assert gis["objects"] == 2
    assert gateway.call_tool("aec.parse_cad", {"source": str(FIXTURE)})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.parse_ifc", {"source": str(fixture_root / "simple_house.ifc")})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.parse_gis", {"source": str(fixture_root / "site.geojson")})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.query_project", {"project_id": "P-HIGH-IFC", "question": "Wall", "top_k": 1})["hits"]
    assert gateway.call_tool("aec.build_ontology", {"project_id": "P-HIGH-IFC"})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.build_project_graph", {"project_id": "P-HIGH-IFC"})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.find_project_artifacts", {"project_id": "P-HIGH-IFC"})["artifacts"]
    assert gateway.call_tool("aec.publish_cair_snapshot", {"project_id": "P-HIGH-IFC"})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.publish_graph_snapshot", {"project_id": "P-HIGH-IFC"})["status"] == "SUCCESS"
    assert gateway.call_tool("aec.register_drive_repository", {"root_folder_id": "drive-root-test"})["status"] == "CONFIGURED"
    assert gateway.call_tool("aec.upload_project_source", {"project_id": "P-HIGH-IFC", "source": "model.ifc"})["status"] == "REQUIRES_CONFIGURATION"


def test_mcp_gateway_exposes_native_ifc_and_qgis_configuration_boundaries(tmp_path: Path):
    gateway = MCPGateway(tmp_path)
    missing = gateway.call_tool("aec.probe_native_ifc", {"source": str(tmp_path / "missing.ifc")})
    assert missing["status"] == "FAILED"
    assert "IFC source was not found" in missing["errors"][0]
    qgis = gateway.call_tool("aec.open_in_qgis", {"executable": str(tmp_path / "qgis_process.exe")})
    assert qgis["status"] == "REQUIRES_CONFIGURATION"
    runtime = gateway.call_tool("aec.probe_qgis_runtime", {"executable": str(tmp_path / "qgis_process.exe")})
    assert runtime["status"] == "REQUIRES_CONFIGURATION"


def test_mcp_gateway_runs_dwg_through_oda_and_full_cair_pipeline(tmp_path: Path, monkeypatch):
    import aec_intelligence.mcp_gateway as gateway_module
    from aec_intelligence.dwg import DWGConversionResult

    source = tmp_path / "simple_house.dwg"
    source.write_bytes(b"DWG fixture placeholder")
    dxf_fixture = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"

    class FakeODA:
        def __init__(self, executable=None):
            self.executable = executable

        def convert_to_dxf(self, source_path, output_dir):
            output = Path(output_dir) / "simple_house.dxf"
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dxf_fixture, output)
            return DWGConversionResult("SUCCESS", str(source_path), str(output), "ODA File Converter", [], {"returncode": 0, "output_size": output.stat().st_size})

    selected = {}

    def fake_select(mode="auto", oda_executable=None, libredwg_executable=None):
        selected.update(mode=mode, oda=oda_executable, libre=libredwg_executable)
        return FakeODA(oda_executable)

    monkeypatch.setattr(gateway_module, "select_dwg_converter", fake_select)
    result = MCPGateway(tmp_path).call_tool("aec.ingest_file", {"source": str(source), "project_id": "P-DWG", "oda_executable": "fake-oda.exe"})
    assert result["status"] == "SUCCESS"
    assert result["source_format"] == "DWG"
    assert result["objects"] == 7
    assert result["conversion"]["status"] == "SUCCESS"
    assert result["authoritative_source"]["artifact_type"] == "CAD/DWG"
    assert result["converted_source"]["artifact_type"] == "CAD/DXF"
    manifest = (tmp_path / "projects" / "P-DWG" / "00_MANIFEST" / "project-manifest.json").read_text(encoding="utf-8")
    assert "authoritative_source_artifact" in manifest


def test_mcp_gateway_dwg_converter_honors_env_and_arguments(monkeypatch):
    import aec_intelligence.mcp_gateway as gateway_module
    from aec_intelligence.dwg import LibreDWGConverter, ODAConverter

    monkeypatch.setenv("AEC_DWG_CONVERTER", "libredwg")
    monkeypatch.setenv("AEC_LIBREDWG_EXECUTABLE", "/opt/dwg2dxf")
    converter = gateway_module._dwg_converter({})
    assert isinstance(converter, LibreDWGConverter)
    # explicit tool argument overrides env
    converter = gateway_module._dwg_converter({"dwg_converter": "oda", "oda_executable": "fake-oda.exe"})
    assert isinstance(converter, ODAConverter)
