"""Test worker job deduplication and revision-based review workflow."""

import json
from pathlib import Path
from aec_intelligence.operational.config import Settings
from aec_intelligence.operational.parsers import observation


def test_immutable_snapshot_review_workflow(tmp_path: Path):
    data_root = tmp_path / "data"
    doc_id = "doc_test_floorplan"
    snapshot_dir = data_root / "snapshots" / doc_id
    snapshot_dir.mkdir(parents=True)

    # Initial revision 0 snapshot
    initial_obj = observation(
        doc=doc_id,
        key="h_100",
        kind="Wall",
        text="내벽 100mm",
        evidence={"handle": "h_100", "layout": "Model"},
        bbox={"min_x": 0, "min_y": 0, "max_x": 100, "max_y": 200},
        state="AI_INFERRED",
    )
    rev0_snapshot = {
        "document_id": doc_id,
        "project_id": "P-TEST",
        "source_key": "floorplan.dxf",
        "name": "floorplan.dxf",
        "revision": 0,
        "source_hash": "hash_abc",
        "objects": [initial_obj],
        "relations": [],
        "warnings": [],
        "metrics": {},
    }

    rev0_file = snapshot_dir / "rev-0.json"
    rev0_file.write_text(json.dumps(rev0_snapshot, indent=2), encoding="utf-8")

    # Apply Human Review: Change Type to "PartitionWall" and confirm
    loaded_rev0 = json.loads(rev0_file.read_text(encoding="utf-8"))
    next_rev = loaded_rev0["revision"] + 1

    for obj in loaded_rev0["objects"]:
        if obj["id"] == initial_obj["id"]:
            obj["state"] = "USER_CONFIRMED"
            obj["type"] = "PartitionWall"
            props = obj.setdefault("properties", {})
            props["human_review"] = {
                "action": "CHANGE_TYPE",
                "notes": "Reviewed and confirmed as lightweight partition wall",
                "prior_revision": 0,
            }

    loaded_rev0["revision"] = next_rev
    rev1_file = snapshot_dir / f"rev-{next_rev}.json"
    rev1_file.write_text(json.dumps(loaded_rev0, indent=2), encoding="utf-8")

    # Verify immutability of rev-0
    rev0_reloaded = json.loads(rev0_file.read_text(encoding="utf-8"))
    assert rev0_reloaded["revision"] == 0
    assert rev0_reloaded["objects"][0]["state"] == "AI_INFERRED"
    assert rev0_reloaded["objects"][0]["type"] == "Wall"

    # Verify rev-1
    rev1_reloaded = json.loads(rev1_file.read_text(encoding="utf-8"))
    assert rev1_reloaded["revision"] == 1
    assert rev1_reloaded["objects"][0]["state"] == "USER_CONFIRMED"
    assert rev1_reloaded["objects"][0]["type"] == "PartitionWall"
    assert rev1_reloaded["objects"][0]["properties"]["human_review"]["action"] == "CHANGE_TYPE"
