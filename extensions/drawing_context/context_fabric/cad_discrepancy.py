"""Read-only CAD parser discrepancy detection.

The purpose of this module is to compare observations from independent CAD
parsing lanes without turning either lane into a truth oracle.

ACadSharp remains an external process (for example the All-In-Cad probe).
Ontology consumes census JSON, normalizes only comparable fields, and emits a
rebuildable discrepancy report. No source drawing or canonical CAIR state is
modified.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
from typing import Any, Iterable


_NORMALIZED_SCHEMA = "aec-cad-normalized-census/1"
_REPORT_SCHEMA = "aec-cad-discrepancy-report/1"
_SUPPORTED_INPUT_SCHEMAS = {
    "all-in-cad-headless-census/v1",
    "aec-cad-census/1",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | Path) -> str:
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"source file does not exist: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value.strip()


def _finite_nonnegative(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric or null")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _counter(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _normalize_handle(value: Any) -> str | None:
    if value is None:
        return None
    handle = str(value).strip()
    return handle.upper() if handle else None


def _normalize_entity(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("census entities must be objects")
    entity_type = str(
        row.get("entity_type")
        or row.get("type")
        or row.get("dxftype")
        or "UNKNOWN"
    ).strip().upper()
    if not entity_type:
        entity_type = "UNKNOWN"
    layer = str(row.get("layer") or "0").strip() or "0"
    layout_value = row.get("layout")
    layout = str(layout_value).strip() if layout_value not in (None, "") else None
    geometry = row.get("geometry")
    if geometry is None:
        geometry = {}
    if not isinstance(geometry, dict):
        raise ValueError("entity geometry must be an object")
    properties = row.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        raise ValueError("entity properties must be an object")
    block_name = (
        row.get("block_name")
        or properties.get("block_name")
        or properties.get("effective_name")
    )
    return {
        "handle": _normalize_handle(row.get("handle")),
        "entity_type": entity_type,
        "layer": layer,
        "layout": layout,
        "block_name": str(block_name).strip() if block_name not in (None, "") else None,
        "geometry_digest": _canonical_digest(geometry),
        "geometry": geometry,
        "properties": properties,
    }


def _normalize_block(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("block records must be objects")
    name = _nonempty(str(row.get("name") or ""), "block.name")
    nested = row.get("nested_reference_counts") or row.get("nested") or {}
    if not isinstance(nested, dict):
        raise ValueError("block nested references must be an object")
    by_type = row.get("entity_count_by_type") or {}
    if not isinstance(by_type, dict):
        raise ValueError("block entity_count_by_type must be an object")
    return {
        "name": name,
        "is_xref": bool(row.get("is_xref", False)),
        "entity_count": int(row.get("entity_count", sum(int(v) for v in by_type.values()))),
        "entity_count_by_type": {
            str(key).upper(): int(value)
            for key, value in sorted(by_type.items())
        },
        "nested_reference_counts": {
            str(key): int(value)
            for key, value in sorted(nested.items())
        },
    }


@dataclass(frozen=True)
class ExternalCadProbeConfig:
    enabled: bool = False
    command_prefix: tuple[str, ...] = ()
    provider: str = "acadsharp"
    provider_version: str = "unknown"
    adapter_commit: str | None = None
    timeout_seconds: float = 120.0
    handle_space: str = "source-dwg"

    def __post_init__(self):
        _nonempty(self.provider, "provider")
        _nonempty(self.provider_version, "provider_version")
        _nonempty(self.handle_space, "handle_space")
        if self.enabled and not self.command_prefix:
            raise ValueError("enabled external probe requires command_prefix")
        for part in self.command_prefix:
            _nonempty(part, "command_prefix item")
        if not 1 <= self.timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 1 and 300")


@dataclass(frozen=True)
class HandleMappingEvidence:
    source_sha256: str
    mapping: dict[str, str]
    verified: bool
    evidence_id: str
    method: str

    def __post_init__(self):
        _require_sha256(self.source_sha256, "HandleMappingEvidence.source_sha256")
        _nonempty(self.evidence_id, "HandleMappingEvidence.evidence_id")
        _nonempty(self.method, "HandleMappingEvidence.method")
        if self.verified is not True:
            raise ValueError("handle mapping evidence must be independently verified")
        if not self.mapping:
            raise ValueError("verified handle mapping must contain at least one pair")
        normalized: dict[str, str] = {}
        for left, right in self.mapping.items():
            a = _normalize_handle(left)
            b = _normalize_handle(right)
            if not a or not b:
                raise ValueError("handle mapping keys and values must be non-empty")
            if a in normalized and normalized[a] != b:
                raise ValueError(f"conflicting handle mapping for {a}")
            normalized[a] = b


def normalize_census(
    payload: dict[str, Any],
    *,
    provider: str,
    provider_version: str,
    source_sha256: str,
    handle_space: str = "unknown",
    adapter_commit: str | None = None,
    elapsed_ms: float | None = None,
    peak_rss_mb: float | None = None,
) -> dict[str, Any]:
    """Normalize an external or internal census into a comparison-only DTO."""
    if not isinstance(payload, dict):
        raise ValueError("census payload must be an object")
    schema = payload.get("schema")
    if schema not in _SUPPORTED_INPUT_SCHEMAS and schema != _NORMALIZED_SCHEMA:
        raise ValueError(f"unsupported census schema: {schema!r}")
    provider = _nonempty(provider, "provider")
    provider_version = _nonempty(provider_version, "provider_version")
    handle_space = _nonempty(handle_space, "handle_space")
    source_sha256 = _require_sha256(source_sha256, "source_sha256")

    if schema == _NORMALIZED_SCHEMA:
        declared = payload.get("source_sha256")
        if declared != source_sha256:
            raise ValueError("normalized census source hash does not match supplied source hash")
        return payload

    raw_entities = payload.get("entities")
    if not isinstance(raw_entities, list):
        raise ValueError("census payload requires entities list")
    entities = [_normalize_entity(row) for row in raw_entities]

    raw_blocks = payload.get("blocks")
    blocks_available = isinstance(raw_blocks, list)
    blocks = [_normalize_block(row) for row in raw_blocks] if blocks_available else []

    raw_unsupported = payload.get("unsupported")
    unsupported_available = isinstance(raw_unsupported, list)
    unsupported = (
        sorted(str(value) for value in raw_unsupported)
        if unsupported_available
        else []
    )

    layout_values = [row["layout"] for row in entities if row["layout"] is not None]
    layout_coverage = len(layout_values) == len(entities) and bool(entities)

    return {
        "schema": _NORMALIZED_SCHEMA,
        "provider": provider,
        "provider_version": provider_version,
        "adapter_commit": adapter_commit,
        "input_schema": schema,
        "source_sha256": source_sha256,
        "handle_space": handle_space,
        "entity_count": len(entities),
        "entity_type_counts": _counter(row["entity_type"] for row in entities),
        "layer_counts": _counter(row["layer"] for row in entities),
        "layout_coverage": layout_coverage,
        "layout_counts": _counter(layout_values) if layout_values else {},
        "blocks_available": blocks_available,
        "blocks": sorted(blocks, key=lambda row: row["name"].lower()),
        "unsupported_available": unsupported_available,
        "unsupported": unsupported,
        "entities": entities,
        "cost": {
            "elapsed_ms": _finite_nonnegative(elapsed_ms, "elapsed_ms"),
            "peak_rss_mb": _finite_nonnegative(peak_rss_mb, "peak_rss_mb"),
        },
        "canonical": False,
        "derived_projection": True,
    }


def run_external_census(
    source: str | Path,
    config: ExternalCadProbeConfig,
) -> dict[str, Any]:
    """Run an external read-only probe without shell interpolation."""
    if not config.enabled:
        return {
            "status": "DISABLED",
            "canonical": False,
            "derived_projection": True,
        }
    path = Path(source)
    if not path.is_file():
        raise ValueError(f"source file does not exist: {path}")
    source_hash = file_sha256(path)
    command = [*config.command_prefix, str(path.resolve())]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"external CAD probe timed out after {config.timeout_seconds:g}s"
        ) from exc
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:2000]
        raise RuntimeError(
            f"external CAD probe failed with exit code {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("external CAD probe returned invalid JSON") from exc
    census = normalize_census(
        payload,
        provider=config.provider,
        provider_version=config.provider_version,
        source_sha256=source_hash,
        handle_space=config.handle_space,
        adapter_commit=config.adapter_commit,
        elapsed_ms=elapsed_ms,
    )
    return {
        "status": "SUCCESS",
        "command": list(config.command_prefix),
        "source_sha256": source_hash,
        "census": census,
        "canonical": False,
        "derived_projection": True,
    }


def _validate_normalized(census: dict[str, Any]) -> None:
    if not isinstance(census, dict) or census.get("schema") != _NORMALIZED_SCHEMA:
        raise ValueError("expected normalized CAD census")
    _require_sha256(census.get("source_sha256"), "census.source_sha256")
    _nonempty(census.get("provider"), "census.provider")
    _nonempty(census.get("provider_version"), "census.provider_version")
    if census.get("canonical") is not False:
        raise ValueError("normalized census must declare canonical=false")
    if not isinstance(census.get("entities"), list):
        raise ValueError("normalized census requires entities list")


def _append_discrepancy(
    rows: list[dict[str, Any]],
    code: str,
    dimension: str,
    left: Any,
    right: Any,
    note: str,
) -> None:
    rows.append(
        {
            "code": code,
            "dimension": dimension,
            "left": left,
            "right": right,
            "note": note,
        }
    )


def _compare_dict(
    discrepancies: list[dict[str, Any]],
    *,
    code: str,
    dimension: str,
    left: dict[str, Any],
    right: dict[str, Any],
    note: str,
) -> None:
    if left != right:
        _append_discrepancy(discrepancies, code, dimension, left, right, note)


def _block_index(census: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"].lower(): row for row in census.get("blocks", [])}


def _entity_index(census: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in census.get("entities", []):
        handle = row.get("handle")
        if handle and handle not in index:
            index[handle] = row
    return index


def compare_censuses(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    handle_mapping: HandleMappingEvidence | None = None,
) -> dict[str, Any]:
    """Compare independent parser observations without selecting a winner."""
    _validate_normalized(left)
    _validate_normalized(right)

    source_hash = left["source_sha256"]
    if right["source_sha256"] != source_hash:
        return {
            "schema": _REPORT_SCHEMA,
            "status": "INCOMPARABLE",
            "reasons": ["SOURCE_HASH_MISMATCH"],
            "left_provider": left["provider"],
            "right_provider": right["provider"],
            "source_sha256": None,
            "discrepancies": [],
            "coverage_gaps": [],
            "object_level_comparison": "NOT_RUN",
            "truth_oracle": False,
            "winner": None,
            "correctness_score": None,
            "canonical_mutation": False,
            "note": "Different source bytes cannot be compared as parser evidence.",
        }

    discrepancies: list[dict[str, Any]] = []
    coverage_gaps: list[str] = []

    if left["entity_count"] != right["entity_count"]:
        _append_discrepancy(
            discrepancies,
            "ENTITY_COUNT_MISMATCH",
            "entity_count",
            left["entity_count"],
            right["entity_count"],
            "Entity counts differ; this does not identify which parser is correct.",
        )
    _compare_dict(
        discrepancies,
        code="ENTITY_TYPE_COUNTS_MISMATCH",
        dimension="entity_type_counts",
        left=left.get("entity_type_counts", {}),
        right=right.get("entity_type_counts", {}),
        note="Entity-type census differs.",
    )
    _compare_dict(
        discrepancies,
        code="LAYER_COUNTS_MISMATCH",
        dimension="layer_counts",
        left=left.get("layer_counts", {}),
        right=right.get("layer_counts", {}),
        note="Layer census differs.",
    )

    if left.get("layout_coverage") and right.get("layout_coverage"):
        _compare_dict(
            discrepancies,
            code="LAYOUT_COUNTS_MISMATCH",
            dimension="layout_counts",
            left=left.get("layout_counts", {}),
            right=right.get("layout_counts", {}),
            note="Layout membership/counts differ.",
        )
    else:
        coverage_gaps.append("LAYOUT_COMPARISON_NOT_FULLY_AVAILABLE")

    if left.get("blocks_available") and right.get("blocks_available"):
        _compare_dict(
            discrepancies,
            code="BLOCK_CATALOG_MISMATCH",
            dimension="blocks",
            left=_block_index(left),
            right=_block_index(right),
            note="Block definition/nesting/XREF observations differ.",
        )
    else:
        coverage_gaps.append("BLOCK_COMPARISON_NOT_AVAILABLE")

    if left.get("unsupported_available") and right.get("unsupported_available"):
        if left.get("unsupported") != right.get("unsupported"):
            _append_discrepancy(
                discrepancies,
                "UNSUPPORTED_OBJECTS_MISMATCH",
                "unsupported",
                left.get("unsupported"),
                right.get("unsupported"),
                "Unsupported/proxy observations differ.",
            )
    else:
        coverage_gaps.append("UNSUPPORTED_OBJECT_COMPARISON_NOT_AVAILABLE")

    object_level = "SKIPPED_NO_VERIFIED_HANDLE_MAPPING"
    compared_pairs = 0
    if handle_mapping is not None:
        if handle_mapping.source_sha256 != source_hash:
            raise ValueError("handle mapping evidence source hash does not match census source")
        left_entities = _entity_index(left)
        right_entities = _entity_index(right)
        object_level = "VERIFIED_MAPPING_USED"
        for raw_left, raw_right in sorted(handle_mapping.mapping.items()):
            left_handle = _normalize_handle(raw_left)
            right_handle = _normalize_handle(raw_right)
            a = left_entities.get(left_handle or "")
            b = right_entities.get(right_handle or "")
            if a is None or b is None:
                _append_discrepancy(
                    discrepancies,
                    "MAPPED_ENTITY_MISSING",
                    "mapped_entity",
                    left_handle if a is None else "present",
                    right_handle if b is None else "present",
                    "A verified mapping references an entity absent from one census.",
                )
                continue
            compared_pairs += 1
            if a["entity_type"] != b["entity_type"]:
                _append_discrepancy(
                    discrepancies,
                    "MAPPED_ENTITY_TYPE_MISMATCH",
                    f"entity:{left_handle}->{right_handle}:type",
                    a["entity_type"],
                    b["entity_type"],
                    "Mapped entity types differ.",
                )
            if a["layer"] != b["layer"]:
                _append_discrepancy(
                    discrepancies,
                    "MAPPED_ENTITY_LAYER_MISMATCH",
                    f"entity:{left_handle}->{right_handle}:layer",
                    a["layer"],
                    b["layer"],
                    "Mapped entity layers differ.",
                )
            if a["geometry_digest"] != b["geometry_digest"]:
                _append_discrepancy(
                    discrepancies,
                    "MAPPED_GEOMETRY_DIGEST_MISMATCH",
                    f"entity:{left_handle}->{right_handle}:geometry",
                    a["geometry_digest"],
                    b["geometry_digest"],
                    "Mapped geometry differs; this is a review signal, not a truth decision.",
                )

    status = "DISCREPANCIES" if discrepancies else "MATCH"
    return {
        "schema": _REPORT_SCHEMA,
        "status": status,
        "reasons": [],
        "left_provider": left["provider"],
        "right_provider": right["provider"],
        "source_sha256": source_hash,
        "discrepancies": discrepancies,
        "coverage_gaps": sorted(set(coverage_gaps)),
        "object_level_comparison": object_level,
        "mapped_pairs_compared": compared_pairs,
        "cost_observations": {
            "left": left.get("cost", {}),
            "right": right.get("cost", {}),
        },
        "truth_oracle": False,
        "winner": None,
        "correctness_score": None,
        "canonical_mutation": False,
        "note": (
            "MATCH means no discrepancy was found in the comparable dimensions. "
            "It is not proof that either parser is correct. Cost observations do "
            "not affect discrepancy status."
        ),
    }


def load_normalized_census(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    _validate_normalized(value)
    return value
