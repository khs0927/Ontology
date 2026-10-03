from __future__ import annotations

from copy import deepcopy
import re
from pathlib import PurePosixPath
from typing import Any

from .contracts import digest

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_VIEWS = {"plan", "section", "elevation", "rcp", "model"}
_ALLOWED_FORMATS = {"dxf", "svg", "ifc", "pdf"}


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("derived artifact relative_path is required")
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if path.is_absolute() or ":" in path.parts[0] or ".." in path.parts:
        raise ValueError("derived artifact path must stay inside the export root")
    return normalized


def adapt_sketcharch_export_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate SketchArch drawing exports as derived, noncanonical evidence."""
    if manifest.get("schema") != "sketcharch-drawing-export/1":
        raise ValueError("unsupported SketchArch drawing export schema")
    if manifest.get("ontology_ingest_mode") != "DERIVED_ARTIFACT":
        raise ValueError("SketchArch exports must use DERIVED_ARTIFACT ingest mode")
    if manifest.get("canonical_mutation") is not False:
        raise ValueError("derived exports must not request canonical mutation")

    project_id = manifest.get("project_id")
    fingerprint = manifest.get("source_model_fingerprint")
    revision = manifest.get("source_model_revision")
    artifacts = manifest.get("artifacts")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id is required")
    if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
        raise ValueError("source_model_fingerprint must be lowercase SHA-256")
    if not isinstance(revision, str) or not revision:
        raise ValueError("source_model_revision is required")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("at least one derived artifact is required")

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("derived artifact entries must be objects")
        artifact_id = item.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id or artifact_id in seen_ids:
            raise ValueError("derived artifact IDs must be non-empty and unique")
        seen_ids.add(artifact_id)

        if item.get("derived") is not True:
            raise ValueError("SketchArch artifact must be explicitly derived")
        if item.get("source_model_fingerprint") != fingerprint:
            raise ValueError("artifact source model fingerprint mismatch")
        if item.get("view_kind") not in _ALLOWED_VIEWS:
            raise ValueError("unsupported SketchArch view kind")
        if item.get("format") not in _ALLOWED_FORMATS:
            raise ValueError("unsupported SketchArch export format")
        sha256 = item.get("sha256")
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise ValueError("artifact sha256 must be lowercase SHA-256")
        relative_path = _safe_relative_path(item.get("relative_path"))
        path_key = relative_path.casefold()
        if path_key in seen_paths:
            raise ValueError("derived artifact paths must be unique")
        seen_paths.add(path_key)
        if item.get("units") != "mm":
            raise ValueError("SketchArch drawing export units must be mm")

        rows.append(
            {
                "schema": "drawing-context-derived-artifact/1",
                "id": digest(
                    [
                        "sketcharch-drawing-export/1",
                        project_id,
                        fingerprint,
                        revision,
                        artifact_id,
                        sha256,
                    ]
                ),
                "artifact_id": artifact_id,
                "project_id": project_id,
                "source_model_fingerprint": fingerprint,
                "source_model_revision": revision,
                "view_kind": item["view_kind"],
                "format": item["format"],
                "relative_path": relative_path,
                "sha256": sha256,
                "source_element_ids": deepcopy(item.get("source_element_ids") or []),
                "units": "mm",
                "derived": True,
                "canonical": False,
                "execution_authorized": False,
            }
        )

    return {
        "schema": "drawing-context-derived-artifacts/1",
        "source_schema": "sketcharch-drawing-export/1",
        "project_id": project_id,
        "source_model_fingerprint": fingerprint,
        "source_model_revision": revision,
        "records": rows,
        "canonical_mutation": False,
        "execution_authorized": False,
    }
