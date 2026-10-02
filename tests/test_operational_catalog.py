"""Element catalog (SQL, REST, MCP) and embedding client tests.

The database tests need AEC_TEST_DATABASE_URL (a disposable Postgres with AGE, pgvector, PostGIS);
they use unique project ids and remove their rows afterwards because the database may be shared.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from aec_intelligence.mcp_gateway import MCPGateway
from aec_intelligence.operational import catalog
from aec_intelligence.operational import embeddings as emb
from aec_intelligence.operational.config import Settings

DSN = os.getenv("AEC_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="AEC_TEST_DATABASE_URL not set")
FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"
CATALOG_TOOLS = {"aec.element_catalog", "aec.find_elements", "aec.block_catalog", "aec.drawing_index", "aec.element_context"}


# --------------------------------------------------------------------------- pure helpers

def test_kind_and_category_vocabulary_resolve_korean_terms():
    assert catalog.resolve_kinds("문") == ["Door"]
    assert catalog.resolve_kinds("창호, wall") == ["Window", "Wall"]
    assert catalog.resolve_kinds(["도곽", "블록"]) == ["TitleBlock", "BlockDefinition"]
    assert catalog.resolve_kinds("CustomKind") == ["CustomKind"]
    assert catalog.resolve_category("detail")[0] == "상세도"
    assert catalog.resolve_category("평면도")[0] == "평면도"
    assert catalog.parse_bbox("10,20,0,0") == [0.0, 0.0, 10.0, 20.0]
    with pytest.raises(ValueError):
        catalog.decode_cursor("!!notacursor")
    assert catalog.decode_cursor(catalog.encode_cursor("obs_x")) == "obs_x"
    assert catalog.attribute_tags([{"tag": "NO"}, "W"]) == ["NO", "W"]
    assert catalog.attribute_tags({"NO": "D1"}) == ["NO"]


def test_catalog_tools_are_listed_and_require_configuration_without_dsn(tmp_path, monkeypatch):
    monkeypatch.delenv("AEC_DATABASE_URL", raising=False)
    gateway = MCPGateway(tmp_path)
    assert CATALOG_TOOLS <= {tool["name"] for tool in gateway.list_tools()}
    for name in CATALOG_TOOLS:
        args = {"object_id": "obs_missing"} if name == "aec.element_context" else {}
        assert gateway.call_tool(name, args)["status"] == "REQUIRES_CONFIGURATION"


def test_catalog_tools_report_unreachable_database_as_configuration(tmp_path, monkeypatch):
    pytest.importorskip("psycopg")
    monkeypatch.setenv("AEC_DATABASE_URL", f"postgresql://nobody@/aec?host={tmp_path}&port=1&connect_timeout=1")
    result = MCPGateway(tmp_path).call_tool("aec.element_catalog", {})
    assert result["status"] == "REQUIRES_CONFIGURATION"
    assert "unreachable" in result["error"]


# --------------------------------------------------------------------------- embedding client

class _StubEmbeddings(BaseHTTPRequestHandler):
    calls: list = []
    fail_first = 0
    dims = emb.EMBEDDING_DIM

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append((self.path, body))
        if type(self).fail_first > 0:
            type(self).fail_first -= 1
            self.send_response(503); self.end_headers(); return
        if self.path == "/embed":
            texts = body["inputs"]
            data = [[1.0 / (i + 1)] + [0.0] * (self.dims - 1) for i, _ in enumerate(texts)]
        else:
            texts = body["input"]
            data = {"object": "list", "model": body.get("model"),
                    "data": [{"object": "embedding", "index": i, "embedding": [0.0] * i + [1.0] + [0.0] * (self.dims - i - 1)}
                             for i, _ in reversed(list(enumerate(texts)))]}
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture()
def stub_server():
    _StubEmbeddings.calls, _StubEmbeddings.fail_first, _StubEmbeddings.dims = [], 0, emb.EMBEDDING_DIM
    emb._CIRCUIT.clear()
    server = HTTPServer(("127.0.0.1", 0), _StubEmbeddings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    emb._CIRCUIT.clear()


def _settings(url="", dsn="dummy", root=Path(".")):
    return Settings(dsn=dsn, data_root=root, import_roots=(), embedding_url=url, embedding_model="BAAI/bge-m3")


def test_embedding_client_batches_openai_route_and_orders_by_index(stub_server):
    service = emb.EmbeddingService(_settings(stub_server), batch_size=2, retries=1)
    model, vectors = service.embed_with_model(["a", "b", "c", "d", "e"])
    assert model == "BAAI/bge-m3" == service.active_model()
    assert [path for path, _ in _StubEmbeddings.calls] == ["/v1/embeddings"] * 3
    assert [len(body["input"]) for _, body in _StubEmbeddings.calls] == [2, 2, 1]
    assert _StubEmbeddings.calls[0][1]["model"] == "BAAI/bge-m3"
    assert vectors[0][0] == 1.0 and vectors[1][1] == 1.0  # index order restored within each batch


def test_embedding_client_retries_then_succeeds(stub_server):
    _StubEmbeddings.fail_first = 2
    service = emb.EmbeddingService(_settings(stub_server), retries=3, backoff=0.01)
    model, vectors = service.embed_with_model(["벽체"])
    assert model == "BAAI/bge-m3" and len(vectors[0]) == emb.EMBEDDING_DIM
    assert len(_StubEmbeddings.calls) == 3


def test_embedding_client_supports_native_tei_route(stub_server):
    service = emb.EmbeddingService(_settings(stub_server + "/embed"), retries=1)
    model, vectors = service.embed_with_model(["x", "y"])
    assert model == "BAAI/bge-m3" and vectors[1][0] == 0.5
    assert _StubEmbeddings.calls[0][1] == {"inputs": ["x", "y"], "truncate": True}


def test_embedding_client_falls_back_to_labelled_hash_and_opens_circuit(stub_server):
    _StubEmbeddings.dims = 384  # wrong dimension must never be padded into vector(1024)
    service = emb.EmbeddingService(_settings(stub_server), retries=2, backoff=0.01)
    model, vectors = service.embed_with_model(["문", "창"])
    assert model == emb.HASH_MODEL
    assert vectors[0] == emb._deterministic_hash_vector("문")
    assert "384" in service.last_error
    calls = len(_StubEmbeddings.calls)
    assert emb.EmbeddingService(_settings(stub_server)).embed_with_model(["x"])[0] == emb.HASH_MODEL
    assert len(_StubEmbeddings.calls) == calls  # circuit open: no further remote calls


def test_embedding_client_without_endpoint_uses_hash_model():
    service = emb.EmbeddingService(_settings(""))
    assert service.active_model() == emb.HASH_MODEL
    assert service.embed_with_model(["벽"])[0] == emb.HASH_MODEL


# --------------------------------------------------------------------------- database seeding

def _obs(doc, key, kind, label, layout="Model", handle=None, props=None, bbox=None, state="OBSERVED"):
    from aec_intelligence.operational.parsers import observation
    evidence = {"source_path": f"imports/{doc}.dxf", "source_name": f"{doc}.dxf", "layout": layout,
                "coordinate_system": "CAD_WCS"}
    if handle:
        evidence["handle"] = handle
    return observation(doc, key, kind, label, evidence, bbox, state=state, properties=props or {})


def _synthetic_snapshot(project, doc):
    from aec_intelligence.operational.parsers import relation
    document = _obs(doc, "document", "Document", f"{doc}.dxf")
    plan = _obs(doc, "layout:A-101", "View", "1층 평면도", layout="A-101", props={"drawing_category": "평면도"})
    detail = _obs(doc, "layout:A-501", "View", "창호 상세도", layout="A-501", props={"drawing_category": "상세도"})
    tb = _obs(doc, "tb", "TitleBlock", "TB", layout="A-101", handle="TB1",
              props={"drawingNumber": "A-101", "drawingTitle": "1층 평면도", "scale": "1/100", "block_name": "TITLE_A1"})
    door_def = _obs(doc, "blk:DOOR_SD", "BlockDefinition", "DOOR_SD",
                    props={"name": "DOOR_SD", "effective_names": ["DOOR_SD"], "attribute_defs": [{"tag": "DOOR_NO"}, {"tag": "FIRE"}],
                           "insert_count": 2, "layers": ["A-DOOR"], "is_xref": False, "is_anonymous": False})
    dyn_def = _obs(doc, "blk:*U12", "BlockDefinition", "*U12",
                   props={"name": "*U12", "effective_names": ["WIN_SLIDE"], "is_anonymous": True, "insert_count": 1})
    door1 = _obs(doc, "A-101:D1", "Door", "SD1", layout="A-101", handle="D1", bbox=dict(min_x=0, min_y=0, max_x=1, max_y=1),
                 props={"block_name": "DOOR_SD", "layer": "A-DOOR", "attributes": {"DOOR_NO": "SD1", "FIRE": "갑종"}})
    door2 = _obs(doc, "A-101:D2", "Door", "SD2", layout="A-101", handle="D2", bbox=dict(min_x=50, min_y=50, max_x=51, max_y=51),
                 props={"block_name": "DOOR_SD", "dxf_attributes": {"layer": "A-DOOR"}, "attributes": {"DOOR_NO": "SD2"}})
    door1["storey"] = "1F"
    window = _obs(doc, "A-501:W1", "Window", "W1", layout="A-501", handle="W1",
                  props={"block_name": "*U12", "effective_name": "WIN_SLIDE", "layer": "A-WIND", "attributes": {"WIN_NO": "W1"}})
    wall = _obs(doc, "A-101:L1", "Wall", "벽체", layout="A-101", handle="L1", props={"layer": "A-WALL"},
                bbox=dict(min_x=0, min_y=0, max_x=10, max_y=0.2), state="AI_INFERRED")
    section = _obs(doc, "sec:H200", "SteelSection", "H-200x100", props={"designation": "H-200x100"})
    objects = [document, plan, detail, tb, door_def, dyn_def, door1, door2, window, wall, section]
    relations = [relation(document["id"], "contains", plan["id"]), relation(document["id"], "contains", detail["id"]),
                 relation(plan["id"], "contains", door1["id"]), relation(plan["id"], "contains", door2["id"]),
                 relation(plan["id"], "contains", wall["id"]), relation(detail["id"], "contains", window["id"]),
                 relation(plan["id"], "hasTitleBlock", tb["id"]),
                 relation(door1["id"], "instanceOf", door_def["id"]), relation(door2["id"], "instanceOf", door_def["id"]),
                 relation(window["id"], "instanceOf", dyn_def["id"]), relation(wall["id"], "hasSection", section["id"])]
    ids = {"plan": plan["id"], "door1": door1["id"], "door_def": door_def["id"], "wall": wall["id"], "tb": tb["id"],
           "section": section["id"]}
    return {"document_id": doc, "project_id": project, "source_key": f"/imports/{doc}.dxf", "name": f"{doc}.dxf",
            "revision": 0, "source_hash": "synthetic", "objects": objects, "relations": relations, "units": "mm",
            "discipline": "ARCH"}, ids


@pytest.fixture()
def seeded(tmp_path):
    pytest.importorskip("psycopg")
    pytest.importorskip("ezdxf")
    from aec_intelligence.operational.db import Database, graph_name
    from aec_intelligence.operational.worker import IngestionWorker

    db = Database(DSN)
    db.initialize()
    tag = uuid.uuid4().hex[:10]
    project, doc, dxf_doc = f"P-cat-{tag}", f"doc_cat_{tag}", f"doc_catdxf_{tag}"
    imports = tmp_path / "imports"; imports.mkdir()
    source = imports / FIXTURE.name
    shutil.copy(FIXTURE, source)
    settings = Settings(dsn=DSN, data_root=tmp_path, import_roots=(imports,))
    dedup = f"cat:{tag}"
    try:
        db.enqueue({"source": str(source), "project_id": project, "document_id": dxf_doc}, dedup_key=dedup)
        assert IngestionWorker(db, settings).run_once()
        snapshot, ids = _synthetic_snapshot(project, doc)
        with db.connect() as conn:
            job = conn.execute("SELECT state, error FROM aec.jobs WHERE dedup_key=%s", (dedup,)).fetchone()
            assert job["state"] == "SUCCEEDED", job["error"]
            db.project(conn, snapshot, f"snapshots/{doc}/rev-0.json")
        yield {"db": db, "project": project, "doc": doc, "dxf_doc": dxf_doc, "ids": ids, "settings": settings}
    finally:
        with db.connect() as conn:
            conn.execute("DELETE FROM aec.objects WHERE project_id=%s", (project,))
            conn.execute("DELETE FROM aec.relations WHERE project_id=%s", (project,))
            for d in (doc, dxf_doc):
                conn.execute("DELETE FROM aec.index_state WHERE document_id=%s", (d,))
                conn.execute("DELETE FROM aec.snapshots WHERE document_id=%s", (d,))
            conn.execute("DELETE FROM aec.documents WHERE project_id=%s", (project,))
            conn.execute("DELETE FROM aec.jobs WHERE dedup_key=%s", (dedup,))
        _drop_graph(db, graph_name(project))


def _drop_graph(db, graph):
    """drop_graph takes exclusive locks on the AGE catalog; retry when a concurrent session on the shared DB deadlocks."""
    import time
    import psycopg

    for attempt in range(5):
        try:
            with db.connect() as conn:
                if conn.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name=%s", (graph,)).fetchone():
                    conn.execute("SELECT drop_graph(%s, true)", (graph,))
            return
        except psycopg.errors.DeadlockDetected:
            time.sleep(0.2 * (attempt + 1))


# --------------------------------------------------------------------------- catalog functions

@needs_db
def test_element_catalog_is_a_table_of_contents(seeded):
    result = catalog.element_catalog(seeded["db"], project_id=seeded["project"])
    kinds = {k["kind"]: k for k in result["kinds"]}
    assert kinds["Door"]["count"] >= 2 and "문" in kinds["Door"]["aliases_ko"]
    assert kinds["Wall"]["ai_inferred_candidates"] >= 1
    assert {"BlockDefinition", "TitleBlock", "View", "SteelSection", "Window"} <= set(kinds)
    assert result["totals"]["documents"] == 2
    cats = {c["category"]: c for c in result["drawing_categories"]}
    assert cats["평면도"]["kinds"] == {"View": 1} and "상세도" in cats
    predicates = {p["predicate"] for p in result["relation_predicates"]}
    assert {"contains", "instanceOf", "hasTitleBlock", "hasSection"} <= predicates
    layers = {l["layer"]: l for l in result["layers"]}
    assert set(layers["A-DOOR"]["kinds"]) == {"Door"} and layers["A-DOOR"]["count"] >= 2  # DXF fixture adds more
    assert result["blocks"]["definitions"] == 2
    assert result["projects"][0]["project_id"] == seeded["project"]


@needs_db
def test_find_elements_filters_and_paginates(seeded):
    db, project = seeded["db"], seeded["project"]
    doors = catalog.find_elements(db, kind="문", project_id=project, document_id=seeded["doc"])
    assert doors["count"] == 2
    first = doors["items"][0]
    assert first["block_name"] == "DOOR_SD" and first["layer"] == "A-DOOR"
    assert first["evidence"]["handle"] in {"D1", "D2"} and first["evidence"]["layout"] == "A-101"
    assert first["evidence"]["source_path"].endswith(".dxf")

    page1 = catalog.find_elements(db, project_id=project, document_id=seeded["doc"], limit=4)
    page2 = catalog.find_elements(db, project_id=project, document_id=seeded["doc"], limit=4, cursor=page1["next_cursor"])
    assert page1["next_cursor"] and page2["items"]
    assert not {i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]}

    assert {i["kind"] for i in catalog.find_elements(db, project_id=project, drawing_category="detail")["items"]} == {"View", "Window"}
    plan_items = catalog.find_elements(db, project_id=project, drawing_category="평면도")["items"]
    assert {"Door", "Wall", "TitleBlock", "View"} <= {i["kind"] for i in plan_items}
    assert [i["label"] for i in catalog.find_elements(db, document_id=seeded["doc"], layer="a-wa*")["items"]] == ["벽체"]
    by_effective = catalog.find_elements(db, project_id=project, block_name="WIN_SLIDE")["items"]
    assert sorted(i["kind"] for i in by_effective) == ["BlockDefinition", "Window"]  # anonymous *U12 def + its INSERT
    window = next(i for i in by_effective if i["kind"] == "Window")
    assert window["attributes"] == {"WIN_NO": "W1"} and window["block_name"] == "WIN_SLIDE"
    assert [i["label"] for i in catalog.find_elements(db, project_id=project, text="갑종")["items"]] == ["SD1"]
    for storey in ("1층", "1F", "1f", "1"):
        on_first = catalog.find_elements(db, project_id=project, document_id=seeded["doc"], storey=storey)
        assert [i["label"] for i in on_first["items"]] == ["SD1"] and on_first["filters"]["storey"] == "1F"
    assert catalog.find_elements(db, project_id=project, document_id=seeded["doc"], storey="지하1층")["items"] == []
    near_origin = catalog.find_elements(db, document_id=seeded["doc"], kind="Door", bbox=[-1, -1, 2, 2])["items"]
    assert [i["label"] for i in near_origin] == ["SD1"]
    assert catalog.find_elements(db, project_id=project, kind="Door", include_properties=True)["items"][0]["properties"]
    # real DXF ingestion is discoverable through the same call
    dxf_items = catalog.find_elements(db, project_id=project, document_id=seeded["dxf_doc"], limit=500)["items"]
    assert any(i["kind"] == "Wall" and i["layer"] for i in dxf_items)


@needs_db
def test_block_catalog_aggregates_definitions_and_instances(seeded):
    result = catalog.block_catalog(seeded["db"], project_id=seeded["project"])
    blocks = {b["name"]: b for b in result["items"]}
    door = blocks["DOOR_SD"]
    assert door["instance_count"] == 2 and door["declared_insert_count"] == 2
    assert door["instance_kinds"] == {"Door": 2} and door["classified_as"] == "Door"
    assert {"DOOR_NO", "FIRE"} <= set(door["attribute_tags"]) and door["instanceOf_relations"] == 2
    assert door["layers"] == ["A-DOOR"] and door["definitions"][0]["document_id"] == seeded["doc"]
    assert blocks["*U12"]["is_anonymous"] and blocks["*U12"]["effective_names"] == ["WIN_SLIDE"]
    assert blocks["WIN_SLIDE"]["instance_kinds"] == {"Window": 1}
    assert blocks["TITLE_A1"]["instance_kinds"] == {"TitleBlock": 1}
    filtered = catalog.block_catalog(seeded["db"], project_id=seeded["project"], name_like="door")
    assert [b["name"] for b in filtered["items"]] == ["DOOR_SD"]
    paged = catalog.block_catalog(seeded["db"], project_id=seeded["project"], limit=1)
    assert paged["count"] == 1 and paged["next_cursor"]


@needs_db
def test_drawing_index_lists_sheets_with_title_blocks(seeded):
    result = catalog.drawing_index(seeded["db"], project_id=seeded["project"])
    docs = {d["document_id"]: d for d in result["items"]}
    assert set(docs) == {seeded["doc"], seeded["dxf_doc"]}
    synthetic = docs[seeded["doc"]]
    assert synthetic["drawing_categories"] == ["상세도", "평면도"]
    sheets = {s["layout"]: s for s in synthetic["sheets"]}
    assert sheets["A-101"]["title_block"]["drawing_number"] == "A-101"
    assert sheets["A-101"]["title_block"]["scale"] == "1/100"
    assert sheets["A-101"]["drawing_category"] == "평면도"
    assert sheets["A-101"]["element_counts"]["Door"] == 2
    assert docs[seeded["dxf_doc"]]["sheets"]
    only_detail = catalog.drawing_index(seeded["db"], project_id=seeded["project"], category="상세도")
    assert [d["document_id"] for d in only_detail["items"]] == [seeded["doc"]]


@needs_db
def test_element_context_walks_relations_and_age(seeded):
    ids = seeded["ids"]
    ctx = catalog.element_context(seeded["db"], ids["door1"])
    assert ctx["element"]["id"] == ids["door1"]
    assert ctx["neighbour_summary"] == {"contains:in": 1, "instanceOf:out": 1}
    assert ctx["graph"]["available"] is True and ctx["graph"]["edges"] >= 1
    assert all(e.get("in_graph") for e in ctx["edges"] if e["state"] == "OBSERVED")
    two = catalog.element_context(seeded["db"], ids["door1"], hops=2)
    node_ids = {n["id"] for n in two["nodes"]}
    assert ids["door_def"] in node_ids and ids["tb"] in node_ids  # via instanceOf and the sheet's hasTitleBlock
    assert ids["wall"] not in node_ids  # siblings are not expanded through the container
    wall = catalog.element_context(seeded["db"], ids["wall"])
    assert ids["section"] in {n["id"] for n in wall["nodes"]}
    assert catalog.element_context(seeded["db"], "obs_does_not_exist") is None


# --------------------------------------------------------------------------- REST and MCP

@needs_db
def test_rest_catalog_endpoints(seeded):
    from fastapi.testclient import TestClient
    from aec_intelligence.operational.api import create_app

    client = TestClient(create_app(seeded["settings"]))
    project = seeded["project"]
    assert client.get("/v1/catalog", params={"project_id": project}).json()["totals"]["projects"] == 1
    elements = client.get("/v1/elements", params={"project_id": project, "document_id": seeded["doc"], "kind": "Door,창호", "limit": 2}).json()
    assert elements["count"] == 2 and elements["next_cursor"]
    rest = client.get("/v1/elements", params={"project_id": project, "document_id": seeded["doc"], "kind": "Door,창호", "cursor": elements["next_cursor"]}).json()
    assert rest["count"] == 1
    assert client.get("/v1/elements", params={"project_id": project, "document_id": seeded["doc"], "storey": "1층"}).json()["count"] == 1
    assert client.get("/v1/elements", params={"cursor": "%%%"}).status_code == 400
    assert client.get("/v1/elements", params={"bbox": "1,2"}).status_code == 400
    assert client.get("/v1/elements", params={"project_id": project, "document_id": seeded["doc"], "bbox": "-1,-1,2,2", "kind": "Door"}).json()["count"] == 1
    blocks = client.get("/v1/blocks", params={"project_id": project, "name_like": "DOOR*"}).json()
    assert blocks["items"][0]["name"] == "DOOR_SD"
    drawings = client.get("/v1/drawings", params={"project_id": project, "category": "plan"}).json()
    assert [d["document_id"] for d in drawings["items"]] == [seeded["doc"]]
    ctx = client.get(f"/v1/elements/{seeded['ids']['door1']}/context", params={"hops": 2}).json()
    assert ctx["hops"] == 2
    assert client.get("/v1/elements/obs_missing/context").status_code == 404


@needs_db
def test_mcp_catalog_tools_with_database(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("AEC_DATABASE_URL", DSN)
    gateway = MCPGateway(tmp_path)
    project = seeded["project"]
    toc = gateway.call_tool("aec.element_catalog", {"project_id": project})
    assert toc["status"] == "SUCCESS" and any(k["kind"] == "Door" for k in toc["kinds"])
    found = gateway.call_tool("aec.find_elements", {"project_id": project, "document_id": seeded["doc"], "kind": ["창"], "limit": 10})
    assert found["status"] == "SUCCESS" and found["items"][0]["evidence"]["handle"] == "W1"
    assert gateway.call_tool("aec.block_catalog", {"project_id": project, "name_like": "DOOR"})["items"][0]["instance_count"] == 2
    index = gateway.call_tool("aec.drawing_index", {"project_id": project, "category": "상세도"})
    assert index["status"] == "SUCCESS" and index["count"] == 1
    ctx = gateway.call_tool("aec.element_context", {"object_id": seeded["ids"]["door1"], "hops": 2})
    assert ctx["status"] == "SUCCESS" and ctx["nodes"]
    assert gateway.call_tool("aec.element_context", {"object_id": "obs_missing"})["status"] == "NOT_FOUND"
    assert gateway.call_tool("aec.find_elements", {"cursor": "%%%"})["status"] == "FAILED"
    on_floor = gateway.call_tool("aec.find_elements", {"project_id": project, "document_id": seeded["doc"], "storey": "1층"})
    assert on_floor["status"] == "SUCCESS" and [i["label"] for i in on_floor["items"]] == ["SD1"]


# --------------------------------------------------------------------------- embeddings in Postgres

@needs_db
def test_search_only_compares_vectors_of_the_active_model(seeded, stub_server):
    from aec_intelligence.operational.search import SearchRouter

    db, project = seeded["db"], seeded["project"]
    with db.connect() as conn:
        rows = conn.execute("SELECT payload FROM aec.objects WHERE document_id=%s", (seeded["doc"],)).fetchall()
        snapshot = {"document_id": seeded["doc"], "revision": 0, "objects": [r["payload"] for r in rows]}
        real = emb.index_snapshot_embeddings(conn, snapshot, _settings(stub_server, DSN))  # stub "bge-m3" vectors
        hashed = emb.index_snapshot_embeddings(conn, snapshot, _settings("", DSN))
        models = {r["model"] for r in conn.execute("""SELECT DISTINCT e.model FROM aec.embeddings e JOIN aec.objects o
            ON o.id = e.object_id WHERE o.document_id=%s""", (seeded["doc"],))}
        state = conn.execute("SELECT embedding_model FROM aec.index_state WHERE document_id=%s", (seeded["doc"],)).fetchone()
    assert real == hashed == len(rows)
    assert models == {"BAAI/bge-m3", emb.HASH_MODEL} and state["embedding_model"] == emb.HASH_MODEL

    result = SearchRouter(db, _settings(stub_server, DSN)).search("SD1", project_id=project, top_k=20, expand_graph=False)
    ids = [h.object_id for h in result.hits]
    assert len(ids) == len(set(ids))  # one row per object: the join is restricted to one model
    assert not result.warnings

    report = emb.reindex_embeddings(db, _settings(stub_server, DSN), project_id=project)
    assert report["model"] == "BAAI/bge-m3" and report["skipped"] == 0
    with db.connect() as conn:
        missing = conn.execute("""SELECT count(*) AS n FROM aec.objects o WHERE o.project_id=%s AND o.kind <> 'CADEntity'
            AND NOT EXISTS (SELECT 1 FROM aec.embeddings e WHERE e.object_id=o.id AND e.model='BAAI/bge-m3')""",
            (project,)).fetchone()["n"]
        leftover_hash = conn.execute("""SELECT count(*) AS n FROM aec.embeddings e JOIN aec.objects o ON o.id=e.object_id
            WHERE o.document_id=%s AND e.model=%s""", (seeded["dxf_doc"], emb.HASH_MODEL)).fetchone()["n"]
    assert missing == 0 and leftover_hash == 0
