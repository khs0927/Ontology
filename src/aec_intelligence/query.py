"""Small hybrid query router for the local global-memory indexes."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class QueryPlan:
    route: str
    rationale: str
    sources: tuple[str, ...]


class HybridQueryRouter:
    def plan(self, question: str) -> QueryPlan:
        text = question.lower()
        if any(token in text for token in ("면적", "area", "길이", "두께", "volume", "m²", "m2")):
            return QueryPlan("STRUCTURED", "numeric/property constraint", ("CAIR", "runtime structured index"))
        if any(token in text for token in ("가까운", "인접", "연결", "관계", "nearest", "adjacent", "connected")):
            return QueryPlan("KNOWLEDGE_GRAPH", "topology or relationship question", ("CAIR relations", "JSON-LD", "Neo4j optional"))
        if any(token in text for token in ("컨셉", "concept", "의도", "precedent", "전략")):
            return QueryPlan("VECTOR_PLUS_GRAPH", "design meaning needs semantic text and relationships", ("design knowledge", "CAIR", "graph"))
        if any(token in text for token in ("어디", "위치", "where", "좌표", "geometry")):
            return QueryPlan("GRAPH_PLUS_GEOMETRY", "object lookup needs graph identity and geometry reference", ("CAIR", "geometry index"))
        return QueryPlan("GLOBAL_MEMORY", "general project/object retrieval", ("global registries", "CAIR", "ontology"))


class LocalObjectIndex:
    def __init__(self, global_registry_path: str | Path):
        self.path = Path(global_registry_path)

    def find(self, type_name: str | None = None, project_id: str | None = None, min_confidence: float = 0.0) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            confidence = float((row.get("classification") or {}).get("confidence", 0.0))
            if type_name and row.get("type", "").lower() != type_name.lower():
                continue
            if project_id and row.get("project_id") != project_id:
                continue
            if confidence < min_confidence:
                continue
            rows.append(row)
        return rows

