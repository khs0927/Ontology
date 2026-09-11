"""Portable structured tables derived from CAIR.

JSONL is the always-available canonical interchange.  When the optional
PyArrow dependency is installed, Parquet files are emitted as a derived
acceleration format; they never replace the canonical JSONL rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
from typing import Any, Iterable

from .cair import CAIRSnapshot


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "objects": (
        "id", "project_id", "type", "source_file", "source_format", "source_entity_id",
        "source_layer", "artifact_id", "geometry_ref", "bbox_json", "placement_json",
        "properties_json", "classification_json", "provenance_json", "version_json",
    ),
    "relations": ("subject", "predicate", "object", "confidence", "provenance_json"),
    "classifications": ("object_id", "label", "confidence", "method", "state", "evidence_json"),
    "provenance": (
        "object_id", "source_file", "source_object", "source_format", "source_hash",
        "parser", "parser_version", "timestamp", "agent", "transformation", "derived_from_json",
    ),
    "properties": ("object_id", "property_name", "value_json"),
    "source_mappings": ("global_object_id", "project_id", "source_format", "source_entity_id", "source_file", "artifact_id"),
    "application_mappings": ("global_object_id", "project_id", "application", "application_object_id", "source_snapshot", "mapping_status"),
    "geometry_index": ("geometry_ref", "source_entity_id", "geometry_type", "geometry_json", "bbox_json", "crs"),
    "artifacts": ("artifact_id", "project_id", "artifact_type", "filename", "local_path", "sha256", "size", "status", "version", "metadata_json"),
}


@dataclass
class PortableTableExport:
    directory: Path
    files: dict[str, Path]
    row_counts: dict[str, int]
    format: str = "JSONL"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1.0",
            "format": self.format,
            "canonical_format": "JSONL",
            "directory": self.directory.as_posix(),
            "tables": {
                name: {"columns": list(TABLE_COLUMNS[name]), "row_count": self.row_counts[name], "files": [str(path.name) for key, path in self.files.items() if key.startswith((f"{name}.", f"{name.replace('_', '-')}."))]}
                for name in self.row_counts
            },
            "warnings": self.warnings,
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text("".join(_json(row) + "\n" for row in rows), encoding="utf-8")


def _object_rows(snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    rows = []
    for obj in snapshot.objects:
        rows.append({
            "id": obj.id,
            "project_id": obj.project_id,
            "type": obj.type,
            "source_file": obj.source.file,
            "source_format": obj.source.format,
            "source_entity_id": obj.source.entity_id,
            "source_layer": obj.source.layer,
            "artifact_id": obj.source.artifact_id,
            "geometry_ref": obj.geometry_ref,
            "bbox_json": _json(obj.bbox),
            "placement_json": _json(obj.placement),
            "properties_json": _json(obj.properties),
            "classification_json": _json(obj.classification.to_dict()) if obj.classification else None,
            "provenance_json": _json(obj.provenance.to_dict()) if obj.provenance else None,
            "version_json": _json(obj.version.to_dict()),
        })
    return rows


def _classification_rows(snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    return [
        {"object_id": obj.id, "label": obj.classification.label, "confidence": obj.classification.confidence, "method": obj.classification.method, "state": obj.classification.state, "evidence_json": _json(obj.classification.evidence)}
        for obj in snapshot.objects if obj.classification
    ]


def _provenance_rows(snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    return [
        {"object_id": obj.id, "source_file": obj.provenance.source_file, "source_object": obj.provenance.source_object, "source_format": obj.provenance.source_format, "source_hash": obj.provenance.source_hash, "parser": obj.provenance.parser, "parser_version": obj.provenance.parser_version, "timestamp": obj.provenance.timestamp, "agent": obj.provenance.agent, "transformation": obj.provenance.transformation, "derived_from_json": _json(obj.provenance.derived_from)}
        for obj in snapshot.objects if obj.provenance
    ]


def _property_rows(snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for obj in snapshot.objects:
        for name, value in sorted(obj.properties.items()):
            rows.append({"object_id": obj.id, "property_name": str(name), "value_json": _json(value)})
    return rows


def _source_mapping_rows(snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    return [
        {
            "global_object_id": obj.id,
            "project_id": obj.project_id,
            "source_format": obj.source.format,
            "source_entity_id": obj.source.entity_id,
            "source_file": obj.source.file,
            "artifact_id": obj.source.artifact_id,
        }
        for obj in snapshot.objects
    ]


def _application_mapping_rows(snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    """Persist a truthful cross-application mapping scaffold.

    The exchange manifest carries the CAIR identity into each application. A
    native object identifier is only filled after a live application export
    reports one; the initial table therefore records the reference-only state
    instead of inventing a FreeCAD/Blender/QGIS object ID.
    """

    return [
        {
            "global_object_id": obj.id,
            "project_id": obj.project_id,
            "application": application,
            "application_object_id": None,
            "source_snapshot": snapshot.snapshot_id,
            "mapping_status": "CAIR_REFERENCE_ONLY",
        }
        for application in ("FreeCAD", "Blender", "QGIS")
        for obj in snapshot.objects
    ]


def _relation_rows(snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    return [
        {"subject": relation.subject, "predicate": relation.predicate, "object": relation.object, "confidence": relation.confidence, "provenance_json": _json(relation.provenance)}
        for relation in snapshot.relations
    ]


def _geometry_rows(rows: Iterable[dict[str, Any]], snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        geometry_ref = row.get("geometry_ref")
        if geometry_ref is None and row.get("handle") is not None:
            geometry_ref = f"aec://artifact/geometry/{row['handle']}"
        if geometry_ref is None and row.get("feature_id") is not None:
            geometry_ref = f"gis://feature/{row['feature_id']}"
        geometry = row.get("geometry")
        result.append({
            "geometry_ref": geometry_ref,
            "source_entity_id": row.get("handle") or row.get("source_entity_id") or row.get("source_id") or row.get("feature_id"),
            "geometry_type": (geometry or {}).get("kind") if isinstance(geometry, dict) else row.get("geometry_type"),
            "geometry_json": _json(geometry) if geometry is not None else None,
            "bbox_json": _json(row.get("bbox")) if row.get("bbox") is not None else None,
            "crs": row.get("crs") or snapshot.metadata.get("crs"),
        })
    return result


def _artifact_rows(artifacts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for artifact in artifacts:
        rows.append({
            "artifact_id": artifact.get("artifact_id"),
            "project_id": artifact.get("project_id"),
            "artifact_type": artifact.get("artifact_type"),
            "filename": artifact.get("filename"),
            "local_path": artifact.get("local_path"),
            "sha256": artifact.get("sha256"),
            "size": artifact.get("size"),
            "status": artifact.get("status"),
            "version": artifact.get("version"),
            "metadata_json": _json(artifact.get("metadata") or {}),
        })
    return rows


def _write_parquet(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    arrays = {column: [row.get(column) for row in rows] for column in columns}
    pq.write_table(pa.table(arrays), path)


def write_portable_tables(
    snapshot: CAIRSnapshot,
    directory: str | Path,
    geometry_rows: Iterable[dict[str, Any]] = (),
    artifact_rows: Iterable[dict[str, Any]] = (),
) -> PortableTableExport:
    """Write normalized CAIR tables and a manifest with explicit formats."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    table_rows = {
        "objects": _object_rows(snapshot),
        "relations": _relation_rows(snapshot),
        "classifications": _classification_rows(snapshot),
        "provenance": _provenance_rows(snapshot),
        "properties": _property_rows(snapshot),
        "source_mappings": _source_mapping_rows(snapshot),
        "application_mappings": _application_mapping_rows(snapshot),
        "geometry_index": _geometry_rows(geometry_rows, snapshot),
        "artifacts": _artifact_rows(artifact_rows),
    }
    files: dict[str, Path] = {}
    for name, rows in table_rows.items():
        path = root / f"{name}.jsonl"
        _write_jsonl(path, rows)
        files[f"{name}.jsonl"] = path

    warnings: list[str] = []
    parquet_available = False
    try:
        import pyarrow  # noqa: F401
        parquet_available = True
    except ImportError:
        warnings.append("PyArrow is not installed; JSONL remains the canonical portable table format")
    if parquet_available:
        for name, rows in table_rows.items():
            path = root / f"{name}.parquet"
            _write_parquet(path, rows, TABLE_COLUMNS[name])
            files[f"{name}.parquet"] = path

    # The extension specification uses hyphenated names for these interchange
    # tables. Keep the existing underscored table API and publish exact-name
    # aliases so Drive consumers can use either convention.
    for name in ("source_mappings", "application_mappings", "geometry_index"):
        for suffix in ("jsonl", "parquet"):
            source = root / f"{name}.{suffix}"
            if source.is_file():
                alias = root / f"{name.replace('_', '-')}.{suffix}"
                shutil.copyfile(source, alias)
                files[f"{name.replace('_', '-')}.{suffix}"] = alias

    export = PortableTableExport(root, files, {name: len(rows) for name, rows in table_rows.items()}, "JSONL+PARQUET" if parquet_available else "JSONL", warnings)
    manifest_path = root / "table-manifest.json"
    manifest_path.write_text(json.dumps(export.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files["manifest.json"] = manifest_path
    return export
