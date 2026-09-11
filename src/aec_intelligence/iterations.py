"""Design iteration memory stored outside raw and canonical source artifacts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

from .cair import CAIRSnapshot, utc_now


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _object_state(value: dict[str, Any]) -> dict[str, Any]:
    """Keep the iteration state portable without copying provenance or mesh data."""

    return {
        "id": value.get("id"),
        "type": value.get("type"),
        "geometry_ref": value.get("geometry_ref"),
        "bbox": value.get("bbox") or {},
        "placement": value.get("placement") or {},
        "properties": value.get("properties") or {},
        "classification": value.get("classification"),
    }


def _relation_state(value: dict[str, Any]) -> dict[str, Any]:
    """Store relationship evidence while keeping the comparison key explicit."""

    return {
        "subject": value.get("subject"),
        "predicate": value.get("predicate"),
        "object": value.get("object"),
        "confidence": value.get("confidence", 1.0),
        "provenance": value.get("provenance") or {},
    }


def _relation_key(value: dict[str, Any]) -> str:
    return "|".join(str(value.get(key, "")) for key in ("subject", "predicate", "object"))


def _numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bbox_dimensions(value: dict[str, Any]) -> tuple[float, float, float] | None:
    keys = ("min_x", "min_y", "min_z", "max_x", "max_y", "max_z")
    numbers = [_numeric(value.get(key)) for key in keys]
    if any(number is None for number in numbers):
        return None
    min_x, min_y, min_z, max_x, max_y, max_z = numbers
    return (max_x - min_x, max_y - min_y, max_z - min_z)


def _bbox_center(value: dict[str, Any]) -> tuple[float, float, float] | None:
    keys = ("min_x", "min_y", "min_z", "max_x", "max_y", "max_z")
    numbers = [_numeric(value.get(key)) for key in keys]
    if any(number is None for number in numbers):
        return None
    min_x, min_y, min_z, max_x, max_y, max_z = numbers
    return ((min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2)


def _spatial_change(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, bool]:
    """Return moved/resized only when explicit placement or bbox evidence supports it."""

    placement_changed = left.get("placement", {}) != right.get("placement", {})
    left_bbox = left.get("bbox", {}) or {}
    right_bbox = right.get("bbox", {}) or {}
    left_dimensions = _bbox_dimensions(left_bbox)
    right_dimensions = _bbox_dimensions(right_bbox)
    resized = left_dimensions is not None and right_dimensions is not None and left_dimensions != right_dimensions
    moved = placement_changed
    if left_dimensions is not None and right_dimensions is not None and left_dimensions == right_dimensions:
        left_center = _bbox_center(left_bbox)
        right_center = _bbox_center(right_bbox)
        moved = moved or (left_center is not None and right_center is not None and left_center != right_center)
    return moved, resized


def _object_diff(left_objects: list[dict[str, Any]], right_objects: list[dict[str, Any]], left_relations: list[dict[str, Any]], right_relations: list[dict[str, Any]]) -> dict[str, Any]:
    left_by_id = {str(value.get("id")): value for value in left_objects if value.get("id")}
    right_by_id = {str(value.get("id")): value for value in right_objects if value.get("id")}
    added = sorted(set(right_by_id) - set(left_by_id))
    deleted = sorted(set(left_by_id) - set(right_by_id))
    reclassified: list[dict[str, Any]] = []
    property_changed: list[dict[str, Any]] = []
    geometry_changed: list[dict[str, Any]] = []
    moved: list[str] = []
    resized: list[str] = []
    modified: set[str] = set()

    for object_id in sorted(set(left_by_id) & set(right_by_id)):
        left = _object_state(left_by_id[object_id])
        right = _object_state(right_by_id[object_id])
        if left.get("type") != right.get("type") or left.get("classification") != right.get("classification"):
            reclassified.append({"id": object_id, "from": {"type": left.get("type"), "classification": left.get("classification")}, "to": {"type": right.get("type"), "classification": right.get("classification")}})
            modified.add(object_id)
        if left.get("properties") != right.get("properties"):
            property_changed.append({"id": object_id, "from": left.get("properties", {}), "to": right.get("properties", {})})
            modified.add(object_id)
        if any(left.get(key) != right.get(key) for key in ("geometry_ref", "bbox", "placement")):
            geometry_changed.append({"id": object_id, "from": {key: left.get(key) for key in ("geometry_ref", "bbox", "placement")}, "to": {key: right.get(key) for key in ("geometry_ref", "bbox", "placement")}})
            modified.add(object_id)
        object_moved, object_resized = _spatial_change(left, right)
        if object_moved:
            moved.append(object_id)
        if object_resized:
            resized.append(object_id)

    left_relations_by_key = {_relation_key(value): _relation_state(value) for value in left_relations if _relation_key(value) != "||"}
    right_relations_by_key = {_relation_key(value): _relation_state(value) for value in right_relations if _relation_key(value) != "||"}
    relation_added = [right_relations_by_key[key] for key in sorted(set(right_relations_by_key) - set(left_relations_by_key))]
    relation_deleted = [left_relations_by_key[key] for key in sorted(set(left_relations_by_key) - set(right_relations_by_key))]
    relation_modified = [
        {"key": key, "from": left_relations_by_key[key], "to": right_relations_by_key[key]}
        for key in sorted(set(left_relations_by_key) & set(right_relations_by_key))
        if left_relations_by_key[key] != right_relations_by_key[key]
    ]
    relation_diff = {"added": relation_added, "deleted": relation_deleted, "modified": relation_modified}
    return {
        "status": "AVAILABLE",
        "added": added,
        "deleted": deleted,
        "modified": sorted(modified),
        "reclassified": reclassified,
        "property_changed": property_changed,
        "geometry_changed": geometry_changed,
        "moved": moved,
        "resized": resized,
        "relationship_changed": relation_diff,
        "counts": {
            "added": len(added),
            "deleted": len(deleted),
            "modified": len(modified),
            "reclassified": len(reclassified),
            "property_changed": len(property_changed),
            "geometry_changed": len(geometry_changed),
            "moved": len(moved),
            "resized": len(resized),
            "relationship_added": len(relation_added),
            "relationship_deleted": len(relation_deleted),
            "relationship_modified": len(relation_modified),
        },
    }


@dataclass(frozen=True)
class IterationRecord:
    project_id: str
    iteration_id: str
    path: Path
    manifest: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {**self.manifest, "path": str(self.path)}


class IterationManager:
    def __init__(self, repository_root: str | Path):
        self.root = Path(repository_root).resolve()

    def _project_path(self, project_id: str) -> Path:
        return self.root / "projects" / project_id

    def _snapshot(self, project_id: str) -> tuple[str | None, list[dict[str, Any]]]:
        path = self._project_path(project_id) / "03_CAIR" / "project-cair.json"
        if not path.is_file():
            return None, []
        snapshot = _read_json(path)
        return snapshot.get("snapshot_id"), snapshot.get("objects", [])

    def _snapshot_state(self, project_id: str) -> dict[str, Any]:
        path = self._project_path(project_id) / "03_CAIR" / "project-cair.json"
        if not path.is_file():
            return {"snapshot_id": None, "objects": [], "relations": []}
        snapshot = _read_json(path)
        return {
            "snapshot_id": snapshot.get("snapshot_id"),
            "objects": [_object_state(value) for value in snapshot.get("objects", []) if value.get("id")],
            "relations": [_relation_state(value) for value in snapshot.get("relations", []) if _relation_key(value) != "||"],
        }

    @staticmethod
    def _manifest_state(manifest: dict[str, Any]) -> dict[str, Any] | None:
        value = manifest.get("cair_state")
        if not isinstance(value, dict):
            return None
        objects = value.get("objects")
        relations = value.get("relations")
        if not isinstance(objects, list) or not isinstance(relations, list):
            return None
        return {
            "snapshot_id": value.get("snapshot_id"),
            "objects": [_object_state(item) for item in objects if isinstance(item, dict) and item.get("id")],
            "relations": [_relation_state(item) for item in relations if isinstance(item, dict) and _relation_key(item) != "||"],
        }

    def list(self, project_id: str) -> list[IterationRecord]:
        root = self._project_path(project_id) / "10_ITERATIONS"
        records: list[IterationRecord] = []
        if not root.is_dir():
            return records
        for path in sorted(root.glob("V*/manifest.json")):
            manifest = _read_json(path)
            records.append(IterationRecord(project_id, str(manifest.get("iteration_id", path.parent.name)), path.parent, manifest))
        records.sort(key=lambda record: (int(record.manifest.get("sequence", 0)), record.iteration_id))
        return records

    def create(
        self,
        project_id: str,
        reason: str,
        changes: Iterable[str] = (),
        constraints: Iterable[str] = (),
        agent: str = "aec-agent",
        artifacts: Iterable[str] = (),
        validation_status: str = "REQUIRES_REVIEW",
        metrics_before: dict[str, Any] | None = None,
        metrics_after: dict[str, Any] | None = None,
    ) -> IterationRecord:
        cair_state = self._snapshot_state(project_id)
        snapshot_id = cair_state["snapshot_id"]
        objects = cair_state["objects"]
        existing = self.list(project_id)
        sequence = max((int(record.manifest.get("sequence", 0)) for record in existing), default=0) + 1
        iteration_id = f"V{sequence:03d}"
        path = self._project_path(project_id) / "10_ITERATIONS" / iteration_id
        type_counts = Counter(str(obj.get("type")) for obj in objects)
        status = "VALIDATED" if validation_status in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} else "DRAFT"
        manifest = {
            "iteration_id": iteration_id,
            "project_id": project_id,
            "sequence": sequence,
            "status": status,
            "created_at": utc_now(),
            "agent": agent,
            "source_snapshot": snapshot_id,
            "source_cair": f"projects/{project_id}/03_CAIR/project-cair.json",
            "cair_state": cair_state,
            "artifacts": list(artifacts),
            "diff": {"changes": list(changes)},
            "reason": reason,
            "constraints": list(constraints),
            "metrics_before": metrics_before or {},
            "metrics_after": metrics_after or {},
            "object_type_counts": dict(sorted(type_counts.items())),
            "relation_count": len(cair_state["relations"]),
            "validation": {"status": validation_status},
            "approval": {"status": "PENDING", "approved_by": None, "approved_at": None},
        }
        _write_json(path / "manifest.json", manifest)
        _write_json(path / "design-reasoning.json", {
            "change": list(changes),
            "reason": reason,
            "constraint": list(constraints),
            "source": manifest["source_cair"],
            "metric_before": metrics_before or {},
            "metric_after": metrics_after or {},
            "agent": agent,
            "cair_state": cair_state,
            "approval": manifest["approval"],
            "validation": manifest["validation"],
        })
        return IterationRecord(project_id, iteration_id, path, manifest)

    def compare(self, project_id: str, left_id: str, right_id: str) -> dict[str, Any]:
        records = {record.iteration_id: record for record in self.list(project_id)}
        if left_id not in records or right_id not in records:
            raise FileNotFoundError(f"iteration not found: {left_id} or {right_id}")
        left = records[left_id].manifest
        right = records[right_id].manifest
        left_types = left.get("object_type_counts", {})
        right_types = right.get("object_type_counts", {})
        all_types = sorted(set(left_types) | set(right_types))
        type_delta = {key: int(right_types.get(key, 0)) - int(left_types.get(key, 0)) for key in all_types if int(right_types.get(key, 0)) != int(left_types.get(key, 0))}
        left_state = self._manifest_state(left)
        right_state = self._manifest_state(right)
        if left_state is None or right_state is None:
            object_diff: dict[str, Any] = {
                "status": "UNAVAILABLE",
                "reason": "one or both iterations predate portable CAIR state snapshots",
                "added": [],
                "deleted": [],
                "modified": [],
                "reclassified": [],
                "property_changed": [],
                "geometry_changed": [],
                "moved": [],
                "resized": [],
                "relationship_changed": {"added": [], "deleted": [], "modified": []},
                "counts": {},
            }
        else:
            object_diff = _object_diff(left_state["objects"], right_state["objects"], left_state["relations"], right_state["relations"])
        return {
            "project_id": project_id,
            "left_iteration": left_id,
            "right_iteration": right_id,
            "status_change": {"from": left.get("status"), "to": right.get("status")},
            "type_count_delta": type_delta,
            "object_diff": object_diff,
            "reason_delta": {"from": left.get("reason"), "to": right.get("reason")},
            "metric_delta": {"from": left.get("metrics_after", {}), "to": right.get("metrics_after", {})},
            "change_delta": {"from": left.get("diff", {}).get("changes", []), "to": right.get("diff", {}).get("changes", [])},
        }

    def promote(self, project_id: str, iteration_id: str, approved_by: str) -> IterationRecord:
        if not approved_by.strip():
            raise ValueError("approved_by is required for promotion")
        records = {record.iteration_id: record for record in self.list(project_id)}
        if iteration_id not in records:
            raise FileNotFoundError(f"iteration not found: {iteration_id}")
        record = records[iteration_id]
        validation_status = record.manifest.get("validation", {}).get("status")
        if validation_status not in {"SUCCESS", "SUCCESS_WITH_WARNINGS"}:
            raise ValueError("only successfully validated iterations may be promoted")
        manifest = dict(record.manifest)
        manifest["status"] = "PROMOTED"
        manifest["approval"] = {"status": "APPROVED", "approved_by": approved_by, "approved_at": utc_now()}
        _write_json(record.path / "manifest.json", manifest)
        project_manifest_path = self._project_path(project_id) / "00_MANIFEST" / "project-manifest.json"
        project_manifest = _read_json(project_manifest_path)
        project_manifest["latest_iteration"] = iteration_id
        project_manifest["active_design_state"] = f"projects/{project_id}/13_AGENT_MEMORY/active-design-state.json"
        _write_json(project_manifest_path, project_manifest)
        _write_json(self._project_path(project_id) / "13_AGENT_MEMORY" / "active-design-state.json", {"project_id": project_id, "latest_iteration": iteration_id, "status": "PROMOTED", "source_cair": manifest["source_cair"], "promoted_at": manifest["approval"]["approved_at"]})
        return IterationRecord(project_id, iteration_id, record.path, manifest)
