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


def typed_candidate_space(candidates, allowed_ids, limit=20):
    """Build a numbered, immutable choice space after ACL/revision filtering.

    Agents choose an integer option instead of inventing an object ID. This is
    the CAD-side equivalent of Jev-style constrained choice. Selection alone
    never authorizes a mutation; native live verification is still mandatory.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    options, seen = [], set()
    for candidate in candidates:
        candidate_id = candidate.get("id") if isinstance(candidate, dict) else candidate
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen:
            continue
        if candidate_id not in allowed_ids:
            continue
        seen.add(candidate_id)
        option = {
            "choice": len(options) + 1,
            "id": candidate_id,
        }
        if isinstance(candidate, dict) and "score" in candidate:
            option["score"] = candidate["score"]
        options.append(option)
        if len(options) >= limit:
            break
    return {
        "schema": "power-cad-typed-choice/1",
        "options": options,
        "may_execute_mutation": False,
        "requires_live_verification": True,
    }


def resolve_typed_choice(space, choice):
    """Resolve only an integer listed in a previously built choice space."""
    if type(choice) is not int:
        raise ValueError("choice must be an integer option, not a generated object ID")
    options = space.get("options") if isinstance(space, dict) else None
    if not isinstance(options, list):
        raise ValueError("invalid typed choice space")
    match = next((option for option in options if option.get("choice") == choice), None)
    if match is None:
        raise ValueError("choice is outside the allowed candidate space")
    return {
        "schema": "power-cad-typed-selection/1",
        "choice": choice,
        "selected_id": match["id"],
        "may_execute_mutation": False,
        "requires_live_verification": True,
    }
