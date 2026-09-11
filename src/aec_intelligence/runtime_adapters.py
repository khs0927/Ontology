"""Optional runtime adapters with explicit dependency/configuration states.

Canonical JSON/JSONL remains the source of truth. These adapters only build
reconstructible runtime indexes or exports and never write into `global/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from .cair import CAIRSnapshot
from .repository import RepositoryLayout


@dataclass
class RuntimeAdapterReport:
    status: str
    backend: str
    target: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "backend": self.backend, "target": self.target, "counts": self.counts, "warnings": self.warnings, "errors": self.errors}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _runtime_target(repository_root: str | Path, target: str | Path | None, suffix: str) -> Path:
    root = Path(repository_root).resolve()
    destination = (root / "runtime" / suffix) if target is None else Path(target).resolve()
    try:
        destination.relative_to(root / "global")
    except ValueError:
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination
    raise ValueError("runtime adapters must not write under the canonical global directory")


class DuckDBRuntimeAdapter:
    """Materialize global JSONL registries into a rebuildable DuckDB database."""

    backend = "DuckDB"

    def materialize(self, repository_root: str | Path, target: str | Path | None = None) -> RuntimeAdapterReport:
        try:
            import duckdb
        except ImportError:
            return RuntimeAdapterReport("REQUIRES_DEPENDENCY", self.backend, None if target is None else str(target), warnings=["DuckDB is not installed; install DuckDB separately from the optional PyArrow extra"])
        try:
            destination = _runtime_target(repository_root, target, "aec.duckdb")
            root = RepositoryLayout(Path(repository_root).resolve()).ensure().root / "global" / "00_GLOBAL"
            connection = duckdb.connect(str(destination))
            rows_by_table = {
                "projects": _jsonl(root / "global-project-registry.jsonl"),
                "artifacts": _jsonl(root / "global-artifact-registry.jsonl"),
                "objects": _jsonl(root / "global-object-registry.jsonl"),
                "relations": _jsonl(root / "global-relations.jsonl"),
                "provenance": _jsonl(root / "global-provenance.jsonl"),
            }
            definitions = {
                "projects": "project_id VARCHAR, name VARCHAR, status VARCHAR, last_ingest VARCHAR",
                "artifacts": "artifact_id VARCHAR, project_id VARCHAR, artifact_type VARCHAR, filename VARCHAR, local_path VARCHAR, sha256 VARCHAR, size BIGINT, created_at VARCHAR, status VARCHAR, version INTEGER, source_artifact_id VARCHAR, storage_provider VARCHAR, storage_id VARCHAR, metadata JSON, security_classification VARCHAR",
                "objects": "id VARCHAR, project_id VARCHAR, type VARCHAR, classification JSON, geometry_ref VARCHAR",
                "relations": "key VARCHAR, subject VARCHAR, predicate VARCHAR, object VARCHAR, confidence DOUBLE, provenance JSON",
                "provenance": "object_id VARCHAR, source_file VARCHAR, source_object VARCHAR, source_format VARCHAR, source_hash VARCHAR, parser VARCHAR, parser_version VARCHAR, timestamp VARCHAR, agent VARCHAR, transformation VARCHAR, derived_from JSON",
            }
            for table, rows in rows_by_table.items():
                connection.execute(f"CREATE OR REPLACE TABLE {table} ({definitions[table]})")
                if not rows:
                    continue
                columns = [part.split()[0] for part in definitions[table].split(", ")]
                values = []
                for row in rows:
                    values.append(tuple(json.dumps(row.get(column), ensure_ascii=False) if column in {"classification", "provenance", "metadata", "derived_from"} and row.get(column) is not None else row.get(column) for column in columns))
                placeholders = ", ".join("?" for _ in columns)
                connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", values)
            connection.close()
            return RuntimeAdapterReport("SUCCESS", self.backend, str(destination), {table: len(rows) for table, rows in rows_by_table.items()})
        except Exception as exc:  # boundary must report, never silently downgrade
            return RuntimeAdapterReport("FAILED", self.backend, str(target) if target else None, errors=[str(exc)])


class RDFLibRuntimeAdapter:
    backend = "RDFLib"

    def load_turtle(self, ontology_path: str | Path) -> RuntimeAdapterReport:
        try:
            import rdflib
        except ImportError:
            return RuntimeAdapterReport("REQUIRES_DEPENDENCY", self.backend, str(ontology_path), warnings=["RDFLib is not installed; install the [semantic] extra"])
        try:
            graph = rdflib.Graph()
            graph.parse(str(Path(ontology_path).resolve()), format="turtle")
            return RuntimeAdapterReport("SUCCESS", self.backend, str(Path(ontology_path).resolve()), {"triples": len(graph)})
        except Exception as exc:
            return RuntimeAdapterReport("FAILED", self.backend, str(ontology_path), errors=[str(exc)])


class OxigraphRuntimeAdapter:
    backend = "Oxigraph"

    def load_turtle(self, ontology_path: str | Path) -> RuntimeAdapterReport:
        try:
            from pyoxigraph import Store
        except ImportError:
            return RuntimeAdapterReport("REQUIRES_DEPENDENCY", self.backend, str(ontology_path), warnings=["PyOxigraph is not installed; use RDFLib for the portable baseline"])
        try:
            store = Store()
            from pyoxigraph import RdfFormat
            store.load(Path(ontology_path).read_bytes(), RdfFormat.TURTLE)
            return RuntimeAdapterReport("SUCCESS", self.backend, str(Path(ontology_path).resolve()), {"quads": len(store)})
        except Exception as exc:
            return RuntimeAdapterReport("FAILED", self.backend, str(ontology_path), errors=[str(exc)])


def _cypher_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def write_neo4j_cypher(snapshot: CAIRSnapshot, path: str | Path) -> Path:
    """Write a deterministic, reviewable Neo4j seed script without connecting."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["// Generated from CAIR; runtime-only export. Do not treat as canonical storage.", "CREATE CONSTRAINT aec_object_id IF NOT EXISTS FOR (n:AECObject) REQUIRE n.id IS UNIQUE;"]
    for obj in snapshot.objects:
        lines.append(f"MERGE (n:AECObject {{id: {_cypher_literal(obj.id)}}}) SET n.project_id = {_cypher_literal(obj.project_id)}, n.type = {_cypher_literal(obj.type)}, n.geometry_ref = {_cypher_literal(obj.geometry_ref)};")
    for relation in snapshot.relations:
        predicate = "RELATED_TO"
        if relation.predicate.isidentifier():
            predicate = relation.predicate.upper()
        lines.append(f"MATCH (s:AECObject {{id: {_cypher_literal(relation.subject)}}}), (o:AECObject {{id: {_cypher_literal(relation.object)}}}) MERGE (s)-[:{predicate}]->(o);")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


