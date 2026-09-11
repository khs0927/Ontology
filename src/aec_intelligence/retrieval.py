"""Cross-project retrieval over portable CAIR/global indexes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .query import HybridQueryRouter


TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣]+", re.UNICODE)
VECTOR_DIMENSION = 64


def _tokens(value: Any) -> set[str]:
    return {token.lower() for token in TOKEN_PATTERN.findall(str(value or ""))}


def _lexical_vector(value: Any) -> list[float]:
    """Build a deterministic portable fallback vector without an external model."""
    vector = [0.0] * VECTOR_DIMENSION
    for token in sorted(_tokens(value)):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % VECTOR_DIMENSION
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [round(value / norm, 8) for value in vector] if norm else vector


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left or not right:
        return 0.0
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right))))


def _json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> bool:
    """Write an optional acceleration package without making Parquet canonical."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        normalized = [
            {
                key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            }
            for row in rows
        ]
        table = pa.Table.from_pylist(normalized)
    else:
        table = pa.table({"records": pa.array([], type=pa.string())})
    pq.write_table(table, path)
    return True


def _write_jsonl_package(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return path


@dataclass(frozen=True)
class RetrievalHit:
    project_id: str
    object_id: str | None
    score: float
    reasons: tuple[str, ...]
    type: str | None = None
    geometry_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "object_id": self.object_id, "score": round(self.score, 4), "reasons": list(self.reasons), "type": self.type, "geometry_ref": self.geometry_ref}


