"""Cross-repository contracts used by the knowledge graph.

* **ArchOntos** (law/rule ontology): legal assertions point at AEC subjects through
  ``archontos-aec-subject-ref/1`` (``archontos.domain.contracts.AecSubjectRef``). This module builds those
  references for KG nodes, validates incoming ones with the same rules, loads exported rules
  (``archontos-rule-export/1``) and exports per-project facts (``aec-facts-export/1``) that the ArchOntos rule
  engine evaluates (``python -m archontos.integration.ontology``).
* **hs-steel-cad** (steel detailing): section tables arrive either as the licensed legacy ``attributes/*.dat``
  files or as the ``hs-steel-section-catalog/1`` handoff JSON that ``HsSteel.Assets.SectionCatalogHandoff``
  emits. Both are normalised to the drawing-side ``canonical_section`` key.

Nothing here talks to the network; all inputs are local files or the Ontology database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .resolve import canonical_room, canonical_section, storey_sort_key

log = logging.getLogger(__name__)

SUBJECT_REF_SCHEMA = "archontos-aec-subject-ref/1"
RULE_EXPORT_SCHEMA = "archontos-rule-export/1"
FACTS_EXPORT_SCHEMA = "aec-facts-export/1"
SECTION_HANDOFF_SCHEMA = "hs-steel-section-catalog/1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
STEEL_DENSITY_KG_M_PER_MM2 = 0.00785  # 7.85 t/m3 -> kg per metre per mm2 of cross-section
MAX_RULES = 5000
MAX_SUBJECTS_PER_RULE = 10_000  # same bound as ArchOntos AssertionContract.applies_to


# --------------------------------------------------------------------------- ArchOntos subject references
def parser_revision_id(document_id: str, revision: int | None, source_hash: str | None) -> str:
    """Ontology-defined parser revision identity: changes whenever the document is re-parsed (revision bump)
    or its bytes change. 64 hex chars as AecSubjectRef requires."""
    raw = f"aec-parser-revision/1|{document_id}|{int(revision or 0)}|{source_hash or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def subject_ref(project_id: str, *, object_id: str | None = None, document: dict[str, Any] | None = None,
                locator: dict[str, Any] | None = None) -> dict[str, Any]:
    """An ``archontos-aec-subject-ref/1`` dict. The revision trio is set only when the document's byte hash is
    known (ArchOntos requires all three or none)."""
    ref: dict[str, Any] = {"schema": SUBJECT_REF_SCHEMA, "project_id": str(project_id),
                           "object_id": object_id, "locator": dict(locator or {})}
    if document:
        ref["locator"].setdefault("document_id", document.get("id"))
        sh = str(document.get("source_hash") or "").lower()
        if _HEX64.match(sh):
            ref["source_id"] = sh
            ref["source_byte_revision_id"] = sh
            ref["parser_revision_id"] = parser_revision_id(document["id"], document.get("revision"), sh)
    return ref


def validate_subject_ref(ref: Any) -> list[str]:
    """Mirror of ArchOntos ``AecSubjectRef`` validation (kept dependency-free). Returns problems; [] = valid."""
    if not isinstance(ref, dict):
        return ["subject ref must be an object"]
    problems = []
    if ref.get("schema", SUBJECT_REF_SCHEMA) != SUBJECT_REF_SCHEMA:
        problems.append(f"unsupported schema {ref.get('schema')!r}")
    if not isinstance(ref.get("project_id"), str) or not ref["project_id"]:
        problems.append("project_id is required")
    if ref.get("object_id") is not None and (not isinstance(ref["object_id"], str) or not ref["object_id"]):
        problems.append("object_id must be a non-empty string")
    trio = [ref.get(k) for k in ("source_id", "source_byte_revision_id", "parser_revision_id")]
    if any(v is not None for v in trio):
        if not all(v is not None for v in trio):
            problems.append("source_id, source_byte_revision_id and parser_revision_id must be supplied together")
        elif not all(isinstance(v, str) and _HEX64.match(v) for v in trio):
            problems.append("revision ids must be 64 lowercase hex chars")
    if not isinstance(ref.get("locator", {}), dict):
        problems.append("locator must be an object")
    return problems


def load_rules(path: str | Path | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Rules to link into the KG. Two accepted shapes:

    * ``{"schema": "archontos-rule-export/1", "rules": [{"rule_id", "version_label", "title", "text", "source",
      "logic_expr", "applies_to": [AecSubjectRef...], "space_uses": [...]}]}`` (the ArchOntos contract);
    * the legacy list ``[{"id", "title", "text", "applies_to": ["계단실", ...], "source"}]`` (space uses only).

    Returns ``(rules, problems)``; invalid subject refs are dropped and reported, never linked."""
    problems: list[str] = []
    if not path or not Path(path).is_file():
        return [], problems
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if data.get("schema") != RULE_EXPORT_SCHEMA:
            return [], [f"unsupported rules schema {data.get('schema')!r}"]
        raw_rules = data.get("rules") or []
    elif isinstance(data, list):
        raw_rules = data
    else:
        return [], ["rules file must be a list or an archontos-rule-export/1 object"]
    rules = []
    for i, raw in enumerate(raw_rules[:MAX_RULES]):
        if not isinstance(raw, dict):
            problems.append(f"rules[{i}]: not an object")
            continue
        rid = raw.get("rule_id") or raw.get("id")
        if not rid:
            problems.append(f"rules[{i}]: rule_id missing")
            continue
        uses, subjects = list(raw.get("space_uses") or []), []
        for j, item in enumerate((raw.get("applies_to") or [])[:MAX_SUBJECTS_PER_RULE]):
            if isinstance(item, str):  # legacy: space use name
                uses.append(item)
                continue
            bad = validate_subject_ref(item)
            if bad:
                problems.append(f"rules[{i}].applies_to[{j}]: {'; '.join(bad)}")
            else:
                subjects.append(item)
        if not uses and not subjects:
            problems.append(f"rules[{i}] ({rid}): no space_uses and no valid applies_to")
            continue
        rules.append({"id": str(rid), "version_label": raw.get("version_label"),
                      "title": str(raw.get("title") or rid)[:200], "text": raw.get("text"),
                      "source": raw.get("source"), "logic_expr": raw.get("logic_expr"),
                      "outcome": raw.get("outcome") if raw.get("outcome") in ("PASS", "FAIL", "REVIEW") else None,
                      "space_uses": sorted({u for u in (canonical_room(x) for x in uses) if u}),
                      "subjects": subjects})
    if len(raw_rules) > MAX_RULES:
        problems.append(f"only the first {MAX_RULES} rules were read")
    return rules, problems


