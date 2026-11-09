"""Fill ``aec.objects.storey`` for already-loaded objects from data already in the DB (no re-parse).

The parser (see ``parsers.py`` end_layout / _split_modelspace_sheet_regions) fills storey for new
ingests. Objects loaded before that have ``storey = ''``. Everything the rules need is still in each
object's ``payload`` JSON, so this command applies the same rules, in the same priority order:

1. ``title_block_bbox_region`` - the object lies in a floor-plan frame (``properties.sheet_region``)
   whose region View's drawingTitle names one storey.
2. ``layer`` - the object's own layer names one storey ('A-2F-WALL').
3. the storey of its layout View (``evidence.layout``): title-block drawingTitle, layout name, then
   the document file name (source ``title_block`` / ``layout_name`` / ``file_name``).
4. ``file_name`` - objects outside any layout (document root, PDF pages) take the file name's storey.

Level marker texts ('1FL') are not stored per layout, so that parser fallback is not replayed.

Only rows with ``storey = ''`` are touched, and each update re-checks that, so re-running is
idempotent and an interrupted run resumes where it stopped (every batch commits). Memory is bounded
by ``batch_size`` rows plus one document's View map.

Projections: AGE nodes do not carry storey; text vectors embed ``search_text``, which is unchanged.
The canonical knowledge graph (``kg-build``) reads ``aec.objects.storey``; run ``kg-build --force``
after a backfill because its fingerprint does not see storey changes.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Iterable

from ..classifier import storey_from, storey_from_layer
from .catalog import layer_sql

FLOOR_PLAN = "평면도"


def view_storey(view: dict[str, Any], document_name: str) -> dict[str, str]:
    """Storey of a View row ({id, storey, props, layout}) as the parser would have set it."""
    props = view.get("props") or {}
    if view.get("storey"):
        return {"storey": view["storey"], "storey_source": props.get("storey_source") or "existing"}
    title = str(props.get("drawingTitle") or "")
    if props.get("view_kind") == "sheet_region":
        if props.get("drawing_category") != FLOOR_PLAN:
            return {}
        found = storey_from(("title_block", title))
        return {**found, "storey_source": "title_block_bbox_region"} if found else {}
    return storey_from(("title_block", title), ("layout_name", view.get("layout") or ""),
                       ("file_name", document_name))


def infer_storey(obj: dict[str, Any], regions: dict[str, dict], layouts: dict[str, dict],
                 document: dict[str, str]) -> dict[str, str]:
    """Storey for one object row ({id, kind, sheet_region, layer, layout}); {} when none is known."""
    region_id = obj.get("sheet_region") or (obj["id"] if obj["id"] in regions else None)
    if region_id and regions.get(region_id):
        return regions[region_id]
    if obj.get("kind") != "View":
        own = storey_from_layer(obj.get("layer"))
        if own:
            return own
    layout = obj.get("layout")
    if layout is not None and layout in layouts:
        return layouts[layout]
    return document


def document_context(conn, document_id: str, document_name: str):
    """(regions, layouts, document) storey maps of one document from its View rows."""
    regions: dict[str, dict] = {}
    layouts: dict[str, dict] = {}
    rows = conn.execute(
        """SELECT id, storey, payload->'properties' AS props, payload->'evidence'->>'layout' AS layout
           FROM aec.objects WHERE document_id=%s AND kind='View'""", (document_id,)).fetchall()
    for row in rows:
        found = view_storey(row, document_name)
        props = row["props"] or {}
        if props.get("view_kind") == "sheet_region":
            regions[row["id"]] = found
        elif row["layout"] is not None:
            layouts.setdefault(row["layout"], found)
    document = storey_from(("file_name", document_name))
    return regions, layouts, document


_SELECT = f"""SELECT o.id, o.kind, o.payload->'properties'->>'sheet_region' AS sheet_region,
       {layer_sql('o')} AS layer, o.payload->'evidence'->>'layout' AS layout
  FROM aec.objects o WHERE o.document_id=%s AND o.storey='' AND o.id > %s ORDER BY o.id LIMIT %s"""

_UPDATE = """UPDATE aec.objects o SET storey = v.storey,
       payload = jsonb_set(o.payload, '{properties}', COALESCE(o.payload->'properties', '{}'::jsonb)
                 || jsonb_build_object('storey_source', v.source, 'storey_evidence', v.evidence,
                                       'storey_backfill', true))
  FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[]) AS v(id, storey, source, evidence)
 WHERE o.id = v.id AND o.storey = ''"""


def _documents(conn, project: str | None, document: str | None) -> Iterable[dict]:
    where, params = ["true"], []
    if project:
        where.append("project_id=%s"); params.append(project)
    if document:
        where.append("id=%s"); params.append(document)
    return conn.execute(f"SELECT id, name FROM aec.documents WHERE {' AND '.join(where)} ORDER BY id",
                        params).fetchall()


def backfill_storey(db, *, batch_size: int = 5000, dry_run: bool = False, project: str | None = None,
                    document: str | None = None,
                    progress: Callable[[int, int, dict], None] | None = None) -> dict[str, Any]:
    """Fill empty storeys document by document; returns counts per source (and what stayed empty)."""
    batch_size = max(1, int(batch_size))
    by_source: Counter = Counter()
    scanned = updated = 0
    with db.connect(statement_timeout_seconds=600) as conn:
        documents = [dict(d) for d in _documents(conn, project, document)]
    for index, doc in enumerate(documents, 1):
        with db.connect(statement_timeout_seconds=600) as conn:
            regions, layouts, document_storey = document_context(conn, doc["id"], doc["name"] or "")
            last = ""
            while True:
                rows = conn.execute(_SELECT, (doc["id"], last, batch_size)).fetchall()
                if not rows:
                    break
                last = rows[-1]["id"]
                scanned += len(rows)
                ids, storeys, sources, evidence = [], [], [], []
                for row in rows:
                    found = infer_storey(row, regions, layouts, document_storey)
                    if not found.get("storey"):
                        by_source["(none)"] += 1
                        continue
                    by_source[found["storey_source"]] += 1
                    ids.append(row["id"]); storeys.append(found["storey"])
                    sources.append(found["storey_source"]); evidence.append(found.get("storey_evidence", ""))
                if ids and not dry_run:
                    cur = conn.execute(_UPDATE, (ids, storeys, sources, evidence))
                    updated += cur.rowcount or 0
                    conn.commit()
        if progress:
            progress(index, len(documents), doc)
    return {"dry_run": dry_run, "documents": len(documents), "scanned_empty": scanned,
            "would_fill" if dry_run else "updated": sum(v for k, v in by_source.items() if k != "(none)")
            if dry_run else updated,
            "by_source": dict(sorted(by_source.items())),
            "note": "Run 'kg-build --force' afterwards so the knowledge graph picks up the new storeys."}
