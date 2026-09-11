"""Portable graph exports from CAIR, independent of Neo4j."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .cair import CAIRSnapshot


def snapshot_to_jsonld(snapshot: CAIRSnapshot) -> dict[str, Any]:
    graph: list[dict[str, Any]] = [{"@id": f"aec://project/{snapshot.project_id}", "@type": "aec:Project", "aec:projectId": snapshot.project_id}]
    for obj in snapshot.objects:
        node: dict[str, Any] = {
            "@id": obj.id,
            "@type": f"aec:{obj.type}",
            "aec:projectId": obj.project_id,
            "aec:sourceHandle": obj.source.entity_id,
            "aec:sourceLayer": obj.source.layer,
            "aec:hasGeometry": {"@id": obj.geometry_ref} if obj.geometry_ref else None,
        }
        if obj.classification:
            node["aec:classification"] = {"label": obj.classification.label, "confidence": obj.classification.confidence, "state": obj.classification.state}
        graph.append(node)
    for relation in snapshot.relations:
        graph.append({"@id": f"aec://relation/{len(graph)}", "@type": "aec:Relation", "aec:subject": {"@id": relation.subject}, "aec:predicate": relation.predicate, "aec:object": {"@id": relation.object}, "aec:confidence": relation.confidence})
    return {
        "@context": {"aec": "https://example.org/aec#", "prov": "http://www.w3.org/ns/prov#"},
        "@id": f"aec://snapshot/{snapshot.snapshot_id}",
        "aec:schemaVersion": snapshot.schema_version,
        "@graph": graph,
    }


def _node_rows(snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    return [
        {
            "id": obj.id,
            "project_id": obj.project_id,
            "type": obj.type,
            "geometry_ref": obj.geometry_ref,
            "classification_json": json.dumps(obj.classification.to_dict() if obj.classification else None, ensure_ascii=False, sort_keys=True),
            "properties_json": json.dumps(obj.properties, ensure_ascii=False, sort_keys=True),
        }
        for obj in snapshot.objects
    ]


def _relationship_rows(snapshot: CAIRSnapshot) -> list[dict[str, Any]]:
    return [
        {
            "subject": relation.subject,
            "predicate": relation.predicate,
            "object": relation.object,
            "confidence": relation.confidence,
            "provenance_json": json.dumps(relation.provenance, ensure_ascii=False, sort_keys=True),
        }
        for relation in snapshot.relations
    ]


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_parquet(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bool:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return False
    pq.write_table(pa.table({column: [row.get(column) for row in rows] for column in columns}), path)
    return True


def _write_graphml(path: Path, snapshot: CAIRSnapshot) -> Path:
    namespace = "http://graphml.graphdrawing.org/xmlns"
    graphml = ElementTree.Element("graphml", {"xmlns": namespace})
    for key_id, name, target in (
        ("d0", "aec_id", "node"),
        ("d1", "project_id", "node"),
        ("d2", "type", "node"),
        ("d3", "geometry_ref", "node"),
        ("d4", "predicate", "edge"),
        ("d5", "confidence", "edge"),
    ):
        ElementTree.SubElement(graphml, "key", {"id": key_id, "for": target, "attr.name": name, "attr.type": "double" if name == "confidence" else "string"})
    graph = ElementTree.SubElement(graphml, "graph", {"id": f"snapshot-{snapshot.snapshot_id}", "edgedefault": "directed"})
    node_ids = {f"aec://project/{snapshot.project_id}": "n-project"}
    project_node = ElementTree.SubElement(graph, "node", {"id": "n-project"})
    ElementTree.SubElement(project_node, "data", {"key": "d0"}).text = f"aec://project/{snapshot.project_id}"
    ElementTree.SubElement(project_node, "data", {"key": "d1"}).text = snapshot.project_id
    ElementTree.SubElement(project_node, "data", {"key": "d2"}).text = "Project"
    for index, obj in enumerate(snapshot.objects):
        node_id = f"n-object-{index}"
        node_ids[obj.id] = node_id
        node = ElementTree.SubElement(graph, "node", {"id": node_id})
        for key, value in (("d0", obj.id), ("d1", obj.project_id), ("d2", obj.type), ("d3", obj.geometry_ref or "")):
            ElementTree.SubElement(node, "data", {"key": key}).text = str(value)
    for index, relation in enumerate(snapshot.relations):
        edge = ElementTree.SubElement(graph, "edge", {"id": f"e-{index}", "source": node_ids.get(relation.subject, relation.subject), "target": node_ids.get(relation.object, relation.object)})
        ElementTree.SubElement(edge, "data", {"key": "d4"}).text = relation.predicate
        ElementTree.SubElement(edge, "data", {"key": "d5"}).text = str(relation.confidence)
    ElementTree.ElementTree(graphml).write(path, encoding="utf-8", xml_declaration=True)
    return path


def write_global_graph_exports(snapshots: list[CAIRSnapshot], directory: str | Path) -> dict[str, Path]:
    """Write one portable global graph package from project CAIR snapshots."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    node_rows = [row for snapshot in snapshots for row in _node_rows(snapshot)]
    relationship_rows = [row for snapshot in snapshots for row in _relationship_rows(snapshot)]
    outputs = {
        "global_nodes": _write_csv(root / "nodes.csv", node_rows, ("id", "project_id", "type", "geometry_ref", "classification_json", "properties_json")),
        "global_relationships": _write_csv(root / "relationships.csv", relationship_rows, ("subject", "predicate", "object", "confidence", "provenance_json")),
    }
    graphml = ElementTree.Element("graphml", {"xmlns": "http://graphml.graphdrawing.org/xmlns"})
    for key_id, name, target in (("d0", "aec_id", "node"), ("d1", "project_id", "node"), ("d2", "type", "node"), ("d3", "geometry_ref", "node"), ("d4", "predicate", "edge"), ("d5", "confidence", "edge")):
        ElementTree.SubElement(graphml, "key", {"id": key_id, "for": target, "attr.name": name, "attr.type": "double" if name == "confidence" else "string"})
    graph = ElementTree.SubElement(graphml, "graph", {"id": "global-aec-graph", "edgedefault": "directed"})
    node_ids: dict[str, str] = {}
    project_ids = sorted({str(row.get("project_id")) for row in node_rows if row.get("project_id")})
    for index, project_id in enumerate(project_ids):
        project_uri = f"aec://project/{project_id}"
        node_id = f"n-project-{index}"
        node_ids[project_uri] = node_id
        node = ElementTree.SubElement(graph, "node", {"id": node_id})
        for key, value in (("d0", project_uri), ("d1", project_id), ("d2", "Project")):
            ElementTree.SubElement(node, "data", {"key": key}).text = value
    for index, row in enumerate(node_rows):
        node_id = f"n-object-{index}"
        node_ids[str(row["id"])] = node_id
        node = ElementTree.SubElement(graph, "node", {"id": node_id})
        for key, column in (("d0", "id"), ("d1", "project_id"), ("d2", "type"), ("d3", "geometry_ref")):
            ElementTree.SubElement(node, "data", {"key": key}).text = str(row.get(column) or "")
    for index, row in enumerate(relationship_rows):
        edge = ElementTree.SubElement(graph, "edge", {"id": f"e-{index}", "source": node_ids.get(str(row["subject"]), str(row["subject"])), "target": node_ids.get(str(row["object"]), str(row["object"]))})
        ElementTree.SubElement(edge, "data", {"key": "d4"}).text = str(row.get("predicate") or "")
        ElementTree.SubElement(edge, "data", {"key": "d5"}).text = str(row.get("confidence") or "")
    global_graphml_path = root / "global.graphml"
    ElementTree.ElementTree(graphml).write(global_graphml_path, encoding="utf-8", xml_declaration=True)
    outputs["global_graphml"] = global_graphml_path
    nodes_parquet = root / "nodes.parquet"
    relationships_parquet = root / "relationships.parquet"
    if _write_parquet(nodes_parquet, node_rows, ("id", "project_id", "type", "geometry_ref", "classification_json", "properties_json")):
        outputs["global_nodes_parquet"] = nodes_parquet
    if _write_parquet(relationships_parquet, relationship_rows, ("subject", "predicate", "object", "confidence", "provenance_json")):
        outputs["global_relationships_parquet"] = relationships_parquet
    return outputs


