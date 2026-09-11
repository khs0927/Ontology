"""Evidence-based CAD semantic classification."""

from __future__ import annotations

import re
from typing import Any

from .cair import CAIRObject, Classification, Provenance, SourceRef, stable_object_id
from .dxf import NormalizedCADEntity


RULES: list[tuple[str, tuple[str, ...], str, float]] = [
    ("Wall", ("wall", "벽", "partition", "a-wal", "w-wall"), "layer/name indicates wall", 0.91),
    ("Door", ("door", "문", "a-dr", "d-door"), "layer/name indicates door", 0.90),
    ("Window", ("window", "창", "a-wi", "w-window"), "layer/name indicates window", 0.90),
    ("Column", ("column", "col", "기둥", "a-col"), "layer/name indicates column", 0.88),
    ("Beam", ("beam", "girder", "보", "s-beam"), "layer/name indicates beam", 0.88),
    ("Slab", ("slab", "floor", "바닥", "s-slab"), "layer/name indicates slab", 0.86),
    ("Grid", ("grid", "axis", "그리드", "a-grid"), "layer/name indicates grid", 0.84),
]


def _contains_token(value: str, token: str) -> bool:
    if token.isascii() and token.isalpha():
        return re.search(rf"(?:^|[-_. ]){re.escape(token)}(?:$|[-_. ])", value) is not None or token in value
    return token in value


def classify(entity: NormalizedCADEntity) -> tuple[str, Classification]:
    layer = entity.layer.lower()
    block_name = str(entity.properties.get("block_name", "")).lower()
    text = str(entity.properties.get("text", "")).lower()
    evidence_source = " ".join((layer, block_name, text))
    for label, tokens, reason, base_confidence in RULES:
        matched = [token for token in tokens if _contains_token(evidence_source, token)]
        if matched:
            evidence = (f"{reason}: {matched[0]}", f"entity_type={entity.entity_type}", f"layer={entity.layer}")
            return label, Classification(label, base_confidence, "hybrid_rules", evidence, _state(base_confidence))
    if entity.entity_type in {"TEXT", "MTEXT"}:
        confidence = 0.78
        return "Annotation", Classification("Annotation", confidence, "entity_type_rule", ("entity_type=text", f"layer={entity.layer}"), _state(confidence))
    if entity.entity_type in {"DIMENSION", "LEADER", "MLEADER"}:
        confidence = 0.82
        return "Dimension", Classification("Dimension", confidence, "entity_type_rule", (f"entity_type={entity.entity_type}",), _state(confidence))
    confidence = 0.35
    return "CADEntity", Classification("CADEntity", confidence, "fallback", (f"entity_type={entity.entity_type}", f"layer={entity.layer}"), _state(confidence))


def _state(confidence: float) -> str:
    if confidence > 0.95:
        return "AUTO_ACCEPT"
    if confidence >= 0.75:
        return "ACCEPT_WITH_WARNING"
    return "REQUIRE_VALIDATION"


def to_cair_object(
    entity: NormalizedCADEntity,
    project_id: str,
    source_file: str,
    source_hash: str,
    artifact_id: str | None = None,
    parser_name: str = "ezdxf",
    parser_version: str = "unknown",
    geometry_index_ref: str | None = None,
) -> CAIRObject:
    label, classification = classify(entity)
    object_id = stable_object_id(project_id, label, "DXF", entity.handle)
    source = SourceRef(source_file, "DXF", entity.handle, entity.layer, artifact_id)
    provenance = Provenance(source_file, entity.handle, "DXF", source_hash, parser_name, parser_version)
    return CAIRObject(
        id=object_id,
        project_id=project_id,
        type=label,
        source=source,
        geometry_ref=geometry_index_ref or f"aec://geometry/{project_id}/{entity.handle}",
        bbox=entity.bbox,
        placement=entity.geometry.get("location", {}),
        properties={"cad_entity_type": entity.entity_type, "layer": entity.layer, **entity.properties},
        classification=classification,
        provenance=provenance,
    )

