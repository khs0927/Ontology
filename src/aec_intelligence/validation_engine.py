"""Repository and project validation beyond parser-local checks."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .cair import CAIRSnapshot


@dataclass
class ProjectValidationReport:
    status: str
    project_id: str | None
    scores: dict[str, float]
    checks: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "project_id": self.project_id, "scores": {key: round(value, 4) for key, value in self.scores.items()}, "checks": self.checks, "errors": self.errors, "warnings": self.warnings}


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_project(repository_root: str | Path, project_id: str) -> ProjectValidationReport:
    root = Path(repository_root).resolve()
    project = root / "projects" / project_id
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    manifest = _read_json(project / "00_MANIFEST" / "project-manifest.json")
    checks["manifest_present"] = manifest is not None
    if manifest is None:
        errors.append("project manifest is missing")
        return ProjectValidationReport("FAILED", project_id, {"manifest_score": 0.0}, checks, errors, warnings)

    cair_data = _read_json(project / "03_CAIR" / "project-cair.json")
    checks["cair_present"] = cair_data is not None
    snapshot: CAIRSnapshot | None = None
    if cair_data is None:
        errors.append("project CAIR snapshot is missing")
    else:
        try:
            snapshot = CAIRSnapshot.from_dict(cair_data)
            checks["cair_schema_version"] = snapshot.schema_version
            checks["object_count"] = len(snapshot.objects)
            checks["relation_count"] = len(snapshot.relations)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"CAIR snapshot is invalid: {exc}")

    objects = snapshot.objects if snapshot else []
    checks["global_object_ids"] = all(obj.id.startswith("aec://") for obj in objects)
    checks["confidence_bounded"] = all(obj.classification is None or 0.0 <= obj.classification.confidence <= 1.0 for obj in objects)
    checks["provenance_coverage"] = sum(bool(obj.provenance) for obj in objects) / len(objects) if objects else 0.0
    if not checks["global_object_ids"]:
        errors.append("one or more objects does not use an aec:// global id")
    if not checks["confidence_bounded"]:
        errors.append("classification confidence is outside [0,1]")
    if checks["provenance_coverage"] < 1.0:
        warnings.append("one or more CAIR objects has incomplete provenance")

    table_manifest = _read_json(project / "03_CAIR" / "tables" / "table-manifest.json")
    checks["portable_tables"] = bool(table_manifest and table_manifest.get("canonical_format") == "JSONL")
    if not checks["portable_tables"]:
        errors.append("portable CAIR table manifest is missing or not JSONL-canonical")

    geometry_rows = _read_jsonl(project / "03_CAIR" / "geometry-index.jsonl")
    if not any(row.get("geometry_ref") for row in geometry_rows):
        geometry_rows = _read_jsonl(project / "03_CAIR" / "tables" / "geometry_index.jsonl")
    geometry_refs = {str(row.get("geometry_ref")) for row in geometry_rows if row.get("geometry_ref")}
    object_geometry_refs = {str(obj.geometry_ref) for obj in objects if obj.geometry_ref}
    missing_geometry = sorted(object_geometry_refs - geometry_refs)
    checks["geometry_reference_coverage"] = 1.0 if not object_geometry_refs else (len(object_geometry_refs & geometry_refs) / len(object_geometry_refs))
    if missing_geometry and snapshot and snapshot.metadata.get("source_format") not in {"IFC", "GIS"}:
        errors.append(f"geometry index is missing {len(missing_geometry)} CAIR geometry references")
    elif missing_geometry:
        warnings.append("format adapter did not emit a local geometry index for all external geometry references")

    parse_report = _read_json(project / "11_VALIDATION" / "parsing" / "parse-report.json")
    if parse_report and isinstance(parse_report.get("counts"), dict) and snapshot:
        parsed_count = parse_report["counts"].get("entity_count")
        # Objects synthesized from sheet metadata (Sheet, Storey) have no source entity of their own.
        source_count = sum(1 for obj in snapshot.objects if not (obj.properties or {}).get("derived_by"))
        checks["parse_object_count_match"] = parsed_count == source_count
        if parsed_count != source_count:
            errors.append(f"parse entity count {parsed_count} differs from CAIR object count {source_count}")
    else:
        checks["parse_object_count_match"] = None
        warnings.append("parse report is unavailable; cross-format count check was not performed")

    expected_outputs = {
        "ontology": project / "04_ONTOLOGY" / "project.ttl",
        "jsonld": project / "05_GRAPH" / "project.jsonld",
        "freecad_exchange": project / "06_MODELS" / "FREECAD" / "cair-exchange.json",
        "blender_exchange": project / "06_MODELS" / "BLENDER" / "cair-exchange.json",
        "qgis_exchange": project / "07_GIS" / "cair-exchange.json",
    }
    checks["derived_outputs"] = {name: path.is_file() for name, path in expected_outputs.items()}
    missing_outputs = [name for name, present in checks["derived_outputs"].items() if not present]
    if missing_outputs:
        warnings.append("missing derived outputs: " + ", ".join(missing_outputs))

    runtime_outputs = {
        "blender_scene": any((project / "06_MODELS" / "BLENDER").glob("cair-derived-scene-*.blend")),
        "blender_glb": any((project / "02_DERIVED" / "BIM" / "GLB").glob("cair-derived-scene-*.glb")),
        "blender_visual_qa": any((project / "02_DERIVED" / "PREVIEWS").glob("cair-derived-scene-*.png")),
        "freecad_scene": any((project / "06_MODELS" / "FREECAD").glob("cair-derived-scene-*.FCStd")),
        "freecad_step": any((project / "02_DERIVED" / "BIM" / "STEP").glob("cair-derived-scene-*.step")),
    }
    checks["application_runtime_outputs"] = runtime_outputs
    blender_runtime = {key: runtime_outputs[key] for key in ("blender_scene", "blender_glb", "blender_visual_qa")}
    freecad_runtime = {key: runtime_outputs[key] for key in ("freecad_scene", "freecad_step")}
    if any(blender_runtime.values()) and not all(blender_runtime.values()):
        warnings.append("Blender runtime outputs are incomplete; scene, GLB, and visual QA preview must be produced together")
    if any(freecad_runtime.values()) and not all(freecad_runtime.values()):
        warnings.append("FreeCAD runtime outputs are incomplete; FCStd and STEP must be produced together")

    scores = {
        "manifest_score": 1.0,
        "cair_score": 1.0 if snapshot else 0.0,
        "provenance_score": float(checks["provenance_coverage"]),
        "geometry_score": float(checks["geometry_reference_coverage"]),
        "derived_output_score": sum(checks["derived_outputs"].values()) / len(checks["derived_outputs"]),
    }
    if errors:
        status = "FAILED"
    elif warnings:
        status = "SUCCESS_WITH_WARNINGS"
    else:
        status = "SUCCESS"
    return ProjectValidationReport(status, project_id, scores, checks, errors, warnings)


def validate_repository(repository_root: str | Path) -> ProjectValidationReport:
    root = Path(repository_root).resolve()
    project_ids = sorted(path.name for path in (root / "projects").iterdir() if path.is_dir()) if (root / "projects").is_dir() else []
    reports = [validate_project(root, project_id) for project_id in project_ids]
    errors = [f"{report.project_id}: {error}" for report in reports for error in report.errors]
    warnings = [f"{report.project_id}: {warning}" for report in reports for warning in report.warnings]
    scores = {
        "project_count": float(len(reports)),
        "project_success_rate": sum(report.status == "SUCCESS" for report in reports) / len(reports) if reports else 0.0,
        "average_provenance_score": sum(report.scores.get("provenance_score", 0.0) for report in reports) / len(reports) if reports else 0.0,
    }
    checks = {"project_reports": [report.to_dict() for report in reports], "project_count": len(reports)}
    status = "FAILED" if errors else ("SUCCESS_WITH_WARNINGS" if warnings else "SUCCESS")
    return ProjectValidationReport(status, None, scores, checks, errors, warnings)


def write_validation_report(report: ProjectValidationReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