# --------------------------------------------------------------------------- facts export for ArchOntos
def _level_number(level: str) -> tuple[str, int] | None:
    if level.endswith("F") and level[:-1].isdigit():
        return "above", int(level[:-1])
    if level.startswith("B") and level[1:].isdigit():
        return "below", int(level[1:])
    return None


def project_facts(db, project_key: str) -> dict[str, Any] | None:
    """``aec-facts-export/1`` for one canonical project: rule-engine facts derived from the KG, each with
    provenance, plus the AEC subject references a decision can attach to.

    Only facts the drawings positively show are emitted. Anything else stays absent so an ArchOntos rule that
    needs it evaluates to REVIEW (fail closed), e.g. ``stair.direct_count`` (stairs are not reliably countable
    from 2D drawings) or ``building.basement_count`` when no basement storey was seen."""
    with db.connect() as conn:
        proj = conn.execute("SELECT id, name, props FROM aec.kg_nodes WHERE project_key=%s AND type='Project'",
                            (project_key,)).fetchone()
        if proj is None:
            return None
        nodes = conn.execute(
            """SELECT id, type, name, props, document_ids, object_ids FROM aec.kg_nodes
               WHERE project_key=%s AND type IN ('Storey','Space','SteelSection','Drawing')""",
            (project_key,)).fetchall()
        doc_ids = sorted({d for n in nodes if n["type"] == "Drawing" for d in n["document_ids"]})
        docs = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, project_id, name, revision, source_hash FROM aec.documents WHERE id = ANY(%s)",
            (doc_ids,)).fetchall()}
    facts: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    storeys = [n for n in nodes if n["type"] == "Storey"]
    above = [(lv[1], n) for n in storeys if (lv := _level_number(n["name"])) and lv[0] == "above"]
    below = [(lv[1], n) for n in storeys if (lv := _level_number(n["name"])) and lv[0] == "below"]
    if above:
        top, node = max(above, key=lambda t: t[0])
        facts.setdefault("building", {})["floor_count"] = top
        provenance["building.floor_count"] = {
            "method": "highest above-ground storey label depicted in the project's drawings (nF)",
            "kg_nodes": [node["id"]], "documents": list(node["document_ids"])[:10]}
    if below:
        deep, node = max(below, key=lambda t: t[0])
        facts.setdefault("building", {})["basement_count"] = deep
        provenance["building.basement_count"] = {
            "method": "deepest basement storey label depicted (Bn); absent when no basement is drawn",
            "kg_nodes": [node["id"]], "documents": list(node["document_ids"])[:10]}
    if storeys:
        facts.setdefault("building", {})["storeys"] = sorted((n["name"] for n in storeys), key=storey_sort_key)
        provenance["building.storeys"] = {"method": "canonical storey nodes", "kg_nodes": [n["id"] for n in storeys]}
    spaces = [n for n in nodes if n["type"] == "Space"]
    if spaces:
        facts["space"] = {"uses": sorted({(n["props"] or {}).get("room_key") or n["name"] for n in spaces})}
        provenance["space.uses"] = {"method": "merged room names (Space nodes)", "count": len(spaces)}
    sections = [n for n in nodes if n["type"] == "SteelSection"]
    if sections:
        facts["steel"] = {"sections": sorted(n["name"] for n in sections),
                          "catalogued_sections": sorted(n["name"] for n in sections
                                                        if (n["props"] or {}).get("catalog"))}
        provenance["steel.sections"] = {"method": "canonical section designations; catalog = hs-steel-cad",
                                        "kg_nodes": [n["id"] for n in sections][:50]}
    props = proj["props"] or {}
    subjects = [{"kg_node": proj["id"], "type": "Project", "name": proj["name"],
                 "ref": subject_ref(pid, locator={"kg_node": proj["id"], "project_key": project_key})}
                for pid in props.get("source_project_ids") or []]
    for n in nodes:
        if n["type"] != "Drawing" or not n["document_ids"]:
            continue
        doc = docs.get(n["document_ids"][0])
        if doc:
            subjects.append({"kg_node": n["id"], "type": "Drawing", "name": n["name"],
                             "ref": subject_ref(doc["project_id"], document=doc, locator={"kg_node": n["id"]})})
    return {"schema": FACTS_EXPORT_SCHEMA, "producer": "khs0927/Ontology", "project_key": project_key,
            "project_name": proj["name"], "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "facts": facts, "provenance": provenance, "subjects": subjects,
            "absent_by_design": ["stair.direct_count", "building.use_group", "building.gross_floor_area"]}


