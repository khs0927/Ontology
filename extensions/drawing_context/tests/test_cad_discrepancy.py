from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from context_fabric.cad_discrepancy import (
    ExternalCadProbeConfig,
    HandleMappingEvidence,
    compare_censuses,
    normalize_census,
    run_external_census,
)


SOURCE_SHA = "a" * 64


def payload(
    *,
    geometry_x=1.0,
    layer="WAL1",
    layout="Model",
    include_layout=True,
    blocks=None,
    unsupported=None,
):
    entity = {
        "handle": "1A",
        "entity_type": "LINE",
        "layer": layer,
        "geometry": {"start": [geometry_x, 2.0, 0.0], "end": [3.0, 4.0, 0.0]},
        "properties": {},
    }
    if include_layout:
        entity["layout"] = layout
    value = {
        "schema": "all-in-cad-headless-census/v1",
        "entity_count": 1,
        "entities": [entity],
    }
    if blocks is not None:
        value["blocks"] = blocks
    if unsupported is not None:
        value["unsupported"] = unsupported
    return value


def census(
    provider,
    *,
    provider_version="1",
    source_sha=SOURCE_SHA,
    elapsed_ms=10.0,
    peak_rss_mb=20.0,
    handle_space="unknown",
    **payload_kwargs,
):
    return normalize_census(
        payload(**payload_kwargs),
        provider=provider,
        provider_version=provider_version,
        source_sha256=source_sha,
        handle_space=handle_space,
        elapsed_ms=elapsed_ms,
        peak_rss_mb=peak_rss_mb,
    )


def mapping(**kwargs):
    values = {
        "source_sha256": SOURCE_SHA,
        "mapping": {"1A": "1A"},
        "verified": True,
        "evidence_id": "mapping-evidence-1",
        "method": "native-source-to-converted-handle-fixture",
    }
    values.update(kwargs)
    return HandleMappingEvidence(**values)


def test_equal_comparable_censuses_match_without_claiming_truth():
    left = census("acadsharp", handle_space="source-dwg")
    right = census("ezdxf", handle_space="converted-dxf")

    report = compare_censuses(left, right)

    assert report["status"] == "MATCH"
    assert report["truth_oracle"] is False
    assert report["winner"] is None
    assert report["correctness_score"] is None
    assert report["canonical_mutation"] is False
    assert report["object_level_comparison"] == "SKIPPED_NO_VERIFIED_HANDLE_MAPPING"
    assert "BLOCK_COMPARISON_NOT_AVAILABLE" in report["coverage_gaps"]


def test_different_source_bytes_are_incomparable():
    left = census("acadsharp")
    right = census("ezdxf", source_sha="b" * 64)

    report = compare_censuses(left, right)

    assert report["status"] == "INCOMPARABLE"
    assert report["reasons"] == ["SOURCE_HASH_MISMATCH"]
    assert report["discrepancies"] == []
    assert report["truth_oracle"] is False


def test_entity_and_layer_census_differences_are_reported_not_scored():
    left = census("acadsharp")
    right_payload = {
        "schema": "all-in-cad-headless-census/v1",
        "entities": [
            {
                "handle": "2B",
                "entity_type": "CIRCLE",
                "layer": "COL",
                "layout": "Model",
                "geometry": {"center": [0.0, 0.0, 0.0], "radius": 10.0},
                "properties": {},
            },
            {
                "handle": "2C",
                "entity_type": "CIRCLE",
                "layer": "COL",
                "layout": "Model",
                "geometry": {"center": [20.0, 0.0, 0.0], "radius": 10.0},
                "properties": {},
            },
        ],
    }
    right = normalize_census(
        right_payload,
        provider="ezdxf",
        provider_version="1",
        source_sha256=SOURCE_SHA,
    )

    report = compare_censuses(left, right)
    codes = {row["code"] for row in report["discrepancies"]}

    assert report["status"] == "DISCREPANCIES"
    assert "ENTITY_COUNT_MISMATCH" in codes
    assert "ENTITY_TYPE_COUNTS_MISMATCH" in codes
    assert "LAYER_COUNTS_MISMATCH" in codes
    assert report["winner"] is None


def test_layout_mismatch_is_compared_only_when_both_lanes_have_full_layout_coverage():
    left = census("acadsharp", layout="Model")
    right = census("ezdxf", layout="A101")
    report = compare_censuses(left, right)
    assert "LAYOUT_COUNTS_MISMATCH" in {row["code"] for row in report["discrepancies"]}

    no_layout = census("ezdxf", include_layout=False)
    report = compare_censuses(left, no_layout)
    assert "LAYOUT_COMPARISON_NOT_FULLY_AVAILABLE" in report["coverage_gaps"]
    assert "LAYOUT_COUNTS_MISMATCH" not in {row["code"] for row in report["discrepancies"]}


