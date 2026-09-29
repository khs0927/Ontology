"""Offline scheduling decisions. No Drive downloads, LLM calls or CAD launches."""
from dataclasses import dataclass
from .contracts import digest


@dataclass(frozen=True)
class Capabilities:
    acadsharp: bool = False
    oda: bool = False
    native2027: bool = False
    docling: bool = False
    ifcopenshell: bool = False


def plan_ingestion(source, capabilities, previous=None, *, uncertain=False, priority=0):
    if not 0 <= priority <= 100:
        raise ValueError("priority must be between 0 and 100")
    if previous == source:
        return {"status": "UNCHANGED", "jobs": []}
    fmt = source.format.lower()
    routes = {
        "dxf": "existing-ezdxf",
        "dwg": "existing-oda-ezdxf" if capabilities.oda else (
            "acadsharp-readonly" if capabilities.acadsharp else (
                "native2027-readonly" if capabilities.native2027 else None)),
        "ifc": "ifcopenshell" if capabilities.ifcopenshell else None,
        "pdf": "docling" if capabilities.docling else None,
        "docx": "docling" if capabilities.docling else None,
        "xlsx": "docling" if capabilities.docling else None,
        "pptx": "docling" if capabilities.docling else None,
    }
    backend = routes.get(fmt)
    if not backend:
        return {"status": "BLOCKED", "reason": "No verified worker for this format", "jobs": []}
    # Blob parse cache is reusable; source identity/ACL bindings are never reused across files.
    parse_key = digest([source.sha256, fmt, backend, source.parser, source.parser_version])
    stages = ["parse", "bind-source", "index"]
    if uncertain:
        stages.append("native-review" if fmt in {"dwg", "dxf"} and capabilities.native2027 else "manual-review")
    return {"status": "PLANNED", "backend": backend, "parse_cache_key": parse_key,
            "jobs": [{"id": digest([source.revision_id, backend, stage]), "stage": stage,
                      "priority": priority, "source_revision_id": source.revision_id}
                     for stage in stages],
            "requires_mcp_per_entity": False, "implemented_dispatcher": False}


def reciprocal_rank_fusion(rankings, allowed_ids, limit=20, k=60):
    """Fuse candidate IDs only after ACL/current-revision filtering by the catalog.

    Rankings are ordered IDs from lexical/vector/visual/graph providers. Duplicate
    provider results cannot multiply a candidate's score. No fabricated similarities.
    """
    if not 1 <= limit <= 100 or k < 1:
        raise ValueError("Invalid fusion bounds")
    scores = {}
    for ranked in rankings:
        seen = set()
        for rank, candidate in enumerate(ranked, 1):
            if candidate in seen or candidate not in allowed_ids:
                continue
            seen.add(candidate)
            scores[candidate] = scores.get(candidate, 0.0) + 1.0 / (k + rank)
    return [{"id": candidate, "score": score} for candidate, score in
            sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]]
