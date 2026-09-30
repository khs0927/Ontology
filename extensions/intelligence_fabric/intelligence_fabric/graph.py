"""Rebuildable graph exports and an optional HydraDB HTTP boundary.

No method in this module rewrites CAIR, project source artifacts, or files under
global/. The graph backends are accelerators that can be reconstructed from
canonical JSON/JSONL at any time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _stable_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cypher_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _runtime_target(repository_root: str | Path, target: str | Path | None) -> Path:
    root = Path(repository_root).resolve()
    destination = (root / "runtime" / "hydradb" / "seed.cypher") if target is None else Path(target).resolve()
    global_root = (root / "global").resolve()
    try:
        destination.relative_to(global_root)
    except ValueError:
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination
    raise ValueError("graph acceleration output must not be written under canonical global/")


@dataclass(frozen=True)
class GraphExportReport:
    status: str
    backend: str
    target: str
    counts: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_hydradb_seed(repository_root: str | Path, target: str | Path | None = None) -> GraphExportReport:
    """Create conservative OpenCypher suitable for HydraDB's documented subset.

    The export intentionally avoids Neo4j-only constraints/procedures. Relations
    use a stable AEC_RELATION type and keep the ontology predicate as a property
    so predicate strings never become executable Cypher identifiers.
    """
    root = Path(repository_root).resolve()
    global_root = root / "global" / "00_GLOBAL"
    projects = _jsonl(global_root / "global-project-registry.jsonl")
    objects = _jsonl(global_root / "global-object-registry.jsonl")
    relations = _jsonl(global_root / "global-relations.jsonl")
    provenance = {
        str(row.get("object_id")): row
        for row in _jsonl(global_root / "global-provenance.jsonl")
        if row.get("object_id")
    }
    destination = _runtime_target(root, target)
    lines = [
        "// Generated from canonical CAIR/global JSONL; rebuildable runtime export.",
        "// HydraDB integration boundary: no AGPL source is vendored into this repository.",
    ]
    project_count = object_count = relation_count = containment_count = 0
    known_project_ids: set[str] = set()
    for project in projects:
        project_id = str(project.get("project_id") or "").strip()
        if not project_id:
            continue
        graph_id = f"aec://project/{project_id}"
        known_project_ids.add(project_id)
        lines.append(
            "MERGE (p:AECProject {id: %s}) SET p.project_id = %s, p.name = %s, p.status = %s;"
            % (
                _cypher_literal(graph_id),
                _cypher_literal(project_id),
                _cypher_literal(project.get("name")),
                _cypher_literal(project.get("status")),
            )
        )
        project_count += 1
    for obj in objects:
        object_id = str(obj.get("id") or "").strip()
        if not object_id:
            continue
        project_id = str(obj.get("project_id") or "").strip()
        lines.append(
            "MERGE (n:AECObject {id: %s}) SET n.project_id = %s, n.type = %s, n.geometry_ref = %s, n.classification = %s, n.provenance = %s;"
            % (
                _cypher_literal(object_id),
                _cypher_literal(project_id or None),
                _cypher_literal(obj.get("type")),
                _cypher_literal(obj.get("geometry_ref")),
                _cypher_literal(_stable_json(obj.get("classification"))),
                _cypher_literal(_stable_json(provenance.get(object_id))),
            )
        )
        object_count += 1
        if project_id and project_id in known_project_ids:
            lines.append(
                "MATCH (p:AECProject {id: %s}), (n:AECObject {id: %s}) MERGE (p)-[:CONTAINS]->(n);"
                % (_cypher_literal(f"aec://project/{project_id}"), _cypher_literal(object_id))
            )
            containment_count += 1
    for relation in relations:
        subject = str(relation.get("subject") or "").strip()
        obj = str(relation.get("object") or "").strip()
        if not subject or not obj:
            continue
        lines.append(
            "MATCH (s {id: %s}), (o {id: %s}) MERGE (s)-[r:AEC_RELATION {predicate: %s}]->(o) SET r.confidence = %s, r.provenance = %s;"
            % (
                _cypher_literal(subject),
                _cypher_literal(obj),
                _cypher_literal(relation.get("predicate") or "relatedTo"),
                _cypher_literal(float(relation.get("confidence", 1.0))),
                _cypher_literal(_stable_json(relation.get("provenance"))),
            )
        )
        relation_count += 1
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return GraphExportReport(
        "SUCCESS",
        "HydraDB",
        str(destination),
        {
            "projects": project_count,
            "objects": object_count,
            "relations": relation_count,
            "containment_edges": containment_count,
            "provenance": len(provenance),
        },
    )


@dataclass(frozen=True)
class HydraDBConfig:
    base_url: str
    token: str
    namespace: str = "default"
    graph_id: str = "default"
    cell_id: str = "cell-0"
    consistency: str = "causal"

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be http(s)")
        if not self.token:
            raise ValueError("HydraDB bearer token is required")
        if not self.namespace or not self.graph_id or not self.cell_id:
            raise ValueError("namespace, graph_id and cell_id are required")
        if self.consistency not in {"causal", "strong"}:
            raise ValueError("consistency must be 'causal' or 'strong'")

    @property
    def query_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/graphs/{quote(self.graph_id, safe='')}/query"


class HydraDBHTTPAdapter:
    """Small stdlib HTTP client for an already-running HydraDB service."""

    def __init__(
        self,
        config: HydraDBConfig,
        *,
        opener: Callable[..., Any] = urlopen,
        timeout: float = 30.0,
    ) -> None:
        self.config = config
        self._opener = opener
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.token}",
            "X-Graph-Namespace": self.config.namespace,
            "Content-Type": "application/json",
        }

    def health(self) -> dict[str, Any]:
        request = Request(f"{self.config.base_url.rstrip('/')}/healthz", headers=self._headers(), method="GET")
        return self._request(request)

    def query(self, cypher: str) -> dict[str, Any]:
        if not isinstance(cypher, str) or not cypher.strip():
            raise ValueError("cypher must be a non-empty string")
        body = json.dumps(
            {
                "cell_id": self.config.cell_id,
                "query": cypher,
                "consistency": self.config.consistency,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(self.config.query_url, headers=self._headers(), data=body, method="POST")
        return self._request(request)

    def apply_seed(self, path: str | Path, *, max_statements: int | None = None) -> dict[str, Any]:
        source = Path(path).resolve()
        if not source.is_file():
            raise ValueError(f"seed file does not exist: {source}")
        statements = [
            line[:-1] if line.endswith(";") else line
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("//")
        ]
        if max_statements is not None:
            if max_statements < 0:
                raise ValueError("max_statements must be non-negative")
            statements = statements[:max_statements]
        applied = 0
        for statement in statements:
            result = self.query(statement)
            if result.get("status") != "SUCCESS":
                return {"status": "FAILED", "applied": applied, "failure": result}
            applied += 1
        return {"status": "SUCCESS", "applied": applied, "target": self.config.query_url}

    def _request(self, request: Request) -> dict[str, Any]:
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "") if getattr(response, "headers", None) else ""
                parsed: Any = payload
                if "json" in content_type.lower() or payload.lstrip().startswith(("{", "[")):
                    try:
                        parsed = json.loads(payload)
                    except json.JSONDecodeError:
                        parsed = payload
                return {"status": "SUCCESS", "http_status": getattr(response, "status", 200), "body": parsed}
        except HTTPError as exc:
            return {"status": "FAILED", "http_status": exc.code, "error": str(exc)}
        except URLError as exc:
            return {"status": "FAILED", "error": str(exc.reason)}
        except OSError as exc:
            return {"status": "FAILED", "error": str(exc)}
