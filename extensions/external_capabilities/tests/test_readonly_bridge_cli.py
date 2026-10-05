import json
from datetime import datetime, timezone

from capability_registry.history import EvidenceHistory
from readonly_bridges.cli import main
from readonly_bridges.evidence import CONTRACT_CAPABILITY_ID


NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def projection():
    return {
        "identity": {
            "provider_id": "rhino",
            "upstream_repo": "https://example.test/rhino-bridge",
            "upstream_commit": "a" * 40,
            "source_path": "bridge/probe.json",
            "adapter_version": "1",
        },
        "payload_sha256": "b" * 64,
        "status": "DECLARED",
        "verification_kind": "contract_only",
        "execution_allowed": False,
        "canonical_allowed": False,
        "native_mapping_verified": False,
    }


def test_runtime_cli_records_projection_into_ledger(tmp_path, capsys):
    projection_path = tmp_path / "projection.json"
    projection_path.write_text(json.dumps(projection()), encoding="utf-8")
    ledger = tmp_path / "evidence.db"

    rc = main([
        "record",
        "--projection", str(projection_path),
        "--ledger", str(ledger),
        "--id", "cli-probe-1",
        "--run-url", "https://example.test/run/cli-1",
        "--run-timestamp", "2026-10-05T08:00:00Z",
        "--valid-until", "2026-10-06T08:00:00Z",
    ])
    assert rc == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["recorded"] is True
    assert emitted["capability_id"] == CONTRACT_CAPABILITY_ID
    assert emitted["outcome"] == "PASS"
    assert emitted["verification_kind"] == "headless"
    assert emitted["execution_allowed"] is False
    assert emitted["canonical_allowed"] is False

    history = EvidenceHistory(ledger)
    rows, _ = history._read()
    assert len(rows) == 1
    assert rows[0]["record"]["capability_id"] == CONTRACT_CAPABILITY_ID
    assert rows[0]["record"]["host"] == "headless-contract"
    history.close()


def test_runtime_cli_rejects_self_promoted_status(tmp_path):
    row = projection()
    row["status"] = "VERIFIED"
    projection_path = tmp_path / "projection.json"
    projection_path.write_text(json.dumps(row), encoding="utf-8")

    try:
        main([
            "record",
            "--projection", str(projection_path),
            "--ledger", str(tmp_path / "evidence.db"),
            "--id", "bad",
            "--run-url", "https://example.test/run/bad",
            "--run-timestamp", "2026-10-05T08:00:00Z",
            "--valid-until", "2026-10-06T08:00:00Z",
        ])
    except ValueError:
        pass
    else:
        raise AssertionError("self-promoted status must fail closed")
