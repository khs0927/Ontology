"""CAIR v0.1 domain model.

CAIR is intentionally independent from DXF, IFC, Google Drive, and any runtime
database. Geometry is referenced by an object while semantic meaning,
relationships, and provenance remain portable JSON data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid4, uuid5

CAIR_SCHEMA_VERSION = "0.1.0"
INGEST_STATUSES = {
    "SUCCESS",
    "SUCCESS_WITH_WARNINGS",
    "PARTIAL",
    "FAILED",
    "REQUIRES_REVIEW",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_object_id(project_id: str, entity_type: str, source_format: str, source_id: str) -> str:
    """Create a stable cross-application identifier for one source object."""

    seed = f"{project_id}|{source_format.upper()}|{source_id}"
    object_uuid = uuid5(NAMESPACE_URL, seed)
    slug = re.sub(r"[^a-z0-9]+", "-", entity_type.lower()).strip("-") or "object"
    return f"aec://project/{project_id}/{slug}/{object_uuid}"


@dataclass(frozen=True)
class SourceRef:
    file: str
    format: str
    entity_id: str | None = None
    layer: str | None = None
    artifact_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "format": self.format,
            "entity_id": self.entity_id,
            "layer": self.layer,
            "artifact_id": self.artifact_id,
        }


@dataclass(frozen=True)
class Classification:
    label: str
    confidence: float
    method: str
    evidence: tuple[str, ...] = ()
    state: str = "REQUIRES_REVIEW"

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("classification confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "evidence": list(self.evidence),
            "state": self.state,
        }


@dataclass(frozen=True)
class Provenance:
    source_file: str
    source_object: str | None
    source_format: str
    source_hash: str | None
    parser: str
    parser_version: str
    timestamp: str = field(default_factory=utc_now)
    agent: str = "aec-dxf-ingest"
    transformation: str = "normalized-to-cair"
    derived_from: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_object": self.source_object,
            "source_format": self.source_format,
            "source_hash": self.source_hash,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "timestamp": self.timestamp,
            "agent": self.agent,
            "transformation": self.transformation,
            "derived_from": list(self.derived_from),
        }


@dataclass(frozen=True)
class VersionInfo:
    version: int = 1
    revision: str = "V001"
    valid_from: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "revision": self.revision,
            "valid_from": self.valid_from,
        }


@dataclass
class CAIRObject:
    id: str
    project_id: str
    type: str
    source: SourceRef
    geometry_ref: str | None = None
    bbox: dict[str, float] = field(default_factory=dict)
    placement: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    classification: Classification | None = None
    provenance: Provenance | None = None
    version: VersionInfo = field(default_factory=VersionInfo)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "type": self.type,
            "source": self.source.to_dict(),
            "geometry_ref": self.geometry_ref,
            "bbox": self.bbox,
            "placement": self.placement,
            "properties": self.properties,
            "classification": self.classification.to_dict() if self.classification else None,
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "version": self.version.to_dict(),
        }


@dataclass(frozen=True)
class CAIRRelation:
    subject: str
    predicate: str
    object: str
    confidence: float = 1.0
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "confidence": round(self.confidence, 4),
            "provenance": self.provenance,
        }


@dataclass
class CAIRSnapshot:
    project_id: str
    objects: list[CAIRObject] = field(default_factory=list)
    relations: list[CAIRRelation] = field(default_factory=list)
    schema_version: str = CAIR_SCHEMA_VERSION
    snapshot_id: str = field(default_factory=lambda: f"cair-{utc_now().replace(':', '').replace('-', '')}-{uuid4().hex[:8]}")
    created_at: str = field(default_factory=utc_now)
    status: str = "SUCCESS"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in INGEST_STATUSES:
            raise ValueError(f"unsupported CAIR status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "project_id": self.project_id,
            "status": self.status,
            "metadata": self.metadata,
            "objects": [obj.to_dict() for obj in self.objects],
            "relations": [relation.to_dict() for relation in self.relations],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CAIRSnapshot":
        def source(value: dict[str, Any]) -> SourceRef:
            return SourceRef(value.get("file", ""), value.get("format", ""), value.get("entity_id"), value.get("layer"), value.get("artifact_id"))

        def classification(value: dict[str, Any] | None) -> Classification | None:
            if value is None:
                return None
            return Classification(value["label"], float(value["confidence"]), value["method"], tuple(value.get("evidence", [])), value.get("state", "REQUIRES_REVIEW"))

        def provenance(value: dict[str, Any] | None) -> Provenance | None:
            if value is None:
                return None
            return Provenance(value.get("source_file", ""), value.get("source_object"), value.get("source_format", ""), value.get("source_hash"), value.get("parser", ""), value.get("parser_version", ""), value.get("timestamp", utc_now()), value.get("agent", "aec"), value.get("transformation", ""), tuple(value.get("derived_from", [])))

        objects = [
            CAIRObject(
                id=value["id"], project_id=value["project_id"], type=value["type"], source=source(value["source"]),
                geometry_ref=value.get("geometry_ref"), bbox=value.get("bbox", {}), placement=value.get("placement", {}),
                properties=value.get("properties", {}), classification=classification(value.get("classification")), provenance=provenance(value.get("provenance")),
                version=VersionInfo(**value.get("version", {})),
            )
            for value in data.get("objects", [])
        ]
        relations = [CAIRRelation(**value) for value in data.get("relations", [])]
        return cls(
            project_id=data["project_id"], objects=objects, relations=relations,
            schema_version=data.get("schema_version", CAIR_SCHEMA_VERSION), snapshot_id=data.get("snapshot_id", ""),
            created_at=data.get("created_at", utc_now()), status=data.get("status", "SUCCESS"), metadata=data.get("metadata", {}),
        )

    def write_json(self, path: str) -> None:
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rows_for_objects(objects: Iterable[CAIRObject]) -> list[dict[str, Any]]:
    return [obj.to_dict() for obj in objects]
