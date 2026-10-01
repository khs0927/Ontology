"""Read-only live retrieval benchmark runner for a configured RAGFlow adapter."""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from .benchmark import (
    BenchmarkCase,
    PromotionThresholds,
    evaluate_benchmark,
    promotion_decision,
)
from .ragflow_http import RagflowHttpAdapter


def run_live_benchmark(
    adapter: RagflowHttpAdapter,
    cases: Iterable[BenchmarkCase],
    *,
    current_revision_by_source: dict[str, str],
    k: int = 5,
    thresholds: PromotionThresholds | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Execute diagnostic retrieval only and evaluate the existing hard gates.

    No upload, delete, revision replacement, or canonical mutation is performed.
    """
    case_list = list(cases)
    if not case_list:
        raise ValueError("live benchmark requires at least one case")
    runs: dict[str, dict[str, Any]] = {}
    for case in case_list:
        started = clock()
        result = adapter.benchmark_search(case.query, top_k=k)
        elapsed_ms = (clock() - started) * 1000.0
        if result.get("status") != "SUCCESS":
            raise RuntimeError(f"RAGFlow benchmark search failed for {case.case_id}")
        runs[case.case_id] = {
            "hits": result.get("hits", []),
            "latency_ms": elapsed_ms,
        }

    metrics = evaluate_benchmark(
        case_list,
        runs,
        current_revision_by_source=current_revision_by_source,
        k=k,
    )
    decision = promotion_decision(metrics, thresholds)
    return {
        "schema": "drawing-context-ragflow-live-benchmark/1",
        "provider": "ragflow",
        "release": adapter.config.release,
        "dataset_id": adapter.config.dataset_id,
        "metrics": metrics,
        "promotion": decision,
        "read_only": True,
        "canonical_mutation": False,
    }
