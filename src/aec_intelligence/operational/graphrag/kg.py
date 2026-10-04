"""Build the canonical knowledge graph (aec.kg_*) from ingested documents, objects and relations.

Graph model (node types -> edges):

    Project -hasPhase-> Phase -hasDrawing-> Drawing          (Project -hasDrawing-> Drawing as well)
    DrawingSeries -hasRevision-> Drawing; Drawing -supersedes-> Drawing (newer -> older, same series)
    Drawing -hasSheet-> Sheet;  Drawing/Sheet -depictsStorey-> Storey;  Project -hasStorey-> Storey
    Storey -hasSpace-> Space;   Drawing -depictsSpace-> Space
    Drawing -hasElements-> ElementGroup (one per drawing x kind, with counts); Storey -hasElements-> ElementGroup
    Drawing -usesSection-> SteelSection; ElementGroup -hasSection-> SteelSection; Project -usesSection-> SteelSection
    Space/Drawing/Project/... -subjectTo-> Requirement (only when a rules file is configured: ArchOntos
    ``archontos-rule-export/1`` subject refs, or legacy space-use lists; see integrations.load_rules)

Entity resolution (graphrag.resolve): folder project ids -> one Project (+ Phase), copies/revisions of a drawing
-> one DrawingSeries, storey spellings -> one Storey per project, room spellings -> one Space per project+storey,
section spellings -> one SteelSection per project. Duplicate files (same sha256) are already one document with
aliases (census v3), so they never become extra nodes.

The build is per project and atomic (delete + insert in one transaction); a fingerprint of the project's
documents makes re-runs skip unchanged projects.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .integrations import (
    catalog_spec_key,
    load_rules,
    load_section_handoffs,
    match_section,
    subject_ref,
)
from .resolve import (
    DISCIPLINE_KO,
    ELEMENT_KINDS,
    KIND_KO,
    canonical_project,
    canonical_room,
    canonical_section,
    canonical_storey,
    drawing_fields,
    short_hash,
    storey_sort_key,
)

log = logging.getLogger(__name__)
MAX_EVIDENCE_IDS = 20


@dataclass
class Node:
    id: str
    project_key: str
    type: str
    name: str
    props: dict[str, Any] = field(default_factory=dict)
    object_ids: list[str] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)
    search_text: str = ""


@dataclass
class ProjectGraph:
    key: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    aliases: set[tuple[str, str, str]] = field(default_factory=set)

    def node(self, node_id: str, type_: str, name: str, **props: Any) -> Node:
        existing = self.nodes.get(node_id)
        if existing is None:
            existing = self.nodes[node_id] = Node(node_id, self.key, type_, name, dict(props))
        else:
            for k, v in props.items():
                existing.props.setdefault(k, v)
        return existing

    def edge(self, src: str, predicate: str, dst: str, **evidence: Any) -> None:
        if src == dst or src not in self.nodes or dst not in self.nodes:
            return
        self.edges.setdefault((src, predicate, dst), evidence)

    def alias(self, alias_type: str, alias: str, node_id: str) -> None:
        if alias:
            self.aliases.add((alias_type, str(alias)[:500], node_id))


def _add_ids(target: list[str], ids, limit: int = MAX_EVIDENCE_IDS) -> None:
    for i in ids:
        if len(target) >= limit:
            return
        if i and i not in target:
            target.append(i)


_DOCS_SQL = """
SELECT d.id, d.project_id, d.name, d.source_key, d.revision, d.source_hash, d.updated_at,
       j.payload->>'top_folder' AS top_folder, j.payload->>'mtime' AS mtime,
       CASE WHEN jsonb_typeof(j.payload->'aliases') = 'array' THEN jsonb_array_length(j.payload->'aliases') ELSE 0 END
         AS n_aliases
FROM aec.documents d
LEFT JOIN (
  SELECT DISTINCT ON (payload->>'document_id') payload->>'document_id' AS did, payload
  FROM aec.jobs WHERE payload ? 'document_id' AND state = 'SUCCEEDED'
  ORDER BY payload->>'document_id', updated_at DESC
) j ON j.did = d.id
"""


def load_documents(conn) -> dict[str, list[dict[str, Any]]]:
    """Documents grouped by canonical project key (with the resolved project name/phase attached)."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute(_DOCS_SQL).fetchall():
        project = canonical_project(row["project_id"], row["top_folder"])
        row = dict(row)
        row["project"] = project
        grouped[project["key"]].append(row)
    return grouped


