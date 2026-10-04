"""Phase 4 knowledge graph + Graph RAG against a real PostgreSQL (synthetic rows, no private data).

Skipped unless AEC_TEST_DATABASE_URL points at a disposable database.
"""

import json
import os
import uuid

import pytest

DSN = os.getenv("AEC_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="AEC_TEST_DATABASE_URL not set")
RUN = f"{os.getpid()}"


def _obj(doc, n, kind, label, storey="", props=None, bbox=True, layout="Model"):
    oid = f"obs_gr_{RUN}_{doc}_{n}"
    payload = {"id": oid, "type": kind, "label": label, "state": "AI_INFERRED", "storey": storey,
               "evidence": {"layout": layout, "handle": f"H{n}", "coordinate_system": "CAD_WCS"},
               "properties": props or {}}
    if bbox:
        payload["bbox"] = {"min_x": n, "min_y": 0, "max_x": n + 10, "max_y": 10}
    return oid, kind, storey, label, payload


@pytest.fixture()
def seeded():
    pytest.importorskip("psycopg")
    from aec_intelligence.operational.db import Database

    db = Database(DSN)
    db.initialize()
    pid_a, pid_b = f"P-gr{RUN}-허가", f"P-gr{RUN}-사용승인"
    top = f"##그래프시험{RUN}"
    docs = [
        (f"doc_gr_{RUN}_1", pid_a, "A-101 1층 평면도_0611.dwg", f"{top}/#허가"),
        (f"doc_gr_{RUN}_2", pid_a, "A-101 1층 평면도_0626.dwg", f"{top}/#허가"),
        (f"doc_gr_{RUN}_3", pid_b, "S-201 2층 구조평면도.dwg", f"{top}/#사용승인"),
    ]
    objects = {
        docs[0][0]: [_obj(1, 1, "Space", "회의실 1", "1F", {"roomName": "회의실 1", "roomNameNormalized": "회의실1",
                                                         "roomNameAliases": ["회의실 1", "회의실1"], "area": 25.5}),
                     _obj(1, 2, "Door", "D1"), _obj(1, 3, "Door", "D2"),
                     _obj(1, 4, "Sheet", "A-101", props={"drawingNumber": "A-101", "drawingTitle": "1층 평면도"},
                          bbox=False)],
        docs[1][0]: [_obj(2, 1, "Space", "회의실1", "1F", {"roomName": "회의실1", "roomNameNormalized": "회의실1"}),
                     _obj(2, 2, "Space", "화장실", "1F", {"roomName": "화장실"}), _obj(2, 3, "Door", "D1")],
        docs[2][0]: [_obj(3, 1, "SteelSection", "H-300x150x6.5x9",
                          props={"sectionDesignation": "H-300x150x6.5x9"}),
                     _obj(3, 2, "Beam", "B1"), _obj(3, 3, "Space", "기계실", "2F", {"roomName": "기계실"})],
    }
    with db.connect() as conn:
        for did, pid, name, folder in docs:
            conn.execute("INSERT INTO aec.documents(id, project_id, source_key, name) VALUES (%s,%s,%s,%s)",
                         (did, pid, f"G:/x/{did}/{name}", name))
            conn.execute("INSERT INTO aec.jobs(id, dedup_key, payload, state) VALUES (%s,%s,%s,'SUCCEEDED')",
                         (str(uuid.uuid4()), f"gr:{did}", json.dumps({"document_id": did, "project_id": pid,
                                                                       "top_folder": folder, "aliases": []})))
            for oid, kind, storey, label, payload in objects[did]:
                conn.execute("""INSERT INTO aec.objects(id, project_id, document_id, revision, kind, storey, label,
                                search_text, payload) VALUES (%s,%s,%s,0,%s,%s,%s,%s,%s)""",
                             (oid, pid, did, kind, storey, label, f"{name} {label} {kind}", json.dumps(payload)))
        beam, sec = objects[docs[2][0]][1][0], objects[docs[2][0]][0][0]
        conn.execute("""INSERT INTO aec.relations(id, project_id, document_id, revision, subject, predicate, object,
                        state, evidence) VALUES (%s,%s,%s,0,%s,'hasSection',%s,'AI_INFERRED','{}')""",
                     (f"rel_gr_{RUN}", pid_b, docs[2][0], beam, sec))
        conn.commit()
    yield db, f"그래프시험{RUN}".casefold(), docs
    with db.connect() as conn:
        ids = [d[0] for d in docs]
        conn.execute("DELETE FROM aec.kg_communities WHERE project_key LIKE %s", (f"%{RUN}%",))
        conn.execute("DELETE FROM aec.kg_nodes WHERE project_key LIKE %s", (f"%{RUN}%",))
        conn.execute("DELETE FROM aec.kg_build_state WHERE project_key LIKE %s", (f"%{RUN}%",))
        conn.execute("DELETE FROM aec.relations WHERE document_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM aec.objects WHERE document_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM aec.jobs WHERE dedup_key LIKE %s", (f"gr:doc_gr_{RUN}%",))
        conn.execute("DELETE FROM aec.documents WHERE id = ANY(%s)", (ids,))
        conn.commit()


class FakeLLM:
    model = "fake"

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def chat(self, system, user, max_tokens=600, temperature=0.0):
        self.calls += 1
        return {"text": self.text, "model": "fake", "seconds": 0.01}


def test_kg_resolves_projects_rooms_revisions_and_sections(seeded):
    from aec_intelligence.operational.graphrag.kg import KnowledgeGraphBuilder

    db, key, docs = seeded
    stats = KnowledgeGraphBuilder(db).build(key)
    assert stats["projects"] == 1  # two folder project ids -> one project with two phases
    with db.connect() as conn:
        types = {r["type"]: r["n"] for r in conn.execute(
            "SELECT type, count(*) AS n FROM aec.kg_nodes WHERE project_key=%s GROUP BY 1", (key,))}
        assert types["Phase"] == 2 and types["Drawing"] == 3
        spaces = {r["name"]: r for r in conn.execute(
            "SELECT name, props, object_ids FROM aec.kg_nodes WHERE project_key=%s AND type='Space'", (key,))}
        merged = [s for s in spaces.values() if s["props"]["room_key"] == "회의실1"]
        assert len(merged) == 1 and len(merged[0]["object_ids"]) == 2  # 회의실 1 + 회의실1 -> one Space
        sup = conn.execute("SELECT src, dst FROM aec.kg_edges WHERE project_key=%s AND predicate='supersedes'",
                           (key,)).fetchall()
        assert [(r["src"], r["dst"]) for r in sup] == [(f"kg:d:{docs[1][0]}", f"kg:d:{docs[0][0]}")]
        sec = conn.execute("SELECT props FROM aec.kg_nodes WHERE project_key=%s AND type='SteelSection'",
                           (key,)).fetchone()
        assert sec["props"]["member_kinds"] == {"Beam": 1}
    assert KnowledgeGraphBuilder(db).build(key)["skipped"] == 1  # unchanged fingerprint


def test_ask_routes_cites_and_refuses(seeded):
    from aec_intelligence.operational.graphrag.ask import REFUSAL, GraphRAG
    from aec_intelligence.operational.graphrag.kg import KnowledgeGraphBuilder

    db, key, docs = seeded
    KnowledgeGraphBuilder(db).build(key, force=True)
    rag = GraphRAG(db, settings=None, llm=FakeLLM("1층에는 회의실 1이 있습니다 [C1]. 근거 없는 번호 [C99]."))
    res = rag.ask(f"그래프시험{RUN} 1층 실 목록 알려줘")
    assert res["route"] == "graph:storey" and not res["refused"]
    assert res["citations"] and res["citations"][0]["id"] == "C1" and "[C99]" not in res["answer"]
    assert res["citations"][0]["document_id"] in {d[0] for d in docs}

    res = rag.ask("H-300x150x6.5x9 단면은 어디에 쓰였나", project=key, generate=False)
    assert res["route"] == "graph:section" and res["answer_mode"] == "extractive"
    cited = res["citations"][0]
    assert cited["bbox"] and cited["object_ids"] and cited["document_id"] == docs[2][0]
    assert cited["document_ids"][0] == cited["document_id"]

    uncited = GraphRAG(db, settings=None, llm=FakeLLM("회의실이 있습니다."))
    res = uncited.ask(f"그래프시험{RUN} 1층 실 목록 알려줘")
    assert res["answer_mode"] == "extractive" and res["citations"]

    refusing = GraphRAG(db, settings=None, llm=FakeLLM(REFUSAL))
    res = refusing.ask("오늘 서울 날씨는 어때?", project=key)
    assert res["refused"] and res["citations"] == []


def test_communities_are_cached_and_resumable(seeded):
    from aec_intelligence.operational.graphrag import communities
    from aec_intelligence.operational.graphrag.kg import KnowledgeGraphBuilder

    db, key, _ = seeded
    KnowledgeGraphBuilder(db).build(key, force=True)
    first = communities.refresh(db, key)
    assert first.get("new", 0) >= 2  # project overview + at least one storey aspect
    llm = FakeLLM("요약입니다.")
    assert communities.summarize(db, llm, project_key=key, limit=1)["summarized"] == 1
    rest = communities.summarize(db, llm, project_key=key)
    assert rest["summarized"] == first["new"] - 1  # resumes where the first run stopped
    assert communities.refresh(db, key) == {"unchanged": first["new"]}
    assert communities.summarize(db, llm, project_key=key)["summarized"] == 0  # cached by input_hash


def test_mcp_gateway_graph_rag_query_and_explain_path(seeded, tmp_path, monkeypatch):
    from aec_intelligence.mcp_gateway import MCPGateway
    from aec_intelligence.operational.graphrag.kg import KnowledgeGraphBuilder

    db, key, docs = seeded
    KnowledgeGraphBuilder(db).build(key, force=True)
    monkeypatch.setenv("AEC_DATABASE_URL", DSN)
    gateway = MCPGateway(tmp_path)
    res = gateway.call_tool("aec.graph_rag_query", {"question": f"그래프시험{RUN} 1층 실 목록 알려줘", "generate": False})
    assert res["status"] == "SUCCESS" and res["route"] == "graph:storey" and res["citations"]
    node = res["citations"][0]["kg_node_id"]
    explained = gateway.call_tool("aec.explain_path", {"node_id": node})
    assert explained["status"] == "SUCCESS"
    assert gateway.call_tool("aec.explain_path", {"node_id": "kg:none"})["status"] == "NOT_FOUND"
    off = gateway.call_tool("aec.graph_rag_query", {"question": "주식 시장 전망을 알려줘", "generate": False})
    assert off["status"] == "SUCCESS" and off["refused"] and off["citations"] == []