class CrossProjectRetriever:
    def __init__(self, repository_root: str | Path):
        self.root = Path(repository_root).resolve()
        self.global_root = self.root / "global" / "00_GLOBAL"
        self.projects_root = self.root / "projects"

    def _vector_index(self) -> dict[str, dict[str, Any]]:
        return {
            str(row.get("id")): row
            for row in _jsonl(self.root / "global" / "09_AGENT_MEMORY" / "vector-index.jsonl")
            if row.get("id") and isinstance(row.get("vector"), list)
        }

    def _projects(self) -> list[dict[str, Any]]:
        return _jsonl(self.global_root / "global-project-registry.jsonl")

    def _project_context(self, project_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        project_path = self.projects_root / project_id
        manifest = _json(project_path / "00_MANIFEST" / "project-manifest.json") or {"project_id": project_id}
        snapshot = _json(project_path / "03_CAIR" / "project-cair.json") or {}
        return manifest, snapshot.get("objects", [])

    def query_global_memory(self, question: str, top_k: int = 10, project_id: str | None = None) -> dict[str, Any]:
        query_tokens = _tokens(question)
        route = HybridQueryRouter().plan(question)
        hits: list[RetrievalHit] = []
        projects = self._projects()
        vector_index = self._vector_index()
        query_vector = _lexical_vector(question)
        if project_id:
            projects = [row for row in projects if row.get("project_id") == project_id]
        for project in projects:
            current_project_id = str(project.get("project_id"))
            manifest, objects = self._project_context(current_project_id)
            project_text = " ".join(str(value) for value in [current_project_id, project.get("name"), manifest.get("name"), manifest.get("tags"), manifest.get("type"), manifest.get("discipline")])
            project_tokens = _tokens(project_text)
            project_overlap = query_tokens & project_tokens
            for obj in objects:
                classification = obj.get("classification") or {}
                properties = obj.get("properties") or {}
                object_text = " ".join(str(value) for value in [obj.get("id"), obj.get("type"), classification.get("label"), classification.get("evidence"), properties])
                object_tokens = _tokens(object_text)
                overlap = query_tokens & object_tokens
                if not overlap and not project_overlap:
                    continue
                score = (len(overlap) * 2.0 + len(project_overlap)) / max(len(query_tokens) * 2.0, 1.0)
                reasons = [f"object terms: {', '.join(sorted(overlap))}"] if overlap else []
                if project_overlap:
                    reasons.append(f"project terms: {', '.join(sorted(project_overlap))}")
                vector_row = vector_index.get(str(obj.get("id")))
                vector_score = _cosine(query_vector, vector_row.get("vector", [])) if vector_row else 0.0
                if route.route == "VECTOR_PLUS_GRAPH" and vector_score:
                    score = min(1.0, score + vector_score * 0.25)
                    reasons.append(f"portable vector evidence: {vector_score:.4f}")
                hits.append(RetrievalHit(current_project_id, obj.get("id"), min(score, 1.0), tuple(reasons), obj.get("type"), obj.get("geometry_ref")))
        hits.sort(key=lambda hit: (-hit.score, hit.project_id, hit.object_id or ""))
        return {"route": route.route, "rationale": route.rationale, "query": question, "hits": [hit.to_dict() for hit in hits[:max(top_k, 0)]]}

    def find_similar_projects(self, project_id: str, top_k: int = 5) -> list[dict[str, Any]]:
        target_manifest, target_objects = self._project_context(project_id)
        target_types = Counter(str(obj.get("type")) for obj in target_objects)
        target_labels = {str((obj.get("classification") or {}).get("label")) for obj in target_objects if (obj.get("classification") or {}).get("label")}
        target_features = set(target_types) | target_labels | _tokens(target_manifest.get("tags"))
        hits: list[RetrievalHit] = []
        for project in self._projects():
            candidate_id = str(project.get("project_id"))
            if candidate_id == project_id:
                continue
            manifest, objects = self._project_context(candidate_id)
            candidate_types = Counter(str(obj.get("type")) for obj in objects)
            candidate_labels = {str((obj.get("classification") or {}).get("label")) for obj in objects if (obj.get("classification") or {}).get("label")}
            candidate_features = set(candidate_types) | candidate_labels | _tokens(manifest.get("tags"))
            union = target_features | candidate_features
            score = len(target_features & candidate_features) / len(union) if union else 0.0
            shared = sorted(target_features & candidate_features)
            hits.append(RetrievalHit(candidate_id, None, score, (f"shared semantic features: {', '.join(shared)}",) if shared else ("no shared semantic features",)))
        hits.sort(key=lambda hit: (-hit.score, hit.project_id))
        return [hit.to_dict() for hit in hits[:max(top_k, 0)]]


def _write_design_knowledge_packages(root: Path, project_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]] | Path]:
    """Promote iteration reasoning into portable cross-project knowledge rows."""

    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in ("lessons", "strategies", "problems", "solutions", "design-moves", "precedents", "metrics", "failures", "design-patterns")}
    for project in project_rows:
        project_id = str(project.get("project_id"))
        iteration_root = root / "projects" / project_id / "10_ITERATIONS"
        for manifest_path in sorted(iteration_root.glob("V*/manifest.json")):
            manifest = _json(manifest_path) or {}
            iteration_id = str(manifest.get("iteration_id") or manifest_path.parent.name)
            approved = (manifest.get("approval") or {}).get("status") == "APPROVED"
            base = {
                "knowledge_id": f"{project_id}:{iteration_id}",
                "project_id": project_id,
                "iteration_id": iteration_id,
                "reason": manifest.get("reason"),
                "changes": manifest.get("diff", {}).get("changes", []),
                "constraints": manifest.get("constraints", []),
                "metrics_before": manifest.get("metrics_before", {}),
                "metrics_after": manifest.get("metrics_after", {}),
                "validation_status": (manifest.get("validation") or {}).get("status"),
                "approval_status": (manifest.get("approval") or {}).get("status"),
                "source_cair": manifest.get("source_cair"),
            }
            rows["lessons"].append(base)
            rows["solutions"].append({**base, "solution": manifest.get("reason"), "approved": approved})
            for change in base["changes"]:
                rows["strategies"].append({"knowledge_id": f"{project_id}:{iteration_id}:strategy:{len(rows['strategies'])}", "project_id": project_id, "iteration_id": iteration_id, "strategy": change, "approved": approved})
                rows["design-moves"].append({"knowledge_id": f"{project_id}:{iteration_id}:move:{len(rows['design-moves'])}", "project_id": project_id, "iteration_id": iteration_id, "move": change, "reason": base["reason"], "approved": approved})
            for constraint in base["constraints"]:
                rows["problems"].append({"knowledge_id": f"{project_id}:{iteration_id}:problem:{len(rows['problems'])}", "project_id": project_id, "iteration_id": iteration_id, "problem": constraint})
            for metric, value in base["metrics_after"].items():
                rows["metrics"].append({"knowledge_id": f"{project_id}:{iteration_id}:metric:{metric}", "project_id": project_id, "iteration_id": iteration_id, "metric": metric, "value": value})
            if base["validation_status"] not in {"SUCCESS", "SUCCESS_WITH_WARNINGS"}:
                rows["failures"].append({**base, "failure": base["validation_status"]})
            if approved:
                rows["precedents"].append({"knowledge_id": base["knowledge_id"], "project_id": project_id, "iteration_id": iteration_id, "reason": base["reason"], "source_cair": base["source_cair"]})

    knowledge_root = root / "global" / "07_DESIGN_KNOWLEDGE"
    knowledge_root.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, list[dict[str, Any]] | Path] = {}
    for category, category_rows in rows.items():
        category_root = knowledge_root / category
        path = _write_jsonl_package(category_root / f"{category}.jsonl", category_rows)
        outputs[category] = category_rows
        outputs[f"{category}_jsonl"] = path
        parquet_path = category_root / f"{category}.parquet"
        if _write_parquet(parquet_path, category_rows):
            outputs[f"{category}_parquet"] = parquet_path
    return outputs


