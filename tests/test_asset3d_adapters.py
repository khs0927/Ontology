import json
from pathlib import Path

from aec_intelligence.asset3d_adapters import GLTFParser, OBJParser, STLParser, STEPParser, parse_3d_asset
from aec_intelligence.mcp_gateway import MCPGateway


def test_obj_and_ascii_stl_parsers_report_mesh_evidence(tmp_path: Path):
    obj = tmp_path / "triangle.obj"
    obj.write_text("o Triangle\nv 0 0 0\nv 2 0 0\nv 0 3 1\nf 1 2 3\n", encoding="utf-8")
    obj_result = OBJParser().parse(obj)
    assert obj_result.source_format == "OBJ"
    assert obj_result.metadata["vertex_count"] == 3
    assert obj_result.metadata["face_count"] == 1
    assert obj_result.metadata["bbox"]["max_z"] == 1.0

    stl = tmp_path / "triangle.stl"
    stl.write_text(
        "solid triangle\n"
        " facet normal 0 0 1\n"
        "  outer loop\n"
        "   vertex 0 0 0\n"
        "   vertex 2 0 0\n"
        "   vertex 0 3 1\n"
        "  endloop\n"
        " endfacet\n"
        "endsolid triangle\n",
        encoding="ascii",
    )
    stl_result = STLParser().parse(stl)
    assert stl_result.source_format == "STL"
    assert stl_result.metadata["encoding"] == "ascii"
    assert stl_result.metadata["facet_count"] == 1
    assert stl_result.metadata["vertex_count"] == 3


def test_step_and_glb_parsers_read_actual_exchange_artifacts(tmp_path: Path):
    step = next((Path(__file__).parents[1] / "projects" / "AEC-2026-000004" / "02_DERIVED" / "BIM" / "STEP").glob("*.step"))
    step_result = STEPParser().parse(step)
    assert step_result.source_format == "STEP"
    assert step_result.metadata["part21_entity_count"] > 0

    glb = next((Path(__file__).parents[1] / "projects" / "AEC-2026-000004" / "02_DERIVED" / "BIM" / "GLB").glob("*.glb"))
    glb_result = parse_3d_asset(glb)
    assert glb_result.source_format == "GLB"
    assert glb_result.metadata["glb_version"] == 2
    assert glb_result.metadata["mesh_count"] >= 1

    gltf = tmp_path / "minimal.gltf"
    gltf.write_text(json.dumps({"asset": {"version": "2.0"}, "scenes": [{}], "nodes": [], "meshes": []}), encoding="utf-8")
    assert GLTFParser().parse(gltf).source_format == "GLTF"


def test_mcp_3d_asset_tool_is_explicit_and_semantic_neutral(tmp_path: Path):
    obj = tmp_path / "triangle.obj"
    obj.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    result = MCPGateway(tmp_path).call_tool("aec.parse_3d_asset", {"source": str(obj)})
    assert result["status"] == "SUCCESS"
    assert result["metadata"]["semantic_cair_generated"] is False
    definition = next(item for item in MCPGateway(tmp_path).list_tools() if item["name"] == "aec.parse_3d_asset")
    assert definition["inputSchema"]["required"] == ["source"]


def test_ingest_file_registers_3d_exchange_without_fabricating_cair(tmp_path: Path):
    obj = tmp_path / "reference.obj"
    obj.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    gateway = MCPGateway(tmp_path)
    result = gateway.call_tool(
        "aec.ingest_file",
        {"source": str(obj), "project_id": "P-OBJ-REFERENCE", "name": "OBJ Reference"},
    )
    assert result["status"] == "SUCCESS"
    assert result["semantic_cair_generated"] is False
    assert result["exchange"]["metadata"]["face_count"] == 1
    report = Path(result["outputs"]["exchange_report"])
    assert report.is_file()
    project = tmp_path / "projects" / "P-OBJ-REFERENCE"
    assert not (project / "03_CAIR" / "project-cair.json").exists()
    repeated = gateway.call_tool(
        "aec.ingest_file",
        {"source": str(obj), "project_id": "P-OBJ-REFERENCE", "name": "OBJ Reference"},
    )
    assert repeated["skipped"] is True
