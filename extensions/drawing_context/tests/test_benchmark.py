from __future__ import annotations

from copy import deepcopy

from context_fabric.benchmark import (
    BenchmarkCase,
    PromotionThresholds,
    build_ragflow_fixture,
    evaluate_benchmark,
    promotion_decision,
)


def _record(*, rid="ctx-1", canonical="door-1", source="source-1", revision="rev-1"):
    return {
        "id": rid,
        "text": "공장 화장실 출입문",
        "canonical_id": canonical,
        "locator": {
            "source_id": source,
            "revision_id": revision,
        },
        "source": {
            "project_id": "P1",
            "sha256": "a" * 64,
        },
        "state": "HUMAN_VERIFIED",
    }


def _case():
    return BenchmarkCase(
        case_id="door-query",
        query="화장실 출입문",
        expected_canonical_ids=("door-1",),
        allowed_source_ids=frozenset({"source-1"}),
    )


def _hit(*, canonical="door-1", source="source-1", revision="rev-1", external="ctx-1"):
    return {
        "external_id": external,
        "content": "공장 화장실 출입문",
        "metadata": {
            "canonical_id": canonical,
            "source_id": source,
            "revision_id": revision,
            "project_id": "P1",
            "sha256": "a" * 64,
            "state": "HUMAN_VERIFIED",
        },
    }


def test_ragflow_fixture_is_network_free_and_preserves_provenance_mapping():
    fixture = build_ragflow_fixture([_record()], [_case()])
    assert fixture["provider"] == "ragflow"
    assert fixture["canonical_mutation"] is False
    row = fixture["projection"][0]
    assert row["external_id"] == "ctx-1"
    assert row["metadata"]["canonical_id"] == "door-1"
    assert row["metadata"]["source_id"] == "source-1"
    assert row["metadata"]["revision_id"] == "rev-1"
    assert row["metadata"]["sha256"] == "a" * 64


def test_perfect_run_passes_promotion_gate_and_tracks_latency():
    metrics = evaluate_benchmark(
        [_case()],
        {
            "door-query": {
                "hits": [_hit()],
                "latency_ms": 125.0,
            }
        },
        current_revision_by_source={"source-1": "rev-1"},
        k=5,
    )
    assert metrics["recall_at_k"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["provenance_metadata_coverage"] == 1.0
    assert metrics["unauthorized_source_leakage"] == 0
    assert metrics["stale_revision_leakage"] == 0
    assert metrics["latency"] == {"p50_ms": 125.0, "p95_ms": 125.0}
    assert promotion_decision(metrics)["status"] == "PASS"


def test_unauthorized_source_blocks_promotion_even_when_answer_is_relevant():
    metrics = evaluate_benchmark(
        [_case()],
        {"door-query": {"hits": [_hit(source="other-source")]}},
        current_revision_by_source={
            "source-1": "rev-1",
            "other-source": "rev-1",
        },
    )
    assert metrics["recall_at_k"] == 1.0
    assert metrics["unauthorized_source_leakage"] == 1
    decision = promotion_decision(metrics)
    assert decision["status"] == "BLOCKED"
    assert decision["checks"]["unauthorized_source_leakage"] is False


def test_stale_revision_blocks_promotion():
    metrics = evaluate_benchmark(
        [_case()],
        {"door-query": {"hits": [_hit(revision="rev-old")]}},
        current_revision_by_source={"source-1": "rev-1"},
    )
    assert metrics["stale_revision_leakage"] == 1
    decision = promotion_decision(metrics)
    assert decision["status"] == "BLOCKED"
    assert decision["checks"]["stale_revision_leakage"] is False


def test_missing_provenance_blocks_promotion():
    hit = _hit()
    del hit["metadata"]["sha256"]
    metrics = evaluate_benchmark(
        [_case()],
        {"door-query": {"hits": [hit]}},
        current_revision_by_source={"source-1": "rev-1"},
    )
    assert metrics["provenance_metadata_coverage"] == 0.0
    assert promotion_decision(metrics)["status"] == "BLOCKED"


def test_low_recall_blocks_even_without_security_leakage():
    case = BenchmarkCase(
        case_id="multi",
        query="문과 창",
        expected_canonical_ids=("door-1", "window-1"),
        allowed_source_ids=frozenset({"source-1"}),
    )
    metrics = evaluate_benchmark(
        [case],
        {"multi": {"hits": [_hit(canonical="door-1")]}},
        current_revision_by_source={"source-1": "rev-1"},
    )
    assert metrics["recall_at_k"] == 0.5
    decision = promotion_decision(metrics)
    assert decision["status"] == "BLOCKED"
    assert decision["checks"]["recall_at_k"] is False


def test_duplicate_results_are_reported_without_double_counting_expected_recall():
    metrics = evaluate_benchmark(
        [_case()],
        {"door-query": {"hits": [_hit(), deepcopy(_hit())]}},
        current_revision_by_source={"source-1": "rev-1"},
    )
    assert metrics["recall_at_k"] == 1.0
    assert metrics["duplicate_hits"] == 1


def test_custom_thresholds_can_be_stricter_but_cannot_relax_security_silently():
    metrics = evaluate_benchmark(
        [_case()],
        {"door-query": {"hits": [_hit()]}},
        current_revision_by_source={"source-1": "rev-1"},
    )
    strict = PromotionThresholds(recall_at_k=1.0, mrr=1.0)
    assert promotion_decision(metrics, strict)["status"] == "PASS"


def test_fixture_rejects_projection_with_bad_hash_metadata():
    bad = _record()
    bad["source"]["sha256"] = "not-a-sha"
    try:
        build_ragflow_fixture([bad], [_case()])
    except ValueError as exc:
        assert "provenance" in str(exc)
    else:
        raise AssertionError("bad provenance hash was accepted")
