"""Small, dependency-free Turtle exporter for CAIR snapshots."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .cair import CAIRSnapshot


PREFIXES = """@prefix aec: <https://example.org/aec#> .
@prefix bot: <https://w3id.org/bot#> .
@prefix omg: <https://w3id.org/omg#> .
@prefix fog: <https://w3id.org/fog#> .
@prefix geo: <http://www.opengis.net/ont/geosparql#> .
@prefix bsdd: <https://identifier.buildingsmart.org/uri/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

"""


def _literal(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _uri(value: str) -> str:
    return "<" + value.replace(">", "%3E").replace(" ", "%20") + ">"


def _safe_class(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


# Data properties exported per class, as declared in ontology_model (name, CAIR property key, xsd type).
CLASS_DATA_PROPERTIES: dict[str, tuple[tuple[str, str, str | None], ...]] = {
    "Space": (("roomName", "roomName", None), ("roomNumber", "roomNumber", None), ("area", "area", "decimal")),
    "SteelSection": (("sectionDesignation", "sectionDesignation", None),),
    "Sheet": (("drawingCategory", "drawingCategory", None),),
}


def _data_lines(obj) -> list[str]:
    lines = []
    for name, key, datatype in CLASS_DATA_PROPERTIES.get(obj.type, ()):
        value = (obj.properties or {}).get(key)
        if value in (None, ""):
            continue
        if datatype == "decimal":
            try:
                lines.append(f'    aec:{name} "{float(value)}"^^xsd:decimal ;')
            except (TypeError, ValueError):
                continue
        else:
            lines.append(f"    aec:{name} {_literal(value)} ;")
    return lines


def snapshot_to_turtle(snapshot: CAIRSnapshot) -> str:
    lines = [PREFIXES]
    project_uri = f"aec://project/{snapshot.project_id}"
    lines.extend([
        f"{_uri(project_uri)} a aec:Project ;",
        f"    aec:projectId {_literal(snapshot.project_id)} ;",
        f"    aec:cairSnapshot {_literal(snapshot.snapshot_id)} ;",
        f"    aec:schemaVersion {_literal(snapshot.schema_version)} .",
        "",
    ])
    for obj in snapshot.objects:
        lines.extend([
            f"{_uri(obj.id)} a aec:{_safe_class(obj.type)} ;",
            f"    aec:projectId {_literal(obj.project_id)} ;",
            f"    aec:sourceHandle {_literal(obj.source.entity_id or '')} ;",
            f"    aec:sourceLayer {_literal(obj.source.layer or '')} ;",
            f"    aec:sourceFile {_literal(obj.source.file)} ;",
            *_data_lines(obj),
            f"    aec:classificationConfidence {obj.classification.confidence if obj.classification else 0.0} ;",
            f"    aec:classificationState {_literal(obj.classification.state if obj.classification else 'REQUIRES_REVIEW')} ;",
            f"    aec:hasGeometry {_uri(obj.geometry_ref or f'aec://geometry/{obj.id}')} .",
            "",
        ])
        if obj.provenance:
            lines.extend([
                f"{_uri(obj.id)} prov:wasDerivedFrom [",
                f"    prov:entity {_literal(obj.provenance.source_file)} ;",
                f"    prov:hadRole {_literal(obj.provenance.transformation)} ;",
                f"    aec:sourceHash {_literal(obj.provenance.source_hash or '')}",
                "] .",
                "",
            ])
    for relation in snapshot.relations:
        lines.append(f"{_uri(relation.subject)} aec:{_safe_class(relation.predicate)} {_uri(relation.object)} .")
    return "\n".join(lines).rstrip() + "\n"


def write_turtle(snapshot: CAIRSnapshot, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(snapshot_to_turtle(snapshot), encoding="utf-8")
    return target
