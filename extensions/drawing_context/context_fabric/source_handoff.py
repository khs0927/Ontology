"""Portable captured-byte handoffs; no host, signature or execution attestation."""
from copy import deepcopy

from .contracts import SourceRevision, digest
from .source_mapping import source_byte_revision_id
import hashlib


def build_source_handoff(source: SourceRevision, content: bytes, *, capability_id: str,
                         adapter_id: str) -> dict:
    if not isinstance(content, bytes):
        raise ValueError("Captured content must be bytes")
    for name, value in (("capability_id", capability_id), ("adapter_id", adapter_id)):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be a nonempty trimmed identifier")
    observed = hashlib.sha256(content).hexdigest()
    if observed != source.sha256:
        raise ValueError("Captured bytes do not match SourceRevision")
    payload = {
        "schema": "aec-captured-source-handoff/1",
        "source": source.to_dict(), "source_id": source.source_id,
        "source_byte_revision_id": source_byte_revision_id(source),
        "parser_revision_id": source.revision_id,
        "source_sha256": observed, "byte_count": len(content),
        "capability_id": capability_id, "adapter_id": adapter_id,
        "evidence_kind": "captured-bytes", "status": "BYTES_MATCHED",
        "acquisition_authenticated": False, "native_validation": "NOT_RUN",
        "execution_allowed": False, "canonical_allowed": False,
    }
    payload["handoff_digest"] = digest(payload)
    return payload


def validate_source_handoff(handoff: dict, source: SourceRevision, content: bytes, *,
                            capability_id: str, adapter_id: str) -> dict:
    """Recompute the complete contract; a digest is integrity, never authenticity."""
    if not isinstance(handoff, dict):
        raise ValueError("Handoff must be an object")
    expected = build_source_handoff(source, content,
                                   capability_id=capability_id, adapter_id=adapter_id)
    if handoff != expected:
        raise ValueError("Handoff identity, scope or integrity mismatch")
    return deepcopy(expected)
