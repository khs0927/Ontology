"""End-to-end first-milestone DXF → CAIR pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .cair import CAIRRelation, CAIRSnapshot, utc_now
from .spatial_relations import build_spatial_relations
from .classifier import refine_with_context
from .application_adapters import write_application_exchange_manifest
from .classifier import to_cair_object
from .dxf import DXFParser
from .graph import write_graph_exports
from .ontology import write_turtle
from .registry import ArtifactRegistry
from .repository import RepositoryLayout
from .rebuild import write_global_registry_parquets
from .storage import ArtifactRecord, ArtifactStore, LocalArtifactStore
from .tables import write_portable_tables
from .validation import ValidationReport, validate_dxf_ingest
from .validation_engine import validate_project, write_validation_report


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
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                existing[str(row[key])] = row
    for row in rows:
        existing[str(row[key])] = row
    return _write_jsonl(path, list(existing.values()))


def render_svg(result: Any, path: Path) -> Path:
    """Create a deterministic lightweight preview for visual validation."""

    boxes = [entity.bbox for entity in result.entities if entity.bbox]
    extents = result.extents
    min_x = extents.get("min_x", min((box["min_x"] for box in boxes), default=0.0))
    min_y = extents.get("min_y", min((box["min_y"] for box in boxes), default=0.0))
    max_x = extents.get("max_x", max((box["max_x"] for box in boxes), default=100.0))
    max_y = extents.get("max_y", max((box["max_y"] for box in boxes), default=100.0))
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    margin = max(width, height) * 0.04
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x-margin} {-max_y-margin} {width+2*margin} {height+2*margin}" width="1200" height="800">', '<g fill="none" stroke="#1f2937" stroke-width="0.8">']
    for entity in result.entities:
        geometry = entity.geometry
        kind = geometry.get("kind")
        if kind == "line":
            start, end = geometry.get("start", [0, 0]), geometry.get("end", [0, 0])
            lines.append(f'<line x1="{start[0]}" y1="{-start[1]}" x2="{end[0]}" y2="{-end[1]}" />')
        elif kind == "polyline":
            points = " ".join(f"{point[0]},{-point[1]}" for point in geometry.get("points", []))
            if points:
                tag = "polygon" if geometry.get("closed") else "polyline"
                lines.append(f'<{tag} points="{points}" />')
        elif kind in {"circle", "arc"}:
            center = geometry.get("center", [0, 0])
            lines.append(f'<circle cx="{center[0]}" cy="{-center[1]}" r="{geometry.get("radius", 0)}" />')
    lines.extend(["</g>", "</svg>"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@dataclass
class DXFIngestResult:
    project_id: str
    source_artifact: ArtifactRecord
    snapshot: CAIRSnapshot
    validation: ValidationReport
    outputs: dict[str, str]


class DXFIngestionPipeline:
    def __init__(self, repository_root: str | Path, store: ArtifactStore | None = None, registry: ArtifactRegistry | None = None):
        self.repository = RepositoryLayout(Path(repository_root).resolve()).ensure()
        self.store = store or LocalArtifactStore(self.repository.root)
        self.registry = registry or ArtifactRegistry(self.repository.runtime_registry_path)

    def ingest(self, source: str | Path, project_id: str, name: str | None = None, force: bool = False) -> DXFIngestResult:
        source_path = Path(source).resolve()
        project = self.repository.project(project_id).ensure()
        manifest_name = name or source_path.stem
        self.registry.register_project(project_id, manifest_name, utc_now())
        source_record = self.store.put(source_path, project_id, "CAD/DXF", format="DXF", role="RAW_SOURCE")
        self.registry.register_artifact(source_record)
        existing_manifest = None
        if project.manifest_path.exists():
            existing_manifest = project.read_manifest()
        if not force and existing_manifest and existing_manifest.get("last_source_hash") == source_record.sha256 and existing_manifest.get("latest_cair_snapshot") and (project.path / "03_CAIR" / "project-cair.json").is_file():
            return self._load_existing_result(project, source_record, existing_manifest)
        project.write_manifest(manifest_name)

        parser = DXFParser()
        parsed = parser.parse(source_path)
        geometry_path = project.path / "03_CAIR" / "geometry-index.jsonl"
        _write_jsonl(geometry_path, [entity.to_dict() for entity in parsed.entities])
        objects = [
            to_cair_object(
                entity,
                project_id,
                source_record.local_path,
                source_record.sha256,
                source_record.artifact_id,
                parser.name,
                parser.version,
                f"aec://artifact/{source_record.artifact_id}/geometry/{entity.handle}",
            )
            for entity in parsed.entities
        ]
        refine_with_context(objects, parsed.entities)
        # Geometric relations also complete objects in place (area, section designation), so run before registering.
        derived_objects, spatial_relations = build_spatial_relations(objects, parsed.entities, project_id, parsed.sheet, parsed.units)
        for obj in objects + derived_objects:
            self.registry.register_object(obj.id, obj.project_id, obj.type, obj.classification.to_dict() if obj.classification else None, obj.geometry_ref)
        relations = [
            CAIRRelation(f"aec://project/{project_id}", "containsElement", obj.id, obj.classification.confidence if obj.classification else 1.0)
            for obj in objects
        ]
        relations.extend(spatial_relations)
        snapshot = CAIRSnapshot(
            project_id=project_id,
            objects=objects + derived_objects,
            relations=relations,
            metadata={"parser": parser.name, "parser_version": parser.version, "parse": parsed.to_dict()},
        )
        cair_path = project.path / "03_CAIR" / "project-cair.json"
        snapshot.write_json(str(cair_path))
        snapshot_dir = project.path / "03_CAIR" / "snapshots" / snapshot.snapshot_id
        snapshot_copy = snapshot_dir / "project-cair.json"
        snapshot.write_json(str(snapshot_copy))
        table_export = write_portable_tables(
            snapshot,
            project.path / "03_CAIR" / "tables",
            geometry_rows=[{**entity.to_dict(), "geometry_ref": f"aec://artifact/{source_record.artifact_id}/geometry/{entity.handle}"} for entity in parsed.entities],
            artifact_rows=[record.to_dict() for record in self.store.list(project_id)],
        )
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
        application_exchange_paths = {
            "freecad_exchange": write_application_exchange_manifest(snapshot, "FreeCAD", project.path / "06_MODELS" / "FREECAD" / "cair-exchange.json"),
            "blender_exchange": write_application_exchange_manifest(snapshot, "Blender", project.path / "06_MODELS" / "BLENDER" / "cair-exchange.json"),
            "qgis_exchange": write_application_exchange_manifest(snapshot, "QGIS", project.path / "07_GIS" / "cair-exchange.json"),
        }
        ontology_path = write_turtle(snapshot, project.path / "04_ONTOLOGY" / "project.ttl")
        graph_paths = write_graph_exports(snapshot, project.path / "05_GRAPH")
        preview_path = render_svg(parsed, project.path / "02_DERIVED" / "CAD" / "SVG" / f"{source_path.stem}.svg")
        validation = validate_dxf_ingest(parsed, objects, snapshot, preview_path.is_file())
        validation_path = _write_json(project.path / "11_VALIDATION" / "parsing" / "validation-status.json", validation.to_dict())
        parse_report_path = _write_json(project.path / "11_VALIDATION" / "parsing" / "parse-report.json", parsed.to_dict())

        output_paths = {
            "geometry_index": geometry_path,
            "cair": cair_path,
            "cair_manifest": cair_manifest_path,
            "cair_snapshot": snapshot_copy,
            **{f"table_{key.replace('.', '_')}": value for key, value in table_export.files.items()},
            **application_exchange_paths,
            "ontology": ontology_path,
            **graph_paths,
            "preview": preview_path,
            "validation": validation_path,
            "parse_report": parse_report_path,
        }
        derived_records = []
        seen_derived_ids: set[str] = set()
        for artifact_type, output_path in output_paths.items():
            relative = output_path.relative_to(self.repository.root).as_posix()
            record = self.store.put(output_path, project_id, f"DERIVED/{artifact_type.upper()}", relative_destination=relative, source_artifact_id=source_record.artifact_id)
            self.registry.register_artifact(record)
            if record.artifact_id not in seen_derived_ids:
                derived_records.append(record)
                seen_derived_ids.add(record.artifact_id)

        manifest = project.read_manifest()
        manifest.update({
            "latest_cair_snapshot": snapshot.snapshot_id,
            "latest_ontology": str(ontology_path.relative_to(self.repository.root)),
            "source_artifacts": list(dict.fromkeys([*manifest.get("source_artifacts", []), source_record.artifact_id])),
            "derived_artifacts": [record.artifact_id for record in derived_records],
            "last_ingest": utc_now(),
            "last_source_hash": source_record.sha256,
            "ingest_status": validation.status,
            "portable_table_format": table_export.format,
            "portable_table_warnings": table_export.warnings,
        })
        project.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        project_validation = validate_project(self.repository.root, project_id)
        project_validation_path = write_validation_report(project_validation, project.path / "11_VALIDATION" / "cross-format" / "project-validation.json")
        output_paths["project_validation"] = project_validation_path
        project_validation_record = self.store.put(project_validation_path, project_id, "DERIVED/PROJECT_VALIDATION", relative_destination=project_validation_path.relative_to(self.repository.root).as_posix(), source_artifact_id=source_record.artifact_id)
        self.registry.register_artifact(project_validation_record)
        if project_validation_record.artifact_id not in seen_derived_ids:
            derived_records.append(project_validation_record)
        manifest = project.read_manifest()
        manifest.update({
            "derived_artifacts": [record.artifact_id for record in derived_records],
            "project_validation": str(project_validation_path.relative_to(self.repository.root)),
            "project_validation_status": project_validation.status,
        })
        project.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._update_global_indexes(project_id, manifest_name, source_record, objects, relations, validation)
        return DXFIngestResult(project_id, source_record, snapshot, validation, {key: str(value) for key, value in output_paths.items()})

    def _load_existing_result(self, project: Any, source_record: ArtifactRecord, manifest: dict[str, Any]) -> DXFIngestResult:
        project_path = project.path
        cair_path = project_path / "03_CAIR" / "project-cair.json"
        ontology_path = project_path / "04_ONTOLOGY" / "project.ttl"
        preview_path = next((project_path / "02_DERIVED" / "CAD" / "SVG").glob("*.svg"), project_path / "02_DERIVED" / "CAD" / "SVG" / "preview.svg")
        validation_path = project_path / "11_VALIDATION" / "parsing" / "validation-status.json"
        parse_report_path = project_path / "11_VALIDATION" / "parsing" / "parse-report.json"
        snapshot = json.loads(cair_path.read_text(encoding="utf-8"))
        validation_data = json.loads(validation_path.read_text(encoding="utf-8"))
        validation = ValidationReport(**validation_data)
        table_outputs = {f"table_{path.name.replace('.', '_')}": path for path in (project_path / "03_CAIR" / "tables").iterdir()} if (project_path / "03_CAIR" / "tables").is_dir() else {}
        application_exchange_paths = {
            "freecad_exchange": project_path / "06_MODELS" / "FREECAD" / "cair-exchange.json",
            "blender_exchange": project_path / "06_MODELS" / "BLENDER" / "cair-exchange.json",
            "qgis_exchange": project_path / "07_GIS" / "cair-exchange.json",
        }
        application_exchange_paths = {key: path for key, path in application_exchange_paths.items() if path.is_file()}
        project_validation_path = project_path / "11_VALIDATION" / "cross-format" / "project-validation.json"
        if project_validation_path.is_file():
            application_exchange_paths["project_validation"] = project_validation_path
        return DXFIngestResult(
            project.project_id, source_record, CAIRSnapshot.from_dict(snapshot), validation,
            {"geometry_index": str(project_path / "03_CAIR" / "geometry-index.jsonl"), "cair": str(cair_path), "cair_snapshot": str(project_path / "03_CAIR" / "snapshots" / manifest["latest_cair_snapshot"] / "project-cair.json"), **{key: str(path) for key, path in table_outputs.items()}, **{key: str(path) for key, path in application_exchange_paths.items()}, "ontology": str(ontology_path), "jsonld": str(project_path / "05_GRAPH" / "project.jsonld"), "nodes": str(project_path / "05_GRAPH" / "nodes.jsonl"), "relationships": str(project_path / "05_GRAPH" / "relationships.jsonl"), "preview": str(preview_path), "validation": str(validation_path), "parse_report": str(parse_report_path)},
        )

    def _update_global_indexes(self, project_id: str, name: str, source: ArtifactRecord, objects: list[Any], relations: list[Any], validation: ValidationReport) -> None:
        global_root = self.repository.root / "global" / "00_GLOBAL"
        project_rows = [{"project_id": project_id, "name": name, "status": "ACTIVE", "last_ingest": utc_now()}]
        artifact_rows = [record.to_dict() for record in self.store.list()]
        object_rows = [{"id": obj.id, "project_id": obj.project_id, "type": obj.type, "classification": obj.classification.to_dict() if obj.classification else None, "geometry_ref": obj.geometry_ref} for obj in objects]
        relation_rows = [{"key": f"{relation.subject}|{relation.predicate}|{relation.object}", **relation.to_dict()} for relation in relations]
        provenance_rows = [{"object_id": obj.id, **obj.provenance.to_dict()} for obj in objects if obj.provenance]
        _upsert_jsonl(global_root / "global-project-registry.jsonl", "project_id", project_rows)
        _upsert_jsonl(global_root / "global-artifact-registry.jsonl", "artifact_id", artifact_rows)
        _upsert_jsonl(global_root / "global-object-registry.jsonl", "id", object_rows)
        _upsert_jsonl(global_root / "global-relations.jsonl", "key", relation_rows)
        _upsert_jsonl(global_root / "global-provenance.jsonl", "object_id", provenance_rows)
        project_count = len((global_root / "global-project-registry.jsonl").read_text(encoding="utf-8").splitlines())
        object_count = len((global_root / "global-object-registry.jsonl").read_text(encoding="utf-8").splitlines())
        _write_json(global_root / "global-manifest.json", {
            "schema_version": "0.1.0",
            "ontology_version": "0.1.0",
            "total_projects": project_count,
            "total_objects": object_count,
            "total_artifacts": len(artifact_rows),
            "latest_global_snapshot": None,
            "project_registry": "global-project-registry.jsonl",
            "last_ingest": utc_now(),
            "last_sync": None,
            "latest_ingest_status": validation.status,
        })
        write_global_registry_parquets(self.repository.root)
