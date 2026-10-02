"""Read-only element catalog over the operational Postgres projection.

These queries let an agent (or power-cad-mcp) discover what a parsed drawing set
contains without being told element names: first the table of contents
(``element_catalog``), then filtered element lists (``find_elements``), the block
library (``block_catalog``), the sheet index (``drawing_index``) and the graph
neighbourhood of one element (``element_context``).

All filters read ``aec.objects.payload`` (the parser observation: id, type, label,
search_text, state, evidence, bbox, properties, storey) and tolerate missing keys,
so documents parsed by older parser versions still appear.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Iterable

from ..classifier import normalize_storey
from .db import Database, graph_name


MAX_LIMIT = 500
DEFAULT_LIMIT = 50

# Korean/English vocabulary per kind. Agents can pass any of these words as ``kind``.
KIND_ALIASES: dict[str, str] = {
    "Wall": "벽 벽체 외벽 내벽 칸막이벽",
    "Door": "문 출입문 방화문 도어",
    "Window": "창 창호 창문 윈도우",
    "Slab": "슬래브 바닥",
    "Column": "기둥",
    "Beam": "보 거더",
    "Space": "실 공간 방 실명",
    "Storey": "층",
    "Stair": "계단",
    "Furniture": "가구 집기 비품",
    "BlockDefinition": "블록 블럭 블록정의 심볼",
    "TitleBlock": "도곽 표제란 타이틀블록",
    "SteelSection": "철골 형강 강재 단면",
    "Layer": "레이어 도면층",
    "View": "뷰 시트 레이아웃 도면",
    "Annotation": "주석 문자 텍스트",
    "Dimension": "치수",
    "Grid": "그리드 통심 축선",
    "Document": "도면파일 문서",
    "Page": "페이지",
    "Hatch": "해치 패턴",
    "Railing": "난간",
    "Roof": "지붕",
    "Ceiling": "천장 천정",
    "Opening": "개구부",
}

# drawing_category canonical Korean value -> accepted synonyms
CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "평면도": ("plan", "floor_plan", "floorplan", "평면"),
    "입면도": ("elevation", "입면"),
    "단면도": ("section", "단면"),
    "상세도": ("detail", "details", "상세"),
    "창호도": ("window_schedule", "door_schedule", "창호", "창호일람표"),
    "배치도": ("site_plan", "site", "배치"),
    "천장도": ("ceiling_plan", "rcp", "천장평면도", "천정도"),
    "구조도": ("structural", "structure", "구조"),
    "전개도": ("interior_elevation", "전개"),
    "계단상세도": ("stair_detail",),
}


# --- payload accessors (SQL fragments; ``o`` is the aec.objects alias) -------------

def _props(alias: str) -> str:
    return f"{alias}.payload->'properties'"


def layer_sql(alias: str = "o") -> str:
    p = _props(alias)
    return (f"COALESCE({p}->>'layer', {p}->'dxf_attributes'->>'layer', "
            f"{alias}.payload->'evidence'->>'layer', {alias}.payload->>'layer')")


def block_name_sql(alias: str = "o") -> str:
    p = _props(alias)
    return (f"(CASE WHEN {alias}.kind = 'BlockDefinition' THEN COALESCE({p}->>'name', {p}->>'block_name', {alias}.label) "
            f"ELSE COALESCE(NULLIF({p}->>'effective_name', ''), NULLIF({p}->>'effective_block_name', ''), "
            f"NULLIF({p}->>'block_name', ''), NULLIF({p}->'dxf_attributes'->>'name', '')) END)")


def category_sql(alias: str = "o") -> str:
    p = _props(alias)
    return f"COALESCE({p}->>'drawing_category', {alias}.payload->>'drawing_category', {p}->>'category')"


def _jsonb_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# --- vocabulary helpers ------------------------------------------------------------

def kind_vocabulary() -> dict[str, str]:
    vocab = dict(KIND_ALIASES)
    try:  # parser aliases stay the source of truth for the kinds they emit
        from .parsers import ALIASES
        for kind, words in ALIASES.items():
            merged = " ".join(dict.fromkeys((vocab.get(kind, "") + " " + words).split()))
            vocab[kind] = merged
    except Exception:  # parsers pulls heavy CAD deps; the catalog must still work without them
        pass
    return vocab


def resolve_kinds(value: str | Iterable[str] | None) -> list[str]:
    """Map 'door', '문', 'Door,Window' or ['창호'] to canonical kind names."""
    if value is None:
        return []
    terms = [t.strip() for t in (value.split(",") if isinstance(value, str) else value) if t and str(t).strip()]
    vocab = kind_vocabulary()
    lower = {k.lower(): k for k in vocab}
    resolved: list[str] = []
    for term in terms:
        term = str(term)
        kind = lower.get(term.lower())
        if kind is None:
            kind = next((k for k, words in vocab.items() if term in words.split()), None)
        resolved.append(kind or term)
    return list(dict.fromkeys(resolved))


def resolve_category(value: str | None) -> list[str]:
    if not value:
        return []
    v = value.strip()
    for canonical, synonyms in CATEGORY_ALIASES.items():
        if v == canonical or v.lower() in synonyms:
            return [canonical, *synonyms]
    return [v]


# --- cursors -----------------------------------------------------------------------

def encode_cursor(value: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> Any:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid cursor") from exc


def _limit(limit: int | None) -> int:
    try:
        value = int(limit or DEFAULT_LIMIT)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    return max(1, min(value, MAX_LIMIT))


def _bbox_list(bbox: Any) -> list[float] | None:
    if isinstance(bbox, dict) and all(k in bbox for k in ("min_x", "min_y", "max_x", "max_y")):
        return [bbox["min_x"], bbox["min_y"], bbox["max_x"], bbox["max_y"]]
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return list(bbox)
    return None


def parse_bbox(value: Any) -> list[float] | None:
    if value in (None, ""):
        return None
    parts = value.split(",") if isinstance(value, str) else list(value)
    try:
        numbers = [float(p) for p in parts]
    except (TypeError, ValueError) as exc:
        raise ValueError("bbox must be min_x,min_y,max_x,max_y") from exc
    if len(numbers) != 4:
        raise ValueError("bbox must be min_x,min_y,max_x,max_y")
    x, y, X, Y = numbers
    return [min(x, X), min(y, Y), max(x, X), max(y, Y)]


def attribute_tags(value: Any) -> list[str]:
    """Normalise attribute containers ({tag: value}, [{tag: ..}], ['TAG']) to tag names."""
    tags: list[str] = []
    if isinstance(value, dict):
        tags = [str(k) for k in value]
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                tag = item.get("tag") or item.get("name") or item.get("Tag")
                if tag:
                    tags.append(str(tag))
            elif item is not None:
                tags.append(str(item))
    return tags


def _attributes(props: dict[str, Any]) -> Any:
    for key in ("attributes", "attribs", "block_attributes"):
        if props.get(key):
            return props[key]
    return {}


def _summary(row: dict[str, Any], include_properties: bool = False) -> dict[str, Any]:
    payload = row.get("payload") or {}
    props = payload.get("properties") or {}
    evidence = payload.get("evidence") or {}
    item = {
        "id": row["id"],
        "kind": row["kind"],
        "label": row["label"],
        "state": payload.get("state"),
        "project_id": row["project_id"],
        "document_id": row["document_id"],
        "document_name": row.get("document_name"),
        "revision": row["revision"],
        "storey": row.get("storey") or payload.get("storey") or "",
        "layer": row.get("layer"),
        "block_name": row.get("block_name"),
        "drawing_category": row.get("drawing_category"),
        "attributes": _attributes(props),
        "bbox": _bbox_list(payload.get("bbox")),
        "evidence": {
            "source_path": evidence.get("source_path"),
            "source_name": evidence.get("source_name"),
            "handle": evidence.get("handle"),
            "layout": evidence.get("layout") or evidence.get("page"),
            "coordinate_system": evidence.get("coordinate_system"),
            "preview_path": evidence.get("preview_path"),
            "geometry_path": evidence.get("geometry_path"),
        },
    }
    if include_properties:
        item["properties"] = props
    return item


_SELECT = f"""SELECT o.id, o.project_id, o.document_id, d.name AS document_name, o.revision, o.kind,
       o.label, o.storey, o.payload, {layer_sql()} AS layer, {block_name_sql()} AS block_name,
       {category_sql()} AS drawing_category
  FROM aec.objects o JOIN aec.documents d ON d.id = o.document_id"""


# --- 1. table of contents ----------------------------------------------------------

def element_catalog(db: Database, project_id: str | None = None) -> dict[str, Any]:
    """Counts by kind, drawing category, project, relation predicate, layer and block."""
    vocab = kind_vocabulary()
    p = {"p": project_id}
    scope = "(%(p)s::text IS NULL OR o.project_id = %(p)s)"
    with db.connect() as conn:
        kinds = conn.execute(f"""SELECT o.kind, count(*) AS count, count(DISTINCT o.document_id) AS documents,
                count(*) FILTER (WHERE o.payload->>'state' = 'AI_INFERRED') AS candidates
            FROM aec.objects o WHERE {scope} GROUP BY o.kind ORDER BY count(*) DESC, o.kind""", p).fetchall()
        categories = conn.execute(f"""SELECT {category_sql()} AS category, o.kind, count(*) AS count,
                count(DISTINCT o.document_id) AS documents
            FROM aec.objects o WHERE {scope} AND {category_sql()} IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1, 2""", p).fetchall()
        projects = conn.execute(f"""SELECT o.project_id, count(DISTINCT o.document_id) AS documents, count(*) AS objects
            FROM aec.objects o WHERE {scope} GROUP BY o.project_id ORDER BY o.project_id""", p).fetchall()
        predicates = conn.execute("""SELECT predicate, count(*) AS count FROM aec.relations
            WHERE (%(p)s::text IS NULL OR project_id = %(p)s) GROUP BY predicate ORDER BY count(*) DESC""", p).fetchall()
        layer_kinds = conn.execute(f"""SELECT {layer_sql()} AS layer, o.kind, count(*) AS count
            FROM aec.objects o WHERE {scope} AND {layer_sql()} IS NOT NULL GROUP BY 1, 2""", p).fetchall()
        blocks = conn.execute(f"""SELECT count(*) FILTER (WHERE o.kind = 'BlockDefinition') AS definitions,
                count(DISTINCT {block_name_sql()}) FILTER (WHERE o.kind = 'BlockDefinition') AS definition_names,
                count(*) FILTER (WHERE o.kind <> 'BlockDefinition' AND {block_name_sql()} IS NOT NULL) AS instances,
                count(DISTINCT {block_name_sql()}) FILTER (WHERE o.kind <> 'BlockDefinition') AS instance_names
            FROM aec.objects o WHERE {scope}""", p).fetchone()
        storeys = conn.execute(f"""SELECT o.storey, count(*) AS count FROM aec.objects o
            WHERE {scope} AND o.storey <> '' GROUP BY 1 ORDER BY 1""", p).fetchall()

    by_layer: dict[str, dict[str, int]] = {}
    for row in layer_kinds:
        by_layer.setdefault(row["layer"], {})[row["kind"]] = row["count"]
    layers = sorted(({"layer": name, "count": sum(k.values()), "kinds": k} for name, k in by_layer.items()),
                    key=lambda e: (-e["count"], e["layer"]))[:200]
    category_map: dict[str, dict[str, Any]] = {}
    for row in categories:
        entry = category_map.setdefault(row["category"], {"category": row["category"], "count": 0, "kinds": {}, "documents": 0})
        entry["count"] += row["count"]
        entry["kinds"][row["kind"]] = row["count"]
        entry["documents"] = max(entry["documents"], row["documents"])
    return {
        "project_id": project_id,
        "totals": {"objects": sum(r["count"] for r in kinds), "documents": sum(r["documents"] for r in projects),
                   "projects": len(projects)},
        "kinds": [{"kind": r["kind"], "count": r["count"], "documents": r["documents"],
                   "ai_inferred_candidates": r["candidates"], "aliases_ko": vocab.get(r["kind"], "")} for r in kinds],
        "drawing_categories": sorted(category_map.values(), key=lambda e: -e["count"]),
        "projects": [dict(r) for r in projects],
        "relation_predicates": [dict(r) for r in predicates],
        "layers": layers,
        "blocks": dict(blocks or {}),
        "storeys": [dict(r) for r in storeys],
        "vocabulary": {"kinds": vocab, "drawing_categories": {k: list(v) for k, v in CATEGORY_ALIASES.items()}},
        "next_steps": {
            "find_elements": "filter by kind (Korean alias ok), drawing_category, layer, block_name, text, bbox",
            "block_catalog": "block library with instance counts, attribute tags and instance classification",
            "drawing_index": "sheets with title-block fields, category and per-sheet element counts",
            "element_context": "relations of one element (contains/instanceOf/hasTitleBlock/hasSection ...)",
        },
    }


# --- 2. filtered element list ------------------------------------------------------

def find_elements(db: Database, kind: str | Iterable[str] | None = None, project_id: str | None = None,
                  document_id: str | None = None, drawing_category: str | None = None, layer: str | None = None,
                  block_name: str | None = None, text: str | None = None, bbox: Any = None,
                  limit: int | None = DEFAULT_LIMIT, cursor: str | None = None, state: str | None = None,
                  include_properties: bool = False, storey: str | None = None) -> dict[str, Any]:
    limit = _limit(limit)
    storey = normalize_storey(storey)
    after = decode_cursor(cursor)
    kinds = resolve_kinds(kind)
    categories = resolve_category(drawing_category)
    box = parse_bbox(bbox)
    where, params = [], {}
    if project_id:
        where.append("o.project_id = %(project)s"); params["project"] = project_id
    if document_id:
        where.append("o.document_id = %(document)s"); params["document"] = document_id
    if kinds:
        where.append("o.kind = ANY(%(kinds)s)"); params["kinds"] = kinds
    if state:
        where.append("o.payload->>'state' = %(state)s"); params["state"] = state
    if storey:
        where.append("upper(o.storey) = upper(%(storey)s)"); params["storey"] = storey
    if layer:
        if any(c in layer for c in "*%?"):
            where.append(f"{layer_sql()} ILIKE %(layer)s")
            params["layer"] = layer.replace("*", "%").replace("?", "_")
        else:
            where.append(f"upper({layer_sql()}) = upper(%(layer)s)"); params["layer"] = layer
    if block_name:
        pattern = block_name.replace("*", "%").replace("?", "_")
        where.append(f"""({block_name_sql()} ILIKE %(block)s
            OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(o.payload->'properties'->'effective_names') = 'array'
                       THEN o.payload->'properties'->'effective_names' ELSE '[]'::jsonb END) en WHERE en ILIKE %(block)s))""")
        params["block"] = pattern
    if text:
        where.append("""(o.search_text ILIKE %(text)s OR o.label ILIKE %(text)s
            OR (o.payload->'properties'->'attributes')::text ILIKE %(text)s)""")
        params["text"] = f"%{text}%"
    if box:
        where.append("o.bounds && ST_MakeEnvelope(%(x)s, %(y)s, %(X)s, %(Y)s, 0)")
        params.update(x=box[0], y=box[1], X=box[2], Y=box[3])
    if categories:
        params["cats"] = categories
        where.append(f"""({category_sql()} = ANY(%(cats)s)
            OR EXISTS (SELECT 1 FROM aec.relations r JOIN aec.objects v ON v.id = r.subject
                       WHERE r.object = o.id AND r.predicate = 'contains' AND r.project_id = o.project_id
                         AND {category_sql('v')} = ANY(%(cats)s))
            OR EXISTS (SELECT 1 FROM aec.objects v WHERE v.document_id = o.document_id AND v.kind = 'View'
                         AND v.payload->'evidence'->>'layout' = o.payload->'evidence'->>'layout'
                         AND {category_sql('v')} = ANY(%(cats)s))
            OR EXISTS (SELECT 1 FROM aec.objects v WHERE v.document_id = o.document_id AND v.kind = 'Document'
                         AND {category_sql('v')} = ANY(%(cats)s)))""")
    if after is not None:
        where.append("o.id > %(after)s"); params["after"] = str(after)
    params["lim"] = limit + 1
    sql_text = _SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY o.id LIMIT %(lim)s"
    with db.connect() as conn:
        rows = conn.execute(sql_text, params).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "filters": {"kind": kinds or None, "project_id": project_id, "document_id": document_id,
                    "drawing_category": categories[:1] or None, "layer": layer, "block_name": block_name,
                    "text": text, "bbox": box, "state": state, "storey": storey},
        "count": len(rows),
        "items": [_summary(r, include_properties) for r in rows],
        "next_cursor": encode_cursor(rows[-1]["id"]) if more and rows else None,
    }


# --- 3. block library --------------------------------------------------------------

def block_catalog(db: Database, project_id: str | None = None, name_like: str | None = None,
                  limit: int | None = 100, cursor: str | None = None) -> dict[str, Any]:
    """Block definitions aggregated by name across documents, joined to their INSERT instances."""
    limit = _limit(limit)
    after = decode_cursor(cursor)
    params: dict[str, Any] = {"p": project_id, "like": (name_like or "%").replace("*", "%").replace("?", "_"),
                              "after": after, "lim": limit + 1}
    if name_like and not any(c in name_like for c in "*%?"):
        params["like"] = f"%{name_like}%"
    bn = block_name_sql()
    with db.connect() as conn:
        names = conn.execute(f"""SELECT {bn} AS name FROM aec.objects o
            WHERE (%(p)s::text IS NULL OR o.project_id = %(p)s) AND {bn} IS NOT NULL AND {bn} ILIKE %(like)s
              AND (%(after)s::text IS NULL OR {bn} > %(after)s)
            GROUP BY 1 ORDER BY 1 LIMIT %(lim)s""", params).fetchall()
        more = len(names) > limit
        selected = [r["name"] for r in names[:limit]]
        if not selected:
            return {"count": 0, "items": [], "next_cursor": None}
        q = {"p": project_id, "names": selected}
        defs = conn.execute(f"""SELECT {bn} AS name, o.id, o.document_id, d.name AS document_name,
                o.payload->'properties' AS props
            FROM aec.objects o JOIN aec.documents d ON d.id = o.document_id
            WHERE o.kind = 'BlockDefinition' AND (%(p)s::text IS NULL OR o.project_id = %(p)s)
              AND {bn} = ANY(%(names)s)""", q).fetchall()
        inst = conn.execute(f"""SELECT {bn} AS name, o.kind, count(*) AS count,
                array_agg(DISTINCT o.document_id) AS documents, array_agg(DISTINCT {layer_sql()}) AS layers
            FROM aec.objects o
            WHERE o.kind <> 'BlockDefinition' AND (%(p)s::text IS NULL OR o.project_id = %(p)s) AND {bn} = ANY(%(names)s)
            GROUP BY 1, 2""", q).fetchall()
        inst_tags = conn.execute(f"""SELECT DISTINCT {bn} AS name, t.tag
            FROM aec.objects o
            CROSS JOIN LATERAL jsonb_object_keys(CASE WHEN jsonb_typeof(o.payload->'properties'->'attributes') = 'object'
                 THEN o.payload->'properties'->'attributes' ELSE '{{}}'::jsonb END) AS t(tag)
            WHERE o.kind <> 'BlockDefinition' AND (%(p)s::text IS NULL OR o.project_id = %(p)s) AND {bn} = ANY(%(names)s)""",
            q).fetchall()
        rel_counts = conn.execute(f"""SELECT {bn} AS name, count(r.id) AS count
            FROM aec.objects o JOIN aec.relations r ON r.object = o.id AND r.predicate = 'instanceOf' AND r.project_id = o.project_id
            WHERE o.kind = 'BlockDefinition' AND (%(p)s::text IS NULL OR o.project_id = %(p)s) AND {bn} = ANY(%(names)s)
            GROUP BY 1""", q).fetchall()

    items: dict[str, dict[str, Any]] = {name: {
        "name": name, "definitions": [], "documents": set(), "attribute_tags": set(), "effective_names": set(),
        "layers": set(), "is_xref": False, "is_anonymous": name.startswith("*"), "declared_insert_count": 0,
        "instance_count": 0, "instance_kinds": {}, "instanceOf_relations": 0} for name in selected}
    for row in defs:
        item = items[row["name"]]
        props = row["props"] or {}
        item["definitions"].append({"id": row["id"], "document_id": row["document_id"], "document_name": row["document_name"]})
        item["documents"].add(row["document_id"])
        for key in ("attribute_defs", "attdefs", "attribute_definitions", "attributes"):
            item["attribute_tags"].update(attribute_tags(props.get(key)))
        en = props.get("effective_names") or props.get("effective_name") or []
        item["effective_names"].update([en] if isinstance(en, str) else [str(e) for e in en])
        lay = props.get("layers") or []
        item["layers"].update([lay] if isinstance(lay, str) else [str(x) for x in lay])
        item["is_xref"] = item["is_xref"] or bool(props.get("is_xref"))
        item["is_anonymous"] = item["is_anonymous"] or bool(props.get("is_anonymous"))
        try:
            item["declared_insert_count"] += int(props.get("insert_count") or 0)
        except (TypeError, ValueError):
            pass
    for row in inst:
        item = items[row["name"]]
        item["instance_count"] += row["count"]
        item["instance_kinds"][row["kind"]] = item["instance_kinds"].get(row["kind"], 0) + row["count"]
        item["documents"].update(d for d in row["documents"] if d)
        item["layers"].update(x for x in row["layers"] if x)
    for row in inst_tags:
        items[row["name"]]["attribute_tags"].add(row["tag"])
    for row in rel_counts:
        items[row["name"]]["instanceOf_relations"] = row["count"]
    out = []
    for name in selected:
        item = items[name]
        kinds = item["instance_kinds"]
        item["classified_as"] = max(kinds, key=kinds.get) if kinds else None
        for key in ("documents", "attribute_tags", "effective_names", "layers"):
            item[key] = sorted(item[key])
        out.append(item)
    return {"count": len(out), "items": out, "next_cursor": encode_cursor(selected[-1]) if more else None}


# --- 4. sheet index ----------------------------------------------------------------

_TB_FIELDS = {
    "drawing_number": ("drawingNumber", "drawing_number", "drawing_no", "dwg_no", "도면번호", "DWG_NO", "DWGNO"),
    "drawing_title": ("drawingTitle", "drawing_title", "title", "도면명", "TITLE"),
    "scale": ("scale", "축척", "SCALE"),
    "project_name": ("projectName", "project_name", "공사명", "PROJECT"),
    "date": ("date", "일자", "DATE"),
    "revision": ("revision_mark", "rev", "REV"),
}


def title_block_fields(props: dict[str, Any]) -> dict[str, Any]:
    attrs = _attributes(props)
    attrs = attrs if isinstance(attrs, dict) else {}
    fields = {}
    for field_name, keys in _TB_FIELDS.items():
        fields[field_name] = next((props.get(k) or attrs.get(k) for k in keys if props.get(k) or attrs.get(k)), None)
    return fields


def drawing_index(db: Database, project_id: str | None = None, category: str | None = None,
                  limit: int | None = 100, cursor: str | None = None) -> dict[str, Any]:
    limit = _limit(limit)
    after = decode_cursor(cursor)
    cats = resolve_category(category)
    params: dict[str, Any] = {"p": project_id, "after": after, "lim": limit + 1, "cats": cats or None}
    with db.connect() as conn:
        docs = conn.execute(f"""SELECT d.id, d.project_id, d.name, d.revision, d.source_key, d.updated_at
            FROM aec.documents d
            WHERE (%(p)s::text IS NULL OR d.project_id = %(p)s) AND (%(after)s::text IS NULL OR d.id > %(after)s)
              AND (%(cats)s::text[] IS NULL OR EXISTS (SELECT 1 FROM aec.objects o WHERE o.document_id = d.id
                   AND {category_sql()} = ANY(%(cats)s)))
            ORDER BY d.id LIMIT %(lim)s""", params).fetchall()
        more = len(docs) > limit
        docs = docs[:limit]
        ids = [d["id"] for d in docs]
        if not ids:
            return {"count": 0, "items": [], "next_cursor": None}
        sheets = conn.execute(f"""SELECT o.id, o.document_id, o.kind, o.label, o.payload, {category_sql()} AS category
            FROM aec.objects o WHERE o.document_id = ANY(%(ids)s) AND o.kind IN ('View', 'Page', 'TitleBlock', 'Document')
            ORDER BY o.document_id, o.id""", {"ids": ids}).fetchall()
        counts = conn.execute("""SELECT o.document_id, COALESCE(o.payload->'evidence'->>'layout', o.payload->'evidence'->>'page', '') AS layout,
                o.kind, count(*) AS count
            FROM aec.objects o WHERE o.document_id = ANY(%(ids)s) AND o.kind NOT IN ('Document')
            GROUP BY 1, 2, 3""", {"ids": ids}).fetchall()
        tb_links = conn.execute("""SELECT r.subject, r.object FROM aec.relations r
            WHERE r.document_id = ANY(%(ids)s) AND r.predicate = 'hasTitleBlock'""", {"ids": ids}).fetchall()

    linked = {r["object"]: r["subject"] for r in tb_links}
    per_doc: dict[str, dict[str, Any]] = {d["id"]: {
        "document_id": d["id"], "project_id": d["project_id"], "name": d["name"], "revision": d["revision"],
        "source_key": d["source_key"], "updated_at": d["updated_at"].isoformat() if d["updated_at"] else None,
        "drawing_categories": set(), "sheets": {}, "title_blocks": [], "element_counts": {}} for d in docs}
    for row in counts:
        doc = per_doc[row["document_id"]]
        doc["element_counts"][row["kind"]] = doc["element_counts"].get(row["kind"], 0) + row["count"]
        sheet = doc["sheets"].setdefault(str(row["layout"]), {"layout": str(row["layout"]), "element_counts": {}})
        sheet["element_counts"][row["kind"]] = row["count"]
    view_ids: dict[str, str] = {}
    for row in sheets:
        doc = per_doc[row["document_id"]]
        payload = row["payload"] or {}
        evidence = payload.get("evidence") or {}
        layout = str(evidence.get("layout") or evidence.get("page") or "")
        if row["category"]:
            doc["drawing_categories"].add(row["category"])
        if row["kind"] in ("View", "Page"):
            sheet = doc["sheets"].setdefault(layout, {"layout": layout, "element_counts": {}})
            sheet.update({"view_id": row["id"], "view_label": row["label"], "drawing_category": row["category"],
                          "preview_path": evidence.get("preview_path")})
            view_ids[row["id"]] = layout
        elif row["kind"] == "TitleBlock":
            fields = title_block_fields(payload.get("properties") or {})
            tb = {"id": row["id"], "layout": layout, **fields, "handle": evidence.get("handle")}
            doc["title_blocks"].append(tb)
    for doc in per_doc.values():
        for tb in doc["title_blocks"]:
            layout = view_ids.get(linked.get(tb["id"], ""), tb["layout"])
            sheet = doc["sheets"].setdefault(layout, {"layout": layout, "element_counts": {}})
            sheet.setdefault("title_block", {k: tb[k] for k in ("id", "drawing_number", "drawing_title", "scale")})
        sheets_list = [s for s in doc["sheets"].values() if s.get("view_id") or s.get("title_block") or s["layout"]]
        if cats:
            # keep the document but flag matching sheets so agents can jump straight to them
            for s in sheets_list:
                s["matches_category"] = s.get("drawing_category") in cats
        doc["sheets"] = sorted(sheets_list, key=lambda s: s["layout"])
        doc["drawing_categories"] = sorted(doc["drawing_categories"])
    return {"count": len(per_doc), "items": list(per_doc.values()),
            "next_cursor": encode_cursor(ids[-1]) if more else None}


# --- 5. neighbourhood --------------------------------------------------------------

def element_context(db: Database, object_id: str, hops: int = 1, limit: int = 200) -> dict[str, Any] | None:
    """Relations around one element in both directions (1-2 hops), plus AGE graph confirmation."""
    hops = 2 if int(hops or 1) >= 2 else 1
    limit = max(1, min(int(limit or 200), 1000))
    with db.connect() as conn:
        root = conn.execute(_SELECT + " WHERE o.id = %s", (object_id,)).fetchone()
        if not root:
            return None
        project = root["project_id"]
        edges: dict[str, dict[str, Any]] = {}
        frontier, seen = {object_id}, {object_id}
        for hop in range(1, hops + 1):
            if not frontier or len(edges) >= limit:
                break
            # Second hop never fans out from a container to all of its children (sheet siblings).
            rows = conn.execute("""SELECT id, subject, predicate, object, state, evidence FROM aec.relations
                WHERE project_id = %(p)s AND (subject = ANY(%(f)s) OR object = ANY(%(f)s))
                  AND NOT (%(hop)s > 1 AND predicate = 'contains' AND subject = ANY(%(f)s) AND NOT subject = %(root)s)
                ORDER BY (predicate = 'contains'), predicate, id LIMIT %(lim)s""",
                {"p": project, "f": list(frontier), "hop": hop, "root": object_id, "lim": limit - len(edges)}).fetchall()
            nxt = set()
            for r in rows:
                if r["id"] in edges:
                    continue
                direction = "out" if r["subject"] == object_id else "in" if r["object"] == object_id else "indirect"
                edges[r["id"]] = {"id": r["id"], "subject": r["subject"], "predicate": r["predicate"], "object": r["object"],
                                  "state": r["state"], "hop": hop, "direction": direction, "source": "sql"}
                for node in (r["subject"], r["object"]):
                    if node not in seen:
                        seen.add(node); nxt.add(node)
            frontier = nxt
        node_rows = conn.execute(_SELECT + " WHERE o.id = ANY(%s)", (list(seen - {object_id}),)).fetchall() if len(seen) > 1 else []
        graph = _age_neighbours(db, conn, project, object_id)

    for g in graph.get("edges", []):
        key = (g["subject"], g["predicate"], g["object"])
        if not any((e["subject"], e["predicate"], e["object"]) == key for e in edges.values()):
            edges["age:" + "|".join(key)] = {**g, "hop": 1, "source": "age"}
        else:
            for e in edges.values():
                if (e["subject"], e["predicate"], e["object"]) == key:
                    e["in_graph"] = True
    nodes = {r["id"]: _summary(r) for r in node_rows}
    summary_by_predicate: dict[str, int] = {}
    for e in edges.values():
        if e["hop"] == 1:
            label = f'{e["predicate"]}:{e["direction"]}'
            summary_by_predicate[label] = summary_by_predicate.get(label, 0) + 1
    return {
        "element": _summary(root, include_properties=True),
        "hops": hops,
        "edges": list(edges.values()),
        "nodes": list(nodes.values()),
        "neighbour_summary": summary_by_predicate,
        "graph": {"available": graph["available"], "edges": len(graph.get("edges", [])), "error": graph.get("error")},
        "truncated": len(edges) >= limit,
    }


def _agtype(value: Any) -> Any:
    text = str(value)
    for suffix in ("::vertex", "::edge", "::path"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    try:
        return json.loads(text)
    except ValueError:
        return text


def _age_neighbours(db: Database, conn, project: str, object_id: str) -> dict[str, Any]:
    oid = json.dumps(object_id)
    try:
        with conn.transaction():  # savepoint: a missing graph must not abort the read transaction
            out = db.cypher(conn, graph_name(project),
                f"MATCH (a:Entity)-[r:Rel]->(b:Entity) WHERE a.id = {oid} RETURN {{s: a.id, p: r.kind, o: b.id, state: b.state}} LIMIT 200")
            inc = db.cypher(conn, graph_name(project),
                f"MATCH (b:Entity)-[r:Rel]->(a:Entity) WHERE a.id = {oid} RETURN {{s: b.id, p: r.kind, o: a.id, state: b.state}} LIMIT 200")
    except Exception as exc:
        return {"available": False, "edges": [], "error": f"{type(exc).__name__}: {exc}".splitlines()[0]}
    edges = []
    for direction, rows in (("out", out), ("in", inc)):
        for row in rows:
            data = _agtype(row["value"] if isinstance(row, dict) else row[0])
            if isinstance(data, dict):
                edges.append({"subject": data.get("s"), "predicate": data.get("p"), "object": data.get("o"),
                              "direction": direction, "state": None})
    return {"available": True, "edges": edges}
