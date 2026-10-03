"""Source-to-live-CAD identity mapping built on the existing drawing-context contract.

This module deliberately stops at SOURCE_BOUND. It never returns
EXECUTION_AUTHORIZED: approval, single-writer coordination, transaction-time
revalidation and receipts belong to the executor (Power CAD/Sion routing layer).
"""

from __future__ import annotations

from dataclasses import dataclass
import ntpath
from typing import Any

from .contracts import SourceRevision, digest, verify_live_candidate


def source_byte_revision_id(source: SourceRevision) -> str:
    """Identity of captured source bytes, intentionally independent of parser/version."""
    return digest([source.source_id, source.revision, source.sha256])


def normalize_native_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("native path must be non-empty")
    return ntpath.normcase(ntpath.normpath(value.strip()))


@dataclass(frozen=True)
class TrustedSourceResolution:
    source_id: str
    source_byte_revision_id: str
    resolved_path: str
    resolved_sha256: str
    resolver_id: str
    resolver_issuer: str
    trust_domain: str
    signature_key_id: str
    receipt_signature_verified: bool
    cache_entry_id: str
    resolver_receipt_sha256: str
    immutable_cache: bool
    resolved_at: str

    def __post_init__(self):
        for name in (
            "source_id",
            "source_byte_revision_id",
            "resolved_path",
            "resolved_sha256",
            "resolver_id",
            "resolver_issuer",
            "trust_domain",
            "signature_key_id",
            "cache_entry_id",
            "resolver_receipt_sha256",
            "resolved_at",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"TrustedSourceResolution.{name} must be non-empty")
        normalize_native_path(self.resolved_path)
        if len(self.resolver_receipt_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.resolver_receipt_sha256):
            raise ValueError("resolver_receipt_sha256 must be lowercase SHA-256")
        if self.receipt_signature_verified is not True:
            raise ValueError("source resolution requires a verified resolver receipt signature")
        if self.immutable_cache is not True:
            raise ValueError("source resolution must point to an immutable cache entry")


@dataclass(frozen=True)
class LiveObjectObservation:
    session_id: str
    document_id: str
    native_path: str
    source_id: str
    source_byte_revision_id: str
    file_sha256: str
    state_digest: str
    modification_generation: str
    revision: str
    sha256: str
    layout: str
    handle: str
    instance_path: Any
    native_mapping_verified: bool
    document_dirty: bool
    units: str
    fingerprint: str

    def __post_init__(self):
        for name in (
            "session_id",
            "document_id",
            "native_path",
            "source_id",
            "source_byte_revision_id",
            "file_sha256",
            "state_digest",
            "modification_generation",
            "revision",
            "sha256",
            "layout",
            "handle",
            "units",
            "fingerprint",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"LiveObjectObservation.{name} must be non-empty")
        normalize_native_path(self.native_path)

    def guard_input(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "revision": self.revision,
            "sha256": self.sha256,
            "layout": self.layout,
            "handle": self.handle,
            "instance_path": self.instance_path,
            "native_mapping_verified": self.native_mapping_verified,
            "document_dirty": self.document_dirty,
            "units": self.units,
            "fingerprint": self.fingerprint,
        }


def _record_identity_reasons(record: dict[str, Any], source: SourceRevision) -> list[str]:
    reasons: list[str] = []
    if record.get("source") != source.to_dict():
        reasons.append("record_source_revision_mismatch")
    locator = record.get("locator")
    if not isinstance(locator, dict):
        return reasons + ["missing_locator"]
    if locator.get("source_id") != source.source_id:
        reasons.append("locator_source_id_mismatch")
    if locator.get("revision_id") != source.revision_id:
        reasons.append("locator_parser_revision_mismatch")
    return reasons


def candidate_binding(record: dict[str, Any], source: SourceRevision) -> dict[str, Any]:
    """Create a discovery-stage identity report without claiming live binding."""
    reasons = _record_identity_reasons(record, source)
    locator = record.get("locator") if isinstance(record.get("locator"), dict) else {}
    return {
        "schema": "aec-source-live-binding/1",
        "binding_state": "CANDIDATE",
        "candidate_id": record.get("id"),
        "source_id": source.source_id,
        "source_byte_revision_id": source_byte_revision_id(source),
        "parser_revision_id": source.revision_id,
        "locator": {
            "layout": locator.get("layout"),
            "handle": locator.get("handle"),
            "instance_path": locator.get("instance_path"),
        },
        "reasons": reasons,
        "may_execute_mutation": False,
        "execution_authorized": False,
        "requires_executor_authorization": True,
        "canonical": False,
    }


