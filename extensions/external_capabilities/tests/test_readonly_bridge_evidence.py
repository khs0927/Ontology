from datetime import datetime, timezone
import hashlib

import pytest

from capability_registry.history import EvidenceHistory
from readonly_bridges.evidence import (
    CONTRACT_CAPABILITY_ID,
    CONTRACT_HOST,
    CONTRACT_HOST_VERSION,
    record_readonly_probe_evidence,
)


NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def projection(status="DECLARED", **changes):
    row = {
        "identity": {
            "provider_id": "rhino",
            "upstream_repo": "https://example.test/rhino-bridge",
            "upstream_commit": "a" * 40,
            "source_path": "bridge/probe.json",
            "adapter_version": "1",
        },
        "payload_sha256": hashlib.sha256(b"fixture").hexdigest(),
        "status": status,
        "verification_kind": "contract_only",
        "execution_allowed": False,
        "canonical_allowed": False,
        "native_mapping_verified": False,
    }
    row.update(changes)
    return row


def test_declared_contract_is_recorded_as_headless_only(tmp_path):
    history = EvidenceHistory(tmp_path / "evidence.db")
    record = record_readonly_probe_evidence(
        history, "probe-1", projection(),
        run_url="https://example.test/run/1",
        run_timestamp="2026-10-05T08:00:00Z",
        valid_until="2026-10-06T08:00:00Z",
    )
    assert record["capability_id"] == CONTRACT_CAPABILITY_ID
    assert record["host"] == CONTRACT_HOST
    assert record["host_version"] == CONTRACT_HOST_VERSION
    assert record["verification_kind"] == "headless"
    assert record["outcome"] == "PASS"
    result = history.project(scope=record, now=NOW)
    assert result["status"] == "TESTED"
    assert result["execution_allowed"] is False
    assert result["canonical_allowed"] is False
    history.close()


def test_not_run_stays_not_run_and_never_becomes_tested(tmp_path):
    history = EvidenceHistory(tmp_path / "evidence.db")
    row = projection("NOT_RUN", payload_sha256=None, transport_state="timeout")
    record = record_readonly_probe_evidence(
        history, "probe-timeout", row,
        run_url="https://example.test/run/2",
        run_timestamp="2026-10-05T08:00:00Z",
        valid_until="2026-10-06T08:00:00Z",
    )
    assert record["outcome"] == "NOT_RUN"
    result = history.project(scope=record, now=NOW)
    assert result["status"] == "UNKNOWN"
    assert result["observations"][0]["outcome"] == "NOT_RUN"
    assert result["execution_allowed"] is False
    history.close()


@pytest.mark.parametrize("status", ["VERIFIED", "ERROR", "PASS", None])
def test_unsupported_projection_status_cannot_enter_ledger(tmp_path, status):
    history = EvidenceHistory(tmp_path / "evidence.db")
    with pytest.raises(ValueError):
        record_readonly_probe_evidence(
            history, "probe-1", projection(status),
            run_url="https://example.test/run/3",
            run_timestamp="2026-10-05T08:00:00Z",
            valid_until="2026-10-06T08:00:00Z",
        )
    history.close()


def test_contract_pass_cannot_claim_native_host_scope(tmp_path):
    history = EvidenceHistory(tmp_path / "evidence.db")
    row = projection(host="Rhino", host_version="8", native_mapping_verified=True,
                     execution_allowed=True, canonical_allowed=True)
    record = record_readonly_probe_evidence(
        history, "probe-1", row,
        run_url="https://example.test/run/4",
        run_timestamp="2026-10-05T08:00:00Z",
        valid_until="2026-10-06T08:00:00Z",
    )
    assert record["host"] == "headless-contract"
    assert record["host_version"] == "1"
    result = history.project(scope=record, now=NOW)
    assert result["status"] == "TESTED"
    assert result["execution_allowed"] is False
    assert result["canonical_allowed"] is False
    history.close()
