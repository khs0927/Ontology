"""Read existing parser results; preserve canonical object IDs and evidence verbatim."""
from copy import deepcopy
import json
import re

from .contracts import digest


def adapt_snapshot(snapshot, source):
    if snapshot.get("project_id") != source.project_id:
        raise ValueError("Project mismatch")
    operational = "document_id" in snapshot
    if operational:
        if snapshot.get("source_hash") != source.sha256 or str(snapshot.get("revision")) != source.revision:
            raise ValueError("Snapshot revision/hash does not match source manifest")
        if snapshot.get("units", "unknown") != source.units:
            raise ValueError("Snapshot units do not match manifest")
    elif snapshot.get("schema_version") != "0.1.0":
        raise ValueError("Supported inputs: CAIR 0.1.0 or operational snapshot")
    if snapshot.get("status", "SUCCESS") not in {"SUCCESS", "SUCCESS_WITH_WARNINGS", "PARTIAL", "REQUIRES_REVIEW"}:
        raise ValueError("Failed snapshots cannot be indexed")
    records, ids = [], set()
    for obj in snapshot.get("objects", []):
        external_id = obj.get("id")
        if not external_id or external_id in ids:
            raise ValueError("Missing or duplicate canonical object ID")
        ids.add(external_id)
        evidence = deepcopy(obj.get("evidence") or {}) if operational else deepcopy(obj.get("provenance") or {})
        obj_source = obj.get("source") or {}
        original_hash = evidence.get("source_hash")
        if original_hash is not None and original_hash != source.sha256:
            raise ValueError("Object provenance hash mismatch; fix upstream, do not silently relabel")
        if not operational and original_hash != source.sha256:
            raise ValueError("CAIR import requires per-source provenance; split multi-source snapshots")
        properties = deepcopy(obj.get("properties") or {})
        # Do not invent ModelSpace or infer a CAD handle from a generic object ID.
        layout = evidence.get("layout") or properties.get("layout")
        handle = evidence.get("handle") if operational else obj_source.get("entity_id")
        if source.format.lower() not in {"dwg", "dxf"} or not isinstance(handle, str) or not re.fullmatch(r"[0-9a-fA-F]+", handle):
            handle = None
        classification = obj.get("classification") or properties.get("classification") or {}
        text = obj.get("search_text") or obj.get("label") or " ".join([
            str(obj.get("type", "")), str(obj_source.get("layer", "")),
            json.dumps(properties, ensure_ascii=False, sort_keys=True),
        ])
        record = {
            "schema": "drawing-context/1",
            "id": digest([source.revision_id, external_id]),
            "canonical_id": external_id,
            "source": source.to_dict(),
            "locator": {
                "source_id": source.source_id, "revision_id": source.revision_id,
                "layout": layout, "handle": handle,
                "instance_path": deepcopy(evidence.get("instance_path", [])),
                "bbox": deepcopy(obj.get("bbox") or {}),
                "coordinate_system": evidence.get("coordinate_system"),
                "geometry_ref": obj.get("geometry_ref") or evidence.get("geometry_path"),
                "page": evidence.get("page"),
            },
            "kind": obj.get("type", "Unknown"),
            "state": obj.get("state") or classification.get("state", "REQUIRES_REVIEW"),
            "text": text,
            "evidence": evidence,
            "canonical_object": deepcopy(obj),
        }
        records.append(record)
    mapping = {r["canonical_id"]: r["id"] for r in records}
    relations = []
    for edge in snapshot.get("relations", []):
        # Keep unresolved/external links explicit, rather than claiming a resolved graph.
        resolved = edge.get("subject") in mapping and edge.get("object") in mapping
        relations.append({"canonical_relation": deepcopy(edge), "resolved": resolved,
                          "subject": mapping.get(edge.get("subject")),
                          "object": mapping.get(edge.get("object")),
                          "source_revision_id": source.revision_id})
    return {"schema": "drawing-context-bundle/1", "source": source.to_dict(),
            "records": records, "relations": relations,
            "warnings": deepcopy(snapshot.get("warnings", []))}


def ragflow_projection(records):
    """Internal export DTO, NOT a claim of compatibility with every RAGFlow HTTP API.

    Deployment adapter maps these to the version-pinned chunk API and records
    remote dataset/document/chunk IDs. No network or canonical mutation here.
    """
    return [{"external_id": r["id"], "content": r["text"], "metadata": {
        "canonical_id": r["canonical_id"], "source_id": r["locator"]["source_id"],
        "revision_id": r["locator"]["revision_id"], "project_id": r["source"]["project_id"],
        "sha256": r["source"]["sha256"], "state": r["state"],
    }} for r in records]