def build_executor_handoff(binding_report: dict[str, Any]) -> dict[str, Any]:
    """Create a bounded, non-authorizing handoff for the native CAD executor.

    Only a clean SOURCE_BOUND report that is VERIFIED_FOR_REVIEW can cross this
    boundary. The handoff is provenance, not an execution token: Power CAD must
    still bind the live document, re-read target fingerprints inside its write
    transaction, obtain approval and emit its own receipt.
    """
    if binding_report.get("schema") != "aec-source-live-binding/1":
        raise ValueError("unsupported source binding report schema")
    if binding_report.get("binding_state") != "SOURCE_BOUND":
        raise ValueError("executor handoff requires SOURCE_BOUND")
    review = binding_report.get("review_guard")
    if not isinstance(review, dict) or review.get("status") != "VERIFIED_FOR_REVIEW":
        raise ValueError("executor handoff requires VERIFIED_FOR_REVIEW")
    if binding_report.get("execution_authorized") is not False:
        raise ValueError("source binding report must not authorize execution")

    live_document = binding_report.get("live_document")
    live_object = binding_report.get("live_object")
    resolver = binding_report.get("resolver")
    if not all(isinstance(value, dict) for value in (live_document, live_object, resolver)):
        raise ValueError("source binding report is missing live/resolver evidence")

    payload = {
        "schema": "aec-executor-handoff/1",
        "binding_state": "SOURCE_BOUND",
        "review_status": "VERIFIED_FOR_REVIEW",
        "document_id": live_document.get("document_id"),
        "session_id": live_document.get("session_id"),
        "source_id": binding_report.get("source_id"),
        "source_byte_revision_id": binding_report.get("source_byte_revision_id"),
        "parser_revision_id": binding_report.get("parser_revision_id"),
        "source_sha256": binding_report.get("source_sha256"),
        "file_sha256": live_document.get("file_sha256"),
        "candidate_id": binding_report.get("candidate_id"),
        "native_path": live_document.get("native_path"),
        "state_digest": live_document.get("state_digest"),
        "modification_generation": live_document.get("modification_generation"),
        "units": live_document.get("units"),
        "resolver_receipt_sha256": resolver.get("resolver_receipt_sha256"),
        "cache_entry_id": resolver.get("cache_entry_id"),
        "object_locator": {
            "layout": live_object.get("layout"),
            "handle": live_object.get("handle"),
            "instance_path": live_object.get("instance_path"),
            "fingerprint": live_object.get("fingerprint"),
        },
        "execution_authorized": False,
        "may_execute_mutation": False,
        "requires_executor_authorization": True,
    }
    required = (
        "document_id",
        "session_id",
        "source_id",
        "source_byte_revision_id",
        "parser_revision_id",
        "source_sha256",
        "file_sha256",
        "candidate_id",
        "native_path",
        "state_digest",
        "modification_generation",
        "units",
        "resolver_receipt_sha256",
        "cache_entry_id",
    )
    if any(not isinstance(payload[name], str) or not payload[name] for name in required):
        raise ValueError("source binding report is incomplete for executor handoff")
    locator = payload["object_locator"]
    if not isinstance(locator["layout"], str) or not locator["layout"]:
        raise ValueError("executor handoff requires a layout")
    if not isinstance(locator["handle"], str) or not locator["handle"]:
        raise ValueError("executor handoff requires a handle")
    if not isinstance(locator["fingerprint"], str) or not locator["fingerprint"]:
        raise ValueError("executor handoff requires a live fingerprint")

    payload["handoff_digest"] = digest(payload)
    return payload


