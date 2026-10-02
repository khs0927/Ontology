"""Deterministic comparison of provider-neutral RAG benchmark results.

Comparison never promotes a retrieval provider into canonical storage. Only
providers that pass the existing provenance/ACL/revision/quality promotion gate
are eligible. Every run records a reproducible provider execution profile and a
cryptographic snapshot of the exact indexed projection.
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
_INDEX_SCHEMA = "drawing-context-rag-index-snapshot/1"
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


def _validate_fixture(fixture: dict[str, Any]) -> None:
    if not isinstance(fixture, dict):
        raise ValueError("benchmark fixture must be an object")
    if fixture.get("schema") != "drawing-context-rag-benchmark/1":
        raise ValueError("unsupported benchmark fixture schema")
    if fixture.get("canonical_mutation") is not False:
        raise ValueError("benchmark fixture must declare canonical_mutation=false")
    projection = fixture.get("projection")
    if not isinstance(projection, list):
        raise ValueError("benchmark fixture requires projection")
    if not isinstance(fixture.get("cases"), list) or not fixture["cases"]:
        raise ValueError("benchmark fixture requires non-empty cases")
    seen: set[str] = set()
    for row in projection:
        if not isinstance(row, dict):
            raise ValueError("benchmark projection rows must be objects")
        external_id = row.get("external_id")
        if not isinstance(external_id, str) or not external_id.strip():
            raise ValueError("benchmark projection rows require external_id")
        if external_id in seen:
            raise ValueError("benchmark projection external_id values must be unique")
        seen.add(external_id)


def fixture_digest(fixture: dict[str, Any]) -> str:
    """Digest provider-neutral benchmark inputs.

    Existing benchmark fixtures may carry a provider label. That label is the
    only excluded field so RAGFlow and LightRAG can compare the same projection
    and cases while every other fixture field remains identical.
    """
    _validate_fixture(fixture)
    normalized = {key: value for key, value in fixture.items() if key != "provider"}
    return _stable_digest(normalized)


def projection_digest(fixture: dict[str, Any]) -> str:
    _validate_fixture(fixture)
    return _stable_digest(fixture["projection"])


def projection_content_digest(fixture: dict[str, Any]) -> str:
    """Digest exact benchmark content keyed by stable external_id."""
    _validate_fixture(fixture)
    rows: list[dict[str, str]] = []
    for row in fixture["projection"]:
        content = row.get("content")
        if not isinstance(content, str):
            raise ValueError("benchmark projection rows require string content")
        rows.append({"external_id": row["external_id"], "content": content})
    return _stable_digest(sorted(rows, key=lambda row: row["external_id"]))


def _fixture_external_ids(fixture: dict[str, Any]) -> tuple[str, ...]:
    _validate_fixture(fixture)
    return tuple(sorted(row["external_id"] for row in fixture["projection"]))


def external_ids_digest(external_ids: Iterable[str]) -> str:
    values = list(external_ids)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("indexed external IDs must be non-empty strings")
    if len(values) != len(set(values)):
        raise ValueError("indexed external IDs must be unique")
    return _stable_digest(sorted(values))


def build_index_snapshot(
    fixture: dict[str, Any],
    *,
    provider_index_id: str,
    indexed_external_ids: Iterable[str],
    isolated_namespace: bool,
) -> dict[str, Any]:
    """Bind an observed provider index to the exact fixture projection.

    Callers must pass the external IDs actually bound/indexed in the provider
    namespace. The helper refuses missing, extra or duplicated IDs.
    """
    _validate_fixture(fixture)
    if not isinstance(provider_index_id, str) or not provider_index_id.strip():
        raise ValueError("provider_index_id must be non-empty")
    if isolated_namespace is not True:
        raise ValueError("benchmark provider index must use an isolated namespace")

    expected = _fixture_external_ids(fixture)
    observed = tuple(sorted(indexed_external_ids))
    if len(observed) != len(set(observed)):
        raise ValueError("indexed external IDs must be unique")
    if observed != expected:
        raise ValueError("indexed provider corpus does not exactly match fixture projection")

    return {
        "schema": _INDEX_SCHEMA,
        "provider_index_id": provider_index_id,
        "isolated_namespace": True,
        "record_count": len(observed),
        "projection_digest": projection_digest(fixture),
        "external_ids_digest": external_ids_digest(observed),
        "assurance": "caller-attested-local-view",
        "remote_inventory_verified": False,
    }


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


def _validate_hex_digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} requires a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be hexadecimal SHA-256") from exc
    return value


def _validate_index_snapshot(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema") != _INDEX_SCHEMA:
        raise ValueError("unsupported provider index snapshot schema")
    index_id = snapshot.get("provider_index_id")
    if not isinstance(index_id, str) or not index_id.strip():
        raise ValueError("provider index snapshot requires provider_index_id")
    if snapshot.get("isolated_namespace") is not True:
        raise ValueError("provider index snapshot must declare isolated_namespace=true")
    count = snapshot.get("record_count")
    if type(count) is not int or count < 0:
        raise ValueError("provider index record_count must be a non-negative integer")
    _validate_hex_digest(snapshot.get("projection_digest"), "projection_digest")
    _validate_hex_digest(snapshot.get("external_ids_digest"), "external_ids_digest")
    assurance = snapshot.get("assurance")
    remote_verified = snapshot.get("remote_inventory_verified")
    if assurance == "caller-attested-local-view":
        if remote_verified is not False:
            raise ValueError("caller-attested snapshot cannot claim remote inventory verification")
    elif assurance == "remote-readback-complete":
        if remote_verified is not True:
            raise ValueError("remote-readback-complete snapshot must declare verification")
        if snapshot.get("canonical_freshness_verified") is not True:
            raise ValueError(
                "remote-readback-complete snapshot requires canonical freshness proof"
            )
        if snapshot.get("processing_completion_verified") is not True:
            raise ValueError(
                "remote-readback-complete snapshot requires completed provider indexing"
            )
        if type(snapshot.get("deployment_identity_verified")) is not bool:
            raise ValueError(
                "remote-readback-complete snapshot requires deployment identity state"
            )
        if snapshot.get("deployment_identity_verified") is True:
            _validate_hex_digest(
                snapshot.get("deployment_attestation_digest"),
                "deployment_attestation_digest",
            )
            method = snapshot.get("deployment_verification_method")
            if not isinstance(method, str) or not method.strip():
                raise ValueError(
                    "verified deployment identity requires verification method"
                )
        for name in (
            "remote_document_count",
            "remote_chunk_count",
            "remote_document_ids_digest",
            "remote_chunk_ids_digest",
            "remote_projection_content_digest",
            "current_source_state_digest",
        ):
            if name.endswith("_count"):
                value = snapshot.get(name)
                if type(value) is not int or value < 0:
                    raise ValueError(f"{name} must be a non-negative integer")
            else:
                _validate_hex_digest(snapshot.get(name), name)
    else:
        raise ValueError("unsupported provider index snapshot assurance")


def _validate_snapshot_against_fixture(
    snapshot: dict[str, Any],
    fixture: dict[str, Any],
) -> None:
    _validate_index_snapshot(snapshot)
    expected_ids = _fixture_external_ids(fixture)
    if snapshot["record_count"] != len(expected_ids):
        raise ValueError("provider index record_count does not match fixture projection")
    if snapshot["projection_digest"] != projection_digest(fixture):
        raise ValueError("provider index projection digest does not match fixture")
    if snapshot["external_ids_digest"] != external_ids_digest(expected_ids):
        raise ValueError("provider index external ID digest does not match fixture")
    if (
        snapshot.get("assurance") == "remote-readback-complete"
        and snapshot["remote_projection_content_digest"] != projection_content_digest(fixture)
    ):
        raise ValueError("provider remote content digest does not match fixture projection")


def wrap_provider_result(
    provider: str,
    fixture: dict[str, Any],
    metrics: dict[str, Any],
    profile: dict[str, Any],
    index_snapshot: dict[str, Any],
) -> dict[str, Any]:
    provider = provider.strip().lower()
    if not provider:
        raise ValueError("provider must be non-empty")
    _validate_metrics(metrics)
    _validate_profile(profile)
    _validate_snapshot_against_fixture(index_snapshot, fixture)
    return {
        "schema": _RUN_SCHEMA,
        "provider": provider,
        "fixture_digest": fixture_digest(fixture),
        "profile": profile,
        "profile_digest": profile_digest(profile),
        "index_snapshot": index_snapshot,
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


def _validate_run(
    run: dict[str, Any],
) -> tuple[
    str,
    str,
    dict[str, Any],
    str,
    dict[str, Any],
    dict[str, Any],
]:
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
    if declared_profile_hash != profile_digest(profile):
        raise ValueError("provider profile digest does not match profile contents")

    index_snapshot = run.get("index_snapshot")
    _validate_index_snapshot(index_snapshot)

    metrics = run.get("metrics")
    _validate_metrics(metrics)
    return (
        provider.strip().lower(),
        fixture_hash,
        profile,
        declared_profile_hash,
        index_snapshot,
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
    """Compare runs produced from the same benchmark inputs and index corpus.

    Winner selection is intentionally non-weighted:
    1. Existing promotion gate must PASS.
    2. Higher Recall@K.
    3. Higher MRR.
    4. Lower p95 only if every still-tied provider reported p95.
    Otherwise the result remains a tie.
    """
    rows = list(runs)
    if len(rows) < 2:
        raise ValueError("comparison requires at least two provider runs")

    parsed = [_validate_run(row) for row in rows]
    providers = [provider for provider, _, _, _, _, _ in parsed]
    if len(set(providers)) != len(providers):
        raise ValueError("provider names must be unique")

    fixture_hashes = {digest for _, digest, _, _, _, _ in parsed}
    if len(fixture_hashes) != 1:
        raise ValueError("providers must use the exact same benchmark fixture")

    index_identities = {
        (
            snapshot["record_count"],
            snapshot["projection_digest"],
            snapshot["external_ids_digest"],
            snapshot.get("remote_projection_content_digest"),
            snapshot.get("current_source_state_digest"),
        )
        for _, _, _, _, snapshot, _ in parsed
    }
    if len(index_identities) != 1:
        raise ValueError("providers must index the exact same projection corpus")

    ks = {metrics["k"] for _, _, _, _, _, metrics in parsed}
    case_sets = {_case_ids(metrics) for _, _, _, _, _, metrics in parsed}
    if len(ks) != 1 or len(case_sets) != 1:
        raise ValueError("providers must use identical k and ordered case ids")

    thresholds = thresholds or PromotionThresholds()
    report_rows: list[dict[str, Any]] = []
    eligible: list[tuple[str, dict[str, Any]]] = []
    for provider, _, profile, profile_hash, index_snapshot, metrics in parsed:
        gate = promotion_decision(metrics, thresholds)
        row = {
            "provider": provider,
            "profile": profile,
            "profile_digest": profile_hash,
            "index_snapshot": index_snapshot,
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

    index_identity = next(iter(index_identities))
    all_remote_verified = all(
        snapshot.get("remote_inventory_verified") is True
        for _, _, _, _, snapshot, _ in parsed
    )
    all_deployment_verified = all(
        snapshot.get("deployment_identity_verified") is True
        for _, _, _, _, snapshot, _ in parsed
    )
    production_evidence_ready = (
        status == "SELECTED"
        and all_remote_verified
        and all_deployment_verified
    )
    return {
        "schema": _COMPARISON_SCHEMA,
        "fixture_digest": next(iter(fixture_hashes)),
        "index_corpus": {
            "record_count": index_identity[0],
            "projection_digest": index_identity[1],
            "external_ids_digest": index_identity[2],
            "remote_projection_content_digest": index_identity[3],
            "current_source_state_digest": index_identity[4],
        },
        "k": next(iter(ks)),
        "case_ids": list(next(iter(case_sets))),
        "thresholds": asdict(thresholds),
        "providers": sorted(report_rows, key=lambda row: row["provider"]),
        "status": status,
        "selected_provider": selected,
        "deciding_metric": deciding_metric,
        "tied_providers": tied,
        "canonical_mutation": False,
        "production_evidence_ready": production_evidence_ready,
        "production_adoption_eligible": False,
        "operator_approval_required": True,
        "remote_inventory_verified": all_remote_verified,
        "deployment_identity_verified": all_deployment_verified,
        "note": (
            "Selection applies only to this benchmark fixture and recorded provider "
            "profiles. Technical evidence becomes ready only when complete remote corpus "
            "proof and deployment identity proof both exist. It never authorizes production "
            "adoption automatically; a separate explicit operator approval artifact is "
            "required. Canonical CAIR remains authoritative."
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