def fingerprint(docs: list[dict[str, Any]], extra: str = "") -> str:
    parts = sorted(f"{d['id']}:{d['revision']}:{d['updated_at']}" for d in docs)
    return short_hash(extra, *parts, n=32)


def _bbox(payload: dict[str, Any]) -> list[float] | None:
    b = (payload or {}).get("bbox") or {}
    if all(k in b for k in ("min_x", "min_y", "max_x", "max_y")):
        return [b["min_x"], b["min_y"], b["max_x"], b["max_y"]]
    return None


def load_steel_catalog(directory: str | os.PathLike | None) -> dict[str, dict[str, Any]]:
    """hs-steel-cad section tables: ``hs-steel-section-catalog/1`` handoff JSON (validated, preferred) and the
    legacy ``attributes/*.dat`` tables (CP949, whitespace separated, header line; columns Spec Shape M2..M7
    UnitWeight PaintArea Color). Licensed data: read on the PC, never committed."""
    if not directory:
        return {}
    catalog: dict[str, dict[str, Any]] = load_section_handoffs(directory)
    root = Path(directory)
    files = sorted(root.glob("*.dat")) + sorted((root / "attributes").glob("*.dat"))
    for path in files:
        try:
            text = path.read_bytes().decode("cp949", errors="replace")
        except OSError:
            continue
        for line in text.replace("\r", "").split("\n")[1:]:
            fields = line.split()
            if len(fields) < 11:
                continue
            try:
                dims = [float(x) for x in fields[2:8]]
                weight, paint = float(fields[8]), float(fields[9])
            except ValueError:
                continue
            key = catalog_spec_key(fields[0], fields[1], path.stem)
            if key and key not in catalog:
                catalog[key] = {"spec": fields[0], "family": path.stem, "shape": fields[1], "dims_mm": dims,
                                "unit_weight_kg_m": weight, "paint_area_m2_m": paint, "source": "hs-steel-cad"}
    return catalog


def load_requirements(path: str | os.PathLike | None) -> list[dict[str, Any]]:
    """Backward-compatible wrapper around integrations.load_rules (problems are logged)."""
    rules, problems = load_rules(path)
    for p in problems[:20]:
        log.warning("rules file: %s", p)
    return rules


