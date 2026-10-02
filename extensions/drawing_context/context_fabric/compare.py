"""Deterministic comparison of provider-neutral RAG benchmark results.

Comparison never promotes a retrieval provider into canonical storage. Only
providers that pass the existing provenance/ACL/revision/quality promotion gate
are eligible. Every run also records a reproducible provider execution profile.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from .benchmark import PromotionThresholds, promotion_decision


_RUN_SCHEMA = "drawing-context-rag-provider-run/1"
_COMPARISON_SCHEMA = "drawing-context-rag-provider-comparison/1"
_REQUIRED_PROFILE_FIELDS = (
    "provider_version",
    "retrieval_mode",
    "embedding_model",
    "embedding_revision",
    "index_revision",
)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fixture_digest(fixture: dict[str, Any]) -> str:
    """Digest provider-neutral benchmark inputs.

    Existing benchmark fixtures may carry a provider label. That label is the
    only excluded field so RAGFlow and LightRAG can compare the same projection
    and cases while every other fixture field remains identical.
    """
    if not isinstance(fixture, dict):
        raise ValueError("benchmark fixture must be an object")
    if fixture.get("schema") != "drawing-context-rag-benchmark/1":
        raise ValueError("unsupported benchmark fixture schema")
    if fixture.get("canonical_mutation") is not False:
        raise ValueError("benchmark fixture must declare canonical_mutation=false")
    if not isinstance(fixture.get("projection"), list):
        raise ValueError("benchmark fixture requires projection")
    if not isinstance(fixture.get("cases"), list) or not fixture["cases"]:
        raise ValueError("benchmark fixture requires non-empty cases")
    normalized = {key: value for key, value in fixture.items() if key != "provider"}
    return _stable_digest(normalized)


def _validate_profile(profile: dict[str, Any]) -> None:
    if not isinstance(profile, dict):
        raise ValueError("provider profile must be an object")
    for field in _REQUIRED_PROFILE_FIELDS:
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"provider profile requires non-empty {field}")


def profile_digest(profile: dict[str, Any]) -> str:
    _validate_profile(profile)
    return _stable_digest(profile)


def wrap_provider_result(
    provider: str,
    fixture: dict[str, Any],
    metrics: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    provider = provider.strip().lower()
    if not provider:
        raise ValueError("provider must be non-empty")
    _validate_metrics(metrics)
    _validate_profile(profile)
    return {
        "schema": _RUN_SCHEMA,
        "provider": provider,
        "fixture_digest": fixture_digest(fixture),
        "profile": profile,
        "profile_digest": profile_digest(profile),
        "metrics": metrics,
        "canonical_mutation": False,
    }


def _case_ids(metrics: dict[str, Any]) -> tuple[str, ...]:
    cases = metrics.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("benchmark metrics require non-empty cases")
    ids: list[str] = []
    for row in cases:
        if not isinstance(row, dict):
            raise ValueError("benchmark case result must be an object")
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("benchmark case result requires case_id")
        ids.append(case_id)
    if len(set(ids)) != len(ids):
        raise ValueError("benchmark case ids must be unique")
    return tuple(ids)


def _finite_unit(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0 <= numeric <= 1:
        raise ValueError(f"{name} must be finite and between 0 and 1")
    return numeric


def _validate_metrics(metrics: dict[str, Any]) -> None:
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be an object")
    if metrics.get("schema") != "drawing-context-rag-benchmark-result/1":
        raise ValueError("unsupported benchmark metrics schema")
    if metrics.get("canonical_mutation") is not False:
        raise ValueError("benchmark metrics must declare canonical_mutation=false")
    k = metrics.get("k")
    if type(k) is not int or not 1 <= k <= 100:
        raise ValueError("benchmark metrics require k between 1 and 100")
    _finite_unit(metrics.get("recall_at_k"), "recall_at_k")
    _finite_unit(metrics.get("mrr"), "mrr")
    _finite_unit(
        metrics.get("provenance_metadata_coverage"),
        "provenance_metadata_coverage",
    )
    for name in ("unauthorized_source_leakage", "stale_revision_leakage"):
        value = metrics.get(name)
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    _case_ids(metrics)


def _validate_hex_digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} requires a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be hexadecimal SHA-256") from exc
    return value


def _validate_run(
    run: dict[str, Any],
) -> tuple[str, str, dict[str, Any], str, dict[str, Any]]:
    if not isinstance(run, dict) or run.get("schema") != _RUN_SCHEMA:
        raise ValueError("unsupported provider run schema")
    if run.get("canonical_mutation") is not False:
        raise ValueError("provider run must declare canonical_mutation=false")
    provider = run.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider run requires provider")

    fixture_hash = _validate_hex_digest(run.get("fixture_digest"), "fixture_digest")
    profile = run.get("profile")
    _validate_profile(profile)
    declared_profile_hash = _validate_hex_digest(
        run.get("profile_digest"),
        "profile_digest",
    )
    actual_profile_hash = profile_digest(profile)
    if declared_profile_hash != actual_profile_hash:
        raise ValueError("provider profile digest does not match profile contents")

    metrics = run.get("metrics")
    _validate_metrics(metrics)
    return (
        provider.strip().lower(),
        fixture_hash,
        profile,
        declared_profile_hash,
        metrics,
    )


def _p95(metrics: dict[str, Any]) -> float | None:
    latency = metrics.get("latency")
    if not isinstance(latency, dict):
        return None
    value = latency.get("p95_ms")
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("p95_ms must be numeric or null")
    numeric = float(value)
    if numeric < 0 or not math.isfinite(numeric):
        raise ValueError("p95_ms must be finite and non-negative")
    return numeric


def compare_provider_runs(
    runs: Iterable[dict[str, Any]],
    thresholds: PromotionThresholds | None = None,
) -> dict[str, Any]:
    """Compare runs produced from the same benchmark inputs.

    Winner selection is intentionally non-weighted:
    1. Existing promotion gate must PASS.
    2. Higher Recall@K.
    3. Higher MRR.
    4. Lower p95 only if every still-tied provider reported p95.
    Otherwise the result remains a tie.

    Provider profiles are recorded, not required to match: comparing different
    retrieval systems is the purpose of the benchmark, while the profile makes
    each system configuration reproducible.
    """
    rows = list(runs)
    if len(rows) < 2:
        raise ValueError("comparison requires at least two provider runs")

    parsed = [_validate_run(row) for row in rows]
    providers = [provider for provider, _, _, _, _ in parsed]
    if len(set(providers)) != len(providers):
        raise ValueError("provider names must be unique")

    digests = {digest for _, digest, _, _, _ in parsed}
    if len(digests) != 1:
        raise ValueError("providers must use the exact same benchmark fixture")

    ks = {metrics["k"] for _, _, _, _, metrics in parsed}
    case_sets = {_case_ids(metrics) for _, _, _, _, metrics in parsed}
    if len(ks) != 1 or len(case_sets) != 1:
        raise ValueError("providers must use identical k and ordered case ids")

    thresholds = thresholds or PromotionThresholds()
    report_rows: list[dict[str, Any]] = []
    eligible: list[tuple[str, dict[str, Any]]] = []
    for provider, _, profile, profile_hash, metrics in parsed:
        gate = promotion_decision(metrics, thresholds)
        row = {
            "provider": provider,
            "profile": profile,
            "profile_digest": profile_hash,
            "promotion_status": gate["status"],
            "checks": gate["checks"],
            "recall_at_k": metrics["recall_at_k"],
            "mrr": metrics["mrr"],
            "p95_ms": _p95(metrics),
            "unauthorized_source_leakage": metrics["unauthorized_source_leakage"],
            "stale_revision_leakage": metrics["stale_revision_leakage"],
            "provenance_metadata_coverage": metrics["provenance_metadata_coverage"],
        }
        report_rows.append(row)
        if gate["status"] == "PASS":
            eligible.append((provider, metrics))

    selected: str | None = None
    status = "NO_ELIGIBLE"
    deciding_metric: str | None = None
    tied: list[str] = []

    if eligible:
        best_recall = max(metrics["recall_at_k"] for _, metrics in eligible)
        finalists = [
            (provider, metrics)
            for provider, metrics in eligible
            if metrics["recall_at_k"] == best_recall
        ]
        if len(finalists) == 1:
            selected = finalists[0][0]
            status = "SELECTED"
            deciding_metric = "recall_at_k"
        else:
            best_mrr = max(metrics["mrr"] for _, metrics in finalists)
            finalists = [
                (provider, metrics)
                for provider, metrics in finalists
                if metrics["mrr"] == best_mrr
            ]
            if len(finalists) == 1:
                selected = finalists[0][0]
                status = "SELECTED"
                deciding_metric = "mrr"
            else:
                p95_values = [
                    (provider, _p95(metrics))
                    for provider, metrics in finalists
                ]
                if all(value is not None for _, value in p95_values):
                    best_p95 = min(
                        value for _, value in p95_values if value is not None
                    )
                    p95_finalists = [
                        provider
                        for provider, value in p95_values
                        if value == best_p95
                    ]
                    if len(p95_finalists) == 1:
                        selected = p95_finalists[0]
                        status = "SELECTED"
                        deciding_metric = "p95_ms"
                    else:
                        status = "TIE"
                        tied = sorted(p95_finalists)
                else:
                    status = "TIE"
                    tied = sorted(provider for provider, _ in finalists)

    return {
        "schema": _COMPARISON_SCHEMA,
        "fixture_digest": next(iter(digests)),
        "k": next(iter(ks)),
        "case_ids": list(next(iter(case_sets))),
        "thresholds": asdict(thresholds),
        "providers": sorted(report_rows, key=lambda row: row["provider"]),
        "status": status,
        "selected_provider": selected,
        "deciding_metric": deciding_metric,
        "tied_providers": tied,
        "canonical_mutation": False,
        "note": (
            "Selection applies only to this benchmark fixture and recorded "
            "provider profiles. It does not make the provider canonical."
        ),
    }


def load_provider_run(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate_run(value)
    return value


def save_comparison(report: dict[str, Any], path: str | Path) -> None:
    if (
        report.get("schema") != _COMPARISON_SCHEMA
        or report.get("canonical_mutation") is not False
    ):
        raise ValueError("invalid comparison report")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
