"""Guardrails that check every implementation phase against the AEC framework."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .repository import RepositoryLayout
from .storage import LocalArtifactStore
from .ontology_alignment import ONTOLOGY_FILES
from .shacl import shacl_available, validate_turtle_file


@dataclass
class ComplianceReport:
    status: str
    checks: dict[str, bool]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": self.checks, "errors": self.errors, "warnings": self.warnings, "notes": self.notes}


def audit_repository(repository_root: str | Path) -> ComplianceReport:
    root = Path(repository_root).resolve()
    layout = RepositoryLayout(root)
    checks: dict[str, bool] = {}
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    checks["cair_schema_present"] = (root / "global" / "schemas" / "cair" / "cair-v0.1.schema.json").is_file()
    checks["cair_table_schema_present"] = (root / "global" / "schemas" / "cair" / "cair-tables-v0.1.schema.json").is_file()
    checks["ontology_alignment_present"] = all((root / "global" / "ontology" / relative).is_file() for relative in ONTOLOGY_FILES)
    checks["ontology_alignment_manifest_present"] = (root / "global" / "ontology" / "alignment-manifest.json").is_file()
    checks["runtime_separate_from_canonical"] = not (root / "global" / "neo4j" / "data").exists() and not (root / "global" / "oxigraph" / "data").exists()
    checks["global_manifest_present"] = layout.global_manifest_path.is_file()
    if not checks["cair_schema_present"]:
        errors.append("CAIR v0.1 schema is missing")
    if not checks["cair_table_schema_present"]:
        errors.append("portable CAIR table schema is missing")
    if not checks["ontology_alignment_present"]:
        errors.append("one or more external ontology alignment files are missing")
    if not checks["ontology_alignment_manifest_present"]:
        errors.append("ontology alignment manifest is missing")
    if not checks["runtime_separate_from_canonical"]:
        errors.append("live database data must not be placed in the canonical repository")

    project_dirs = [path for path in (root / "projects").glob("*") if path.is_dir()] if (root / "projects").is_dir() else []
    checks["project_manifests_present"] = all((path / "00_MANIFEST" / "project-manifest.json").is_file() for path in project_dirs)
    if not checks["project_manifests_present"]:
        errors.append("one or more projects has no manifest")
    checks["portable_tables_present"] = all((path / "03_CAIR" / "tables" / "table-manifest.json").is_file() for path in project_dirs)
    if not checks["portable_tables_present"] and project_dirs:
        warnings.append("one or more projects predates the portable CAIR table export")
    checks["application_exchange_manifests_present"] = all(
        (path / "06_MODELS" / "FREECAD" / "cair-exchange.json").is_file()
        and (path / "06_MODELS" / "BLENDER" / "cair-exchange.json").is_file()
        and (path / "07_GIS" / "cair-exchange.json").is_file()
        for path in project_dirs
    )
    if not checks["application_exchange_manifests_present"] and project_dirs:
        warnings.append("one or more projects predates application exchange manifests")

    ontology_exports = [path / "04_ONTOLOGY" / "project.ttl" for path in project_dirs if (path / "04_ONTOLOGY" / "project.ttl").is_file()]
    if ontology_exports and shacl_available():
        nonconforming = []
        for export in ontology_exports:
            shacl_report = validate_turtle_file(export, max_violations=1)
            if not shacl_report.conforms:
                first = shacl_report.violations[0] if shacl_report.violations else {}
                nonconforming.append(f"{export.parent.parent.name} ({shacl_report.violation_count} violations; first: {first.get('focus_node', '')} {first.get('path', '')})")
        checks["ontology_exports_shacl_conformant"] = not nonconforming
        errors.extend(f"CAIR ontology export fails SHACL shapes: {item}" for item in nonconforming)
    elif ontology_exports:
        notes.append("SHACL validation skipped: install the 'shacl' extra (pyshacl)")

    store = LocalArtifactStore(root)
    records = store.list()
    checks["artifact_ids_unique"] = len({record.artifact_id for record in records}) == len(records)
    checks["raw_artifacts_not_overwritten"] = all(record.status in {"ACTIVE", "ARCHIVED", "DEPRECATED", "DELETED_PENDING"} for record in records)
    checks["artifact_security_classification_bounded"] = all(record.security_classification in {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"} for record in records)
    if not checks["artifact_ids_unique"]:
        errors.append("artifact registry contains duplicate IDs")
    if not checks["raw_artifacts_not_overwritten"]:
        errors.append("artifact registry contains an invalid lifecycle state")
    if not checks["artifact_security_classification_bounded"]:
        errors.append("artifact registry contains an invalid security classification")

    object_files = list((root / "global" / "00_GLOBAL").glob("global-object-registry.jsonl"))
    objects = []
    if object_files:
        objects = [json.loads(line) for line in object_files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    checks["object_ids_are_global"] = all(str(row.get("id", "")).startswith("aec://") for row in objects)
    checks["classification_confidence_is_bounded"] = all(0 <= float((row.get("classification") or {}).get("confidence", 0)) <= 1 for row in objects)
    if objects and not checks["object_ids_are_global"]:
        errors.append("one or more CAIR object IDs is not a global aec:// identifier")
    if objects and not checks["classification_confidence_is_bounded"]:
        errors.append("one or more classification confidence values is outside [0,1]")
    if not objects:
        warnings.append("no global objects found; run an ingest before production use")
    return ComplianceReport("FAIL" if errors else ("PASS_WITH_WARNINGS" if warnings else "PASS"), checks, errors, warnings, notes)