class KnowledgeGraphBuilder:
    def __init__(self, db, *, steel_catalog_dir: str | None = None, rules_file: str | None = None):
        self.db = db
        self.catalog = load_steel_catalog(steel_catalog_dir or os.getenv("AEC_STEEL_CATALOG_DIR"))
        self.requirements = load_requirements(rules_file or os.getenv("AEC_RULES_FILE"))

    # ------------------------------------------------------------------ public
    def build(self, project_key: str | None = None, *, force: bool = False) -> dict[str, Any]:
        stats: dict[str, Any] = {"projects": 0, "skipped": 0, "nodes": 0, "edges": 0, "aliases": 0,
                                 "by_type": Counter(), "resolution": Counter()}
        with self.db.connect(statement_timeout_seconds=600) as conn:
            grouped = load_documents(conn)
            state = {r["project_key"]: r["fingerprint"] for r in
                     conn.execute("SELECT project_key, fingerprint FROM aec.kg_build_state").fetchall()}
            # Catalog/rules content (not just their sizes) is part of every project's fingerprint.
            extra = short_hash(json.dumps(sorted(self.catalog), ensure_ascii=False),
                               json.dumps(self.requirements, ensure_ascii=False, sort_keys=True, default=str), n=16)
            # Projects whose documents are all gone (or moved to another canonical project) go first.
            stale = [k for k in state if k not in grouped and (not project_key or k == project_key)]
            for key in stale:
                with conn.transaction():
                    conn.execute("DELETE FROM aec.kg_nodes WHERE project_key=%s", (key,))
                    conn.execute("DELETE FROM aec.kg_build_state WHERE project_key=%s", (key,))
                    conn.execute("DELETE FROM aec.kg_communities WHERE project_key=%s", (key,))
                conn.commit()
            stats["removed_projects"] = len(stale)
            for key, docs in sorted(grouped.items()):
                if project_key and key != project_key:
                    continue
                fp = fingerprint(docs, extra)
                if not force and state.get(key) == fp:
                    stats["skipped"] += 1
                    continue
                graph = self.project_graph(conn, key, docs)
                self._write(conn, graph, fp)
                stats["projects"] += 1
                stats["nodes"] += len(graph.nodes)
                stats["edges"] += len(graph.edges)
                stats["aliases"] += len(graph.aliases)
                stats["by_type"].update(n.type for n in graph.nodes.values())
                stats["resolution"].update(graph.nodes[f"kg:p:{key}"].props.get("resolution", {}))
                log.info("kg project %s: %d nodes, %d edges", key, len(graph.nodes), len(graph.edges))
        stats["by_type"] = dict(stats["by_type"])
        stats["resolution"] = dict(stats["resolution"])
        return stats

    # ------------------------------------------------------------------ graph assembly
    def project_graph(self, conn, key: str, docs: list[dict[str, Any]]) -> ProjectGraph:
        g = ProjectGraph(key)
        doc_ids = [d["id"] for d in docs]
        project_name = docs[0]["project"]["name"]
        pid = f"kg:p:{key}"
        proj = g.node(pid, "Project", project_name)
        proj.document_ids = doc_ids[:MAX_EVIDENCE_IDS]
        for d in docs:
            g.alias("project_id", d["project_id"], pid)
        g.alias("project_name", project_name, pid)

        per_doc_storeys: dict[str, set[str]] = defaultdict(set)
        drawings: dict[str, Node] = {}
        series: dict[str, list[Node]] = defaultdict(list)
        for d in docs:
            fields = drawing_fields(d["name"], d["source_key"])
            phase = d["project"]["phase"]
            did = f"kg:d:{d['id']}"
            node = g.node(did, "Drawing", d["name"], file_name=d["name"], sheet_number=fields["sheet_number"],
                          title=fields["title"], date=fields["date"], discipline=fields["discipline"],
                          phase=phase or None, revision=d["revision"], mtime=d["mtime"],
                          duplicate_copies=d["n_aliases"], file_type=Path(d["name"]).suffix.lower().lstrip("."))
            node.document_ids = [d["id"]]
            drawings[d["id"]] = node
            g.edge(pid, "hasDrawing", did)
            if phase:
                phid = f"kg:ph:{key}:{short_hash(phase, n=10)}"
                g.node(phid, "Phase", phase, project=project_name)
                g.edge(pid, "hasPhase", phid)
                g.edge(phid, "hasDrawing", did)
            series[fields["series"]].append(node)
            if fields["sheet_number"]:
                g.alias("sheet_number", fields["sheet_number"], did)
            for level in fields["storeys"]:
                per_doc_storeys[d["id"]].add(level)

        self._sheets(conn, g, doc_ids, drawings, per_doc_storeys)
        self._series(g, key, series)
        self._storeys_and_spaces(conn, g, key, project_name, doc_ids, drawings, per_doc_storeys)
        groups = self._elements(conn, g, doc_ids, drawings, per_doc_storeys)
        self._sections(conn, g, key, doc_ids, drawings, groups, sorted({d['project_id'] for d in docs}))
        self._requirements(conn, g, key, docs)
        self._finish_text(g, project_name)

        counts = Counter(n.type for n in g.nodes.values())
        proj.props.update({
            "drawings": len(drawings), "storeys": counts.get("Storey", 0), "spaces": counts.get("Space", 0),
            "sections": counts.get("SteelSection", 0), "phases": sorted(n.name for n in g.nodes.values()
                                                                        if n.type == "Phase"),
            "disciplines": dict(Counter(n.props.get("discipline") or "UNKNOWN" for n in drawings.values())),
            "file_types": dict(Counter(n.props.get("file_type") for n in drawings.values())),
            "source_project_ids": sorted({d["project_id"] for d in docs}),
        })
        proj.props["resolution"] = {**proj.props.get("resolution", {}),
                                    "project_ids_merged": max(0, len(proj.props["source_project_ids"]) - 1)}
        return g

    def _sheets(self, conn, g, doc_ids, drawings, per_doc_storeys):
        rows = conn.execute(
            """SELECT id, document_id, kind, label, storey, payload->'properties' AS props,
                      payload->'evidence'->>'layout' AS layout, payload->'bbox' AS bbox
               FROM aec.objects WHERE document_id = ANY(%s) AND kind IN ('Sheet','View','TitleBlock','Storey')""",
            (doc_ids,)).fetchall()
        for r in rows:
            props = r["props"] or {}
            drawing = drawings.get(r["document_id"])
            if drawing is None:
                continue
            level = canonical_storey(r["storey"] or props.get("storeyName") or props.get("storey"))
            if level:
                per_doc_storeys[r["document_id"]].add(level)
            if props.get("drawingNumber") and not drawing.props.get("sheet_number_titleblock"):
                drawing.props["sheet_number_titleblock"] = props["drawingNumber"]
            if props.get("drawingTitle") and not drawing.props.get("title_titleblock"):
                drawing.props["title_titleblock"] = props["drawingTitle"]
            if props.get("drawing_category") and props.get("drawing_category") != "기타":
                drawing.props.setdefault("categories", [])
                if props["drawing_category"] not in drawing.props["categories"]:
                    drawing.props["categories"].append(props["drawing_category"])
            is_sheet = r["kind"] == "Sheet" or (r["kind"] == "View" and props.get("layout_kind") == "paper")
            if not is_sheet:
                continue
            sid = f"kg:sh:{r['id']}"
            name = " ".join(x for x in (props.get("drawingNumber"), props.get("drawingTitle")) if x) or (
                r["layout"] or r["label"])
            node = g.node(sid, "Sheet", str(name)[:200], layout=r["layout"], number=props.get("drawingNumber"),
                          title=props.get("drawingTitle"), scale=props.get("scale"),
                          category=props.get("drawing_category"), storey=level)
            node.object_ids = [r["id"]]
            node.document_ids = [r["document_id"]]
            g.edge(drawing.id, "hasSheet", sid)
            if props.get("drawingNumber"):
                g.alias("sheet_number", props["drawingNumber"], sid)

    def _series(self, g, key, series):
        merged = 0
        for skey, nodes in series.items():
            if len(nodes) < 2:
                continue
            merged += len(nodes) - 1
            ser_id = f"kg:ser:{key}:{short_hash(skey, n=12)}"
            ordered = sorted(nodes, key=lambda n: (n.props.get("date") or "", n.props.get("mtime") or "", n.name))
            title = ordered[-1].props.get("sheet_number") or ordered[-1].props.get("title") or ordered[-1].name
            ser = g.node(ser_id, "DrawingSeries", str(title)[:200], series_key=skey, revisions=len(nodes),
                         latest=ordered[-1].id)
            ser.document_ids = [d for n in ordered for d in n.document_ids][:MAX_EVIDENCE_IDS]
            for i, n in enumerate(ordered):
                g.edge(ser_id, "hasRevision", n.id, order=i)
                n.props["series_latest"] = n is ordered[-1]
                if i:
                    g.edge(n.id, "supersedes", ordered[i - 1].id, by="date" if n.props.get("date") else "mtime")
        g.nodes[f"kg:p:{key}"].props.setdefault("resolution", {})["drawing_revisions_linked"] = merged

    def _storey_node(self, g, key, project_name, level):
        stid = f"kg:st:{key}:{level}"
        if stid not in g.nodes:
            g.node(stid, "Storey", level, level=level, order=list(storey_sort_key(level)), project=project_name)
            g.edge(f"kg:p:{key}", "hasStorey", stid)
        return stid

    def _storeys_and_spaces(self, conn, g, key, project_name, doc_ids, drawings, per_doc_storeys):
        for doc_id, levels in per_doc_storeys.items():
            for level in levels:
                stid = self._storey_node(g, key, project_name, level)
                g.edge(f"kg:d:{doc_id}", "depictsStorey", stid)
                _add_ids(g.nodes[stid].document_ids, [doc_id])
        for n in [n for n in g.nodes.values() if n.type == "Sheet" and n.props.get("storey")]:
            g.edge(n.id, "depictsStorey", self._storey_node(g, key, project_name, n.props["storey"]))

        rows = conn.execute(
            """SELECT o.id, o.document_id, o.storey, o.payload->'properties' AS props, rs.storey AS rel_storey
               FROM aec.objects o
               LEFT JOIN LATERAL (
                 SELECT s.storey FROM aec.relations r JOIN aec.objects s ON s.id = r.object
                 WHERE r.project_id = o.project_id AND r.subject = o.id AND r.predicate = 'onStorey' LIMIT 1
               ) rs ON true
               WHERE o.document_id = ANY(%s) AND o.kind = 'Space'""", (doc_ids,)).fetchall()
        raw = 0
        for r in rows:
            props = r["props"] or {}
            name = props.get("roomNameNormalized") or props.get("roomName")
            room = canonical_room(name)
            if not room:
                continue
            raw += 1
            level = canonical_storey(r["storey"] or r["rel_storey"])
            doc_levels = per_doc_storeys.get(r["document_id"], set())
            if not level and len(doc_levels) == 1:
                level = next(iter(doc_levels))
            spid = f"kg:sp:{key}:{level or 'NA'}:{short_hash(room, n=12)}"
            node = g.node(spid, "Space", props.get("roomName") or name, room_key=room, storey=level,
                          areas=[], room_numbers=[], occurrences=0)
            node.props["occurrences"] += 1
            if props.get("area") and len(node.props["areas"]) < 10:
                node.props["areas"].append(props["area"])
            if props.get("roomNumber") and props["roomNumber"] not in node.props["room_numbers"]:
                node.props["room_numbers"].append(props["roomNumber"])
            _add_ids(node.object_ids, [r["id"]])
            _add_ids(node.document_ids, [r["document_id"]])
            for alias in props.get("roomNameAliases") or [props.get("roomName")]:
                g.alias("room_name", alias, spid)
            g.edge(f"kg:d:{r['document_id']}", "depictsSpace", spid)
            if level:
                g.edge(self._storey_node(g, key, project_name, level), "hasSpace", spid)
        spaces = sum(1 for n in g.nodes.values() if n.type == "Space")
        g.nodes[f"kg:p:{key}"].props.setdefault("resolution", {}).update(
            {"space_objects": raw, "space_nodes": spaces})

    def _elements(self, conn, g, doc_ids, drawings, per_doc_storeys):
        rows = conn.execute(
            """SELECT document_id, kind, count(*) AS n, (array_agg(id ORDER BY id))[1:%s] AS ids,
                      count(*) FILTER (WHERE payload->>'state' = 'OBSERVED') AS observed
               FROM aec.objects WHERE document_id = ANY(%s) AND kind = ANY(%s)
               GROUP BY 1, 2""", (MAX_EVIDENCE_IDS, doc_ids, list(ELEMENT_KINDS))).fetchall()
        groups: dict[tuple[str, str], str] = {}
        for r in rows:
            drawing = drawings.get(r["document_id"])
            if drawing is None:
                continue
            egid = f"kg:eg:{r['document_id']}:{r['kind']}"
            node = g.node(egid, "ElementGroup", f"{drawing.name} {KIND_KO.get(r['kind'], r['kind'])}",
                          kind=r["kind"], kind_ko=KIND_KO.get(r["kind"], r["kind"]), count=int(r["n"]),
                          observed=int(r["observed"]), drawing=drawing.name)
            node.object_ids = list(r["ids"] or [])
            node.document_ids = [r["document_id"]]
            g.edge(drawing.id, "hasElements", egid)
            levels = per_doc_storeys.get(r["document_id"], set())
            if len(levels) == 1:
                level = next(iter(levels))
                node.props["storey"] = level
                g.edge(f"kg:st:{g.key}:{level}", "hasElements", egid)
            groups[(r["document_id"], r["kind"])] = egid
            drawing.props.setdefault("element_counts", {})[r["kind"]] = int(r["n"])
        return groups

    def _sections(self, conn, g, key, doc_ids, drawings, groups, project_ids):
        rows = conn.execute(
            """SELECT o.id, o.document_id, o.payload->'properties'->>'sectionDesignation' AS des
               FROM aec.objects o WHERE o.document_id = ANY(%s) AND o.kind = 'SteelSection'
                 AND o.payload->'properties' ? 'sectionDesignation'""", (doc_ids,)).fetchall()
        sec_of_object: dict[str, str] = {}
        spellings = 0
        for r in rows:
            des = canonical_section(r["des"])
            if not des:
                continue
            spellings += 1
            secid = f"kg:sec:{key}:{des}"
            entry, how = match_section(des, self.catalog)
            node = g.node(secid, "SteelSection", des, designation=des, occurrences=0, catalog=entry,
                          catalog_match=how)
            node.props["occurrences"] += 1
            _add_ids(node.object_ids, [r["id"]])
            _add_ids(node.document_ids, [r["document_id"]])
            g.alias("section", r["des"], secid)
            sec_of_object[r["id"]] = secid
            g.edge(f"kg:d:{r['document_id']}", "usesSection", secid)
            g.edge(f"kg:p:{key}", "usesSection", secid)
        if sec_of_object:
            members = conn.execute(
                """SELECT m.document_id, m.kind, r.object AS section
                   FROM aec.relations r JOIN aec.objects m ON m.id = r.subject
                   WHERE r.project_id = ANY(%s) AND r.document_id = ANY(%s) AND r.predicate = 'hasSection'""",
                (project_ids, doc_ids)).fetchall()
            for m in members:
                secid = sec_of_object.get(m["section"])
                egid = groups.get((m["document_id"], m["kind"]))
                if secid and egid:
                    g.edge(egid, "hasSection", secid)
                    used = g.nodes[secid].props.setdefault("member_kinds", {})
                    used[m["kind"]] = used.get(m["kind"], 0) + 1
        g.nodes[f"kg:p:{key}"].props.setdefault("resolution", {}).update(
            {"section_objects": spellings, "section_nodes": sum(1 for n in g.nodes.values()
                                                                if n.type == "SteelSection")})

    def _requirements(self, conn, g, key, docs):
        """Link rules to the nodes they apply to. ArchOntos subject refs resolve, most specific first, by
        object id (node evidence, else the object's drawing), ``locator.kg_node``, ``locator.document_id`` /
        ``source_id`` (drawing) and finally ``project_id`` (project). A ref whose byte revision differs from the
        drawing's current hash is linked with ``stale: true`` so a reviewer re-checks it. Legacy space-use
        rules link every Space with that room key."""
        if not self.requirements:
            return
        by_id = {d["id"]: d for d in docs}
        by_hash = {d["source_hash"]: d for d in docs if d.get("source_hash")}
        project_ids = {d["project_id"] for d in docs}
        pid = f"kg:p:{key}"
        node_of_object: dict[str, str] = {}
        for n in g.nodes.values():
            for oid in n.object_ids:
                node_of_object.setdefault(oid, n.id)
        wanted = [s["object_id"] for r in self.requirements for s in r["subjects"]
                  if s.get("object_id") and s["object_id"] not in node_of_object]
        object_doc = {}
        if wanted:
            object_doc = {r["id"]: r["document_id"] for r in conn.execute(
                "SELECT id, document_id FROM aec.objects WHERE id = ANY(%s) AND document_id = ANY(%s)",
                (wanted, list(by_id))).fetchall()}
        spaces = [n for n in g.nodes.values() if n.type == "Space"]
        unresolved = 0
        for req in self.requirements:
            links: dict[str, dict[str, Any]] = {}
            for s in spaces:
                if s.props.get("room_key") in req["space_uses"]:
                    links.setdefault(s.id, {"via": "space_use"})
            for ref in req["subjects"]:
                loc = ref.get("locator") or {}
                doc = None
                target = None
                if ref.get("object_id"):
                    target = node_of_object.get(ref["object_id"])
                    if target is None and ref["object_id"] in object_doc:
                        doc = by_id[object_doc[ref["object_id"]]]
                        target = f"kg:d:{doc['id']}"
                if target is None and loc.get("kg_node") in g.nodes:
                    target = loc["kg_node"]
                if target is None:
                    doc = by_id.get(loc.get("document_id")) or by_hash.get(ref.get("source_id"))
                    if doc:
                        target = f"kg:d:{doc['id']}"
                if target is None and not ref.get("object_id") and ref["project_id"] in project_ids:
                    target = pid
                if target is None or target not in g.nodes:
                    unresolved += ref["project_id"] in project_ids
                    continue
                if doc is None:
                    tdocs = g.nodes[target].document_ids
                    doc = by_id.get(tdocs[0]) if len(tdocs) == 1 else None
                evidence: dict[str, Any] = {"via": "subject_ref"}
                if ref.get("source_byte_revision_id") and doc is not None:
                    evidence["stale"] = ref["source_byte_revision_id"] != (doc.get("source_hash") or "")
                links[target] = evidence
            if not links:
                continue
            rid = f"kg:req:{key}:{short_hash(str(req['id']), str(req.get('version_label') or ''), n=12)}"
            node = g.node(rid, "Requirement", req["title"], rule_id=req["id"], version_label=req.get("version_label"),
                          text=req.get("text"), source=req.get("source"), has_logic=bool(req.get("logic_expr")),
                          outcome=req.get("outcome"),
                          stale_links=sum(1 for e in links.values() if e.get("stale")))
            docs_seen: list[str] = []
            for target, evidence in sorted(links.items()):
                g.edge(target, "subjectTo", rid, **evidence)
                t = g.nodes[target]
                _add_ids(docs_seen, t.document_ids)
                if t.type in ("Space", "Drawing") and "aec_subject_ref" not in t.props:
                    d = by_id.get(t.document_ids[0]) if t.document_ids else None
                    t.props["aec_subject_ref"] = subject_ref(
                        d["project_id"] if d else next(iter(sorted(project_ids))),
                        object_id=t.object_ids[0] if t.type == "Space" and t.object_ids else None,
                        document=d, locator={"kg_node": t.id})
            node.document_ids = docs_seen
        if unresolved:
            g.nodes[pid].props.setdefault("resolution", {})["unresolved_subject_refs"] = unresolved

    def _finish_text(self, g, project_name):
        for n in g.nodes.values():
            p = n.props
            bits = [n.type, n.name, project_name]
            if n.type == "Drawing":
                bits += ["도면", p.get("sheet_number") or "", p.get("title") or "", p.get("title_titleblock") or "",
                         DISCIPLINE_KO.get(p.get("discipline") or "", ""), p.get("phase") or "",
                         " ".join(p.get("categories") or [])]
            elif n.type == "Space":
                bits += ["실", "공간", p.get("storey") or "", " ".join(p.get("room_numbers") or [])]
            elif n.type == "Storey":
                bits += ["층"]
            elif n.type == "ElementGroup":
                bits += [p.get("kind_ko") or "", p.get("storey") or ""]
            elif n.type == "SteelSection":
                bits += ["철골", "형강", "단면"]
            elif n.type == "Sheet":
                bits += ["시트", "도곽", p.get("layout") or "", p.get("category") or ""]
            n.search_text = re.sub(r"\s+", " ", " ".join(str(b) for b in bits if b)).strip()[:2000]

    # ------------------------------------------------------------------ persistence
    def _write(self, conn, g: ProjectGraph, fp: str) -> None:
        with conn.transaction():
            # A document can move to another canonical project (container folders are data-driven), so
            # its old node ids are dropped too, not only this project's rows.
            conn.execute("DELETE FROM aec.kg_nodes WHERE project_key=%s OR id = ANY(%s)", (g.key, list(g.nodes)))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO aec.kg_nodes(id, project_key, type, name, props, object_ids, document_ids,
                                                search_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [(n.id, n.project_key, n.type, n.name, json.dumps(n.props, ensure_ascii=False, default=str),
                      n.object_ids, n.document_ids, n.search_text) for n in g.nodes.values()])
                cur.executemany(
                    "INSERT INTO aec.kg_edges(src, predicate, dst, project_key, evidence) VALUES (%s,%s,%s,%s,%s)",
                    [(s, p, d, g.key, json.dumps(ev, ensure_ascii=False, default=str))
                     for (s, p, d), ev in g.edges.items()])
                cur.executemany(
                    "INSERT INTO aec.kg_aliases(alias_type, alias, node_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    sorted(g.aliases))
            conn.execute(
                """INSERT INTO aec.kg_build_state(project_key, fingerprint, nodes, edges, built_at)
                   VALUES (%s,%s,%s,%s,now())
                   ON CONFLICT (project_key) DO UPDATE SET fingerprint=EXCLUDED.fingerprint, nodes=EXCLUDED.nodes,
                     edges=EXCLUDED.edges, built_at=now()""", (g.key, fp, len(g.nodes), len(g.edges)))
        conn.commit()  # each project is visible (and kept) as soon as it is built


def kg_stats(db) -> dict[str, Any]:
    with db.connect() as conn:
        types = {r["type"]: r["n"] for r in conn.execute(
            "SELECT type, count(*) AS n FROM aec.kg_nodes GROUP BY 1 ORDER BY 2 DESC").fetchall()}
        preds = {r["predicate"]: r["n"] for r in conn.execute(
            "SELECT predicate, count(*) AS n FROM aec.kg_edges GROUP BY 1 ORDER BY 2 DESC").fetchall()}
        comm = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM aec.kg_communities GROUP BY 1").fetchall()}
        projects = conn.execute("SELECT count(*) AS n FROM aec.kg_build_state").fetchone()["n"]
    return {"projects": projects, "nodes": types, "edges": preds, "communities": comm}
