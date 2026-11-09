"""backfill-storey: fill empty storey of loaded objects from their stored payload, no re-parse."""

from __future__ import annotations

import copy
import os
from contextlib import contextmanager

import pytest

from aec_intelligence.operational.storey_backfill import backfill_storey, infer_storey, view_storey


def test_view_storey_rules():
    region = {"id": "r1", "storey": "", "layout": "Model",
              "props": {"view_kind": "sheet_region", "drawing_category": "평면도", "drawingTitle": "2층 평면도"}}
    assert view_storey(region, "x.dwg") == {"storey": "2F", "storey_source": "title_block_bbox_region",
                                            "storey_evidence": "2층 평면도"}
    section = {**region, "props": {**region["props"], "drawing_category": "단면도"}}
    assert view_storey(section, "x.dwg") == {}
    layout = {"id": "v", "storey": "", "layout": "B1F", "props": {}}
    assert view_storey(layout, "plan.dwg")["storey_source"] == "layout_name"
    assert view_storey({"id": "v", "storey": "", "layout": "Model", "props": {}}, "3층.dwg")["storey"] == "3F"
    assert view_storey({"id": "v", "storey": "RF", "layout": "M", "props": {}}, "1층.dwg")["storey"] == "RF"


def test_infer_storey_priority():
    regions = {"r1": {"storey": "2F", "storey_source": "title_block_bbox_region"}}
    layouts = {"Model": {"storey": "1F", "storey_source": "file_name"}}
    doc = {"storey": "1F", "storey_source": "file_name"}
    assert infer_storey({"id": "a", "kind": "Line", "sheet_region": "r1", "layer": "A-3F-W", "layout": "Model"},
                        regions, layouts, doc)["storey"] == "2F"
    assert infer_storey({"id": "a", "kind": "Line", "layer": "A-3F-W", "layout": "Model"},
                        regions, layouts, doc)["storey_source"] == "layer"
    assert infer_storey({"id": "a", "kind": "Line", "layer": "A-WALL", "layout": "Model"},
                        regions, layouts, doc)["storey"] == "1F"
    assert infer_storey({"id": "root", "kind": "Document", "layer": None, "layout": None},
                        regions, layouts, doc) == doc
    assert infer_storey({"id": "a", "kind": "Line", "layer": "A-WALL", "layout": "Model"}, {}, {}, {}) == {}


