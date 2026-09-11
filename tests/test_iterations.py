from pathlib import Path
import copy
import json

import pytest

from aec_intelligence.iterations import IterationManager
from aec_intelligence.pipeline import DXFIngestionPipeline


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_iteration_memory_diff_and_promotion_are_non_destructive(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "P-ITER")
    manager = IterationManager(tmp_path)
    first = manager.create("P-ITER", "Imported", changes=["normalize source"], agent="test", validation_status="SUCCESS", metrics_after={"wall_count": 2})
    second = manager.create("P-ITER", "Circulation study", changes=["move door"], agent="test", validation_status="SUCCESS", metrics_after={"wall_count": 2, "door_count": 1})
    diff = manager.compare("P-ITER", first.iteration_id, second.iteration_id)
    assert diff["right_iteration"] == "V002"
    promoted = manager.promote("P-ITER", second.iteration_id, "test-approver")
    assert promoted.manifest["status"] == "PROMOTED"
    assert (tmp_path / "projects" / "P-ITER" / "13_AGENT_MEMORY" / "active-design-state.json").is_file()
    with pytest.raises(ValueError):
        manager.promote("P-ITER", first.iteration_id, "")


def test_iteration_diff_tracks_cair_object_and_relationship_changes(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "P-DIFF")
    manager = IterationManager(tmp_path)
    first = manager.create("P-DIFF", "Baseline", validation_status="SUCCESS")

    cair_path = tmp_path / "projects" / "P-DIFF" / "03_CAIR" / "project-cair.json"
    cair = json.loads(cair_path.read_text(encoding="utf-8"))
    changed = cair["objects"][0]
    changed_id = changed["id"]
    changed["properties"]["design_revision"] = "study-02"
    changed["classification"]["label"] = "Column"
    changed["bbox"]["min_x"] += 100
    changed["bbox"]["max_x"] += 100
    changed["bbox"]["max_y"] += 100
    changed["placement"] = {"x": 100, "y": 0, "z": 0}
    deleted_id = cair["objects"].pop()["id"]
    added = copy.deepcopy(cair["objects"][0])
    added["id"] = "aec://project/P-DIFF/study/synthetic-object"
    added["type"] = "StudyObject"
    cair["objects"].append(added)
    cair["relations"][0]["predicate"] = "relatedTo"
    cair["snapshot_id"] = "cair-manual-study-02"
    cair_path.write_text(json.dumps(cair, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    second = manager.create("P-DIFF", "Study", validation_status="SUCCESS")
    diff = manager.compare("P-DIFF", first.iteration_id, second.iteration_id)["object_diff"]
    assert diff["status"] == "AVAILABLE"
    assert added["id"] in diff["added"]
    assert deleted_id in diff["deleted"]
    assert changed_id in diff["modified"]
    assert changed_id in {item["id"] for item in diff["reclassified"]}
    assert changed_id in {item["id"] for item in diff["property_changed"]}
    assert changed_id in {item["id"] for item in diff["geometry_changed"]}
    assert changed_id in diff["moved"]
    assert changed_id in diff["resized"]
    assert diff["relationship_changed"]["added"]
    assert diff["relationship_changed"]["deleted"]
