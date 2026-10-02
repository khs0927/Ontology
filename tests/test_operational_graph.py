"""Operational DXF path: spatial relations, Storey/Sheet objects, AGE projection and search ranking."""

import os
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from aec_intelligence.operational.config import Settings  # noqa: E402
from aec_intelligence.operational.db import edge_label, graph_edges  # noqa: E402
from aec_intelligence.operational.parsers import parse_source  # noqa: E402
from aec_intelligence.operational.search import kind_intents, ranking_boost, storey_intent  # noqa: E402


def build_plan(path: Path) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    for name in ("A-WALL", "A-DOOR", "A-ANNO", "가구"):
        doc.layers.add(name)
    door = doc.blocks.new("SD1")
    door.add_line((0, 0), (900, 0))
    wc = doc.blocks.new("변기")
    wc.add_circle((0, 0), 200)
    msp = doc.modelspace()
    msp.add_line((0, 0), (10000, 0), dxfattribs={"layer": "A-WALL"})
    msp.add_line((5000, 0), (5000, 8000), dxfattribs={"layer": "A-WALL"})
    msp.add_blockref("SD1", (2000, 50), dxfattribs={"layer": "A-DOOR"})
    msp.add_text("거실", height=250, dxfattribs={"layer": "A-ANNO", "insert": (2000, 4000)})
    msp.add_text("25.5㎡", height=200, dxfattribs={"layer": "A-ANNO", "insert": (2000, 3600)})
    msp.add_text("화장실", height=250, dxfattribs={"layer": "A-ANNO", "insert": (7000, 4000)})
    msp.add_blockref("변기", (7500, 5000), dxfattribs={"layer": "가구"})
    doc.saveas(path)
    return path


def _parse(tmp_path: Path):
    source = build_plan(tmp_path / "1층 평면도.dxf")
    settings = Settings(dsn="", data_root=tmp_path, import_roots=(tmp_path,))
    return parse_source(source, "doc_plan", tmp_path / "out", settings)


def test_operational_dxf_emits_spatial_relations_and_storey(tmp_path: Path):
    result = _parse(tmp_path)
    objects = {o["id"]: o for o in result["objects"]}
    by_kind = {}
    for obj in result["objects"]:
        by_kind.setdefault(obj["type"], []).append(obj)
    assert [o["properties"]["storeyName"] for o in by_kind["Storey"]] == ["1F"]
    assert by_kind["Sheet"], "layout sheet object"
    spaces = {o["properties"]["roomName"]: o for o in by_kind["Space"]}
    assert set(spaces) >= {"거실", "화장실"}
    assert all(s["storey"] == "1F" for s in spaces.values())
    assert spaces["거실"]["properties"]["area"] == 25.5

    predicates = {}
    for rel in result["relations"]:
        predicates.setdefault(rel["predicate"], []).append(rel)
    for predicate in ("hostedBy", "containsElement", "hasSpace", "onStorey", "depicts"):
        assert predicate in predicates, predicate
    storey_id = by_kind["Storey"][0]["id"]
    assert {r["object"] for r in predicates["hasSpace"] if r["subject"] == storey_id} >= {s["id"] for s in spaces.values()}
    hosted = predicates["hostedBy"][0]
    assert objects[hosted["subject"]]["type"] == "Door" and objects[hosted["object"]]["type"] == "Wall"
    contained = [r for r in predicates["containsElement"] if objects[r["object"]]["type"] == "Furniture"]
    assert contained and contained[0]["subject"] == spaces["화장실"]["id"]
    assert all(r["state"] == "AI_INFERRED" and 0 < r["evidence"]["confidence"] <= 1 for r in predicates["hasSpace"])
    # every endpoint is an object of the document (no dangling CAIR ids)
    for rel in result["relations"]:
        assert rel["subject"] in objects and rel["object"] in objects