def write_graph_exports(snapshot: CAIRSnapshot, directory: str | Path) -> dict[str, Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    jsonld_path = root / "project.jsonld"
    jsonld_path.write_text(json.dumps(snapshot_to_jsonld(snapshot), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    node_rows = _node_rows(snapshot)
    relationship_rows = _relationship_rows(snapshot)
    nodes_path = root / "nodes.jsonl"
    legacy_node_rows = [{"id": obj.id, "project_id": obj.project_id, "type": obj.type, "properties": obj.properties} for obj in snapshot.objects]
    nodes_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in legacy_node_rows), encoding="utf-8")
    relations_path = root / "relationships.jsonl"
    relations_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in relationship_rows), encoding="utf-8")
    nodes_csv_path = _write_csv(root / "nodes.csv", node_rows, ("id", "project_id", "type", "geometry_ref", "classification_json", "properties_json"))
    relationships_csv_path = _write_csv(root / "relationships.csv", relationship_rows, ("subject", "predicate", "object", "confidence", "provenance_json"))
    graphml_path = _write_graphml(root / "project.graphml", snapshot)
    outputs = {
        "jsonld": jsonld_path,
        "nodes": nodes_path,
        "relationships": relations_path,
        "nodes_csv": nodes_csv_path,
        "relationships_csv": relationships_csv_path,
        "graphml": graphml_path,
    }
    nodes_parquet = root / "nodes.parquet"
    relationships_parquet = root / "relationships.parquet"
    if _write_parquet(nodes_parquet, node_rows, ("id", "project_id", "type", "geometry_ref", "classification_json", "properties_json")):
        outputs["nodes_parquet"] = nodes_parquet
    if _write_parquet(relationships_parquet, relationship_rows, ("subject", "predicate", "object", "confidence", "provenance_json")):
        outputs["relationships_parquet"] = relationships_parquet
    return outputs
