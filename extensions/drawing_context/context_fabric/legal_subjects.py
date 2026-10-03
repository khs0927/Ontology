from __future__ import annotations

import re
from typing import Any

from .contracts import digest

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_LIVE_KEYS = {
    "document_id",
    "session_id",
    "state_digest",
    "modification_generation",
    "execution_authorized",
    "may_execute_mutation",
}


def adapt_archontos_subject_ref(value: dict[str, Any]) -> dict[str, Any]:
    """Validate an ArchOntos legal subject reference as non-live Ontology evidence."""
    if value.get("schema") != "archontos-aec-subject-ref/1":
        raise ValueError("unsupported ArchOntos AEC subject schema")

    project_id = value.get("project_id")
    object_id = value.get("object_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    if object_id is not None and (not isinstance(object_id, str) or not object_id.strip()):
        raise ValueError("object_id must be a non-empty string when supplied")

    revision_names = (
        "source_id",
        "source_byte_revision_id",
        "parser_revision_id",
    )
    revision_values = [value.get(name) for name in revision_names]
    if any(item is not None for item in revision_values):
        if not all(isinstance(item, str) and _SHA256.fullmatch(item) for item in revision_values):
            raise ValueError(
                "source_id, source_byte_revision_id and parser_revision_id "
                "must be supplied together as lowercase SHA-256 values"
            )

    locator = value.get("locator") or {}
    if not isinstance(locator, dict):
        raise ValueError("locator must be an object")
    forbidden = sorted(_FORBIDDEN_LIVE_KEYS.intersection(locator))
    if forbidden:
        raise ValueError(
            "legal subject locator must not carry live CAD authority/state: "
            + ", ".join(forbidden)
        )

    normalized = {
        "schema": "drawing-context-legal-subject/1",
        "source_schema": "archontos-aec-subject-ref/1",
        "project_id": project_id,
        "object_id": object_id,
        "source_id": value.get("source_id"),
        "source_byte_revision_id": value.get("source_byte_revision_id"),
        "parser_revision_id": value.get("parser_revision_id"),
        "locator": locator,
        "canonical": False,
        "legal_evidence_only": True,
        "execution_authorized": False,
        "may_execute_mutation": False,
    }
    normalized["evidence_id"] = digest(normalized)
    return normalized