class _Result:
    def __init__(self, rows=(), rowcount=0):
        self._rows, self.rowcount = list(rows), rowcount

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Answers the four statements backfill_storey issues from an in-memory objects table."""

    def __init__(self, store):
        self.store = store
        self.commits = 0

    def commit(self):
        self.commits += 1

    def execute(self, sql, params=()):
        objs = self.store["objects"]
        if "FROM aec.documents" in sql:
            return _Result(self.store["documents"])
        if "kind='View'" in sql:
            return _Result([{"id": o["id"], "storey": o["storey"], "props": o["payload"].get("properties"),
                             "layout": o["payload"].get("evidence", {}).get("layout")}
                            for o in objs if o["document_id"] == params[0] and o["kind"] == "View"])
        if sql.lstrip().startswith("SELECT o.id"):
            doc, last, limit = params
            rows = sorted((o for o in objs if o["document_id"] == doc and o["storey"] == "" and o["id"] > last),
                          key=lambda o: o["id"])[:limit]
            return _Result([{"id": o["id"], "kind": o["kind"],
                             "sheet_region": o["payload"].get("properties", {}).get("sheet_region"),
                             "layer": o["payload"].get("properties", {}).get("layer"),
                             "layout": o["payload"].get("evidence", {}).get("layout")} for o in rows])
        if sql.lstrip().startswith("UPDATE"):
            n = 0
            for oid, storey, source, evidence in zip(*params):
                for o in objs:
                    if o["id"] == oid and o["storey"] == "":
                        o["storey"] = storey
                        o["payload"].setdefault("properties", {}).update(
                            storey_source=source, storey_evidence=evidence, storey_backfill=True)
                        n += 1
            return _Result(rowcount=n)
        raise AssertionError(sql)


class _FakeDb:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def connect(self, statement_timeout_seconds=None):
        yield _FakeConn(self.store)


def _obj(oid, kind, layer=None, layout="Model", region=None, storey="", props=None):
    p = dict(props or {})
    if layer:
        p["layer"] = layer
    if region:
        p["sheet_region"] = region
    return {"id": oid, "document_id": "d1", "kind": kind, "storey": storey,
            "payload": {"properties": p, "evidence": {"layout": layout} if layout else {}}}


def _store():
    return {"documents": [{"id": "d1", "name": "A동 도면.dwg"}], "objects": [
        _obj("o00", "Document", layout=None),
        _obj("o01", "View", layout="1층 평면"),
        _obj("o02", "View", props={"view_kind": "sheet_region", "drawing_category": "평면도",
                                   "drawingTitle": "지하1층 평면도"}),
        _obj("o03", "Line", layer="A-WALL", region="o02"),
        _obj("o04", "Line", layer="A-2F-WALL"),
        _obj("o05", "Text", layer="0", layout="1층 평면"),
        _obj("o06", "Line", layer="A-WALL"),
        _obj("o07", "Line", layer="A-3F-WALL", storey="RF"),
    ]}


def test_backfill_dry_run_counts_and_changes_nothing():
    store = _store()
    before = copy.deepcopy(store)
    result = backfill_storey(_FakeDb(store), batch_size=2, dry_run=True)
    assert store == before
    assert result["would_fill"] == 5
    assert result["by_source"] == {"(none)": 2, "layer": 1, "layout_name": 2, "title_block_bbox_region": 2}
    assert result["scanned_empty"] == 7


def test_backfill_fills_only_empty_and_is_idempotent():
    store = _store()
    db = _FakeDb(store)
    first = backfill_storey(db, batch_size=3)
    by_id = {o["id"]: o for o in store["objects"]}
    assert first["updated"] == 5
    assert by_id["o02"]["storey"] == "B1" and by_id["o03"]["storey"] == "B1"
    assert by_id["o03"]["payload"]["properties"]["storey_source"] == "title_block_bbox_region"
    assert by_id["o04"]["storey"] == "2F"
    assert by_id["o01"]["storey"] == "1F" and by_id["o05"]["storey"] == "1F"
    assert by_id["o06"]["storey"] == "" and by_id["o00"]["storey"] == ""
    assert by_id["o07"]["storey"] == "RF" and "storey_source" not in by_id["o07"]["payload"]["properties"]
    second = backfill_storey(db, batch_size=3)
    assert second["updated"] == 0 and second["scanned_empty"] == 2


@pytest.mark.skipif(not os.getenv("AEC_TEST_DATABASE_URL"), reason="AEC_TEST_DATABASE_URL not set")
def test_backfill_sql_against_postgres():
    pytest.importorskip("psycopg")
    from psycopg.types.json import Jsonb

    from aec_intelligence.operational.db import Database

    db = Database(os.environ["AEC_TEST_DATABASE_URL"])
    db.initialize()
    with db.connect() as conn:
        conn.execute("DELETE FROM aec.objects WHERE document_id='bf_doc'")
        conn.execute("""INSERT INTO aec.documents(id,project_id,source_key,name,revision,source_hash,snapshot_path)
                        VALUES('bf_doc','bf_proj','bf','2층 평면도.dwg',1,'h','p') ON CONFLICT(id) DO NOTHING""")
        for oid, layer in (("bf_1", "A-B1-COL"), ("bf_2", "A-WALL")):
            conn.execute("""INSERT INTO aec.objects(id,project_id,document_id,revision,kind,label,search_text,payload)
                            VALUES(%s,'bf_proj','bf_doc',1,'Line',%s,%s,%s)""",
                         (oid, oid, oid, Jsonb({"properties": {"layer": layer}, "evidence": {}})))
        conn.commit()
    dry = backfill_storey(db, document="bf_doc", dry_run=True)
    assert dry["by_source"] == {"file_name": 1, "layer": 1}
    assert backfill_storey(db, document="bf_doc")["updated"] == 2
    assert backfill_storey(db, document="bf_doc")["updated"] == 0
    with db.connect() as conn:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, storey, payload->'properties'->>'storey_source' AS src FROM aec.objects "
            "WHERE document_id='bf_doc'")}
        conn.execute("DELETE FROM aec.objects WHERE document_id='bf_doc'")
        conn.execute("DELETE FROM aec.documents WHERE id='bf_doc'")
        conn.commit()
    assert rows["bf_1"]["storey"] == "B1" and rows["bf_2"]["src"] == "file_name"
