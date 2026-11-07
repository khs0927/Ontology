"""Producer side of contract ``aec-cair-snapshot/1`` (consolidation phase 1, docs/CONSOLIDATION_PLAN.md).

khs0927/Ontology-platform consumes ``projects/<id>/03_CAIR/project-cair.json`` through
``sion_cair.ingest`` and maps it into its graph/evidence model. The schema in
``tests/contracts/aec-cair-snapshot-1.schema.json`` is a verbatim copy of the consumer's
``sion_core/contracts/schemas/aec-cair-snapshot-1.schema.json``; this test fails when an ingestion change
here would break that consumer. Change both copies (and bump the contract) together.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "tests" / "contracts" / "aec-cair-snapshot-1.schema.json").read_text(encoding="utf-8"))
FIXTURES = ROOT / "fixtures"


def _ingest_via_cli(tmp_path: Path, command: str, source: Path, project_id: str) -> dict:
    # Exactly what the consumer runs: `python -m aec_intelligence.cli --root <tmp> <command> <src> --project-id <id>`.
    proc = subprocess.run(
        [sys.executable, "-m", "aec_intelligence.cli", "--root", str(tmp_path), command, str(source), "--project-id", project_id],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert proc.returncode in (0, 2), proc.stderr[-2000:]
    return json.loads((tmp_path / "projects" / project_id / "03_CAIR" / "project-cair.json").read_text(encoding="utf-8"))


def _assert_consumer_invariants(snapshot: dict) -> None:
    jsonschema.validate(snapshot, SCHEMA)
    ids = [obj["id"] for obj in snapshot["objects"]]
    assert ids and len(ids) == len(set(ids)), "CAIR object ids must be unique (they become Sion stable keys)"
    for obj in snapshot["objects"]:
        assert obj["provenance"] and len(obj["provenance"]["source_hash"]) == 64, "consumer keys the source Artifact by this hash"


def test_dxf_cli_output_satisfies_consumer_contract(tmp_path):
    pytest.importorskip("ezdxf")
    snapshot = _ingest_via_cli(tmp_path, "ingest-dxf", FIXTURES / "simple_house.dxf", "simple-house-dxf")
    _assert_consumer_invariants(snapshot)
    assert {r["predicate"] for r in snapshot["relations"]} >= {"containsElement", "hostedBy"}


def test_ifc_cli_output_satisfies_consumer_contract(tmp_path):
    pytest.importorskip("ifcopenshell")
    snapshot = _ingest_via_cli(tmp_path, "ingest-file", FIXTURES / "simple_house.ifc", "simple-house-ifc")
    _assert_consumer_invariants(snapshot)
    assert any(r["predicate"] == "contains" for r in snapshot["relations"])


def test_object_ids_are_stable_across_runs(tmp_path):
    pytest.importorskip("ezdxf")
    first = _ingest_via_cli(tmp_path / "a", "ingest-dxf", FIXTURES / "simple_house.dxf", "simple-house-dxf")
    second = _ingest_via_cli(tmp_path / "b", "ingest-dxf", FIXTURES / "simple_house.dxf", "simple-house-dxf")
    assert {o["id"] for o in first["objects"]} == {o["id"] for o in second["objects"]}