def write_memory_packages(repository_root: str | Path) -> dict[str, Path]:
    """Write rebuildable global/project context packages from canonical indexes."""
    root = Path(repository_root).resolve()
    retriever = CrossProjectRetriever(root)
    project_rows = retriever._projects()
    summaries = []
    outputs: dict[str, Path] = {}
    for project in project_rows:
        project_id = str(project.get("project_id"))
        manifest, objects = retriever._project_context(project_id)
        type_counts = Counter(str(obj.get("type")) for obj in objects)
        context = {
            "project": {"project_id": project_id, "name": manifest.get("name", project.get("name")), "status": manifest.get("status", project.get("status")), "source_artifacts": manifest.get("source_artifacts", []), "latest_iteration": manifest.get("latest_iteration")},
            "major_elements": [{"type": key, "count": value} for key, value in sorted(type_counts.items())],
            "current_design": manifest.get("current_design"),
            "constraints": manifest.get("constraints", []),
            "concept": manifest.get("concept"),
            "active_issues": manifest.get("active_issues", []),
            "important_artifacts": manifest.get("derived_artifacts", []),
            "object_count": len(objects),
        }
        path = root / "projects" / project_id / "13_AGENT_MEMORY" / "project-context.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outputs[f"{project_id}_context"] = path
        summary_path = path.parent / "project-summary.md"
        summary_path.write_text(
            "# Project summary\n\n"
            f"- Project ID: `{project_id}`\n"
            f"- Name: {context['project']['name']}\n"
            f"- Objects: {len(objects)}\n"
            f"- Latest iteration: {context['project']['latest_iteration'] or 'none'}\n"
            "\nThis summary is generated from the project manifest and CAIR registry.\n",
            encoding="utf-8",
        )
        outputs[f"{project_id}_summary"] = summary_path
        semantic_rows = [
            {"id": obj.get("id"), "project_id": project_id, "type": obj.get("type"), "classification": obj.get("classification"), "geometry_ref": obj.get("geometry_ref")}
            for obj in objects
        ]
        semantic_jsonl = _write_jsonl_package(path.parent / "semantic-index.jsonl", semantic_rows)
        outputs[f"{project_id}_semantic_jsonl"] = semantic_jsonl
        semantic_parquet = path.parent / "semantic-index.parquet"
        if _write_parquet(semantic_parquet, semantic_rows):
            outputs[f"{project_id}_semantic_parquet"] = semantic_parquet
        project_vector_rows = [
            {
                "id": obj.get("id"),
                "project_id": project_id,
                "kind": "object",
                "text": " ".join(str(value) for value in [obj.get("id"), obj.get("type"), obj.get("classification"), obj.get("properties")]),
                "vector": _lexical_vector(" ".join(str(value) for value in [obj.get("id"), obj.get("type"), obj.get("classification"), obj.get("properties")])),
            }
            for obj in objects
        ]
        project_vector_rows.append({
            "id": f"project:{project_id}",
            "project_id": project_id,
            "kind": "project",
            "text": " ".join(str(value) for value in [project_id, manifest.get("name"), manifest.get("tags"), manifest.get("concept"), manifest.get("constraints")]),
            "vector": _lexical_vector(" ".join(str(value) for value in [project_id, manifest.get("name"), manifest.get("tags"), manifest.get("concept"), manifest.get("constraints")])),
        })
        project_vector_jsonl = _write_jsonl_package(path.parent / "vector-index.jsonl", project_vector_rows)
        outputs[f"{project_id}_vector_index_jsonl"] = project_vector_jsonl
        project_vector_parquet = path.parent / "vector-index.parquet"
        if _write_parquet(project_vector_parquet, project_vector_rows):
            outputs[f"{project_id}_vector_index_parquet"] = project_vector_parquet
        active_state = {
            "project_id": project_id,
            "latest_iteration": manifest.get("latest_iteration"),
            "active_design_state": manifest.get("active_design_state", {}),
            "source": "project manifest and iteration memory",
        }
        active_path = path.parent / "active-design-state.json"
        active_path.write_text(json.dumps(active_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outputs[f"{project_id}_active_design"] = active_path
        ontology_path = root / "projects" / project_id / "04_ONTOLOGY" / "project.ttl"
        ontology_summary = {
            "project_id": project_id,
            "path": str(ontology_path.relative_to(root)) if ontology_path.is_file() else None,
            "line_count": len(ontology_path.read_text(encoding="utf-8").splitlines()) if ontology_path.is_file() else 0,
            "source": "project ontology export",
        }
        ontology_summary_path = path.parent / "ontology-summary.json"
        ontology_summary_path.write_text(json.dumps(ontology_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outputs[f"{project_id}_ontology_summary"] = ontology_summary_path
        artifact_rows = [row for row in _jsonl(retriever.global_root / "global-artifact-registry.jsonl") if row.get("project_id") == project_id]
        artifact_index_path = path.parent / "artifact-index.json"
        artifact_index_path.write_text(json.dumps({"project_id": project_id, "artifacts": artifact_rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outputs[f"{project_id}_artifact_index"] = artifact_index_path
        summaries.append({"project_id": project_id, "name": context["project"]["name"], "object_count": len(objects), "major_elements": context["major_elements"], "latest_iteration": context["project"]["latest_iteration"]})
    global_context = {
        "schema_version": "0.1.0",
        "project_count": len(summaries),
        "projects": summaries,
        "retrieval_source": "CAIR/global registries; Drive is artifact persistence, not semantic search",
    }
    global_path = root / "global" / "09_AGENT_MEMORY" / "global-context.json"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(json.dumps(global_context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    outputs["global_context"] = global_path
    global_package_root = global_path.parent
    project_summary_rows = [{"project_id": row["project_id"], "name": row["name"], "object_count": row["object_count"], "latest_iteration": row["latest_iteration"]} for row in summaries]
    knowledge = _write_design_knowledge_packages(root, project_rows)
    package_rows = {
        "project-summaries": project_summary_rows,
        "lessons": knowledge["lessons"],
        "strategies": knowledge["strategies"],
        "design-patterns": knowledge["design-patterns"],
        "ontology-index": [],
    }
    for package_name, rows in package_rows.items():
        jsonl_path = _write_jsonl_package(global_package_root / f"{package_name}.jsonl", rows)
        outputs[f"{package_name}_jsonl"] = jsonl_path
        parquet_path = global_package_root / f"{package_name}.parquet"
        if _write_parquet(parquet_path, rows):
            outputs[f"{package_name}_parquet"] = parquet_path
    vector_rows: list[dict[str, Any]] = []
    for project in project_rows:
        project_id = str(project.get("project_id"))
        manifest, objects = retriever._project_context(project_id)
        vector_rows.extend(
            {
                "id": obj.get("id"),
                "project_id": project_id,
                "kind": "object",
                "text": " ".join(str(value) for value in [obj.get("id"), obj.get("type"), obj.get("classification"), obj.get("properties")]),
                "vector": _lexical_vector(" ".join(str(value) for value in [obj.get("id"), obj.get("type"), obj.get("classification"), obj.get("properties")])),
            }
            for obj in objects
        )
        project_text = " ".join(str(value) for value in [project_id, manifest.get("name"), manifest.get("tags"), manifest.get("concept"), manifest.get("constraints")])
        vector_rows.append({"id": f"project:{project_id}", "project_id": project_id, "kind": "project", "text": project_text, "vector": _lexical_vector(project_text)})
    vector_jsonl = _write_jsonl_package(global_package_root / "vector-index.jsonl", vector_rows)
    outputs["vector_index_jsonl"] = vector_jsonl
    vector_parquet = global_package_root / "vector-index.parquet"
    if _write_parquet(vector_parquet, vector_rows):
        outputs["vector_index_parquet"] = vector_parquet
    global_object_rows = _jsonl(root / "global" / "00_GLOBAL" / "global-object-registry.jsonl")
    global_object_jsonl = _write_jsonl_package(global_package_root / "global-object-registry.jsonl", global_object_rows)
    outputs["global_object_registry_jsonl"] = global_object_jsonl
    global_object_parquet = global_package_root / "global-object-registry.parquet"
    if _write_parquet(global_object_parquet, global_object_rows):
        outputs["global_object_registry_parquet"] = global_object_parquet
    mapping_rows: list[dict[str, Any]] = []
    for project in project_rows:
        project_id = str(project.get("project_id"))
        snapshot = _json(root / "projects" / project_id / "03_CAIR" / "project-cair.json") or {}
        for obj in snapshot.get("objects", []):
            source = obj.get("source") or {}
            mapping_rows.append({
                "global_object_id": obj.get("id"),
                "project_id": project_id,
                "sources": {str(source.get("format", "")).lower(): source.get("entity_id")},
                "source_file": source.get("file"),
                "geometry_ref": obj.get("geometry_ref"),
            })
    mapping_jsonl = _write_jsonl_package(global_package_root / "source-mapping.jsonl", mapping_rows)
    outputs["source_mapping_jsonl"] = mapping_jsonl
    mapping_parquet = global_package_root / "source-mapping.parquet"
    if _write_parquet(mapping_parquet, mapping_rows):
        outputs["source_mapping_parquet"] = mapping_parquet
    return outputs
