"""Non-silent ingestion validation and quality scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cair import CAIRObject, CAIRSnapshot
from .dxf import DXFParseResult


@dataclass
class ValidationReport:
    status: str
    scores: dict[str, float]
    ingest_confidence: float
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scores": {key: round(value, 4) for key, value in self.scores.items()},
            "ingest_confidence": round(self.ingest_confidence, 4),
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": self.checks,
        }


def validate_dxf_ingest(parsed: DXFParseResult, objects: list[CAIRObject], snapshot: CAIRSnapshot, preview_written: bool = False) -> ValidationReport:
    errors: list[str] = []
    warnings = list(parsed.warnings)
    if not parsed.entities:
        errors.append("no modelspace entities were parsed")
    if parsed.unsupported_entity_types:
        warnings.append("unsupported entity types: " + ", ".join(parsed.unsupported_entity_types))
    if any(obj.classification and obj.classification.state == "REQUIRE_VALIDATION" for obj in objects):
        warnings.append("one or more semantic classifications require validation")
    scores = {
        "parse_score": 1.0 if parsed.entities else 0.0,
        "semantic_score": (sum(obj.classification.confidence for obj in objects if obj.classification) / len(objects)) if objects else 0.0,
        "geometry_score": 1.0 if parsed.extents or not parsed.entities else 0.5,
        "relationship_score": 1.0 if len(snapshot.relations) >= len(objects) else 0.5,
        "visual_match_score": 1.0 if preview_written else 0.0,
        "provenance_score": sum(bool(obj.provenance and obj.source.entity_id) for obj in objects) / len(objects) if objects else 0.0,
    }
    ingest_confidence = sum(scores.values()) / len(scores)
    if errors:
        status = "FAILED"
    elif warnings:
        status = "SUCCESS_WITH_WARNINGS"
    else:
        status = "SUCCESS"
    snapshot.status = status
    return ValidationReport(
        status=status,
        scores=scores,
        ingest_confidence=ingest_confidence,
        errors=errors,
        warnings=warnings,
        checks={
            "source_file": parsed.source_file,
            "dxf_version": parsed.dxf_version,
            "units": parsed.units,
            "counts": parsed.counts,
            "bbox": parsed.extents,
            "object_count": len(objects),
            "snapshot_id": snapshot.snapshot_id,
        },
    )