def test_graph_edges_project_confident_inferences_with_predicate_labels(monkeypatch):
    rels = [
        {"subject": "a", "predicate": "hasSpace", "object": "b", "state": "AI_INFERRED", "evidence": {"confidence": 0.8}},
        {"subject": "a", "predicate": "containsElement", "object": "c", "state": "AI_INFERRED", "evidence": {"confidence": 0.5}},
        {"subject": "a", "predicate": "hasSection", "object": "d", "state": "AI_INFERRED", "evidence": {}},
        {"subject": "a", "predicate": "contains", "object": "e", "state": "OBSERVED", "evidence": {}},
        {"subject": "a", "predicate": "bad label!", "object": "f", "state": "OBSERVED", "evidence": {}},
    ]
    edges = graph_edges(rels)
    assert [(e["b"], e["label"]) for e in edges] == [("b", "hasSpace"), ("e", "contains"), ("f", "Rel")]
    assert edges[0]["props"] == {"kind": "hasSpace", "state": "AI_INFERRED", "confidence": 0.8}
    monkeypatch.setenv("AEC_GRAPH_MIN_CONFIDENCE", "0.4")
    assert "c" in {e["b"] for e in graph_edges(rels)}
    assert edge_label("Entity") == "Rel" and edge_label("x" * 64) == "Rel" and edge_label("onStorey") == "onStorey"


def test_ranking_prefers_names_and_asked_kind():
    assert kind_intents("1층 평면도 방 목록") == ["Space"]
    assert "SteelSection" in kind_intents("H-400x200 부재")
    assert storey_intent("1층 평면도 방 목록") == "1F" and storey_intent("지하2층") == "B2"
    query = "1층 평면도 방 목록"
    space = ranking_boost(query, "Space", "plan 거실", {"roomName": "거실"}, "1F")
    note = ranking_boost(query, "Annotation", "1층 평면도", {}, "")
    assert space > note
    exact = ranking_boost("화장실", "Space", "plan 화장실 실 공간", {"roomName": "화장실"})
    similar = ranking_boost("화장실", "Annotation", "plan 화장 마감", {})
    assert exact >= 1.0 > similar
    assert ranking_boost("H-400x200", "SteelSection", "x", {"sectionDesignation": "H-400x200"}) > 1.0


@pytest.mark.skipif(not os.getenv("AEC_TEST_DATABASE_URL"), reason="AEC_TEST_DATABASE_URL not set")
def test_storey_spaces_answerable_in_age(tmp_path: Path):
    pytest.importorskip("psycopg")
    from aec_intelligence.operational.db import Database, graph_name
    from aec_intelligence.operational.search import SearchRouter
    from aec_intelligence.operational.worker import IngestionWorker

    dsn = os.environ["AEC_TEST_DATABASE_URL"]
    imports = tmp_path / "imports"
    imports.mkdir()
    source = build_plan(imports / "1층 평면도.dxf")
    settings = Settings(dsn=dsn, data_root=tmp_path, import_roots=(imports,))
    db = Database(dsn)
    db.initialize()
    project = f"P-graph-{os.getpid()}"
    key = f"graph:{project}:{tmp_path.name}"
    db.enqueue({"source": str(source), "project_id": project, "document_id": f"doc_graph_{os.getpid()}"}, dedup_key=key)
    assert IngestionWorker(db, settings).run_once()
    graph = graph_name(project)
    with db.connect() as conn:
        job = conn.execute("SELECT state, error FROM aec.jobs WHERE dedup_key=%s", (key,)).fetchone()
        assert job["state"] == "SUCCEEDED", job["error"]
        # "1층 평면도 방 목록": Storey 1F -hasSpace-> Space
        rooms = db.cypher(conn, graph, "MATCH (s:Entity {kind: 'Storey', name: '1F'})-[r:hasSpace]->(sp:Entity) "
                                       "RETURN [sp.name, r.state, r.confidence]")
        compat = db.cypher(conn, graph, "MATCH ()-[r:Rel]->() WHERE r.kind = 'onStorey' RETURN count(r)")
        hosted = db.cypher(conn, graph, "MATCH (:Entity {kind: 'Door'})-[r:hostedBy]->(:Entity {kind: 'Wall'}) RETURN count(r)")
        storeys = {r["storey"] for r in conn.execute(
            "SELECT storey FROM aec.objects WHERE project_id=%s AND kind='Space'", (project,))}
    names = sorted(str(r["value"]) for r in rooms)
    assert any("거실" in n for n in names) and any("화장실" in n for n in names)
    assert all('"AI_INFERRED"' in n for n in names)
    assert int(str(compat[0]["value"])) >= 2
    assert int(str(hosted[0]["value"])) >= 1
    assert storeys == {"1F"}
    result = SearchRouter(db, settings).search("1층 평면도 방 목록", project_id=project, top_k=3, expand_graph=True)
    assert result.hits and result.hits[0].kind == "Space"
    assert any(rel.get("predicate") in ("onStorey", "hasSpace") for rel in result.hits[0].relations)
