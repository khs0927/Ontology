"""Bridge contract outcomes into the append-only evidence lifecycle.

This records only headless contract verification. It never upgrades a provider to
native verification, authenticated acquisition, canonical authority or execution.
"""
from datetime import datetime

from capability_registry import evidence_record
from capability_registry.history import EvidenceHistory


CONTRACT_CAPABILITY_ID = "readonly-bridge-contract"
CONTRACT_SCOPE = "headless-contract/1"
CONTRACT_HOST = "headless-contract"
CONTRACT_HOST_VERSION = "1"


def _time(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Evidence timestamps require timezone")
    return stamp


def record_readonly_probe_evidence(history: EvidenceHistory, identifier: str, projection: dict, *,
                                   run_url: str, run_timestamp: str, valid_until: str):
    """Append one readonly-bridge contract outcome to EvidenceHistory.

    DECLARED means only that the headless contract accepted the projection.
    NOT_RUN stays NOT_RUN. No other status is promoted.
    """
    if not isinstance(history, EvidenceHistory):
        raise ValueError("Require EvidenceHistory")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Require evidence identifier")
    if not isinstance(projection, dict):
        raise ValueError("Require readonly probe projection")

    identity = projection.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("Missing probe identity")
    for key in ("provider_id", "upstream_commit", "adapter_version"):
        if not isinstance(identity.get(key), str) or not identity[key].strip():
            raise ValueError("Incomplete probe identity")

    status = projection.get("status")
    if status == "DECLARED":
        outcome = "PASS"
    elif status == "NOT_RUN":
        outcome = "NOT_RUN"
    else:
        raise ValueError("Unsupported probe status for evidence lifecycle")

    fixture_sha256 = projection.get("payload_sha256")
    if outcome == "PASS" and (not isinstance(fixture_sha256, str) or len(fixture_sha256) != 64):
        raise ValueError("Accepted contract evidence requires payload hash")
    if outcome == "NOT_RUN" and fixture_sha256 is None:
        # Use an explicit synthetic fixture identity for transport-only NOT_RUN evidence.
        import hashlib
        fixture_sha256 = hashlib.sha256(
            f"readonly-bridge:{identity['provider_id']}:{projection.get('transport_state','not-run')}".encode()
        ).hexdigest()

    _time(run_timestamp)
    _time(valid_until)
    record = evidence_record(
        capability_id=CONTRACT_CAPABILITY_ID,
        provider_id=identity["provider_id"],
        upstream_commit=identity["upstream_commit"],
        host=CONTRACT_HOST,
        host_version=CONTRACT_HOST_VERSION,
        adapter_version=identity["adapter_version"],
        fixture_sha256=fixture_sha256,
        verification_kind="headless",
        outcome=outcome,
        run_url=run_url,
        run_timestamp=run_timestamp,
        valid_until=valid_until,
    )
    history.add(identifier, record)
    return record