# --------------------------------------------------------------------------- hs-steel-cad section catalog
_PREFIX_ALIASES = (("%%C", "PIPE-"), ("Φ", "PIPE-"), ("Ø", "PIPE-"), ("ø", "PIPE-"), ("[", "C-"),
                   ("□", "SQ-"), ("ㅁ", "SQ-"))


def catalog_spec_key(spec: str, shape: str | None = None, family: str | None = None) -> str | None:
    """hs-steel catalog spec to the drawing-side canonical key: ``[150x75x6.5x10`` -> ``C-150x75x6.5x10``,
    ``ㅁ150x150x6`` -> ``SHS-150x150x6`` (``RHS-`` when not square), ``Φ60.5x3.2`` -> ``PIPE-60.5x3.2``,
    flat bar ``F50x6`` -> ``FB-6x50``; others via ``canonical_section`` (``H100x50x5x7`` -> ``H-100x50x5x7``)."""
    text = str(spec or "").strip()
    if not text:
        return None
    for raw, repl in _PREFIX_ALIASES:
        if text.startswith(raw):
            text = repl + text[len(raw):].lstrip("-")
            break
    else:
        if (family or "").upper() == "FLAT-BAR" and re.match(r"^F-?\d", text, re.I):
            text = "FB-" + re.sub(r"^F-?", "", text, flags=re.I)
    if text.upper().startswith("SQ-"):
        dims = [d for d in re.split(r"[xX×*]", text[3:]) if d]
        try:
            square = len(dims) >= 2 and float(dims[0]) == float(dims[1])
        except ValueError:
            return canonical_section(text)
        text = ("SHS-" if square else "RHS-") + text[3:]
    return canonical_section(text)


