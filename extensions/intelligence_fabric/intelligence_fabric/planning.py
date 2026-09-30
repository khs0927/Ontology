"""Backend selection rules for the opt-in intelligence fabric."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class GraphRequirements:
    local_only: bool = True
    requires_sparql: bool = False
    object_store_durability: bool = False
    distributed_compute: bool = False
    neo4j_protocol: bool = False

    def __post_init__(self) -> None:
        if self.local_only and (self.object_store_durability or self.distributed_compute):
            raise ValueError("local_only conflicts with distributed/object-store requirements")


def choose_graph_backend(requirements: GraphRequirements) -> dict[str, Any]:
    """Choose by required capability, not popularity or star count."""
    if requirements.requires_sparql:
        return {
            "backend": "PyOxigraph",
            "role": "local RDF/SPARQL semantic index",
            "canonical": False,
            "reason": "SPARQL is required",
        }
    if requirements.object_store_durability or requirements.distributed_compute:
        return {
            "backend": "HydraDB",
            "role": "external distributed property-graph accelerator",
            "canonical": False,
            "reason": "object-store durability or disaggregated compute is required",
            "deployment_boundary": "external-service-only",
        }
    if requirements.neo4j_protocol and not requirements.local_only:
        return {
            "backend": "Neo4j-compatible",
            "role": "external property-graph runtime",
            "canonical": False,
            "reason": "Neo4j protocol compatibility is required",
        }
    return {
        "backend": "Apache AGE",
        "role": "local PostgreSQL property-graph runtime",
        "canonical": False,
        "reason": "local-first baseline with existing repository deployment",
    }


def intelligence_plan(
    *,
    private_source: bool = True,
    wants_code_context: bool = True,
    graph_requirements: GraphRequirements | None = None,
) -> dict[str, Any]:
    requirements = graph_requirements or GraphRequirements()
    graph = choose_graph_backend(requirements)
    stages: list[dict[str, Any]] = [
        {
            "stage": "canonical",
            "action": "keep CAIR/project/global JSON/JSONL authoritative",
            "writes_source": False,
        }
    ]
    if wants_code_context:
        stages.append(
            {
                "stage": "context-selection",
                "backend": "dzhng/jevgrep",
                "mode": "external-cli",
                "source_egress": "explicit-approval" if private_source else "allowed-by-policy",
                "writes_source": False,
            }
        )
    stages.append(
        {
            "stage": "graph-acceleration",
            **graph,
            "writes_source": False,
        }
    )
    stages.append(
        {
            "stage": "validation",
            "action": "rebuild accelerators from canonical sources and compare counts/provenance before promotion",
            "writes_source": False,
        }
    )
    return {"schema": "ontology-intelligence-plan/1", "requirements": asdict(requirements), "stages": stages}
