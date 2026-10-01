"""Provider-neutral retrieval benchmark contracts for derived RAG sidecars.

The benchmark never mutates canonical CAIR, source artifacts, ACL state, or CAD.
It evaluates provider results against drawing-context identities that already
carry source/revision/provenance bindings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from statistics import median
from typing import Any, Iterable

from .adapters import ragflow_projection


REQUIRED_METADATA = (
    "canonical_id",
    "source_id",
    "revision_id",
    "project_id",
    "sha256",
    "state",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    query: str
    expected_canonical_ids: tuple[str, ...]
    allowed_source_ids: frozenset[str]

    def __post_init__(self):
        if not self.case_id.strip() or not self.query.strip():
            raise ValueError("case_id and query must be non-empty")
        if not self.expected_canonical_ids:
            raise ValueError("benchmark case needs at least one expected canonical id")
        if not self.allowed_source_ids:
            raise ValueError("benchmark case needs at least one allowed source id")


@dataclass(frozen=True)
class PromotionThresholds:
    recall_at_k: float = 0.80
    mrr: float = 0.60
    provenance_metadata_coverage: float = 1.0
    unauthorized_source_leakage: int = 0
    stale_revision_leakage: int = 0

    def __post_init__(self):
        for name in ("recall_at_k", "mrr", "provenance_metadata_coverage"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.unauthorized_source_leakage < 0 or self.stale_revision_leakage < 0:
            raise ValueError("leakage thresholds cannot be negative")


def _metadata(hit: dict[str, Any]) -> dict[str, Any]:
    value = hit.get("metadata")
    return value if isinstance(value, dict) else {}


def _has_provenance_metadata(hit: dict[str, Any]) -> bool:
    metadata = _metadata(hit)
    if any(not isinstance(metadata.get(key), str) or not metadata[key].strip() for key in REQUIRED_METADATA):
        return False
    return bool(_SHA256.fullmatch(metadata["sha256"]))


def build_ragflow_fixture(records: Iterable[dict[str, Any]], cases: Iterable[BenchmarkCase]) -> dict[str, Any]:
    """Build a reviewable, network-free RAGFlow benchmark fixture.

    This is deliberately not a RAGFlow HTTP client. A deployment adapter may
    upload the projection and later feed retrieval hits into evaluate_benchmark.
    """
    projected = ragflow_projection(list(records))
    for row in projected:
        if not isinstance(row.get("external_id"), str) or not row["external_id"]:
            raise ValueError("RAGFlow projection requires a stable external_id")
        if not _has_provenance_metadata(row):
            raise ValueError("RAGFlow projection lost required provenance metadata")
    return {
        "schema": "drawing-context-rag-benchmark/1",
        "provider": "ragflow",
        "canonical_mutation": False,
        "projection": projected,
        "cases": [
            {
                **asdict(case),
                "allowed_source_ids": sorted(case.allowed_source_ids),
            }
            for case in cases
        ],
    }


def evaluate_benchmark(
    cases: Iterable[BenchmarkCase],
    runs: dict[str, dict[str, Any]],
    *,
    current_revision_by_source: dict[str, str],
    k: int = 5,
) -> dict[str, Any]:
    if not 1 <= k <= 100:
        raise ValueError("k must be between 1 and 100")

    case_list = list(cases)
    if not case_list:
        raise ValueError("at least one benchmark case is required")

    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    provenance_ok = 0
    hit_count = 0
    unauthorized = 0
    stale = 0
    duplicates = 0
    details: list[dict[str, Any]] = []

    for case in case_list:
        run = runs.get(case.case_id)
        if not isinstance(run, dict):
            raise ValueError(f"missing benchmark run for case: {case.case_id}")
        hits = run.get("hits")
        if not isinstance(hits, list):
            raise ValueError(f"hits must be a list for case: {case.case_id}")
        top = hits[:k]
        expected = set(case.expected_canonical_ids)
        seen_external: set[str] = set()
        found_expected: set[str] = set()
        first_rank: int | None = None
        case_unauthorized = 0
        case_stale = 0
        case_provenance_ok = 0

        for rank, raw_hit in enumerate(top, 1):
            if not isinstance(raw_hit, dict):
                raise ValueError(f"hit {rank} for {case.case_id} must be an object")
            hit_count += 1
            if _has_provenance_metadata(raw_hit):
                provenance_ok += 1
                case_provenance_ok += 1
            metadata = _metadata(raw_hit)
            source_id = metadata.get("source_id")
            revision_id = metadata.get("revision_id")
            canonical_id = metadata.get("canonical_id")
            external_id = raw_hit.get("external_id")

            if isinstance(external_id, str):
                if external_id in seen_external:
                    duplicates += 1
                seen_external.add(external_id)

            if source_id not in case.allowed_source_ids:
                unauthorized += 1
                case_unauthorized += 1

            current_revision = current_revision_by_source.get(source_id) if isinstance(source_id, str) else None
            if current_revision is None or revision_id != current_revision:
                stale += 1
                case_stale += 1

            if canonical_id in expected:
                found_expected.add(canonical_id)
                if first_rank is None:
                    first_rank = rank

        recall = len(found_expected) / len(expected)
        rr = 0.0 if first_rank is None else 1.0 / first_rank
        recalls.append(recall)
        reciprocal_ranks.append(rr)

        latency = run.get("latency_ms")
        if latency is not None:
            if not isinstance(latency, (int, float)) or isinstance(latency, bool) or latency < 0 or not math.isfinite(float(latency)):
                raise ValueError(f"invalid latency_ms for case: {case.case_id}")
            latencies.append(float(latency))

        details.append(
            {
                "case_id": case.case_id,
                "recall_at_k": recall,
                "reciprocal_rank": rr,
                "hit_count": len(top),
                "provenance_metadata_coverage": 1.0 if not top else case_provenance_ok / len(top),
                "unauthorized_source_leakage": case_unauthorized,
                "stale_revision_leakage": case_stale,
            }
        )

    latency_metrics: dict[str, float | None] = {"p50_ms": None, "p95_ms": None}
    if latencies:
        ordered = sorted(latencies)
        p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        latency_metrics = {
            "p50_ms": median(ordered),
            "p95_ms": ordered[p95_index],
        }

    return {
        "schema": "drawing-context-rag-benchmark-result/1",
        "k": k,
        "case_count": len(case_list),
        "hit_count": hit_count,
        "recall_at_k": sum(recalls) / len(recalls),
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "provenance_metadata_coverage": 1.0 if hit_count == 0 else provenance_ok / hit_count,
        "unauthorized_source_leakage": unauthorized,
        "stale_revision_leakage": stale,
        "duplicate_hits": duplicates,
        "latency": latency_metrics,
        "cases": details,
        "canonical_mutation": False,
    }


def promotion_decision(
    metrics: dict[str, Any],
    thresholds: PromotionThresholds | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or PromotionThresholds()
    checks = {
        "recall_at_k": metrics.get("recall_at_k", 0.0) >= thresholds.recall_at_k,
        "mrr": metrics.get("mrr", 0.0) >= thresholds.mrr,
        "provenance_metadata_coverage": (
            metrics.get("provenance_metadata_coverage", 0.0)
            >= thresholds.provenance_metadata_coverage
        ),
        "unauthorized_source_leakage": (
            metrics.get("unauthorized_source_leakage", math.inf)
            <= thresholds.unauthorized_source_leakage
        ),
        "stale_revision_leakage": (
            metrics.get("stale_revision_leakage", math.inf)
            <= thresholds.stale_revision_leakage
        ),
    }
    return {
        "schema": "drawing-context-rag-promotion/1",
        "status": "PASS" if all(checks.values()) else "BLOCKED",
        "checks": checks,
        "thresholds": asdict(thresholds),
        "canonical_mutation": False,
        "note": "Latency, indexing time and storage are reported separately during the first benchmark phase.",
    }
