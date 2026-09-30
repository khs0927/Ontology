import json
from pathlib import Path
import shutil

from aec_intelligence.mcp_gateway import MCPGateway


def _prepare_repo(tmp_path: Path) -> Path:
    source_root = Path(__file__).parents[1]
    shutil.copytree(
        source_root / "extensions" / "intelligence_fabric",
        tmp_path / "extensions" / "intelligence_fabric",
    )
    global_root = tmp_path / "global" / "00_GLOBAL"
    global_root.mkdir(parents=True)
    rows = {
        "global-project-registry.jsonl": [
            {"project_id": "P1", "name": "Factory", "status": "ACTIVE"},
        ],
        "global-object-registry.jsonl": [
            {"id": "aec://object/door-1", "project_id": "P1", "type": "Door", "geometry_ref": "geom://1"},
            {"id": "aec://object/wall-1", "project_id": "P1", "type": "Wall", "geometry_ref": "geom://2"},
        ],
        "global-relations.jsonl": [
            {"subject": "aec://object/door-1", "predicate": "HOSTED_BY", "object": "aec://object/wall-1", "confidence": 0.9},
        ],
        "global-provenance.jsonl": [
            {"object_id": "aec://object/door-1", "source_file": "factory.dwg", "source_hash": "a" * 64},
        ],
    }
    for name, values in rows.items():
        (global_root / name).write_text(
            "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
            encoding="utf-8",
        )
    return tmp_path


def test_context_inspection_denies_source_egress_by_default(tmp_path: Path):
    root = _prepare_repo(tmp_path)
    result = MCPGateway(root).call_tool(
        "aec.context_inspect_code",
        {"question": "Where is provenance validated?"},
    )
    assert result["status"] == "REQUIRES_EGRESS_APPROVAL"
    assert result["mode"] == "READ_ONLY_CONTEXT_SELECTION"
    assert result["canonical_mutation"] is False
    assert result["execution_authorized"] is False


def test_context_inspection_cannot_escape_repository(tmp_path: Path):
    root = _prepare_repo(tmp_path)
    result = MCPGateway(root).call_tool(
        "aec.context_inspect_code",
        {"question": "anything", "root": ".."},
    )
    assert result["status"] == "FAILED"
    assert "inside the repository" in result["error"]


def test_graph_backend_plan_prefers_existing_local_baseline(tmp_path: Path):
    root = _prepare_repo(tmp_path)
    gateway = MCPGateway(root)
    local = gateway.call_tool("aec.graph_backend_plan", {})
    distributed = gateway.call_tool(
        "aec.graph_backend_plan",
        {"local_only": False, "object_store_durability": True},
    )
    semantic = gateway.call_tool(
        "aec.graph_backend_plan",
        {"requires_sparql": True},
    )
    assert local["backend"] == "Apache AGE"
    assert distributed["backend"] == "HydraDB"
    assert semantic["backend"] == "PyOxigraph"
    assert local["canonical"] is False


def test_hydradb_preview_is_read_only_and_export_is_runtime_only(tmp_path: Path):
    root = _prepare_repo(tmp_path)
    gateway = MCPGateway(root)
    canonical = root / "global" / "00_GLOBAL" / "global-object-registry.jsonl"
    before = canonical.read_bytes()

    preview = gateway.call_tool("aec.graph_hydradb_preview", {"max_statements": 3})
    assert preview["status"] == "SUCCESS"
    assert preview["counts"]["objects"] == 2
    assert preview["statement_count"] >= 3
    assert len(preview["statements"]) == 3
    assert not (root / "runtime" / "hydradb").exists()
    assert canonical.read_bytes() == before

    exported = gateway.call_tool("aec.graph_hydradb_export", {})
    target = Path(exported["target"])
    assert exported["status"] == "SUCCESS"
    assert exported["canonical_mutation"] is False
    assert exported["rebuildable"] is True
    assert target.is_file()
    assert str(target).startswith(str((root / "runtime").resolve()))
    assert canonical.read_bytes() == before