def test_block_nested_and_xref_differences_are_review_findings():
    left_blocks = [
        {
            "name": "DOOR",
            "is_xref": False,
            "entity_count": 2,
            "entity_count_by_type": {"LINE": 2},
            "nested_reference_counts": {},
        }
    ]
    right_blocks = [
        {
            "name": "DOOR",
            "is_xref": True,
            "entity_count": 3,
            "entity_count_by_type": {"LINE": 2, "INSERT": 1},
            "nested_reference_counts": {"FRAME": 1},
        }
    ]
    report = compare_censuses(
        census("acadsharp", blocks=left_blocks),
        census("ezdxf", blocks=right_blocks),
    )

    assert report["status"] == "DISCREPANCIES"
    assert "BLOCK_CATALOG_MISMATCH" in {row["code"] for row in report["discrepancies"]}


def test_unsupported_proxy_differences_are_explicit_when_both_lanes_report_them():
    report = compare_censuses(
        census("acadsharp", unsupported=["ACAD_PROXY_ENTITY"]),
        census("ezdxf", unsupported=[]),
    )

    assert "UNSUPPORTED_OBJECTS_MISMATCH" in {
        row["code"] for row in report["discrepancies"]
    }


def test_geometry_difference_is_not_claimed_without_verified_handle_mapping():
    left = census("acadsharp", geometry_x=1.0, handle_space="source-dwg")
    right = census("ezdxf", geometry_x=9.0, handle_space="converted-dxf")

    report = compare_censuses(left, right)

    assert report["status"] == "MATCH"
    assert report["object_level_comparison"] == "SKIPPED_NO_VERIFIED_HANDLE_MAPPING"
    assert "MAPPED_GEOMETRY_DIGEST_MISMATCH" not in {
        row["code"] for row in report["discrepancies"]
    }


def test_verified_mapping_allows_object_geometry_discrepancy_detection():
    left = census("acadsharp", geometry_x=1.0, handle_space="source-dwg")
    right = census("ezdxf", geometry_x=9.0, handle_space="converted-dxf")

    report = compare_censuses(left, right, handle_mapping=mapping())

    assert report["status"] == "DISCREPANCIES"
    assert report["object_level_comparison"] == "VERIFIED_MAPPING_USED"
    assert report["mapped_pairs_compared"] == 1
    assert "MAPPED_GEOMETRY_DIGEST_MISMATCH" in {
        row["code"] for row in report["discrepancies"]
    }
    assert report["truth_oracle"] is False


def test_unverified_or_wrong_source_handle_mapping_is_rejected():
    with pytest.raises(ValueError, match="independently verified"):
        mapping(verified=False)

    with pytest.raises(ValueError, match="source hash"):
        compare_censuses(
            census("acadsharp"),
            census("ezdxf"),
            handle_mapping=mapping(source_sha256="b" * 64),
        )


def test_mapped_missing_entity_is_a_discrepancy():
    report = compare_censuses(
        census("acadsharp"),
        census("ezdxf"),
        handle_mapping=mapping(mapping={"1A": "FFFF"}),
    )

    assert "MAPPED_ENTITY_MISSING" in {
        row["code"] for row in report["discrepancies"]
    }


def test_cost_observations_do_not_change_correctness_status():
    left = census("acadsharp", elapsed_ms=1000, peak_rss_mb=500)
    right = census("ezdxf", elapsed_ms=10, peak_rss_mb=20)

    report = compare_censuses(left, right)

    assert report["status"] == "MATCH"
    assert report["cost_observations"]["left"]["elapsed_ms"] == 1000.0
    assert report["cost_observations"]["right"]["elapsed_ms"] == 10.0
    assert report["correctness_score"] is None


def test_external_probe_is_disabled_without_running_subprocess(monkeypatch, tmp_path):
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not run")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_external_census(
        tmp_path / "missing.dwg",
        ExternalCadProbeConfig(enabled=False),
    )
    assert result["status"] == "DISABLED"
    assert called is False


def test_external_probe_is_run_without_shell_and_bound_to_local_source_hash(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "fixture.dwg"
    source.write_bytes(b"dwg-fixture")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload()),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = ExternalCadProbeConfig(
        enabled=True,
        command_prefix=("dotnet", "AllInCad.ACadSharpProbe.dll"),
        provider="acadsharp",
        provider_version="3.7.1",
        adapter_commit="957c1f8e1d1e212f830325f08de2d7543efe1c2d",
        timeout_seconds=30,
        handle_space="source-dwg",
    )
    result = run_external_census(source, config)

    assert result["status"] == "SUCCESS"
    assert captured["shell"] is False
    assert captured["command"][-1] == str(source.resolve())
    assert result["source_sha256"] == result["census"]["source_sha256"]
    assert result["census"]["provider_version"] == "3.7.1"
    assert result["census"]["adapter_commit"] == config.adapter_commit
    assert result["census"]["canonical"] is False


def test_external_probe_failure_is_fail_closed(monkeypatch, tmp_path):
    source = tmp_path / "fixture.dwg"
    source.write_bytes(b"dwg-fixture")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="bad dwg")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = ExternalCadProbeConfig(
        enabled=True,
        command_prefix=("probe",),
        provider_version="3.7.1",
    )

    with pytest.raises(RuntimeError, match="bad dwg"):
        run_external_census(source, config)
