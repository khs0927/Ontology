import copy
import hashlib
import json

import pytest
from aec_intelligence.secondary_census import SCOPE, compare_reports, load_report


def report(reader="oda-ezdxf"):
    return {"schema_version": 1, "source_sha256": "a" * 64,
            "reader": reader, "reader_version": "fixture-1", "units": "mm",
            "scope": SCOPE, "verification_kind": "synthetic",
            "entities": {"LINE": 2}, "layers": {"0": 2},
            "block_definitions": {"fixture": 1}, "unsupported": {}}


def test_parity_is_not_verification_or_permission():
    result = compare_reports(report(), report("acadsharp"))
    assert result["status"] == "PARITY"
    assert result["verification_kind"] == "synthetic"
    assert result["independent_verification_proven"] is False
    assert result["canonical_allowed"] is result["execution_allowed"] is False


@pytest.mark.parametrize("field", ["entities", "layers", "block_definitions"])
def test_each_census_mismatch(field):
    secondary = report("acadsharp")
    secondary[field]["extra"] = 1
    result = compare_reports(report(), secondary)
    assert result["status"] == "MISMATCH"
    assert result["differences"][field]["extra"] == {"primary": 0, "secondary": 1}


@pytest.mark.parametrize("key,value", [("source_sha256", "b"*64), ("units", "m"),
                                      ("verification_kind", "headless_fixture")])
def test_scope_identity_must_match(key, value):
    secondary = report("acadsharp")
    secondary[key] = value
    assert compare_reports(report(), secondary)["status"] == "INCOMPARABLE"


def test_same_reader_not_independent():
    assert compare_reports(report(), report())["status"] == "INCOMPARABLE"


def test_unavailable_not_success():
    assert compare_reports(report())["status"] == "NOT_RUN"


def test_unsupported_not_parity():
    secondary = report("acadsharp")
    secondary["unsupported"] = {"PROXY": 1}
    assert compare_reports(report(), secondary)["status"] == "INCOMPLETE"


@pytest.mark.parametrize("change", [{"entities": {}}, {"entities": {"LINE": True}},
                                   {"entities": {"LINE": -1}}, {"scope": "expanded"},
                                   {"units": "unknown"}, {"schema_version": True}])
def test_invalid_reports_fail_closed(change):
    secondary = report("acadsharp") | change
    assert compare_reports(report(), secondary)["status"] == "INVALID"


def test_report_hash_is_actual_bytes_and_duplicate_keys_rejected(tmp_path):
    path = tmp_path / "report.json"
    raw = json.dumps(report()).encode()
    path.write_bytes(raw)
    loaded, digest = load_report(path)
    assert loaded == report()
    assert digest == hashlib.sha256(raw).hexdigest()
    path.write_text('{"reader":"a","reader":"b"}')
    with pytest.raises(ValueError, match="duplicate_key"):
        load_report(path)


def test_report_bound(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="report_too_large"):
        load_report(path)