def build_execution_evidence(
    handoff: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Validate a native executor receipt against the original source handoff.

    The result is immutable evidence material for later persistence/projection.
    It does not mutate canonical CAIR or grant any further execution authority.
    """
    if handoff.get("schema") != "aec-executor-handoff/1":
        raise ValueError("unsupported executor handoff schema")
    if handoff.get("binding_state") != "SOURCE_BOUND":
        raise ValueError("execution evidence requires SOURCE_BOUND handoff")
    if handoff.get("execution_authorized") is not False:
        raise ValueError("executor handoff must remain non-authorizing")

    if receipt.get("committed") is not True or receipt.get("dry_run") is not False:
        raise ValueError("execution evidence requires a committed non-dry-run receipt")

    required_receipt = (
        "executor",
        "plan_id",
        "document_id",
        "source_binding_handoff_digest",
        "source_id",
        "source_byte_revision_id",
        "parser_revision_id",
    )
    if any(not isinstance(receipt.get(name), str) or not receipt.get(name) for name in required_receipt):
        raise ValueError("executor receipt is incomplete for source-linked evidence")

    comparisons = {
        "document_id": "document_id",
        "source_binding_handoff_digest": "handoff_digest",
        "source_id": "source_id",
        "source_byte_revision_id": "source_byte_revision_id",
        "parser_revision_id": "parser_revision_id",
    }
    mismatches = [
        receipt_name
        for receipt_name, handoff_name in comparisons.items()
        if receipt.get(receipt_name) != handoff.get(handoff_name)
    ]
    if mismatches:
        raise ValueError(
            "executor receipt does not match source handoff: " + ", ".join(sorted(mismatches))
        )

    receipt_digest = digest(receipt)
    return {
        "schema": "aec-execution-evidence/1",
        "evidence_state": "EXECUTED",
        "executor": receipt["executor"],
        "plan_id": receipt["plan_id"],
        "document_id": receipt["document_id"],
        "source_id": handoff["source_id"],
        "source_byte_revision_id": handoff["source_byte_revision_id"],
        "parser_revision_id": handoff["parser_revision_id"],
        "candidate_id": handoff.get("candidate_id"),
        "handoff_digest": handoff["handoff_digest"],
        "receipt_digest": receipt_digest,
        "committed": True,
        "canonical_mutation": False,
        "execution_authorized": False,
        "note": (
            "Validated executor evidence only. Persisting or promoting this evidence "
            "into canonical project state requires a separate repository/application policy."
        ),
    }


def verify_source_binding(
    record: dict[str, Any],
    source: SourceRevision,
    resolution: TrustedSourceResolution,
    live: LiveObjectObservation,
) -> dict[str, Any]:
    """Verify read-only source identity and live object mapping.

    SOURCE_BOUND means the native observation is linked to the captured source
    revision and object locator. It does not mean the current live state is safe
    to mutate; the existing verify_live_candidate result is returned separately.
    """
    reasons = _record_identity_reasons(record, source)
    byte_revision = source_byte_revision_id(source)

    if resolution.source_id != source.source_id:
        reasons.append("resolver_source_id_mismatch")
    if resolution.source_byte_revision_id != byte_revision:
        reasons.append("resolver_source_byte_revision_mismatch")
    if resolution.resolved_sha256 != source.sha256:
        reasons.append("resolver_file_hash_mismatch")

    if live.source_id != source.source_id:
        reasons.append("live_source_id_mismatch")
    if live.source_byte_revision_id != byte_revision:
        reasons.append("live_source_byte_revision_mismatch")
    if live.file_sha256 != source.sha256 or live.sha256 != source.sha256:
        reasons.append("live_file_hash_mismatch")
    if normalize_native_path(live.native_path) != normalize_native_path(resolution.resolved_path):
        reasons.append("native_path_mismatch")

    locator = record.get("locator") if isinstance(record.get("locator"), dict) else {}
    if locator.get("layout") != live.layout:
        reasons.append("live_layout_mismatch")
    if locator.get("handle") != live.handle:
        reasons.append("live_handle_mismatch")
    if locator.get("instance_path") != live.instance_path:
        reasons.append("live_instance_path_mismatch")
    if live.native_mapping_verified is not True:
        reasons.append("native_mapping_not_verified")

    review = verify_live_candidate(record, live.guard_input())
    bound = not reasons
    return {
        "schema": "aec-source-live-binding/1",
        "binding_state": "SOURCE_BOUND" if bound else "CANDIDATE",
        "candidate_id": record.get("id"),
        "source_id": source.source_id,
        "source_byte_revision_id": byte_revision,
        "parser_revision_id": source.revision_id,
        "source_sha256": source.sha256,
        "resolver": {
            "resolver_id": resolution.resolver_id,
            "resolver_issuer": resolution.resolver_issuer,
            "trust_domain": resolution.trust_domain,
            "signature_key_id": resolution.signature_key_id,
            "receipt_signature_verified": resolution.receipt_signature_verified,
            "cache_entry_id": resolution.cache_entry_id,
            "resolver_receipt_sha256": resolution.resolver_receipt_sha256,
            "immutable_cache": resolution.immutable_cache,
            "resolved_path": resolution.resolved_path,
            "resolved_sha256": resolution.resolved_sha256,
            "resolved_at": resolution.resolved_at,
        },
        "live_document": {
            "session_id": live.session_id,
            "document_id": live.document_id,
            "native_path": live.native_path,
            "file_sha256": live.file_sha256,
            "state_digest": live.state_digest,
            "modification_generation": live.modification_generation,
            "document_dirty": live.document_dirty,
            "units": live.units,
        },
        "live_object": {
            "layout": live.layout,
            "handle": live.handle,
            "instance_path": live.instance_path,
            "fingerprint": live.fingerprint,
        },
        "reasons": sorted(set(reasons)),
        "review_guard": review,
        "may_execute_mutation": False,
        "execution_authorized": False,
        "requires_executor_authorization": True,
        "canonical": False,
        "note": (
            "SOURCE_BOUND proves read-only identity linkage only. The executor must "
            "revalidate current document/object state inside its single-writer "
            "transaction and issue a separate authorization/receipt."
        ),
    }
