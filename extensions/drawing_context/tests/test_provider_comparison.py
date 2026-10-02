from __future__ import annotations

from copy import deepcopy

import pytest

from context_fabric.compare import (
    build_index_snapshot,
    compare_provider_runs,
    fixture_digest,
    profile_digest,
    projection_content_digest,
    wrap_provider_result,
)


def fixture(provider="neutral"):
    return {
        "schema": "drawing-context-rag-benchmark/1",
        "provider": provider,
        "canonical_mutation": False,
        "projection": [{"external_id": "ctx-1"}],
        "cases": [{"case_id": "q1", "query": "door"}],
    }


def profile(provider):
    return {
        "provider_version": "v0.27.2" if provider == "ragflow" else "v1.5.7",
        "retrieval_mode": "hybrid" if provider == "ragflow" else "mix",
        "embedding_model": "BAAI/bge-m3",
        "embedding_revision": "commit-123",
        "index_revision": "fixture-index-1",
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


def run(provider, result, *, f=None, p=None):
    benchmark_fixture = f or fixture()
    index_snapshot = build_index_snapshot(
        benchmark_fixture,
        provider_index_id=f"{provider}-isolated-index",
        indexed_external_ids=[
            row["external_id"] for row in benchmark_fixture["projection"]
        ],
        isolated_namespace=True,
    )
    return wrap_provider_result(
        provider,
        benchmark_fixture,
        result,
        p or profile(provider),
        index_snapshot,
    )


def test_fixture_digest_is_stable_for_key_order_and_ignores_provider_label_only():
    a = fixture("ragflow")
    b = {
        "cases": a["cases"],
        "projection": a["projection"],
        "canonical_mutation": False,
        "provider": "lightrag",
        "schema": a["schema"],
    }
    assert fixture_digest(a) == fixture_digest(b)

    changed = deepcopy(b)
    changed["cases"][0]["query"] = "window"
    assert fixture_digest(a) != fixture_digest(changed)


def test_profile_digest_is_stable_and_profile_is_required():
    p = profile("ragflow")
    assert profile_digest(p) == profile_digest(dict(reversed(list(p.items()))))
    bad = dict(p)
    del bad["embedding_revision"]
    with pytest.raises(ValueError, match="embedding_revision"):
        profile_digest(bad)


def test_index_snapshot_requires_exact_isolated_fixture_corpus():
    f = fixture()
    snapshot = build_index_snapshot(
        f,
        provider_index_id="dataset-1",
        indexed_external_ids=["ctx-1"],
        isolated_namespace=True,
    )
    assert snapshot["record_count"] == 1
    assert snapshot["isolated_namespace"] is True
    assert snapshot["assurance"] == "caller-attested-local-view"
    assert snapshot["remote_inventory_verified"] is False

    with pytest.raises(ValueError, match="exactly match"):
        build_index_snapshot(
            f,
            provider_index_id="dataset-1",
            indexed_external_ids=[],
            isolated_namespace=True,
        )
    with pytest.raises(ValueError, match="isolated namespace"):
        build_index_snapshot(
            f,
            provider_index_id="dataset-1",
            indexed_external_ids=["ctx-1"],
            isolated_namespace=False,
        )


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


def test_tampered_provider_profile_is_rejected():
    a = run("ragflow", metrics())
    b = run("lightrag", metrics())
    b["profile"]["retrieval_mode"] = "tampered"
    with pytest.raises(ValueError, match="profile digest"):
        compare_provider_runs([a, b])


def test_different_indexed_corpus_identity_is_rejected():
    a = run("ragflow", metrics())
    b = run("lightrag", metrics())
    b["index_snapshot"]["external_ids_digest"] = "c" * 64
    with pytest.raises(ValueError, match="exact same projection corpus"):
        compare_provider_runs([a, b])


def test_report_preserves_profiles_and_index_identity_for_reproducibility():
    ragflow = run("ragflow", metrics())
    light = run("lightrag", metrics(p95=120))
    report = compare_provider_runs([ragflow, light])

    rows = {row["provider"]: row for row in report["providers"]}
    assert rows["ragflow"]["profile"]["provider_version"] == "v0.27.2"
    assert rows["lightrag"]["profile"]["provider_version"] == "v1.5.7"
    assert rows["ragflow"]["profile_digest"] == profile_digest(profile("ragflow"))
    assert rows["ragflow"]["index_snapshot"]["record_count"] == 1
    assert report["index_corpus"]["record_count"] == 1


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
    assert report["production_evidence_ready"] is False
    assert report["production_adoption_eligible"] is False
    assert report["operator_approval_required"] is True
    assert report["remote_inventory_verified"] is False
    assert "never authorizes production adoption automatically" in report["note"]


def test_remote_corpus_proof_without_deployment_identity_is_not_evidence_ready():
    ragflow = run("ragflow", metrics())
    light = run("lightrag", metrics(recall=0.9, mrr=0.8, p95=120))
    for item in (ragflow, light):
        snapshot = item["index_snapshot"]
        snapshot["assurance"] = "remote-readback-complete"
        snapshot["remote_inventory_verified"] = True
        snapshot["remote_document_count"] = 1
        snapshot["remote_chunk_count"] = 1
        snapshot["remote_document_ids_digest"] = "a" * 64
        snapshot["remote_chunk_ids_digest"] = "b" * 64
        snapshot["remote_projection_content_digest"] = projection_content_digest(fixture())
        snapshot["canonical_freshness_verified"] = True
        snapshot["current_source_state_digest"] = "c" * 64
        snapshot["processing_completion_verified"] = True
        snapshot["deployment_identity_verified"] = False

    report = compare_provider_runs([ragflow, light])
    assert report["status"] == "SELECTED"
    assert report["selected_provider"] == "ragflow"
    assert report["remote_inventory_verified"] is True
    assert report["production_evidence_ready"] is False
    assert report["deployment_identity_verified"] is False
    assert report["production_adoption_eligible"] is False
    assert report["operator_approval_required"] is True


def test_remote_and_deployment_proofs_still_wait_for_execution_provenance():
    ragflow = run("ragflow", metrics())
    light = run("lightrag", metrics(recall=0.9, mrr=0.8, p95=120))
    for item in (ragflow, light):
        snapshot = item["index_snapshot"]
        snapshot["assurance"] = "remote-readback-complete"
        snapshot["remote_inventory_verified"] = True
        snapshot["remote_document_count"] = 1
        snapshot["remote_chunk_count"] = 1
        snapshot["remote_document_ids_digest"] = "a" * 64
        snapshot["remote_chunk_ids_digest"] = "b" * 64
        snapshot["remote_projection_content_digest"] = projection_content_digest(fixture())
        snapshot["canonical_freshness_verified"] = True
        snapshot["current_source_state_digest"] = "c" * 64
        snapshot["processing_completion_verified"] = True
        snapshot["deployment_identity_verified"] = True
        snapshot["deployment_attestation_digest"] = "d" * 64
        snapshot["deployment_verification_method"] = "operator-verified-local-container"

    report = compare_provider_runs([ragflow, light])
    assert report["status"] == "SELECTED"
    assert report["remote_inventory_verified"] is True
    assert report["deployment_identity_verified"] is True
    assert report["benchmark_execution_verified"] is False
    assert report["execution_provenance_required"] is True
    assert report["production_evidence_ready"] is False
    assert report["production_adoption_eligible"] is False
    assert report["operator_approval_required"] is True
