"""Repository ingestion for IFC and GIS sources.

DXF has a richer preview/classification pipeline in :mod:`pipeline`.  This
module gives the other authoritative format adapters the same repository
contract: source registration, CAIR snapshot, external geometry index,
portable tables, ontology/graph exports, application exchange manifests, and
integrated validation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .application_adapters import write_application_exchange_manifest
from .cair import CAIRSnapshot, utc_now
from .formats import GISParser, IFCParser, GISParseResult, IFCParseResult, normalize_gis_to_cair, normalize_ifc_to_cair
from .graph import write_graph_exports
from .ontology import write_turtle
from .registry import ArtifactRegistry
from .rebuild import write_global_registry_parquets
from .repository import RepositoryLayout
from .storage import ArtifactRecord, ArtifactStore, LocalArtifactStore
from .tables import write_portable_tables
from .validation_engine import ProjectValidationReport, validate_project, write_validation_report


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return path


def _upsert_jsonl(path: Path, key: str, rows: list[dict[str, Any]]) -> Path:
    existing: dict[str, dict[str, Any]] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                existing[str(row[key])] = row
    for row in rows:
        existing[str(row[key])] = row
    return _write_jsonl(path, list(existing.values()))


@dataclass
class FormatIngestResult:
    project_id: str
    source_format: str
    status: str
    source_artifact: ArtifactRecord
    snapshot: CAIRSnapshot
    parse_result: IFCParseResult | GISParseResult | None
    validation: ProjectValidationReport | None
    outputs: dict[str, str]
    skipped: bool = False


class SemanticFormatIngestionPipeline:
    """Ingest non-DXF authoritative formats without converting them to DXF."""

    def __init__(self, repository_root: str | Path, store: ArtifactStore | None = None, registry: ArtifactRegistry | None = None):
        self.repository = RepositoryLayout(Path(repository_root).resolve()).ensure()
        self.store = store or LocalArtifactStore(self.repository.root)
        self.registry = registry or ArtifactRegistry(self.repository.runtime_registry_path)

    @staticmethod
    def _format_for(source: Path) -> str:
        suffix = source.suffix.lower()
        if suffix == ".ifc":
            return "IFC"
        if suffix in {".geojson", ".json"}:
            return "GIS"
        if suffix in {".gpkg", ".shp"}:
            return "GIS"
        raise ValueError(f"unsupported semantic source format: {source.suffix or '<none>'}")

    def _parse_and_normalize(self, source: Path, project_id: str, source_record: ArtifactRecord) -> tuple[str, IFCParseResult | GISParseResult, CAIRSnapshot, list[dict[str, Any]]]:
        source_format = self._format_for(source)
        if source_format == "IFC":
            parsed = IFCParser().parse(source)
            snapshot = normalize_ifc_to_cair(parsed, project_id, source_record.artifact_id, source_record.sha256)
            geometry_by_source = {str(row.get("source_id")): row for row in parsed.geometry_rows}
            geometry_rows = [
                {
                    "geometry_ref": obj.geometry_ref,
                    "source_id": obj.source.entity_id,
                    "source_format": "IFC",
                    "geometry": (geometry_by_source.get(str(obj.source.entity_id)) or {}).get("geometry"),
                    "bbox": (geometry_by_source.get(str(obj.source.entity_id)) or {}).get("bbox"),
                    "properties": (geometry_by_source.get(str(obj.source.entity_id)) or {}).get("properties", {}),
                }
                for obj in snapshot.objects if obj.geometry_ref
            ]
        else:
            parsed = GISParser().parse(source)
            snapshot = normalize_gis_to_cair(parsed, project_id, source_record.artifact_id, source_record.sha256)
            geometry_rows = parsed.geometry_index()
        return source_format, parsed, snapshot, geometry_rows

    def ingest(self, source: str | Path, project_id: str, name: str | None = None, force: bool = False) -> FormatIngestResult:
        source_path = Path(source).resolve()
        source_format = self._format_for(source_path)
        project = self.repository.project(project_id).ensure()
        manifest_name = name or source_path.stem
        source_type = "BIM/IFC" if source_format == "IFC" else ("GIS/GPKG" if source_path.suffix.lower() == ".gpkg" else "GIS/SHP" if source_path.suffix.lower() == ".shp" else "GIS/GEOJSON")
        source_record = self.store.put(source_path, project_id, source_type, format=source_format, role="RAW_SOURCE")
        self.registry.register_project(project_id, manifest_name, utc_now())
        self.registry.register_artifact(source_record)
        existing_manifest = project.read_manifest() if project.manifest_path.is_file() else None
        if not force and existing_manifest and existing_manifest.get("last_source_hash") == source_record.sha256 and existing_manifest.get("latest_cair_snapshot") and (project.path / "03_CAIR" / "project-cair.json").is_file():
            snapshot = CAIRSnapshot.from_dict(json.loads((project.path / "03_CAIR" / "project-cair.json").read_text(encoding="utf-8")))
            validation_path = project.path / "11_VALIDATION" / "cross-format" / "project-validation.json"
            validation = None
            if validation_path.is_file():
                validation = ProjectValidationReport(**json.loads(validation_path.read_text(encoding="utf-8")))
            return FormatIngestResult(project_id, source_format, "SUCCESS", source_record, snapshot, None, validation, {"cair": str(project.path / "03_CAIR" / "project-cair.json"), "project_validation": str(validation_path)}, True)

        project.write_manifest(manifest_name)
        source_format, parsed, snapshot, geometry_rows = self._parse_and_normalize(source_path, project_id, source_record)
        geometry_path = _write_jsonl(project.path / "03_CAIR" / "geometry-index.jsonl", geometry_rows)
        snapshot_path = project.path / "03_CAIR" / "project-cair.json"
        snapshot.write_json(str(snapshot_path))
        snapshot_copy = project.path / "03_CAIR" / "snapshots" / snapshot.snapshot_id / "project-cair.json"
        snapshot.write_json(str(snapshot_copy))
        table_export = write_portable_tables(snapshot, project.path / "03_CAIR" / "tables", geometry_rows=geometry_rows, artifact_rows=[source_record.to_dict()])
        cair_manifest_path = _write_json(project.path / "03_CAIR" / "cair-manifest.json", {
            "schema_version": snapshot.schema_version,
            "project_id": project_id,
            "snapshot_id": snapshot.snapshot_id,
            "status": snapshot.status,
            "object_count": len(snapshot.objects),
            "relation_count": len(snapshot.relations),
            "canonical_cair": "project-cair.json",
            "geometry_index": "geometry-index.jsonl",
            "tables": {key: str(path.name) for key, path in table_export.files.items()},
        })
        application_paths = {
            "freecad_exchange": write_application_exchange_manifest(snapshot, "FreeCAD", project.path / "06_MODELS" / "FREECAD" / "cair-exchange.json"),
            "blender_exchange": write_application_exchange_manifest(snapshot, "Blender", project.path / "06_MODELS" / "BLENDER" / "cair-exchange.json"),
            "qgis_exchange": write_application_exchange_manifest(snapshot, "QGIS", project.path / "07_GIS" / "cair-exchange.json"),
        }
        ontology_path = write_turtle(snapshot, project.path / "04_ONTOLOGY" / "project.ttl")
        graph_paths = write_graph_exports(snapshot, project.path / "05_GRAPH")
        parse_data = parsed.to_dict()
        parse_data["counts"] = {
            "entity_count": len(snapshot.objects),
            "raw_entity_count": len(parsed.entities) if isinstance(parsed, IFCParseResult) else len(parsed.features),
            "relation_count": len(snapshot.relations),
        }
        parse_report_path = _write_json(project.path / "11_VALIDATION" / "parsing" / "parse-report.json", parse_data)
        validation_status_path = _write_json(project.path / "11_VALIDATION" / "parsing" / "validation-status.json", {
            "status": snapshot.status,
            "source_format": source_format,
            "parser": getattr(parsed, "parser", source_format),
            "parser_version": getattr(parsed, "parser_version", "0.1.0"),
            "counts": parse_data["counts"],
            "warnings": snapshot.metadata.get("unmapped_ifc_types", []) if source_format == "IFC" else parsed.warnings,
        })
        output_paths: dict[str, Path] = {
            "geometry_index": geometry_path,
            "cair": snapshot_path,
            "cair_manifest": cair_manifest_path,
            "cair_snapshot": snapshot_copy,
            **{f"table_{key.replace('.', '_')}": value for key, value in table_export.files.items()},
            **application_paths,
            "ontology": ontology_path,
            **graph_paths,
            "validation": validation_status_path,
            "parse_report": parse_report_path,
        }
        derived_records: list[ArtifactRecord] = []
        seen: set[str] = set()
        for artifact_type, output_path in output_paths.items():
            record = self.store.put(output_path, project_id, f"DERIVED/{artifact_type.upper()}", relative_destination=output_path.relative_to(self.repository.root).as_posix(), source_artifact_id=source_record.artifact_id)
            self.registry.register_artifact(record)
            if record.artifact_id not in seen:
                derived_records.append(record)
                seen.add(record.artifact_id)
        manifest = project.read_manifest()
        manifest.update({
            "latest_cair_snapshot": snapshot.snapshot_id,
            "latest_ontology": str(ontology_path.relative_to(self.repository.root)),
            "latest_graph_snapshot": str(graph_paths["jsonld"].relative_to(self.repository.root)),
            "source_artifacts": list(dict.fromkeys([*manifest.get("source_artifacts", []), source_record.artifact_id])),
            "derived_artifacts": [record.artifact_id for record in derived_records],
            "last_ingest": utc_now(),
            "last_source_hash": source_record.sha256,
            "ingest_status": snapshot.status,
            "portable_table_format": table_export.format,
            "portable_table_warnings": table_export.warnings,
        })
        project.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validation = validate_project(self.repository.root, project_id)
        validation_path = write_validation_report(validation, project.path / "11_VALIDATION" / "cross-format" / "project-validation.json")
        output_paths["project_validation"] = validation_path
        validation_record = self.store.put(validation_path, project_id, "DERIVED/PROJECT_VALIDATION", relative_destination=validation_path.relative_to(self.repository.root).as_posix(), source_artifact_id=source_record.artifact_id)
        self.registry.register_artifact(validation_record)
        if validation_record.artifact_id not in seen:
            derived_records.append(validation_record)
        manifest = project.read_manifest()
        manifest.update({"derived_artifacts": [record.artifact_id for record in derived_records], "project_validation": str(validation_path.relative_to(self.repository.root)), "project_validation_status": validation.status})
        project.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._update_global_indexes(project_id, manifest_name, source_record, snapshot, validation.status)
        return FormatIngestResult(project_id, source_format, snapshot.status, source_record, snapshot, parsed, validation, {key: str(value) for key, value in output_paths.items()})

    def _update_global_indexes(self, project_id: str, name: str, source: ArtifactRecord, snapshot: CAIRSnapshot, ingest_status: str) -> None:
        global_root = self.repository.root / "global" / "00_GLOBAL"
        project_rows = [{"project_id": project_id, "name": name, "status": "ACTIVE", "last_ingest": utc_now()}]
        artifact_rows = [record.to_dict() for record in self.store.list()]
        object_rows = [{"id": obj.id, "project_id": obj.project_id, "type": obj.type, "classification": obj.classification.to_dict() if obj.classification else None, "geometry_ref": obj.geometry_ref} for obj in snapshot.objects]
        relation_rows = [{"key": f"{relation.subject}|{relation.predicate}|{relation.object}", **relation.to_dict()} for relation in snapshot.relations]
        provenance_rows = [{"object_id": obj.id, **obj.provenance.to_dict()} for obj in snapshot.objects if obj.provenance]
        _upsert_jsonl(global_root / "global-project-registry.jsonl", "project_id", project_rows)
        _upsert_jsonl(global_root / "global-artifact-registry.jsonl", "artifact_id", artifact_rows)
        _upsert_jsonl(global_root / "global-object-registry.jsonl", "id", object_rows)
        _upsert_jsonl(global_root / "global-relations.jsonl", "key", relation_rows)
        _upsert_jsonl(global_root / "global-provenance.jsonl", "object_id", provenance_rows)
        _write_json(global_root / "global-manifest.json", {
            "schema_version": "0.1.0",
            "ontology_version": "0.1.0",
            "total_projects": len(_read_jsonl(global_root / "global-project-registry.jsonl")),
            "total_objects": len(_read_jsonl(global_root / "global-object-registry.jsonl")),
            "total_artifacts": len(artifact_rows),
            "latest_global_snapshot": None,
            "project_registry": "global-project-registry.jsonl",
            "last_ingest": utc_now(),
            "last_sync": None,
            "latest_ingest_status": ingest_status,
        })
        write_global_registry_parquets(self.repository.root)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
