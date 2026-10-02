import importlib.util
import json
from pathlib import Path

from aec_intelligence.pipeline import DXFIngestionPipeline
from aec_intelligence.rebuild import rebuild_global_indexes_from_projects, rebuild_runtime_from_repository, refresh_derived_exports
from aec_intelligence.storage import LocalArtifactStore

HAS_PYARROW = importlib.util.find_spec("pyarrow") is not None

FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_runtime_rebuild_uses_portable_global_indexes(tmp_path: Path):
    pipeline = DXFIngestionPipeline(tmp_path)
    pipeline.ingest(FIXTURE, "AEC-TEST-REBUILD")
    pipeline.registry.close()
    target = tmp_path / "rebuilt" / "runtime.sqlite3"
    report = rebuild_runtime_from_repository(tmp_path, target)
    assert report.status == "SUCCESS"
    assert report.projects == 1
    assert report.objects == 7
    assert report.artifacts == len(pipeline.store.list("AEC-TEST-REBUILD"))


def test_global_rebuild_removes_projects_not_present_locally(tmp_path: Path):
    pipeline = DXFIngestionPipeline(tmp_path)
    pipeline.ingest(FIXTURE, "AEC-TEST-KEEP")
    global_root = tmp_path / "global" / "00_GLOBAL"
    with (global_root / "global-project-registry.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"project_id": "P-GONE", "name": "Gone", "status": "ACTIVE", "last_ingest": ""}) + "\n")
    relation_path = global_root / "global-relations.jsonl"
    original_relations = [line for line in relation_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = rebuild_global_indexes_from_projects(tmp_path)
    assert report["status"] == "SUCCESS"
    assert report["projects"] == 1
    assert report["relations"] == len(original_relations)
    assert "P-GONE" not in (global_root / "global-project-registry.jsonl").read_text(encoding="utf-8")
    assert (global_root / "global-project-registry.parquet").is_file() == HAS_PYARROW  # parquet mirrors need the [parquet] extra
    assert (tmp_path / "global" / "03_KNOWLEDGE_GRAPH" / "relationships.parquet").is_file() == HAS_PYARROW  # parquet mirrors need the [parquet] extra


def test_global_rebuild_preserves_drive_materialized_global_artifacts(tmp_path: Path):
    pipeline = DXFIngestionPipeline(tmp_path)
    pipeline.ingest(FIXTURE, "AEC-TEST-GLOBAL-ARTIFACT")
    global_file = tmp_path / "global" / "03_KNOWLEDGE_GRAPH" / "drive-materialized.graphml"
    global_file.write_text("<graphml />\n", encoding="utf-8")
    store = LocalArtifactStore(tmp_path)
    record = store.put(
        global_file,
        "GLOBAL",
        "GLOBAL/03_KNOWLEDGE_GRAPH",
        relative_destination="global/03_KNOWLEDGE_GRAPH/drive-materialized.graphml",
    )

    report = rebuild_global_indexes_from_projects(tmp_path)

    assert report["status"] == "SUCCESS"
    rows = [json.loads(line) for line in (tmp_path / "global" / "00_GLOBAL" / "global-artifact-registry.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(row["artifact_id"] == record.artifact_id and row["project_id"] == "GLOBAL" for row in rows)


def test_refresh_derived_exports_backfills_without_rewriting_cair(tmp_path: Path):
    pipeline = DXFIngestionPipeline(tmp_path)
    initial = pipeline.ingest(FIXTURE, "AEC-TEST-REFRESH")
    cair_path = tmp_path / "projects" / "AEC-TEST-REFRESH" / "03_CAIR" / "project-cair.json"
    before = cair_path.read_bytes()
    graph_path = tmp_path / "projects" / "AEC-TEST-REFRESH" / "05_GRAPH" / "project.graphml"
    graph_path.unlink()

    report = refresh_derived_exports(tmp_path, "AEC-TEST-REFRESH")

    assert report["status"] == "SUCCESS"
    assert cair_path.read_bytes() == before
    assert (cair_path.parent / "cair-manifest.json").is_file()
    assert (cair_path.parent / "tables" / "properties.jsonl").is_file()
    assert (cair_path.parent / "tables" / "source-mappings.jsonl").is_file()
    geometry_rows = [json.loads(line) for line in (cair_path.parent / "tables" / "geometry_index.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {row["geometry_ref"] for row in geometry_rows} == {obj.geometry_ref for obj in initial.snapshot.objects if obj.geometry_ref}
    assert graph_path.is_file()