def load_section_handoffs(directory: str | Path | None) -> dict[str, dict[str, Any]]:
    """``hs-steel-section-catalog/1`` JSON files (``*.json`` in the catalog folder or its ``handoff/``)."""
    catalog: dict[str, dict[str, Any]] = {}
    if not directory:
        return catalog
    root = Path(directory)
    for path in sorted(root.glob("*.json")) + sorted((root / "handoff").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("section handoff %s: unreadable", path.name)
            continue
        if not isinstance(data, dict) or data.get("schema") != SECTION_HANDOFF_SCHEMA:
            continue
        if data.get("validation_status") != "PASS" or not _HEX64.match(str(data.get("source_sha256") or "")):
            log.warning("section handoff %s: not PASS or no source hash, ignored", path.name)
            continue
        for row in data.get("rows") or []:
            if not isinstance(row, dict) or not row.get("spec"):
                continue
            key = catalog_spec_key(row["spec"], row.get("shape"), row.get("family") or data.get("family"))
            if key and key not in catalog:
                catalog[key] = {"spec": row["spec"], "family": row.get("family") or data.get("family"),
                                "shape": row.get("shape"), "dims_mm": row.get("dimensions_mm"),
                                "unit_weight_kg_m": row.get("unit_weight_kg_m"),
                                "paint_area_m2_m": row.get("paint_area_m2_m"), "source": "hs-steel-cad",
                                "contract": SECTION_HANDOFF_SCHEMA, "source_sha256": data["source_sha256"],
                                "contract_digest": data.get("contract_digest")}
    return catalog


def match_section(designation: str, catalog: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """Catalog entry for a canonical drawing designation and how it matched:
    ``exact``; ``nominal`` (``H-350x350`` / ``H-300x150x6.5`` -> the single catalog entry extending the written
    dimensions; ambiguous -> no match);
    ``computed`` (plates/flat bars ``PL-t x w`` / ``FB-t x w``: weight from 7.85 t/m3, no catalog row)."""
    if not designation:
        return None, None
    if designation in catalog:
        return catalog[designation], "exact"
    if re.match(r"^(H|BH|C|T|L|SHS|RHS)-\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)+$", designation):
        # Drawings often omit trailing thicknesses (H-300x150, H-300x150x6.5): a unique catalog entry that
        # extends the written dimensions is the section; two or more candidates are left unmatched.
        prefix = f"{designation}x"
        hits = [k for k in catalog if k.startswith(prefix)]
        if len(hits) == 1:
            return catalog[hits[0]], "nominal"
    m = re.match(r"^(PL|FB)-(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)$", designation)
    if m:
        t, w = float(m.group(2)), float(m.group(3))
        return {"spec": designation, "family": "PLATE" if m.group(1) == "PL" else "FLAT-BAR", "dims_mm": [t, w],
                "unit_weight_kg_m": round(t * w * STEEL_DENSITY_KG_M_PER_MM2, 3),
                "paint_area_m2_m": round(2 * (t + w) / 1000, 4), "source": "computed (7.85 t/m3)"}, "computed"
    return None, None