class Neo4jRuntimeAdapter:
    backend = "Neo4j"

    def connect(self, uri: str | None = None, user: str | None = None, password: str | None = None) -> RuntimeAdapterReport:
        if not uri or not user or not password:
            return RuntimeAdapterReport("REQUIRES_CONFIGURATION", self.backend, uri, warnings=["Neo4j URI, user, and password are required; no network connection was attempted"])
        try:
            from neo4j import GraphDatabase
        except ImportError:
            return RuntimeAdapterReport("REQUIRES_DEPENDENCY", self.backend, uri, warnings=["neo4j driver is not installed"])
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password))
            driver.verify_connectivity()
            driver.close()
            return RuntimeAdapterReport("SUCCESS", self.backend, uri)
        except Exception as exc:
            return RuntimeAdapterReport("FAILED", self.backend, uri, errors=[str(exc)])

    def ingest_global(
        self,
        repository_root: str | Path,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> RuntimeAdapterReport:
        """Idempotently materialize canonical global registries into Neo4j.

        Neo4j is a disposable operational graph here.  The JSON/JSONL files
        remain authoritative, and this method only uses MERGE statements so a
        rebuild can be repeated without duplicating graph data.
        """
        if not uri or not user or not password:
            return RuntimeAdapterReport("REQUIRES_CONFIGURATION", self.backend, uri, warnings=["Neo4j URI, user, and password are required; no network connection was attempted"])
        try:
            from neo4j import GraphDatabase
        except ImportError:
            return RuntimeAdapterReport("REQUIRES_DEPENDENCY", self.backend, uri, warnings=["neo4j driver is not installed"])
        try:
            root = RepositoryLayout(Path(repository_root).resolve()).ensure().root / "global" / "00_GLOBAL"
            projects = _jsonl(root / "global-project-registry.jsonl")
            objects = _jsonl(root / "global-object-registry.jsonl")
            relations = _jsonl(root / "global-relations.jsonl")
            provenance = {str(row.get("object_id")): row for row in _jsonl(root / "global-provenance.jsonl")}
            driver = GraphDatabase.driver(uri, auth=(user, password))
            try:
                with driver.session() as session:
                    for project in projects:
                        project_id = str(project.get("project_id"))
                        session.run(
                            "MERGE (p:AECProject {id:$id}) SET p.project_id=$project_id, p.name=$name, p.status=$status",
                            id=f"aec://project/{project_id}",
                            project_id=project_id,
                            name=project.get("name"),
                            status=project.get("status"),
                        ).consume()
                    for obj in objects:
                        object_id = str(obj.get("id"))
                        session.run(
                            "MERGE (n:AECObject {id:$id}) SET n.project_id=$project_id, n.type=$type, n.geometry_ref=$geometry_ref, n.classification=$classification, n.provenance=$provenance",
                            id=object_id,
                            project_id=obj.get("project_id"),
                            type=obj.get("type"),
                            geometry_ref=obj.get("geometry_ref"),
                            classification=json.dumps(obj.get("classification"), ensure_ascii=False),
                            provenance=json.dumps(provenance.get(object_id), ensure_ascii=False) if object_id in provenance else None,
                        ).consume()
                    for relation in relations:
                        session.run(
                            "MATCH (s {id:$subject}), (o:AECObject {id:$object}) MERGE (s)-[r:AEC_RELATION {predicate:$predicate}]->(o) SET r.confidence=$confidence",
                            subject=relation.get("subject"),
                            object=relation.get("object"),
                            predicate=re.sub(r"[^A-Za-z0-9_]+", "_", str(relation.get("predicate") or "relatedTo")),
                            confidence=float(relation.get("confidence", 1.0)),
                        ).consume()
                    project_count = session.run("MATCH (n:AECProject) RETURN count(n) AS count").single()["count"]
                    object_count = session.run("MATCH (n:AECObject) RETURN count(n) AS count").single()["count"]
                    relation_count = session.run("MATCH ()-[r:AEC_RELATION]->() RETURN count(r) AS count").single()["count"]
                return RuntimeAdapterReport("SUCCESS", self.backend, uri, {"projects": project_count, "objects": object_count, "relations": relation_count, "provenance": len(provenance)})
            finally:
                driver.close()
        except Exception as exc:
            return RuntimeAdapterReport("FAILED", self.backend, uri, errors=[str(exc)])
