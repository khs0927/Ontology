from __future__ import annotations

from copy import deepcopy

import pytest

from context_fabric.compare import (
    compare_provider_runs,
    fixture_digest,
    wrap_provider_result,
)


def fixture():
    return {
        "schema": "drawing-context-rag-benchmark/1",
        "provider": "neutral",
        "canonical_mutation": False,
        "projection": [{"external_id": "ctx-1"}],
        "cases": [{"case_id": "q1", "query": "door"}],
    }


def metrics(
    *,
    recall=1.0,
    mrr=1.0,
    p95=100.0,
    provenance=1.0,
    unauthorized=0,
    stale=0,
):
    return {
        "schema": "drawing-context-rag-benchmark-result/1",
        "k": 5,
        "case_count": 1,
        "hit_count": 1,
        "recall_at_k": recall,
        "mrr": mrr,
        "provenance_metadata_coverage": provenance,
        "unauthorized_source_leakage": unauthorized,
        "stale_revision_leakage": stale,
        "duplicate_hits": 0,
        "latency": {"p50_ms": p95, "p95_ms": p95},
        "cases": [
            {
                "case_id": "q1",
                "recall_at_k": recall,
                "reciprocal_rank": mrr,
                "hit_count": 1,
                "provenance_metadata_coverage": provenance,
                "unauthorized_source_leakage": unauthorized,
                "stale_revision_leakage": stale,
            }
        ],
        "canonical_mutation": False,
    }


def run(provider, result, *, f=None):
    return wrap_provider_result(provider, f or fixture(), result)


def test_fixture_digest_is_stable_for_key_order():
    a = fixture()
    b = {
        "cases": a["cases"],
        "projection": a["projection"],
        "canonical_mutation": False,
        "provider": "neutral",
        "schema": a["schema"],
    }
    assert fixture_digest(a) == fixture_digest(b)


def test_security_failed_provider_is_never_selected_even_with_better_quality():
    ragflow = run("ragflow", metrics(recall=0.9, mrr=0.8, p95=150))
    light = run(
        "lightrag",
        metrics(recall=1.0, mrr=1.0, p95=50, unauthorized=1),
    )
    report = compare_provider_runs([ragflow, light])
    assert report["status"] == "SELECTED"
    assert report["selected_provider"] == "ragflow"
    light_row = next(row for row in report["providers"] if row["provider"] == "lightrag")
    assert light_row["promotion_status"] == "BLOCKED"


def test_recall_then_mrr_then_latency_are_used_without_weighted_score():
    f = fixture()
    report = compare_provider_runs([
        run("a", metrics(recall=0.9, mrr=0.7, p95=100), f=f),
        run("b", metrics(recall=0.8, mrr=1.0, p95=10), f=f),
    ])
    assert report["selected_provider"] == "a"
    assert report["deciding_metric"] == "recall_at_k"

    report = compare_provider_runs([
        run("a", metrics(recall=0.9, mrr=0.8, p95=100), f=f),
        run("b", metrics(recall=0.9, mrr=0.7, p95=10), f=f),
    ])
    assert report["selected_provider"] == "a"
    assert report["deciding_metric"] == "mrr"

    report = compare_provider_runs([
        run("a", metrics(recall=0.9, mrr=0.8, p95=100), f=f),
        run("b", metrics(recall=0.9, mrr=0.8, p95=80), f=f),
    ])
    assert report["selected_provider"] == "b"
    assert report["deciding_metric"] == "p95_ms"


def test_missing_latency_keeps_quality_tie_instead_of_penalizing_provider():
    a = metrics(recall=0.9, mrr=0.8)
    b = metrics(recall=0.9, mrr=0.8)
    a["latency"]["p95_ms"] = None
    report = compare_provider_runs([run("a", a), run("b", b)])
    assert report["status"] == "TIE"
    assert report["selected_provider"] is None
    assert report["tied_providers"] == ["a", "b"]


def test_fixture_mismatch_is_rejected_before_comparison():
    other = deepcopy(fixture())
    other["cases"][0]["query"] = "window"
    with pytest.raises(ValueError, match="exact same benchmark fixture"):
        compare_provider_runs([
            run("ragflow", metrics(), f=fixture()),
            run("lightrag", metrics(), f=other),
        ])


def test_case_or_k_mismatch_is_rejected_even_if_wrapper_is_manually_tampered():
    a = run("ragflow", metrics())
    b = run("lightrag", metrics())
    b["metrics"]["k"] = 10
    with pytest.raises(ValueError, match="identical k"):
        compare_provider_runs([a, b])

    b = run("lightrag", metrics())
    b["metrics"]["cases"][0]["case_id"] = "q2"
    with pytest.raises(ValueError, match="ordered case ids"):
        compare_provider_runs([a, b])


def test_no_provider_is_selected_when_all_fail_hard_or_quality_gate():
    report = compare_provider_runs([
        run("ragflow", metrics(recall=0.2, mrr=0.2)),
        run("lightrag", metrics(provenance=0.0)),
    ])
    assert report["status"] == "NO_ELIGIBLE"
    assert report["selected_provider"] is None


def test_comparison_never_claims_canonical_mutation():
    report = compare_provider_runs([
        run("ragflow", metrics()),
        run("lightrag", metrics(p95=120)),
    ])
    assert report["canonical_mutation"] is False
    assert "does not make the provider canonical" in report["note"]
