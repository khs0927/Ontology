"""Bounded headless comparison of reader reports; does not invoke or trust a reader.

This is a preparation contract, not proof of independent DWG verification.
Reports describe the original DWG (not the intermediate DXF) and a common,
unexpanded model-space scope. Actual acquisition/invocation remains external.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

MAX_BYTES = 4 * 1024 * 1024
MAX_KEYS = 10_000
SCOPE = "model_space_top_level_unexpanded"
FIELDS = ("entities", "layers", "block_definitions")


def load_report(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read bounded JSON and hash the actual report bytes; reject duplicate keys."""
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("report_too_large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    report = json.loads(raw, object_pairs_hook=unique)
    validate_report(report)
    return report, hashlib.sha256(raw).hexdigest()


def validate_report(report: Any) -> None:
    if not isinstance(report, dict):
        raise ValueError("report_not_object")
    if report.get("schema_version") != 1 or isinstance(report.get("schema_version"), bool):
        raise ValueError("schema_version")
    if not isinstance(report.get("source_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", report["source_sha256"]):
        raise ValueError("source_sha256")
    for key in ("reader", "reader_version", "units"):
        value = report.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(key)
    if report["units"].lower() == "unknown":
        raise ValueError("units_unknown")
    if report.get("scope") != SCOPE:
        raise ValueError("scope")
    if report.get("verification_kind") not in ("synthetic", "headless_fixture"):
        raise ValueError("verification_kind")
    for key in (*FIELDS, "unsupported"):
        counts = report.get(key)
        if not isinstance(counts, dict) or len(counts) > MAX_KEYS:
            raise ValueError(key)
        for name, count in counts.items():
            if not isinstance(name, str) or not name.strip() or len(name) > 256:
                raise ValueError(key + "_name")
            if type(count) is not int or not 0 <= count <= 1_000_000_000:
                raise ValueError(key + "_count")
    if sum(report["entities"].values()) == 0:
        raise ValueError("empty_census")


def compare_reports(primary: Any, secondary: Any = None) -> dict[str, Any]:
    """Compare supplied counts only; never authorizes execution or canonical writes.

    Reader names do not prove independent implementations or trusted acquisition.
    Even PARITY requires separate trusted run/source evidence before promotion.
    """
    result = {"status": "NOT_RUN", "reasons": [], "differences": {},
              "execution_allowed": False, "canonical_allowed": False,
              "independent_verification_proven": False}
    if secondary is None:
        result["reasons"] = ["secondary_report_unavailable"]
        return result
    try:
        validate_report(primary)
        validate_report(secondary)
    except ValueError as exc:
        result.update(status="INVALID", reasons=[str(exc)])
        return result
    for key in ("source_sha256", "scope", "units", "verification_kind"):
        if primary[key] != secondary[key]:
            result["reasons"].append(key + "_mismatch")
    if primary["reader"] == secondary["reader"]:
        result["reasons"].append("same_reader")
    if result["reasons"]:
        result["status"] = "INCOMPARABLE"
        return result
    for field in FIELDS:
        delta = {name: {"primary": primary[field].get(name, 0),
                        "secondary": secondary[field].get(name, 0)}
                 for name in sorted(primary[field].keys() | secondary[field].keys())
                 if primary[field].get(name, 0) != secondary[field].get(name, 0)}
        if delta:
            result["differences"][field] = delta
    if result["differences"]:
        result["status"] = "MISMATCH"
    elif any(sum(report["unsupported"].values()) for report in (primary, secondary)):
        result.update(status="INCOMPLETE", reasons=["unsupported_objects"])
    else:
        result["status"] = "PARITY"
    result["verification_kind"] = primary["verification_kind"]
    result["source_sha256"] = primary["source_sha256"]
    return result
